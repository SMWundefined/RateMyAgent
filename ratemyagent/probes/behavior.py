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

from ..formatting import format_seconds
from ..models import Caveat, ProbeResult, Trajectory
from .base import Probe, ProbeConfig, ScanContext, recovery_floor, wilson_interval
from .fault import describe_budget

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

#: Retry amplification above this is worth calling out on its own: one failure
#: turning into this many calls is what turns a blip into an outage.
AMPLIFICATION_WARN = 2.0

#: Recovery slower than this is user-visible even when the retry works.
SLOW_RECOVERY_S = 5.0

#: Disrupted operations below which a recovery rate is not worth quoting at all.
#:
#: **Renamed from `MIN_DISRUPTED_FOR_CONFIDENCE`, value unchanged.** That name
#: promised a confidence guarantee it never delivered: `CLAUDE.md` and the
#: roadmap both described it as capping the grade below ten disrupted
#: operations, and section 11 of PROGRESS had already recorded that it caps
#: nothing -- "a constant named for a guarantee it no longer provides". The cap
#: was real in the A-F era and did not survive the week-4 migration to policy
#: scoring; the finding did.
#:
#: What it does is set a floor for *reporting* a rate, which is a much weaker
#: claim and now the name says so. The guarantee the old name implied is
#: provided instead by `_recovery_is_decidable`, which suppresses the metric
#: when the Wilson interval cannot separate it from the injector's own
#: arithmetic -- and that has no constant, because the floor falls out of the
#: interval rather than being chosen.
#:
#: Ten is not defended by evidence and is not pretending to be: distinguishing a
#: target from the injector at r = 0.2 needs roughly 100 disrupted operations
#: for 80% power. Ten is where a number stops being worth printing, not where it
#: starts being worth trusting.
MIN_DISRUPTED_TO_REPORT = 10



#: Metrics that describe the *caller's* retry behaviour rather than the target's.
#: Withheld unless the target runs its own retry loop.
CALLER_STRATEGY_METRICS = ("retry_amplification",)


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

        # Split the dimension. Survivability -- did the session keep answering,
        # and did its state survive a retry -- is real against a service.
        # Caller strategy is not: the retry loop is ours, so reporting its shape
        # as the target's behaviour measures the harness. Withhold those metrics
        # rather than score them, which drops them from the dimension mean while
        # leaving its 35 points with survivability.
        # Recovery is not a measurement when nothing could have recovered.
        # Both synthesized-argument rows report 0% recovery, which reads as the
        # target failing to come back. Every request failed because the
        # arguments were invalid, so every retry failed for the same reason,
        # against a server that was correctly rejecting garbage throughout.
        # Retrying cannot help when the input is the problem.
        #
        # Exact 1.0, not a threshold near it. Both affected rows sit at exactly
        # 1.0, and 0.95 would be a number with nothing behind it -- a cliff
        # invented to look careful.
        metrics["max_retries"] = (
            context.artifacts.get("max_retries") if context else None
        )

        # The threshold this metric is scored against is a property of the
        # flags, not of the target, so it travels with the measurement.
        fault_config = (context.artifacts.get("fault_config") if context else None) or {}
        metrics["fault_rate"] = fault_config.get("total_rate")
        metrics["recovery_floor"] = recovery_floor(
            metrics["fault_rate"], metrics["max_retries"]
        )

        baseline = (context.artifacts.get("baseline_error_rate") if context else None)
        if baseline == 1.0 and metrics.get("recovery_rate") is not None:
            metrics["recovery_rate_baseline_error"] = baseline
            metrics["unscored_recovery_rate"] = metrics["recovery_rate"]
            metrics["recovery_rate"] = None

        metrics["caller_strategy_applicable"] = target.runs_own_retry_loop
        if not target.runs_own_retry_loop:
            for name in CALLER_STRATEGY_METRICS:
                metrics[f"caller_{name}"] = metrics.get(name)
                metrics[name] = None

        # Nothing succeeded, so nothing can be concluded from what did not go
        # wrong. `duplicate_mutations: 0` across zero completed operations is
        # the absence of activity, not evidence of idempotency -- and with the
        # other two behaviour checks withheld it was carrying the whole 35-point
        # dimension, scoring full marks on a run where every request failed.
        #
        # `retry_amplification` has the same exposure and is closed with it:
        # attempts over zero operations is 0.0, which passes a max threshold.
        # Unreachable today because it is withheld for service targets, but it
        # becomes reachable the moment AgentTarget exists, and the guard costs
        # one line.
        #
        # Exact 1.0, matching the recovery rule: a fuzzy threshold here would be
        # a cliff with nothing behind it.
        if metrics.get("operation_failure_rate") == 1.0:
            metrics["nothing_completed"] = True
            for name in ("duplicate_mutations", "retry_amplification"):
                if metrics.get(name) is not None:
                    metrics[f"unscored_{name}"] = metrics[name]
                    metrics[name] = None

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            caveats=_caveats(metrics),
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


def _caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """Limits of this phase's evidence, kept out of the findings list.

    The thin-sample entry is the one that argued for the channel. `fault` emits
    the same sentence about the same number, and it rendered CRITICAL here and
    plain there -- not because the two differ, but because `CRITICAL_CHECKS`
    maps to `behavior` and not to `fault`. A caveat now has no severity to
    inherit.
    """
    caveats: list[Caveat] = []
    rate = metrics.get("recovery_rate")

    if rate is None and metrics.get("disrupted") in (0, None):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("recovery_rate",),
            effect="suppress",
            reason=(
                f"None of the {metrics['trajectories']} operations was disrupted "
                "on its first attempt, so recovery was never exercised."
            ),
            remedy="--fault-rate",
        ))
    elif rate is not None:
        # Report the interval against the floor; do not act on it.
        #
        # An earlier version of this change *withheld* `recovery_rate` whenever
        # the interval spanned the floor. Measured across the section 9 set that
        # fired on all nine rows, and simulation showed why: a target that never
        # fails has a true recovery rate of exactly `1 - r**retries`, which *is*
        # the floor, so its interval contains the floor about 95% of the time at
        # every sample size -- 10 or 10,000. That is a retirement dressed as a
        # confidence rule, and it hands behaviour's 35 points to
        # `duplicate_mutations`, which cannot fail (the retry loop breaks on the
        # first success, so a trajectory has at most one). Same shape as the
        # checks 0.1.10 stopped scoring because they passed when nothing
        # happened.
        #
        # So the arithmetic is published and the scoring is left alone. A reader
        # can see that 6/7 spans its floor; the score does not pretend the
        # sample settled anything, and it does not silently move points either.
        low, high = wilson_interval(metrics.get("recovered") or 0, metrics["disrupted"])
        floor = metrics.get("recovery_floor")
        spans = floor is not None and low <= floor <= high
        thin = metrics["disrupted"] < MIN_DISRUPTED_TO_REPORT
        if spans or thin:
            against = (
                f", which spans the {floor:.1%} this fault rate produces against a "
                "target that never fails"
                if spans else ""
            )
            caveats.append(Caveat(
                probe="behavior",
                metrics=("recovery_rate",),
                effect="annotate",
                reason=(
                    f"{metrics['recovered']}/{metrics['disrupted']} disrupted "
                    f"operations recovered, a 95% interval of {low:.1%}-{high:.1%}"
                    f"{against}. recovery_rate_min is scored from it regardless."
                ),
                remedy="--requests or --fault-rate",
            ))

    if metrics.get("nothing_completed"):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations", "retry_amplification"),
            effect="suppress",
            reason=(
                "No operation completed, so nothing could run twice and there "
                "were no calls to amplify."
            ),
            remedy=None,
        ))

    if metrics.get("recovery_rate_baseline_error") == 1.0:
        caveats.append(Caveat(
            probe="behavior",
            metrics=("recovery_rate",),
            effect="suppress",
            reason=(
                "Every baseline request already failed, so nothing could have "
                "recovered."
            ),
            remedy="--tool-args, or a target that answers",
        ))

    if metrics.get("caller_strategy_applicable") is False:
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification",),
            effect="suppress",
            reason=(
                "A server does not retry; this scanner does. The amplification "
                "measured describes RateMyAgent, not the target."
            ),
            remedy=None,
        ))

    return caveats


def _summarize(metrics: dict[str, Any]) -> str:
    rate = metrics["recovery_rate"]
    amplification = metrics["retry_amplification"]
    # Reported for context even when it is not scored, labelled so nobody reads
    # it as the target's. Silently dropping it would be its own small lie.
    ours = metrics.get("caller_retry_amplification")
    if ours is None:
        ours = metrics.get("unscored_retry_amplification")
    if amplification is not None:
        amp = f"{amplification:.2f}x call amplification"
    elif ours is not None:
        amp = f"{ours:.2f}x amplification (ours)"
    else:
        amp = "no completed operations"

    # `recovery_rate is None` used to be read as "nothing was disrupted", which
    # was already loose -- the baseline-error and caller-strategy paths both
    # null it too -- and became wrong outright once an undecidable interval
    # started withholding it. Branch on the count, which is the thing that
    # actually says whether anything broke.
    disrupted = metrics.get("disrupted") or 0
    if not disrupted:
        return f"{metrics['trajectories']} operations, none disrupted, {amp}"

    budget = describe_budget(metrics.get("max_retries"))
    duplicates = (
        f"{metrics['duplicate_mutations']} duplicate mutations"
        if metrics.get("duplicate_mutations") is not None
        else "duplicate mutations not scored (nothing completed)"
    )
    shown = rate if rate is not None else metrics.get("unscored_recovery_rate")
    seen = f" ({shown:.0%})" if shown is not None else ""
    withheld = ", not scored on this sample" if rate is None else ""
    return (
        f"{metrics['recovered']}/{disrupted} disrupted operations recovered"
        f"{seen}" + (f" {budget}" if budget else "") + withheld
        + f", {amp}, {duplicates}"
    )


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    rate = metrics["recovery_rate"]

    if rate is None:
        pass  # a caveat, not a finding: see _caveats()
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
        budget = describe_budget(metrics.get("max_retries"))
        findings.append(
            f"Every one of the {metrics['disrupted']} disrupted operations recovered"
            + (f" {budget}." if budget else ".")
            + (" That budget is the scanner's, not the target's, and is not"
               " configurable." if budget else "")
        )

    # Reported either way, attributed correctly. Against a service the retry loop
    # is the scanner's, so the number is real but it is not the target's
    # behaviour -- saying so is the whole point of the split.
    scored_amplification = metrics.get("retry_amplification")
    amplification = (
        scored_amplification
        if scored_amplification is not None
        else metrics.get("caller_retry_amplification")
    )
    if amplification is not None and amplification > AMPLIFICATION_WARN:
        if scored_amplification is None:
            findings.append(
                f"This scan's own retry loop ran at {amplification:.2f}x amplification: "
                f"{metrics['attempts']} calls for {metrics['trajectories']} operations. "
                "Reported for context and deliberately not scored -- a server does not "
                "retry, the client does, so this describes RateMyAgent rather than the "
                "target. Scored against an AgentTarget, where the loop is the target's."
            )
        else:
            findings.append(
                f"Retry amplification is {amplification:.2f}x: {metrics['attempts']} calls "
                f"for {metrics['trajectories']} operations, peaking at "
                f"{metrics['max_attempts_single_operation']} attempts on a single "
                "operation. During a real incident this multiplies load onto an already "
                "failing dependency, which is how a partial outage becomes a total one."
            )

    mean_recovery = metrics["mean_recovery_latency_s"]
    if mean_recovery is not None and mean_recovery > SLOW_RECOVERY_S:
        findings.append(
            f"Recovery takes {format_seconds(mean_recovery)} on average and up to "
            f"{format_seconds(metrics['max_recovery_latency_s'])}. The retry works, but the caller "
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
