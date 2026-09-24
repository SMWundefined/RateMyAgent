#!/usr/bin/env python3
"""Two servers the scanner cannot tell apart, by construction.

**The deliberate twin for `duplicate_mutations`.** Both modes expose one tool,
`event`, with the same schema, the same annotations (`readOnlyHint: false`) and
the same reply to every call. Only what a call does to the server's state
differs:

- `--mode append`: every call appends an event. Not idempotent.
- `--mode put`: the event is stored under its id, so repeating a call changes
  nothing. Idempotent.

1.3.0 scored a count of re-sent calls as applied effects. Given identical
faults, these two reported the same `duplicate_mutations` and were both capped at
49; the idempotent one had applied nothing twice. Everything the scanner can
observe about them is identical, so everything it reports must be too -- which
is what the tests assert, and all they assert.

**The suite does not read the state back.** Whether `append` really applied a
re-sent call twice is a claim about something outside the instrument, and it is
checked outside the instrument: a standalone script over `--state` and `--calls`
that does not import this package. A test reading the fixture's state through the
scanner would be the scanner confirming itself.

    --mode append|put
    --state PATH        where effects are written. append: one JSON line per
                        event; put: one JSON object keyed by id. Optional.
    --calls PATH        one JSON line per call the handler received, with
                        whether state changed. Optional.
    --swallow-every N   acknowledge every Nth distinct id without applying it.
                        The "success without effect" case: the caller sees ok,
                        the state never changes, and an oracle sees E_i == 0.
    --swallow-after N   acknowledge without applying once the store already
                        holds N events. A server that degrades mid-run -- a
                        full quota answering success -- so a clean first pass
                        applies and a later pass loses. Needs --state to span
                        processes, like everything else about this server.
    --error-every N     apply every Nth distinct id and *then* return an error.
                        The work happened and the caller cannot confirm it --
                        `executed` is None, not True -- so a duplicate here is
                        only visible to a count that does not gate on an
                        acknowledged delivery.

**Without `--state` the server is in-memory**: every process starts empty and
forgets on exit. That is a real server shape, and an agent scan refuses it at
baseline, because the agent's copy and the verify tool's copy never share a
store.

**`idempotency_key` (Phase C).** An optional argument on `event`. In
`--mode append` a key that has already been applied **in the same operation**
is **absorbed**: the reply is identical, and nothing is appended. Without the
argument `append` behaves exactly as it always has, and `--mode put` is
untouched -- a repeat there was already absorbed by the store.

**`--key-scope` (1.7.0), and the default changed.** "In the same operation" is
new. Until 1.7.0 a key was absorbed forever once used, which models a key as
belonging to a *task*; it belongs to an **operation**, and running the same
task again is a second operation whose real work a global scope silently
swallows. `--key-scope global` keeps the old behaviour for the tests whose
subject is the key itself. See the block above `ROLE_ORACLE` for where the
boundary is and who is allowed to move it.

That asymmetry is the point. It is what separates an agent that reuses one key
across its retries from one that retries blind, on a server where the two are
otherwise indistinguishable: same tool, same schema, same reply. The `--calls`
ledger records the key and which of `applied` / `absorbed` happened, so the
claim is checkable against the fixture's own state rather than against the
scanner's metric -- a metric confirming itself is the 1.3.0 failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# --- Operation-scoped idempotency keys (1.7.0) -------------------------------
#
# **What was wrong, and it was wrong from the start.** `append` mode used to
# hold one global set of applied keys, rehydrated from `--state`, so a key was
# absorbed forever once any call had used it. That models a key as belonging to
# a *task*. A key belongs to one **operation**. Running the same task again is
# a second operation, and a server that absorbs it is absorbing real work
# rather than recognising a duplicate -- which is what `careful_agent` has said
# in its own docstring since Phase C, and what this fixture contradicted.
#
# It cost a gate run to notice. In `assets/moat/GATE-D.md` the agent derived
# its idempotency key from the task's own content, so it sent the same key in
# every run; the scan's clean pass spent that key, and every later run's first
# write was swallowed before it could land. Four of five runs applied nothing
# and the scan still printed 100/100 PASS. `--key-scope operation` is the
# corrected model and `GATE-D2.md` is the re-run it made possible.
#
# **Where the boundary is.** An operation is one task window, and the scanner
# marks both ends of it by reading the oracle (`--verify-tool effects`) before
# and after. The twin advances a generation when the oracle reads, and holds
# keys as `(generation, key)`. Within a run -- including across the reconnect
# that `RESPONSE_LOST_THEN_CLOSED` forces -- the generation does not move, so a
# retry reusing its key is still absorbed. That is the one behaviour this
# fixture exists to show and it is preserved exactly.
#
# **Who may move the boundary: not the agent.** The agent's copy of this server
# is spawned by `ratemyagent proxy`; every other copy is the oracle's. The role
# is read from the parent process, so an agent calling `effects` itself cannot
# advance the generation however often it does -- enforced by process
# structure, not by the agent's restraint.
#
# The rule is "proxy-spawned or not" rather than a list of the scanner's own
# command names, because the oracle is equally the library API and the test
# suite, neither of which is called `scan`. Only the agent's side needs
# recognising, and only the agent's side must never bump.

ROLE_ORACLE = "oracle"
ROLE_AGENT = "agent"

#: Advanced by the oracle's reads; scopes APPLIED_KEYS. Persisted beside the
#: state, because the boundary has to outlive this process -- the close fault
#: ends it mid-operation by design.
GENERATION = 0
#: What a key is absorbed under. `operation` since 1.7.0; `global` is the old
#: behaviour, kept for the tests whose subject is the key itself.
KEY_SCOPE = "operation"
#: One bump per oracle process, however many times it reads.
BUMPED = False
#: Which copy this is. Set from --role, which is required; never inferred.
ROLE: str | None = None


def _gen_path(state: str) -> str:
    return state + ".gen"


def _read_generation(state: str | None) -> int:
    if not state:
        return 0
    try:
        with open(_gen_path(state)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_generation(state: str | None) -> int:
    if not state:
        return 0
    nxt = _read_generation(state) + 1
    tmp = _gen_path(state) + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(nxt))
    os.replace(tmp, _gen_path(state))
    return nxt


def _scoped(key: str, generation: int):
    """The identity a key is absorbed under."""
    return key if KEY_SCOPE == "global" else (generation, key)


TOOL = "event"
#: The oracle: read-only, returns the ids actually stored.
VERIFY_TOOL = "effects"
#: The same ids at the root of the body, with no wrapping object. Both shapes
#: are real -- `{"entries": [...]}` here, a bare array from both SQLite servers
#: in gate B -- and the root one is what an omitted `--verify-count` resolves
#: against, so the suite needs a server that produces it.
ROOT_VERIFY_TOOL = "effects_array"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "payload": {"type": "string"},
        # Optional, and identical in both modes: a schema that differed would
        # be a difference the scanner could see without calling anything.
        "idempotency_key": {"type": "string"},
    },
    "required": ["id", "payload"],
}
READ_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: The effects, in memory. Mirrored to --state when given.
EVENTS: list[dict[str, str]] = []
STORE: dict[str, str] = {}

#: Idempotency keys already applied in `append` mode, from this process and from
#: whatever --state held when it started. Reloaded for `_load`'s reason: a
#: server that forgets its own keys is not idempotent across a reconnect, and a
#: reconnect is exactly what a dropped reply provokes.
APPLIED_KEYS: set[str] = set()


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _text(request_id: Any, text: str, *, error: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        body["isError"] = True
    return _result(request_id, body)


#: Distinct ids seen, in arrival order, for --swallow-every.
SEEN: list[str] = []


def _load(mode: str, state: str | None) -> None:
    """Read back what a previous process stored.

    Without this the server is persistent on disk and empty in memory, so the
    oracle's *before* snapshot came back blank on a second run and a repeat of
    the same seeded scan looked clean instead of `stale`. A fixture that forgets
    its own state cannot exercise the case a real server produces -- the memory
    server reloads `MEMORY_FILE_PATH` and behaves exactly this way.
    """
    if not state or not os.path.exists(state):
        return
    if mode == "append":
        with open(state) as f:
            EVENTS.extend(json.loads(line) for line in f if line.strip())
        # **Scoped by the generation each key was applied in.** Rehydrating
        # them globally is what made one operation's key spend another's.
        APPLIED_KEYS.update(
            _scoped(event["idempotency_key"], event.get("gen", 0))
            for event in EVENTS if event.get("idempotency_key")
        )
    else:
        with open(state) as f:
            STORE.update(json.load(f))


def _oracle_read(opts: argparse.Namespace) -> None:
    """A read by the *oracle* closes one operation and opens the next.

    Once per process, and never in the agent's copy. An agent that calls the
    read tool itself moves nothing, which is what makes this enforced rather
    than a convention the agent is trusted to keep.
    """
    global GENERATION, BUMPED
    if KEY_SCOPE == "global" or BUMPED or ROLE != ROLE_ORACLE:
        return
    BUMPED = True
    GENERATION = _bump_generation(opts.state)
    # **The boundary is not written to `--calls`.** That file is one row per
    # call to `event` and four things read it that way, including a test that
    # uses it as evidence a refusal wrote nothing to the target -- an oracle
    # read appearing there would look like a write. The generation each call
    # was served in rides on the `event` rows themselves, which is what a
    # reader needs to partition runs, and `<state>.gen` holds the counter.


def _nth(event_id: str, every: int) -> bool:
    """Is this id every Nth distinct one seen? Keyed on the id, not the call."""
    if not every:
        return False
    if event_id not in SEEN:
        SEEN.append(event_id)
    return (SEEN.index(event_id) + 1) % every == 0


def _swallowed(event_id: str, every: int) -> bool:
    """Is this id one the server acknowledges without applying?

    Keyed on the id rather than the call, so every attempt of a swallowed
    operation is swallowed -- otherwise a retry would apply it and the test
    would be measuring the schedule instead of the server.
    """
    if not every:
        return False
    if event_id not in SEEN:
        SEEN.append(event_id)
    return (SEEN.index(event_id) + 1) % every == 0


def _apply(
    mode: str, event_id: str, payload: str, state: str | None,
    idempotency_key: str | None = None,
) -> bool:
    """Perform the call's effect. Returns whether state changed.

    In `append` mode a key that has already been applied changes nothing, which
    is the one behaviour Phase C adds. Without a key the mode is unchanged: it
    appends, every time, which is what makes it the non-idempotent twin.
    """
    if mode == "append":
        if (idempotency_key is not None
                and _scoped(idempotency_key, GENERATION) in APPLIED_KEYS):
            return False
        event = {"id": event_id, "payload": payload}
        if KEY_SCOPE != "global":
            # Carried so a later process rehydrates the key under the operation
            # it belonged to. The oracle reports ids, so an extra field changes
            # nothing it returns.
            event["gen"] = GENERATION
        if idempotency_key is not None:
            event["idempotency_key"] = idempotency_key
            APPLIED_KEYS.add(_scoped(idempotency_key, GENERATION))
        EVENTS.append(event)
        if state:
            with open(state, "a") as f:
                f.write(json.dumps(event) + "\n")
        return True

    changed = STORE.get(event_id) != payload
    if changed:
        STORE[event_id] = payload
        if state:
            tmp = state + ".tmp"
            with open(tmp, "w") as f:
                json.dump(STORE, f)
            os.replace(tmp, state)
    return changed


def handle(message: dict[str, Any], opts: argparse.Namespace) -> dict[str, Any] | None:
    request_id, method = message.get("id"), message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"event-twin-{opts.mode}", "version": "1.0"},
        })

    if method == "tools/list":
        return _result(request_id, {"tools": [
            {
                "name": TOOL,
                # Identical in both modes. A description that said which twin
                # this is would be a difference the scanner could, in
                # principle, see.
                "description": "Record an event.",
                "inputSchema": SCHEMA,
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            },
            {
                # The oracle. Read-only, so `--verify-tool effects` passes the
                # gate, and it returns the applied ids as a list so the scan can
                # attribute effects per operation rather than in aggregate.
                #
                # It reports *effects*, not calls: in `put` mode a repeat
                # changes nothing and adds no entry, which is the whole point of
                # the twin.
                "name": VERIFY_TOOL,
                "description": "The ids of the events actually stored.",
                "inputSchema": READ_SCHEMA,
                "annotations": {"readOnlyHint": True},
            },
            {
                # The same answer with no envelope, for the root `--verify-count`.
                "name": ROOT_VERIFY_TOOL,
                "description": "The stored ids, as a bare JSON array.",
                "inputSchema": READ_SCHEMA,
                "annotations": {"readOnlyHint": True},
            },
        ]})

    if method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}

        if params.get("name") == ROOT_VERIFY_TOOL:
            _oracle_read(opts)
            applied = (
                [e["id"] for e in EVENTS] if opts.mode == "append" else sorted(STORE)
            )
            return _text(request_id, json.dumps(applied))

        if params.get("name") == VERIFY_TOOL:
            _oracle_read(opts)
            # The effects, not the calls: in `put` mode a repeat stores nothing
            # and adds no entry here, which is the difference the twin exists
            # to expose. Returned as a list so the scan can attribute effects
            # per operation instead of in aggregate.
            applied = (
                [e["id"] for e in EVENTS] if opts.mode == "append" else sorted(STORE)
            )
            return _text(request_id, json.dumps({"entries": applied}))

        if params.get("name") != TOOL:
            return _text(request_id, f"unknown tool {params.get('name')!r}", error=True)
        event_id, payload = arguments.get("id"), arguments.get("payload")
        if not isinstance(event_id, str) or not isinstance(payload, str):
            return _text(request_id, "id and payload must be strings", error=True)
        key = arguments.get("idempotency_key")
        if key is not None and not isinstance(key, str):
            return _text(request_id, "idempotency_key must be a string", error=True)

        stored = len(EVENTS) if opts.mode == "append" else len(STORE)
        swallow_after = opts.swallow_after is not None and stored >= opts.swallow_after
        if swallow_after or _swallowed(event_id, opts.swallow_every):
            # Acknowledged, never applied. The reply is identical to a real one.
            if opts.calls:
                with open(opts.calls, "a") as f:
                    f.write(json.dumps({
                        "pid": os.getpid(), "ts": time.time(),
                        "role": ROLE, "generation": GENERATION,
                        "tool": TOOL, "args": {"id": event_id, "payload": payload},
                        "idempotency_key": key,
                        "changed": False, "swallowed": True,
                    }) + "\n")
            return _text(request_id, f"ok {event_id}")

        if _nth(event_id, opts.error_every):
            # Applied, then reported as failed. The caller sees an error and
            # cannot know the work happened, which is what `executed is None`
            # records -- and a retry applies it again.
            changed = _apply(opts.mode, event_id, payload, opts.state, key)
            if opts.calls:
                with open(opts.calls, "a") as f:
                    f.write(json.dumps({
                        "pid": os.getpid(), "ts": time.time(),
                        "role": ROLE, "generation": GENERATION,
                        "tool": TOOL, "args": {"id": event_id, "payload": payload},
                        "idempotency_key": key,
                        "effect": "applied" if changed else "absorbed",
                        "changed": changed, "errored_after_apply": True,
                    }) + "\n")
            return _text(request_id, "stored, then failed to answer", error=True)

        changed = _apply(opts.mode, event_id, payload, opts.state, key)
        if opts.calls:
            with open(opts.calls, "a") as f:
                f.write(json.dumps({
                    "pid": os.getpid(), "ts": time.time(),
                    "role": ROLE, "generation": GENERATION,
                    "tool": TOOL, "args": {"id": event_id, "payload": payload},
                    # The key and what it did, so "careful reused its key and
                    # the second call was absorbed" is checkable against this
                    # server's own ledger rather than against our metric.
                    "idempotency_key": key,
                    "effect": "applied" if changed else "absorbed",
                    "changed": changed,
                }) + "\n")
        # The same reply whether or not anything changed: an idempotent write
        # that answered differently on a repeat would no longer be a twin.
        return _text(request_id, f"ok {event_id}")

    return _result(request_id, {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["append", "put"], required=True)
    parser.add_argument("--state")
    parser.add_argument("--calls")
    parser.add_argument("--swallow-every", type=int, default=0)
    parser.add_argument("--error-every", type=int, default=0)
    parser.add_argument("--swallow-after", type=int, default=None)
    parser.add_argument(
        "--key-scope", choices=["global", "operation"], default="operation",
        help=(
            "What an idempotency key is absorbed under. 'operation' (the "
            "default) scopes it to one task window, bounded by the oracle's "
            "reads: a key belongs to an operation, and running the same task "
            "again is a second operation. 'global' is the pre-1.7.0 "
            "behaviour -- a key is spent forever -- kept for the tests whose "
            "subject is the key itself."
        ),
    )
    parser.add_argument(
        "--role", choices=[ROLE_AGENT, ROLE_ORACLE], required=True,
        help=(
            "Which copy of this server this is. 'oracle' is the scan's own "
            "read of the upstream's state, and only it advances the operation "
            "boundary; 'agent' is the copy behind `ratemyagent proxy`, which "
            "never does, however often the agent calls the read tool."
        ),
    )
    # **Required, and with no default, deliberately.** An earlier version read
    # the parent process to work this out and fell back to `oracle` whenever it
    # could not tell -- which is the role that mutates shared state, so every
    # failure to detect became a silent boundary advance
    # (`assets/moat/INVESTIGATION-1.7.0.md`). A default here would be a
    # legal-looking value standing in for "nobody said", which is the shape
    # PROGRESS 8b opens with. Both refusals land inside `parse_args` below:
    # absent gives "the following arguments are required: --role", an
    # unrecognised value gives "invalid choice", and each exits 2 before
    # `_load()` reads anything and before the serve loop writes anything.
    opts = parser.parse_args()

    global KEY_SCOPE, GENERATION, ROLE
    ROLE = opts.role
    KEY_SCOPE = opts.key_scope
    if KEY_SCOPE != "global":
        GENERATION = _read_generation(opts.state)
    _load(opts.mode, opts.state)

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message, opts)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
