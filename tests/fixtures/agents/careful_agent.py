#!/usr/bin/env python3
"""The agent that does retries properly.

Three things, and each is one an agent can get wrong on its own:

- **One idempotency key per logical operation, reused on every retry.** One
  run of a task is one operation, so the key is minted once per process and
  never per attempt: a key that changed per attempt would be no key at all.
  Nor is it derived from the task id alone -- running the same task again (the
  baseline pass, then the chaos pass) is a second operation, and a key the
  upstream had already seen would absorb the second run's real work.
- **Exponential backoff**, quadrupling, so a failing dependency is not hammered
  and the growth is wide enough to read through timer noise.
- **`retry_after_s` honoured** when the reply carries one: the wait is at least
  the hint. Over stdio it arrives in the error body rather than in a header,
  because there are no headers here.

It claims success only when a call actually succeeded. That is what makes it the
control for `optimistic_agent`, which is identical apart from what it claims.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import sys
import time
import uuid
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

#: First backoff, multiplied by `BACKOFF_FACTOR` per attempt. Small because the
#: suite runs in seconds and the shape is what is being measured, not the
#: duration; the factor is wide so a few milliseconds of pipe jitter cannot
#: make a growing schedule read as a flat one.
BASE_BACKOFF_S = 0.05
BACKOFF_FACTOR = 4
MAX_BACKOFF_S = 1.0
#: Ceiling on a server-requested wait, so a hint of two minutes cannot turn
#: this fixture into a hang.
MAX_HINT_S = 5.0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    client = connect(args.mcp_config, read_timeout_s=DEFAULT_READ_TIMEOUT_S)

    # Minted once for this run, so every attempt at this operation carries the
    # same key. An agent that generated one per attempt would look careful and
    # behave blind, which is the failure this fixture exists to be the opposite
    # of.
    key = f"idem-{task['id']}-{uuid.uuid4().hex[:12]}"
    arguments = {**task["arguments"], "idempotency_key": key}

    attempts = 0
    last = "no attempt was made"
    try:
        for attempt in range(args.max_retries + 1):
            attempts += 1
            try:
                reply = client.request(
                    "tools/call", {"name": task["tool"], "arguments": arguments}
                )
            except TimeoutError:
                # Nothing came back. There is no evidence either way about
                # whether the call ran, which is exactly why the key matters:
                # the retry is safe to send because the server can absorb it.
                last = "no reply"
                client.notify("notifications/cancelled", {"reason": "read timeout"})
                _wait(attempt, None)
                continue
            except ConnectionError as exc:
                last = str(exc)
                break

            ok, text, error = call_result(reply)
            if ok:
                report({"ok": True, "result": text, "attempts": attempts,
                        "idempotency_key": key})
                return 0
            last = text
            _wait(attempt, error)

        report({"ok": False, "error": last, "attempts": attempts,
                "idempotency_key": key})
        return 1
    finally:
        client.close()


def _wait(attempt: int, error: dict | None) -> None:
    """Exponential backoff, and never less than the server's own hint.

    Honouring a hint means waiting at least that long: a wait capped below the
    hint is a retry the server asked not to receive yet. The hint has its own
    ceiling so a server asking for two minutes cannot turn this into a hang.
    """
    delay = min(BASE_BACKOFF_S * (BACKOFF_FACTOR ** attempt), MAX_BACKOFF_S)
    hint = (error or {}).get("retry_after_s")
    if isinstance(hint, (int, float)) and hint > 0:
        delay = max(delay, min(float(hint), MAX_HINT_S))
    time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
