"""`ratemyagent proxy`: the FaultProxy, one process away.

Phase C scans an *agent*, and an agent's tool calls happen inside a process the
scanner did not write. This module is how the scanner gets between them: a stdio
MCP server that the agent launches from its own MCP config, holding
`FaultProxy(MCPTarget(<upstream>))` underneath.

It owns three things and deliberately nothing else:

1. the server half of MCP -- `initialize`, `tools/list`, `tools/call`,
2. translation of an inbound `tools/call` into a `Request` and of the resulting
   `Response` back onto the wire,
3. the record writer, and the replay that reads it back.

**Interposition is a new way to fill a `Trajectory`, not a new pipeline.** The
rows this writes replay into the same `Invocation` objects `FaultProxy` builds
in-process, and phase 3 cannot tell which produced them.

**The writer and the replay live in one module on purpose.** They are one format
described twice, and a format described in two files drifts.

Why a file and not a socket or an in-process hook: in Phase D the agent's MCP
config is a static JSON file a *user* maintains. A path in it is durable; a port
is not, and an in-process hook needs an agent the scanner started, in Python.
The record is also the artifact -- it is what settles "the agent says it
retried twice" after a run that already ended.

The server half is spoken as raw JSON-RPC over stdin/stdout rather than through
the SDK's server framework, because of one requirement the framework cannot
express: `RESPONSE_LOST` means **no response is ever written for that id**, and
a framework whose contract is "a handler returns a result" has nowhere to put
that. The fixtures in `tests/fixtures/` are raw JSON-RPC servers for the same
reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .models import ErrorKind, FaultKind, Invocation, Request, Response, Trajectory
from .targets.fault_proxy import FaultConfig, FaultProxy
from .targets.mcp import MCPTarget

logger = logging.getLogger(__name__)

#: The protocol version the proxy answers `initialize` with when the client did
#: not name one. Echoing the client's is the normal path; this is the fallback.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: Argument an agent may set to make a retry idempotent. Passed through to the
#: upstream byte-identical and recorded beside the call, so "careful reused its
#: key" is a fact in the record rather than a claim in a docstring.
IDEMPOTENCY_ARG = "idempotency_key"

#: Row kinds in the record. `invocation` replays into an `Invocation`; anything
#: else is context that is kept and not replayed.
ROW_INVOCATION = "invocation"
ROW_NOTIFICATION = "notification"

#: Keys a row carries that `Invocation` does not have a field for.
#:
#: `replied_at` and `retry_after_s` (C2) are what the agent-side timing metrics
#: read. `Invocation.started_at` and `latency_s` cannot serve: an injected
#: timeout *reports* thirty seconds and sleeps none, so `finished_at` is a
#: fiction for exactly the calls a backoff follows. Wall clock at arrival and at
#: reply is the only gap the agent actually waited through.
_ROW_EXTRA = (
    "kind", "task_id", "idempotency_key", "received_at", "replied_at",
    "retry_after_s", "held_s",
)

#: The error code a refused connection carries on the wire. Named rather than
#: taken from `ErrorKind.CONNECTION.value`, because "connection" says which
#: taxonomy bucket and not what happened, and the agent reads this body.
CONNECTION_REFUSED_CODE = "connection_refused"


# -- the record ------------------------------------------------------------


class RecordWriter:
    """Append-only JSONL, flushed per line.

    **One handle, flushed per line.** The process that writes this file is the
    one the fault is being done to: it can be killed by a task deadline, and an
    agent that hangs is exactly the case the scan has to be able to read
    afterwards. Without the flush the tail sits in a userspace buffer that no
    reader -- this process or any other -- can see, so a killed proxy leaves a
    record that looks like a short run rather than an interrupted one. The
    handle is held rather than reopened per line because that is what makes the
    flush the thing doing the work, instead of a close nobody can see.

    Sequence numbers are assigned from the row count of the file rather than
    from the proxy's own list, so a reconnecting agent's second proxy process
    continues the numbering instead of restarting it at zero.
    """

    def __init__(self, path: str | os.PathLike[str], *, task_id: str | None = None) -> None:
        self.path = Path(path)
        self.task_id = task_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_record(self.path)
        self._rows = len(existing)
        #: `(task_id, tool) -> calls already recorded`, so the forced schedule
        #: survives a reconnect. See `FaultProxy._ordinals`.
        self._handle: Any = None
        self._ordinals: dict[tuple[str, str], int] = {}
        for row in existing:
            if row.get("kind", ROW_INVOCATION) != ROW_INVOCATION:
                continue
            key = (row.get("task_id") or "", row.get("op") or "")
            self._ordinals[key] = self._ordinals.get(key, 0) + 1

    def ordinals(self) -> dict[tuple[str, str], int]:
        """Where the schedule had got to when the last process stopped."""
        return dict(self._ordinals)

    def write(
        self,
        invocation: Invocation,
        *,
        idempotency_key: str | None,
        received_at: float,
        replied_at: float | None = None,
        retry_after_s: float | None = None,
        held_s: float | None = None,
    ) -> dict[str, Any]:
        row = {
            "kind": ROW_INVOCATION,
            **invocation.to_dict(),
            "sequence": self._rows,
            "task_id": self.task_id,
            "idempotency_key": idempotency_key,
            "received_at": received_at,
            # None when nothing was written back: the reply was dropped on
            # purpose, and the gap before the next attempt is then the agent's
            # read timeout plus its backoff, which no arithmetic separates.
            "replied_at": replied_at,
            "retry_after_s": retry_after_s,
            # How long this reply was deliberately held before being sent, so a
            # reader can tell a slow upstream from one we made slow on purpose.
            # `None` on every row of every scan that did not ask for a hold.
            "held_s": held_s,
        }
        self._append(row)
        return row

    def note(self, method: str, params: Any, *, received_at: float) -> None:
        """Record something that is not an invocation but bears on one.

        `notifications/cancelled` after a dropped reply is the case this exists
        for: the agent gave up on a request the proxy deliberately never
        answered, and that is evidence about the agent rather than noise.
        """
        self._append({
            "kind": ROW_NOTIFICATION,
            "sequence": self._rows,
            "task_id": self.task_id,
            "method": method,
            "params": params,
            "received_at": received_at,
        })

    def _append(self, row: dict[str, Any]) -> None:
        if self._handle is None:
            self._handle = open(self.path, "a", encoding="utf-8")
        self._handle.write(json.dumps(row, default=str) + "\n")
        self._handle.flush()
        self._rows += 1

    def close(self) -> None:
        """Safe to call twice, and on a writer that never wrote."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_record(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every row in a record file, in the order it was written.

    A missing file is an empty list and **not an error here**: whether "no rows"
    is a legal answer depends on what asked, and the caller that knows is the
    one that refuses. `FaultInjector` treats an empty record for a task as
    unmeasured and exits 2; this function has no business deciding that.

    A truncated final line -- the proxy was killed mid-write -- is skipped with
    a warning rather than raising, because the rows before it are real
    observations.
    """
    file = Path(path)
    if not file.exists():
        return []

    rows: list[dict[str, Any]] = []
    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("record %s line %d is not JSON; skipping", file, number)
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def invocation_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the rows that describe a call."""
    return [row for row in rows if row.get("kind", ROW_INVOCATION) == ROW_INVOCATION]


#: The advice that used to be printed whatever was on disk. Kept, word for word,
#: for the one case the evidence still supports it: nothing was written, and
#: nothing else in the work directory shows the record path arriving.
ENV_BLOCK_ADVICE = (
    "Check the `env` block in {config} reaches the proxy: the MCP SDK copies six "
    "variables into a stdio child and drops the rest, so RMA_PROXY_RECORD travels "
    "in that block or not at all."
)


def explain_unrecorded(
    task_id: str,
    record_path: str | os.PathLike[str],
    config_path: str | os.PathLike[str],
    *,
    response: Any = None,
    under_fault: bool = False,
) -> str:
    """Why a task has no calls on its record -- from what is on disk, not a prior.

    **The refusal this feeds is right; the advice it used to carry was not.** It
    said the record "is empty or missing" and pointed at the config's `env`
    block whatever the record held. Gate BD's 2026-09-24 replication refused on
    a chaos record holding a `notifications/initialized` row -- written by the
    proxy, so the record path had demonstrably arrived -- and the refusal still
    said the file was empty and blamed the env block.

    **What the record can prove.** `RecordWriter` creates the file on its first
    row, in append mode, and the scan never creates or truncates one. So:

    - **missing** -- no proxy for this pass wrote anything. The env block is a
      real candidate, *unless* another pass's record for the same task, in the
      same work directory, holds rows: the same config template then carried
      the path, and this pass's proxy was never started or never connected.
    - **rows, none a call** -- the proxy wrote them, so the path arrived. The
      agent connected and made no tool call. The env block is never mentioned.
    - **present, no readable row** -- bytes but no parseable row means a proxy
      with the path was cut off mid-write; zero bytes means this proxy did not
      write it, and only then is the env block one candidate among several.
      Another pass's rows for the task outrank either.

    Every branch says "holds no tool calls" or what is literally true -- never
    "empty or missing" -- states the rows it found, and carries the agent's own
    account from `response` when there is one.
    """
    record = Path(record_path)
    where = f"for task {task_id!r}" + (" under fault" if under_fault else "")
    rows = read_record(record)
    siblings = _sibling_records(record, task_id)
    evidence = [(name, n_rows, n_calls) for name, n_rows, n_calls in siblings if n_rows]
    agent = _agent_account(response)

    if not record.exists():
        head = (
            f"no calls recorded {where}: there is no record at {record} -- no proxy "
            f"for this pass wrote a row, so nothing about this task was measured. "
            f"A record without a call is not zero calls -- it is no evidence."
        )
        if evidence:
            body = (
                f"{_sibling_sentence(evidence)} -- written through the same config "
                f"template, so the record path does reach the proxy, and this is "
                f"not the `env` block. This pass's proxy never wrote a row: the "
                f"agent did not start the server from {config_path}, or stopped "
                f"before connecting."
            )
        else:
            body = ENV_BLOCK_ADVICE.format(config=config_path)
        return _join(head, body, agent)

    if rows:
        head = (
            f"no calls recorded {where}: the record at {record} holds no tool calls "
            f"-- {_describe_rows(rows)} -- so nothing about this task was measured. "
            f"A record without a call is not zero calls -- it is no evidence."
        )
        body = (
            "Those rows were written by the proxy, so the record path reached it: "
            "this is not the `env` block. The agent connected and made no tool call."
        )
        if evidence:
            body += f" {_sibling_sentence(evidence)}."
        return _join(head, body, agent)

    size = record.stat().st_size
    head = (
        f"no calls recorded {where}: the record at {record} holds no tool calls -- "
        f"no readable row in {size} byte(s) -- so nothing about this task was "
        f"measured. A record without a call is not zero calls -- it is no evidence."
    )
    if evidence:
        body = (
            f"{_sibling_sentence(evidence)}, so the record path reaches the proxy "
            f"for this config, and this is not the `env` block. This pass recorded "
            f"nothing readable: the agent did not start the server, or it exited "
            f"first."
        )
    elif size:
        body = (
            "The proxy creates this file on its first write, so a proxy that had "
            "the record path started writing here and was cut off: this is not "
            "the `env` block."
        )
    else:
        body = (
            "The proxy creates this file only when it writes a row, so this "
            "zero-byte file was not written by it. Several causes fit and nothing "
            "here tells them apart: the agent never started the server from the "
            "config, or it exited before connecting, or the record path did not "
            "reach the proxy. For the last, "
            + ENV_BLOCK_ADVICE.format(config=config_path)[0].lower()
            + ENV_BLOCK_ADVICE.format(config=config_path)[1:]
        )
    return _join(head, body, agent)


def _sibling_records(record: Path, task_id: str) -> list[tuple[str, int, int]]:
    """Other passes' records for the same task, in the same work directory.

    Named `record-<pass>-<task>.jsonl`, and a pass name is a plain word (the
    ones in use are `baseline`, `chaos`, `chaos<n>`, `deadline`), so the pass is
    whatever sits between the prefix and `-<task>.jsonl` and contains no `-`.
    That keeps task `1` from claiming task `b-1`'s record.
    """
    suffix = f"-{task_id}.jsonl"
    found = []
    for path in sorted(record.parent.glob(f"record-*{suffix}")):
        name = path.name[len("record-"):-len(suffix)]
        if path == record or not name or "-" in name:
            continue
        rows = read_record(path)
        found.append((name, len(rows), len(invocation_rows(rows))))
    return found


def _sibling_sentence(evidence: list[tuple[str, int, int]]) -> str:
    parts = [
        f"the {name} record for this task holds {n_rows} row(s), "
        f"{n_calls} of them tool call(s)"
        for name, n_rows, n_calls in evidence
    ]
    return ("; ".join(parts))[0].upper() + ("; ".join(parts))[1:]


def _describe_rows(rows: list[dict[str, Any]]) -> str:
    """`1 row: notifications/initialized` -- what was found, counted."""
    counts: dict[str, int] = {}
    for row in rows:
        label = str(row.get("method") or row.get("kind") or "row")
        counts[label] = counts.get(label, 0) + 1
    listed = ", ".join(f"{label} x{n}" if n > 1 else label for label, n in counts.items())
    return f"{len(rows)} row(s): {listed}"


def _agent_account(response: Any) -> str | None:
    """What the agent itself reported, from the `Response` the scan holds."""
    if response is None:
        return None
    meta = getattr(response, "meta", None) or {}
    outcome = meta.get("outcome")
    if outcome == "abandoned":
        return ("The agent did not finish within the scan's deadline and was "
                "killed, so it reported nothing.")
    exit_code = meta.get("exit_code")
    exited = f"exited {exit_code}" if exit_code is not None else "exited"
    if getattr(response, "ok", False):
        return f"The agent {exited} and claimed success, with no call on the record."
    error = getattr(response, "error", None) or "no reason given"
    return f"The agent {exited} and reported failure: {error}"


def _join(head: str, body: str, agent: str | None) -> str:
    return head + "\n\n" + body + (f"\n\n{agent}" if agent else "")


def invocation_from_row(row: dict[str, Any]) -> Invocation:
    """One row back into the dataclass it was written from.

    The enums are re-read by value rather than trusted: a record written by an
    older build can carry a name this one does not have, and `None` is the
    honest answer for a kind we cannot name -- not a guess at the nearest one.
    """
    fields = {key: value for key, value in row.items() if key not in _ROW_EXTRA}
    error_kind = fields.pop("error_kind", None)
    injected = fields.pop("injected", None)
    return Invocation(
        **fields,
        error_kind=_enum(ErrorKind, error_kind),
        injected=_enum(FaultKind, injected),
    )


def _enum(cls: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        return cls(value)
    except ValueError:
        logger.warning("record carries unknown %s %r", cls.__name__, value)
        return None


def replay(rows: Iterable[dict[str, Any]]) -> tuple[list[Invocation], list[Trajectory]]:
    """Rows -> the invocations and trajectories phase 3 already reads.

    **Grouped by `trajectory_id`, which is the key rule `Trajectory` has always
    used** (`Request.trajectory_key`), and never by `op`. Grouping by the tool
    name would fold every call a task made to one tool into a single
    trajectory, so a task's three separate operations would read as one
    operation retried twice -- retry amplification 3.0 against an agent that
    retried nothing.

    The proxy sets `trajectory_id` to `{task}:{fingerprint}`, so an agent's
    retries -- identical arguments, therefore an identical fingerprint -- group
    together, and an agent that *changed* its arguments between attempts starts
    a new trajectory. That second case is a real property of the agent and is
    reported rather than hidden: it is not a retry if the call is not the same
    call.
    """
    invocations = sorted(
        (invocation_from_row(row) for row in invocation_rows(rows)),
        key=lambda inv: inv.sequence,
    )
    trajectories: dict[str, Trajectory] = {}
    for invocation in invocations:
        trajectories.setdefault(
            invocation.trajectory_id, Trajectory(trajectory_id=invocation.trajectory_id)
        ).invocations.append(invocation)
    return invocations, list(trajectories.values())


# -- the schedule ----------------------------------------------------------


def write_schedule(
    path: str | os.PathLike[str],
    schedule: dict[tuple[str, str, int], FaultKind],
    *,
    close_after_s: float | None = None,
    hold_s: float | None = None,
) -> None:
    """Write the forced fault table the proxy reads.

    A list of entries rather than an object keyed on a joined string: the key is
    a triple, and flattening it into `"t3|event|2"` puts a parser between the
    writer and the reader for no gain. A separator is also one tool name away
    from being ambiguous.

    `close_after_s` rides here because the schedule file is the only channel
    between the scan and the proxy that already carries fault decisions. It is
    written only when a closing fault is in play, so a schedule from a scan that
    does not use one is byte-identical to what 1.5.1 wrote.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"task_id": task, "tool": tool, "ordinal": ordinal, "fault": fault.value}
        for (task, tool, ordinal), fault in sorted(schedule.items())
    ]
    body: dict[str, Any] = {"entries": entries}
    if close_after_s is not None:
        body["close_after_s"] = close_after_s
    if hold_s is not None:
        body["hold_s"] = hold_s
    file.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _scalar(path: str | os.PathLike[str] | None, key: str) -> float | None:
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        return None
    value = json.loads(file.read_text(encoding="utf-8")).get(key)
    return float(value) if isinstance(value, (int, float)) else None


def read_close_after(path: str | os.PathLike[str] | None) -> float | None:
    """The session-close delay the schedule carries, or `None`.

    Separate from `read_schedule` rather than bundled into a return tuple: every
    existing caller wants the table and nothing else, and widening that return
    type would touch each of them to say "and ignore the second element".
    """
    return _scalar(path, "close_after_s")


def read_hold(path: str | os.PathLike[str] | None) -> float | None:
    """How long to hold the first reply of a task before sending it (1.6.1).

    **Not a fault, and deliberately not a `FaultKind`.** The reply arrives; it
    is late. Nothing is dropped, nothing is corrupted and no call is refused, so
    there is nothing for a fault table to record and no cumulative threshold to
    move -- and adding an enum member would be a minor release for something
    that is a measurement rather than a break (`docs/API-STABILITY.md`,
    Enumerations).

    What it measures is the agent's own client-side read timeout, by putting it
    in the one situation that reveals it: a dependency that has gone quiet but
    is not gone. See `probes.agent_deadline`.
    """
    return _scalar(path, "hold_s")


def read_schedule(
    path: str | os.PathLike[str] | None,
) -> dict[tuple[str, str, int], FaultKind] | None:
    """The table, or `None` when no schedule was given.

    `None` and `{}` mean different things and both are reachable: `None` leaves
    `_choose_fault`'s seeded draw alone, and an empty table forces *no faults*,
    which is what the baseline probe runs under. Collapsing them would make a
    clean baseline run indistinguishable from an unconfigured one.
    """
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        return None

    body = json.loads(file.read_text(encoding="utf-8"))
    schedule: dict[tuple[str, str, int], FaultKind] = {}
    for entry in body.get("entries", []):
        schedule[(entry["task_id"], entry["tool"], int(entry["ordinal"]))] = FaultKind(
            entry["fault"]
        )
    return schedule


# -- the wire --------------------------------------------------------------


def _jsonrpc_result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_payload(target: MCPTarget) -> list[dict[str, Any]]:
    """The upstream's tool listing, as close to verbatim as the SDK allows.

    Dumped from the SDK's own models rather than rebuilt from `ToolInfo`, which
    is a lossy projection -- it keeps four fields and drops everything else a
    server declared. An agent under test has to see the surface its real
    upstream exposes, including the parts this project has no opinion about.

    The fallback exists because `model_dump` is a pydantic detail of the SDK,
    and this project has already been bitten once by pinning itself to an SDK
    shape that changed between majors.
    """
    payload: list[dict[str, Any]] = []
    for raw in target.raw_tools():
        dumped = None
        dump = getattr(raw, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump(by_alias=True, mode="json", exclude_none=True)
            except Exception:  # pragma: no cover - SDK shape we have not seen
                dumped = None
        if isinstance(dumped, dict):
            payload.append(dumped)
            continue
        info = next(
            (t for t in target.list_tools() if t.name == getattr(raw, "name", None)), None
        )
        if info is not None:
            payload.append({
                "name": info.name,
                "description": info.description,
                "inputSchema": info.input_schema,
            })
    return payload


def response_to_wire(response: Response) -> dict[str, Any] | None:
    """One `Response` as a `tools/call` result, or `None` for silence.

    **Silence means nothing came back, and only a hang is rendered that way.**
    `RESPONSE_LOST` and an injected `TIMEOUT` are the two: the caller waits and
    has no evidence either way about whether the target ran, and over MCP the
    only faithful rendering is to write nothing for the id -- not then, not
    later. A JSON-RPC error of any code would be a delivered reply and would
    turn the hard case into the easy one.

    **A refused connection is not a hang** (C2, decision D1). A client whose
    dependency refuses finds out at once -- ECONNREFUSED arrives, it does not
    fail to arrive -- and it knows the call did not run. C1 read both off
    `delivered=False` and rendered refused as silence, which made every
    `CONNECTION_REFUSED` a three-second read timeout the agent could not tell
    from a lost reply: the one fault that carries certainty about execution,
    delivered as the one that carries none. `delivered` stays False on the
    `Response` -- nothing came back *from the dependency* -- and the proxy, which
    is what the agent is talking to, answers immediately with an error that says
    the call was not executed.
    """
    if _is_refused(response):
        return {
            "content": [{"type": "text", "text": json.dumps({"error": {
                "code": CONNECTION_REFUSED_CODE,
                "message": response.error or "connection refused",
                # A fact, not a guess: the proxy rejected it before forwarding.
                # The one failure where the caller can know the work did not
                # happen, so it is said in the body the caller reads.
                "executed": False,
                **({"injected": response.meta["injected"]}
                   if response.meta.get("injected") else {}),
            }})}],
            "isError": True,
        }

    if not response.delivered:
        return None

    if response.ok:
        return {"content": [{"type": "text", "text": _as_text(response.output)}]}

    return {"content": [{"type": "text", "text": _error_text(response)}], "isError": True}


def _is_refused(response: Response) -> bool:
    """A refused connection, injected or real.

    Keyed on the error kind rather than on `meta["injected"]`, so a real
    upstream that refuses -- `MCPTarget` classifies it `CONNECTION` -- reaches
    the agent the same way the injected one does. One rule for both, or the
    proxy would render the fault differently from the thing it imitates.
    """
    return not response.ok and response.error_kind is ErrorKind.CONNECTION


def _as_text(output: Any) -> str:
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, default=str)


def _error_text(response: Response) -> str:
    """A delivered failure, as a body an agent can act on.

    A JSON error payload rather than a sentence, because the hint an agent needs
    most -- `retry_after_s` on a 429 -- has nowhere else to go over stdio. There
    are no headers on this transport, which is exactly the case
    `parse_retry_after`'s docstring names: a server relaying an upstream 429
    inside a tool result. Servers really do answer this way (the FastMCP-shaped
    error bodies `_error_payload` already handles), so the fixture agents parse
    something they would meet in the field.

    A damaged payload is the exception: it is returned as the damage, because
    replacing it with a well-formed error would hand the caller the very
    structure the fault removed.
    """
    injected = response.meta.get("injected")
    if injected == FaultKind.MALFORMED.value:
        return _as_text(response.output)

    body: dict[str, Any] = {
        "error": {
            "code": response.error_kind.value if response.error_kind else "unknown",
            "message": response.error or "the call failed",
        }
    }
    for key in ("status", "retry_after_s"):
        if key in response.meta:
            body["error"][key] = response.meta[key]
    if injected:
        body["error"]["injected"] = injected
    return json.dumps(body)


class ProxyServer:
    """The server half of MCP, over stdin/stdout, in front of one upstream.

    Requests are served one at a time. That is not a limitation worked around:
    a dropped reply has to leave the session usable, and the test for it is that
    the *next* request is answered normally. Serialising makes that property
    hold by construction rather than by hoping a task group behaves.
    """

    def __init__(
        self,
        target: MCPTarget,
        *,
        record: RecordWriter,
        schedule: dict[tuple[str, str, int], FaultKind] | None,
        task_id: str | None,
        close_after_s: float | None = None,
        hold_s: float | None = None,
    ) -> None:
        self.target = target
        self.record = record
        self.task_id = task_id
        #: Hold the first reply of this task for this long, then send it.
        #: Cleared once it has been used, because it is one held reply per task
        #: and not one per call -- see `read_hold`.
        #:
        #: **Only when the record is empty**, so a reconnecting agent's second
        #: session does not hold a second time. The record is the one piece of
        #: state that survives the reconnect, which makes it the only honest
        #: place to ask "has this task already had its hold".
        self.hold_s = hold_s if not record.ordinals() else None
        #: `(deliver_at_monotonic, reply)` for replies computed but not yet
        #: written. The serve loop owns the clock; see `handle`.
        self.pending: list[tuple[float, dict[str, Any]]] = []
        self.proxy = FaultProxy(
            target,
            FaultConfig(
                rates={}, schedule=schedule, task_id=task_id,
                close_after_s=close_after_s,
            ),
            ordinals=record.ordinals(),
        )
        self._recorded = 0
        #: Seconds until this session must be closed, set by the one call that
        #: `RESPONSE_LOST_THEN_CLOSED` fired on. `None` until then and after the
        #: serve loop has read it: the close happens once per session, because a
        #: session can only end once.
        self.close_after: float | None = None

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        received_at = time.time()

        if request_id is None:
            if isinstance(method, str) and method.startswith("notifications/"):
                self.record.note(method, message.get("params"), received_at=received_at)
            return None

        if method == "initialize":
            params = message.get("params") or {}
            return _jsonrpc_result(request_id, {
                "protocolVersion": params.get(
                    "protocolVersion", DEFAULT_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {}},
                # The upstream's identity, not ours. The agent is being tested
                # against that server; a proxy that renamed it would be a
                # difference the agent could see.
                "serverInfo": {
                    "name": self.target.describe().name,
                    "version": self.target.describe().metadata.get("server_version")
                    or "unknown",
                },
            })

        if method == "ping":
            return _jsonrpc_result(request_id, {})

        if method == "tools/list":
            return _jsonrpc_result(request_id, {"tools": tool_payload(self.target)})

        if method == "tools/call":
            return await self._call(request_id, message.get("params") or {}, received_at)

        return _jsonrpc_error(request_id, -32601, f"method not found: {method}")

    async def _call(
        self, request_id: Any, params: dict[str, Any], received_at: float
    ) -> dict[str, Any] | None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return _jsonrpc_error(request_id, -32602, "tools/call needs a tool name")

        base = Request(
            op=name,
            # Byte-identical to what the agent sent, `idempotency_key` included.
            # The proxy relays; it does not edit.
            payload=dict(arguments),
            timeout_s=self.target.timeout_s,
            label=f"{self.task_id or 'task'}:{name}#{self._recorded}",
        )
        # Retries of one operation share arguments, so they share a fingerprint
        # and group into one trajectory. The task prefix keeps two tasks that
        # happen to make an identical call apart.
        request = replace(
            base, trajectory_id=f"{self.task_id or 'task'}:{base.fingerprint}"
        )

        before = len(self.proxy.invocations)
        response = await self.proxy.invoke(request)

        payload = response_to_wire(response)
        # **Held, not slept on.** The reply is computed now and written later,
        # and the serve loop keeps reading in between. A `sleep` here would stop
        # the session serving for the whole hold, so a `notifications/cancelled`
        # sent by an agent that gave up would not be *read* until after the
        # release -- and its arrival stamp is the measurement. Holding the
        # reply while still reading is the difference between observing the
        # agent's deadline and observing our own sleep.
        held = self.hold_s if payload is not None and self.hold_s else None
        if held:
            self.hold_s = None
        # Stamped before the row is written, and the row is written before the
        # reply: the reply goes out one line after this method returns, so the
        # stamp is early by microseconds and never late. A stamp taken after the
        # write would let a fast agent's next call arrive "before" the reply.
        replied_at = time.time() if payload is not None else None
        if held and replied_at is not None:
            # The stamp is when the reply goes out, not when it was computed:
            # the gap between the two is the hold, and a reader measuring the
            # agent's wait against the earlier stamp would measure our latency.
            replied_at += held
        hint = response.meta.get("retry_after_s")

        for invocation in self.proxy.invocations[before:]:
            self.record.write(
                invocation,
                idempotency_key=(
                    arguments.get(IDEMPOTENCY_ARG)
                    if isinstance(arguments.get(IDEMPOTENCY_ARG), str)
                    else None
                ),
                received_at=received_at,
                replied_at=replied_at,
                retry_after_s=float(hint) if isinstance(hint, (int, float)) else None,
                held_s=held,
            )
            self._recorded += 1

        if payload is None:
            # The reply is lost on purpose. Nothing is written for this id, and
            # the loop goes straight back to reading -- the session stays up.
            logger.info("dropping the reply to %s (%s)", request_id, response.error)
            hold = response.meta.get("close_after_s")
            if isinstance(hold, (int, float)) and self.close_after is None:
                # `RESPONSE_LOST_THEN_CLOSED`, and only that: a plain
                # `RESPONSE_LOST` carries no hold and leaves the session up,
                # exactly as it did in 1.5.1. The serve loop owns the clock; a
                # sleep here would stop the session serving during the hold,
                # and the hold is meant to be time in which the client can
                # still use a connection that is about to vanish.
                self.close_after = float(hold)
                logger.info(
                    "session will close %.1fs after the dropped reply to %s",
                    self.close_after, request_id,
                )
            return None
        reply = _jsonrpc_result(request_id, payload)
        if held:
            logger.info("holding the reply to %s for %.1fs", request_id, held)
            self.pending.append((time.monotonic() + held, reply))
            return None
        return reply


async def serve(
    *,
    upstream: str,
    record_path: str,
    schedule_path: str | None,
    task_id: str | None,
    timeout_s: float = 30.0,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Run the proxy until stdin closes."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    target = MCPTarget(
        upstream,
        timeout_s=timeout_s,
        # The agent is going to mutate; that is the point of the scan, and the
        # scan has already been cleared for it by `--allow-mutating` one process
        # up. The proxy does not re-litigate a decision it cannot see the flags
        # for.
        allow_mutating=True,
        # No preflight, no probe-tool selection. A proxy relays the agent's
        # calls and synthesizes none of its own, so the one preflight write
        # `setup()` normally makes would be a mutation nobody asked for -- once
        # per task, into the state the effect oracle is about to count.
        probe_traffic=False,
    )
    await target.setup()

    record = RecordWriter(record_path, task_id=task_id)
    server = ProxyServer(
        target,
        record=record,
        schedule=read_schedule(schedule_path),
        task_id=task_id,
        close_after_s=read_close_after(schedule_path),
        hold_s=read_hold(schedule_path),
    )
    logger.info("proxy up: upstream=%s record=%s task=%s", upstream, record_path, task_id)

    # **One daemon reader, feeding an asyncio queue.** The loop has to be able
    # to wake on a deadline as well as on a line, and `asyncio.to_thread` cannot
    # do that: a thread parked on a blocking `readline` is joined by
    # `asyncio.run` at shutdown, so a proxy that stopped waiting for input would
    # hang on the way out instead of exiting. A daemon thread is not joined, and
    # `call_soon_threadsafe` hands its lines to the loop without an executor.
    # Same shape the fixture agents' client uses, for the same reason.
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str] = asyncio.Queue()

    def pump() -> None:
        for line in source:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, "")  # EOF

    threading.Thread(target=pump, name="rma-proxy-stdin", daemon=True).start()

    # Set once a closing fault has fired; the deadline the loop races against.
    close_at: float | None = None

    def flush_before_closing() -> None:
        """Get anything already written out before the process ends."""
        try:
            sink.flush()
        except (OSError, ValueError):  # the client tore down first
            logger.debug("proxy stdout was already closed")

    def release_due() -> None:
        """Write out any held reply whose release time has come.

        **The hold is a delay, not a drop.** The reply was computed when the
        call was made and has been sitting here since; releasing it is the
        second half of the measurement, because an agent that waited it out and
        accepted the answer is a different finding from one that gave up.
        """
        now = time.monotonic()
        due = [reply for deliver_at, reply in server.pending if deliver_at <= now]
        server.pending[:] = [
            item for item in server.pending if item[0] > now
        ]
        for reply in due:
            sink.write(json.dumps(reply, default=str) + "\n")
            sink.flush()

    try:
        while True:
            # The earliest thing the loop has to wake for: a session that must
            # close, or a held reply that is due. Both are deadlines the loop
            # owns, and reading is what it does in between.
            deadlines = [at for at, _ in server.pending]
            if close_at is not None:
                deadlines.append(close_at)
            wake_at = min(deadlines) if deadlines else None

            if wake_at is None:
                line = await lines.get()
            else:
                try:
                    line = await asyncio.wait_for(
                        lines.get(), timeout=max(0.0, wake_at - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    release_due()
                    if close_at is None or time.monotonic() < close_at:
                        continue
                    # The hold elapsed with the client still connected. This is
                    # the branch `RESPONSE_LOST_THEN_CLOSED` exists for.
                    #
                    # **Ending the process is the close, and closing stdout is
                    # not.** The obvious implementation -- flush and close our
                    # own stdout -- does not work here and fails silently, which
                    # is worth the paragraph. The proxy spawns the upstream
                    # server, and that child inherits this process's descriptor
                    # 1. The pipe therefore still has a writer after we close
                    # our copy, so the client sits there reading a pipe that
                    # nobody will ever write to and nobody has closed: a hang,
                    # arrived at by the code that exists to prevent hangs.
                    # Measured while building this: EOF reached the client only
                    # when the process actually exited, ten seconds after the
                    # close.
                    #
                    # Returning runs the `finally` below, which closes the
                    # record and tears the upstream down, and then every
                    # descriptor goes with the process. That is also the more
                    # faithful fault: a stdio MCP server whose transport dies is
                    # a server that died.
                    logger.info(
                        "closing the session: the hold after a dropped reply "
                        "elapsed, so the proxy is exiting"
                    )
                    flush_before_closing()
                    return 0

            if not line:
                return 0
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("proxy received a line that is not JSON; ignoring")
                continue
            if not isinstance(message, dict):
                continue

            reply = await server.handle(message)
            if reply is not None:
                sink.write(json.dumps(reply, default=str) + "\n")
                sink.flush()
            # A held reply may have come due while that call was in flight.
            release_due()
            if server.close_after is not None and close_at is None:
                close_at = time.monotonic() + server.close_after
    finally:
        record.close()
        await target.teardown()


__all__ = [
    "CONNECTION_REFUSED_CODE",
    "IDEMPOTENCY_ARG",
    "ProxyServer",
    "RecordWriter",
    "invocation_from_row",
    "invocation_rows",
    "read_close_after",
    "read_record",
    "read_schedule",
    "replay",
    "response_to_wire",
    "serve",
    "tool_payload",
    "write_schedule",
]
