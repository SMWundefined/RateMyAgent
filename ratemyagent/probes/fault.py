"""FaultInjector: phase 2, chaos.

Two passes against one FaultProxy-wrapped target:

1. Degradation -- re-run the phase 1 baseline probes through the proxy. Any
   difference from their phase 1 result is caused by the faults, because
   nothing else about the run changed.
2. Recovery -- send fresh requests and retry the ones that fail, so each
   logical operation produces a Trajectory. This is what answers "does it
   recover", which no amount of error-rate counting can.

This probe never fabricates a failure itself. Every fault comes from the
FaultProxy, so the probe stays readable and the injection logic stays in one
place.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..models import FaultKind, Grade, ProbeResult, Trajectory
from ..targets.fault_proxy import FaultConfig, FaultProxy
from .base import Probe, ProbeConfig

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

DEFAULT_FAULT_RATE = 0.2
DEFAULT_MAX_RETRIES = 2

#: Disrupted operations needed before a recovery rate is worth a top grade.
#: Under the rule of three, a clean run of n disrupted operations only bounds
#: the failure rate at 3/n, so 2-for-2 is not evidence of a resilient system.
MIN_DISRUPTED_FOR_CONFIDENCE = 10

# Minimum recovery rate for each grade, best first.
RECOVERY_THRESHOLDS: tuple[tuple[Grade, float], ...] = (
    (Grade.A, 0.95),
    (Grade.B, 0.90),
    (Grade.C, 0.80),
    (Grade.D, 0.60),
)


class FaultInjector(Probe):
    """Breaks things on purpose and measures what happens next."""

    name = "fault"
    description = "injects timeouts, 429s, 500s, malformed responses and refused connections"
    phase = "chaos"

    def __init__(
        self,
        faults: FaultConfig | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._faults = faults
        self.max_retries = max_retries

    async def run(self, target: "Target", config: ProbeConfig) -> ProbeResult:
        started = time.perf_counter()
        faults = self._faults or self._faults_from(config)
        proxy = FaultProxy(target, faults)

        degradation = await self._degradation_pass(proxy, config)
        recovery = await self._recovery_pass(proxy, config)

        metrics: dict[str, Any] = {
            "faults": faults.to_dict(),
            "max_retries": self.max_retries,
            "calls": len(proxy.invocations),
            "injected": proxy.injected_count,
            "injected_by_kind": proxy.injected_by_kind(),
            "injection_rate": (
                proxy.injected_count / len(proxy.invocations) if proxy.invocations else 0.0
            ),
            **degradation,
            **recovery,
        }

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            sample_count=len(proxy.invocations),
            error_rate=metrics["error_rate_under_fault"],
            duration_s=time.perf_counter() - started,
        )

    def grade(self, result: ProbeResult) -> Grade:
        """Graded on recovery, not on failure count.

        A target that fails when we inject a 500 is behaving correctly. The
        question phase 2 asks is whether it comes back, so a run where nothing
        was injected cannot be graded and returns None-equivalent F only when
        recovery was genuinely possible and did not happen.
        """
        recovery_rate = result.metrics.get("recovery_rate")
        if recovery_rate is None:
            # Nothing failed, so nothing had to recover. Not evidence of health.
            return Grade.A if result.metrics.get("injected", 0) == 0 else Grade.C

        grade = _grade_recovery(recovery_rate)

        # Too few disrupted operations to justify a confident grade. Capping
        # rather than failing: the evidence is thin, not bad.
        if result.metrics.get("disrupted", 0) < MIN_DISRUPTED_FOR_CONFIDENCE:
            grade = Grade.worst([grade, Grade.C])

        # A repeated mutation is a correctness bug, not a slow recovery. The
        # shipped policy sets duplicate_mutation_max to 0 for this reason.
        if result.metrics.get("duplicate_mutations", 0) > 0:
            grade = Grade.worst([grade, Grade.D])

        # loops_detected is deliberately NOT penalized here. While faults are
        # injected independently per attempt, an operation that exhausts its
        # retries is exactly an operation that did not recover, so grading it
        # again would count one failure twice. It becomes an independent signal
        # in week 3, once the target does its own retrying.
        return grade

    # -- passes --------------------------------------------------------------

    async def _degradation_pass(self, proxy: FaultProxy, config: ProbeConfig) -> dict[str, Any]:
        """Re-run the opted-in baseline probes through the proxy."""
        from . import fault_rerun_probes

        results = []
        for probe in fault_rerun_probes():
            results.append(await probe.execute(proxy, config))

        under_fault = {
            result.probe: {
                "grade": result.grade.value if result.grade else None,
                "error_rate": result.error_rate,
                "p95_s": result.metrics.get("p95_s"),
                "p50_s": result.metrics.get("p50_s"),
            }
            for result in results
        }
        return {"baseline_probes_under_fault": under_fault}

    async def _recovery_pass(self, proxy: FaultProxy, config: ProbeConfig) -> dict[str, Any]:
        """Send requests, retry failures, and read the trajectories.

        Requests start past the degradation pass's labels so the two passes
        cannot share a trajectory: a retry must be distinguishable from an
        unrelated call that happens to hit the same tool.
        """
        offset = config.warmup + config.requests
        requests = proxy.probe_requests(config.requests, offset=offset)
        keys = [request.trajectory_key for request in requests]

        for request in requests:
            for _ in range(self.max_retries + 1):
                response = await proxy.invoke(request)
                if response.ok:
                    break

        trajectories = [proxy.trajectories[key] for key in keys if key in proxy.trajectories]
        return _trajectory_metrics(trajectories, proxy)

    def _faults_from(self, config: ProbeConfig) -> FaultConfig:
        rate = config.extra.get("fault_rate", DEFAULT_FAULT_RATE)
        return FaultConfig.uniform(rate, seed=config.seed)


def _trajectory_metrics(trajectories: list[Trajectory], proxy: FaultProxy) -> dict[str, Any]:
    total = len(trajectories)
    attempts = sum(t.attempts for t in trajectories)
    failed_first = [t for t in trajectories if t.invocations and not t.invocations[0].ok]
    recovered = [t for t in failed_first if t.recovered]
    recovery_latencies = [
        t.recovery_latency_s for t in recovered if t.recovery_latency_s is not None
    ]

    calls = len(proxy.invocations)
    failures = sum(1 for inv in proxy.invocations if not inv.ok)

    return {
        "trajectories": total,
        "attempts": attempts,
        "retries": sum(t.retries for t in trajectories),
        # 1.0 means one call per operation; 2.0 means every operation cost two.
        "retry_amplification": (attempts / total) if total else 0.0,
        "disrupted": len(failed_first),
        "recovered": len(recovered),
        "recovery_rate": (len(recovered) / len(failed_first)) if failed_first else None,
        "mean_recovery_latency_s": (
            sum(recovery_latencies) / len(recovery_latencies) if recovery_latencies else None
        ),
        "duplicate_mutations": sum(t.duplicates for t in trajectories),
        "loops_detected": sum(1 for t in trajectories if t.loops_detected),
        "unrecovered": [t.trajectory_id for t in failed_first if not t.recovered][:10],
        "error_rate_under_fault": (failures / calls) if calls else 0.0,
        "final_status_counts": _count(t.final_status for t in trajectories),
    }


def _count(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _summarize(metrics: dict[str, Any]) -> str:
    injected = metrics["injected"]
    rate = metrics["recovery_rate"]
    if rate is None:
        return f"{injected} faults injected, nothing needed recovery"
    return (
        f"{injected} faults injected, {metrics['recovered']}/{metrics['disrupted']} "
        f"operations recovered ({rate:.0%}), "
        f"{metrics['retry_amplification']:.2f}x call amplification"
    )


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    if not metrics["injected"]:
        findings.append(
            "No faults were injected, so this phase proved nothing. "
            "Raise --fault-rate above 0 to exercise failure handling."
        )
        return findings

    kinds = ", ".join(
        f"{count} {kind}" for kind, count in sorted(
            metrics["injected_by_kind"].items(), key=lambda item: -item[1]
        )
    )
    findings.append(
        f"Injected {metrics['injected']} faults across {metrics['calls']} calls "
        f"({metrics['injection_rate']:.0%}): {kinds}."
    )

    rate = metrics["recovery_rate"]
    if rate is None:
        findings.append(
            "No operation was disrupted on its first attempt, so recovery behavior is "
            "untested. Raise --fault-rate or --requests for a meaningful sample."
        )
    elif rate < 1.0:
        unrecovered = metrics["disrupted"] - metrics["recovered"]
        findings.append(
            f"{unrecovered}/{metrics['disrupted']} disrupted operations never recovered "
            f"({rate:.0%} recovery rate) within {metrics['max_retries']} retries. "
            "These are the calls that would surface to a user as a hard failure."
        )
    else:
        findings.append(
            f"Every one of the {metrics['disrupted']} disrupted operations recovered "
            f"within {metrics['max_retries']} retries."
        )

    if rate is not None and metrics["disrupted"] < MIN_DISRUPTED_FOR_CONFIDENCE:
        bound = 3 / metrics["disrupted"]
        findings.append(
            f"Only {metrics['disrupted']} operations were disrupted, which bounds the "
            f"failure-to-recover rate at roughly {bound:.0%} rather than measuring it. "
            "The grade is capped at C until more operations are disrupted -- raise "
            "--fault-rate or --requests."
        )

    amplification = metrics["retry_amplification"]
    if amplification > 2.0:
        findings.append(
            f"Retry amplification is {amplification:.2f}x: {metrics['attempts']} calls for "
            f"{metrics['trajectories']} operations. Under a real incident this multiplies "
            "load on an already failing dependency."
        )

    latency = metrics["mean_recovery_latency_s"]
    if latency is not None and latency > 5.0:
        findings.append(
            f"Mean recovery takes {latency:.1f}s from first failure to success. "
            "That is user-visible even when the retry eventually works."
        )

    if metrics["duplicate_mutations"]:
        findings.append(
            f"{metrics['duplicate_mutations']} operations succeeded more than once. "
            "If any of those calls mutate state, the retry duplicated it."
        )

    # No separate loop finding: with faults injected independently per attempt,
    # the operations that exhausted their retries are the same ones the recovery
    # line already named. Reporting it twice would inflate one failure into two.

    for name, observed in metrics["baseline_probes_under_fault"].items():
        if observed["grade"]:
            findings.append(
                f"Under fault the {name} probe grades {observed['grade']} "
                f"with a {observed['error_rate']:.0%} error rate."
            )

    return findings


def _grade_recovery(recovery_rate: float) -> Grade:
    for grade, minimum in RECOVERY_THRESHOLDS:
        if recovery_rate >= minimum:
            return grade
    return Grade.F


__all__ = ["FaultConfig", "FaultInjector", "FaultKind"]
