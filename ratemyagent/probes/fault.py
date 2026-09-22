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
import json
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from ..formatting import format_seconds
from ..models import Caveat, ErrorKind, FaultKind, Invocation, ProbeResult, Response, Trajectory
from ..proxy import invocation_rows, read_record, replay
from ..targets.fault_proxy import (
    ALL_FAULTS,
    FAULT_ORDER,
    OPT_IN_FAULTS,
    FaultConfig,
    FaultProxy,
)
from ..targets.mcp import count_matching
from .base import Probe, ProbeConfig, ProbeRefusal, ScanContext

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

#: How deep the forced schedule is built per `(task, tool)` when the clean-path
#: call count is not available -- a `--probes fault,behavior` run, where
#: `agent_baseline` did not produce the denominator.
#:
#: The table has to cover every ordinal an agent might reach, because an ordinal
#: past the end of the table draws no fault: a table that ran out would inject
#: nothing for the rest of the task and the run would read as an agent that
#: sailed through. Twelve is not defended by evidence and does not pretend to
#: be; it is roughly four times the default retry budget, and the number is
#: stated rather than dressed up as derived. With `agent_baseline` in the probe
#: set -- the default for an agent target -- the depth is computed from the
#: clean run instead and this is not reached.
FALLBACK_SCHEDULE_DEPTH = 12


def _repeat_count(config: ProbeConfig) -> int:
    """How many times the whole task set runs. Default 1.

    Read off `ProbeConfig.extra`, which is where every CLI-backed knob on this
    probe already lives. A value below 1 is a caller error rather than a
    request for zero runs, so it is clamped up and not down: a scan that ran
    the task set zero times would report every metric as withheld and look like
    a target that could not be measured.
    """
    try:
        return max(1, int(config.extra.get("repeats") or 1))
    except (TypeError, ValueError):
        return 1


def realized_schedule(
    rows_by_task: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Which calls were actually faulted, in order, read off the record.

    **The intended schedule is a table; this is what happened to it.** The table
    is keyed `(task_id, tool, ordinal)`, and an ordinal is only reached if the
    agent makes that many calls to that tool in that task. A scripted fixture
    makes the same calls every run, so the two are the same document. An LLM
    chooses its own calls, so **one seed can fault a different call between two
    runs** -- and two runs at one seed are then two different experiments that
    share a random number, not two replicates of one.

    That is why this exists and why it is reported beside the intended table
    rather than instead of it. A range computed over runs whose faults landed in
    different places is the spread of the schedule wearing the label of the
    spread of the agent.

    **Counted the way the proxy counts.** `FaultProxy._ordinals` increments per
    `(task_id, tool)` on every call including retries, and `RecordWriter` restores
    that counter from the record so it survives a reconnect. Walking the record
    in sequence order and counting the same way reproduces the ordinal the
    schedule was consulted with, rather than guessing at it.
    """
    entries: list[dict[str, Any]] = []
    for task_id, rows in rows_by_task.items():
        ordinals: dict[str, int] = {}
        for row in sorted(rows, key=lambda r: r.get("sequence") or 0):
            tool = str(row.get("op") or "")
            ordinals[tool] = ordinals.get(tool, 0) + 1
            if row.get("injected"):
                entries.append({
                    "task_id": task_id,
                    "tool": tool,
                    "ordinal": ordinals[tool],
                    "fault": row["injected"],
                })
    return entries


def placement_key(entries: list[dict[str, Any]]) -> str:
    """One comparable string for a run's realized placement.

    Repeats are grouped on this. It is deliberately the whole ordered placement
    and not a hash: a user reading two groups in a report has to be able to see
    *how* they differed, and a digest turns "the fault moved from the first call
    to the second" into two opaque hex strings.

    The empty string is a run in which nothing was faulted, which is a placement
    like any other and groups with its own kind.
    """
    return ", ".join(
        f"{entry['task_id']}:{entry['tool']}#{entry['ordinal']}={entry['fault']}"
        for entry in entries
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
        schedule: dict[tuple[str, str, int], FaultKind] | None = None,
    ) -> None:
        self._faults = faults
        #: A forced table to use verbatim on an out-of-process target, instead
        #: of one generated from `--seed` and `--fault-rate`. For a gate that
        #: needs a specific fault at a specific position; ignored in-process,
        #: where there is no table.
        self._schedule = dict(schedule) if schedule is not None else None
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

        if getattr(target, "injects_out_of_process", False):
            # Nothing here to wrap: the wrapping already happened, inside the
            # proxy the agent launched. What this branch does instead is write
            # the schedule, run the tasks, and read the record back -- and it
            # deposits the same three artifacts, so phase 3 cannot tell which
            # branch filled them.
            return await self._out_of_process(target, config, context, faults, started)

        # Op ids are salted with the seed, so a scan replays exactly. This salt
        # covers the degradation pass. The recovery pass narrows it to its own
        # `recovery:` namespace (see `_recovery_pass`), because the *registered*
        # ids are the ones a collision misreports: at the default seed this salt
        # equals the adapter's constructor default, so the baseline probes and
        # the concurrency ramp draw ids from the very same space.
        if hasattr(target, "_op_id_salt"):
            target._op_id_salt = config.seed
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
            # The oracle's reading, for phase 3 to score. Deposited as data
            # rather than as a live object so the behaviour probe stays readable
            # on its own and cannot re-read the target.
            oracle = getattr(self, "_oracle", None)
            if oracle is not None:
                context.artifacts["effect_oracle"] = oracle.metrics()

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

    # -- the out-of-process branch -------------------------------------------

    async def _out_of_process(
        self,
        target: "Target",
        config: ProbeConfig,
        context: ScanContext | None,
        faults: FaultConfig,
        started: float,
    ) -> ProbeResult:
        """Run tasks through an agent whose calls a proxy is already faulting.

        The in-process path wraps the target and reads `proxy.trajectories`.
        Here there is nothing to wrap -- `FaultProxy(MCPTarget(upstream))` lives
        inside `ratemyagent proxy`, which the agent launched -- so this writes
        the forced schedule, runs the tasks, and replays the record the proxy
        wrote. Everything downstream is unchanged, which is the test of whether
        the design is right.

        **Strictly sequential, and the code is what enforces it.** Window-based
        attribution needs it: with two tasks in flight the effects around task A
        include B's, and no arithmetic recovers the split. The recovery pass has
        the same rule for the same reason.

        **There is no degradation pass.** It re-runs the baseline probes through
        the proxy, and the agent baseline is not a probe that can be re-run --
        it runs tasks, and running them again under fault is what this pass
        already is.
        """
        schedule = (
            self._schedule if self._schedule is not None
            else self._schedule_for(target, config, context, faults)
        )
        requests = target.probe_requests(config.requests)
        oracle = getattr(target, "has_effect_oracle", False)
        repeats_n = _repeat_count(config)

        runs: list[dict[str, Any]] = []
        for index in range(1, repeats_n + 1):
            # **The first run keeps the pass name `chaos`**, so a scan at the
            # default R=1 writes exactly the files it wrote before repeats
            # existed. Each repeat gets its own pass, and therefore its own
            # record and schedule files: the proxy restores its ordinal counter
            # from the record, so a shared file would start the second run's
            # schedule wherever the first one stopped.
            target.start_pass("chaos" if index == 1 else f"chaos{index}")
            target.write_schedule(schedule, close_after_s=faults.close_after_s)
            runs.append(await self._one_chaos_run(target, requests, oracle))

        first = runs[0]
        claims = first["claims"]
        outcomes = first["outcomes"]
        rows_by_task = first["rows_by_task"]
        tasks = first["tasks"]
        invocations, trajectories = first["invocations"], first["trajectories"]
        realized = first["realized"]

        if context is not None:
            context.artifacts["trajectories"] = trajectories
            context.artifacts["invocations"] = invocations
            context.artifacts["fault_config"] = faults.to_dict()
            context.artifacts["max_retries"] = self.max_retries
            context.artifacts["agent_tasks"] = tasks
            context.artifacts["agent_rows"] = rows_by_task
            context.artifacts["scheduled_faults"] = len(schedule)
            context.artifacts["realized_schedule"] = realized
            context.artifacts["realized_placement"] = placement_key(realized)
            # Every run, for the repeat grouping in phase 3. One element at
            # R=1, which is the path that has always existed.
            context.artifacts["agent_runs"] = runs

        metrics: dict[str, Any] = {
            "faults": faults.to_dict(),
            "max_retries": self.max_retries,
            "calls": len(invocations),
            "injected": sum(1 for inv in invocations if inv.injected is not None),
            "injected_by_kind": _count(
                inv.injected.value for inv in invocations if inv.injected is not None
            ),
            "injection_rate": (
                sum(1 for inv in invocations if inv.injected is not None) / len(invocations)
                if invocations else 0.0
            ),
            "baseline_probes_under_fault": {},
            "interposed": True,
            "tasks": len(requests),
            "task_claims": claims,
            "task_outcomes": outcomes,
            "task_results": tasks,
            "scheduled_faults": len(schedule),
            "schedule_source": "explicit" if self._schedule is not None else "seeded",
            "intended_schedule": [
                {"task_id": task, "tool": tool, "ordinal": ordinal, "fault": fault.value}
                for (task, tool, ordinal), fault in sorted(schedule.items())
            ],
            "realized_schedule": realized,
            "realized_placement": placement_key(realized),
            "record_dir": str(target.work_dir),
            # **The keys above describe run 1**, and at R=1 that is the whole
            # scan. They are not pooled across repeats: `injected_by_kind`
            # summed over five runs is a number no single run produced, and
            # `realized_schedule` pooled would splice placements that differ.
            # The per-run detail is here, and phase 3 is where repeats are
            # grouped and reported.
            "repeats": repeats_n,
            "runs": [
                {
                    "run": n,
                    "calls": len(run["invocations"]),
                    "injected_by_kind": _count(
                        inv.injected.value for inv in run["invocations"]
                        if inv.injected is not None
                    ),
                    "realized_placement": placement_key(run["realized"]),
                }
                for n, run in enumerate(runs, start=1)
            ],
            **_trajectory_metrics(trajectories, invocations),
        }

        findings = _findings(metrics)
        if repeats_n > 1:
            findings.insert(0, (
                f"The task set ran {repeats_n} times. The fault-injection numbers "
                f"above describe the first run; every run's calls and realized "
                f"fault placement are in `runs`, and the behaviour probe reports "
                f"them as a range grouped by placement. The clean pass ran once, "
                f"so every run shares one call-count denominator."
            ))

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=findings,
            caveats=_caveats(metrics),
            sample_count=len(invocations),
            error_rate=metrics["error_rate_under_fault"],
            duration_s=time.perf_counter() - started,
        )

    async def _one_chaos_run(
        self, target: "Target", requests: list, oracle: bool,
    ) -> dict[str, Any]:
        """One pass of the whole task set, against the schedule already written.

        Everything that was inline before repeats existed, moved here unchanged
        so that a repeat is literally the same run again rather than a second
        implementation of it.
        """
        claims: dict[str, bool] = {}
        outcomes: dict[str, str] = {}
        rows: list[dict[str, Any]] = []
        rows_by_task: dict[str, list[dict[str, Any]]] = {}
        windows: dict[str, tuple[Any, Any]] = {}

        # One task at a time, each inside its own pair of reads. The target
        # refuses a second task in flight as well; this loop is simply the
        # shape that never asks it to.
        for request in requests:
            task_id = request.op
            before = await target.read_effect_entries() if oracle else None
            response = await target.invoke(request)
            after = await target.read_effect_entries() if oracle else None
            windows[task_id] = (before, after)
            claims[task_id] = response.ok
            outcomes[task_id] = response.meta.get("outcome", "unknown")

            task_rows = read_record(target.record_path(task_id))
            if not invocation_rows(task_rows):
                # Absence, not zero. See `ProbeRefusal`, and PROGRESS 8b entry
                # 28: a measurement that did not happen must not be scored as a
                # measurement that came back clean.
                raise ProbeRefusal(
                    f"no calls recorded for task {task_id!r} under fault: the "
                    f"record at {target.record_path(task_id)} is empty or "
                    f"missing, so nothing about this task was measured. An empty "
                    f"record is not zero calls -- it is no evidence.\n\n"
                    f"Check the `env` block in "
                    f"{target.config_path(task_id)} reaches the proxy: the "
                    f"MCP SDK copies six variables into a stdio child and drops "
                    f"the rest, so RMA_PROXY_RECORD travels in that block or not "
                    f"at all."
                )
            rows.extend(task_rows)
            rows_by_task[task_id] = invocation_rows(task_rows)

        abandoned = [task for task, outcome in outcomes.items() if outcome == "abandoned"]
        if abandoned:
            # **A scan that never finished is not a target that failed.** The
            # agent was still waiting on a reply this scan dropped on purpose
            # when its deadline expired, and an agent with no read timeout waits
            # forever -- a real production failure mode, and the right output
            # for it is this plus a finding, not a lower score. Exit 2, because
            # exit 1 would say the target missed a policy it was never measured
            # against.
            raise ProbeRefusal(
                f"the agent abandoned {len(abandoned)} "
                f"{'task' if len(abandoned) == 1 else 'tasks'} "
                f"({', '.join(abandoned)}): it did not finish within the "
                f"per-task deadline and was killed.\n\n"
                f"A dropped reply ends only when the client decides it has "
                f"waited long enough, and this client did not decide. That is a "
                f"finding about the agent -- every MCP client library can bound "
                f"a request, and whatever this one is built on, it did not -- "
                f"and not a result about the target, so the scan stops rather "
                f"than scoring it.\n\n"
                f"Two ways forward. Give the agent a per-request timeout, if it "
                f"is yours to change. Or run with --lost-reply-close-after, "
                f"which closes the session a few seconds after the reply is "
                f"dropped: the agent is handed an end-of-stream it cannot "
                f"ignore, while still not learning whether the call ran.\n\n"
                f"The records are in {target.work_dir}."
            )

        # Each task's effects are the diff across *its own* window.
        from ..targets.agent import effect_count

        effects = {task: _window_diff(b, a) for task, (b, a) in windows.items()}
        tasks: dict[str, dict[str, Any]] = {}
        for task_spec in target.tasks:
            task_id = str(task_spec["id"])
            if task_id not in claims:
                continue
            before, after = windows[task_id]
            if not oracle:
                status = "absent"
            elif before is None or after is None or effects[task_id] is None:
                status = "failed"
            else:
                status = "ok"
            tasks[task_id] = {
                "expected_effects": task_spec["expected_effects"],
                "claimed_ok": claims[task_id],
                "outcome": outcomes[task_id],
                "effects": effects[task_id] if status == "ok" else None,
                # What the store already held when this task's window opened
                # (1.6.2). The diff is what the task applied; this is what it
                # applied *on top of*, and the scan's own clean pass is part of
                # it. Carried so the behaviour probe can say so rather than
                # leave a reader to discover it from the twin's ledger.
                #
                # **A count, through the same converter the diff uses.** The
                # raw window is a list from one oracle and a number from
                # another (`_window_diff`), and a consumer branching on which
                # would be reading the verify tool's reply shape rather than
                # its reading.
                "before": effect_count(before) if status == "ok" else None,
                "oracle_status": status,
                "calls": len(rows_by_task[task_id]),
                "delivered_ok": any(row.get("ok") is True for row in rows_by_task[task_id]),
            }

        invocations, trajectories = replay(rows)
        return {
            "claims": claims,
            "outcomes": outcomes,
            "rows_by_task": rows_by_task,
            "tasks": tasks,
            "invocations": invocations,
            "trajectories": trajectories,
            "realized": realized_schedule(rows_by_task),
            "placement": placement_key(realized_schedule(rows_by_task)),
        }

    def _schedule_for(
        self,
        target: "Target",
        config: ProbeConfig,
        context: ScanContext | None,
        faults: FaultConfig,
    ) -> dict[tuple[str, str, int], FaultKind]:
        """Build the forced fault table this run will be measured under.

        **Keyed on where the call sits, not on what it is called.** The seeded
        draw is keyed on `request.trajectory_key`, which an agent chooses; two
        agents doing one task make different numbers of calls with different
        keys, so the same seed gives them different faults and any comparison
        between them measures the draw. Keyed on `(task, tool, ordinal)`, both
        agents' first call to a tool in a task gets the same fault and their
        second gets the same next one -- which is what "identical forced
        schedule" has to mean when the two make different numbers of calls.

        **Still derived from `--seed` and `--fault-rate`, so a run replays.**
        The table is generated, not hand-written; what changed is the key it is
        generated against.

        **Depth matters.** An ordinal past the end of the table draws nothing,
        so a table that stops short stops injecting, and the tail of a task
        reads as an agent sailing through. It is sized from the clean-path call
        count the baseline measured, times the retry budget, plus headroom.
        """
        clean = (context.artifacts.get("agent_clean_calls") if context else None) or {}
        kinds = [kind for kind, rate in faults.rates.items() if rate > 0]
        if not kinds:
            return {}
        # The same canonical order `_choose_fault` walks, so a kind's identity
        # does not depend on how a config dict was built.
        kinds = [kind for kind in FAULT_ORDER if kind in kinds]
        rate = faults.total_rate

        schedule: dict[tuple[str, str, int], FaultKind] = {}
        for task in target.tasks:
            task_id = str(task["id"])
            tools = clean.get(task_id) or {str(task["tool"]): 1}
            for tool, count in tools.items():
                depth = max(
                    count * (self.max_retries + 1) + self.max_retries,
                    FALLBACK_SCHEDULE_DEPTH,
                )
                for ordinal in range(1, depth + 1):
                    rng = random.Random(f"{config.seed}:{task_id}:{tool}:{ordinal}")
                    if rng.random() >= rate:
                        continue
                    schedule[(task_id, tool, ordinal)] = kinds[
                        rng.randrange(len(kinds))
                    ]
        return schedule

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

        **The state oracle brackets this loop and nothing else** (1.4.0). This
        is the only window where a retry can duplicate anything, it is strictly
        sequential -- one `await` at a time, below -- and every other probe that
        calls the tool runs in an earlier phase, so no write from the baseline
        or the degradation pass can land inside it. Ids are registered before
        the first snapshot, which is what makes the window exactly this set of
        operations rather than whatever the result happens to contain.

        **Registered ids get their own namespace**, so an id sent earlier in
        the scan cannot be one of them. A label offset is not enough: the
        concurrency ramp restarts at `offset=0` and walks `requests` indices per
        level over `_ladder(concurrency)`, so it covers this window's indices,
        and at the default seed its salt is the same one -- the adapter's
        constructor default equals the default `--seed`. Every registered id
        was then already applied before the window opened, and a first run on
        clean state reported `stale`. Salting the registered ids separately
        removes the collision for every seed rather than for the seeds the
        tests happened to pin.
        """
        offset = config.warmup + config.requests

        # The adapter's state, so it is restored: a probe borrows the namespace
        # for this window and hands it back.
        target = proxy.inner
        salted = hasattr(target, "_op_id_salt")
        previous = getattr(target, "_op_id_salt", None)
        if salted:
            target._op_id_salt = _recovery_salt(config.seed)

        try:
            requests = proxy.probe_requests(config.requests, offset=offset)
            keys = [request.trajectory_key for request in requests]

            # The ids come from `recovery_op_ids`, which the setup staleness
            # check also calls (1.4.1). One derivation, two callers: a check
            # that re-derived the ids could agree with a scan that sends
            # different ones, and the whole point of the check is that those
            # two sets are identical.
            oracle = _EffectOracle(target, recovery_op_ids(target, config))
            await oracle.before()

            backoff = _BackoffBudget(config.backoff_max_s, config.backoff_budget_s)

            for request in requests:
                for _ in range(self.max_retries + 1):
                    response = await proxy.invoke(request)
                    if response.ok:
                        break
                    await backoff.wait_for(response)

            await oracle.after()
        finally:
            if salted:
                target._op_id_salt = previous

        trajectories = [proxy.trajectories[key] for key in keys if key in proxy.trajectories]
        metrics = _trajectory_metrics(trajectories, proxy.invocations)
        metrics.update(backoff.metrics())
        metrics.update(oracle.metrics())
        self._oracle = oracle
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
        close_after = config.extra.get("lost_reply_close_after")
        if getattr(target, "allow_mutating", False):
            opt_in = OPT_IN_FAULTS
            if close_after is not None:
                # **Substituted, never added.** The comment above says adding a
                # sixth kind would move every boundary; adding a seventh would
                # do it again, to every agent scan ever recorded. Swapping the
                # opt-in kind for its closing twin keeps the count at six, so
                # every cumulative threshold and every schedule ordinal stays
                # exactly where it was -- the same seed picks the same slot, and
                # only the slot's meaning changes. That is why the spike's seed
                # 490 still lands a lost reply on `event` ordinal 1.
                opt_in = tuple(
                    FaultKind.RESPONSE_LOST_THEN_CLOSED
                    if kind is FaultKind.RESPONSE_LOST else kind
                    for kind in OPT_IN_FAULTS
                )
            kinds = (*ALL_FAULTS, *opt_in)
        return FaultConfig.uniform(
            rate, kinds, seed=config.seed,
            close_after_s=float(close_after) if close_after is not None else None,
        )


def _window_diff(before: Any, after: Any) -> int | None:
    """Effects applied between two reads of the upstream, or None.

    A list is counted by length and a number is taken as a count. Negative is
    returned as it is: state that shrank inside a task window is a finding
    about the upstream or another writer, not a zero to be clamped into a
    clean reading.
    """
    from ..targets.agent import effect_count

    first, last = effect_count(before), effect_count(after)
    if first is None or last is None:
        return None
    return last - first


def recovery_op_ids(target: Any, config: ProbeConfig) -> dict[str, str]:
    """`{trajectory_key: op_id}` for the operations the recovery pass registers.

    The single derivation of "which ids is this scan going to write", called
    twice: by `_recovery_pass` when it opens the window, and by the scanner's
    setup check before any probe runs. Re-deriving it in the check would let the
    two drift, and a staleness check that looks for different ids than the scan
    sends is worse than none -- it reports clean on dirty state.

    Empty, and therefore skipped, unless the target has an oracle and carries
    `{op_id}`: without both there is nothing to attribute and nothing to be
    stale about.

    Borrows the recovery namespace and hands it straight back, so calling it is
    invisible to the adapter. Safe inside the window too, where it sets the salt
    that is already set.
    """
    op_id_of = getattr(target, "op_id", None)
    if not callable(op_id_of):
        return {}
    if not getattr(target, "has_effect_oracle", False):
        return {}
    if not getattr(target, "uses_op_id", False):
        return {}

    offset = config.warmup + config.requests
    salted = hasattr(target, "_op_id_salt")
    previous = getattr(target, "_op_id_salt", None)
    if salted:
        target._op_id_salt = _recovery_salt(config.seed)
    try:
        requests = target.probe_requests(config.requests, offset=offset)
        return {
            request.trajectory_key: op_id_of(offset + index)
            for index, request in enumerate(requests)
        }
    finally:
        if salted:
            target._op_id_salt = previous


def stale_op_ids(target: Any, config: ProbeConfig) -> dict[str, str]:
    """Registered ids the target's pre-scan state already contains.

    Reads the snapshot `setup()` took, never the wire: the check costs no call
    and cannot itself disturb what it is measuring. A target with no such
    snapshot -- no oracle, a read that did not answer -- has nothing to say
    here, and silence is not evidence of a clean target, which is why a failed
    read is handled as `failed` later rather than as "not stale" now.
    """
    entries = getattr(target, "setup_effect_entries", None)
    if not isinstance(entries, list):
        return {}
    return {
        key: op_id
        for key, op_id in recovery_op_ids(target, config).items()
        if count_matching(entries, op_id)
    }


def _recovery_salt(seed: int | str) -> str:
    """The namespace the *registered* op ids live in.

    One source of truth, and imported by the test that sweeps for collisions,
    so a change here cannot quietly re-open the hole it closes. Registered ids
    must not be reachable from any earlier phase: the baseline probes use the
    adapter's constructor salt, which equals the default `--seed`, and the
    concurrency ramp covers the recovery window's indices. Note that a plain
    seed would not do -- `f"{1337}"` and `f"{'1337'}"` are the same string.
    """
    return f"recovery:{seed}"


class _EffectOracle:
    """Counts what the retried operations actually applied, per operation.

    **Why per operation.** An aggregate `effects - successes` lets two errors
    cancel: one operation applied twice and one acknowledged but never applied
    give `E == S`, both metrics zero, and a clean report over a target that both
    double-charged and dropped work. Each operation is counted on its own id.

    **Why a diff and not the after count.** Counting only the after snapshot
    assumes the window started empty, which is a property of the sample rather
    than of the world: a second run of the same seeded scan against a persistent
    target begins with the first run's entries in place, and every count reads 1
    before a call goes out. Ids are derived from the seed, so the collision is
    by construction.

    **Why the ids are registered first.** The set of operations in the window is
    fixed before the before-snapshot is taken, so an entry that matches no
    registered id is attributable to something else -- pre-existing state, or a
    concurrent writer -- and is reported as `unattributed` rather than counted.

    **Why it reads the unwrapped target.** `read_effect_entries` is called on
    `proxy.inner`, so a verify call cannot be faulted and is never recorded as
    an invocation. That does not weaken "the FaultProxy is the only place faults
    are injected": faults are *created* in `_choose_fault`, `_reject`,
    `_corrupt` and `_lose`, all inside `FaultProxy.invoke`, and a call that
    never enters it cannot be faulted. The preflight and every baseline probe
    already call the unwrapped target.
    """

    def __init__(self, target: Any, ids: dict[str, str]) -> None:
        self._target = target
        # Gated on the declared capability, never on the presence of the method:
        # `MCPTarget` always defines `read_effect_entries`, so probing for it
        # made every MCP scan look oracle-equipped and report `failed` where
        # `absent` was true.
        self._reader = (
            getattr(target, "read_effect_entries", None)
            if getattr(target, "has_effect_oracle", False)
            else None
        )
        self._uses_op_id = bool(getattr(target, "uses_op_id", False))
        #: {trajectory_key: op_id}, registered before the first attempt and
        #: built by `recovery_op_ids` so the setup check and this window cannot
        #: disagree about which ids belong to the scan.
        self._ids: dict[str, str] = dict(ids) if self._reader is not None else {}

        self._before: Any = None
        self._after: Any = None
        self._failed = False

    @property
    def active(self) -> bool:
        return self._reader is not None

    async def before(self) -> None:
        if self._reader is None:
            return
        self._before = await self._read()

    async def after(self) -> None:
        if self._reader is None:
            return
        self._after = await self._read()

    async def _read(self) -> Any:
        try:
            entries = await self._reader()
        except Exception as exc:  # the oracle is not the subject of the scan
            logger.warning("verify tool raised: %s", exc)
            self._failed = True
            return None
        if entries is None:
            self._failed = True
        return entries

    def status(self) -> str:
        """`absent`, `unattributed`, `failed`, `stale`, or `ok`.

        Five values rather than a boolean, because each one licenses a different
        claim and a consumer should not have to substring-match prose to tell
        them apart -- the `DimensionScore.not_scored` lesson, applied here.
        """
        # `absent` is tested first and on its own. Written the other way round --
        # falling through to `failed` when `_before` is None -- made `absent`
        # unreachable, because an oracle that was never configured never takes a
        # snapshot. That reports "we could not look" where "nothing was asked"
        # is true, which is the same collapse this release exists to undo.
        if self._reader is None:
            return "absent"
        if self._failed or self._before is None or self._after is None:
            return "failed"
        if not self._uses_op_id or not self._ids:
            return "unattributed"
        if any(count_matching(self._before, op_id) for op_id in self._ids.values()):
            return "stale"
        return "ok"

    def metrics(self) -> dict[str, Any]:
        status = self.status()
        effects: dict[str, int] = {}
        if status == "ok":
            effects = {
                key: (
                    count_matching(self._after, op_id)
                    - count_matching(self._before, op_id)
                )
                for key, op_id in self._ids.items()
            }

        data: dict[str, Any] = {
            "effect_oracle_status": status,
            "effects_by_op": effects,
            "operations_registered": len(self._ids),
            # The ids themselves, so a report can name the one that was applied
            # twice (1.4.1). `effects_by_op` keys are operation labels; the id
            # is what a reader greps their own logs for, and deriving it again
            # in a renderer would put the phase salt in two places.
            "op_ids": dict(self._ids),
        }
        if status == "ok":
            data["unattributed_effects"] = self._unattributed()
        if status == "unattributed":
            # Aggregate mode: the numbers are real observations, and reported,
            # but nothing is scored from them (§2c).
            data["observed_effects"] = self._aggregate()
        return data

    def _aggregate(self) -> int | None:
        for value in (self._after, self._before):
            if value is None:
                return None
        after = len(self._after) if isinstance(self._after, list) else self._after
        before = len(self._before) if isinstance(self._before, list) else self._before
        if isinstance(after, int) and isinstance(before, int):
            return after - before
        return None

    def _unattributed(self) -> int:
        """Entries that appeared in the window and match no registered id.

        Pre-existing state is excluded by the diff; what is left is a write from
        outside this window -- a concurrent writer, or a contract case that was
        accepted and applied. Neither a duplicate nor a loss, so it is reported
        and never counted, and a non-zero value caveats both scored metrics.
        """
        if not isinstance(self._after, list) or not isinstance(self._before, list):
            return 0
        ids = set(self._ids.values())

        def attributable(entry: Any) -> bool:
            blob = json.dumps(entry, sort_keys=True, default=str)
            return any(op_id in blob for op_id in ids)

        after = sum(1 for entry in self._after if not attributable(entry))
        before = sum(1 for entry in self._before if not attributable(entry))
        return max(0, after - before)


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


def _trajectory_metrics(
    trajectories: list[Trajectory], invocations: list[Invocation]
) -> dict[str, Any]:
    """The same arithmetic over either source of invocations.

    Takes the list rather than the `FaultProxy` that happens to hold it, because
    since Phase C there are two producers: the proxy this process built, and a
    record file written by one a process away. One function, so the two paths
    cannot report the same thing differently.
    """
    total = len(trajectories)
    attempts = sum(t.attempts for t in trajectories)
    failed_first = [t for t in trajectories if t.invocations and not t.invocations[0].ok]
    recovered = [t for t in failed_first if t.recovered]
    recovery_latencies = [
        t.recovery_latency_s for t in recovered if t.recovery_latency_s is not None
    ]

    calls = len(invocations)
    failures = sum(1 for inv in invocations if not inv.ok)

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
        # Calls this scan re-sent after losing or damaging an acknowledged
        # reply. Not `duplicate_mutations` (1.3.0's key here): whether any was
        # applied twice is in the target's state, which nothing reads.
        "duplicate_deliveries": sum(t.duplicates for t in trajectories),
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

    if metrics["duplicate_deliveries"]:
        count = metrics["duplicate_deliveries"]
        # Who re-sent it depends on which branch ran: the recovery pass retries
        # a service, and an agent retries itself. `interposed` is the fact that
        # decides it, and it is set only by the out-of-process branch.
        whose = "The agent re-sent" if metrics.get("interposed") else "Re-sent"
        findings.append(
            f"{whose} {count} {'call' if count == 1 else 'calls'} the target had "
            "already acknowledged, after this scan dropped or damaged the reply. "
            "Not scored: whether any was applied twice is in the target's state."
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
