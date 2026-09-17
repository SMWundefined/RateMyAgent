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
_ROW_EXTRA = ("kind", "task_id", "idempotency_key", "received_at")


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
    ) -> dict[str, Any]:
        row = {
            "kind": ROW_INVOCATION,
            **invocation.to_dict(),
            "sequence": self._rows,
            "task_id": self.task_id,
            "idempotency_key": idempotency_key,
            "received_at": received_at,
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
    path: str | os.PathLike[str], schedule: dict[tuple[str, str, int], FaultKind]
) -> None:
    """Write the forced fault table the proxy reads.

    A list of entries rather than an object keyed on a joined string: the key is
    a triple, and flattening it into `"t3|event|2"` puts a parser between the
    writer and the reader for no gain. A separator is also one tool name away
    from being ambiguous.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"task_id": task, "tool": tool, "ordinal": ordinal, "fault": fault.value}
        for (task, tool, ordinal), fault in sorted(schedule.items())
    ]
    file.write_text(json.dumps({"entries": entries}, indent=2) + "\n", encoding="utf-8")


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

    **`None` is the whole point of this function.** `delivered=False` means
    nothing arrived, and over MCP the only faithful rendering of that is to
    write nothing for the id -- not then, not later. A JSON-RPC error of any
    code would be a delivered reply, and would turn the hard case (the caller
    has no evidence either way about whether the target ran) into the easy one
    (the caller knows it failed). That is `RESPONSE_LOST`'s entire reason to
    exist, and it also covers the injected `TIMEOUT` and `CONNECTION_REFUSED`,
    which `FaultProxy._reject` marks undelivered for the same reason.

    Read off `delivered` rather than branched per fault kind, so the split has
    one definition and the proxy cannot disagree with the injector about which
    faults arrived.
    """
    if not response.delivered:
        return None

    if response.ok:
        return {"content": [{"type": "text", "text": _as_text(response.output)}]}

    return {"content": [{"type": "text", "text": _error_text(response)}], "isError": True}


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
    ) -> None:
        self.target = target
        self.record = record
        self.task_id = task_id
        self.proxy = FaultProxy(
            target,
            FaultConfig(rates={}, schedule=schedule, task_id=task_id),
            ordinals=record.ordinals(),
        )
        self._recorded = 0

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

        for invocation in self.proxy.invocations[before:]:
            self.record.write(
                invocation,
                idempotency_key=(
                    arguments.get(IDEMPOTENCY_ARG)
                    if isinstance(arguments.get(IDEMPOTENCY_ARG), str)
                    else None
                ),
                received_at=received_at,
            )
            self._recorded += 1

        payload = response_to_wire(response)
        if payload is None:
            # The reply is lost on purpose. Nothing is written for this id, and
            # the loop goes straight back to reading -- the session stays up.
            logger.info("dropping the reply to %s (%s)", request_id, response.error)
            return None
        return _jsonrpc_result(request_id, payload)


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
    )
    logger.info("proxy up: upstream=%s record=%s task=%s", upstream, record_path, task_id)

    try:
        while True:
            line = await asyncio.to_thread(source.readline)
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
    finally:
        record.close()
        await target.teardown()


__all__ = [
    "IDEMPOTENCY_ARG",
    "ProxyServer",
    "RecordWriter",
    "invocation_from_row",
    "invocation_rows",
    "read_record",
    "read_schedule",
    "replay",
    "response_to_wire",
    "serve",
    "tool_payload",
    "write_schedule",
]
