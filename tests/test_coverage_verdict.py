"""What a scan measured, separated from what it declined to look for.

**The run this exists for.** `--probes contract` against `mcp-server-fetch`
printed:

    Probes: 1/1 complete
    contract        15/15
    latency         -/20      (probe did not run)
    Score: 100/100  (policy production-default)
    PASS: score 100 meets pass threshold 75.

Five of six probes never started, the contract caveat said 7 of 12 cases were
not counted, and every number on the page was correct. `_weighted_score`
renormalises over measured dimensions, so one dimension at 15/15 is 100.

Four things were wrong with it, and they are four separate defects:

1. `probe did not run` covered a `--probes` exclusion, a `--phases` exclusion, a
   crash, and a probe missing from the build. `scan()` recorded results and
   never recorded what was asked for.
2. That reason reached consumers only as English prose in `note`.
3. The composite claimed a verdict over 18% of the policy.
4. `Probes: 1/1 complete` used the probes that ran as its own denominator, so a
   partial scan reported itself complete.
"""

from __future__ import annotations

import pytest

from ratemyagent import Policy, scan
from ratemyagent.policy import MINIMUM_MEASURED_WEIGHT, _graded_weight
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget


def config(**kwargs) -> ProbeConfig:
    return ProbeConfig(**{"requests": 8, "warmup": 0, "concurrency": 4, **kwargs})


async def run(probes=None, policy=None, profile="healthy"):
    return await scan(
        getattr(MockTarget, profile)(), probes=probes, policy=policy,
        config=config(),
    )


def dimension(result, name):
    return next(d for d in result.breakdown if d.probe == name)


class TestTheScanRecordsWhatWasAskedFor:
    async def test_the_selection_is_recorded(self):
        result = await run(probes="latency,contract")
        assert sorted(result.config["probes"]) == ["contract", "latency"]

    async def test_phase_exclusion_is_recorded_separately(self):
        """`probes` is what was chosen; `probes_expected` is what a phase runs.

        The gap between them is a user choice too, and lumping it in with
        "crashed" repeats the conflation one level down.
        """
        result = await scan(
            MockTarget.healthy(), probes="latency,behavior", phases="baseline",
            config=config(),
        )
        assert "behavior" in result.config["probes"]
        assert "behavior" not in result.config["probes_expected"]


class TestTheReasonIsACategoryNotProse:
    async def test_not_selected(self):
        result = await run(probes="contract")
        assert dimension(result, "latency").not_scored == "not_selected"

    async def test_phase_excluded(self):
        result = await scan(
            MockTarget.healthy(), probes="latency,behavior", phases="baseline",
            config=config(),
        )
        assert dimension(result, "behavior").not_scored == "phase_excluded"

    async def test_not_applicable_is_a_fact_about_the_target(self):
        """Cost is n/a because this target reports no price -- not a gap."""
        result = await run()
        assert dimension(result, "cost").not_scored == "not_applicable"

    async def test_no_threshold_is_a_fact_about_the_policy(self):
        result = await run()
        assert dimension(result, "concurrency").not_scored == "no_threshold"

    async def test_a_scored_dimension_has_no_category(self):
        result = await run()
        assert dimension(result, "latency").not_scored is None

    async def test_it_survives_the_json_export(self):
        """The whole point: a consumer must not have to match English."""
        result = await run(probes="contract")
        exported = {d["probe"]: d["not_scored"] for d in
                    (dim.to_dict() for dim in result.breakdown)}
        assert exported["latency"] == "not_selected"
        assert exported["contract"] is None

    async def test_the_prose_is_derived_from_the_category(self):
        """One table, so the two cannot drift -- they used to be one string."""
        from ratemyagent.policy import NOT_SCORED_NOTES

        result = await run(probes="contract")
        for dim in result.breakdown:
            if dim.not_scored is not None:
                assert dim.note == NOT_SCORED_NOTES[dim.not_scored]

    async def test_a_result_with_no_recorded_selection_still_renders(self):
        """Every scan before 1.1.0 looks like this. Never claim more than is
        recorded: an absent probe falls back to what the old string said."""
        result = await run(probes="contract")
        result.config.pop("probes")
        result.config.pop("probes_expected")
        reevaluated = Policy.default()
        from ratemyagent.policy import evaluate

        again = evaluate(result, reevaluated)
        assert dimension(again, "latency").not_scored == "did_not_run"


class TestAPartialScanDeclinesAVerdict:
    async def test_a_full_scan_still_gets_one(self):
        """The bar has to clear what this tool actually produces.

        A clean six-probe run measures 70 of the default policy's 85 graded
        weight -- cost is n/a on every real target. Any threshold above 0.82
        would decline every scan ever run, which is the Wilson mistake.
        """
        result = await run()
        assert result.passed is not None

    @pytest.mark.parametrize("probes", ["contract", "latency", "latency,contract"])
    async def test_a_partial_scan_does_not(self, probes):
        result = await run(probes=probes)
        assert result.passed is None

    async def test_the_score_is_kept(self):
        """Declines the verdict, never the score or the findings.

        `--probes contract` is a useful command -- it is how the run-3 refusal
        is worked around -- and this must not turn it into an error.
        """
        result = await run(probes="contract")
        assert result.score is not None
        assert result.probe("contract") is not None
        assert result.breakdown, "the breakdown survives too"

    async def test_the_verdict_line_says_what_it_measured(self):
        from ratemyagent.outputs.common import verdict_lines

        lines = " ".join(verdict_lines(await run(probes="contract")))
        assert "NO VERDICT" in lines
        assert "15 of 85" in lines, "quote the same denominator the rule used"

    async def test_a_narrow_policy_is_complete_not_partial(self):
        """The failure the first version of this rule caused.

        A policy asking only about p95 latency is a whole policy for whoever
        wrote it. Dividing by a fixed 100 declined it, which punishes a user for
        the policy they chose. The denominator is what the policy asks about.
        """
        narrow = Policy(name="narrow", thresholds={"p95_latency_ms": 60000},
                        pass_score=10)
        result = await run(probes="latency", policy=narrow)

        assert _graded_weight(narrow) == 20.0
        assert result.passed is True

    async def test_the_threshold_is_a_stated_judgement(self):
        assert MINIMUM_MEASURED_WEIGHT == 0.5


class TestTheHeaderCountsWhatShouldHaveRun:
    async def test_a_partial_scan_is_not_reported_complete(self):
        from ratemyagent.outputs.scorecard import render_scorecard

        rendered = render_scorecard(await run(probes="contract"))
        assert "Probes: 1/1 complete" not in rendered
        assert "1/6 complete (5 not selected)" in rendered

    async def test_a_full_scan_is(self):
        from ratemyagent.outputs.scorecard import render_scorecard

        assert "Probes: 6/6 complete" in render_scorecard(await run())


class TestTheFrozenFieldsStayFrozen:
    """Step 3 of the promotion procedure in docs/API-STABILITY.md.

    "Freezing a shape nothing asserts is how it thaws quietly."
    """

    async def test_not_scored_is_exported(self):
        for dim in (await run()).breakdown:
            assert "not_scored" in dim.to_dict()

    async def test_graded_weight_is_exported(self):
        assert "graded_weight" in (await run()).to_dict()

    async def test_every_category_is_documented(self):
        """The five values are the contract; a sixth needs a doc entry."""
        import pathlib

        from ratemyagent.policy import NOT_SCORED_NOTES

        doc = pathlib.Path(__file__).resolve().parents[1] / "docs" / "API-STABILITY.md"
        text = doc.read_text()
        for category in NOT_SCORED_NOTES:
            assert f'"{category}"' in text, f"{category} is not in API-STABILITY.md"
