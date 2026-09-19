"""AgentBaseline: phase 1, for an agent target.

The four baseline probes measure a *service*: how fast it answers, what it
costs, where it saturates, whether it enforces its own schema. None of those is
a question about an agent, and §f of the Phase C design says why each one is
withheld. What an agent scan needs from phase 1 instead is the two denominators
nothing else can produce:

1. **Does the agent complete the task at all with no faults?** If it does not,
   every number the chaos phase produces is about a broken fixture, and the run
   should refuse rather than publish. This is `_refuse_unusable_baseline`
   (`scanner.py`) one level up, and the same argument: a scan measuring its own
   misconfiguration should refuse rather than score.

2. **How many tool calls does one clean run of the task take?** Retry
   amplification needs that denominator, and with an agent it cannot be assumed:
   "one attempt" is the agent's judgement, and the clean run is the only place
   to learn it. Against a service the same number comes from `attempts /
   operations` inside one pass, which works only because the scanner is the one
   deciding to retry.

It runs each task once with the schedule empty -- a table forcing no faults,
which is a different instruction to the proxy from no table at all.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..models import ProbeResult
from ..proxy import invocation_rows, read_record
from . import agent_deadline
from .agent_deadline import DEADLINE_PASS
from .base import Probe, ProbeConfig, ProbeRefusal, ScanContext
from .fault import _window_diff

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)


class AgentBaseline(Probe):
    """Runs every task once, unfaulted, and counts what a clean run costs."""

    name = "agent_baseline"
    description = "runs each task once with no faults: completion, and the clean call count"
    phase = "baseline"

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        if not getattr(target, "injects_out_of_process", False):
            # Gated on the declared capability, not on the class. A service
            # target has no tasks and no record to read, and reporting a zero
            # here would be a measurement of nothing.
            return ProbeResult(
                probe=self.name,
                phase=self.phase,
                applicable=False,
                summary="not an agent target",
                metrics={"applicable": False},
                duration_s=time.perf_counter() - started,
            )

        # Its own records. A shared file would carry this pass's clean calls
        # into the chaos pass's ordinals and trajectories.
        target.start_pass("baseline")
        # An empty table, not an absent one: the proxy must be told to force no
        # faults, rather than left to its seeded draw.
        target.write_schedule({})

        requests = target.probe_requests(config.requests)
        outcomes: dict[str, str] = {}
        failed: list[str] = []
        calls_by_task: dict[str, dict[str, int]] = {}
        latencies: dict[str, float] = {}
        oracle = getattr(target, "has_effect_oracle", False)
        effects: dict[str, int | None] = {}
        unseen: dict[str, int | None] = {}

        for request in requests:
            task_id = request.op
            before = await target.read_effect_entries() if oracle else None
            response = await target.invoke(request)
            after = await target.read_effect_entries() if oracle else None
            latencies[task_id] = response.latency_s
            outcomes[task_id] = response.meta.get("outcome", "unknown")

            rows = invocation_rows(read_record(target.record_path(task_id)))
            _refuse_if_unrecorded(task_id, rows, target)
            calls_by_task[task_id] = _count_by_tool(rows)

            if not response.ok:
                failed.append(task_id)
            elif oracle and any(row.get("ok") is True for row in rows):
                # A clean run the agent says worked, with a success reply on
                # the record: the upstream applied the task, so the oracle has
                # to see exactly that. If it does not, every count in the chaos
                # pass is read from a state the agent never wrote to.
                effects[task_id] = _window_diff(before, after)
                expected = target.task(task_id)["expected_effects"]
                if effects[task_id] != expected:
                    unseen[task_id] = effects[task_id]

        if unseen:
            detail = ", ".join(
                f"{task} saw {'no reading' if seen is None else seen} of "
                f"{target.task(task)['expected_effects']}"
                for task, seen in unseen.items()
            )
            raise ProbeRefusal(
                f"refusing to scan: the verify tool does not see the effects the "
                f"agent's upstream applied; the upstream's state must persist "
                f"outside its process ({detail}, on clean runs the agent "
                f"completed with a success reply).\n\n"
                f"The agent's proxy starts its own copy of a stdio upstream per "
                f"task, and the verify tool reads through another. An upstream "
                f"that keeps its state in memory gives each copy an empty store, "
                f"so the oracle would count zero for every task whatever the "
                f"agent did. Point the upstream at a file or a database -- or, if "
                f"it does persist, it acknowledged work it did not apply with no "
                f"faults injected, which is a finding about the server and not a "
                f"measurement of the agent."
            )

        if failed:
            raise ProbeRefusal(
                f"refusing to scan: {len(failed)} of {len(requests)} tasks did not "
                f"complete with no faults injected ({', '.join(failed)}). The chaos "
                f"phase would measure a broken agent or a broken task file rather "
                f"than the agent's behavior under fault.\n\n"
                f"The records are in {target.work_dir}. Fix the task or the agent "
                f"and re-run."
            )

        clean_calls = {task: sum(tools.values()) for task, tools in calls_by_task.items()}
        metrics: dict[str, Any] = {
            "tasks": len(requests),
            "tasks_completed": len(requests) - len(failed),
            "task_outcomes": outcomes,
            # The denominator retry amplification needs, per task and per tool.
            # Per tool as well as per task because the forced schedule is keyed
            # on `(task, tool, ordinal)` and the chaos phase sizes the table
            # from exactly this.
            "clean_calls_by_task": calls_by_task,
            "clean_calls_per_task": clean_calls,
            "clean_calls": sum(clean_calls.values()),
            "task_latency_s": latencies,
            # The oracle's reading of each clean task, checked against
            # `expected_effects` above. Empty without --verify-tool.
            "baseline_effects_by_task": effects,
            "work_dir": str(target.work_dir),
        }

        # **After the denominators, and in its own pass.** The held task is an
        # extra run of one task, so folding it into the counts above would put
        # it in `clean_calls_by_task` -- the denominator `retry_amplification`
        # is measured against -- and a probe that moved another metric's
        # denominator would be measuring itself.
        hold_s = getattr(target, "hold_reply_s", None)
        if hold_s:
            metrics.update(await _measure_client_timeout(target, requests[0], hold_s))

        if context is not None:
            # Phase 2 sizes the forced schedule from this. Handed over through
            # the context for the reason phase 2 hands trajectories to phase 3:
            # neither probe reaches into the other, and both still run alone.
            context.artifacts["agent_clean_calls"] = calls_by_task
            for key in (
                "client_timeout_outcome", "client_timeout_s",
                "client_timeout_bound_s", "client_timeout_hold_s",
                "task_deadline_s",
            ):
                if key in metrics:
                    context.artifacts[key] = metrics[key]

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=(
                f"{metrics['tasks_completed']}/{metrics['tasks']} tasks completed "
                f"with no faults, {metrics['clean_calls']} tool calls in total"
            ),
            metrics=metrics,
            findings=_findings(metrics),
            sample_count=len(requests),
            error_rate=len(failed) / len(requests) if requests else 0.0,
            duration_s=time.perf_counter() - started,
        )


async def _measure_client_timeout(
    target: Any, request: Any, hold_s: float
) -> dict[str, Any]:
    """Run one task again with its first reply held, and read what happened.

    **The hold and the per-task deadline are both bounds, and which one bound
    first decides what the run can establish.** There is deliberately no
    refusal here, and the reason is worth stating because the obvious guard is
    wrong:

    - with the deadline **longer** than the hold, the reply is released and
      every client eventually gets it, so `no_deadline` is unreachable -- the
      run can only distinguish "acted at *t*" from "waited past the hold";
    - with the deadline **shorter**, a patient client is killed mid-wait, which
      is `no_deadline`, and `waited_out` is the unreachable one.

    Neither configuration is a mistake and no single one produces all three. So
    the run reports **which bound it actually reached** rather than refusing one
    of them -- `client_timeout_bound_by` -- and the sentence a reader sees names
    it. What must never happen is reporting a deadline-bounded run as though the
    hold had established it.
    """
    target.start_pass(DEADLINE_PASS)
    # No faults and one held reply. The table is empty rather than absent, the
    # same instruction the clean pass gives.
    target.write_schedule({}, hold_s=hold_s)
    response = await target.invoke(request)
    rows = read_record(target.record_path(request.op))
    measured = agent_deadline.measure(
        rows,
        task_outcome=response.meta.get("outcome", "unknown"),
        finished_at=response.meta.get("finished_at"),
        task_deadline_s=target.timeout_s,
    )
    logger.info(
        "client timeout probe: %s (%s)",
        measured["client_timeout_outcome"], target.record_path(request.op),
    )
    return {**measured, "client_timeout_task": request.op}


def _refuse_if_unrecorded(task_id: str, rows: list[dict[str, Any]], target: Any) -> None:
    """An empty record is the absence of evidence, and it is refused as one.

    Zero calls is a legal-looking value: it is what a scan of an agent that did
    nothing would report, and it is also what a scan whose proxy never ran
    reports. Nothing downstream can tell those apart, so the distinction has to
    be made here -- the shape of PROGRESS 8b entry 28, where a withheld
    measurement lifted the cap that measurement existed to apply and a dirty
    run printed 100/100.

    The most likely cause is the one worth naming: the config's `env` block did
    not reach the proxy, so it wrote its record somewhere else.
    """
    if rows:
        return
    raise ProbeRefusal(
        f"no calls recorded for task {task_id!r}: the record at "
        f"{target.record_path(task_id)} is empty or missing, so nothing about "
        f"this task was measured. An empty record is not zero calls -- it is no "
        f"evidence.\n\n"
        f"The usual cause is that the proxy never received {'RMA_PROXY_RECORD'}: "
        f"check the `env` block in {target.config_path(task_id)}, and that "
        f"the agent launches the proxy from that config rather than reconstructing "
        f"the command."
    )


def _count_by_tool(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        tool = str(row.get("op") or "")
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings = [
        f"Every one of the {metrics['tasks']} tasks completed with no faults "
        f"injected, costing {metrics['clean_calls']} tool calls in total."
    ]
    noisy = {
        task: calls for task, calls in metrics["clean_calls_per_task"].items()
        if calls > 1
    }
    told = agent_deadline.describe(metrics)
    if told:
        findings.append(told + ".")
    defect = agent_deadline.finding(metrics)
    if defect:
        findings.append(defect)
    if noisy:
        detail = ", ".join(f"{task} {calls}" for task, calls in sorted(noisy.items()))
        findings.append(
            f"On the clean path {len(noisy)} "
            f"{'task' if len(noisy) == 1 else 'tasks'} took more than one tool "
            f"call ({detail}). That is the denominator retry amplification is "
            f"measured against, not a finding about the agent."
        )
    return findings


__all__ = ["AgentBaseline"]
