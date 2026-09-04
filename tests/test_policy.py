"""Policy loading, the scoring curve, and evaluation against a scan."""

from __future__ import annotations

import pytest

from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.policy import (
    COMPLIANT_SCORE,
    DEFAULT_POLICY_PATH,
    SPECS_BY_NAME,
    THRESHOLD_SPECS,
    Policy,
    PolicyError,
    evaluate,
    score_check,
)

MAX_SPEC = SPECS_BY_NAME["p95_latency_ms"]
MIN_SPEC = SPECS_BY_NAME["concurrency_min"]
ABSOLUTE_SPEC = SPECS_BY_NAME["duplicate_mutation_max"]


def write(tmp_path, text: str):
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


class TestLoading:
    def test_the_shipped_default_loads(self):
        policy = Policy.default()
        assert policy.name == "production-default"
        assert policy.pass_score == 75
        assert policy.thresholds["p95_latency_ms"] == 5000

    def test_the_shipped_default_file_exists_where_the_package_expects(self):
        assert DEFAULT_POLICY_PATH.exists()

    def test_every_shipped_threshold_is_a_known_key(self):
        assert set(Policy.default().thresholds) <= set(SPECS_BY_NAME)

    def test_loads_from_a_file(self, tmp_path):
        path = write(tmp_path, "name: mine\nthresholds:\n  error_rate_max: 0.01\npass_score: 90\n")
        policy = Policy.load(path)

        assert policy.name == "mine"
        assert policy.pass_score == 90
        assert policy.thresholds == {"error_rate_max": 0.01}

    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(PolicyError, match="not found"):
            Policy.load(tmp_path / "nope.yaml")

    def test_invalid_yaml_is_reported(self, tmp_path):
        with pytest.raises(PolicyError, match="not valid YAML"):
            Policy.load(write(tmp_path, "name: [unclosed\n"))

    def test_empty_file_is_reported(self, tmp_path):
        with pytest.raises(PolicyError, match="empty"):
            Policy.load(write(tmp_path, ""))

    def test_a_policy_with_no_thresholds_is_rejected(self, tmp_path):
        """Nothing to score means no score, which is not a useful gate."""
        with pytest.raises(PolicyError, match="no thresholds"):
            Policy.load(write(tmp_path, "name: empty\nthresholds: {}\n"))

    def test_unknown_threshold_names_the_valid_keys(self, tmp_path):
        with pytest.raises(PolicyError, match="unknown threshold"):
            Policy.load(write(tmp_path, "name: x\nthresholds:\n  made_up_key: 1\n"))

    def test_non_numeric_threshold_is_rejected(self, tmp_path):
        with pytest.raises(PolicyError, match="must be a number"):
            Policy.load(write(tmp_path, "name: x\nthresholds:\n  error_rate_max: fast\n"))

    def test_negative_threshold_is_rejected(self):
        with pytest.raises(PolicyError, match="cannot be negative"):
            Policy(thresholds={"error_rate_max": -0.1})

    @pytest.mark.parametrize("score", [-1, 101])
    def test_pass_score_out_of_range_is_rejected(self, score):
        with pytest.raises(PolicyError, match="pass_score"):
            Policy(thresholds={"error_rate_max": 0.05}, pass_score=score)

    def test_specs_lists_only_thresholds_that_are_set(self):
        policy = Policy(thresholds={"error_rate_max": 0.05, "p95_latency_ms": 1000})
        assert {s.name for s in policy.specs} == {"error_rate_max", "p95_latency_ms"}


class TestScoringCurve:
    def test_meeting_a_max_threshold_scores_full_marks(self):
        assert score_check(MAX_SPEC, 5000, 5.0).score == COMPLIANT_SCORE

    def test_beating_a_max_threshold_is_not_worth_more_than_meeting_it(self):
        """A threshold is a limit, not a target."""
        comfortable = score_check(MAX_SPEC, 5000, 0.1).score
        exact = score_check(MAX_SPEC, 5000, 5.0).score
        assert comfortable == exact == COMPLIANT_SCORE

    def test_missing_a_max_threshold_decays_with_distance(self):
        near = score_check(MAX_SPEC, 5000, 5.5)
        far = score_check(MAX_SPEC, 5000, 8.0)

        assert not near.passed and not far.passed
        assert near.score > far.score

    def test_a_max_threshold_bottoms_out_at_double(self):
        assert score_check(MAX_SPEC, 5000, 10.0).score == 0.0
        assert score_check(MAX_SPEC, 5000, 100.0).score == 0.0

    def test_meeting_a_min_threshold_scores_full_marks(self):
        assert score_check(MIN_SPEC, 5, 5).score == COMPLIANT_SCORE
        assert score_check(MIN_SPEC, 5, 50).score == COMPLIANT_SCORE

    def test_missing_a_min_threshold_decays_toward_zero(self):
        assert score_check(MIN_SPEC, 10, 5).score == pytest.approx(50.0)
        assert score_check(MIN_SPEC, 10, 0).score == 0.0

    def test_a_zero_max_threshold_is_absolute(self):
        """duplicate_mutation_max: 0 has no partial credit by design."""
        assert score_check(ABSOLUTE_SPEC, 0, 0).score == COMPLIANT_SCORE
        assert score_check(ABSOLUTE_SPEC, 0, 1).score == 0.0
        assert score_check(ABSOLUTE_SPEC, 0, 9).score == 0.0

    def test_a_missing_metric_is_skipped_not_failed(self):
        check = score_check(MAX_SPEC, 5000, None)

        assert check.skipped is True
        assert check.passed is True
        assert "skipped" in check.reason

    def test_units_are_scaled_into_the_policy_units(self):
        """Probes measure seconds; policies are written in milliseconds."""
        check = score_check(MAX_SPEC, 5000, 0.442)
        assert check.observed == pytest.approx(442.0)
        assert "442ms" in check.reason

    def test_the_reason_states_both_sides(self):
        reason = score_check(MAX_SPEC, 5000, 7.0).reason
        assert "7,000ms" in reason and "5,000ms" in reason and "at most" in reason

    def test_a_min_threshold_reason_says_at_least(self):
        assert "at least" in score_check(MIN_SPEC, 5, 2).reason


class TestEvaluate:
    def _scan(self, **probe_metrics) -> ScanResult:
        probes = [
            ProbeResult(probe=name, metrics=metrics, applicable=True)
            for name, metrics in probe_metrics.items()
        ]
        return ScanResult(target=TargetInfo(name="t", kind="mock"), probes=probes)

    def test_checks_attach_to_the_probe_that_measured_them(self):
        result = self._scan(latency={"p95_s": 1.0, "error_rate": 0.0})
        evaluate(result, Policy(thresholds={"p95_latency_ms": 5000, "error_rate_max": 0.05}))

        assert {c.name for c in result.probe("latency").checks} == {
            "p95_latency_ms", "error_rate_max"
        }

    def test_probe_score_is_the_mean_of_its_checks(self):
        result = self._scan(latency={"p95_s": 1.0, "error_rate": 0.10})
        evaluate(result, Policy(thresholds={"p95_latency_ms": 5000, "error_rate_max": 0.05}))

        checks = result.probe("latency").checks
        assert result.probe("latency").score == pytest.approx(
            sum(c.score for c in checks) / len(checks)
        )

    def test_overall_score_is_the_mean_of_every_check_that_ran(self):
        result = self._scan(latency={"p95_s": 1.0, "error_rate": 0.0})
        evaluate(result, Policy(thresholds={"p95_latency_ms": 5000, "error_rate_max": 0.05}))

        assert result.score == COMPLIANT_SCORE

    def test_pass_and_fail_are_decided_by_pass_score(self):
        policy = Policy(thresholds={"p95_latency_ms": 1000}, pass_score=75)

        good = evaluate(self._scan(latency={"p95_s": 0.5}), policy)
        bad = evaluate(self._scan(latency={"p95_s": 5.0}), policy)

        assert good.passed is True
        assert bad.passed is False

    def test_a_skipped_check_does_not_move_the_score(self):
        with_metric = evaluate(
            self._scan(latency={"p95_s": 1.0}),
            Policy(thresholds={"p95_latency_ms": 5000}),
        )
        with_missing = evaluate(
            self._scan(latency={"p95_s": 1.0}),
            Policy(thresholds={"p95_latency_ms": 5000, "cost_per_request_max": 0.1}),
        )
        assert with_metric.score == with_missing.score

    def test_a_probe_that_did_not_run_becomes_an_unmeasured_check(self):
        result = evaluate(
            self._scan(latency={"p95_s": 1.0}),
            Policy(thresholds={"p95_latency_ms": 5000, "concurrency_min": 5}),
        )

        assert [c.name for c in result.unmeasured_checks] == ["concurrency_min"]
        # ...and it does not masquerade as a probe that ran.
        assert [p.probe for p in result.probes] == ["latency"]

    def test_an_inapplicable_probe_has_its_metrics_ignored(self):
        result = self._scan(cost={"cost_per_request": 99.0})
        result.probes[0].applicable = False
        evaluate(result, Policy(thresholds={"cost_per_request_max": 0.1}))

        assert result.checks[0].skipped is True
        assert result.score is None

    def test_no_evaluable_threshold_leaves_the_score_unset(self):
        result = evaluate(self._scan(latency={}), Policy(thresholds={"p95_latency_ms": 5000}))

        assert result.score is None
        assert result.passed is None

    def test_evaluating_twice_does_not_double_the_checks(self):
        policy = Policy(thresholds={"p95_latency_ms": 5000})
        result = self._scan(latency={"p95_s": 1.0})

        evaluate(result, policy)
        evaluate(result, policy)

        assert len(result.probe("latency").checks) == 1
        assert len(result.unmeasured_checks) == 0

    def test_policy_name_and_pass_score_are_recorded(self):
        result = evaluate(
            self._scan(latency={"p95_s": 1.0}),
            Policy(name="mine", thresholds={"p95_latency_ms": 5000}, pass_score=80),
        )

        assert result.policy_name == "mine"
        assert result.pass_score == 80

    def test_boolean_metrics_are_not_treated_as_numbers(self):
        """bool is an int in Python; scoring True as 1 would be nonsense."""
        result = self._scan(cost={"cost_per_request": True})
        evaluate(result, Policy(thresholds={"cost_per_request_max": 0.1}))

        assert result.checks[0].skipped is True


class TestThresholdSpecs:
    def test_every_spec_points_at_a_real_direction(self):
        assert all(spec.direction in {"max", "min"} for spec in THRESHOLD_SPECS)

    def test_spec_names_are_unique(self):
        names = [spec.name for spec in THRESHOLD_SPECS]
        assert len(names) == len(set(names))

    def test_every_spec_names_a_probe_that_exists(self):
        from ratemyagent.probes import PROBES

        assert all(spec.probe in PROBES for spec in THRESHOLD_SPECS)

    def test_every_spec_metric_is_produced_by_its_probe(self):
        """A threshold reading a metric no probe emits would silently never score."""
        emitted = {
            "latency": {"p95_s", "p99_s", "error_rate"},
            "cost": {"cost_per_request"},
            "concurrency": {"max_sustained_concurrency"},
            "contract": {"crash_rate", "accepted_invalid"},
            "behavior": {"recovery_rate", "retry_amplification", "duplicate_mutations"},
        }
        for spec in THRESHOLD_SPECS:
            assert spec.metric in emitted[spec.probe], f"{spec.name} -> {spec.metric}"
