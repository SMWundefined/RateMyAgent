"""BehaviorAnalyzer: phase 3.

Phase 2 broke things and recorded what happened. This phase reads those
trajectories and answers the question the whole tool exists for: **what did the
target actually do when things went wrong?**

Not "did it fail" -- a target that returns an error when you inject a 500 is
behaving correctly. The interesting questions are downstream of the failure:

- Did it come back, and how long did that take?
- How many calls did one logical operation end up costing?
- How many calls did this scan re-send after it lost or damaged a reply the
  target had acknowledged? Reported as the scanner's own and never scored:
  whether a re-sent call was applied twice is in the target's state, which
  nothing here reads.
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
from . import agent_metrics, repeats
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
    description = "retry patterns, recovery, re-sent calls and loops from phase 2"
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

        agent_runs = (context.artifacts.get("agent_runs") if context else None) or []
        agent_tasks = context.artifacts.get("agent_tasks") if context else None
        repeat_groups: list[repeats.RepeatGroup] | None = None
        if len(agent_runs) > 1:
            # **Repeats, and the worst run is the one the trajectory metrics
            # describe.** Which run that is has to be decided before `_analyze`
            # reads the trajectories, so it is decided here on the agent
            # metrics alone -- they need no trajectories, only the record and
            # the oracle's windows.
            clean = context.artifacts.get("agent_clean_calls") if context else None
            per_run = [
                repeats.RunRecord(
                    metrics=_agent_run_metrics(run, clean), placement=run["placement"]
                )
                for run in agent_runs
            ]
            repeat_groups = repeats.summarize(
                per_run, REPEATED_METRICS, directions=_metric_directions()
            )
            worst = _worst_run_index(per_run)
            chosen = agent_runs[worst]
            trajectories = chosen["trajectories"]
            metrics = _analyze(trajectories)
            metrics["max_retries"] = (
                context.artifacts.get("max_retries") if context else None
            )
            metrics["fault_rate"] = fault_config.get("total_rate")
            metrics["recovery_floor"] = recovery_floor(
                metrics["fault_rate"], metrics["max_retries"]
            )
            agent_tasks = chosen["tasks"]
            context.artifacts["agent_tasks"] = agent_tasks
            context.artifacts["agent_rows"] = chosen["rows_by_task"]
            context.artifacts["realized_schedule"] = chosen["realized"]
            context.artifacts["realized_placement"] = chosen["placement"]

        if agent_tasks is not None:
            # An agent target (C2). Effects are attributed per task window, the
            # claim is joined with the record, and the timing metrics read wall
            # clock off the rows. See `agent_metrics` for why each reading uses
            # the source it does.
            metrics.update(agent_metrics.effect_metrics(
                agent_tasks, context.artifacts.get("agent_clean_calls"),
            ))
            metrics.update(agent_metrics.timing_metrics(
                context.artifacts.get("agent_rows") or {}
            ))
            # Replaces the delivery-based count `_analyze` produced: on this
            # path the count is of tasks with a call whose outcome the agent
            # could not know, read off the record. It has its own key;
            # `duplicate_opportunities` keeps its server meaning only and is
            # not exported here.
            metrics.pop("duplicate_opportunities", None)
            metrics.update(agent_metrics.opportunity_metrics(
                agent_tasks, context.artifacts.get("agent_rows") or {},
            ))
            metrics["scheduled_faults"] = context.artifacts.get("scheduled_faults")
            # Which calls the table actually caught, in order. Carried here as
            # well as on the fault probe because this is the probe a repeat
            # groups by, and a run's placement has to travel with the numbers it
            # explains rather than one probe away from them.
            metrics["realized_schedule"] = context.artifacts.get("realized_schedule") or []
            metrics["realized_placement"] = context.artifacts.get("realized_placement") or ""
            # **Reported, not scored.** The floor `1 - fault_rate**max_retries`
            # is derived from *our* retry budget, and against an agent the
            # budget is the agent's, which this scan neither sets nor knows.
            # Scoring against it would be `concurrency_min` again: a check
            # comparing a flag against itself.
            metrics["unscored_recovery_rate"] = metrics.get("recovery_rate")
            metrics["recovery_rate"] = None
            metrics["recovery_floor"] = None
            metrics.update(_llm_withholding(target, metrics))
            if repeat_groups is not None:
                metrics.update(_repeat_metrics(repeat_groups, metrics))
        else:
            # The state oracle (1.4.0). Everything the behaviour probe knows
            # about applied effects arrives here as data from phase 2; this
            # probe never reads the target.
            metrics.update(_effect_metrics(
                (context.artifacts.get("effect_oracle") if context else None) or {},
                trajectories,
            ))

        metrics["caller_strategy_applicable"] = target.runs_own_retry_loop
        if not target.runs_own_retry_loop:
            for name in CALLER_STRATEGY_METRICS:
                metrics[f"caller_{name}"] = metrics.get(name)
                metrics[name] = None
            if agent_tasks is not None:
                for name in agent_metrics.AGENT_STRATEGY_METRICS:
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
            # `duplicate_mutations` is not guarded here any more: since 1.3.1 it
            # is withheld on every scan, in `_analyze`, for a reason that holds
            # whether or not anything completed.
            for name in ("retry_amplification",):
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


#: Metrics a repeat run reports a range for. Every one is read from the record
#: or the oracle's windows, so it can be computed per run without trajectories
#: -- which is what lets the worst run be chosen before `_analyze` sees one.
REPEATED_METRICS = (
    "duplicate_mutations",
    "lost_effects",
    "unsupported_claims",
    "lost_acknowledgements",
    "uncertain_tasks",
    "retry_amplification",
    "calls_under_fault",
)


def _metric_directions() -> dict[str, str]:
    """Which way is worse, per metric, from the policy rather than from a list.

    A second table of directions here would be a second thing to keep in step
    with `THRESHOLD_SPECS`, and the metrics that are scored already have one.
    A metric with no entry has no worst, and `repeats.MetricRepeat.worst`
    withholds rather than guessing a direction.
    """
    from ..policy import THRESHOLD_SPECS

    return {spec.metric: spec.direction for spec in THRESHOLD_SPECS}


def _agent_run_metrics(run: dict[str, Any], clean: Any) -> dict[str, Any]:
    """One run's agent metrics, with no trajectory analysis in them."""
    out = dict(agent_metrics.effect_metrics(run["tasks"], clean))
    out.update(agent_metrics.timing_metrics(run["rows_by_task"]))
    out.update(agent_metrics.opportunity_metrics(run["tasks"], run["rows_by_task"]))
    return out


def _worst_run_index(per_run: list["repeats.RunRecord"]) -> int:
    """Which run the trajectory-level metrics describe.

    **A stated choice, not arithmetic.** Scoring takes the worst observed value
    *per metric* (`repeats.scored_metrics`), but the trajectory metrics --
    attempts, recovery, loops -- are a description of one run and cannot be
    assembled from several. So one run has to be named, and it is the run worst
    on `duplicate_mutations`: the metric with an absolute cap, the one a user
    opens this report about, and the one whose run they will want to read.
    Ties go to the earliest, so the choice is reproducible.

    A run whose count is withheld does not win the comparison -- `None` is "we
    could not tell", and promoting it to worst would hand the report a run with
    nothing in it.
    """
    best = 0
    seen = -1.0
    for index, run in enumerate(per_run):
        value = run.metrics.get("duplicate_mutations")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > seen:
                seen, best = float(value), index
    return best


def _repeat_metrics(
    groups: list["repeats.RepeatGroup"], metrics: dict[str, Any]
) -> dict[str, Any]:
    """The repeat reporting, and the worst-run values the policy reads.

    **Scored from the worst observed run, per metric, across every group.**
    Grouping by realized placement decides how ranges are *reported* -- runs
    that faulted different calls are not replicates and do not share a range --
    but it does not soften the score. A duplicate that happened in one run of
    five is a duplicate whichever group it landed in.

    A metric already withheld above stays withheld: `_llm_withholding` runs
    first, and a value put back here would re-report what the previous step
    just declined to read.
    """
    total = sum(group.n for group in groups)
    out: dict[str, Any] = {
        "repeats": total,
        "repeat_groups": len(groups),
        "repeat_scoring": repeats.describe_scoring(
            repeats.RepeatGroup(placement="", runs=[r for g in groups for r in g.runs])
        ),
        "repeat_ranges": {
            metric: {
                "placement": group.placement,
                "runs": group.n,
                "values": group.metrics[metric].values,
                "rendered": group.metrics[metric].render(),
            }
            for group in groups
            for metric in group.metrics
        } if len(groups) == 1 else {},
        "repeat_by_group": [
            {
                "placement": group.placement,
                "runs": group.n,
                "metrics": {
                    metric: group.metrics[metric].render() for metric in group.metrics
                },
            }
            for group in groups
        ],
    }
    split = repeats.describe_placements(groups)
    if split:
        out["repeat_placement_split"] = split

    for metric, value in repeats.scored_metrics(
        repeats.RepeatGroup(
            placement="",
            runs=[r for g in groups for r in g.runs],
            metrics={
                name: repeats.MetricRepeat(
                    metric=name,
                    values=[r.metrics.get(name) for g in groups for r in g.runs],
                    direction=_metric_directions().get(name),
                )
                for name in REPEATED_METRICS
            },
        )
    ).items():
        if metrics.get(metric) is not None:
            out[metric] = value
    return out


#: Timing metrics withheld outright against an LLM agent. Not "unscored":
#: **withheld**, and the value is not stashed under another key either.
#:
#: These are wall-clock gaps between attempts. When a retry loop produces the
#: gap, the gap is a schedule. When a model produces it, the gap is one
#: inference round trip -- a second or two of token generation that has nothing
#: to do with waiting and everything to do with thinking -- and `growing` would
#: be reported for a model whose second response happened to be longer than its
#: first. Publishing the number under `unscored_backoff_shape` would invite
#: exactly the reading we are saying is unavailable, so there is no such key.
LLM_WITHHELD_METRICS = (
    "backoff_shape",
    "backoff_growth",
    "retry_after_honored",
    # The ratio in count form. Withholding one and publishing the other would
    # be withholding nothing.
    "retry_after_honored_count",
)


def _llm_withholding(target: "Target", metrics: dict[str, Any]) -> dict[str, Any]:
    """What an LLM agent is not entitled to have scored or reported.

    **`retry_amplification` is unscored and still reported**, because its parts
    are real: `calls_under_fault` and `clean_path_calls` are counts of calls
    that happened. What is not real is the *ratio*, because the denominator was
    measured on the clean pass where no fault was injected, and against a model
    it is a random variable -- two clean runs of one task can differ by a
    `tools/list` the model decided to make, and that difference lands in the
    denominator of a fault-tolerance metric. A reported 1.6x may be entirely a
    model that was chattier on one pass than the other, and the noise runs in
    the direction that looks like a finding.

    **The timing metrics are withheld**, which is a stronger statement than
    unscored: see `LLM_WITHHELD_METRICS`.

    Both are keyed on `agent_kind`, which the user declares. A scripted fixture
    has a real retry loop with real sleeps, so nothing here applies to it and
    the Phase C gate results are untouched.
    """
    from ..targets.agent import AGENT_KIND_LLM, AGENT_KIND_SCRIPTED

    kind = getattr(target, "agent_kind", AGENT_KIND_SCRIPTED)
    out: dict[str, Any] = {"agent_kind": kind}
    if kind != AGENT_KIND_LLM:
        return out

    out["unscored_retry_amplification"] = metrics.get("retry_amplification")
    out["retry_amplification"] = None
    out["amplification_unscored_reason"] = "llm_denominator"
    # The raw counts stay exactly as they were: they are observations, and only
    # the ratio built from them is the thing that cannot be trusted.
    for name in LLM_WITHHELD_METRICS:
        out[name] = None
    return out


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
    opportunities = sum(t.duplicate_opportunities for t in trajectories)
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
        # **Withheld on every target, since 1.3.1.** A duplicate *mutation* is
        # an effect applied twice, and effects live in the target's state, which
        # this scanner never reads. 1.3.0 scored the count below as this metric:
        # an idempotent write tool and a non-idempotent one, given identical
        # faults, reported the same number and were both capped at 49
        # (`tests/test_duplicate_deliveries.py`). The idempotent one had applied
        # nothing twice.
        #
        # Name, policy key and cap semantics are unchanged. Nothing can feed the
        # check until something reads the target's state (NextSteps, effect
        # oracle). `unscored_duplicate_mutations` is None as well, deliberately:
        # the house rule puts the raw value there, and the raw value here is a
        # delivery count -- the wrong number under the wrong name.
        "duplicate_mutations": None,
        "unscored_duplicate_mutations": None,
        # The scanner's own count, reported and never scored, and labelled
        # "(ours)" the way retry amplification is. Every unit is an act of this
        # scan: the proxy lost or damaged a reply the target acknowledged, and
        # the retry loop re-sent the call. Σ `Trajectory.duplicates`, which is
        # the frozen field this number is read from.
        "duplicate_deliveries": duplicates,
        # Acknowledged deliveries whose reply the caller did not see: how many
        # chances the scan had to re-send anything. Derived from trajectories
        # rather than the fault config, so a fault that was enabled and never
        # drawn does not count.
        "duplicate_opportunities": opportunities,
        "loops_detected": len(loops),
        "operations_failed": len(failed_final),
        "operation_failure_rate": (len(failed_final) / total) if total else 0.0,
        "final_status_counts": dict(Counter(t.final_status for t in trajectories)),
        "injected_faults_by_kind": dict(fault_counter),
        "unrecovered_by_fault_kind": dict(unrecovered_faults),
    }


def _effect_metrics(
    oracle: dict[str, Any], trajectories: list[Trajectory]
) -> dict[str, Any]:
    """Turn the oracle's reading into scored and reported numbers.

    `duplicate_mutations` counts **every** registered operation's excess
    effects, with no delivered gate. Gating on `executed is True` would drop the
    case that matters most -- a real timeout the proxy cannot confirm, followed
    by a retry that applies the work again -- which is absence read as presence:
    "we could not confirm the delivery" turned into "there was no duplicate".
    The gate survives as a report, `effects_without_acknowledged_delivery`.
    """
    status = oracle.get("effect_oracle_status", "absent")
    effects: dict[str, int] = oracle.get("effects_by_op") or {}
    by_key = {t.trajectory_id: t for t in trajectories}

    data: dict[str, Any] = {
        "effect_oracle_status": status,
        "operations_registered": oracle.get("operations_registered", 0),
        # Operations that had a chance to duplicate: a retry went out after the
        # target had acknowledged one attempt. The denominator a clean zero
        # needs -- zero duplicates over zero opportunities is the absence of a
        # test, not evidence of idempotency.
        "duplicate_opportunities": sum(
            1 for t in trajectories if t.duplicate_opportunities
        ),
    }
    for key in ("observed_effects", "unattributed_effects", "op_ids"):
        if key in oracle:
            data[key] = oracle[key]

    if status != "ok":
        # Absent, unattributed, stale or failed: nothing is scored, and the raw
        # reading (when there is one) is reported rather than dropped.
        data["duplicate_mutations"] = None
        data["lost_effects"] = None
        return data

    data["effects_by_op"] = dict(effects)
    data["duplicate_mutations"] = sum(max(0, count - 1) for count in effects.values())
    data["lost_effects"] = sum(
        1 for key, count in effects.items()
        if count == 0 and any(inv.ok for inv in by_key[key].invocations)
    ) if by_key else 0
    data["effects_without_acknowledged_delivery"] = sum(
        1 for key, count in effects.items()
        if count >= 1 and key in by_key
        and not any(inv.executed is True for inv in by_key[key].invocations)
    )
    data["operations_succeeded"] = sum(
        1 for t in trajectories if any(inv.ok for inv in t.invocations)
    )
    return data


def _caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """Limits of this phase's evidence, kept out of the findings list.

    The thin-sample entry is the one that argued for the channel. `fault` emits
    the same sentence about the same number, and it rendered CRITICAL here and
    plain there -- not because the two differ, but because `CRITICAL_CHECKS`
    maps to `behavior` and not to `fault`. A caveat now has no severity to
    inherit.
    """
    if metrics.get("effect_attribution") == "task_window":
        return _agent_caveats(metrics)

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

    # Five states, each licensing a different claim (1.4.0). Only "ok" scores.
    status = metrics.get("effect_oracle_status", "absent")
    if status == "absent":
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations",),
            effect="suppress",
            reason=(
                "The scanner observes delivered calls, not applied effects, so it "
                "cannot tell a repeated mutation from an idempotent retry."
            ),
            remedy="--verify-tool, with {op_id} in --tool-args",
        ))
    elif status == "unattributed":
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations", "lost_effects"),
            effect="suppress",
            reason=(
                "The verify tool answered, but --tool-args carries no {op_id}, so "
                "effects cannot be attributed to an operation -- and in aggregate a "
                "duplicated mutation and a lost effect cancel out."
            ),
            remedy="{op_id} in an argument the server stores",
        ))
    elif status == "stale":
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations", "lost_effects"),
            effect="suppress",
            reason=(
                "State from a previous scan with this seed is already present, so "
                "a count of what this window applied cannot be separated from what "
                "the last one did."
            ),
            remedy="a different --seed, or clear the target's state",
        ))
    elif status == "failed":
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations", "lost_effects"),
            effect="suppress",
            reason=(
                "The verify tool did not answer, so applied effects are unknown. "
                "That is not the same as none, and it is not scored as zero."
            ),
            remedy="check the verify tool and its --verify-count path",
        ))
    else:
        if not metrics.get("duplicate_opportunities"):
            caveats.append(Caveat(
                probe="behavior",
                metrics=("duplicate_mutations",),
                effect="annotate",
                reason=(
                    "No operation had a chance to duplicate: none was re-sent after "
                    "the target had acknowledged it. The zero is scored, and it is a "
                    "zero over no opportunities."
                ),
                remedy="--fault-rate or --requests",
            ))
        # The window is the recovery pass, and nothing else. Ground truth on the
        # twin fixture found an effect applied twice *outside* it -- the
        # preflight call and the first baseline operation carry the same
        # `{op_id}` -- so the design's claim that recovery is the only place a
        # duplicate can arise was wrong. The scope is stated rather than the
        # window widened: counting the baseline would mix a probe's own traffic
        # into a number about retries.
        caveats.append(Caveat(
            probe="behavior",
            metrics=("duplicate_mutations", "lost_effects"),
            effect="annotate",
            reason=(
                "This counts effects applied during the retried operations only. "
                "Effects from the preflight call, the baseline probes or the "
                "degradation pass are outside the window and are not counted."
            ),
            remedy=None,
        ))
        if metrics.get("unattributed_effects"):
            caveats.append(Caveat(
                probe="behavior",
                metrics=("duplicate_mutations", "lost_effects"),
                effect="annotate",
                reason=(
                    f"{metrics['unattributed_effects']} effects in this window match "
                    "no operation this scan sent, so something else is writing to the "
                    "target and the window is not clean."
                ),
                remedy=None,
            ))

    if metrics.get("nothing_completed"):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification",),
            effect="suppress",
            reason="No operation completed, so there were no calls to amplify.",
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


def _agent_caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """What an agent scan could not establish (C2).

    A separate function rather than branches through `_caveats`, because
    almost every sentence there is about the scanner's own retry loop and
    would be false here, and a server scan's caveats must not move.
    """
    caveats: list[Caveat] = [Caveat(
        probe="behavior",
        metrics=("recovery_rate",),
        effect="suppress",
        reason=(
            "Reported, not scored: the retry budget is the agent's, so the "
            "derived floor 1 - fault_rate**max_retries has nothing to be "
            "derived from."
        ),
        remedy=None,
    )]

    effects = ("duplicate_mutations", "lost_effects", "lost_acknowledgements")
    status = metrics.get("effect_oracle_status")
    if status == "absent":
        caveats.append(Caveat(
            probe="behavior",
            metrics=effects,
            effect="suppress",
            reason=(
                "Nothing read the upstream's state, so applied effects are "
                "unmeasured, and an agent scan gets no verdict without them. "
                "Unsupported claims are still read off the record."
            ),
            remedy="--verify-tool and --verify-count",
        ))
    elif status != "ok":
        unread = [
            task for task, value in (metrics.get("task_oracle_status") or {}).items()
            if value != "ok"
        ]
        caveats.append(Caveat(
            probe="behavior",
            metrics=effects,
            effect="suppress",
            reason=(
                f"The verify tool did not answer around "
                f"{'task' if len(unread) == 1 else 'tasks'} {', '.join(unread)}, so "
                "applied effects are unknown. That is not the same as none, and it "
                "is not scored as zero."
            ),
            remedy="check the verify tool and its --verify-count path",
        ))
    else:
        caveats.append(Caveat(
            probe="behavior",
            metrics=effects,
            effect="annotate",
            reason=(
                "Counted per task window: the upstream's state is read before and "
                "after each task, one task at a time. Which attempt applied an "
                "extra effect is not visible from the two endpoints."
            ),
            remedy=None,
        ))
        if not metrics.get("uncertain_tasks"):
            caveats.append(Caveat(
                probe="behavior",
                metrics=("duplicate_mutations",),
                effect="annotate",
                reason=(
                    "No task had a call whose outcome was unknown, so no agent "
                    "could have applied anything twice. The zero is not evidence, "
                    "and the scan gets no verdict."
                ),
                remedy="--fault-rate",
            ))

    if metrics.get("nothing_completed"):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification",),
            effect="suppress",
            reason="No operation completed, so there were no calls to amplify.",
            remedy=None,
        ))
    elif metrics.get("retry_amplification") is None and metrics.get(
        "caller_strategy_applicable"
    ):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification",),
            effect="suppress",
            reason=(
                "No clean-path call count to divide by: agent_baseline did not "
                "run, and for an agent one attempt is whatever its clean run makes."
            ),
            remedy="--probes agent_baseline,fault,behavior",
        ))

    if metrics.get("retry_after_retries"):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_after_honored",),
            effect="annotate",
            reason=(
                "The Retry-After hint travels in the tool error body, because "
                "stdio has no headers. That is a convention, not a standard a "
                "real client is bound to read, so a low value can mean the hint "
                "was never seen rather than ignored."
            ),
            remedy=None,
        ))
    withheld_for_llm = metrics.get("agent_kind") == "llm"
    if withheld_for_llm:
        # **Before the two suppressions below, and they are skipped.** Those
        # say "no retry followed a rate limit" and "no two waits were long
        # enough to measure", which are statements about what the run saw. Here
        # the run may well have seen both; we are declining to read them. A
        # withheld metric carrying the reason for a different absence is the
        # defect this project keeps cataloguing -- the number is gone either
        # way, and the sentence beside it is the only thing that says why.
        caveats.append(Caveat(
            probe="behavior",
            metrics=("backoff_shape", "backoff_growth", "retry_after_honored"),
            effect="suppress",
            reason=(
                "Withheld against an LLM agent. These read wall-clock gaps "
                "between attempts: when a retry loop produces the gap it is a "
                "schedule, and when a model produces it the gap is an inference "
                "round trip. A model that pauses is not a model that is backing "
                "off, and nothing in the record tells waiting from thinking -- "
                "so `growing` would be reported for a model whose second "
                "response was simply longer than its first."
            ),
            remedy=None,
        ))
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification",),
            effect="suppress",
            reason=(
                "Reported and not scored against an LLM agent. The denominator "
                "is the clean-pass call count, measured where no fault was "
                "injected at all, and a model chooses its own calls -- two clean "
                "runs of one task can differ by a `tools/list` it felt like "
                "making. `calls_under_fault` and `clean_path_calls` are counts "
                "of calls that happened and are reported as such; the ratio "
                "built from them is not a measurement of fault tolerance until "
                "the denominator's spread is reported with it."
            ),
            remedy=None,
        ))
    if not metrics.get("caller_strategy_applicable"):
        caveats.append(Caveat(
            probe="behavior",
            metrics=("retry_amplification", "backoff_shape", "retry_after_honored"),
            effect="suppress",
            reason=(
                "This target does not declare its own retry loop, so the retry "
                "strategy measured is not attributable to it."
            ),
            remedy=None,
        ))
    elif not withheld_for_llm:
        if metrics.get("retry_after_honored") is None:
            caveats.append(Caveat(
                probe="behavior",
                metrics=("retry_after_honored",),
                effect="suppress",
                reason="No retry followed a rate limit carrying a hint.",
                remedy=None,
            ))
        if metrics.get("backoff_shape") is None:
            caveats.append(Caveat(
                probe="behavior",
                metrics=("backoff_shape", "backoff_growth"),
                effect="suppress",
                reason=(
                    "No two consecutive waits after a delivered failure were long "
                    "enough to measure."
                ),
                remedy=None,
            ))
    return caveats


def _agent_findings(metrics: dict[str, Any]) -> list[str]:
    """Findings for an agent scan, each naming the tasks it is about."""
    findings: list[str] = []
    duplicates = metrics.get("duplicate_mutations")
    if duplicates:
        detail = ", ".join(
            f"{task} applied {count} of "
            f"{metrics['expected_effects_by_task'][task]}"
            for task, count in (metrics.get("duplicate_mutation_tasks") or {}).items()
        )
        findings.append(
            f"{duplicates} extra {'effect' if duplicates == 1 else 'effects'} "
            f"applied under retry ({detail}). The agent re-sent a write the "
            "upstream had already applied, with nothing that let the upstream "
            "recognize the repeat -- an idempotency key reused across attempts is "
            "the usual fix."
        )

    claims = metrics.get("unsupported_claims") or 0
    if claims:
        detail = ", ".join(
            f"{task} (effects {'n/a' if count is None else count})"
            for task, count in (metrics.get("unsupported_claim_tasks") or {}).items()
        )
        findings.append(
            f"The agent reported success on {claims} "
            f"{'task' if claims == 1 else 'tasks'} whose record holds no "
            f"successful reply: {detail}. Nothing it was told supports the claim. "
            "Not scored in this release."
        )

    lost = metrics.get("lost_effects")
    if lost:
        findings.append(
            f"The upstream answered success and applied nothing on {lost} "
            f"{'task' if lost == 1 else 'tasks'} "
            f"({', '.join(metrics.get('lost_effect_tasks') or [])}). That is the "
            "server's fault, not the agent's: the agent reported what it was "
            "told. Not scored."
        )

    acknowledged = metrics.get("lost_acknowledgements")
    if acknowledged:
        findings.append(
            f"{acknowledged} {'task' if acknowledged == 1 else 'tasks'} "
            f"({', '.join(metrics.get('lost_acknowledgement_tasks') or [])}) "
            "reported failure although the work was applied: the reply was lost "
            "and the agent said so honestly. Reported only."
        )

    amplification = metrics.get("retry_amplification")
    if amplification is not None and amplification > AMPLIFICATION_WARN:
        findings.append(
            f"Retry amplification is {amplification:.2f}x: "
            f"{metrics.get('calls_under_fault')} calls under fault against "
            f"{metrics.get('clean_path_calls')} on the clean path. During a real "
            "incident this multiplies load onto an already failing dependency."
        )

    shape = metrics.get("backoff_shape")
    if shape == "flat" or shape == "shrinking":
        findings.append(
            f"The agent's waits between retries are {shape} "
            f"(median ratio {metrics['backoff_growth']:.2f}), so repeated failures "
            "are met at the same pace or faster. Not scored."
        )
    honored = metrics.get("retry_after_honored")
    if honored is not None and honored < 1.0:
        findings.append(
            f"{metrics['retry_after_retries'] - metrics['retry_after_honored_count']}"
            f" of {metrics['retry_after_retries']} retries after a rate limit went "
            "out before the Retry-After hint had elapsed. Not scored."
        )

    deliveries = metrics.get("duplicate_deliveries") or 0
    if deliveries:
        findings.append(
            f"The agent re-sent {deliveries} "
            f"{'call' if deliveries == 1 else 'calls'} the upstream had already "
            "acknowledged, after this scan dropped or damaged the reply. Whether "
            "any applied twice is the duplicate-mutation count, read from state."
        )

    if metrics["loops_detected"]:
        findings.append(
            f"{metrics['loops_detected']} operations made three or more attempts "
            "without ever succeeding."
        )
    return findings


def _summarize(metrics: dict[str, Any]) -> str:
    if metrics.get("effect_attribution") == "task_window":
        return _agent_summary(metrics)
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
    # The scanner's count, labelled as its own the way amplification is. Not
    # "duplicate mutations": that is a claim about the target's state, and until
    # 1.3.1 this line made it from a count of calls this scan re-sent.
    deliveries = metrics.get("duplicate_deliveries") or 0
    duplicates = (
        f"{deliveries} duplicate {'delivery' if deliveries == 1 else 'deliveries'} (ours)"
    )
    if metrics.get("nothing_completed"):
        duplicates += ", nothing completed"
    shown = rate if rate is not None else metrics.get("unscored_recovery_rate")
    seen = f" ({shown:.0%})" if shown is not None else ""
    withheld = ", not scored on this sample" if rate is None else ""
    return (
        f"{metrics['recovered']}/{disrupted} disrupted operations recovered"
        f"{seen}" + (f" {budget}" if budget else "") + withheld
        + f", {amp}, {duplicates}"
    )


def _agent_summary(metrics: dict[str, Any]) -> str:
    """One line, with every agent-side count labelled as the agent's.

    The server summary says "(ours)" about re-sent calls, which against an agent
    is false: the agent re-sent them. C1 fixed that sentence in the findings and
    left this one, which is the one-branch-not-its-twin shape.
    """
    tasks = len(metrics.get("task_claims") or {})
    amplification = metrics.get("retry_amplification")
    amp = (
        f"{amplification:.2f}x amplification"
        if amplification is not None else "amplification n/a"
    )
    duplicates = metrics.get("duplicate_mutations")
    dup = (
        f"{duplicates} duplicate mutations"
        if duplicates is not None else "duplicate mutations n/a"
    )
    return (
        f"{tasks} tasks, {metrics.get('disrupted') or 0} operations disrupted, "
        f"{dup}, {metrics.get('unsupported_claims') or 0} unsupported claims, {amp}"
    )


def _findings(metrics: dict[str, Any]) -> list[str]:
    if metrics.get("effect_attribution") == "task_window":
        return _agent_findings(metrics)
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

    # Reported, attributed to whoever actually re-sent it, and never scored --
    # the amplification finding's shape, and the same branch, because it is the
    # same question. Against a service the retry loop is the scanner's and every
    # unit of this is something this scan did; against an agent the loop is the
    # target's, and calling the agent's re-send "ours" would misattribute the
    # one number Phase C exists to put on the agent.
    deliveries = metrics.get("duplicate_deliveries") or 0
    if deliveries:
        whose = (
            "The target's own retry loop"
            if metrics.get("caller_strategy_applicable")
            else "This scan's own retry loop"
        )
        findings.append(
            f"{whose} re-sent {deliveries} "
            f"{'call' if deliveries == 1 else 'calls'} the target had already "
            "acknowledged, after this scan dropped or damaged the reply. Reported for "
            "context and deliberately not scored: the scanner observes delivered calls, "
            "not applied effects, so it cannot tell a repeated mutation from an "
            "idempotent retry."
        )

    if metrics["loops_detected"]:
        findings.append(
            f"{metrics['loops_detected']} operations made three or more attempts without "
            "ever succeeding. Retrying past the point where it can help spends the "
            "dependency's capacity on calls that were never going to land."
        )

    return findings


__all__ = ["AMPLIFICATION_WARN", "SLOW_RECOVERY_S", "BehaviorAnalyzer"]
