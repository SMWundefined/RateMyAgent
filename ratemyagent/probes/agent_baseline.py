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
from .base import Probe, ProbeConfig, ProbeRefusal, ScanContext

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

        # An empty table, not an absent one: the proxy must be told to force no
        # faults, rather than left to its seeded draw.
        target.write_schedule({})

        requests = target.probe_requests(config.requests)
        outcomes: dict[str, str] = {}
        failed: list[str] = []
        calls_by_task: dict[str, dict[str, int]] = {}
        latencies: dict[str, float] = {}

        for request in requests:
            task_id = request.op
            response = await target.invoke(request)
            latencies[task_id] = response.latency_s
            outcomes[task_id] = response.meta.get("outcome", "unknown")

            rows = invocation_rows(read_record(target.record_path(task_id)))
            _refuse_if_unrecorded(task_id, rows, target)
            calls_by_task[task_id] = _count_by_tool(rows)

            if not response.ok:
                failed.append(task_id)

        if failed:
            raise ProbeRefusal(
                f"refusing to scan: {len(failed)} of {len(requests)} tasks did not "
                f"complete with no faults injected ({', '.join(failed)}). The chaos "
                f"phase would measure a broken agent or a broken task file rather "
                f"than the agent's behaviour under fault.\n\n"
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
            "work_dir": str(target.work_dir),
        }

        if context is not None:
            # Phase 2 sizes the forced schedule from this. Handed over through
            # the context for the reason phase 2 hands trajectories to phase 3:
            # neither probe reaches into the other, and both still run alone.
            context.artifacts["agent_clean_calls"] = calls_by_task

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
        f"check the `env` block in {target.work_dir}/mcp-{task_id}.json, and that "
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
