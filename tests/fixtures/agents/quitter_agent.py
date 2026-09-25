#!/usr/bin/env python3
"""The agent that connects, makes no tool call, and reports failure.

Gate BD's replication on 2026-09-24, replicate 2, reduced to a fixture: in the
chaos pass the agent completed the MCP handshake -- so the proxy wrote a
`notifications/initialized` row to the record -- and then made no tool call and
reported failure. The scan refused, correctly, and told the user the record was
"empty or missing" and to check the config's `env` block, both of which the
record itself proved false. `tests/test_unrecorded_refusal.py` drives this to
pin what the refusal says instead.

`--quit-in PASS` (default `chaos`): in that pass it connects and quits; in any
other it makes the task's one call, blind, like `blind_agent`. The pass is read
off the record path in its own MCP config (`record-<pass>-<task>.jsonl`), the
only place an agent can learn it.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import json
import os
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


def _pass_of(config_path: str) -> str:
    config = json.loads(open(config_path, encoding="utf-8").read())
    entry = next(iter(config["mcpServers"].values()))
    record = os.path.basename((entry.get("env") or {}).get("RMA_PROXY_RECORD", ""))
    parts = record.split("-")
    return parts[1] if len(parts) > 2 else ""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quit_in = "chaos"
    if "--quit-in" in argv:
        index = argv.index("--quit-in")
        quit_in = argv[index + 1]
        del argv[index:index + 2]
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    client = connect(args.mcp_config, read_timeout_s=DEFAULT_READ_TIMEOUT_S)
    try:
        if _pass_of(args.mcp_config) == quit_in:
            # Connected, handshake done, and nothing else.
            report({"ok": False, "error": "connected, made no tool call",
                    "attempts": 0})
            return 1
        reply = client.request(
            "tools/call", {"name": task["tool"], "arguments": dict(task["arguments"])}
        )
        ok, text, _ = call_result(reply)
        report({"ok": ok, "result": text, "attempts": 1} if ok
               else {"ok": False, "error": text, "attempts": 1})
        return 0 if ok else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
