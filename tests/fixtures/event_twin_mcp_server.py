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
    --state PATH   where effects are written. append: one JSON line per event;
                   put: one JSON object keyed by id. Optional.
    --calls PATH   one JSON line per call the handler received, with whether
                   state changed. Optional.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

TOOL = "event"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "payload": {"type": "string"}},
    "required": ["id", "payload"],
}

#: The effects, in memory. Mirrored to --state when given.
EVENTS: list[dict[str, str]] = []
STORE: dict[str, str] = {}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _text(request_id: Any, text: str, *, error: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        body["isError"] = True
    return _result(request_id, body)


def _apply(mode: str, event_id: str, payload: str, state: str | None) -> bool:
    """Perform the call's effect. Returns whether state changed."""
    if mode == "append":
        EVENTS.append({"id": event_id, "payload": payload})
        if state:
            with open(state, "a") as f:
                f.write(json.dumps({"id": event_id, "payload": payload}) + "\n")
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
        return _result(request_id, {"tools": [{
            "name": TOOL,
            # Identical in both modes. A description that said which twin this is
            # would be a difference the scanner could, in principle, see.
            "description": "Record an event.",
            "inputSchema": SCHEMA,
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        }]})

    if method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}
        if params.get("name") != TOOL:
            return _text(request_id, f"unknown tool {params.get('name')!r}", error=True)
        event_id, payload = arguments.get("id"), arguments.get("payload")
        if not isinstance(event_id, str) or not isinstance(payload, str):
            return _text(request_id, "id and payload must be strings", error=True)

        changed = _apply(opts.mode, event_id, payload, opts.state)
        if opts.calls:
            with open(opts.calls, "a") as f:
                f.write(json.dumps({
                    "tool": TOOL, "args": {"id": event_id, "payload": payload},
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
    opts = parser.parse_args()

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
