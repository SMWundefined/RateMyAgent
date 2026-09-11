#!/usr/bin/env python3
"""A server whose failure clears if you wait. The case seeded injection cannot make.

**This fixture is the deliverable of the backoff work, not the backoff.**

`FaultProxy._choose_fault` seeds on `(trajectory, attempt)`, so a retry draws
the same fault whether it happens immediately or ten seconds later. That is
correct -- it is what makes recovery observable at all -- and it means **no
injected fault can ever reward waiting.** Every rate-limit in the regression set
clears or does not clear independently of the clock.

So a scanner could implement backoff perfectly and every test in the corpus
would pass identically with it removed. Fifth instance of the regression set
being structurally unable to reach a new capability (PROGRESS section 8b).

This server is the missing case: it refuses with a 429-shaped error until
`--recover-after` seconds have passed since its first call, then answers
normally. A caller that waits recovers; a caller that hammers does not, and the
difference is measurable.

    --recover-after N   seconds before calls start succeeding (default 2.0)
    --retry-after N     value to advertise in the error text (default 1.0)
    --no-hint           omit the Retry-After hint entirely, which is the
                        firecrawl case: an upstream 429 relayed as a tool error
                        with no header anywhere
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}
TOOLS = ["get_thing"]


def handle(
    message: dict[str, Any], started: float, recover_after: float,
    retry_after: float, hint: bool,
) -> dict[str, Any] | None:
    request_id, method = message.get("id"), message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "rate-limited-stub", "version": "1.0"},
        })

    if method == "tools/list":
        return _result(request_id, {
            "tools": [
                {"name": n, "description": f"{n} (stub)", "inputSchema": SCHEMA}
                for n in TOOLS
            ]
        })

    if method == "tools/call":
        if time.monotonic() - started < recover_after:
            # A tool *result* carrying an error, not a JSON-RPC error: this is
            # how a server relays an upstream 429, and it is why the scanner
            # classifies it by matching the message text rather than a header.
            suffix = f" Retry-After: {retry_after:g}s." if hint else ""
            return _result(request_id, {
                "content": [{
                    "type": "text",
                    "text": f"429 rate limit exceeded.{suffix}",
                }],
                "isError": True,
            })
        return _result(request_id, {
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        })

    return {
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recover-after", type=float, default=2.0)
    parser.add_argument("--retry-after", type=float, default=1.0)
    parser.add_argument("--no-hint", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
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
        response = handle(
            message, started, args.recover_after, args.retry_after,
            not args.no_hint,
        )
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
