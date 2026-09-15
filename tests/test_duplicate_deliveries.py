"""`duplicate_mutations` is withheld, because the scanner cannot see effects.

1.3.0 scored `duplicate_mutations` from `Invocation.executed`, which records that
the target *acknowledged* a call before the proxy damaged or dropped the reply.
A retry after that is a second delivery. Whether it is a second *effect* is a
property of the target's state, which the scanner never reads.

Measured against `tests/fixtures/event_twin_mcp_server.py` at 1.3.0: an
idempotent `put` and a non-idempotent `append`, given identical faults, both
reported 2 and both were capped at 49 by `duplicate_mutation_max`. The idempotent
one had applied nothing twice. 1.3.0 would cap a correct write tool and fail it.

What these tests pin:

- neither twin is capped by `duplicate_mutation_max`, and that check is skipped;
- both report the same `duplicate_deliveries`, labelled as the scanner's own in
  every output;
- the fault schedule is forced per (operation, attempt) rather than found by
  searching seeds, and a self-check fails if any scheduled fault did not fire.

The `MALFORMED` schedule is its own case because it is the path the metric
counted most: every duplicate in the seed-42 twin scan came from a damaged
reply, not a lost one.

**No ground truth here, on purpose.** Whether `append` applied its re-sent calls
twice is established outside the instrument, by a script that does not import
this package. Reading the fixture's state through the scanner would be the
scanner confirming itself -- entry 23's rule, and the reason the fixture it
built could only ever agree.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections import Counter
from pathlib import Path

import pytest

from ratemyagent import Policy, scan
from ratemyagent.models import FaultKind
from ratemyagent.outputs.agents_md import render_agents_md
from ratemyagent.outputs.report import render_report
from ratemyagent.outputs.scorecard import render_scorecard
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes import fault as fault_probe
from ratemyagent.targets import MCPTarget
from ratemyagent.targets.fault_proxy import FaultProxy

ROOT = Path(__file__).resolve().parents[1]
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TOOL = "event"
ARGS = {"id": "evt-1", "payload": "p"}

#: `requests=3, warmup=0`: the degradation pass sends `event#0..#2` once each,
#: and the recovery pass -- the one that retries, and the one behaviour reads --
#: sends `event#3..#5`.
CONFIG = {"requests": 3, "warmup": 0, "timeout_s": 20.0, "seed": 1,
          "extra": {"fault_rate": 0.3}}

SCHEDULES: dict[str, dict[tuple[str, int], FaultKind]] = {
    # Lost replies. event#3: lost, then clean -> one re-delivery. event#4: lost
    # twice, then clean -> two. Three in all.
    "response_lost": {
        (f"{TOOL}#3", 1): FaultKind.RESPONSE_LOST,
        (f"{TOOL}#4", 1): FaultKind.RESPONSE_LOST,
        (f"{TOOL}#4", 2): FaultKind.RESPONSE_LOST,
    },
    # Damaged replies. event#4's second attempt is refused before it reaches
    # the target, so it is not a delivery: two in all, not three.
    "malformed": {
        (f"{TOOL}#3", 1): FaultKind.MALFORMED,
        (f"{TOOL}#4", 1): FaultKind.MALFORMED,
        (f"{TOOL}#4", 2): FaultKind.CONNECTION_REFUSED,
    },
}
EXPECTED_DELIVERIES = {"response_lost": 3, "malformed": 2}


class ScheduledFaultProxy(FaultProxy):
    """Injects exactly the faults named, on exactly the attempts named.

    Still a `FaultProxy`, so injection stays in the one place it is allowed; only
    the choice is fixed instead of drawn. Records what it consumed so a schedule
    that silently never applied fails the self-check rather than passing every
    other assertion on an unfaulted run.
    """

    def __init__(self, inner, faults, schedule):
        super().__init__(inner, faults)
        self.schedule = dict(schedule)
        self.consumed: set[tuple[str, int]] = set()

    def _choose_fault(self, request, attempt):
        key = (request.trajectory_key, attempt)
        if key in self.schedule:
            self.consumed.add(key)
        return self.schedule.get(key)


_RUNS: dict[tuple[str, str], tuple] = {}


async def _run(mode: str, schedule: str):
    """One real scan of one twin under one schedule, memoised across tests."""
    if (mode, schedule) in _RUNS:
        return _RUNS[mode, schedule]

    built: list[ScheduledFaultProxy] = []

    def build(inner, faults):
        proxy = ScheduledFaultProxy(inner, faults, SCHEDULES[schedule])
        built.append(proxy)
        return proxy

    original = fault_probe.FaultProxy
    fault_probe.FaultProxy = build
    try:
        uri = "stdio://" + shlex.join([sys.executable, str(TWIN), "--mode", mode])
        # Handed over unopened: `scan()` owns setup and teardown. Opening it here
        # as well entered the MCP session twice, from two tasks.
        target = MCPTarget(
            uri, tool=TOOL, tool_args=ARGS, allow_mutating=True, timeout_s=20,
        )
        result = await scan(
            target, probes=["latency", "fault", "behavior"],
            config=ProbeConfig(**CONFIG), policy=Policy.default(),
        )
    finally:
        fault_probe.FaultProxy = original

    assert len(built) == 1, f"expected one fault proxy, the scan built {len(built)}"
    _RUNS[mode, schedule] = (result, built[0])
    return _RUNS[mode, schedule]


def _behavior(result) -> dict:
    return result.probe("behavior").metrics


def _check(result, name: str):
    return next(check for check in result.checks if check.name == name)


@pytest.mark.parametrize("schedule", sorted(SCHEDULES))
@pytest.mark.parametrize("mode", ["append", "put"])
class TestNeitherTwinIsCapped:
    """The deliberate failing case. Red against 1.3.0 on every parameter."""

    async def test_duplicate_mutation_max_does_not_cap_the_score(self, mode, schedule):
        result, _ = await _run(mode, schedule)

        assert "duplicate_mutation_max" not in (result.cap_reason or ""), result.cap_reason
        assert _check(result, "duplicate_mutation_max").skipped
        assert result.score is not None and result.score > Policy.default().absolute_fail_cap
        assert result.passed is True, [c.name for c in result.checks
                                       if not c.passed and not c.skipped]

    async def test_the_metric_is_withheld_and_says_why(self, mode, schedule):
        result, _ = await _run(mode, schedule)

        assert _behavior(result)["duplicate_mutations"] is None
        reasons = [
            caveat.reason for caveat in result.probe("behavior").caveats
            if "duplicate_mutations" in caveat.metrics and caveat.effect == "suppress"
        ]
        assert any("delivered calls, not applied effects" in r for r in reasons), reasons


@pytest.mark.parametrize("schedule", sorted(SCHEDULES))
class TestTheScannerCannotTellTheTwinsApart:
    async def test_the_schedule_was_applied_exactly(self, schedule):
        """Denominator first: a schedule that did not fire proves nothing."""
        expected = Counter(kind.value for kind in SCHEDULES[schedule].values())
        for mode in ("append", "put"):
            _, proxy = await _run(mode, schedule)
            assert proxy.consumed == set(SCHEDULES[schedule]), (
                f"{mode}: scheduled faults that never fired: "
                f"{set(SCHEDULES[schedule]) - proxy.consumed}"
            )
            assert proxy.injected_by_kind() == dict(expected)

    async def test_both_report_equal_duplicate_deliveries(self, schedule):
        append, _ = await _run("append", schedule)
        put, _ = await _run("put", schedule)

        assert (
            _behavior(append)["duplicate_deliveries"]
            == _behavior(put)["duplicate_deliveries"]
            == EXPECTED_DELIVERIES[schedule]
        )
        assert append.score == put.score

    async def test_everything_the_behaviour_probe_reports_is_identical(self, schedule):
        """Nothing the scanner can observe differs, so nothing it reports may.

        Wall-clock latencies are the only exclusion: they describe this machine,
        not either twin.
        """
        append, _ = await _run("append", schedule)
        put, _ = await _run("put", schedule)

        def comparable(result):
            return {k: v for k, v in _behavior(result).items()
                    if not k.endswith("latency_s")}

        assert comparable(append) == comparable(put)


@pytest.mark.parametrize("schedule", sorted(SCHEDULES))
class TestLabelledAsOursEverywhere:
    async def test_terminal_report_agents_md_and_json(self, schedule):
        n = EXPECTED_DELIVERIES[schedule]
        for mode in ("append", "put"):
            result, _ = await _run(mode, schedule)

            assert f"{n} duplicate deliveries (ours)" in render_scorecard(result)
            assert f"{n} duplicate deliveries (ours)" in render_report(result)
            assert f"Duplicate deliveries: {n} (ours)" in render_agents_md(result)

            exported = json.loads(json.dumps(result.to_dict()))
            behavior = next(p for p in exported["probes"] if p["probe"] == "behavior")
            assert behavior["metrics"]["duplicate_deliveries"] == n
            assert behavior["metrics"]["duplicate_mutations"] is None

    async def test_the_fix_guide_does_not_prescribe_idempotency(self, schedule):
        """The idempotent twin must not be told to make itself idempotent.

        A coding agent acts on AGENTS.md. 1.3.0 put "Duplicate mutations" first,
        marked critical, with a patch adding an idempotency key -- in the guide
        for a tool that already was.
        """
        result, _ = await _run("put", schedule)
        guide = render_agents_md(result)

        assert "Duplicate mutations" not in guide.split("## Reported, not scored")[0]
        assert "succeeded more than once" not in guide
        assert "idempotency_key" not in guide
        assert "Do not change the tool on the strength of this number" in guide
