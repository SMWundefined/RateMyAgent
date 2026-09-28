#!/usr/bin/env python3
"""The agent whose task is more than one write.

**Tier 0 (`assets/moat/tier0/DESIGN-TIER-0.md`).** Every other fixture sends
one write per task, so the task window the oracle brackets has always held
exactly one operation. This one sends the task's `writes` in order -- one
`tools/call` per entry, each a separate operation -- so a window can hold two
or three, which is the case per-task-window attribution was never run against.

**N comes from `writes` and never from `expected_effects`.** If the agent
derived its behaviour from the denominator, the two could never disagree, and
the baseline check that compares them would be unreachable by construction. A
task with no `writes` is refused rather than defaulted to one write.

**`--key-mode`**, required, with no default -- a fixture flag that defaults is a
legal-looking value standing in for "nobody said" (PROGRESS 8b entry 39):

- `per-write`: one key per write, minted once per process and reused on that
  write's retries. `careful_agent`'s rule, applied to each operation.
- `per-task`: one key per process, sent on every write and every retry. The
  key identifies the task rather than the operation.
- `per-attempt`: a fresh key on every attempt, retries included. A retry
  carrying a new key is indistinguishable from new work.
- `none`: no key. `blind_agent`'s rule.

Retries are `blind_agent`'s, whatever the key: up to `--max-retries` more
attempts per write, no wait, no reading of hints. Timing is not under test and
no scored check reads it (`BUILD-TIER-0.md` section 1). A write that exhausts
its attempts ends the task: the agent stops there and claims failure. It claims
success only when every write got a successful reply.

Does not import `ratemyagent`.
"""

from __future__ import annotations

import sys
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

KEY_MODES = ("per-write", "per-task", "per-attempt", "none")


def _key_mode(argv: list[str]) -> str:
    """Pull `--key-mode` out of argv, refusing an absent or unknown value."""
    if "--key-mode" not in argv:
        raise SystemExit(f"--key-mode is required: one of {', '.join(KEY_MODES)}")
    index = argv.index("--key-mode")
    if index + 1 >= len(argv) or argv[index + 1] not in KEY_MODES:
        raise SystemExit(f"--key-mode must be one of {', '.join(KEY_MODES)}")
    mode = argv[index + 1]
    del argv[index:index + 2]
    return mode


def _writes(task: dict) -> list[dict]:
    writes = task.get("writes")
    if not isinstance(writes, list) or not writes or not all(
        isinstance(write, dict) for write in writes
    ):
        raise SystemExit(
            f"task {task.get('id')!r} has no `writes` list; this agent reads "
            "the number of writes from it and from nothing else"
        )
    return writes


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = _key_mode(argv)
    args = parse_args(argv)
    task = load_task(args.tasks, args.task)
    writes = _writes(task)
    client = connect(args.mcp_config, read_timeout_s=DEFAULT_READ_TIMEOUT_S)

    run = uuid.uuid4().hex[:12]
    task_key = f"idem-{task['id']}-{run}"

    attempts = 0
    try:
        for number, write in enumerate(writes, start=1):
            write_key = f"idem-{task['id']}-w{number}-{run}"
            last = "no attempt was made"
            succeeded = False
            for _ in range(args.max_retries + 1):
                attempts += 1
                arguments = dict(write)
                if mode == "per-write":
                    arguments["idempotency_key"] = write_key
                elif mode == "per-task":
                    arguments["idempotency_key"] = task_key
                elif mode == "per-attempt":
                    arguments["idempotency_key"] = f"idem-{uuid.uuid4().hex[:12]}"
                try:
                    reply = client.request(
                        "tools/call", {"name": task["tool"], "arguments": arguments}
                    )
                except TimeoutError:
                    last = "no reply"
                    client.notify("notifications/cancelled", {"reason": "read timeout"})
                    continue
                except ConnectionError as exc:
                    last = str(exc)
                    break

                ok, text, _ = call_result(reply)
                if ok:
                    succeeded = True
                    break
                last = text

            if not succeeded:
                report({"ok": False, "error": f"write {number}: {last}",
                        "attempts": attempts})
                return 1

        report({"ok": True, "result": f"{len(writes)} writes", "attempts": attempts})
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
