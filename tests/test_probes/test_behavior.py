"""BehaviorAnalyzer: phase 3 reading phase 2's trajectories."""

from __future__ import annotations

import pytest

from ratemyagent.models import FaultKind, Invocation, Trajectory
from ratemyagent.probes import ProbeConfig, ScanContext
from ratemyagent.probes.behavior import (
    AMPLIFICATION_WARN,
    MIN_DISRUPTED_FOR_CONFIDENCE,
    BehaviorAnalyzer,
)
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.targets import FaultConfig, MockTarget


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 30, "warmup": 0, "timeout_s": 5.0}
    return ProbeConfig(**{**defaults, **kwargs})


def _inv(seq, ok, *, started=0.0, latency=1.0, fingerprint="op:a", injected=None):
    return Invocation(
        sequence=seq, op="op", fingerprint=fingerprint, trajectory_id="t",
        attempt=seq + 1, ok=ok, latency_s=latency, started_at=started, injected=injected,
    )


def trajectory(*oks, **kw) -> Trajectory:
    """A trajectory from a sequence of pass/fail attempts."""
    injected = kw.get("injected")
    return Trajectory(
        kw.get("tid", "t"),
        [
            _inv(i, ok, started=float(i), injected=(injected if not ok else None))
            for i, ok in enumerate(oks)
        ],
    )


def context_with(*trajectories: Trajectory) -> ScanContext:
    return ScanContext(artifacts={"trajectories": list(trajectories)})


class TestWithoutPhaseTwo:
    async def test_no_trajectories_is_inapplicable_not_a_failure(self):
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ScanContext())

        assert result.applicable is False
        assert result.metrics["trajectories"] == 0
        assert any("no trajectories" in f.lower() for f in result.findings)

    async def test_a_missing_context_is_handled(self):
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), None)

        assert result.applicable is False

    async def test_it_sends_no_traffic_of_its_own(self):
        """Phase 3 reads what phase 2 recorded; it must not perturb the target."""
        target = MockTarget.healthy()
        async with target:
            await BehaviorAnalyzer().execute(
                target, config(), context_with(trajectory(False, True))
            )

        assert target.calls == []


class TestRetryAnalysis:
    async def test_amplification_is_attempts_over_operations(self):
        ctx = context_with(
            trajectory(True, tid="a"),
            trajectory(False, True, tid="b"),
            trajectory(False, False, True, tid="c"),
        )
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        # 1 + 2 + 3 attempts across 3 operations.
        assert result.metrics["attempts"] == 6
        assert result.metrics["retry_amplification"] == pytest.approx(2.0)

    async def test_no_retries_means_amplification_of_one(self):
        ctx = context_with(trajectory(True, tid="a"), trajectory(True, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["retry_amplification"] == pytest.approx(1.0)

    async def test_high_amplification_is_called_out(self):
        ctx = context_with(*[trajectory(False, False, True, tid=f"t{i}") for i in range(12)])
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["retry_amplification"] > AMPLIFICATION_WARN
        assert any("amplification" in f for f in result.findings)

    async def test_peak_attempts_is_reported(self):
        ctx = context_with(trajectory(True, tid="a"), trajectory(False, False, False, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["max_attempts_single_operation"] == 3


class TestRecovery:
    async def test_recovery_rate_counts_only_disrupted_operations(self):
        """An operation that never broke did not recover from anything."""
        ctx = context_with(
            trajectory(True, tid="clean"),
            trajectory(False, True, tid="recovered"),
            trajectory(False, False, tid="lost"),
        )
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["disrupted"] == 2
        assert result.metrics["recovered"] == 1
        assert result.metrics["recovery_rate"] == pytest.approx(0.5)

    async def test_nothing_disrupted_leaves_recovery_rate_unset(self):
        ctx = context_with(trajectory(True, tid="a"), trajectory(True, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["recovery_rate"] is None
        assert any("untested" in f for f in result.findings)

    async def test_recovery_latency_is_measured(self):
        ctx = context_with(trajectory(False, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["mean_recovery_latency_s"] is not None

    async def test_unrecovered_operations_are_reported_with_their_fault(self):
        ctx = context_with(
            *[
                trajectory(False, False, tid=f"t{i}", injected=FaultKind.TIMEOUT)
                for i in range(3)
            ]
        )
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["unrecovered"] == 3
        assert result.metrics["unrecovered_by_fault_kind"]["timeout"] > 0
        assert any("never" in f and "recovered" in f for f in result.findings)

    async def test_thin_sample_is_called_out_because_the_policy_scores_it(self):
        ctx = context_with(trajectory(False, True, tid="a"), trajectory(False, True, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["disrupted"] < MIN_DISRUPTED_FOR_CONFIDENCE
        assert any("bounds the" in f for f in result.findings)


class TestDuplicatesAndLoops:
    async def test_a_repeated_success_is_a_duplicate_mutation(self):
        ctx = context_with(trajectory(True, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_mutations"] == 1
        assert any("succeeded more than once" in f for f in result.findings)

    async def test_retries_that_fail_are_not_duplicates(self):
        ctx = context_with(trajectory(False, False, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_mutations"] == 0

    async def test_three_unresolved_attempts_is_a_loop(self):
        ctx = context_with(trajectory(False, False, False, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["loops_detected"] == 1
        assert any("without\never succeeding" in f or "ever succeeding" in f
                   for f in result.findings)

    async def test_a_retry_that_works_on_the_third_try_is_not_a_loop(self):
        ctx = context_with(trajectory(False, False, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["loops_detected"] == 0


class TestPipelineIntegration:
    async def test_phase_two_hands_trajectories_to_phase_three(self):
        """The whole point of the ScanContext."""
        context = ScanContext()
        async with MockTarget.healthy() as target:
            await FaultInjector(FaultConfig.uniform(0.4, seed=3)).execute(
                target, config(requests=40), context
            )
            result = await BehaviorAnalyzer().execute(target, config(), context)

        assert context.artifacts["trajectories"]
        assert result.applicable is True
        assert result.metrics["trajectories"] == 40

    async def test_a_broken_target_shows_poor_recovery(self):
        context = ScanContext()
        async with MockTarget.failing() as target:
            await FaultInjector(FaultConfig.uniform(0.2, seed=11)).execute(
                target, config(requests=60), context
            )
            result = await BehaviorAnalyzer().execute(target, config(), context)

        assert result.metrics["recovery_rate"] < 0.6
        assert result.metrics["loops_detected"] > 0

    def test_declares_the_behavior_phase(self):
        assert BehaviorAnalyzer.phase == "behavior"
        assert BehaviorAnalyzer.name == "behavior"
        assert BehaviorAnalyzer.rerun_under_fault is False
