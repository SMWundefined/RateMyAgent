#!/usr/bin/env python3
"""The agent the Phase D gate run actually met, reduced to a fixture.

`claude-haiku-4-5` invented an idempotency key derived from the task's own id
and payload -- `gate-alpha-gate-1` -- and sent the **same** key in every run of
the task, including the scan's clean pass. On a session closed under
`RESPONSE_LOST_THEN_CLOSED` it reconnected and retried, keeping that key in four
runs of five (`assets/moat/GATE-D.md` section 4).

Shipped as a fixture in 1.7.0 because it is the only agent here whose key is a
function of the task rather than of the process, and that difference is the
whole reason the pre-1.7.0 global key scope went unnoticed for four releases:
no fixture could produce it.

This fixture does exactly that and nothing else, so the gate2 twin change can be
proved against a ledger at zero model cost:

- **the key is a pure function of the task's content**, so it repeats across
  runs where `careful_agent`'s per-process key does not. That difference is the
  whole reason `careful_agent` cannot show the defect;
- **it reconnects once on a closed session** and re-sends with the same key.

It is not a model and claims nothing about one. It is the minimal thing that
reproduces the collision GATE-D hit, so that the fix can be shown to remove it.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_base import (  # noqa: E402
    call_result,
    connect,
    load_task,
    parse_args,
    report,
)

#: Long enough that a dropped reply is not mistaken for a slow one, short
#: enough that this fixture can never become the 234-second hang.
READ_TIMEOUT_S = 30.0


def content_key(task: dict) -> str:
    """The key, derived from the task and nothing else.

    The point of the fixture. Two runs of one task produce one key, which is
    what makes a globally-absorbing upstream swallow the second run's real
    work.
    """
    args = task["arguments"]
    return f"{args['id']}-{args['payload']}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    key = content_key(task)
    arguments = {**task["arguments"], "idempotency_key": key}

    client = connect(args.mcp_config, read_timeout_s=READ_TIMEOUT_S)
    attempts = 0
    last = "no attempt was made"
    try:
        for _ in range(2):
            attempts += 1
            try:
                reply = client.request(
                    "tools/call", {"name": task["tool"], "arguments": arguments}
                )
            except TimeoutError:
                last = "no reply"
                continue
            except ConnectionError as exc:
                # The session ended. Whether the write landed is unknown, which
                # is the decision the scan exists to observe: reconnect and send
                # it again, carrying the same key so an upstream that can
                # recognise the repeat is free to absorb it.
                last = str(exc)
                try:
                    client.close()
                except Exception:
                    pass
                client = connect(args.mcp_config, read_timeout_s=READ_TIMEOUT_S)
                continue

            ok, text, _error = call_result(reply)
            if ok:
                report({"ok": True, "result": text, "attempts": attempts,
                        "idempotency_key": key})
                return 0
            last = text

        report({"ok": False, "error": last, "attempts": attempts,
                "idempotency_key": key})
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
