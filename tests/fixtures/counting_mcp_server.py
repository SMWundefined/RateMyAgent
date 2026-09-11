#!/usr/bin/env python3
"""A server that counts its own executions. The oracle no metric can be.

**This fixture is the deliberate inversion of every other test in the suite.**

Everywhere else, the scanner's own output is the thing under test: a caveat
fired, a score was withheld, a rate came out at 0.79. That works because the
claim is about something the scanner observed.

`duplicate_mutations` is not that kind of claim. It asserts **the target did
something the caller could not see** -- ran a mutation whose reply was lost, and
ran it again on retry. Asserting that from the scanner's own metrics would be
circular: the metric would be confirming itself, and if the whole mechanism were
wrong it would be wrong in both places at once.

So this server keeps the ledger:

- `append(item)` is mutating. It records every execution, unconditionally, before
  it answers.
- `executions()` is read-only and reports the ledger.

A test asserts `executions() == 2 and duplicate_mutations == 1`. If the fault
plumbing is broken the first number falls and the test fails, whatever the
metric says.

    --slow N   seconds each append takes, for tests that need the reply to be
               in flight when something happens to it
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

APPEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"item": {"type": "string"}},
    "required": ["item"],
    "additionalProperties": False,
}
READ_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: Every execution of `append`, in order. The ledger the scanner cannot see and
#: cannot fake, which is the entire reason this file exists.
LEDGER: list[str] = []


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _text(request_id: Any, text: str) -> dict[str, Any]:
    return _result(request_id, {"content": [{"type": "text", "text": text}]})


def handle(message: dict[str, Any], slow: float) -> dict[str, Any] | None:
    request_id, method = message.get("id"), message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "counting-stub", "version": "1.0"},
        })

    if method == "tools/list":
        return _result(request_id, {"tools": [
            {
                "name": "append",
                "description": "Append an item. Not idempotent, on purpose.",
                "inputSchema": APPEND_SCHEMA,
                # Declared so the scanner's mutating-tool guard sees it and
                # refuses without --allow-mutating, which is the behaviour the
                # opt-in fault is gated on.
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            },
            {
                "name": "executions",
                "description": "How many times append has run.",
                "inputSchema": READ_SCHEMA,
                "annotations": {"readOnlyHint": True},
            },
        ]})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "append":
            # Recorded *before* the reply is written, because the whole point is
            # that the work happens even when the answer never arrives.
            LEDGER.append(str(arguments.get("item", "")))
            if slow:
                time.sleep(slow)
            return _text(request_id, f"appended; ledger now has {len(LEDGER)}")

        if name == "executions":
            return _result(request_id, {
                "content": [{"type": "text", "text": str(len(LEDGER))}],
                "structuredContent": {"executions": len(LEDGER)},
            })

        return _result(request_id, {
            "content": [{"type": "text", "text": f"unknown tool {name!r}"}],
            "isError": True,
        })

    return _result(request_id, {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow", type=float, default=0.0)
    args = parser.parse_args()

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
        response = handle(message, args.slow)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
