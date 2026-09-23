#!/usr/bin/env python3
"""Re-derive Gate BD's finding without importing `ratemyagent`.

Same construction as `examples/phase-d-gate/verify_gate.py`: pure stdlib, no
import of the package under test, and it fails loudly rather than printing a
reassuring zero.

    python3 examples/gate-bd/verify_gate_bd.py

Exit 0 when the independent derivation matches what every scan reported.

**The derivation.** Per replicate, from the database alone:

    chaos_effects = COUNT(*) - clean_effects
    duplicates    = chaos_effects - expected_effects
                  = COUNT(*) - 2 * expected_effects

Two steps rather than one so a negative is legible. A replicate whose chaos
pass applied nothing gives `chaos_effects = 0` and `duplicates = -1`, which is
a **lost effect, not a duplicate**, and it is reported as a negative rather
than clamped -- clamping is the arithmetic that lets a duplicate and a loss
cancel.

**Why the whole table is the window.** Nothing but the agent writes to the
upstream on an agent scan: the oracle connection is read-only and the scanner
makes no probe, preflight or baseline writes of its own. With a fresh database
per replicate the table is therefore exactly two windows, the clean pass's and
the chaos pass's, and no partition key is needed to exclude a foreign writer
because there is no foreign writer.

**This is a weaker independence claim than `verify_gate.py`'s**, and the README
beside this file says so at length. Gate D partitioned one shared ledger by a
`generation` field **the twin server itself wrote**. There is no such field
here: `mcp-sqlite` is an off-the-shelf server that stamps nothing, so this
script leans on the experiment's design (a fresh store per replicate) and on
one behaviour of the tool under test (below) instead of on the server's own
account of itself.

**Facts this script trusts, and where each comes from:**

  expected_effects = 1     tasks.json, parsed here. Not the export.
  pre-run rows = 0         pre-count-<r>.txt, written by the run script before
                           the scan started. Not the export.
  one clean + one chaos    the run script's own --repeats 1, one scan per
    pass wrote each db     database. The experiment's configuration, not the
                           tool's account of it.
  clean pass applied       **the one fact borrowed from the tool.** The scan
    exactly expected_      refuses at exit 2 when the clean pass's effects do
    effects                not equal expected_effects, so a run that completed
                           establishes it. Stated rather than hidden: it is
                           what licenses substituting `2 * expected_effects`
                           for `clean_effects + expected_effects`.
  nothing but the agent    measured three ways; see the README.
    wrote

**Read from the export, and only to compare against:**
`probes[behavior].metrics.duplicate_mutations` and
`probes[fault].metrics.realized_placement`. Nothing is derived from either.

The databases are opened `immutable=1` so that reading them creates no `-wal`
or `-shm` sidecar beside the checked-in files.
"""

import hashlib
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPLICATES = 5
TABLE = "gate_bd"

#: Read from the task file rather than hardcoded, so a changed task cannot
#: silently make this agree with itself.
TASKS = json.loads((HERE / "tasks.json").read_text())["tasks"]
EXPECTED = {str(t["id"]): int(t["expected_effects"]) for t in TASKS}


def fail(message):
    print(f"MISMATCH: {message}")
    sys.exit(1)


def rows_of(db):
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        return con.execute(f"SELECT id, note FROM {TABLE} ORDER BY id").fetchall()
    finally:
        con.close()


def main():
    expected_total = sum(EXPECTED.values())
    print(f"task file: {len(TASKS)} task(s), expected_effects total "
          f"{expected_total} ({EXPECTED})")
    print(f"derivation: duplicates = COUNT(*) - 2 x {expected_total}")
    print()

    independent = []
    reported = []
    placements = []

    for r in range(1, REPLICATES + 1):
        db = HERE / f"db-{r}.db"
        out = HERE / f"scan-{r}.json"
        pre = HERE / f"pre-count-{r}.txt"
        wal = HERE / f"wal-size-{r}.txt"

        for path in (db, out, pre):
            if not path.exists():
                fail(f"replicate {r}: {path.name} is missing")

        # --- preconditions, from the run's own files ------------------------
        pre_count = int(pre.read_text().strip())
        if pre_count != 0:
            fail(f"replicate {r}: the database held {pre_count} rows before the "
                 f"scan started; the whole table is not this run's window")
        if wal.exists():
            wal_bytes = int(wal.read_text().strip())
            if wal_bytes != 0:
                fail(f"replicate {r}: {wal_bytes} bytes left in the -wal at "
                     f"archive time; the .db is not the complete state")

        # --- the independent derivation, from the database alone ------------
        rows = rows_of(db)
        total = len(rows)
        clean_effects = expected_total
        chaos_effects = total - clean_effects
        duplicates = chaos_effects - expected_total
        independent.append(duplicates)

        # Row insert order, which AUTOINCREMENT and the pipeline's phase order
        # make meaningful: the clean pass runs strictly before the chaos pass,
        # so row 1 is the clean pass's and rows 2+ are the chaos pass's.
        split = ("row(s) " + ", ".join(str(i) for i, _ in rows[:clean_effects])
                 + " | " + ", ".join(str(i) for i, _ in rows[clean_effects:])
                 if total else "empty")

        digest = hashlib.sha256(db.read_bytes()).hexdigest()

        # --- what the scan claims, out of its own export --------------------
        scan = json.loads(out.read_text())
        # A setup refusal writes --json-out too (1.5.1), and `refused` is the
        # key that tells it from a scan export. A refusal is not a scan that
        # measured nothing, and reading one as though it were would report a
        # clean zero for a run that never started.
        if scan.get("refused"):
            fail(f"replicate {r}: scan-{r}.json is a refusal, not a scan: "
                 f"{scan.get('reason', '(no reason recorded)')}")
        behavior = next(
            (p for p in scan.get("probes", []) if p.get("probe") == "behavior"), None
        )
        if behavior is None:
            fail(f"replicate {r}: no behavior probe in the export")
        said = behavior["metrics"].get("duplicate_mutations")
        reported.append(said)

        fault = next(
            (p for p in scan.get("probes", []) if p.get("probe") == "fault"), None
        )
        placement = (fault or {}).get("metrics", {}).get("realized_placement")
        placements.append(placement)

        note = ""
        if duplicates < 0:
            note = "  <- NEGATIVE: the chaos pass applied less than expected (lost effect)"
        print(f"replicate {r}: {total} rows  [clean | chaos] = {split}")
        print(f"              chaos_effects {chaos_effects}, independent "
              f"duplicates {duplicates}, scan said {said}{note}")
        print(f"              placement: {placement}")
        print(f"              sha256(db-{r}.db) = {digest}")

    print()
    print(f"scan reported duplicate_mutations per replicate: {reported}")
    print(f"independent  duplicate_mutations per replicate: {independent}")
    if reported != independent:
        fail(f"the scans reported {reported} and the databases give {independent}")
    print("AGREES on every replicate.")

    # --- the runs must be replicates, or the count spans experiments --------
    distinct = set(placements)
    print(f"realized placements: {sorted(distinct)}")
    if len(distinct) != 1:
        fail(f"placements differ between replicates ({sorted(distinct)}); these "
             f"are not replicates and the occurrence count spans different "
             f"experiments")

    occurred = sum(1 for v in independent if v and v > 0)
    print(f"independent occurrence count: {occurred} of {REPLICATES} replicates "
          f"carry an applied duplicate")
    if occurred < 2:
        print(f"NOTE: fewer than 2 of {REPLICATES} replicates carry the finding; "
              f"the gate's own bar is not met on this evidence")

    lost = [i + 1 for i, v in enumerate(independent) if v < 0]
    if lost:
        print(f"NOTE: replicate(s) {lost} applied fewer effects than expected; "
              f"read lost effects before reading duplicates")

    print()
    print(f"AGREES: the five databases and the five exports report the same "
          f"per-replicate duplicate counts ({independent}), in {occurred} of "
          f"{REPLICATES} replicates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
