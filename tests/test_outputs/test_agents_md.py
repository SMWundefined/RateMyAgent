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


def _result_with(probe: str, metrics: dict) -> ScanResult:
    """A ScanResult carrying one probe's metrics and nothing else.

    Advice predicates are pure functions of the metrics, so a condition is
    clearer asserted against the dict than driven out of a whole scan.
    """
    return ScanResult(
        target=TargetInfo(name="synthetic", kind="mock"),
        probes=[ProbeResult(probe=probe, phase="behavior", metrics=metrics)],
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
    """Every advice predicate reads metrics through `_number`, so none can raise.

    **History, because the assertions below are the shape of a defect that ran
    for thirteen releases.** Probes publish `n/a` rather than dropping a row, so
    `None` is an ordinary value in a metrics dict. The predicates did not treat
    it as one, in two different ways -- both section 8b's oldest shape, a default
    that is also a legal value:

    - `.get(key, 0) > x` defaults on a **missing** key. A withheld metric is
      present and null, so the default never applied and the comparison raised
      `TypeError` into `_safe_applies`, which logged it at DEBUG and returned
      `False`. An absent recommendation is indistinguishable from one that did
      not apply, so nothing ever surfaced.
    - `(.get(key) or 1.0) < x` guarded `None` and swallowed `0.0` with it.

    Three predicates raised and one silently misfired. Two of the three and the
    misfiring one are `critical`. Only the first was found by a fixture; the rest
    came from the sweep this class now pins, which is the point -- one accessor
    removes the class, and these tests stop it growing back one `Advice` at a
    time.
    """

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

    @staticmethod
    def _with(probe: str, metrics: dict) -> ScanResult:
        return _result_with(probe, metrics)

    @pytest.mark.parametrize("profile", PROFILES)
    async def test_no_advice_predicate_raises(self, profile):
        """Was a strict xfail through 1.1.0. The marker could not outlive the fix."""
        result = await scan_mock(target=getattr(MockTarget, profile)())
        assert self._raising(result) == set()

    @pytest.mark.parametrize("key,probe,metric", [
        ("retry_amplification", "behavior", "retry_amplification"),
        ("duplicate_mutations", "behavior", "duplicate_mutations"),
        ("accepts_invalid", "contract", "accepted_invalid"),
    ])
    def test_a_withheld_metric_does_not_fire_and_does_not_raise(
        self, key, probe, metric
    ):
        """The three that raised, pinned individually by the metric that did it.

        `retry_amplification` is withheld for every target that does not run its
        own retry loop, which is every target that exists.
        `duplicate_mutations` is withheld when nothing completed.
        `accepted_invalid` is withheld when the contract evidence is too thin to
        read -- reachable today, and more reachable if `evidence_thin` widens.
        """
        advice = next(a for a in ADVICE if a.key == key)
        assert advice.applies(self._with(probe, {metric: None})) is False

    def test_retry_amplification_advice_is_reachable(self):
        """Renamed from `..._is_dead_on_every_scan`, which is what it was.

        Kept rather than deleted because the unconditional half is the behaviour
        worth naming, and because None-safety alone did not restore it. The
        metric is nulled for every target that does not run its own retry loop,
        so the repaired predicate still read the default and compared
        `0.0 > 2.0` -- a raise replaced by an honest `False`, with the
        recommendation just as absent.

        The advice is now gated on the condition it actually depends on. It is
        inapplicable to a service target *by design* rather than by a nulled key,
        and it fires for a target that owns its retry loop.
        """
        advice = next(a for a in ADVICE if a.key == "retry_amplification")
        caller = {"caller_strategy_applicable": True}

        assert advice.applies(self._with("behavior", {**caller, "retry_amplification": 3.0}))
        assert not advice.applies(self._with("behavior", {**caller, "retry_amplification": 1.5}))
        assert not advice.applies(self._with("behavior", {**caller, "retry_amplification": None}))

    async def test_retry_amplification_never_fires_for_a_service_target(self):
        """The amplification measured against a server is the scanner's own.

        The behaviour probe reports it and refuses to score it, for the reason
        its finding gives: a server does not retry, the client does. A fix guide
        telling a server's owner to retry better is addressed to nobody.
        """
        advice = next(a for a in ADVICE if a.key == "retry_amplification")
        result = await scan_mock(
            target=MockTarget.failing(), extra={"fault_rate": 0.5}, requests=20
        )
        behavior = result.probe("behavior").metrics

        assert behavior["caller_strategy_applicable"] is False
        assert behavior["caller_retry_amplification"] > 2.0, "amplification was real"
        assert not advice.applies(result), "and it is still not the target's"

    def test_poor_recovery_fires_at_exactly_zero(self):
        """The fourth defect, and the only one that was not a raise.

        `(value or 1.0) < 0.9` treats `0.0` and `None` identically because both
        are falsy -- so the recovery recommendation did not fire at a recovery
        rate of zero, which is the worst value it exists to report. A withheld
        rate still must not fire: unknown is not bad.
        """
        advice = next(a for a in ADVICE if a.key == "poor_recovery")

        assert advice.applies(self._with("behavior", {"recovery_rate": 0.0}))
        assert advice.applies(self._with("behavior", {"recovery_rate": 0.25}))
        assert not advice.applies(self._with("behavior", {"recovery_rate": 0.95}))
        assert not advice.applies(self._with("behavior", {"recovery_rate": None}))

    def test_a_real_zero_is_not_a_missing_value(self):
        """The general form of the bug above, at the accessor."""
        from ratemyagent.outputs.agents_md import _number

        assert _number(self._with("behavior", {"x": 0.0}), "behavior", "x", 1.0) == 0.0
        assert _number(self._with("behavior", {"x": None}), "behavior", "x", 1.0) == 1.0
        assert _number(self._with("behavior", {}), "behavior", "x", 1.0) == 1.0

    def test_a_non_numeric_metric_does_not_reach_the_comparison(self):
        """A predicate is not the place to find out a probe published a string."""
        from ratemyagent.outputs.agents_md import _number

        assert _number(self._with("behavior", {"x": "3"}), "behavior", "x", 0.0) == 0.0
        assert _number(self._with("behavior", {"x": True}), "behavior", "x", 0.0) == 0.0

    def test_the_guard_stays_broad_and_is_now_loud(self, caplog):
        """`ADVICE` is a data table, so one bad entry must not lose the guide.

        The breadth was never the defect -- the DEBUG level was. A dropped
        section now says so at `error`, and says it is our bug rather than the
        target's.
        """
        import logging

        from ratemyagent.outputs.agents_md import Advice, _safe_applies

        def explode(_result):
            raise RuntimeError("boom")

        broken = Advice("broken", "Broken", explode, lambda r: "", priority=99)
        with caplog.at_level(logging.ERROR, logger="ratemyagent.outputs.agents_md"):
            assert _safe_applies(broken, self._with("behavior", {})) is False

        assert "broken" in caplog.text
        assert "bug in RateMyAgent" in caplog.text
