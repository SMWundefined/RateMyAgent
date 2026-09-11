"""The control that stops one bad payload being published as a broken server.

**The run this exists for.** A 20-request scan of `mcp-server-fetch`, with no
`--tool-args`, printed `Every one of the 20 requests failed`, `something is
broken at any load`, and **43/100**. The server was fine. `synthesize_args`
filled the required `url` with `"ratemyagent probe"` -- a string, so the schema
is satisfied -- and `fetch` rejected all twenty. The same server with real
arguments scored 89 with a 0.0% error rate.

Our defect published as theirs, which is the retraction this project already
made once (section 9, Finding 1).

**Why the existing control did not catch it.** `vacuous_required_fields` asks
the *schema* whether synthesis could fill the required fields, and it passes:
`"ratemyagent probe"` is not in `VACUOUS_DEFAULTS`. A schema-shaped guard with
no server-shaped twin.

The contract probe's `Control` did not catch it either, and is right not to. It
measures **delivery** -- its docstring says it "works with synthesized arguments
and needs no valid ones" -- and answers *is the session alive?*. Run 3 is the
case where delivery is perfect and acceptance is zero.

The three legs, one test class each below:

1. `MCPTarget._preflight` asks the server, once, at setup, and records the
   answer on `describe().metadata`.
2. The scanner refuses when a baseline-dependent probe would run on a payload
   the target already refused.
3. Latency and concurrency withhold rather than publishing a zero.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent import scan
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MCPTarget, MockTarget
from ratemyagent.targets.base import TargetError, baseline_probe_ok

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "fixtures" / "stub_mcp_server.py"

#: Every tool on the stub requires `repo_path`, and the server rejects the
#: synthesized placeholder because it is not inside `--repository`. That is the
#: run-3 condition exactly, without needing a fixture of its own.
ACCEPTED = {"repo_path": "/tmp"}


def _uri() -> str:
    return "stdio://" + shlex.join(
        [sys.executable, str(STUB), "--repository", "/tmp"]
    )


def _target(tool_args=None) -> MCPTarget:
    return MCPTarget(_uri(), tool="git_status", tool_args=tool_args, timeout_s=30)


def config(**kwargs) -> ProbeConfig:
    return ProbeConfig(**{"requests": 4, "warmup": 0, "timeout_s": 10, **kwargs})


class _SilentPreflight(MCPTarget):
    """Drops the first call only -- the preflight -- then behaves normally.

    Stands in for a transport hiccup at exactly the moment the preflight runs.
    Everything after it is the real adapter against the real stub.
    """

    _dropped = False

    async def invoke(self, request):
        from ratemyagent.models import ErrorKind, Response

        if not self._dropped:
            self._dropped = True
            return Response(
                ok=False, latency_s=0.0, error="connection reset",
                error_kind=ErrorKind.CONNECTION, delivered=False,
            )
        return await super().invoke(request)


class DeadTarget(MockTarget):
    """Every call fails, and no preflight ever ran.

    The case the withholding must not swallow: 100% failure with no evidence
    about *why*, which is what a genuinely broken server looks like.
    """

    async def invoke(self, request):
        from ratemyagent.models import ErrorKind, Response

        return Response(
            ok=False, latency_s=0.0, error="server closed the connection",
            error_kind=ErrorKind.CONNECTION,
        )


class TestThePreflightAsksTheServer:
    async def test_a_rejected_synthesized_payload_is_recorded(self):
        target = _target()
        await target.setup()
        try:
            assert baseline_probe_ok(target) is False
            assert target.describe().metadata["probe_args_source"] == "synthesized"
        finally:
            await target.teardown()

    async def test_an_accepted_payload_is_recorded(self):
        target = _target(ACCEPTED)
        await target.setup()
        try:
            assert baseline_probe_ok(target) is True
        finally:
            await target.teardown()

    async def test_setup_does_not_raise_on_a_rejection(self):
        """Recorded, never raised.

        Whether a rejection is fatal depends on which probes will run, and
        `setup()` cannot see that. Raising here would make the contract probe's
        per-case attribution -- deliberate, tested, and valuable against exactly
        these tools -- unreachable.
        """
        target = _target()
        await target.setup()
        await target.teardown()

    async def test_an_undelivered_preflight_is_undetermined(self):
        """A transport failure is not a verdict on the payload.

        Refusal is keyed on a *rejection* -- a semantic answer from a server
        that is demonstrably up, and therefore deterministic. Treating a
        dropped call the same way would turn one flaky request into a failed
        scan, and would need a preflight retry to be safe. This is the branch
        that makes the retry unnecessary.
        """
        target = _SilentPreflight(_uri(), tool="git_status", timeout_s=30)
        await target.setup()
        try:
            assert baseline_probe_ok(target) is None
        finally:
            await target.teardown()

    async def test_an_undelivered_preflight_does_not_refuse_the_scan(self):
        result = await scan(
            _SilentPreflight(_uri(), tool="git_status", timeout_s=30),
            probes="latency", config=config(),
        )
        assert result.probe("latency") is not None

    async def test_undetermined_is_none_not_false(self):
        """A target that never ran a preflight is "no evidence", not "no".

        Collapsing the third value would make every probe start withholding
        scores from targets that were never asked.
        """
        assert baseline_probe_ok(MockTarget.healthy()) is None


class TestTheScannerRefusesRatherThanScoring:
    async def test_it_refuses_when_a_baseline_dependent_probe_would_run(self):
        target = _target()
        with pytest.raises(TargetError) as caught:
            await scan(target, probes="latency", config=config())

        message = str(caught.value)
        assert "git_status" in message
        assert "--tool-args" in message, "the refusal has to name the way out"

    async def test_contract_only_still_scans(self):
        """The capability the refusal must not take away.

        `ContractTester` synthesizes a baseline per tool and attributes per
        case, so it reports real findings against a tool that rejects its own
        baseline. Refusing the whole scan to protect latency would delete that.
        """
        result = await scan(_target(), probes="contract", config=config())
        assert result.probe("contract") is not None

    async def test_user_supplied_arguments_are_not_overridden(self):
        """A person vouched for the payload. Warn, withhold, do not refuse."""
        target = MCPTarget(
            _uri(), tool="git_status", tool_args={"repo_path": "/nowhere"},
            timeout_s=30,
        )
        result = await scan(target, probes="latency", config=config())
        assert result.probe("latency") is not None

    async def test_a_working_payload_scans_normally(self):
        result = await scan(_target(ACCEPTED), probes="latency", config=config())
        latency = result.probe("latency")
        assert latency.applicable and latency.metrics["error_rate"] == 0.0


class TestTheProbesWithholdRatherThanPublishingAZero:
    async def _unusable(self, probe: str):
        target = MCPTarget(
            _uri(), tool="git_status", tool_args={"repo_path": "/nowhere"},
            timeout_s=30,
        )
        result = await scan(target, probes=probe, config=config())
        return result.probe(probe)

    @pytest.mark.parametrize("probe", ["latency", "concurrency"])
    async def test_it_is_withheld(self, probe):
        found = await self._unusable(probe)
        assert found.applicable is False
        assert found.findings == [], "a withheld probe states no findings"

    @pytest.mark.parametrize("probe", ["latency", "concurrency"])
    async def test_the_caveat_says_why(self, probe):
        found = await self._unusable(probe)
        assert [c for c in found.caveats if c.effect == "withhold"]
        assert any("rejected" in c.reason for c in found.caveats)

    @pytest.mark.parametrize("probe", ["latency", "concurrency"])
    async def test_a_target_that_really_fails_everything_still_scores(self, probe):
        """The half that must not regress, and it needs a real 100% failure.

        `error_rate == 1.0` is not the condition -- the preflight is. A target
        that genuinely fails every call has to keep scoring zero, or this fix
        becomes a way to hide an outage.

        Written against `DeadTarget` rather than `MockTarget.failing()`, which
        fails 50% of calls and never reaches the branch: the first version of
        this test asserted the right thing about a run that could not exercise
        it, which is section 8b entry 7 in miniature.
        """
        result = await scan(DeadTarget(), probes=probe, config=config())
        found = result.probe(probe)

        assert found.metrics.get("error_rate", found.metrics.get("max_error_rate")) == 1.0
        assert baseline_probe_ok(DeadTarget()) is None, "no preflight evidence"
        assert found.applicable, "a probe with no preflight evidence still scores"

    async def test_concurrency_does_not_claim_a_broken_target(self):
        """The sentence that made run 3 read as an outage."""
        found = await self._unusable("concurrency")
        assert not any("broken at any load" in f for f in found.findings)
