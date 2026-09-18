#!/usr/bin/env python3
"""Blind, with no read timeout. The shape a real agent actually turned out to be.

A dropped reply leaves this waiting forever. That is not a harness artifact and
it is no longer hypothetical: the Phase D spike pointed the scan at Claude Code
and measured 234 seconds on one dropped reply, with no retry, no cancellation
and no return. An agent that sets no read timeout is not an edge case, it is the
first real agent this project scanned.

Under `RESPONSE_LOST` the right output for that is `abandoned` plus a finding --
the scan's own task deadline stops it -- and not a fix.

Under `RESPONSE_LOST_THEN_CLOSED` there is something to measure, because the
connection goes away and even a client with no deadline notices that. This
fixture reconnects and retries once, blind and without an idempotency key, which
is what turns the hang into a duplicate the upstream's ledger can confirm.

It exists because the untested branch is the one a real agent hits first.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_base import call_result, connect, load_task, parse_args, report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    # `read_timeout_s=None`: the read blocks until a reply arrives, and when the
    # proxy drops one, none ever does.
    client = connect(args.mcp_config, read_timeout_s=None)

    arguments = dict(task["arguments"])
    attempts = 1
    try:
        try:
            reply = client.request(
                "tools/call", {"name": task["tool"], "arguments": arguments}
            )
        except ConnectionError:
            # **The one thing a client with no read timeout can still notice.**
            # Waiting forever is what it does when a reply is merely dropped;
            # when the connection itself goes away there is an event, and any
            # real client reacts to it. So it reconnects and sends the call
            # again -- blind, no idempotency key, exactly like `blind_agent`.
            #
            # This is what makes `RESPONSE_LOST_THEN_CLOSED` measurable against
            # a client that `RESPONSE_LOST` can only hang: there is now a
            # decision to observe, and on an appending upstream it applies the
            # mutation twice.
            client.close()
            client = connect(args.mcp_config, read_timeout_s=None)
            attempts += 1
            reply = client.request(
                "tools/call", {"name": task["tool"], "arguments": arguments}
            )
        ok, text, _ = call_result(reply)
        report({"ok": ok, "result": text, "attempts": attempts})
        return 0 if ok else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
