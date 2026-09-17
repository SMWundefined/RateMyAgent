#!/usr/bin/env python3
"""Blind, with no read timeout. For the hang test and nothing else.

A dropped reply leaves this waiting forever. That is not a harness artifact: the
MCP Python SDK exposes a per-request read timeout on `ClientSession` and whether
a given host sets one is unverified, so an agent that sets none is a shape a
real scan will meet. The right output for it is `abandoned` plus a finding --
the scan's own task deadline stops it -- and not a fix.

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
    try:
        reply = client.request("tools/call", {"name": task["tool"], "arguments": arguments})
        ok, text, _ = call_result(reply)
        report({"ok": ok, "result": text, "attempts": 1})
        return 0 if ok else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
