"""How long the agent will wait for a reply that is late but coming.

**Experimental (1.6.1), not frozen** -- see `docs/API-STABILITY.md`.

The Phase D spike found that Claude Code 2.1.275 sat on a dropped reply for 234
seconds with no retry, no cancellation and no return, and separately waited out a
90-second hold and accepted the late answer. The two official MCP SDKs disagree
on the default -- TypeScript bounds a request at 60s, Python sets none -- so
"does this agent survive a dependency that stops answering" is a property of the
host, and the only honest way to learn it is to measure it.

**A per-task deadline of our own does not measure it.** It keeps the scan finite
and nothing more: the measurement *is* the agent's next move, and a scan that
killed the agent destroyed the thing it came to observe
(`DESIGN-AGENT-D.md`, Q1).

So the clean pass gets one extra task in which the proxy holds a single reply and
then **sends it**. Nothing is dropped. Three outcomes, and each is a different
finding:

| outcome | what happened | reported as |
|---|---|---|
| `acted` | it gave up, retried or returned at *t* | `client_timeout_s = t` |
| `waited_out` | it sat through the hold and took the late reply | `> hold_s`, a lower bound |
| `no_deadline` | it was still waiting when our own deadline killed it | the finding, unscored |

**`waited_out` is a bound and not a measurement**, and the wording has to say so.
An agent that waits out a 10-second hold has a read timeout longer than ten
seconds; it may have one at sixty. Reporting that as "no deadline" would be the
spike's own mistake in reverse -- it inferred 60s for this stack from an SDK
constant and was wrong by at least four times.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The pass the held task runs in. Its own pass, so its record and its schedule
#: are separate files: folding it into `baseline` would put its calls in
#: `clean_calls_by_task`, which is the denominator of `retry_amplification`, and
#: a probe that changed the denominator of another metric would be measuring
#: itself.
DEADLINE_PASS = "deadline"

#: Hold used by the bare `--hold-reply` flag.
#:
#: **Ten seconds, and it is a bound rather than a verdict.** It is comfortably
#: past the OpenAI Agents SDK's 5s default, so that stack is measured rather
#: than guessed at. It is *under* the TypeScript SDK's 60s default, so a client
#: on that default waits it out and is reported as "none under 10s" -- which is
#: what the scan actually established, and is why `waited_out` is worded as a
#: lower bound everywhere it appears.
#:
#: The alternative was 60s+, which measures more and costs a minute of wall
#: clock per scan plus a whole extra agent run's tokens. A user who wants the
#: stronger statement passes the number; the default is the one that is cheap
#: and honest about what it did not settle.
DEFAULT_HOLD_S = 10.0

#: Slack when deciding whether the agent moved *before* the release. The
#: release time is computed from the proxy's own stamps, and a next call that
#: lands within a few milliseconds of it is the agent reacting to the reply
#: rather than to the wait.
RELEASE_TOLERANCE_S = 0.05

#: Which of the two clocks the run actually reached. `hold` means the reply was
#: released and the agent saw it; `task_deadline` means we killed the agent
#: first, so the bound is ours and not the hold's.
BOUND_BY_HOLD = "hold"
BOUND_BY_DEADLINE = "task_deadline"

OUTCOME_ACTED = "acted"
OUTCOME_WAITED_OUT = "waited_out"
OUTCOME_NO_DEADLINE = "no_deadline"
OUTCOME_UNMEASURED = "unmeasured"


def measure(
    rows: list[dict[str, Any]],
    *,
    task_outcome: str,
    finished_at: float | None,
    task_deadline_s: float,
) -> dict[str, Any]:
    """What the held reply showed, from the record and the agent's exit.

    `rows` is every row of the held task's record, **notifications included**:
    MCP's way to abandon a request is `notifications/cancelled`, so the one
    signal that says "this agent gave up deliberately" is not an invocation and
    would be filtered out by `invocation_rows`.

    The clock is the record's, `time.time()`, and `finished_at` is stamped on
    the same clock when the agent process ends. `Response.latency_s` cannot
    serve: it is `perf_counter`, which shares no origin with the record.
    """
    held = next(
        (row for row in rows if isinstance(row.get("held_s"), (int, float))), None
    )
    if held is None or held.get("received_at") is None:
        # The hold was configured and no row carries it. Absence, reported as
        # absence: a zero here would be "the agent gave up instantly".
        return {
            "client_timeout_outcome": OUTCOME_UNMEASURED,
            "client_timeout_s": None,
            "client_timeout_bound_s": None,
            "client_timeout_bound_by": None,
            "client_timeout_hold_s": None,
            "task_deadline_s": task_deadline_s,
        }

    hold = float(held["held_s"])
    arrived = float(held["received_at"])
    release = arrived + hold

    # Everything the agent did after the call it is waiting on: another call, a
    # cancellation, any notification at all. The earliest is when it stopped
    # waiting.
    moves = [
        float(row["received_at"])
        for row in rows
        if row is not held
        and isinstance(row.get("received_at"), (int, float))
        and float(row["received_at"]) > arrived
    ]
    if finished_at is not None:
        moves.append(float(finished_at))
    earliest = min(moves) if moves else None

    common = {
        "client_timeout_hold_s": hold,
        "task_deadline_s": task_deadline_s,
    }

    if task_outcome == "abandoned":
        # Still waiting when we killed it. The number is a lower bound on its
        # patience and is explicitly not a timeout: it is the absence of one, as
        # far as this hold could see.
        return {
            **common,
            "client_timeout_outcome": OUTCOME_NO_DEADLINE,
            "client_timeout_s": None,
            "client_timeout_bound_s": (
                round(float(finished_at) - arrived, 3) if finished_at else None
            ),
            # Ours, not the hold's: the agent never got to see whether the reply
            # would arrive, because we stopped it first.
            "client_timeout_bound_by": BOUND_BY_DEADLINE,
        }

    if earliest is not None and earliest < release - RELEASE_TOLERANCE_S:
        return {
            **common,
            "client_timeout_outcome": OUTCOME_ACTED,
            "client_timeout_s": round(earliest - arrived, 3),
            "client_timeout_bound_s": None,
            "client_timeout_bound_by": None,
        }

    return {
        **common,
        "client_timeout_outcome": OUTCOME_WAITED_OUT,
        "client_timeout_s": None,
        "client_timeout_bound_s": hold,
        "client_timeout_bound_by": BOUND_BY_HOLD,
    }


def describe(metrics: dict[str, Any]) -> str | None:
    """One line for the report header, beside the per-task deadline.

    **Both numbers or neither.** A scan whose deadline is shorter than the
    agent's patience measures the deadline, and the only way a reader can see
    that is to be shown the pair.
    """
    outcome = metrics.get("client_timeout_outcome")
    if not outcome:
        return None
    deadline = metrics.get("task_deadline_s")
    hold = metrics.get("client_timeout_hold_s")
    suffix = f"; per-task deadline {deadline:.0f}s" if deadline else ""

    if outcome == OUTCOME_ACTED:
        return (
            f"Client timeout: the agent stopped waiting after "
            f"{metrics['client_timeout_s']:.1f}s of a {hold:.0f}s held reply{suffix}"
        )
    if outcome == OUTCOME_WAITED_OUT:
        return (
            f"Client timeout: none under {hold:.0f}s -- the agent waited out the "
            f"held reply and accepted it. A lower bound, not a measurement"
            f"{suffix}"
        )
    if outcome == OUTCOME_NO_DEADLINE:
        bound = metrics.get("client_timeout_bound_s")
        waited = f" after {bound:.0f}s" if bound else ""
        return (
            f"Client timeout: **none observed under {bound:.0f}s**. The agent was "
            f"still waiting on a {hold:.0f}s held reply{waited} when the scan's own "
            f"deadline killed it, so this bounds its patience at the deadline "
            f"rather than at the hold. Reported, not scored{suffix}"
        )
    return (
        f"Client timeout: not measured -- the hold was configured and no call "
        f"carried it{suffix}"
    )


def finding(metrics: dict[str, Any]) -> str | None:
    """The user-facing paragraph, for the outcome that is a production defect."""
    if metrics.get("client_timeout_outcome") != OUTCOME_NO_DEADLINE:
        return None
    hold = metrics.get("client_timeout_hold_s")
    return (
        f"The agent applies no client-side read timeout that a {hold:.0f}s hold "
        f"could reach. A dependency that stops answering therefore blocks it "
        f"indefinitely: not a slow request, but one with no deadline to expire. "
        f"This is reported and not scored -- it is an unmeasurable rather than a "
        f"measurement, and the scan cannot say what the timeout is, only that it "
        f"is longer than the hold. Every MCP client library can bound a request; "
        f"the Python SDK sets no default and the TypeScript one sets 60s, so this "
        f"is a property of the host rather than of MCP."
    )


__all__ = [
    "DEADLINE_PASS",
    "OUTCOME_ACTED",
    "OUTCOME_NO_DEADLINE",
    "OUTCOME_UNMEASURED",
    "OUTCOME_WAITED_OUT",
    "describe",
    "finding",
    "measure",
]
