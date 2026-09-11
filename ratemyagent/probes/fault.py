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

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..formatting import format_seconds
from ..models import Caveat, ErrorKind, FaultKind, ProbeResult, Response, Trajectory
from ..targets.fault_proxy import ALL_FAULTS, OPT_IN_FAULTS, FaultConfig, FaultProxy
from .base import Probe, ProbeConfig, ScanContext

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

DEFAULT_FAULT_RATE = 0.2
DEFAULT_MAX_RETRIES = 2

#: Disrupted operations below which a recovery rate is not worth quoting at all.
#: Renamed from `MIN_DISRUPTED_FOR_CONFIDENCE`, value unchanged -- see the long
#: note on `MIN_DISRUPTED_TO_REPORT` in `behavior.py`. It is a reporting floor,
#: not a confidence guarantee, and never was one outside the A-F era.
MIN_DISRUPTED_TO_REPORT = 10


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
        #: None when the caller did not name one, so `ProbeConfig` supplies it.
        self._explicit_retries = None if max_retries == DEFAULT_MAX_RETRIES else max_retries

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()
        # The config wins: `max_retries` is a public option as of 1.0, and a
        # constructor argument stays available for direct probe use.
        if self._explicit_retries is None:
            self.max_retries = config.max_retries
        faults = self._faults or self._faults_from(config, target)
        proxy = FaultProxy(target, faults)

        degradation = await self._degradation_pass(proxy, config)
        recovery, trajectories = await self._recovery_pass(proxy, config)

        # Phase 3 analyses these. Handing them over through the context keeps the
        # behaviour probe from reaching into this one, and keeps both runnable
        # on their own.
        if context is not None:
            # Only the recovery pass, which retries. The degradation pass sends
            # one-shot probe traffic; counting those as operations would drag
            # retry amplification toward 1.0 and hide real disruption.
            context.artifacts["trajectories"] = trajectories
            context.artifacts["invocations"] = list(proxy.invocations)
            context.artifacts["fault_config"] = faults.to_dict()
            # "Recovered" means "came back within this many retries". The number
            # defines the metric, is hardcoded, and reaches no CLI flag, so the
            # least it can do is travel with the measurement it defines.
            context.artifacts["max_retries"] = self.max_retries

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
            caveats=_caveats(metrics),
            sample_count=len(proxy.invocations),
            error_rate=metrics["error_rate_under_fault"],
            duration_s=time.perf_counter() - started,
        )

    # -- passes --------------------------------------------------------------

    async def _degradation_pass(self, proxy: FaultProxy, config: ProbeConfig) -> dict[str, Any]:
        """Re-run the opted-in baseline probes through the proxy."""
        from . import fault_rerun_probes

        results = []
        for probe in fault_rerun_probes():
            results.append(await probe.execute(proxy, config))

        under_fault = {
            result.probe: {
                "error_rate": result.error_rate,
                "p95_s": result.metrics.get("p95_s"),
                "p50_s": result.metrics.get("p50_s"),
            }
            for result in results
        }
        return {"baseline_probes_under_fault": under_fault}

    async def _recovery_pass(
        self, proxy: FaultProxy, config: ProbeConfig
    ) -> tuple[dict[str, Any], list[Trajectory]]:
        """Send requests, retry failures, and read the trajectories.

        Requests start past the degradation pass's labels so the two passes
        cannot share a trajectory: a retry must be distinguishable from an
        unrelated call that happens to hit the same tool.
        """
        offset = config.warmup + config.requests
        requests = proxy.probe_requests(config.requests, offset=offset)
        keys = [request.trajectory_key for request in requests]

        backoff = _BackoffBudget(config.backoff_max_s, config.backoff_budget_s)

        for request in requests:
            for _ in range(self.max_retries + 1):
                response = await proxy.invoke(request)
                if response.ok:
                    break
                await backoff.wait_for(response)

        trajectories = [proxy.trajectories[key] for key in keys if key in proxy.trajectories]
        metrics = _trajectory_metrics(trajectories, proxy)
        metrics.update(backoff.metrics())
        return metrics, trajectories

    def _faults_from(self, config: ProbeConfig, target: "Target") -> FaultConfig:
        """The injection set for this scan.

        `RESPONSE_LOST` is added **only when the scan is cleared to mutate**, and
        the gate is the target's own `allow_mutating`, read the way the contract
        probe reads it.

        Two reasons, and they point the same way. Losing the reply to a
        read-only call produces a retry that reads twice, which is nothing; the
        failure worth finding is a mutation that runs twice, and a scan that has
        not been told it may mutate should not be executing one on purpose.
        And `uniform()` divides the total rate by the kind count, so adding a
        sixth kind to every scan would move every boundary in `_choose_fault`
        and re-assign every seeded draw ever recorded -- see `ALL_FAULTS`.

        The total fault rate is unchanged either way. With the opt-in kind the
        same `--fault-rate` is spread over six rather than five, so a scan does
        not become more hostile by enabling it, only differently so.
        """
        rate = config.extra.get("fault_rate", DEFAULT_FAULT_RATE)
        kinds = ALL_FAULTS
        if getattr(target, "allow_mutating", False):
            kinds = (*ALL_FAULTS, *OPT_IN_FAULTS)
        return FaultConfig.uniform(rate, kinds, seed=config.seed)


class _BackoffBudget:
    """Waits after a rate limit, up to a ceiling, up to a total.

    **Triggered by `ErrorKind.RATE_LIMIT`, never by the presence of a hint.**
    The case this exists for -- a stdio server relaying an upstream 429 as a
    tool result -- has no `Retry-After` anywhere, and is classified by matching
    the message text. Keying on the hint would have waited politely for our own
    injected faults, which always carry one, and hammered the only real rate
    limiter in the corpus. The hint refines the wait; it does not cause it.

    **A waited retry is still one of `max_retries`.** That is deliberate and it
    makes a rate-limited dependency strictly harder to recover from inside a
    fixed budget, which is the finding rather than a distortion: the derived
    floor is `1 - fault_rate ** max_retries` and its derivation depends on
    exactly that many draws. Giving rate limits extra attempts would break the
    floor and quietly flatter the case the tool is meant to expose.

    **Exhaustion continues without waiting rather than stopping.** Reverting
    silently to hammering is the failure this release fixes, so the count of
    un-waited retries is recorded and surfaced as a caveat.
    """

    def __init__(self, per_retry_max_s: float, budget_s: float) -> None:
        self._max = per_retry_max_s
        self._remaining = budget_s
        self.budget_s = budget_s
        self.waited_s = 0.0
        self.simulated_s = 0.0
        self.waits = 0
        self.unwaited = 0

    async def wait_for(self, response: Response) -> None:
        if response.error_kind is not ErrorKind.RATE_LIMIT:
            return
        if self._max <= 0 or self._remaining <= 0:
            self.unwaited += 1
            return

        hint = response.meta.get("retry_after_s")
        wanted = float(hint) if isinstance(hint, (int, float)) else self._max
        delay = min(wanted, self._max, self._remaining)
        if delay <= 0:
            self.unwaited += 1
            return

        self._remaining -= delay
        self.waits += 1

        # An *injected* 429 is our own fiction, and the fault is seeded per
        # (trajectory, attempt) -- so a retry draws the same fault however long
        # we wait. Sleeping for one costs wall clock and cannot change the
        # outcome, which is measuring the harness.
        #
        # The same rule the mock target already follows: it reports a drawn
        # latency and sleeps `latency * sleep_scale`, default zero, so a
        # 200-request profile of a 3-second target finishes instantly while the
        # arithmetic stays real. A simulated fault gets a simulated wait, and
        # both are counted so the reported numbers and the caveats are true.
        #
        # A real rate limit -- a server relaying an upstream 429 -- is the case
        # waiting exists for, and gets the clock.
        if response.meta.get("injected") or response.meta.get("simulated"):
            self.simulated_s += delay
            return

        self.waited_s += delay
        await asyncio.sleep(delay)

    def metrics(self) -> dict[str, Any]:
        return {
            "backoff_waits": self.waits,
            "backoff_waited_s": round(self.waited_s, 3),
            # Waits that were accounted but not slept, because the fault was
            # ours. Reported separately so a fast scan is not mistaken for a
            # scan that did not back off.
            "backoff_simulated_s": round(self.simulated_s, 3),
            "backoff_unwaited": self.unwaited,
            "backoff_budget_s": self.budget_s,
            "backoff_budget_exhausted": self.unwaited > 0 and self._remaining <= 0,
        }


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
        f"operations recovered ({rate:.0%}) {describe_budget(metrics['max_retries'])}, "
        f"{metrics['retry_amplification']:.2f}x call amplification"
    )


def describe_budget(max_retries: int | None) -> str:
    """"within 2 retries" -- the clause that turns a rate into a measurement.

    A recovery rate is meaningless without the budget it was measured against:
    "100% recovered" answers a different question at one retry than at ten. The
    budget is a hardcoded 2 that no CLI flag reaches (`resolve_probes` builds
    every probe with no arguments), it defines what `recovery_rate_min` scores,
    and until 0.1.12 it appeared in two findings and nowhere else -- not in the
    summary line, not in the metric table, and not in the behaviour probe that
    reports the same number to the policy engine.

    Making it configurable is a separate decision, and a heavier one: it would
    change what "recovered" means and break comparability with every number this
    project has published. Disclosure does not.
    """
    if max_retries is None:
        return ""
    return f"within {max_retries} {'retry' if max_retries == 1 else 'retries'}"


def _caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """What this phase could not establish, kept out of the findings list.

    Three statements about the run rather than the target. The third is the one
    that made the case for the channel: the identical sentence is emitted by
    `behavior` too, and rendered CRITICAL there and plain here, purely because
    `CRITICAL_CHECKS` maps to `behavior` and not to `fault`.
    """
    caveats: list[Caveat] = []

    if not metrics["injected"]:
        caveats.append(Caveat(
            probe="fault",
            metrics=(),
            scope="probe",
            effect="suppress",
            reason=(
                "No faults were injected, so nothing in this phase was tested "
                "under failure."
            ),
            remedy="--fault-rate above 0",
        ))
        return caveats

    if metrics.get("backoff_unwaited"):
        caveats.append(Caveat(
            probe="fault",
            metrics=("recovery_rate",),
            effect="annotate",
            reason=(
                f"The {metrics['backoff_budget_s']:.0f}s backoff budget ran out "
                f"and {metrics['backoff_unwaited']} rate-limited "
                f"{'retry' if metrics['backoff_unwaited'] == 1 else 'retries'} "
                "went out without waiting. Those retries measure a dependency "
                "this scan was still pressing, not one it let recover."
            ),
            remedy="--backoff-budget, or fewer --requests",
        ))

    if metrics["recovery_rate"] is None:
        caveats.append(Caveat(
            probe="fault",
            metrics=("recovery_rate",),
            effect="suppress",
            reason=(
                "No operation was disrupted on its first attempt, so recovery "
                "was never exercised."
            ),
            remedy="--fault-rate or --requests",
        ))
    elif metrics["disrupted"] < MIN_DISRUPTED_TO_REPORT:
        bound = 3 / metrics["disrupted"]
        caveats.append(Caveat(
            probe="fault",
            metrics=("recovery_rate",),
            effect="annotate",
            reason=(
                f"{metrics['disrupted']} disrupted operations bounds the "
                f"failure-to-recover rate at roughly {bound:.0%} rather than "
                "measuring it."
            ),
            remedy="--fault-rate or --requests",
        ))

    return caveats


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    if not metrics["injected"]:
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
        pass  # a caveat, not a finding: see _caveats()
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
            f"Mean recovery takes {format_seconds(latency)} from first failure to success. "
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
        findings.append(
            f"Under fault the {name} probe saw a {observed['error_rate']:.0%} error rate"
            + (f", p95 {format_seconds(observed['p95_s'])}." if observed.get("p95_s") else ".")
        )

    return findings


__all__ = ["FaultConfig", "FaultInjector", "FaultKind"]
