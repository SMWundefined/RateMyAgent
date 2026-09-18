#!/usr/bin/env python3
"""Check the Phase D duplicate against the server's own ledger, without RateMyAgent.

Same job as `examples/mcp_server_git_repro.py`, one layer up: settle a finding
this tool published without trusting this tool. It imports nothing from
`ratemyagent` -- stdlib only, no dependencies at all -- and re-derives the
duplicate count from two files RateMyAgent did not write the contents of:

- `twin-ledger.jsonl`  the event twin's own record of every call it handled, and
                       whether that call changed its state;
- `twin-state.jsonl`   what the twin actually stored.

It then compares that against the number the scorecard printed to the user. The
scorecard, rather than the JSON export, on purpose: what a user reads is what has
to be true, and the export carries an absolute interpreter path that has no
business in a repository.

    python3 examples/phase-d/verify_independent.py

Exit 0 when the independent count matches what was reported, 1 otherwise. It
fails loudly rather than printing a reassuring zero.
"""

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent

#: Read from the task file rather than hardcoded, so a changed task cannot
#: quietly make this script agree with itself.
TASKS = json.loads((HERE / "tasks.json").read_text())["tasks"]
EXPECTED = {str(t["id"]): int(t["expected_effects"]) for t in TASKS}


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fail(message):
    print(f"MISMATCH: {message}")
    sys.exit(1)


def main():
    ledger = rows(HERE / "twin-ledger.jsonl")
    state = rows(HERE / "twin-state.jsonl")
    scorecard = (HERE / "claude-haiku-fault.scorecard.txt").read_text()

    applied = [row for row in ledger if row.get("effect") == "applied"]
    print(f"twin ledger: {len(ledger)} calls, {len(applied)} applied")
    print(f"twin state:  {len(state)} rows stored")

    if len(applied) != len(state):
        fail(
            f"the ledger says {len(applied)} applied and the state holds "
            f"{len(state)} rows; the twin disagrees with itself"
        )

    # The scan runs each task twice: once clean, once under fault. The clean
    # pass is the first application, and everything after it belongs to the
    # chaos window. Counted here from the ledger, not from the scan's windows.
    baseline_effects = 1
    chaos_applied = len(applied) - baseline_effects
    expected = EXPECTED["t1"]
    independent = max(0, chaos_applied - expected)
    print(
        f"chaos window: {chaos_applied} applied against {expected} expected "
        f"-> {independent} duplicate(s)"
    )

    # What the user was shown.
    match = re.search(r"^\s*duplicate mutations\s+(\d+)\s", scorecard, re.M)
    if not match:
        fail("no duplicate mutations row in the scorecard")
    reported = int(match.group(1))
    print(f"scorecard printed: duplicate mutations = {reported}")

    if reported != independent:
        fail(
            f"the scorecard says {reported} and the twin's own ledger says "
            f"{independent}"
        )

    # The mechanism, also from outside the instrument: the same payload applied
    # twice, and the second attempt carrying no idempotency key though the first
    # one did. That is why the upstream could not absorb the repeat.
    chaos_rows = applied[baseline_effects:]
    keys = [row.get("idempotency_key") for row in chaos_rows]
    print(f"chaos idempotency keys: {keys}")
    if len(chaos_rows) >= 2 and keys[0] is not None and keys[-1] is None:
        print(
            "mechanism: the retry dropped the idempotency key the first attempt "
            "carried, so the upstream had nothing to recognise the repeat by"
        )

    # The two lost-reply faults are different faults and must not be summed: a
    # client can retry one and not the other.
    kinds = re.search(r"^\s*Faults injected: (.+?)\.\s*$", scorecard, re.M | re.S)
    if kinds:
        text = " ".join(kinds.group(1).split())
        print(f"faults injected: {text}")
        if "response_lost_then_closed" not in text:
            fail("the scorecard does not name the closing fault that was injected")

    print()
    print("AGREES: the twin's own ledger and the published scorecard report the "
          "same duplicate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
