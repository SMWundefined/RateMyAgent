#!/usr/bin/env python3
"""The agent that retries without thinking about it.

No idempotency key, no backoff, no attention to what the server asked for. It is
not a strawman: it is what retry logic looks like when it is added to make a
flaky call pass, which is most of the retry logic there is.

It differs from `careful_agent` in exactly three lines -- the key, the wait, and
the hint -- and in nothing else, so a difference between their scans is
attributable to those and nothing else.

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

    # No `idempotency_key`. The arguments are exactly the task's, so a retry is
    # a second identical write and the server has nothing to absorb it with.
    arguments = dict(task["arguments"])

    attempts = 0
    last = "no attempt was made"
    try:
        for _ in range(args.max_retries + 1):
            attempts += 1
            try:
                reply = client.request(
                    "tools/call", {"name": task["tool"], "arguments": arguments}
                )
            except TimeoutError:
                last = "no reply"
                client.notify("notifications/cancelled", {"reason": "read timeout"})
                continue  # straight back round: no wait, hint or no hint
            except ConnectionError as exc:
                last = str(exc)
                break

            ok, text, _ = call_result(reply)
            if ok:
                report({"ok": True, "result": text, "attempts": attempts})
                return 0
            last = text

        report({"ok": False, "error": last, "attempts": attempts})
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
