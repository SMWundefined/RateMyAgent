"""What an LLM agent is not entitled to have scored.

Three of Phase C's six reported metrics are properties of a **retry loop**, and
a model deciding does not have one. The split is not cosmetic:

- `retry_amplification` is **reported and unscored**. Its parts are real --
  `calls_under_fault` and `clean_path_calls` are counts of calls that happened.
  The ratio is not, because the denominator was measured on the clean pass where
  no fault was injected, and a model's clean pass is a random variable.
- `backoff_shape`, `backoff_growth` and `retry_after_honored` are **withheld**,
  which is stronger. A wall-clock gap produced by a retry loop is a schedule;
  one produced by a model is an inference round trip, and nothing in the record
  tells waiting from thinking.

**None of it touches a scripted agent**, which is what the shipped fixtures are
and what every Phase C gate result was measured on. The kind is declared, never
sniffed -- see `AGENT_KIND_SCRIPTED` for why every observable candidate was
rejected.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.outputs.scorecard import render_scorecard
from ratemyagent.policy import Policy
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.behavior import LLM_WITHHELD_METRICS, BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.scanner import scan
from ratemyagent.targets import AgentTarget, TargetError

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"

#: Two delivered failures in a row on each task: two waits to compare, which is
#: what makes a backoff shape measurable at all. Borrowed from the gate suite so
#: the arms below differ only in the declared kind.
BACKOFF = {(t, "event", n): FaultKind.SERVER_ERROR
           for t in ("t1", "t2") for n in (1, 2)}

#: One rate limit per task, each carrying the injected hint. A **separate**
#: schedule, because `BACKOFF` produces no rate limits at all -- so under it
#: `retry_after_honored` is `None` before anything is withheld, and a mutation
#: that re-reported it changed nothing and survived. A withholding test needs a
#: run that had something to withhold.
RATE_LIMITED = {("t1", "event", 1): FaultKind.RATE_LIMIT,
                ("t2", "event", 1): FaultKind.RATE_LIMIT}


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


async def _run(work: Path, kind: str, schedule: dict | None = None):
    target = AgentTarget(
        agent_command=_agent("careful_agent.py"),
        tasks_path=TASKS,
        upstream="stdio://" + shlex.join([
            sys.executable, str(TWIN), "--mode", "append",
            "--state", str(work / "state.jsonl"),
        ]),
        work_dir=work / "work",
        allow_mutating=True,
        verify_tool="effects",
        verify_count="entries",
        agent_kind=kind,
    )
    result = await scan(
        target,
        probes=[AgentBaseline(), FaultInjector(schedule=schedule or BACKOFF),
                BehaviorAnalyzer()],
        policy=Policy.default(),
    )
    return result, result.probe("behavior").metrics


@pytest.fixture(scope="module")
def arms(tmp_path_factory):
    """The identical fixture agent under the identical schedule, twice.

    Everything that differs below is the declared kind and nothing else, which
    is what makes this a test of the rule rather than of two agents.
    """
    async def both():
        out = {}
        for kind in ("scripted", "llm"):
            out[kind] = await _run(tmp_path_factory.mktemp(kind), kind)
        for kind in ("scripted", "llm"):
            out[f"{kind}_limited"] = await _run(
                tmp_path_factory.mktemp(f"{kind}_limited"), kind, RATE_LIMITED
            )
        return out
    return asyncio.run(both())


class TestAScriptedAgentIsUnchanged:
    """The Phase C gate results, asserted here as well as in their own suite."""

    def test_amplification_is_still_scored(self, arms):
        result, metrics = arms["scripted"]
        assert metrics["retry_amplification"] is not None
        check = next(c for c in result.checks if c.name == "retry_amplification_max")
        assert not check.skipped

    def test_the_timing_metrics_are_still_reported(self, arms):
        _, metrics = arms["scripted"]
        assert metrics["backoff_shape"] == "growing"
        assert metrics["backoff_growth"] is not None

    def test_the_kind_is_recorded_even_when_it_changes_nothing(self, arms):
        _, metrics = arms["scripted"]
        assert metrics["agent_kind"] == "scripted"


class TestAnLlmAgentIsNotScoredOnItsRetryLoop:
    def test_amplification_is_withheld_from_the_policy(self, arms):
        """Mutation: re-score it and this fails."""
        result, metrics = arms["llm"]
        assert metrics["retry_amplification"] is None
        check = next(c for c in result.checks if c.name == "retry_amplification_max")
        assert check.skipped

    def test_but_it_is_still_reported(self, arms):
        """Unscored is not unmeasured. The ratio is published under its own
        name so a reader can see the number the scan declined to grade."""
        _, metrics = arms["llm"]
        assert metrics["unscored_retry_amplification"] is not None
        assert metrics["amplification_unscored_reason"] == "llm_denominator"

    def test_the_raw_counts_survive_as_raw_counts(self, arms):
        """Calls that happened are facts; only the ratio built from them is
        the thing that cannot be trusted."""
        _, metrics = arms["llm"]
        scripted = arms["scripted"][1]
        assert metrics["calls_under_fault"] == scripted["calls_under_fault"]
        assert metrics["clean_path_calls"] == scripted["clean_path_calls"]
        assert metrics["amplification_denominator"] == "clean_path_calls"

    @pytest.mark.parametrize("metric", LLM_WITHHELD_METRICS)
    def test_each_timing_metric_is_withheld(self, arms, metric):
        """Mutation: re-report any one of these and this fails."""
        _, metrics = arms["llm"]
        assert metrics[metric] is None

    def test_the_withheld_values_are_not_stashed_under_another_key(self, arms):
        """**Withheld, not unscored.** Publishing the shape under
        `unscored_backoff_shape` would invite exactly the reading we are saying
        is unavailable."""
        _, metrics = arms["llm"]
        for metric in LLM_WITHHELD_METRICS:
            assert f"unscored_{metric}" not in metrics
            assert f"caller_{metric}" not in metrics

    def test_the_scripted_arm_proves_there_was_something_to_withhold(self, arms):
        """Without this the test above passes on a run that measured nothing.
        The identical agent under the identical schedule *did* produce a shape."""
        assert arms["scripted"][1]["backoff_shape"] is not None

    def test_retry_after_is_withheld_on_a_run_that_had_one_to_report(self, arms):
        """**The gap the first version of this suite had.** Under `BACKOFF`
        there are no rate limits, so `retry_after_honored` is `None` before
        anything is withheld -- and a mutation that re-reported it survived all
        twenty tests. Under `RATE_LIMITED` the scripted twin reports 100%, so
        there is something for the LLM arm to be withholding."""
        assert arms["scripted_limited"][1]["retry_after_honored"] == 1.0
        assert arms["scripted_limited"][1]["retry_after_honored_count"] == 2
        assert arms["llm_limited"][1]["retry_after_honored"] is None
        assert arms["llm_limited"][1]["retry_after_honored_count"] is None

    def test_the_hinted_retry_count_survives_as_a_raw_count(self, arms):
        """How many retries followed a hint is an observation. Whether they
        honored it is the reading we decline to make."""
        assert arms["llm_limited"][1]["retry_after_retries"] == 2


class TestTheReasonGivenIsTheRealOne:
    def test_the_caveat_names_inference_latency(self, arms):
        result, _ = arms["llm"]
        reasons = " ".join(
            c.reason for c in result.caveats() if "backoff_shape" in c.metrics
        )
        assert "inference round trip" in reasons
        assert "tells waiting from thinking" in reasons

    def test_it_does_not_claim_there_was_nothing_to_measure(self, arms):
        """The pre-existing suppression says "no two consecutive waits were
        long enough to measure", which is a statement about what the run saw.
        Here the run saw them and we declined to read them -- the number is
        gone either way, and the sentence beside it is the only thing that
        says which."""
        result, _ = arms["llm"]
        reasons = " ".join(
            c.reason for c in result.caveats() if "backoff_shape" in c.metrics
        )
        assert "long enough to measure" not in reasons

    def test_the_amplification_caveat_explains_the_denominator(self, arms):
        result, _ = arms["llm"]
        reasons = " ".join(
            c.reason for c in result.caveats() if "retry_amplification" in c.metrics
        )
        assert "clean-pass call count" in reasons
        assert "tools/list" in reasons

    def test_an_llm_agent_is_not_told_it_lacks_a_retry_loop(self, arms):
        """It has one -- a model deciding. What it lacks is a *schedule*."""
        result, _ = arms["llm"]
        reasons = " ".join(c.reason for c in result.caveats())
        assert "does not declare its own retry loop" not in reasons


class TestWhatThePrintedRowsSay:
    def test_the_notes_say_why_the_rows_are_empty(self, arms):
        printed = " ".join(render_scorecard(arms["llm"][0]).split())
        assert "retry amplification n/a unscored (llm)" in printed
        assert "backoff shape n/a withheld (llm)" in printed
        assert "retry-after honored n/a withheld (llm)" in printed

    def test_a_scripted_scan_prints_what_it_always_did(self, arms):
        printed = " ".join(render_scorecard(arms["scripted"][0]).split())
        assert "withheld (llm)" not in printed
        assert "retry amplification" in printed


class TestTheKindIsDeclared:
    def test_an_unknown_kind_refuses_rather_than_defaulting(self, tmp_path):
        """A silent fallback to `scripted` would score an LLM's retry loop on
        the strength of a typo."""
        with pytest.raises(TargetError) as caught:
            AgentTarget(
                agent_command="true", tasks_path=TASKS, upstream="stdio://true",
                agent_kind="model",
            )
        assert "not one of scripted, llm" in str(caught.value)

    def test_the_default_is_scripted(self, tmp_path):
        target = AgentTarget(
            agent_command="true", tasks_path=TASKS, upstream="stdio://true",
        )
        assert target.agent_kind == "scripted"

    def test_it_reaches_the_export(self, arms):
        """A consumer has to be able to tell a run whose timing metrics were
        withheld from one that had none to report."""
        result, _ = arms["llm"]
        assert result.target.metadata["agent_kind"] == "llm"
