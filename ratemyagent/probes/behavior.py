"""BehaviorAnalyzer: phase 3.

Phase 2 broke things and recorded what happened. This phase reads those
trajectories and answers the question the whole tool exists for: **what did the
target actually do when things went wrong?**

Not "did it fail" -- a target that returns an error when you inject a 500 is
behaving correctly. The interesting questions are downstream of the failure:

- Did it come back, and how long did that take?
- How many calls did one logical operation end up costing?
- Did anything succeed *twice*, which for a mutation means it ran twice?
- Did anything spin without ever resolving?

This probe measures the target. It never sends traffic of its own -- everything
here comes from the invocations the FaultProxy already observed, so running it
costs nothing and cannot perturb what it is measuring.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..models import ProbeResult, Trajectory
from .base import Probe, ProbeConfig, ScanContext

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

#: Retry amplification above this is worth calling out on its own: one failure
#: turning into this many calls is what turns a blip into an outage.
AMPLIFICATION_WARN = 2.0

#: Recovery slower than this is user-visible even when the retry works.
SLOW_RECOVERY_S = 5.0

#: Disrupted operations needed before a recovery rate is worth quoting. Under
#: the rule of three, n clean recoveries only bound the failure rate at 3/n --
#: and this number is scored by `recovery_rate_min`, so a thin sample moves the
#: overall score on almost no evidence. Say so rather than let it pass quietly.
MIN_DISRUPTED_FOR_CONFIDENCE = 10


class BehaviorAnalyzer(Probe):
    """Reads phase 2's trajectories and reports what the target did."""

    name = "behavior"
    description = "retry patterns, recovery, duplicate mutations and loops from phase 2"
    phase = "behavior"

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        trajectories: list[Trajectory] = list(
            (context.artifacts.get("trajectories") if context else None) or []
        )

        if not trajectories:
            return ProbeResult(
                probe=self.name,
                phase=self.phase,
                applicable=False,
                summary="no trajectories to analyse",
                metrics={"trajectories": 0, "applicable": False},
                findings=[
                    "Phase 3 analyses what phase 2 recorded, and this scan has no "
                    "trajectories. Run the chaos phase too -- `--phases baseline,chaos,"
                    "behavior`, or just drop --phases to run all three."
                ],
                duration_s=time.perf_counter() - started,
            )

        metrics = _analyze(trajectories)

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            sample_count=len(trajectories),
            error_rate=metrics["operation_failure_rate"],
            duration_s=time.perf_counter() - started,
        )


def _analyze(trajectories: list[Trajectory]) -> dict[str, Any]:
    total = len(trajectories)
    attempts = sum(t.attempts for t in trajectories)

    # "Disrupted" means the first attempt failed. Recovery is only meaningful
    # for those: an operation that never broke did not recover from anything.
    disrupted = [t for t in trajectories if t.invocations and not t.invocations[0].ok]
    recovered = [t for t in disrupted if t.recovered]
    unrecovered = [t for t in disrupted if not t.recovered]

    latencies = [t.recovery_latency_s for t in recovered if t.recovery_latency_s is not None]
    retried = [t for t in trajectories if t.retries > 0]

    duplicates = sum(t.duplicates for t in trajectories)
    loops = [t for t in trajectories if t.loops_detected]
    failed_final = [t for t in trajectories if t.final_status == "failed"]

    fault_counter: Counter[str] = Counter()
    for trajectory in trajectories:
        for fault in trajectory.injected_faults:
            fault_counter[fault.value] += 1

    # Which injected fault most often ended in an operation that never came back.
    unrecovered_faults: Counter[str] = Counter()
    for trajectory in unrecovered:
        for fault in trajectory.injected_faults:
            unrecovered_faults[fault.value] += 1

    return {
        "applicable": True,
        "trajectories": total,
        "attempts": attempts,
        "retries": sum(t.retries for t in trajectories),
        "operations_retried": len(retried),
        # 1.0 means one call per operation. 2.0 means every operation cost two.
        "retry_amplification": (attempts / total) if total else 0.0,
        "max_attempts_single_operation": max((t.attempts for t in trajectories), default=0),
        "disrupted": len(disrupted),
        "recovered": len(recovered),
        "unrecovered": len(unrecovered),
        "recovery_rate": (len(recovered) / len(disrupted)) if disrupted else None,
        "mean_recovery_latency_s": (sum(latencies) / len(latencies)) if latencies else None,
        "max_recovery_latency_s": max(latencies) if latencies else None,
        "duplicate_mutations": duplicates,
        "loops_detected": len(loops),
        "operations_failed": len(failed_final),
        "operation_failure_rate": (len(failed_final) / total) if total else 0.0,
        "final_status_counts": dict(Counter(t.final_status for t in trajectories)),
        "injected_faults_by_kind": dict(fault_counter),
        "unrecovered_by_fault_kind": dict(unrecovered_faults),
    }


def _summarize(metrics: dict[str, Any]) -> str:
    rate = metrics["recovery_rate"]
    amplification = metrics["retry_amplification"]

    if rate is None:
        return (
            f"{metrics['trajectories']} operations, none disrupted, "
            f"{amplification:.2f}x amplification"
        )
    return (
        f"{metrics['recovered']}/{metrics['disrupted']} disrupted operations recovered "
        f"({rate:.0%}), {amplification:.2f}x call amplification, "
        f"{metrics['duplicate_mutations']} duplicate mutations"
    )


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    rate = metrics["recovery_rate"]

    if rate is None:
        findings.append(
            f"None of the {metrics['trajectories']} operations was disrupted on its first "
            "attempt, so recovery behaviour is untested. Raise --fault-rate to exercise it."
        )
    elif metrics["unrecovered"]:
        worst = ", ".join(
            f"{count} after {kind}"
            for kind, count in sorted(
                metrics["unrecovered_by_fault_kind"].items(), key=lambda kv: -kv[1]
            )[:3]
        )
        findings.append(
            f"{metrics['unrecovered']}/{metrics['disrupted']} disrupted operations never "
            f"recovered ({rate:.0%} recovery rate)"
            + (f", most often {worst}." if worst else ".")
            + " These are the calls a user would experience as a hard failure."
        )
    else:
        findings.append(
            f"Every one of the {metrics['disrupted']} disrupted operations recovered."
        )

    if rate is not None and metrics["disrupted"] < MIN_DISRUPTED_FOR_CONFIDENCE:
        bound = 3 / metrics["disrupted"]
        findings.append(
            f"Only {metrics['disrupted']} operations were disrupted, which bounds the "
            f"failure-to-recover rate at roughly {bound:.0%} rather than measuring it. "
            "The recovery_rate_min policy check is scored from this number, so raise "
            "--requests or --fault-rate before trusting it in CI."
        )

    amplification = metrics["retry_amplification"]
    if amplification > AMPLIFICATION_WARN:
        findings.append(
            f"Retry amplification is {amplification:.2f}x: {metrics['attempts']} calls for "
            f"{metrics['trajectories']} operations, peaking at "
            f"{metrics['max_attempts_single_operation']} attempts on a single operation. "
            "During a real incident this multiplies load onto an already failing dependency, "
            "which is how a partial outage becomes a total one."
        )

    mean_recovery = metrics["mean_recovery_latency_s"]
    if mean_recovery is not None and mean_recovery > SLOW_RECOVERY_S:
        findings.append(
            f"Recovery takes {mean_recovery:.1f}s on average and up to "
            f"{metrics['max_recovery_latency_s']:.1f}s. The retry works, but the caller "
            "waits through the whole thing."
        )

    if metrics["duplicate_mutations"]:
        findings.append(
            f"{metrics['duplicate_mutations']} operations succeeded more than once. If any "
            "of those calls mutate state, the retry duplicated the mutation -- the failure "
            "mode that turns a retried payment into two payments."
        )

    if metrics["loops_detected"]:
        findings.append(
            f"{metrics['loops_detected']} operations made three or more attempts without "
            "ever succeeding. Retrying past the point where it can help spends the "
            "dependency's capacity on calls that were never going to land."
        )

    return findings


__all__ = ["AMPLIFICATION_WARN", "SLOW_RECOVERY_S", "BehaviorAnalyzer"]
