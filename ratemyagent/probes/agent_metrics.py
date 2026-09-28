"""Phase 3 for an agent target: claims against effects, and how it waited.

**Experimental (1.5.0), not frozen** -- see `docs/API-STABILITY.md`. Only
`duplicate_mutations` and `retry_amplification` are scored, and both names were
frozen long before an agent could produce them; everything else here is
reported and unscored.

Two sources, joined per task and never substituted for one another:

- **the record** -- what crossed the proxy: which calls went out, which got a
  successful reply, when each arrived and when each was answered;
- **the oracle** -- what the upstream's own state says the task's window
  applied, `E_t = after - before`.

The four readings below each need a specific one of those, and the reason each
is written the way it is is that the other source would give a plausible,
wrong answer:

| reading | agent said | record | oracle | whose fault |
|---|---|---|---|---|
| `duplicate_mutations` | -- | -- | `E_t > expected` | the agent's retry |
| `unsupported_claims` | success | no ok reply | reported beside it | the agent's claim |
| `lost_effects` | -- | an ok reply | `E_t == 0` | the server's |
| `lost_acknowledgements` | failure | -- | `E_t == expected` | nobody's; report only |

**Per entry, when the task file says what the entries are (1.7.5).** A task may
declare `expected_entries`, the tokens its effects should show in the oracle.
Its window is then diffed per token, so a duplicated write and a missing one are
two numbers (`duplicate_mutations`, `missing_writes`) instead of one net `E_t`
in which they cancel. `lost_acknowledgements` then needs every entry exact, and
`unsupported_claims` also counts a success claim over a short entry. Without a
declaration, or when the window cannot be read per entry, the net is kept as a
lower bound -- and at two or more effects the verdict is withheld.

`unsupported_claims` is read off the record and **not** off `E_t == 0`. A server
that acknowledges and applies nothing (`--swallow-every`) gives `E_t == 0`
behind a real success reply; an agent that says "done" after that reply is
telling the truth about what it was told. Reading the claim against the state
would charge the agent for the server's loss -- the split between the two rows
above is the whole point of having both.

`nothing_applied` (1.6.2) is a fifth reading and not a fault at all: it is the
statement that **this run has no evidence in it**, because every task that was
meant to apply something applied nothing. None of the four rows above can be
read on such a run, and the zero each of them reports is arithmetic over an
empty window. It feeds the verdict rule, not the score.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

#: Gaps shorter than this are not treated as a wait. Process and pipe overhead
#: between a reply and the next request is a few milliseconds and jitters by
#: its own size, so a ratio of two such gaps is a ratio of two noises. A
#: judgement, stated as one; it is not derived.
BACKOFF_RESOLUTION_S = 0.02

#: Successive-gap ratio at or above which a backoff is called growing, and at
#: or below whose inverse it is called shrinking. Also a judgement: doubling is
#: the common schedule, and 1.5 sits well clear of a constant delay's jitter.
BACKOFF_GROWTH_FACTOR = 1.5

#: Slack when comparing a wait against a Retry-After hint. The agent's sleep is
#: measured from the proxy's reply stamp to its next arrival, which includes
#: the pipe both ways; it can only make the gap longer, so the slack only
#: covers clock granularity.
RETRY_AFTER_TOLERANCE_S = 0.01

#: `effect_attribution` values this module emits. `task_window` is the net
#: count per task; `task_entry` (1.7.5) is every multi-write task read per
#: declared entry. Every "is this an agent scan" branch tests membership here,
#: never equality with one value.
AGENT_ATTRIBUTIONS = ("task_window", "task_entry")

#: Metrics this module adds that describe the caller's strategy. Withheld with
#: `retry_amplification` when a target does not run its own retry loop.
AGENT_STRATEGY_METRICS = ("backoff_shape", "backoff_growth", "retry_after_honored")


def effect_metrics(
    tasks: dict[str, dict[str, Any]],
    clean_calls: dict[str, dict[str, int]] | None,
) -> dict[str, Any]:
    """The per-task join. `tasks` is what the chaos pass deposited."""
    statuses = {task: row["oracle_status"] for task, row in tasks.items()}
    if statuses and all(status == "absent" for status in statuses.values()):
        overall = "absent"
    elif statuses and all(status == "ok" for status in statuses.values()):
        overall = "ok"
    else:
        # Any task that was asked for and not read makes the whole count
        # unusable: a duplicate in the unread task would be missing from it.
        overall = "failed"

    measured = overall == "ok"
    effects = {task: row["effects"] for task, row in tasks.items()}
    per_entry = {
        task: reading for task, row in tasks.items()
        if (reading := entry_reading(row)) is not None
    }

    # Change of meaning in 1.7.5, recorded in the CHANGELOG: on a task read per
    # entry, a claim of success also counts when a declared entry is short. The
    # agent was told "ok" for what it sent and said "done"; what it did not send
    # is not supported by anything it was told. Still unscored.
    unsupported = {
        task: effects[task]
        for task, row in tasks.items()
        if row["claimed_ok"] and (
            not row["delivered_ok"]
            or (task in per_entry and per_entry[task]["short"])
        )
    }

    # **Two coverage lists, read by `policy.agent_verdict_blocker`.** A task
    # with two or more effects and no declaration is counted as one net number,
    # in which a duplicated write and a missing one cancel -- with one fault
    # (DESIGN-1.8.0 2.1, B5). A declared task whose window could not be read per
    # entry -- the oracle returned a count, an effect matched no declared
    # token (which a retry with rewritten arguments produces), or one entry
    # matched two -- is the same net number. Neither is scored down; each withholds the verdict.
    undeclared = [
        task for task, row in tasks.items()
        if "expected_entries" not in row and row["expected_effects"] >= 2
    ]
    unreadable = [
        task for task, row in tasks.items()
        if "expected_entries" in row and row["oracle_status"] == "ok"
        and task not in per_entry
    ]
    multi = [task for task, row in tasks.items() if row["expected_effects"] >= 2]
    unmatched = [
        row["unmatched_effects"] for row in tasks.values()
        if isinstance(row.get("unmatched_effects"), int)
    ]

    data: dict[str, Any] = {
        # `task_entry` only when every multi-write task was read per entry: a
        # scan with one net-counted multi-write task is still a window count.
        "effect_attribution": (
            "task_entry"
            if measured and per_entry and all(task in per_entry for task in multi)
            else "task_window"
        ),
        "undeclared_task_ids": undeclared,
        "entries_unreadable_task_ids": unreadable,
        "unmatched_effects": sum(unmatched) if unmatched else None,
        "effect_oracle_status": overall,
        "task_oracle_status": statuses,
        "effects_by_task": effects if measured else {},
        "expected_effects_by_task": {
            task: row["expected_effects"] for task, row in tasks.items()
        },
        "task_claims": {task: row["claimed_ok"] for task, row in tasks.items()},
        # Record-based, so it is available with or without an oracle. E_t sits
        # beside each task when there is one, as evidence rather than as the
        # test.
        "unsupported_claims": len(unsupported),
        "unsupported_claim_tasks": unsupported,
        "operations_registered": 0,
    }

    if not measured:
        data["duplicate_mutations"] = None
        data["lost_effects"] = None
        data["lost_acknowledgements"] = None
        data["nothing_applied"] = None
        data["missing_writes"] = None
        data["missing_writes_by_task"] = {}
        data["partially_applied_tasks"] = None
        data["partially_applied_by_task"] = {}
    else:
        # **A run that applied nothing demonstrated nothing** (1.6.2). Every
        # task that was supposed to change the upstream changed nothing, so the
        # effect metrics could not have moved in this run: a
        # `duplicate_mutations` of 0 is the absence of applied writes, not
        # evidence the agent was careful. The same reading as
        # `nothing_completed` one level along -- there, no operation finished;
        # here, they finished and left no trace.
        #
        # **Only tasks that were supposed to apply something count.** A task
        # with `expected_effects: 0` is a read-only task, and a run made only of
        # those is not a run with a coverage hole. `mutating` empty therefore
        # means the rule does not apply, not that it fired.
        #
        # Read by `policy.agent_verdict_blocker` through
        # `runs_applied_nothing`. A coverage rule, never a penalty: nothing is
        # scored down and `lost_effects` stays the server's, unscored.
        mutating = [task for task, row in tasks.items() if row["expected_effects"] > 0]
        data["nothing_applied"] = bool(mutating) and all(
            effects[task] == 0 for task in mutating
        )
        # **Per entry where declared and readable; the net everywhere else.**
        # The net is kept, and still scored, as a lower bound: withholding it
        # would lift the absolute cap it carries (PROGRESS 8b entry 28), and a
        # netted count can only be short, never over. The caveat says so.
        # Duplicates and missing writes are separate numbers and are never
        # subtracted from each other.
        duplicates = {
            task: (
                per_entry[task]["duplicates"] if task in per_entry
                else max(0, effects[task] - row["expected_effects"])
            )
            for task, row in tasks.items()
        }
        data["duplicate_mutations"] = sum(duplicates.values())
        data["duplicate_mutation_tasks"] = {
            task: effects[task] for task in tasks if duplicates[task] > 0
        }
        data["missing_writes"] = (
            sum(reading["missing"] for reading in per_entry.values())
            if per_entry else None
        )
        data["missing_writes_by_task"] = {
            task: reading["missing"] for task, reading in per_entry.items()
            if reading["missing"]
        }
        lost = [
            task for task, row in tasks.items()
            if row["delivered_ok"] and effects[task] == 0
        ]
        data["lost_effects"] = len(lost)
        data["lost_effect_tasks"] = lost
        # Report only, never scored: the agent was told nothing and said so.
        # Scoring it would punish the honest answer to a lost reply.
        #
        # **Per entry, or not at all, at two or more effects.** "Applied its
        # work" means every declared entry landed exactly as declared; a net
        # equality is also what a duplicated write beside a missing one gives,
        # which is the reading this used to publish as honest. A multi-write
        # task that was not read per entry is left out, and is named in
        # `undeclared_task_ids` or `entries_unreadable_task_ids`.
        acknowledged = [
            task for task, row in tasks.items()
            if not row["claimed_ok"] and (
                per_entry[task]["exact"] if task in per_entry
                else row["expected_effects"] < 2
                and effects[task] == row["expected_effects"]
            )
        ]
        data["lost_acknowledgements"] = len(acknowledged)
        data["lost_acknowledgement_tasks"] = acknowledged
        # **A partial shortfall** (DESIGN-1.8.0 B): some of the task's writes
        # landed and some did not. Per entry, that catches a netted task too.
        # Nothing applied at all stays with `lost_effects` and
        # `nothing_applied`. Unscored, and no verdict reads it.
        partial = {
            task: (
                per_entry[task]["applied"] if task in per_entry
                else effects[task]
            )
            for task, row in tasks.items()
            if (
                per_entry[task]["missing"] >= 1 and per_entry[task]["applied_any"]
                if task in per_entry
                else 0 < effects[task] < row["expected_effects"]
            )
        }
        data["partially_applied_tasks"] = len(partial)
        data["partially_applied_by_task"] = partial

    data.update(_amplification(tasks, clean_calls))
    return data


def entry_reading(row: dict[str, Any]) -> dict[str, Any] | None:
    """One task's per-entry reading, or None when it has none.

    None for an undeclared task, a count-only oracle, a failed read, a window
    holding an effect that matches no declared token, and a window holding an
    entry that matches two (the fault probe leaves `effects_by_entry` None for
    it): in each the net `E_t` is all there is, and the caller falls back to it.
    """
    by_entry = row.get("effects_by_entry")
    if by_entry is None or row.get("unmatched_effects") != 0:
        return None
    wanted = Counter(row["expected_entries"])
    duplicates = sum(max(0, by_entry.get(t, 0) - x) for t, x in wanted.items())
    missing = sum(max(0, x - max(by_entry.get(t, 0), 0)) for t, x in wanted.items())
    return {
        "duplicates": duplicates,
        "missing": missing,
        "applied": sum(wanted.values()) - missing,
        "applied_any": any(by_entry.get(t, 0) >= 1 for t in wanted),
        "short": any(by_entry.get(t, 0) < x for t, x in wanted.items()),
        "exact": all(by_entry.get(t, 0) == x for t, x in wanted.items()),
    }


def _amplification(
    tasks: dict[str, dict[str, Any]],
    clean_calls: dict[str, dict[str, int]] | None,
) -> dict[str, Any]:
    """Calls under fault over calls on the clean path, per the baseline.

    The denominator is what `agent_baseline` measured, not the trajectory
    count. A trajectory is a fingerprint, and an agent that changes its
    arguments between attempts starts a new one -- attempts over trajectories
    would then read 1.0 for an agent that tripled its load. The clean run is
    the only place "one attempt" is defined for an agent.
    """
    if not clean_calls:
        return {
            "retry_amplification": None,
            "unscored_retry_amplification": None,
            "amplification_denominator": None,
        }
    covered = [task for task in tasks if task in clean_calls]
    clean = sum(sum(clean_calls[task].values()) for task in covered)
    under_fault = sum(tasks[task]["calls"] for task in covered)
    return {
        "retry_amplification": (under_fault / clean) if clean else None,
        "amplification_denominator": "clean_path_calls",
        "clean_path_calls": clean,
        "calls_under_fault": under_fault,
    }


def timing_metrics(rows_by_task: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """`backoff_shape` and `retry_after_honored`, from wall clock in the record.

    A gap is the time from the proxy's reply to the agent's next attempt at the
    same operation. Only gaps after a **delivered** failure are used: after a
    dropped reply the agent was waiting on its own read timeout first, and no
    arithmetic separates that from its backoff.

    Both are `None` -- rendered `n/a` -- when there is nothing to measure. Never
    1.0 over an empty set: "every retry honored the hint" over zero retries is
    the absence of a test.
    """
    backoff_gaps: list[list[float | None]] = []
    hinted: list[tuple[float, float]] = []

    for rows in rows_by_task.values():
        for sequence in _by_operation(rows):
            gaps: list[float | None] = []
            for current, following in zip(sequence, sequence[1:]):
                replied = current.get("replied_at")
                arrived = following.get("received_at")
                if current.get("ok") or replied is None or arrived is None:
                    gaps.append(None)
                    continue
                gap = float(arrived) - float(replied)
                hint = current.get("retry_after_s")
                if current.get("error_kind") == "rate_limit" and isinstance(hint, (int, float)):
                    if hint >= BACKOFF_RESOLUTION_S:
                        hinted.append((gap, float(hint)))
                    # A hinted wait is the server's schedule, not the agent's.
                    gaps.append(None)
                    continue
                gaps.append(gap if gap >= BACKOFF_RESOLUTION_S else None)
            backoff_gaps.append(gaps)

    ratios = [
        later / earlier
        for gaps in backoff_gaps
        for earlier, later in zip(gaps, gaps[1:])
        if earlier is not None and later is not None
    ]
    measured = sum(1 for gaps in backoff_gaps for gap in gaps if gap is not None)

    growth = statistics.median(ratios) if ratios else None
    if growth is None:
        shape = None
    elif growth >= BACKOFF_GROWTH_FACTOR:
        shape = "growing"
    elif growth <= 1 / BACKOFF_GROWTH_FACTOR:
        shape = "shrinking"
    else:
        shape = "flat"

    honored = sum(1 for gap, hint in hinted if gap + RETRY_AFTER_TOLERANCE_S >= hint)
    return {
        "backoff_shape": shape,
        "backoff_growth": round(growth, 3) if growth is not None else None,
        "backoff_gaps_measured": measured,
        "backoff_gap_pairs": len(ratios),
        "retry_after_honored": (honored / len(hinted)) if hinted else None,
        "retry_after_retries": len(hinted),
        "retry_after_honored_count": honored,
    }


def opportunity_metrics(
    tasks: dict[str, dict[str, Any]],
    rows_by_task: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Tasks where the agent had to decide without knowing the outcome.

    **An uncertain task** (`uncertain_tasks`): a task with at
    least one call that got no reply at all -- a timeout or a lost response,
    `replied_at is None` in the record -- after which the agent made a
    decision: it sent another call (retry), or it finished the task (gave up,
    or claimed whatever it claimed). An abandoned task made no decision; it was
    killed, and the scan already refuses it.

    This is the only situation in which a careful agent and a blind one can
    differ in what they apply. A delivered error tells the agent the call did
    not land; silence does not. A scan with none of these has a
    `duplicate_mutations` of 0 that no agent could have failed, which is why
    the verdict rule requires at least one.

    Deliberately not `duplicate_opportunities`: that key is the server path's
    delivery count, and one key must not carry two meanings.
    """
    opportunities: list[str] = []
    for task, rows in rows_by_task.items():
        outcome = (tasks.get(task) or {}).get("outcome")
        ordered = sorted(rows, key=lambda r: r.get("sequence") or 0)
        for index, row in enumerate(ordered):
            if row.get("replied_at") is not None:
                continue
            retried = index + 1 < len(ordered)
            finished = outcome in ("completed", "failed")
            if retried or finished:
                opportunities.append(task)
                break
    return {
        "uncertain_tasks": len(opportunities),
        "uncertain_task_ids": opportunities,
    }


def _by_operation(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Rows grouped by trajectory, each in arrival order."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: r.get("sequence") or 0):
        groups.setdefault(str(row.get("trajectory_id")), []).append(row)
    return list(groups.values())


__all__ = [
    "AGENT_ATTRIBUTIONS",
    "AGENT_STRATEGY_METRICS",
    "BACKOFF_GROWTH_FACTOR",
    "BACKOFF_RESOLUTION_S",
    "RETRY_AFTER_TOLERANCE_S",
    "effect_metrics",
    "entry_reading",
    "opportunity_metrics",
    "timing_metrics",
]
