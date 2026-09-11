"""AGENTS.md generator: sections, specificity, and re-scan deltas."""

from __future__ import annotations

import json

import pytest

from ratemyagent import Policy, scan
from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.outputs.agents_md import (
    ADVICE,
    build_state,
    read_state,
    render_agents_md,
    write_agents_md,
)
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget
from tests.conftest import BrittleTarget, ValidatingTarget


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 20, "warmup": 0, "timeout_s": 5.0, "concurrency": 8,
                "extra": {"fault_rate": 0.3}}
    return ProbeConfig(**{**defaults, **kwargs})


async def scan_mock(target=None, **kwargs):
    return await scan(target or MockTarget.healthy(), config=config(**kwargs))


def prose(document: str) -> str:
    """Collapse whitespace so assertions survive the document's line wrapping."""
    return " ".join(document.split())


def _result_with_behavior(metrics: dict) -> ScanResult:
    """A ScanResult carrying nothing but one behaviour metrics dict.

    Advice predicates are pure functions of the metrics, so a condition is
    clearer asserted against the dict than driven out of a whole scan.
    """
    return ScanResult(
        target=TargetInfo(name="synthetic", kind="mock"),
        probes=[ProbeResult(probe="behavior", phase="behavior", metrics=metrics)],
    )


class TestStructure:
    async def test_header_names_the_target_and_policy(self):
        document = render_agents_md(await scan_mock())

        assert "# AGENTS.md" in document
        assert "healthy-mock" in document
        assert "production-default" in document

    async def test_verdict_leads_the_document(self):
        document = render_agents_md(await scan_mock(MockTarget.failing()))

        assert "## Verdict" in document
        assert "FAIL: score" in document
        assert document.index("## Verdict") < document.index("things to fix")

    async def test_score_tables_are_included(self):
        document = render_agents_md(await scan_mock())

        assert "## Where the score went" in document
        assert "| dimension | points | note |" in document
        assert "| measurement | actual | target | status |" in document

    async def test_a_clean_scan_says_so_without_claiming_perfection(self):
        """Nothing to fix is a statement about what was measured, not health.

        A target that rejects malformed input, is fast, has no latency tail and
        does not saturate leaves no advice section applicable.
        """
        clean = ValidatingTarget(latency_s=0.3, jitter_s=0.05, tail_probability=0.0)
        document = render_agents_md(await scan_mock(clean))

        assert "Nothing to fix" in document
        assert "not a clean bill of health" in prose(document)

    async def test_sections_are_numbered_and_titled(self):
        document = render_agents_md(await scan_mock(MockTarget.failing()))

        assert "### 1. " in document
        assert "things to fix" in document


class TestSectionQuality:
    """Every section must do more than name the problem."""

    async def test_each_section_states_an_observation_and_a_cause(self):
        document = render_agents_md(await scan_mock(BrittleTarget()))

        text = prose(document)
        assert "**FINDING:" in text
        # A root cause, not just a recommendation.
        assert "AI-generated" in text

    async def test_fixes_are_copy_pasteable_code(self):
        document = render_agents_md(await scan_mock(BrittleTarget()))
        assert "```python" in document

    async def test_the_fix_names_the_tool_that_failed(self):
        document = render_agents_md(await scan_mock(BrittleTarget()))
        assert 'Suggested fix for tool "' in document

    async def test_unvalidated_input_section_matches_the_spec_example(self):
        """CLAUDE.md gives this section verbatim as the bar for output quality."""
        document = render_agents_md(await scan_mock())

        text = prose(document)
        assert "schema-forbidden inputs accepted" in text
        assert "the schema is correct but the handler trusts its input" in text
        assert "that is normal traffic, not an attack" in text
        assert "is required and must be a string" in text

    async def test_it_explains_production_impact_not_just_best_practice(self):
        text = prose(render_agents_md(await scan_mock(MockTarget.failing()))).lower()
        assert "in production" in text

    async def test_a_validating_target_gets_no_validation_section(self):
        document = render_agents_md(await scan_mock(ValidatingTarget()))
        assert "schema-forbidden inputs accepted" not in document

    async def test_llm_targets_are_referenced_by_model(self):
        from ratemyagent import LLMTarget
        from tests.test_targets.test_llm import FakeAnthropicClient

        target = LLMTarget("anthropic", model="claude-opus-5", client=FakeAnthropicClient())
        result = await scan(target, config=config(requests=10), policy=Policy.default())
        document = render_agents_md(result)

        assert "claude-opus-5" in document


class TestPrioritisation:
    def test_advice_keys_are_unique(self):
        keys = [advice.key for advice in ADVICE]
        assert len(keys) == len(set(keys))

    def test_correctness_issues_outrank_efficiency_ones(self):
        priority = {advice.key: advice.priority for advice in ADVICE}

        assert priority["duplicate_mutations"] < priority["slow_p95"]
        assert priority["contract_crashes"] < priority["prompt_bloat"]
        assert priority["poor_recovery"] < priority["expensive"]

    async def test_worst_problem_is_listed_first(self):
        document = render_agents_md(await scan_mock(BrittleTarget()))
        first = document.split("### 1. ")[1].split("\n")[0]

        assert "malformed input" in first or "Duplicate" in first


class TestState:
    async def test_state_round_trips(self):
        result = await scan_mock()
        document = render_agents_md(result)
        state = read_state(document)

        assert state is not None
        assert state["target"] == "healthy-mock"
        assert state["score"] == pytest.approx(result.score)

    async def test_state_is_json_serializable(self):
        state = build_state(await scan_mock())
        assert json.loads(json.dumps(state))["version"] == 1

    def test_a_document_without_state_reads_as_none(self):
        assert read_state("# AGENTS.md\n\nnothing here\n") is None

    def test_corrupt_state_does_not_raise(self):
        assert read_state("<!-- ratemyagent-state\n{not json}\n-->") is None

    def test_empty_input_reads_as_none(self):
        assert read_state("") is None


class TestDeltas:
    async def test_a_first_scan_has_no_delta_section(self):
        document = render_agents_md(await scan_mock())
        assert "## Since the last scan" not in document

    async def test_improvement_is_reported(self):
        before = render_agents_md(await scan_mock(MockTarget.failing()))
        after = render_agents_md(await scan_mock(MockTarget.failing(seed=5)), before)

        assert "## Since the last scan" in after

    async def test_metric_movement_is_named_with_both_values(self):
        slow = render_agents_md(await scan_mock(MockTarget.degraded()))
        fast = render_agents_md(await scan_mock(MockTarget.healthy()), slow)

        assert "P95 latency improved from" in prose(fast)

    async def test_a_regression_is_named_as_such(self):
        fast = render_agents_md(await scan_mock(MockTarget.healthy()))
        slow = render_agents_md(await scan_mock(MockTarget.degraded()), fast)

        assert "regressed" in slow

    async def test_an_unchanged_failing_metric_is_reported_as_still_failing(self):
        first = render_agents_md(await scan_mock())
        second = render_agents_md(await scan_mock(), first)

        assert "still failing" in second

    async def test_comparing_different_targets_is_flagged(self):
        """Otherwise latency 'improved' because it is a different server."""
        first = render_agents_md(await scan_mock(MockTarget.failing()))
        second = render_agents_md(await scan_mock(MockTarget.healthy()), first)

        assert "two different targets" in second

    async def test_a_policy_change_is_flagged(self):
        strict = Policy(name="strict", thresholds={"p95_latency_ms": 1})
        first = render_agents_md(await scan_mock())
        result = await scan(MockTarget.healthy(), config=config(), policy=strict)
        second = render_agents_md(result, first)

        assert "different policy" in second


class TestWriting:
    async def test_writes_and_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "AGENTS.md"
        write_agents_md(await scan_mock(), path)

        assert path.exists()
        assert "# AGENTS.md" in path.read_text()

    async def test_rewriting_diffs_against_what_is_there(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        write_agents_md(await scan_mock(MockTarget.degraded()), path)
        second = write_agents_md(await scan_mock(MockTarget.degraded(seed=9)), path)

        assert "## Since the last scan" in second
        assert path.read_text() == second

    async def test_an_unreadable_previous_file_does_not_stop_generation(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_text("garbage with no state block")

        document = write_agents_md(await scan_mock(), path)
        assert "# AGENTS.md" in document
        assert "## Since the last scan" not in document


class TestStateProvenance:
    """The state block is what a re-scan diffs against, so it carries the call too."""

    async def test_it_records_the_probe_call(self):
        result = await scan(MockTarget.healthy(), config=config())
        result.target.metadata = {
            "probe_tool": "create_entities",
            "probe_args": {"entities": []},
        }
        state = read_state(render_agents_md(result))

        assert state["probe_tool"] == "create_entities"
        assert state["probe_args"] == {"entities": []}

    async def test_the_keys_are_absent_rather_than_null_when_unknown(self):
        result = await scan(MockTarget.healthy(), config=config())
        result.target.metadata = {}
        state = read_state(render_agents_md(result))

        assert "probe_tool" not in state
        assert "probe_args" not in state


class TestNoAdvicePredicateRaises:
    """The invariant `_safe_applies` hides, and the predicates that break it.

    `_safe_applies` catches `Exception` so one malformed metric cannot take the
    whole guide down, and logs at DEBUG. That is a reasonable requirement and the
    guard satisfies it correctly. It also catches *structural* errors -- a
    predicate reading a key its producer stopped populating -- which are not
    tolerate-and-continue cases, and it makes them indistinguishable from a
    predicate that simply did not apply.

    Two predicates read metrics that `behavior.py` withholds as `None` rather
    than dropping, and both use `.get(key, 0)`, which defaults on *absence* and
    not on `None`. They evaluate `None > 0` and raise. The two are withheld for
    different reasons and the consequences are not equal:

    - `retry_amplification` is withheld for every target that does not run its
      own retry loop (`behavior.py:142`), which is every target that exists.
      **Its advice has therefore never fired, on any scan, since 0.1.9.**
    - `duplicate_mutations` is withheld only when nothing completed
      (`behavior.py:162`, since 0.1.10). Its advice is lost exactly on runs where
      every operation failed -- where there were no mutations to duplicate, so it
      would not have applied anyway. The raise is still a defect; the missing
      recommendation is not.

    Found by the 1.0.1 backoff fixture: an arm measuring 3.00x amplification
    produced a guide with no amplification advice in it.

    The repair turns a recommendation back on in scored output and is held for
    1.0.2 so it can be re-scanned on its own. The assertions ship now.
    """

    #: Pinned, not aspirational. Shrinking this is the 1.0.2 change; a key
    #: joining it is a regression, which is what the subset test is for.
    KNOWN_RAISING = {"duplicate_mutations", "retry_amplification"}

    PROFILES = ["healthy", "degraded", "failing", "saturating"]

    @staticmethod
    def _raising(result) -> set[str]:
        raised = set()
        for advice in ADVICE:
            try:
                advice.applies(result)
            except Exception:
                raised.add(advice.key)
        return raised

    @pytest.mark.xfail(
        strict=True,
        reason="predicates read metrics withheld as None; the fix lands in "
               "1.0.2 and this marker comes off with it",
    )
    async def test_no_advice_predicate_raises(self):
        """The real invariant, written down as failing rather than not at all.

        `strict=True` is the whole point: when 1.0.2 makes the predicates
        None-safe this XPASSes, the suite goes red, and the marker has to be
        removed. An invariant nobody can satisfy yet still belongs in the suite,
        provided the suite knows it is unsatisfied.
        """
        assert self._raising(await scan_mock(target=MockTarget.degraded())) == set()

    @pytest.mark.parametrize("profile", PROFILES)
    async def test_no_predicate_outside_the_known_two_raises(self, profile):
        """The protection that was missing for thirteen releases.

        Green today. It goes red the moment a third predicate starts raising --
        a renamed metric, a typo in a key, a bug in a new `Advice` -- which is
        the class of change that produced this one and that `_safe_applies`
        cannot report on its own.
        """
        result = await scan_mock(target=getattr(MockTarget, profile)())
        assert self._raising(result) <= self.KNOWN_RAISING

    @pytest.mark.parametrize("profile", PROFILES)
    async def test_retry_amplification_advice_is_dead_on_every_scan(self, profile):
        """Not conditional, not profile-specific: withheld for every target.

        `caller_strategy_applicable` is false for every target type that exists,
        so this is the unconditional half of the defect. 1.0.2 removing it
        should flip this test, which is why it names the behaviour rather than
        the bug.
        """
        result = await scan_mock(target=getattr(MockTarget, profile)())
        assert "retry_amplification" in self._raising(result)

    def test_duplicate_mutations_advice_is_lost_only_when_nothing_completed(self):
        """The conditional half, asserted where the condition is legible.

        Driving a whole scan to `operation_failure_rate == 1.0` to observe this
        would hide the condition inside a fixture. The predicate is a pure
        function of the metrics dict, so the two cases are stated directly.
        """
        predicate = next(a for a in ADVICE if a.key == "duplicate_mutations")

        assert predicate.applies(_result_with_behavior({"duplicate_mutations": 3}))
        with pytest.raises(TypeError):
            predicate.applies(_result_with_behavior({"duplicate_mutations": None}))

    async def test_the_swallowed_failures_read_as_not_applicable(self):
        """Why there was no later symptom to chase.

        0.1.20's broad `except` displaced a defect -- the swallowed `RuntimeError`
        resurfaced as a `CancelledError` in the next `setup()`. This one deletes
        it. `_safe_applies` returns `False`, which is an ordinary answer, and an
        absent recommendation is indistinguishable from one that did not apply.
        """
        from ratemyagent.outputs.agents_md import _safe_applies

        result = await scan_mock(target=MockTarget.degraded())
        raising = [a for a in ADVICE if a.key in self._raising(result)]
        assert raising, "nothing raised; the pinned defect is gone, update 1.0.2"
        assert all(_safe_applies(a, result) is False for a in raising)
