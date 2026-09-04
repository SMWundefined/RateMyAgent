"""ConcurrencyTester: the ramp, the saturation point, and the latency knee."""

from __future__ import annotations

import pytest

from ratemyagent.models import Grade, ProbeResult
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.concurrency import (
    SATURATION_ERROR_RATE,
    ConcurrencyTester,
    _ladder,
)
from ratemyagent.targets import MockTarget


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 12, "warmup": 0, "timeout_s": 5.0, "concurrency": 8}
    return ProbeConfig(**{**defaults, **kwargs})


class TestLadder:
    def test_doubles_up_to_the_ceiling(self):
        assert _ladder(8) == [1, 2, 4, 8]

    def test_ceiling_is_always_included(self):
        assert _ladder(10) == [1, 2, 4, 8, 10]
        assert _ladder(5) == [1, 2, 4, 5]

    def test_ceiling_of_one_is_a_single_level(self):
        assert _ladder(1) == [1]

    def test_no_duplicate_levels(self):
        for ceiling in range(1, 40):
            levels = _ladder(ceiling)
            assert len(levels) == len(set(levels))
            assert levels[-1] == ceiling

    def test_zero_is_rejected(self):
        with pytest.raises(ValueError):
            _ladder(0)


class TestRamp:
    async def test_every_level_is_measured(self):
        async with MockTarget.healthy() as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=8))

        assert result.metrics["levels_tested"] == [1, 2, 4, 8]
        assert all(level["requests"] == 12 for level in result.metrics["levels"])

    async def test_levels_do_not_reuse_request_labels(self):
        """Each level must be fresh traffic, not a replay of the last level."""
        target = MockTarget.healthy()
        async with target:
            await ConcurrencyTester().execute(target, config(concurrency=4))

        labels = [call.label for call in target.calls]
        assert len(set(labels)) == len(labels)

    async def test_a_healthy_target_never_saturates(self):
        async with MockTarget.healthy() as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=8))

        assert result.metrics["saturation_point"] is None
        assert result.metrics["max_sustained_concurrency"] == 8

    async def test_capacity_limited_target_saturates(self):
        async with MockTarget.saturating(capacity=4) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=32))

        assert result.metrics["saturation_point"] is not None
        assert result.metrics["saturation_point"] > 4
        assert result.metrics["max_sustained_concurrency"] == 4

    async def test_saturation_is_the_first_level_past_the_threshold(self):
        async with MockTarget.saturating(capacity=2) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=16))

        saturation = result.metrics["saturation_point"]
        levels = {lvl["concurrency"]: lvl for lvl in result.metrics["levels"]}
        assert levels[saturation]["error_rate"] > SATURATION_ERROR_RATE
        for level, data in levels.items():
            if level < saturation:
                assert data["error_rate"] <= SATURATION_ERROR_RATE

    async def test_ramp_stops_early_when_mostly_failing(self):
        async with MockTarget.saturating(capacity=1) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=64))

        assert result.metrics["levels_tested"][-1] < 64
        assert any("stopped early" in f for f in result.findings)

    async def test_latency_knee_is_detected(self):
        """A target can saturate by getting slow rather than by failing.

        overload_error_scale=0 isolates exactly that: past capacity it slows
        down but never errors, so the error-rate rule alone would call it
        healthy at every level.
        """
        async with MockTarget.saturating(capacity=2, overload_error_scale=0.0) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=16))

        assert result.metrics["saturation_point"] is None
        assert result.metrics["latency_knee_at"] is not None
        assert any("Latency knee" in f for f in result.findings)

    async def test_a_latency_knee_alone_caps_the_grade(self):
        async with MockTarget.saturating(capacity=2, overload_error_scale=0.0) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=16))

        assert result.grade is Grade.C

    async def test_goodput_excludes_failures(self):
        """A level that fails fast must not post the best throughput number."""
        async with MockTarget.saturating(capacity=2) as target:
            result = await ConcurrencyTester().execute(target, config(concurrency=16))

        failing = [lvl for lvl in result.metrics["levels"] if lvl["error_rate"] > 0]
        assert failing, "expected at least one level past capacity to fail"

        for level in failing:
            raw = level["concurrency"] / level["mean_s"]
            assert level["throughput_rps"] < raw


class TestGrading:
    def _graded(self, **metrics) -> Grade:
        return ConcurrencyTester().grade(ProbeResult(probe="concurrency", metrics=metrics))

    @pytest.mark.parametrize(
        "sustained,expected",
        [(64, Grade.A), (32, Grade.A), (31, Grade.B), (16, Grade.B),
         (15, Grade.C), (5, Grade.C), (4, Grade.D), (2, Grade.D), (1, Grade.F)],
    )
    def test_sustained_concurrency_drives_the_grade(self, sustained, expected):
        assert self._graded(max_sustained_concurrency=sustained) is expected

    def test_failing_at_one_concurrent_is_an_f(self):
        assert self._graded(max_sustained_concurrency=0) is Grade.F

    def test_a_latency_knee_caps_the_grade(self):
        """Saturating by getting slow is still saturating."""
        assert self._graded(max_sustained_concurrency=64, latency_knee_at=8) is Grade.C

    def test_missing_metrics_grade_f(self):
        assert self._graded() is Grade.F


class TestProbeContract:
    def test_declares_baseline_phase_and_opts_out_of_fault_rerun(self):
        """Under injected faults the ramp would call the fault rate saturation."""
        assert ConcurrencyTester.phase == "baseline"
        assert ConcurrencyTester.rerun_under_fault is False

    async def test_runs_without_keys_or_network(self):
        async with MockTarget.healthy() as target:
            result = await ConcurrencyTester().execute(target, config())

        assert result.grade is not None
        assert result.sample_count > 0

    async def test_reproducible_for_a_seed(self):
        async def run():
            async with MockTarget.saturating(capacity=4, seed=5) as target:
                return await ConcurrencyTester().execute(target, config(concurrency=16))

        first, second = await run(), await run()
        assert first.metrics["saturation_point"] == second.metrics["saturation_point"]
