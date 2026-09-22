#!/usr/bin/env python3
"""Re-derive the gate re-run's finding without importing `ratemyagent`.

Same construction as `examples/phase-d/verify_independent.py`: pure stdlib, no
import of the package under test, and it fails loudly rather than printing a
reassuring zero.

    python3 verify_gate.py

Exit 0 when the independent derivation matches what the scan reported.

**Segmentation, which GATE-D could not do.** There, one `--calls` ledger held
six agent runs with nothing in it to cut them by, so the verdict rested on a
bound that needed no segmentation and the per-run split was a labelled
secondary derived from a time gap. Here the twin stamps every row with the
**generation** it served the call in -- the twin's own field, written by the
twin's own process, advanced by the oracle's read at each task-window boundary.
Runs are partitioned by it exactly, with no threshold and nothing tuned.

That is still the server's account of itself and not the scanner's: the
generation is written by a process that does not import this package, and the
checks below compare it against the scan's export rather than deriving it from
one.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

#: Read from the task file rather than hardcoded, so a changed task cannot
#: silently make this agree with itself.
TASKS = json.loads((HERE / "tasks.json").read_text())["tasks"]
EXPECTED = {str(t["id"]): int(t["expected_effects"]) for t in TASKS}


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fail(message):
    print(f"MISMATCH: {message}")
    sys.exit(1)


def main():
    ledger = rows(HERE / "twin-ledger.jsonl")
    state = rows(HERE / "twin-state.jsonl")
    scan = json.loads((HERE / "scan.json").read_text())

    events = [r for r in ledger if r.get("tool") != "effects"]
    applied = [r for r in events if r.get("effect") == "applied"]
    absorbed = [r for r in events if r.get("effect") == "absorbed"]
    print(f"twin ledger: {len(events)} event calls, {len(applied)} applied, "
          f"{len(absorbed)} absorbed")
    print(f"twin state:  {len(state)} rows")
    if len(applied) != len(state):
        fail(f"the ledger says {len(applied)} applied and the state holds "
             f"{len(state)} rows; the twin disagrees with itself")

    # --- Partition by the twin's own generation stamp ----------------------
    by_gen = {}
    for row in events:
        by_gen.setdefault(row["generation"], []).append(row)
    gens = sorted(by_gen)
    print(f"generations holding event calls: {gens}")

    if len(gens) < 2:
        fail("every call landed in one generation; the operation boundary "
             "never advanced and the runs are not separated")

    baseline_gen, chaos_gens = gens[0], gens[1:]
    expected_each = EXPECTED["t1"]

    # The precondition the whole re-run exists to establish.
    baseline_keys = {
        r["idempotency_key"] for r in by_gen[baseline_gen]
        if r.get("idempotency_key")
    }
    print(f"clean pass: generation {baseline_gen}, keys {sorted(baseline_keys)}")
    for gen in chaos_gens:
        if gen == baseline_gen:
            fail(f"a chaos run shares the clean pass's generation {gen}")
    print(f"chaos runs: generations {chaos_gens} -- none shares the clean "
          f"pass's, so no chaos key can be absorbed by it")

    # --- Per-run counts, from the ledger alone -----------------------------
    print()
    independent = []
    for index, gen in enumerate(chaos_gens, start=1):
        run_rows = by_gen[gen]
        run_applied = [r for r in run_rows if r.get("effect") == "applied"]
        dupes = max(0, len(run_applied) - expected_each)
        independent.append(dupes)
        keys = [r.get("idempotency_key") for r in run_rows]
        if len(set(keys)) == 1 and keys[0] is not None:
            verdict = "kept its key"
        elif None in keys and any(k is not None for k in keys):
            verdict = "DROPPED its key on the retry"
        elif len(set(keys)) > 1:
            verdict = "CHANGED its key between attempts"
        else:
            verdict = "sent no key at all"
        print(f"  run {index} (gen {gen}): {len(run_rows)} calls, "
              f"{len(run_applied)} applied, {dupes} duplicate -- {verdict}")
        print(f"            keys: {keys}")

    # --- What the scan claims, out of its own export -----------------------
    behavior = next(
        (p for p in scan.get("probes", []) if p.get("probe") == "behavior"), None
    )
    if behavior is None:
        fail("no behavior probe in the scan export")
    metrics = behavior["metrics"]
    ranges = metrics.get("repeat_ranges", {})
    reported = ranges.get("duplicate_mutations", {}).get("values")
    print()
    print(f"scan reported duplicate_mutations per run: {reported}")
    print(f"independent  duplicate_mutations per run: {independent}")
    if reported != independent:
        fail(f"the scan reported {reported} and the twin's own ledger gives "
             f"{independent}")

    occurred = sum(1 for v in independent if v)
    print(f"independent occurrence count: {occurred} of {len(independent)} runs")
    if occurred < 2:
        print("NOTE: fewer than 2 of R runs carry the finding; (g).6 is not met "
              "on this evidence")

    # --- The runs must be replicates, or the count spans experiments -------
    fault = next((p for p in scan.get("probes", []) if p.get("probe") == "fault"), None)
    placements = {r["realized_placement"] for r in (fault or {}).get("metrics", {}).get("runs", [])}
    print(f"realized placements: {sorted(placements)}")
    if len(placements) != 1:
        fail(f"placements differ between runs ({placements}); these are not "
             f"replicates and the occurrence count spans different experiments")

    kinds = (fault or {}).get("metrics", {}).get("injected_by_kind", {})
    if "response_lost" in kinds:
        fail("the two lost-reply kinds were folded into one count")
    if kinds.get("response_lost_then_closed") != 1:
        fail(f"expected exactly one response_lost_then_closed, got {kinds}")

    # --- The mechanism, from outside ---------------------------------------
    print()
    for index, gen in enumerate(chaos_gens, start=1):
        run_applied = [r for r in by_gen[gen] if r.get("effect") == "applied"]
        if len(run_applied) > expected_each:
            keys = [r.get("idempotency_key") for r in run_applied]
            print(f"run {index}: the upstream applied {len(run_applied)} effects "
                  f"against {expected_each} expected, under keys {keys} -- "
                  f"nothing let it recognize the repeat")

    print()
    print(f"AGREES: the twin's own ledger and the scan report the same per-run "
          f"duplicate counts ({independent}), in {occurred} of "
          f"{len(independent)} runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
