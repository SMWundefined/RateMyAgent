#!/usr/bin/env python3
"""A schema-validating MCP server: rejects invalid params at the JSON-RPC layer.

Every other fixture answers a malformed call with a `result` carrying an error.
**None of them rejects at the protocol layer**, and no server in the section 9
corpus declares `additionalProperties: false` -- so the case where a server
validates its own declared schema and replies `{"error": {"code": -32602}}`
could not arise, and a defect that inverted the contract dimension survived
thirteen releases.

This is that case. It declares `additionalProperties: false`, validates
arguments against the schema, and rejects the way the spec provides for. It
exists so the regression set can see a defect it was previously blind to by
construction.

`--permissive` flips it into the twin that validates nothing, which is the
comparison that shows the inversion: before the fix the strict server scored 49
and the permissive one 50.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "required": ["url"],
    "additionalProperties": False,
}
TOOLS = ["get_page"]


def why_invalid(args: Any) -> str | None:
    """The schema violation in `args`, or None. Mirrors what SCHEMA declares."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in args:
        if key not in SCHEMA["properties"]:
            return f"additional property {key!r} not allowed"
    for field in SCHEMA["required"]:
        if field not in args:
            return f"missing required property {field!r}"
    if not isinstance(args.get("url"), str):
        return "url must be a string"
    return None


def handle(message: dict[str, Any], permissive: bool) -> dict[str, Any] | None:
    request_id, method = message.get("id"), message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "strict-stub", "version": "1.0"},
        })

    if method == "tools/list":
        schema = dict(SCHEMA)
        if permissive:
            schema["additionalProperties"] = True
        return _result(request_id, {
            "tools": [
                {"name": name, "description": f"{name} (stub)", "inputSchema": schema}
                for name in TOOLS
            ]
        })

    if method == "tools/call":
        params = message.get("params") or {}
        reason = None if permissive else why_invalid(params.get("arguments") or {})
        if reason is not None:
            # -32602 INVALID_PARAMS. A validating server has no other way to say
            # this: the spec's error channel is the JSON-RPC error object.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Invalid params: {reason}"},
            }
        return _result(request_id, {
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        })

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--permissive", action="store_true")
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
        response = handle(message, args.permissive)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
