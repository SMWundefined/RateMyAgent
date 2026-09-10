"""The recovery threshold is derived from the flags, and withheld when undecidable.

Two changes with one subject. `recovery_rate` was scored against a fixed 0.90,
but the injector produces `1 - fault_rate**max_retries` against a target that
never fails -- 96% at the default r=0.2, 91% at 0.3, 75% at 0.5. A fixed floor
therefore grades `--fault-rate`: below r=0.316 every healthy target clears it
with room to spare, above r=0.316 no target can reach it however good it is.

The second change is what to do when the sample cannot answer. 6 of 7
recoveries spans [48.7%, 97.4%]; against a 96% floor that is not a target
failing, it is a sample of seven. The metric is withheld rather than scored,
which is the suppression `CLAUDE.md` and NextSteps claimed existed for twenty
releases as a fixed cap below ten disrupted operations.

These tests exist because the first mutation run killed every mutant on this
logic through the README transcript gate alone -- a golden-file check that
catches any output change and so is evidence of nothing in particular.
"""

from __future__ import annotations

import pytest

from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.policy import Policy, evaluate
from ratemyagent.probes.base import recovery_floor, wilson_interval


class TestTheFloorIsTheInjectorsArithmetic:
    """`1 - r**retries` is not a tuning constant; it is what the proxy does."""

    @pytest.mark.parametrize("rate,retries,expected", [
        (0.2, 2, 0.96),
        (0.3, 2, 0.91),
        (0.5, 2, 0.75),
        (0.9, 2, 0.19),
        (0.3, 1, 0.70),
        (0.3, 3, 0.973),
        (0.0, 2, 1.00),
    ])
    def test_the_floor_matches_the_closed_form(self, rate, retries, expected):
        assert recovery_floor(rate, retries) == pytest.approx(expected, abs=5e-4)

    def test_the_retry_budget_is_retries_not_attempts(self):
        """Off by one here silently moves every threshold.

        An operation makes `1 + max_retries` attempts, but only the retries can
        rescue it -- the first attempt is what made it disrupted. Using attempts
        gives 0.973 at r=0.3 where the proxy produces 0.91.
        """
        assert recovery_floor(0.3, 2) == pytest.approx(0.91)
        assert recovery_floor(0.3, 3) != pytest.approx(0.91)

    @pytest.mark.parametrize("rate,retries", [(None, 2), (0.2, None), (-0.1, 2), (1.5, 2)])
    def test_it_declines_rather_than_inventing_a_number(self, rate, retries):
        """No derivation is a fallback to the policy value, not a guess."""
        assert recovery_floor(rate, retries) is None

    def test_the_floor_crosses_the_old_fixed_value_at_a_computable_rate(self):
        """0.90 is reachable only below r = sqrt(0.10) = 0.3162."""
        assert recovery_floor(0.316, 2) > 0.90
        assert recovery_floor(0.317, 2) < 0.90


class TestWilsonNotNormalApproximation:
    def test_a_perfect_sample_still_has_width(self):
        """The normal approximation gives 5/5 a zero-width interval.

        That is the whole reason Wilson is used: a zero-width interval at k=n
        means every small clean sample reads as certainty, and every one of them
        would be scored rather than withheld.
        """
        low, high = wilson_interval(5, 5)
        assert high == 1.0
        assert low < 0.60, "5/5 should not read as near-certain"

    def test_it_stays_inside_the_unit_interval(self):
        for k, n in [(0, 1), (1, 1), (0, 10), (10, 10), (19, 24), (32, 35)]:
            low, high = wilson_interval(k, n)
            assert 0.0 <= low <= high <= 1.0

    def test_it_narrows_with_evidence(self):
        widths = [wilson_interval(round(0.9 * n), n) for n in (10, 50, 200)]
        spans = [high - low for low, high in widths]
        assert spans[0] > spans[1] > spans[2]

    def test_no_trials_is_maximum_ignorance_not_a_crash(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)


def _scored(recovery_rate, disrupted, recovered, fault_rate, max_retries=2):
    """A scan carrying just enough behaviour metrics to score recovery."""
    from ratemyagent.probes.base import recovery_floor as floor_of

    metrics = {
        "recovery_rate": recovery_rate,
        "disrupted": disrupted,
        "recovered": recovered,
        "duplicate_mutations": 0,
        "retry_amplification": None,
        "fault_rate": fault_rate,
        "max_retries": max_retries,
        "recovery_floor": floor_of(fault_rate, max_retries),
    }
    result = ScanResult(
        target=TargetInfo(name="t", kind="mcp"),
        probes=[ProbeResult(probe="behavior", metrics=metrics)],
    )
    return evaluate(result, Policy.default()), metrics


class TestThePolicyScoresTheDerivedFloor:
    def test_the_threshold_shown_is_the_derived_one_not_the_policy_value(self):
        result, _ = _scored(0.80, 200, 160, fault_rate=0.3)
        check = next(c for c in result.checks if c.name == "recovery_rate_min")

        assert check.threshold == pytest.approx(0.91), (
            "recovery was scored against the policy's 0.90 rather than the "
            "0.91 this fault rate produces"
        )
        assert check.threshold_source == "recovery_floor"
        assert Policy.default().thresholds["recovery_rate_min"] == 0.90, (
            "the policy literal should still be 0.90; the derivation overrides "
            "it at runtime rather than replacing it"
        )

    def test_the_same_observation_is_graded_differently_at_a_different_rate(self):
        """The point of the change, stated as a test.

        0.80 recovery is a pass against the 0.75 floor r=0.5 produces and a fail
        against the 0.91 that r=0.3 produces. Under the fixed 0.90 both were
        failures, and the flag was doing the grading.
        """
        strict, _ = _scored(0.80, 200, 160, fault_rate=0.3)
        loose, _ = _scored(0.80, 200, 160, fault_rate=0.5)

        strict_check = next(c for c in strict.checks if c.name == "recovery_rate_min")
        loose_check = next(c for c in loose.checks if c.name == "recovery_rate_min")

        assert not strict_check.passed and strict_check.threshold == pytest.approx(0.91)
        assert loose_check.passed and loose_check.threshold == pytest.approx(0.75)

    def test_it_falls_back_to_the_policy_value_when_nothing_was_derived(self):
        result, _ = _scored(0.80, 200, 160, fault_rate=None)
        check = next(c for c in result.checks if c.name == "recovery_rate_min")

        assert check.threshold == pytest.approx(0.90)
        assert check.threshold_source == "policy"


class TestTheIntervalIsReportedNotActedOn:
    """The suppression that was staged and pulled, and why the caveat replaced it.

    Withholding `recovery_rate` whenever its Wilson interval spanned the derived
    floor fired on all nine section 9 rows. Simulation says that is not a
    property of those samples: a target that never fails has a true recovery
    rate of exactly `1 - r**retries`, which *is* the floor, so its interval
    contains the floor ~95% of the time at any n. It is a retirement, and it
    hands behaviour's 35 points to `duplicate_mutations`, which cannot fail.

    What ships instead: the interval and the floor are published beside the
    number, and scoring is unchanged.
    """

    def test_a_thin_clean_sample_is_still_scored(self):
        result, metrics = _scored(1.0, 5, 5, fault_rate=0.5)

        assert metrics["recovery_rate"] == 1.0
        check = next(c for c in result.checks if c.name == "recovery_rate_min")
        assert not check.skipped and check.passed

    def test_the_caveat_states_the_interval_and_the_floor(self):
        from ratemyagent.probes.behavior import _caveats

        _, metrics = _scored(1.0, 5, 5, fault_rate=0.5)
        metrics.setdefault("trajectories", 20)
        caveat = next(
            c for c in _caveats(metrics) if "recovery_rate" in c.metrics
        )

        assert caveat.effect == "annotate", "reporting, not suppressing"
        assert "56.6%-100.0%" in caveat.reason
        assert "75.0%" in caveat.reason, "the derived floor is not named"
        assert "scored from it regardless" in caveat.reason, (
            "the caveat must say the number is still being scored, or a reader "
            "will assume the interval withheld it"
        )

    def test_a_wide_interval_does_not_move_a_single_point(self):
        """The property the pulled version broke."""
        wide, _ = _scored(1.0, 5, 5, fault_rate=0.2)
        narrow, _ = _scored(1.0, 500, 500, fault_rate=0.2)

        assert wide.score == narrow.score == 100

    def test_a_target_below_the_floor_still_fails_on_a_thin_sample(self):
        """6/7 at r=0.2 is the section 9 row that was the only honest FAIL.

        Its interval spans the floor, so the pulled suppression withheld it and
        the row went 89 -> 100. Scored, it fails and keeps its cap.
        """
        result, metrics = _scored(6 / 7, 7, 6, fault_rate=0.2)

        check = next(c for c in result.checks if c.name == "recovery_rate_min")
        assert not check.skipped and not check.passed
        assert check.threshold == pytest.approx(0.96)
        assert result.cap_reason and "recovery_rate_min" in result.cap_reason


class TestAPerfectTargetSitsOnTheFloor:
    """The measurement that killed the suppression, kept as a standing fact.

    If this ever stops being true the suppression becomes viable again, and
    whoever revisits it should find the reason written down rather than
    re-derive it.
    """

    def test_the_floor_is_the_expected_value_for_a_flawless_target(self):
        """Not a bound on a perfect target -- its mean."""
        import random

        from ratemyagent.probes.base import wilson_interval as ci

        random.seed(11)
        floor = recovery_floor(0.2, 2)
        spans = 0
        trials = 600
        for _ in range(trials):
            k = sum(1 for _ in range(200) if random.random() < floor)
            low, high = ci(k, 200)
            spans += low <= floor <= high
        assert spans / trials > 0.85, (
            "a perfect target's interval should contain the floor almost always; "
            "if it no longer does, revisit the withheld-when-undecidable rule"
        )


class TestTheHeaderDeclaresComparability:
    async def test_every_renderer_carries_the_rate_and_the_derived_floor(self):
        from ratemyagent import scan
        from ratemyagent.outputs import render_agents_md, render_report, render_scorecard
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.targets import MockTarget

        async with MockTarget.healthy() as target:
            result = await scan(
                target,
                config=ProbeConfig(requests=20, warmup=0, extra={"fault_rate": 0.3}),
                policy=Policy.default(),
            )

        for name, rendered in (
            ("scorecard", render_scorecard(result)),
            ("report", render_report(result)),
            ("agents_md", render_agents_md(result)),
        ):
            # The one line, not the whole document. `91.0%` also appears in the
            # checks table's target column, so a document-wide search passes
            # even with the header line deleted -- a mutant survived exactly
            # that way before this was tightened.
            header = next(
                (ln for ln in rendered.splitlines() if "fault rate 30%" in ln), None
            )
            assert header, f"{name} does not state the fault rate in a header line"
            assert "2 retries" in header, f"{name} header omits the retry budget"
            assert "91.0%" in header, (
                f"{name} states the fault rate without the floor it implies, so a "
                "reader still cannot tell whether two scans are comparable: "
                f"{header!r}"
            )

    async def test_two_rates_produce_visibly_different_headers(self):
        """The reason it is in the header: these two scans are not comparable."""
        from ratemyagent import scan
        from ratemyagent.outputs import render_report
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.targets import MockTarget

        rendered = []
        for rate in (0.2, 0.3):
            async with MockTarget.healthy() as target:
                result = await scan(
                    target,
                    config=ProbeConfig(requests=20, warmup=0, extra={"fault_rate": rate}),
                    policy=Policy.default(),
                )
            rendered.append(render_report(result))

        assert "96.0%" in rendered[0] and "91.0%" in rendered[1]
        assert rendered[0] != rendered[1]
