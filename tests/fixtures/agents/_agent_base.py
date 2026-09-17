#!/usr/bin/env python3
"""What the three scripted agents share, and nothing about how they retry.

**Nothing here imports `ratemyagent`.** These fixtures stand in for an agent
somebody else wrote: an agent that imported the scanner would be testing the
scanner's idea of itself, and the one thing Phase C has to establish is that the
MCP boundary is the only thing between them.

So this file is a small JSON-RPC client over a subprocess, and the differences
that matter -- whether a retry reuses an idempotency key, whether it waits, what
it claims at the end -- live in the agents themselves. Each is a `retry` policy
and a `claim` rule, a dozen lines apiece, which is what makes reading two of
them side by side a useful thing to do.

**The proxy is launched from the config, exactly as the config says.** The
command, the args and the `env` block are taken verbatim. An agent that
reconstructed the launch would pass whether or not the scan wrote the block,
which is the one thing the block exists to prove.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

#: Read timeout on a reply, in seconds. Every agent but `no_timeout_agent` sets
#: one. A client with none hangs forever when a reply is dropped -- which is a
#: real production failure mode, not a harness artifact, and gets its own
#: fixture rather than a comment.
DEFAULT_READ_TIMEOUT_S = 3.0


class ProxyClient:
    """A JSON-RPC client over a server this process launched."""

    def __init__(self, entry: dict[str, Any], *, read_timeout_s: float | None) -> None:
        self.read_timeout_s = read_timeout_s
        self._id = 0
        # `{**os.environ, **entry_env}` because this is an ordinary subprocess
        # launch and a client that dropped PATH could not start python. The
        # entry's own block wins, and it is the only place the RMA_* variables
        # come from: they are not in this process's environment.
        env = {**os.environ, **(entry.get("env") or {})}
        self.process = subprocess.Popen(
            [entry["command"], *entry.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
            text=True,
            bufsize=1,
        )
        # **One reader for the life of the connection.** A thread per read,
        # abandoned on timeout, stays blocked on the pipe and takes the *next*
        # line -- the reply to the retry -- so every dropped reply made the
        # following successful call look like a second timeout, and the agent
        # sent a third. One reader and a queue means a late line is simply
        # there for whoever asks next, and the id check below discards it.
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put("")  # EOF

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and wait for its reply.

        Raises `TimeoutError` when nothing comes back inside the read timeout,
        which is what a dropped reply looks like from here -- and, with
        `read_timeout_s=None`, blocks forever, which is what it looks like to a
        client that set none.
        """
        self._id += 1
        request_id = self._id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                     "params": params or {}})
        return self._read(request_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _read(self, request_id: int) -> dict[str, Any]:
        deadline = None if self.read_timeout_s is None else time.monotonic() + self.read_timeout_s
        while True:
            line = self._readline(deadline)
            if line is None:
                raise TimeoutError(f"no reply to request {request_id}")
            if not line:
                raise ConnectionError("the proxy closed its output")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message

    def _readline(self, deadline: float | None) -> str | None:
        """One line, or None if the deadline passes first.

        Read off the pump's queue rather than the pipe, because a pipe has no
        timeout of its own and this fixture must not depend on select()
        semantics that differ per platform. An empty string is EOF.
        """
        if deadline is None:
            return self._lines.get()
        try:
            return self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return None

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def connect(config_path: str, *, read_timeout_s: float | None) -> ProxyClient:
    """Launch the server the config names and shake hands with it."""
    config = json.loads(open(config_path, encoding="utf-8").read())
    servers = config["mcpServers"]
    entry = servers[next(iter(servers))]
    client = ProxyClient(entry, read_timeout_s=read_timeout_s)
    client.request("initialize", {"protocolVersion": "2025-06-18",
                                  "capabilities": {},
                                  "clientInfo": {"name": "rma-fixture", "version": "1.0"}})
    client.notify("notifications/initialized")
    return client


def load_task(tasks_path: str, task_id: str) -> dict[str, Any]:
    body = json.loads(open(tasks_path, encoding="utf-8").read())
    tasks = body["tasks"] if isinstance(body, dict) else body
    for task in tasks:
        if str(task["id"]) == str(task_id):
            return task
    raise SystemExit(f"no task {task_id!r} in {tasks_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    # Both channels, because a real agent reads one or the other and a fixture
    # should not get to pick whichever the scan happened to set.
    parser.add_argument("--mcp-config", default=os.environ.get("RMA_MCP_CONFIG"))
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args(argv)
    if not args.mcp_config:
        raise SystemExit("no --mcp-config and no RMA_MCP_CONFIG")
    return args


def call_result(reply: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """(ok, text, error body) for one `tools/call` reply.

    A tool error arrives as a normal result with `isError`, which is how MCP
    reports one, and the body is parsed as JSON when it is JSON -- a relayed 429
    carries its `retry_after_s` in there, because stdio has no headers to put it
    in.
    """
    if "error" in reply:
        return False, str(reply["error"]), None
    result = reply.get("result") or {}
    blocks = result.get("content") or []
    text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    if not result.get("isError"):
        return True, text, None
    body = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            body = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
    except json.JSONDecodeError:
        pass
    return False, text, body


def report(claim: dict[str, Any]) -> None:
    """The agent's own account of what happened, on stdout, as one JSON line."""
    sys.stdout.write(json.dumps(claim) + "\n")
    sys.stdout.flush()
