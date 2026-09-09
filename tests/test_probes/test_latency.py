"""LatencyProfiler: statistics, findings, and grading."""

from __future__ import annotations

import pytest

from ratemyagent.models import ErrorKind, Response
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.base import percentile
from ratemyagent.probes.latency import LatencyProfiler
from ratemyagent.targets import MockTarget
from tests.conftest import ExplodingTarget, ScriptedTarget


class TestPercentile:
    def test_nearest_rank_picks_a_real_observation(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(values, 50) == 3.0
        assert percentile(values, 100) == 5.0

    def test_p95_of_twenty_samples_is_the_nineteenth(self):
        values = [float(i) for i in range(1, 21)]
        assert percentile(values, 95) == 19.0

    def test_p99_rounds_up_to_the_worst_sample(self):
        values = [float(i) for i in range(1, 21)]
        assert percentile(values, 99) == 20.0

    def test_unsorted_input_is_handled(self):
        assert percentile([5.0, 1.0, 3.0], 50) == 3.0

    def test_single_value(self):
        assert percentile([2.5], 95) == 2.5

    def test_empty_is_none(self):
        assert percentile([], 95) is None

    @pytest.mark.parametrize("pct", [-1, 101])
    def test_out_of_range_is_rejected(self, pct):
        with pytest.raises(ValueError):
            percentile([1.0], pct)


class TestMetrics:
    async def test_percentiles_come_from_the_measured_requests(self):
        latencies = [float(i) for i in range(1, 21)]
        target = ScriptedTarget.from_latencies(latencies)
        config = ProbeConfig(requests=20, warmup=0)

        result = await LatencyProfiler().execute(target, config)

        assert result.metrics["requests"] == 20
        assert result.metrics["p50_s"] == 10.0
        assert result.metrics["p95_s"] == 19.0
        # `observed_p99_s` is what was measured; `p99_s` is what is scored, and
        # below 100 samples it is deliberately None. See TestP99SampleFloor.
        assert result.metrics["observed_p99_s"] == 20.0
        assert result.metrics["p99_s"] is None
        assert result.metrics["min_s"] == 1.0
        assert result.metrics["max_s"] == 20.0
        assert result.metrics["mean_s"] == pytest.approx(10.5)

    async def test_warmup_requests_are_excluded_from_the_stats(self):
        target = ScriptedTarget.from_latencies([1.0])
        config = ProbeConfig(requests=5, warmup=3)

        result = await LatencyProfiler().execute(target, config)

        assert result.metrics["requests"] == 5
        assert len(target.calls) == 8

    async def test_warmup_labels_do_not_collide_with_measured_labels(self):
        target = ScriptedTarget.from_latencies([1.0])
        await LatencyProfiler().execute(target, ProbeConfig(requests=4, warmup=2))

        assert len({call.label for call in target.calls}) == 6

    async def test_failures_drive_error_rate_and_taxonomy(self):
        target = ScriptedTarget.from_latencies([1.0] * 8, failures=2)
        config = ProbeConfig(requests=10, warmup=0)

        result = await LatencyProfiler().execute(target, config)

        assert result.metrics["successes"] == 8
        assert result.metrics["failures"] == 2
        assert result.error_rate == pytest.approx(0.2)
        assert result.metrics["errors_by_kind"] == {"timeout": 2}

    async def test_failed_requests_are_excluded_from_latency_stats(self):
        """A timeout at 30s must not be averaged in as though it were a real sample."""
        target = ScriptedTarget.from_latencies([1.0] * 9, failures=1)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=10, warmup=0))

        assert result.metrics["max_s"] == 1.0

    async def test_all_requests_failing_leaves_no_distribution(self):
        target = ScriptedTarget.from_latencies([], failures=1)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=5, warmup=0))

        assert result.metrics["p95_s"] is None
        assert result.error_rate == 1.0
        assert "all 5 requests failed" in result.summary

    async def test_tool_call_overhead_uses_reported_server_time(self):
        target = ScriptedTarget.from_latencies([1.0] * 10, server_time_ratio=0.8)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=10, warmup=0))

        assert result.metrics["server_time_p50_s"] == pytest.approx(0.8)
        assert result.metrics["tool_call_overhead_s"] == pytest.approx(0.2)

    async def test_large_overhead_is_reported_as_a_finding(self):
        target = ScriptedTarget.from_latencies([1.0] * 20, server_time_ratio=0.5)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert any("Tool call overhead" in f for f in result.findings)

    async def test_overhead_is_none_when_the_target_reports_no_server_time(self):
        target = ScriptedTarget([Response(ok=True, latency_s=1.0)])
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=5, warmup=0))

        assert result.metrics["tool_call_overhead_s"] is None
        assert result.metrics["ttft_p50_s"] is None

    async def test_tail_ratio_compares_p99_to_p50(self):
        target = ScriptedTarget.from_latencies([1.0] * 19 + [10.0])
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert result.metrics["tail_ratio"] == pytest.approx(10.0)

    async def test_stdev_needs_more_than_one_sample(self):
        target = ScriptedTarget.from_latencies([1.0])
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=1, warmup=0))

        assert result.metrics["stdev_s"] is None


class TestFindings:
    async def test_clean_run_reports_the_numbers_without_claiming_health(self):
        """The probe measures; the policy decides. Claiming "no problems" here
        would contradict a FAILED policy check sitting above it."""
        target = ScriptedTarget.from_latencies([0.5] * 20)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert len(result.findings) == 1
        assert "no heavy tail" in result.findings[0]
        assert "No latency problems found" not in result.findings[0]

    async def test_clean_run_bounds_the_error_rate_rather_than_claiming_zero(self):
        """0 failures in 20 requests is a 15% upper bound, not a 0% error rate."""
        target = ScriptedTarget.from_latencies([0.5] * 20)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        # A caveat about the sample, not a finding about the target.
        assert any("15%" in c.reason for c in result.caveats)
        assert all("15%" not in f for f in result.findings)

    async def test_large_clean_run_drops_the_confidence_caveat(self):
        target = ScriptedTarget.from_latencies([0.5] * 100)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=100, warmup=0))

        assert "95% confidence" not in result.findings[0]

    async def test_slow_p95_is_reported_with_the_next_grade_up(self):
        target = ScriptedTarget.from_latencies([3.0] * 20)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert result.metrics["p95_s"] == pytest.approx(3.0)

    async def test_heavy_tail_is_called_out(self):
        target = ScriptedTarget.from_latencies([1.0] * 19 + [8.0])
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert any("Heavy tail" in f for f in result.findings)

    async def test_p99_is_reported_but_not_scored_below_a_hundred(self):
        target = ScriptedTarget.from_latencies([0.5] * 5)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=5, warmup=0))

        # Reported, so the tail is still visible; not scored, so the policy
        # cannot grade the maximum of five samples against a p99 threshold.
        assert result.metrics["observed_p99_s"] is not None
        assert result.metrics["p99_s"] is None
        suppressed = [c for c in result.caveats if c.metrics == ("p99_s",)]
        assert suppressed and suppressed[0].effect == "suppress"
        assert "estimates" in suppressed[0].reason

    async def test_large_sample_has_no_warning(self):
        target = ScriptedTarget.from_latencies([0.5] * 25)
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=25, warmup=0))

        assert not any("p99 is not meaningful" in f for f in result.findings)

    async def test_error_breakdown_lists_kinds_by_frequency(self):
        responses = [Response(ok=True, latency_s=1.0)] * 6 + [
            Response(ok=False, latency_s=1.0, error_kind=ErrorKind.TIMEOUT),
            Response(ok=False, latency_s=1.0, error_kind=ErrorKind.TIMEOUT),
            Response(ok=False, latency_s=1.0, error_kind=ErrorKind.RATE_LIMIT),
            Response(ok=False, latency_s=1.0, error_kind=ErrorKind.SERVER_ERROR),
        ]
        result = await LatencyProfiler().execute(
            ScriptedTarget(responses), ProbeConfig(requests=10, warmup=0)
        )

        finding = next(f for f in result.findings if "requests failed" in f)
        assert "4/10" in finding
        assert finding.index("2 timeout") < finding.index("1 rate_limit")


class TestProbeContract:
    async def test_execute_fills_in_phase_and_duration(self, healthy_target, config):
        async with healthy_target as target:
            result = await LatencyProfiler().execute(target, config)

        assert result.probe == "latency"
        assert result.phase == "baseline"
        assert result.duration_s > 0
        assert result.failed is False

    async def test_a_raising_target_does_not_take_down_the_probe(self, config):
        async with ExplodingTarget() as target:
            result = await LatencyProfiler().execute(target, config)

        assert result.error_rate == 1.0
        assert result.metrics["errors_by_kind"] == {"connection": config.requests}

    async def test_probe_is_reusable_across_targets(self, config):
        probe = LatencyProfiler()
        async with MockTarget.healthy() as fast:
            good = await probe.execute(fast, config)
        async with MockTarget.failing() as broken:
            bad = await probe.execute(broken, config)

        assert good.error_rate == 0.0 and good.metrics["p95_s"] < 2.0
        assert bad.error_rate > 0.1

    async def test_runs_are_reproducible_for_a_given_seed(self, config):
        async with MockTarget.degraded(seed=99) as first:
            one = await LatencyProfiler().execute(first, config)
        async with MockTarget.degraded(seed=99) as second:
            two = await LatencyProfiler().execute(second, config)

        assert one.metrics["p95_s"] == two.metrics["p95_s"]
        assert one.error_rate == two.error_rate

    def test_probe_declares_its_pipeline_phase(self):
        assert LatencyProfiler.phase == "baseline"


class TestMockProfilesBehaveAsAdvertised:
    """The canned profiles are the demo surface, so pin what they measure.

    Checked across several seeds: a profile whose knobs sit on a policy boundary
    flips the score run to run, which makes it useless for demonstrating anything.
    """

    @pytest.mark.parametrize("seed", [1, 7, 42, 1337, 90210])
    @pytest.mark.parametrize(
        "profile,max_p95,max_error_rate",
        [("healthy", 2.0, 0.001), ("degraded", 10.0, 0.001)],
    )
    async def test_profile_stays_in_band_across_seeds(
        self, profile, max_p95, max_error_rate, seed
    ):
        target = getattr(MockTarget, profile)(seed=seed)
        async with target:
            result = await LatencyProfiler().execute(target, ProbeConfig(requests=50, warmup=0))

        assert result.metrics["p95_s"] < max_p95
        assert result.error_rate <= max_error_rate

    @pytest.mark.parametrize("seed", [1, 7, 42, 1337, 90210])
    async def test_failing_profile_is_reliably_bad(self, seed):
        async with MockTarget.failing(seed=seed) as target:
            result = await LatencyProfiler().execute(target, ProbeConfig(requests=50, warmup=0))

        assert result.metrics["p95_s"] > 10.0
        assert result.error_rate > 0.10


class TestP99SampleFloor:
    """p99 is the sample maximum below 100 requests, so it is not scored there.

    The cutoff is exact rather than a rule of thumb, and it follows from
    `percentile()` being nearest-rank: nearest rank for percentile *p* over *n*
    samples is `ceil(n*p/100)`, and the smallest n satisfying `ceil(0.99n) < n`
    is exactly 100. Below it, "p99" *is* the maximum by construction.

    The check this replaced fired below **20** while its own text said "not
    meaningful below ~100", so every default `--requests 20` scan graded the
    maximum of twenty samples against a p99 threshold and was told nothing. The
    guard disagreed with its own stated criterion.
    """

    async def test_the_cutoff_is_where_p99_stops_being_the_maximum(self):
        """The derivation, asserted rather than described."""
        from ratemyagent.probes.base import percentile
        from ratemyagent.probes.latency import P99_MIN_SAMPLE

        for n in range(2, P99_MIN_SAMPLE):
            values = list(range(1, n + 1))
            assert percentile(values, 99) == max(values), (
                f"at n={n} p99 is not the maximum, so the floor is wrong"
            )

        values = list(range(1, P99_MIN_SAMPLE + 1))
        assert percentile(values, 99) != max(values), (
            "at the floor p99 should be a distinct order statistic"
        )

    async def test_ninety_nine_samples_is_still_the_maximum(self):
        """One below the floor, to catch an off-by-one in either direction."""
        from ratemyagent.probes.base import percentile

        assert percentile(list(range(1, 100)), 99) == 99

    async def test_a_hundred_samples_scores_normally(self):
        target = ScriptedTarget.from_latencies([0.5] * 100)
        result = await LatencyProfiler().execute(
            target, ProbeConfig(requests=100, warmup=0)
        )

        assert result.metrics["p99_s"] is not None
        assert not [c for c in result.caveats if c.metrics == ("p99_s",)]

    async def test_the_tail_is_still_reported_and_still_findable(self):
        """Suppression is about scoring. A heavy tail must still be a finding --
        withholding the number would delete a real observation rather than
        qualify it."""
        target = ScriptedTarget.from_latencies([0.1] * 19 + [10.0])
        result = await LatencyProfiler().execute(target, ProbeConfig(requests=20, warmup=0))

        assert result.metrics["p99_s"] is None
        assert result.metrics["observed_p99_s"] == 10.0
        assert result.metrics["tail_ratio"] is not None
        assert any("Heavy tail" in f for f in result.findings)


class TestP99FloorOnATargetTheRegressionSetDoesNotCover:
    """A tail over the p99 threshold with a p95 comfortably under it.

    None of the nine section 9 rows is this target -- every one of them passes
    p99 and p95 together -- so the re-scan cannot show this fix doing anything.
    This fixture is the case where it changes a score: 19 fast requests and one
    12-second outlier, which fails `p99_latency_ms: 10000` while passing
    `p95_latency_ms: 5000`.
    """

    LATENCIES = [0.1] * 19 + [12.0]

    async def _scored(self, requests: int, latencies: list[float]):
        from ratemyagent import Policy
        from ratemyagent.models import ScanResult, TargetInfo
        from ratemyagent.policy import evaluate

        target = ScriptedTarget.from_latencies(latencies)
        probe = await LatencyProfiler().execute(
            target, ProbeConfig(requests=requests, warmup=0)
        )
        result = ScanResult(
            target=TargetInfo(name="synthetic", kind="mock"), probes=[probe]
        )
        return evaluate(result, Policy.default())

    async def test_the_outlier_is_not_scored_at_twenty_requests(self):
        scored = await self._scored(20, self.LATENCIES)
        p99 = next(c for c in scored.checks if c.name == "p99_latency_ms")

        assert p99.skipped, "the maximum of 20 samples was graded as a p99"
        # And the dimension survives on its remaining checks rather than
        # vanishing: latency still carries its full 20 points.
        latency = next(d for d in scored.breakdown if d.probe == "latency")
        assert latency.points == 20.0

    async def test_the_same_outlier_is_scored_at_a_hundred(self):
        """At 100 requests p99 is rank 99, a real order statistic, and one
        12-second outlier no longer reaches it -- which is the point: the
        estimate becomes meaningful rather than merely available."""
        scored = await self._scored(100, [0.1] * 99 + [12.0])
        p99 = next(c for c in scored.checks if c.name == "p99_latency_ms")

        assert not p99.skipped
        assert p99.passed, "rank 99 of 100 should not be the single outlier"
