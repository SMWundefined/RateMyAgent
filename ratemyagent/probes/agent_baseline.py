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
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..models import ProbeResult
from ..proxy import explain_unrecorded, invocation_rows, read_record
from . import agent_deadline
from .agent_deadline import DEADLINE_PASS
from .base import Probe, ProbeConfig, ProbeRefusal, ScanContext
from .fault import _window_diff, entry_diff

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
        #: The last stderr lines of each task that ended with no result line.
        stderr_tails: dict[str, list[str]] = {}
        calls_by_task: dict[str, dict[str, int]] = {}
        latencies: dict[str, float] = {}
        oracle = getattr(target, "has_effect_oracle", False)
        effects: dict[str, int | None] = {}
        unseen: dict[str, int | None] = {}
        #: Per declared token, `(applied, declared)`, where the two differ.
        off_entry: dict[str, dict[str, tuple[int, int]]] = {}
        rows_by_task: dict[str, list[dict[str, Any]]] = {}

        for request in requests:
            task_id = request.op
            before = await target.read_effect_entries() if oracle else None
            response = await target.invoke(request)
            after = await target.read_effect_entries() if oracle else None
            latencies[task_id] = response.latency_s
            outcomes[task_id] = response.meta.get("outcome", "unknown")

            rows = invocation_rows(read_record(target.record_path(task_id)))
            _refuse_if_unrecorded(task_id, rows, target, response)
            calls_by_task[task_id] = _count_by_tool(rows)
            rows_by_task[task_id] = rows

            if not response.ok:
                failed.append(task_id)
                if response.meta.get("stderr_tail"):
                    stderr_tails[task_id] = response.meta["stderr_tail"]
            elif oracle and any(row.get("ok") is True for row in rows):
                # A clean run the agent says worked, with a success reply on
                # the record: the upstream applied the task, so the oracle has
                # to see exactly that. If it does not, every count in the chaos
                # pass is read from a state the agent never wrote to.
                effects[task_id] = _window_diff(before, after)
                spec = target.task(task_id)
                if spec.get("expected_entries") is not None:
                    # **Per entry where declared** (1.7.5). The net check alone
                    # passes a clean run that wrote one entry twice and never
                    # sent another: E == x. Without a declaration it is the
                    # only check, and the chaos-pass blocker covers the verdict.
                    by_entry, _, ambiguous = entry_diff(
                        before, after, spec["expected_entries"]
                    )
                    # An entry matching two tokens has no per-entry reading;
                    # the net check below is all there is, as in the chaos pass.
                    if by_entry is not None and not ambiguous:
                        wanted = Counter(spec["expected_entries"])
                        differ = {
                            token: (by_entry.get(token, 0), count)
                            for token, count in wanted.items()
                            if by_entry.get(token, 0) != count
                        }
                        if differ:
                            off_entry[task_id] = differ
                if effects[task_id] != spec["expected_effects"] or task_id in off_entry:
                    unseen[task_id] = effects[task_id]

        if unseen:
            raise ProbeRefusal(_unseen_refusal(target, unseen, off_entry, rows_by_task))

        if failed:
            raise ProbeRefusal(
                f"refusing to scan: {len(failed)} of {len(requests)} tasks did not "
                f"complete with no faults injected ({', '.join(failed)}). The chaos "
                f"phase would measure a broken agent or a broken task file rather "
                f"than the agent's behavior under fault.\n\n"
                f"The records are in {target.work_dir}. Fix the task or the agent "
                f"and re-run."
                + "".join(
                    f"\n\nTask {task} ended with no result line; the agent's stderr "
                    f"ended:\n" + "\n".join(f"  {line}" for line in lines)
                    for task, lines in stderr_tails.items()
                )
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
            # What the clean pass itself wrote (1.6.2). Phase 3 needs it to say
            # that the scan's own baseline is part of the state every later run
            # starts from -- the reading is the baseline's, so it travels from
            # the baseline rather than being re-derived downstream.
            context.artifacts["agent_baseline_effects"] = effects
            # The clean pass's rows (1.9.0), for one question only: did any
            # call in the scan carry the agent's key? `retry_keys` reads "not
            # read" when none did, and an agent that keyed its clean writes
            # and then dropped the key on a retry must read "no key" instead.
            context.artifacts["agent_clean_rows"] = rows_by_task
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


#: The refusal's explanation for a store the verify tool cannot see. Kept
#: verbatim from 1.7.4 for every task the two newer causes do not explain;
#: `_persistence` names the verify command instead when that is the oracle.
_PERSISTENCE = (
    "The agent's proxy starts its own copy of a stdio upstream per "
    "task, and the verify tool reads through another. An upstream "
    "that keeps its state in memory gives each copy an empty store, "
    "so the oracle would count zero for every task whatever the "
    "agent did. Point the upstream at a file or a database -- or, if "
    "it does persist, it acknowledged work it did not apply with no "
    "faults injected, which is a finding about the server and not a "
    "measurement of the agent."
)


def _unseen_refusal(
    target: Any,
    unseen: dict[str, int | None],
    off_entry: dict[str, dict[str, tuple[int, int]]],
    rows_by_task: dict[str, list[dict[str, Any]]],
) -> str:
    """Why the clean pass's effects are not what the task file says, per task.

    **The cause is read off the record before it is named** (1.7.5, DESIGN-1.8.0
    C). Three causes, task by task:

    - **the agent's own key** (`_shares_key`): one `idempotency_key` on distinct
      writes, so the upstream absorbed the later ones as retries of the first.
      The persistence advice would send the user after the wrong component;
    - **the agent's own writes, netted** (declared tasks only): the window
      holds as many effects as declared, but not the declared ones -- one entry
      twice, another never. The oracle saw the store fine;
    - **otherwise the 1.7.4 text**: the store is invisible to the verify tool,
      or the server dropped the work.

    It still refuses in every case, with exit 2: the chaos pass would measure
    writes the agent's own key suppressed, or a clean path that already
    duplicates.
    """
    expected = {task: target.task(task)["expected_effects"] for task in unseen}
    oracle = _oracle_noun(target)
    persistence = _persistence(target)

    def saw(task: str) -> str:
        seen = unseen[task]
        return f"{task} saw {'no reading' if seen is None else seen} of {expected[task]}"

    reused = [task for task in unseen if _shares_key(rows_by_task.get(task) or [])]
    netted = [
        task for task in unseen
        if task not in reused and task in off_entry and unseen[task] == expected[task]
    ]
    other = [task for task in unseen if task not in reused and task not in netted]

    if not reused and not netted:
        return (
            f"refusing to scan: the {oracle} does not see the effects the "
            f"agent's upstream applied; the upstream's state must persist "
            f"outside its process ({', '.join(saw(t) for t in other)}, on clean "
            f"runs the agent completed with a success reply).\n\n" + persistence
        )

    parts = [
        "refusing to scan: on clean runs the agent completed with a success "
        f"reply, the {oracle} did not see the effects the task file declares."
    ]
    if reused:
        parts.append(
            f"{', '.join(saw(t) for t in reused)}. The record shows the agent "
            f"sending one idempotency_key on distinct writes within "
            f"{'that task' if len(reused) == 1 else 'each of those tasks'}, so "
            f"the upstream took every write after the first as a retry of it and "
            f"applied nothing for them. Every one of those calls was answered "
            f"with success, so the agent was not told. That is the agent's key "
            f"scope -- a key has to name one write, not the task -- and a chaos "
            f"pass would measure writes its own key suppressed."
        )
    if netted:
        detail = "; ".join(
            f"{task}: "
            + ", ".join(
                f"{token} applied {applied} of {declared}"
                for token, (applied, declared) in off_entry[task].items()
            )
            for task in netted
        )
        parts.append(
            f"{detail}. The window holds as many effects as the task expects, "
            f"but not the declared ones: the agent's clean run wrote one entry "
            f"more than once and left another out, so the net count agrees and "
            f"the entries do not. The clean path already duplicates; fix the "
            f"agent or the task's expected_entries."
        )
    if other:
        parts.append(
            f"{', '.join(saw(t) for t in other)}: the upstream's state must "
            f"persist outside its process. " + persistence
        )
    return "\n\n".join(parts)


def _oracle_noun(target: Any) -> str:
    return (
        "verify command"
        if getattr(target, "oracle_name", None) == "--verify-command"
        else "verify tool"
    )


def _persistence(target: Any) -> str:
    """`_PERSISTENCE`, or its reading for a verify command (1.9.0).

    A command reads whatever it is pointed at, so the two ways it misses the
    agent's writes are a store kept in the server's memory and a different
    file from the one the server writes -- the fourth row of the walkthrough's
    NO VERDICT table.
    """
    if _oracle_noun(target) == "verify tool":
        return _PERSISTENCE
    return (
        "The agent's proxy starts its own copy of a stdio upstream per task, "
        "and the verify command reads whatever store it names. An upstream that "
        "keeps its state in memory writes nothing the command can see, and one "
        "that writes to a different database from the one the command reads "
        "looks the same: zero for every task, whatever the agent did. Point the "
        "command at the file or database the server writes -- or, if it does, "
        "the server acknowledged work it did not apply with no faults injected, "
        "which is a finding about the server and not a measurement of the agent."
    )


def _shares_key(rows: list[dict[str, Any]]) -> bool:
    """One `idempotency_key` on two or more distinct writes, all answered ok.

    **Distinct fingerprints, not just a repeated key.** A key repeated on
    identical arguments is a retry, which is what a careful agent does, and
    naming it key reuse would blame the one behaviour the key exists for. Only
    the key at `--key-path` is visible here (1.9.0; `idempotency_key` when the
    flag is not given) -- the proxy records the value it finds there in the
    row's `idempotency_key` field -- so its absence says nothing about a key
    somewhere else.
    """
    fingerprints: dict[str, set[str]] = {}
    for row in rows:
        key = row.get("idempotency_key")
        if row.get("ok") is True and isinstance(key, str):
            fingerprints.setdefault(key, set()).add(str(row.get("fingerprint")))
    return any(len(prints) >= 2 for prints in fingerprints.values())


def _refuse_if_unrecorded(
    task_id: str, rows: list[dict[str, Any]], target: Any, response: Any = None,
) -> None:
    """An empty record is the absence of evidence, and it is refused as one.

    Zero calls is a legal-looking value: it is what a scan of an agent that did
    nothing would report, and it is also what a scan whose proxy never ran
    reports. Nothing downstream can tell those apart, so the distinction has to
    be made here -- the shape of PROGRESS 8b entry 28, where a withheld
    measurement lifted the cap that measurement existed to apply and a dirty
    run printed 100/100.

    **Which cause to name is read off the disk, not assumed.** This used to
    name the config's `env` block whatever the record held. A row the proxy
    wrote proves the record path arrived, and then the env block is not the
    cause; `explain_unrecorded` classifies the record and writes the advice,
    for this refusal and the fault probe's alike.
    """
    if rows:
        return
    raise ProbeRefusal(explain_unrecorded(
        task_id, target.record_path(task_id), target.config_path(task_id),
        response=response,
    ))


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
