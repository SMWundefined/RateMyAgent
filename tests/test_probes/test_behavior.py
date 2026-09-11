"""BehaviorAnalyzer: phase 3 reading phase 2's trajectories."""

from __future__ import annotations

import pytest

from ratemyagent.models import FaultKind, Invocation, Trajectory
from ratemyagent.probes import ProbeConfig, ScanContext
from ratemyagent.probes.behavior import (
    AMPLIFICATION_WARN,
    MIN_DISRUPTED_TO_REPORT,
    BehaviorAnalyzer,
)
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.targets import FaultConfig, MockTarget


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 30, "warmup": 0, "timeout_s": 5.0}
    return ProbeConfig(**{**defaults, **kwargs})


_UNSET = object()


def _inv(seq, ok, *, started=0.0, latency=1.0, fingerprint="op:a", injected=None,
         executed=_UNSET):
    return Invocation(
        sequence=seq, op="op", fingerprint=fingerprint, trajectory_id="t",
        attempt=seq + 1, ok=ok, latency_s=latency, started_at=started, injected=injected,
        executed=(True if ok else None) if executed is _UNSET else executed,
    )


def trajectory(*oks, **kw) -> Trajectory:
    """A trajectory from a sequence of pass/fail attempts.

    `executed` defaults the way `FaultProxy._executed_from` does -- a success
    ran, a failure is unknown -- so these fixtures keep matching the proxy. Pass
    `executed=[...]` for the case that default cannot express: a call the target
    ran whose reply the caller never saw.
    """
    injected = kw.get("injected")
    executed = kw.get("executed")
    return Trajectory(
        kw.get("tid", "t"),
        [
            _inv(i, ok, started=float(i), injected=(injected if not ok else None),
                 **({"executed": executed[i]} if executed else {}))
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
    """Amplification is still computed against a service target -- it is just not
    *scored*, because the retry loop producing it is the scanner's. It moves to
    `caller_retry_amplification`, which is reported and never checked."""

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
        assert result.metrics["caller_retry_amplification"] == pytest.approx(2.0)
        assert result.metrics["retry_amplification"] is None, "must not be scored"

    async def test_no_retries_means_amplification_of_one(self):
        ctx = context_with(trajectory(True, tid="a"), trajectory(True, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["caller_retry_amplification"] == pytest.approx(1.0)

    async def test_high_amplification_is_called_out(self):
        ctx = context_with(*[trajectory(False, False, True, tid=f"t{i}") for i in range(12)])
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["caller_retry_amplification"] > AMPLIFICATION_WARN
        # Still surfaced, and attributed to us rather than to the target.
        finding = next(f for f in result.findings if "amplification" in f)
        assert "own retry loop" in finding
        assert "not scored" in finding

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
        # A caveat, not a finding: it describes the run, not the target.
        assert any("never exercised" in c.reason for c in result.caveats)
        assert all("never exercised" not in f for f in result.findings)

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

        assert result.metrics["disrupted"] < MIN_DISRUPTED_TO_REPORT
        # The wording carries the Wilson interval now, not a rule-of-three
        # bound: the interval is what says whether the sample can separate the
        # target from the injector's own arithmetic.
        thin = [c for c in result.caveats if "95% interval" in c.reason]
        assert thin and thin[0].effect == "annotate"
        assert "scored from it regardless" in thin[0].reason, (
            "an annotate caveat must say the number is still scored"
        )
        assert thin[0].metrics == ("recovery_rate",)
        # The point of the channel: `fault` emits the same sentence about the
        # same number, and it used to render CRITICAL here and plain there.
        assert not hasattr(thin[0], "severity")


class TestDuplicatesAndLoops:
    async def test_a_repeated_success_is_a_duplicate_mutation(self):
        ctx = context_with(trajectory(True, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_mutations"] == 1
        assert any("succeeded more than once" in f for f in result.findings)

    async def test_an_executed_call_whose_reply_was_lost_is_a_duplicate(self):
        """The case the metric was built for and could never see.

        One `ok`, so the old rule -- repeated *successes* -- counted zero. The
        target ran the call twice: once for the attempt whose reply was dropped,
        once for the retry. That is the duplicate mutation.
        """
        ctx = context_with(trajectory(False, True, executed=[True, True], tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_mutations"] == 1

    async def test_an_unknown_execution_is_never_a_duplicate(self):
        """`None` means the caller cannot tell, which is not evidence of work.

        Under-counting is the right direction: a duplicate this reports is one
        the proxy can account for.
        """
        ctx = context_with(trajectory(False, True, executed=[None, True], tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        # Withheld rather than zero: no opportunity arose, so there is nothing
        # to have a zero *of*.
        assert result.metrics["duplicate_mutations"] is None
        assert result.metrics["duplicate_opportunities"] == 0

    async def test_retries_that_fail_are_not_duplicates(self):
        """Failures the target never ran give no evidence either way."""
        ctx = context_with(trajectory(False, False, True, tid="a"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_mutations"] is None
        assert result.metrics["duplicate_opportunities"] == 0

    async def test_an_opportunity_with_no_duplicate_is_a_real_zero(self):
        """The result the old rule could never distinguish from silence.

        The target ran a call whose reply was lost and the retry did *not* run
        it again -- an idempotent tool under at-least-once delivery. That is a
        scored zero, not a withheld one.
        """
        ctx = context_with(trajectory(
            False, True, executed=[True, False], tid="a",
        ))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["duplicate_opportunities"] == 1
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


class TestNothingCompleted:
    """A check that can only fail when something happened reports a pass when
    nothing happened. `duplicate_mutations: 0` across zero completed operations
    is the absence of activity, not evidence of idempotency -- and with recovery
    and amplification withheld it was carrying the whole 35-point dimension,
    scoring full marks on a run where every request failed."""

    async def test_duplicate_mutations_is_withheld(self):
        ctx = context_with(*[trajectory(False, False, False, tid=f"t{i}") for i in range(4)])
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["operation_failure_rate"] == 1.0
        assert result.metrics["duplicate_mutations"] is None
        assert result.metrics["unscored_duplicate_mutations"] == 0

    async def test_the_dimension_stops_being_scored_at_all(self):
        """With all three checks withheld the behaviour weight renormalises out,
        rather than awarding 35/35 for an empty run."""
        from ratemyagent.models import ScanResult, TargetInfo
        from ratemyagent.policy import Policy, evaluate

        ctx = context_with(*[trajectory(False, False, False, tid=f"t{i}") for i in range(4)])
        # The real pipeline publishes this from the baseline latency probe;
        # without it recovery is still scored, which is correct -- we only know
        # the run was doomed if the baseline said so.
        ctx.artifacts["baseline_error_rate"] = 1.0
        async with MockTarget.healthy() as target:
            probe = await BehaviorAnalyzer().execute(target, config(), ctx)

        scored = evaluate(
            ScanResult(target=TargetInfo(name="t", kind="mcp"), probes=[probe]),
            Policy(thresholds={"recovery_rate_min": 0.9, "retry_amplification_max": 2.0,
                               "duplicate_mutation_max": 0.0}),
        )
        behaviour = next(d for d in scored.breakdown if d.probe == "behavior")
        assert behaviour.measured is False, "an empty run must not score 35/35"

    async def test_a_run_with_successes_does_not_trip_this_guard(self):
        """Named for the guard, because two guards now withhold this metric.

        It used to assert `duplicate_mutations is not None` as a stand-in for
        "the nothing-completed guard did not fire". Since 1.3.0 the metric is
        also withheld when no opportunity for a duplicate arose, which is the
        case here -- so the old assertion would now fail for a reason this class
        is not about. Assert the guard, not a side effect of it.
        """
        ctx = context_with(trajectory(True, tid="a"), trajectory(False, True, tid="b"))
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert result.metrics["operation_failure_rate"] < 1.0
        assert not result.metrics.get("nothing_completed")
        assert result.metrics["duplicate_opportunities"] == 0

    async def test_the_summary_survives_every_metric_being_withheld(self):
        ctx = context_with(*[trajectory(False, False, False, tid=f"t{i}") for i in range(4)])
        async with MockTarget.healthy() as target:
            result = await BehaviorAnalyzer().execute(target, config(), ctx)

        assert "nothing completed" in result.summary or "not scored" in result.summary
