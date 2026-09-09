#!/usr/bin/env python3
"""A minimal MCP server over stdio, speaking JSON-RPC directly.

Deliberately does not import the `mcp` SDK. The SDK's Python API changed shape
between 1.x and 2.x, so a stub built on it could only test one major at a time --
and testing both majors is the entire reason this fixture exists. The wire
protocol is stable and camelCase, so a stdlib implementation is version-proof by
construction.

It imitates the part of `mcp-server-git` that matters for verification: three
tools taking a required `repo_path`, and two distinct rejection vocabularies.

    "Input validation error: ..."                     -> recognisable wording
    "Repository path '...' is outside the allowed..." -> unrecognisable wording

That split is the point. A crash detector that reads error text scores the second
group as dead transports; one that reads liveness scores all six as rejections.
Nothing here ever crashes, so any run reporting a crash has found a real bug in
the checker.

    python stub_mcp_server.py --repository /some/path
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# `git_log` sits fourth on purpose: `examples/mcp_server_git_repro.py` probes
# the first three and its published output asserts every call is rejected, so
# the null-accepting tool must stay outside that set. The contract test reaches
# it by raising `contract_tool_limit`, which is the API-reachable knob.
TOOLS = ["git_status", "git_diff_unstaged", "git_show", "git_log"]

SCHEMA = {
    "type": "object",
    "properties": {"repo_path": {"type": "string"}},
    "required": ["repo_path"],
}

#: `git_show` takes two required fields and validates only the first, which is
#: the shape no server in the regression set could produce: every tool in every
#: contract window has zero or one required field, so a probe that corrupted
#: only `required[0]` looked like full coverage everywhere it ran.
#:
#: `revision` is declared and never checked. A per-field probe finds that; a
#: probe that only ever corrupts `repo_path` cannot, and neither can one that
#: corrupts both at once -- `repo_path` is validated, so the call is rejected and
#: the unchecked field stays invisible behind it.
MULTI_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_path": {"type": "string"},
        "revision": {"type": "string"},
    },
    "required": ["repo_path", "revision"],
}

MULTI_FIELD_TOOLS = {"git_show"}

#: `git_log` rejects the synthesized placeholder for `repo_path` -- it is not a
#: real path -- and *accepts* a null in that same field. That combination is the
#: finding the baseline-rejection rule must not swallow: the baseline is
#: rejected, so a per-tool rule would discard every case from this tool
#: including the one that catches a genuine unvalidated null. The per-case rule
#: keeps it, because `null_required[repo_path]` replaces the placeholder rather
#: than carrying it.
NULL_ACCEPTING_TOOLS = {"git_log"}


def call_tool(name: str, arguments: dict[str, Any], allowed_root: str) -> dict[str, Any]:
    """Answer one tool call. Never raises, never exits: rejection is not a crash.

    `git_show` declares `revision` required and never looks at it. That is the
    bug this fixture exists to be caught doing: a null, an integer, an empty
    string or a 50,000-character `revision` all come back as successes.
    """
    if name in MULTI_FIELD_TOOLS and "revision" not in arguments:
        # The one required-field check it does perform, so that omitting
        # `revision` is still rejected and only *corrupting* it slips through.
        return error("Input validation error: 'revision' is a required property")

    if name in NULL_ACCEPTING_TOOLS and arguments.get("repo_path", "") is None:
        # The unvalidated null this fixture exists to expose. A handler that
        # takes None here and carries on is exactly what the contract probe is
        # for, and it must survive a rejected baseline on the same tool.
        return ok("Repository log: (no commits)")

    if "repo_path" not in arguments:
        return error("Input validation error: 'repo_path' is a required property")

    value = arguments["repo_path"]
    if not isinstance(value, str):
        return error(f"Input validation error: {json.dumps(value)} is not of type 'string'")

    resolved = value or "."
    if resolved != allowed_root:
        # Unrecognisable-by-substring wording, on purpose.
        return error(f"Repository path '{resolved}' is outside the allowed repository "
                     f"'{allowed_root}'")

    return ok(f"Repository status: On branch main, nothing to commit ({name})")


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def handle(message: dict[str, Any], allowed_root: str) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")

    if request_id is None:
        return None  # a notification; nothing to answer

    if method == "initialize":
        params = message.get("params") or {}
        return result(request_id, {
            # Echo the client's protocol version rather than pinning one, so the
            # stub does not need updating every time the spec moves.
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub-mcp", "version": "1.0"},
        })

    if method == "tools/list":
        return result(request_id, {
            "tools": [
                {
                    "name": n,
                    "description": f"{n} (stub)",
                    "inputSchema": (
                        MULTI_FIELD_SCHEMA if n in MULTI_FIELD_TOOLS else SCHEMA
                    ),
                }
                for n in TOOLS
            ]
        })

    if method == "tools/call":
        params = message.get("params") or {}
        return result(request_id, call_tool(
            params.get("name", ""), params.get("arguments") or {}, allowed_root
        ))

    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=".")
    args = parser.parse_args()

    # readline(), not `for line in sys.stdin`: the iterator form reads ahead into
    # a buffer and will not yield until that buffer fills or the pipe closes, so
    # a client waiting on a reply to its first request deadlocks. Piped input
    # hides this completely -- it only appears on a live bidirectional pipe,
    # which is the only way this fixture is ever actually used.
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message, args.repository)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
