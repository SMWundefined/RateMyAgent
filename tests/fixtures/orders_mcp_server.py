#!/usr/bin/env python3
"""A database-backed MCP server: `create_order` writes a row into `orders`.

The stranger's server in the README walkthrough (DESIGN-TIER-1 §0), made
concrete: one mutating tool, one table, one file on disk. Stdlib only --
`sqlite3` from the standard library -- and raw JSON-RPC over stdio, the shape
every fixture here uses, so it runs under any Python the scan does.

- `create_order(customer_id, item, quantity, idempotency_key?)` inserts one
  row. A key it has already stored is **absorbed**: the existing order is
  returned and nothing is written, which is what a server that honours keys
  does. With no key, every call writes.
- `list_orders()` is read-only and returns the rows.

The state lives in `--db`, a file, so the scan's verify command reads what the
agent's copy of this server wrote. `--db :memory:` keeps it in the process
instead, which is the failure the clean-pass persistence refusal exists for.

    --key-path PATH  where the key sits in the arguments (default
                     idempotency_key), dotted, e.g. options.key
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "integer"},
        "item": {"type": "string"},
        "quantity": {"type": "integer"},
        "idempotency_key": {"type": "string"},
        "options": {"type": "object"},
    },
    "required": ["customer_id", "item", "quantity"],
}
READ_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _text(request_id: Any, text: str, *, error: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        body["isError"] = True
    return _result(request_id, body)


def _key(arguments: dict[str, Any], path: str) -> str | None:
    value: Any = arguments
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, str) else None


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None)
    db.execute(
        "create table if not exists orders ("
        " id integer primary key autoincrement,"
        " customer_id integer not null,"
        " item text not null,"
        " quantity integer not null,"
        " idempotency_key text unique)"
    )
    return db


def handle(message: dict[str, Any], db: sqlite3.Connection, key_path: str) -> dict | None:
    request_id, method = message.get("id"), message.get("method")
    if request_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "orders", "version": "1.0"},
        })

    if method == "tools/list":
        return _result(request_id, {"tools": [
            {
                "name": "create_order",
                "description": "Create an order. Pass idempotency_key to make a retry safe.",
                "inputSchema": CREATE_SCHEMA,
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            },
            {
                "name": "list_orders",
                "description": "Every order, as a JSON list.",
                "inputSchema": READ_SCHEMA,
                "annotations": {"readOnlyHint": True},
            },
        ]})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "create_order":
            try:
                customer = int(arguments["customer_id"])
                item = str(arguments["item"])
                quantity = int(arguments["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                return _text(request_id, f"invalid arguments: {exc}", error=True)
            key = _key(arguments, key_path)
            if key is not None:
                row = db.execute(
                    "select id from orders where idempotency_key = ?", (key,)
                ).fetchone()
                if row is not None:
                    return _text(request_id, json.dumps({"order_id": row[0], "replayed": True}))
            cursor = db.execute(
                "insert into orders (customer_id, item, quantity, idempotency_key)"
                " values (?, ?, ?, ?)", (customer, item, quantity, key),
            )
            return _text(request_id, json.dumps({"order_id": cursor.lastrowid}))
        if name == "list_orders":
            rows = db.execute(
                "select id, customer_id, item, quantity from orders order by id"
            ).fetchall()
            return _text(request_id, json.dumps([
                {"id": r[0], "customer_id": r[1], "item": r[2], "quantity": r[3]}
                for r in rows
            ]))
        return _text(request_id, f"unknown tool {name!r}", error=True)

    return _result(request_id, {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--key-path", default="idempotency_key")
    args = parser.parse_args()
    db = open_db(args.db)

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
        response = handle(message, db, args.key_path)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
