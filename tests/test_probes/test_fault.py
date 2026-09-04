"""FaultInjector: the phase 2 probe."""

from __future__ import annotations

import pytest

from ratemyagent.models import FaultKind, Grade, ProbeResult
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.fault import MIN_DISRUPTED_FOR_CONFIDENCE, FaultInjector
from ratemyagent.targets import FaultConfig, MockTarget

CONFIDENT = MIN_DISRUPTED_FOR_CONFIDENCE


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 30, "warmup": 0, "timeout_s": 5.0}
    return ProbeConfig(**{**defaults, **kwargs})


class TestProbeContract:
    def test_declares_the_chaos_phase(self):
        assert FaultInjector.phase == "chaos"
        assert FaultInjector.name == "fault"

    async def test_result_is_stamped_with_the_phase(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.3)).execute(target, config())
        assert result.phase == "chaos"

    async def test_runs_without_network_or_keys(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.3)).execute(target, config())
        assert result.sample_count > 0
        assert result.grade is not None


class TestInjection:
    async def test_faults_are_injected_and_counted(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.5, seed=4)).execute(
                target, config()
            )

        assert result.metrics["injected"] > 0
        assert result.metrics["injection_rate"] == pytest.approx(0.5, abs=0.12)
        assert set(result.metrics["injected_by_kind"]) <= {k.value for k in FaultKind}

    async def test_zero_rate_injects_nothing_and_says_so(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.off()).execute(target, config())

        assert result.metrics["injected"] == 0
        assert any("No faults were injected" in f for f in result.findings)

    async def test_fault_rate_comes_from_probe_config(self):
        """The CLI passes --fault-rate through ProbeConfig.extra."""
        async with MockTarget.healthy() as target:
            result = await FaultInjector().execute(
                target, config(extra={"fault_rate": 0.0})
            )
        assert result.metrics["injected"] == 0

    async def test_only_selected_fault_kinds_appear(self):
        faults = FaultConfig(rates={FaultKind.RATE_LIMIT: 0.6}, seed=2)
        async with MockTarget.healthy() as target:
            result = await FaultInjector(faults).execute(target, config())

        assert set(result.metrics["injected_by_kind"]) == {"rate_limit"}


class TestDegradationPass:
    async def test_baseline_probes_are_rerun_under_fault(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.4, seed=1)).execute(
                target, config()
            )

        under_fault = result.metrics["baseline_probes_under_fault"]
        assert "latency" in under_fault
        assert under_fault["latency"]["error_rate"] > 0

    async def test_degradation_is_visible_against_a_clean_run(self):
        """The same probe, same target - only the faults differ."""
        from ratemyagent.probes.latency import LatencyProfiler

        async with MockTarget.healthy() as target:
            clean = await LatencyProfiler().execute(target, config())
            faulted = await FaultInjector(FaultConfig.uniform(0.4, seed=1)).execute(
                target, config()
            )

        assert clean.error_rate == 0.0
        assert faulted.metrics["baseline_probes_under_fault"]["latency"]["error_rate"] > 0.2


class TestRecoveryPass:
    async def test_trajectories_are_built_per_operation(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.3, seed=8)).execute(
                target, config(requests=25)
            )

        assert result.metrics["trajectories"] == 25
        assert result.metrics["attempts"] >= 25

    async def test_healthy_target_recovers_from_transient_faults(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.3, seed=11)).execute(
                target, config(requests=80)
            )

        assert result.metrics["recovery_rate"] > 0.8

    async def test_broken_target_fails_to_recover(self):
        async with MockTarget.failing() as target:
            result = await FaultInjector(FaultConfig.uniform(0.2, seed=11)).execute(
                target, config(requests=60)
            )

        assert result.metrics["recovery_rate"] < 0.6
        assert result.metrics["loops_detected"] > 0

    async def test_retry_amplification_is_at_least_one_call_per_operation(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.4, seed=6)).execute(
                target, config()
            )

        assert result.metrics["retry_amplification"] >= 1.0

    async def test_no_faults_means_one_call_per_operation(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.off()).execute(target, config())

        assert result.metrics["retry_amplification"] == pytest.approx(1.0)
        assert result.metrics["recovery_rate"] is None

    async def test_recovery_pass_does_not_reuse_degradation_labels(self):
        """A retry must be distinguishable from an unrelated call to the same tool."""
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.off()).execute(
                target, config(requests=10, warmup=2)
            )

        # 10 trajectories, each attempted exactly once, none merged with warmup traffic.
        assert result.metrics["trajectories"] == 10
        assert result.metrics["attempts"] == 10

    async def test_max_retries_bounds_the_attempts(self):
        async with MockTarget.failing() as target:
            result = await FaultInjector(
                FaultConfig.uniform(0.5, seed=3), max_retries=1
            ).execute(target, config(requests=20))

        assert result.metrics["attempts"] <= 40


class TestGrading:
    def _graded(self, **metrics) -> Grade:
        defaults = {"recovery_rate": 1.0, "disrupted": CONFIDENT, "injected": 5}
        return FaultInjector().grade(ProbeResult(probe="fault", metrics={**defaults, **metrics}))

    @pytest.mark.parametrize(
        "rate,expected",
        [(1.0, Grade.A), (0.95, Grade.A), (0.94, Grade.B), (0.90, Grade.B),
         (0.85, Grade.C), (0.80, Grade.C), (0.70, Grade.D), (0.60, Grade.D),
         (0.59, Grade.F), (0.0, Grade.F)],
    )
    def test_recovery_rate_drives_the_grade(self, rate, expected):
        assert self._graded(recovery_rate=rate) is expected

    def test_thin_evidence_caps_the_grade_at_c(self):
        """Two-for-two recovery is not proof of resilience."""
        assert self._graded(recovery_rate=1.0, disrupted=2) is Grade.C

    def test_enough_disruption_allows_a_top_grade(self):
        assert self._graded(recovery_rate=1.0, disrupted=CONFIDENT) is Grade.A

    def test_a_repeated_mutation_caps_the_grade(self):
        assert self._graded(recovery_rate=1.0, duplicate_mutations=1) is Grade.D

    def test_exhausted_retries_are_not_penalized_twice(self):
        """An operation that exhausts its retries is one that did not recover.

        recovery_rate already counts it, so grading loops_detected again would
        turn a single failure into two.
        """
        assert self._graded(recovery_rate=1.0, loops_detected=3) is Grade.A

    def test_nothing_injected_is_not_a_failing_grade(self):
        assert self._graded(recovery_rate=None, injected=0) is Grade.A

    def test_injected_but_nothing_disrupted_is_inconclusive(self):
        assert self._graded(recovery_rate=None, injected=10) is Grade.C


class TestFindings:
    async def test_thin_sample_is_called_out(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.05, seed=2)).execute(
                target, config(requests=10)
            )

        if result.metrics["disrupted"] and result.metrics["disrupted"] < CONFIDENT:
            assert any("capped at C" in f for f in result.findings)

    async def test_unrecovered_operations_are_reported(self):
        async with MockTarget.failing() as target:
            result = await FaultInjector(FaultConfig.uniform(0.2, seed=11)).execute(
                target, config(requests=60)
            )

        assert any("never recovered" in f for f in result.findings)

    async def test_exhausted_retries_are_not_reported_as_a_separate_finding(self):
        """Same failures as the recovery line; naming them twice inflates them."""
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.25, seed=1)).execute(
                target, config(requests=120)
            )

        assert not any("retry loop" in f for f in result.findings)

    async def test_injected_kinds_are_summarized(self):
        async with MockTarget.healthy() as target:
            result = await FaultInjector(FaultConfig.uniform(0.5, seed=1)).execute(
                target, config()
            )

        assert any("Injected" in f and "calls" in f for f in result.findings)


class TestReproducibility:
    async def test_same_seed_gives_the_same_result(self):
        async def run(seed: int):
            async with MockTarget.healthy(seed=seed) as target:
                return await FaultInjector(FaultConfig.uniform(0.4, seed=seed)).execute(
                    target, config()
                )

        first, second = await run(21), await run(21)

        assert first.metrics["injected"] == second.metrics["injected"]
        assert first.metrics["recovery_rate"] == second.metrics["recovery_rate"]
        assert first.grade is second.grade
