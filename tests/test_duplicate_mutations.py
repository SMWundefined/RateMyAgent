"""The check that capped the composite at 49 and could not fail.

`duplicate_mutations` has been scored since week two as an **absolute** rule --
any non-zero caps the score at 49, the harshest gate in the policy -- and it had
never once fired. Structurally zero across 25 profile/rate pairs, for two
reasons that had to be fixed together:

1. `Trajectory.duplicates` counted repeated **successes**, and the retry loop
   breaks on the first success, so a trajectory has at most one `ok=True`
   invocation. There was never a second one to find.
2. `Invocation.ok` conflated *the target ran this* with *the caller saw it
   succeed*, so the case that produces duplicate work -- a mutation that
   executed and whose reply was lost -- read as a plain failure.

The second is the interesting half. **The condition was already occurring.** An
injected `MALFORMED` fault damages a reply the target produced successfully, and
60 operations at a 0.4 fault rate contained four of them. All four scored zero.
So the fault was half-present the whole time and only the field was missing.

`RESPONSE_LOST` adds the canonical form -- the reply is dropped rather than
damaged, so the caller has no evidence either way, which is the strictly harder
case and the one at-least-once delivery is about.

**The server is the oracle here, not the metric.** Every other assertion in this
suite can be made from the scanner's own output, because the claim is about
something the scanner observed. This claim is that the target did something the
caller could not see, so confirming it from the caller's own metrics would be
circular. `counting_mcp_server.py` keeps a ledger and reports it through a
read-only tool; the tests below assert against that ledger first.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind, Invocation, Request, Response, Trajectory
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.targets import MCPTarget, MockTarget
from ratemyagent.targets.fault_proxy import (
    ALL_FAULTS,
    OPT_IN_FAULTS,
    FaultConfig,
    FaultProxy,
)

ROOT = Path(__file__).resolve().parents[1]
COUNTER = ROOT / "tests" / "fixtures" / "counting_mcp_server.py"


def _uri(*extra: str) -> str:
    return "stdio://" + shlex.join([sys.executable, str(COUNTER), *extra])


async def _ledger(target: MCPTarget) -> int:
    """Ask the server how many times it actually ran the mutation."""
    response = await target.invoke(
        Request(op="executions", payload={}, timeout_s=20, label="oracle")
    )
    assert response.ok, response.error
    return int(str(response.output).strip().splitlines()[-1].strip(' "}'))


class TestTheServerIsTheOracle:
    """End to end, against a ledger the scanner cannot see or fake."""

    async def test_a_lost_reply_makes_the_target_run_it_twice(self):
        target = MCPTarget(
            _uri(), tool="append", tool_args={"item": "x"},
            allow_mutating=True, timeout_s=20,
        )
        await target.setup()
        try:
            before = await _ledger(target)
            proxy = FaultProxy(
                target,
                FaultConfig(rates={FaultKind.RESPONSE_LOST: 1.0}, seed=1),
            )
            request = Request(op="append", payload={"item": "x"},
                              timeout_s=20, label="append#0")

            first = await proxy.invoke(request)
            assert not first.ok, "the caller must see a failure"
            assert not first.delivered, "nothing came back"

            proxy.faults = FaultConfig(rates={}, seed=1)
            second = await proxy.invoke(request)
            assert second.ok, "the retry succeeds"

            executed = await _ledger(target) - before
            trajectory = proxy.trajectories[request.trajectory_key]

            assert executed == 2, "the server ran the mutation twice"
            assert trajectory.duplicates == 1, "and the metric now says so"
            assert trajectory.duplicate_opportunities == 1
        finally:
            await target.teardown()

    async def test_the_old_rule_would_have_reported_nothing(self):
        """Pins the defect rather than only the fix.

        One `ok` in the trajectory, so counting repeated successes finds zero --
        against a server that demonstrably ran the mutation twice.
        """
        target = MCPTarget(
            _uri(), tool="append", tool_args={"item": "y"},
            allow_mutating=True, timeout_s=20,
        )
        await target.setup()
        try:
            before = await _ledger(target)
            proxy = FaultProxy(
                target, FaultConfig(rates={FaultKind.RESPONSE_LOST: 1.0}, seed=2)
            )
            request = Request(op="append", payload={"item": "y"},
                              timeout_s=20, label="append#0")
            await proxy.invoke(request)
            proxy.faults = FaultConfig(rates={}, seed=2)
            await proxy.invoke(request)

            trajectory = proxy.trajectories[request.trajectory_key]
            old_rule = sum(1 for inv in trajectory.invocations if inv.ok) - 1

            assert await _ledger(target) - before == 2
            assert old_rule == 0, "what the check reported for twelve releases"
            assert trajectory.duplicates == 1
        finally:
            await target.teardown()


class TestExecutedIsThreeValued:
    async def test_a_rejected_call_never_reached_the_target(self):
        """`False` is a fact here: the proxy answered without calling through."""
        async with MockTarget.healthy() as inner:
            proxy = FaultProxy(
                inner, FaultConfig(rates={FaultKind.CONNECTION_REFUSED: 1.0}, seed=3)
            )
            await proxy.invoke(inner.sample_request(0))

        assert proxy.invocations[0].executed is False

    async def test_a_real_failure_is_unknown_not_false(self):
        """The distributed-systems problem, recorded rather than guessed at.

        A timeout from a real server may have completed the work and lost the
        reply, or never started. A boolean would have to assert one of those
        about every real failure.
        """
        async with MockTarget.failing() as inner:
            proxy = FaultProxy(inner, FaultConfig(rates={}, seed=4))
            for i in range(20):
                await proxy.invoke(inner.sample_request(i))

        failures = [inv for inv in proxy.invocations if not inv.ok]
        assert failures, "the failing profile must produce failures"
        assert all(inv.executed is None for inv in failures)

    async def test_a_success_ran(self):
        async with MockTarget.healthy() as inner:
            proxy = FaultProxy(inner, FaultConfig(rates={}, seed=5))
            await proxy.invoke(inner.sample_request(0))

        assert proxy.invocations[0].executed is True

    async def test_a_damaged_reply_still_ran(self):
        """MALFORMED was always an execute-then-fail fault. Nothing recorded it."""
        async with MockTarget.healthy() as inner:
            proxy = FaultProxy(
                inner, FaultConfig(rates={FaultKind.MALFORMED: 1.0}, seed=6)
            )
            await proxy.invoke(inner.sample_request(0))

        invocation = proxy.invocations[0]
        assert invocation.ok is False
        assert invocation.executed is True

    def test_the_field_survives_the_json_export(self):
        invocation = Invocation(
            sequence=0, op="op", fingerprint="op:a", trajectory_id="t",
            attempt=1, ok=False, latency_s=0.0, started_at=0.0, executed=True,
        )
        assert invocation.to_dict()["executed"] is True
        assert Trajectory("t", [invocation]).to_dict()["invocations"][0]["executed"]


class TestTheOptInFaultIsOptIn:
    async def test_it_is_absent_without_allow_mutating(self):
        """Adding it to every scan would re-assign every seeded draw ever made."""
        config = FaultInjector()._faults_from(
            ProbeConfig(requests=4, extra={"fault_rate": 0.2}), MockTarget.healthy()
        )
        assert set(config.rates) == set(ALL_FAULTS)

    async def test_it_is_present_when_the_scan_may_mutate(self):
        target = MockTarget.healthy()
        target.allow_mutating = True
        config = FaultInjector()._faults_from(
            ProbeConfig(requests=4, extra={"fault_rate": 0.2}), target
        )
        assert set(config.rates) == set(ALL_FAULTS) | set(OPT_IN_FAULTS)
        assert config.total_rate == pytest.approx(0.2), "not more hostile, just different"

    async def test_a_lost_reply_is_not_delivered(self):
        """Unlike a damaged one. The caller has no evidence either way, which is
        what makes it the harder case and the one worth modelling."""
        async with MockTarget.healthy() as inner:
            proxy = FaultProxy(
                inner, FaultConfig(rates={FaultKind.RESPONSE_LOST: 1.0}, seed=7)
            )
            response = await proxy.invoke(inner.sample_request(0))

        assert response.ok is False
        assert response.delivered is False
        assert proxy.invocations[0].executed is True

    async def test_an_already_failed_call_is_left_alone(self):
        """Overwriting a real observation with a synthetic one loses the more
        interesting of the two -- `_corrupt`'s rule, applied here too."""
        failed = Response(ok=False, latency_s=0.1, error="real failure")
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig(rates={}, seed=8))

        assert proxy._lose(failed) is failed


class TestTheFrozenFieldStaysFrozen:
    """Step 3 of the promotion procedure in docs/API-STABILITY.md."""

    def test_executed_is_documented(self):
        doc = (ROOT / "docs" / "API-STABILITY.md").read_text()
        assert "`Invocation.executed`" in doc

    def test_the_fault_sets_partition_the_enum(self):
        """A new FaultKind cannot land in the default set by accident.

        Placing it there moves every cumulative threshold in `_choose_fault`,
        which re-assigns every seeded draw in every scan ever recorded. This
        test makes that a decision rather than a side effect.
        """
        assert set(ALL_FAULTS) | set(OPT_IN_FAULTS) == set(FaultKind)
        assert not set(ALL_FAULTS) & set(OPT_IN_FAULTS)
