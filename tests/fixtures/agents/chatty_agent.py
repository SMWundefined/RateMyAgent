#!/usr/bin/env python3
"""The agent that does not make the same calls twice.

**Added in 1.6.1, and it is the only fixture with this property.** `careful`,
`blind` and `optimistic` each make a fixed number of calls, so their realized
fault placement is their intended one, every run, and nothing in the suite could
tell the two apart. That is exactly what stops being true against a model: an
LLM chooses its own calls, so one seed faults a different call between two runs,
and two runs at that seed are two experiments sharing a random number rather
than two replicates of one (`DESIGN-AGENT-D.md` (e)).

**It varies by tally, not by chance.** A run reads a counter out of `--tally`,
increments it, and makes that many read-only `effects` calls before it writes.
So run 1 makes one, run 2 makes two, and the *command is identical* between
them -- which is the property under test -- while the sequence stays exactly
reproducible, because a flaky fixture would make a test about non-determinism
non-deterministic for the wrong reason.

The variation is in calls to a **read-only** tool on purpose. Ordinals are
counted per `(task, tool)`, so varying a read does not move the write's
placement, does not change what is applied, and leaves `expected_effects`
meaning what it says. What it does move is the ordinal an `effects` entry in the
table lands on -- so a scheduled fault on `effects#2` is out of reach on run 1
and fires on run 2, which is the spike's "ordinal 7 was never reached" in a form
a test can assert.

Retries are `careful`'s: one idempotency key, reused across attempts.

Does not import `ratemyagent`.
"""

from __future__ import annotations

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


def _tally(directory: str | None, task_id: str) -> int:
    """How many reads this run makes: one more than the last run of this task.

    **Counted per task, not per process.** A scan launches the agent once per
    task per pass, so a single counter would advance four times a scan and the
    number of reads in any one pass would depend on how many tasks ran before
    it. Per task, the count a given pass sees advances by exactly one per scan,
    which is what makes a test about run-to-run variation assert a number.

    No tally directory means one read, so an agent run without the flag is a
    fixed-shape agent and behaves like the other three.
    """
    if not directory:
        return 1
    counter = Path(directory) / f"{task_id}.txt"
    previous = 0
    if counter.exists():
        try:
            previous = int(counter.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            previous = 0
    current = previous + 1
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(str(current) + "\n", encoding="utf-8")
    return current


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tally_dir = None
    if "--tally" in argv:
        index = argv.index("--tally")
        tally_dir = argv[index + 1]
        del argv[index:index + 2]

    args = parse_args(argv)
    reads = _tally(tally_dir, args.task)
    task = load_task(args.tasks, args.task)
    client = connect(args.mcp_config, read_timeout_s=DEFAULT_READ_TIMEOUT_S)

    # One key for the whole operation, reused by every attempt: `careful`'s
    # rule, so a duplicate in a scan of this agent is about the placement and
    # not about the key.
    arguments = dict(task["arguments"])
    arguments["idempotency_key"] = f"{task['id']}-chatty"

    attempts = 0
    last = "no attempt was made"
    try:
        # The chatter. Read-only, so it changes the call count and nothing else.
        for _ in range(reads):
            try:
                client.request("tools/call", {"name": "effects", "arguments": {}})
            except TimeoutError:
                client.notify("notifications/cancelled", {"reason": "read timeout"})
            except ConnectionError:
                # A read that lost its session is not the task failing: the
                # write below reopens nothing and will report for itself.
                break

        for _ in range(args.max_retries + 1):
            attempts += 1
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
                report({"ok": True, "result": text, "attempts": attempts,
                        "reads": reads})
                return 0
            last = text

        report({"ok": False, "error": last, "attempts": attempts, "reads": reads})
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
