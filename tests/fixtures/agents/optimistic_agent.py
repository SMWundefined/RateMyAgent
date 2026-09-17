#!/usr/bin/env python3
"""Blind, and then it says it worked anyway.

The claimed-vs-actual gap produced by the **caller** rather than by the server.
`event_twin_mcp_server.py --swallow-every N` produces the same gap from the
server's side: acknowledged, never applied. Two sources, one symptom, and a
measurement that cannot tell them apart is not measuring either -- which is why
both fixtures exist.

Identical to `blind_agent` up to the last three lines. It exhausts its retries
and reports success, which is what an agent does when its error handling
swallows the failure and returns a default -- the broad `except` this project
names as the number one AI-code anti-pattern, seen from outside.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_base import (  # noqa: E402
    DEFAULT_READ_TIMEOUT_S,
    call_result,
    connect,
    load_task,
    parse_args,
    report,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    client = connect(args.mcp_config, read_timeout_s=DEFAULT_READ_TIMEOUT_S)

    arguments = dict(task["arguments"])
    attempts = 0
    try:
        for _ in range(args.max_retries + 1):
            attempts += 1
            try:
                reply = client.request(
                    "tools/call", {"name": task["tool"], "arguments": arguments}
                )
            except TimeoutError:
                client.notify("notifications/cancelled", {"reason": "read timeout"})
                continue
            except ConnectionError:
                break

            ok, text, _ = call_result(reply)
            if ok:
                report({"ok": True, "result": text, "attempts": attempts})
                return 0

        # Every attempt failed, and it reports success. The record and the
        # server's state both say otherwise; nothing the *agent* said does.
        report({"ok": True, "result": "done", "attempts": attempts,
                "note": "claimed after exhausting retries"})
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
