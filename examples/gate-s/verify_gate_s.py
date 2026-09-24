#!/usr/bin/env python3
"""Re-derive Gate S's finding without importing `ratemyagent`.

Pure stdlib. No import of the package under test, and it fails loudly rather
than printing a reassuring zero.

    python3 examples/gate-s/verify_gate_s.py                       # the arms of record
    python3 examples/gate-s/verify_gate_s.py --root examples/gate-s/confounded \
        --stacks claude-code                                      # the confounded run
    python3 verify_gate_s.py [--root DIR] [--stacks a,b,c] [--replicates N]

Defaults to `evidence/` beside this file. The task file is read from the root if
it holds one (the seeded cases do), else from `tasks-gate-s.json` beside this
file -- the one copy every replicate ran.

Exit 0 when the independent derivation matches what the scans reported.

**Partition.** The twin stamps every ledger row with the `generation` it served
the call in -- the twin's own field, written by a process that does not import
this package, advanced only by the oracle's read at a task-window boundary. Runs
partition by it exactly, with no threshold and nothing tuned. Gate S runs one
scan per replicate against a fresh state, so each ledger holds exactly two
generations: the clean pass's and the chaos pass's.

**Two tightenings over `verify_gate2.py`** (DESIGN-GATE-S section 8.3):

  1. exactly TWO generations hold calls, not "at least two". Under per-replicate
     isolation a third is contamination, and `>= 2` would pass it.
  2. the clean generation is strictly lower than the chaos one AND the chaos
     window's first call was `applied`. (Phase 1 withdrew the earlier "key sets
     are disjoint" form: a content-derived key is the SAME STRING in both
     passes, and that is the operation scope working -- GATE-S-PHASE1 section 10.)

**The requests guard** (GATE-S-RETRY-VALIDITY section 5.2, and the check that
backs it). The twin's ledger, like the proxy's record, sees only calls that
reached a live transport. An arm whose one MCP session dies at the close and is
never reopened (the OpenAI Agents SDK) can send a retry that NOTHING
downstream observes, so "one call in the chaos window" does not mean "no
retry". For the arms in MODEL_COUNTED, the runner's own sidecar supplies the
model-side count, and a chaos window with one upstream call is split three ways
by it. Measured at $0 in `validity/` (prove_openai_close_path.py): the
`cached` arm made 3 model calls, issued 2 tool calls, and the ledger holds 1.

**What it does not do.** It reports the key table per arm, as counts with their
denominator, and never as a rate and never as a cross-stack difference -- Gate S
is not powered for that comparison (DESIGN-GATE-S section 7.3).
"""

import argparse
import json
import pathlib
import sys

FAILURES: list[str] = []

#: The three outcomes of a chaos window in which exactly ONE call reached the
#: upstream, for an arm whose model-side count is known. Named by the
#: maintainer, 2026-09-24, from GATE-S-RETRY-VALIDITY-2 section 3.2. Each
#: names an observation, not a cause: the guard sees counts.
NOT_ASKED = "no-second-turn"            # one model request; nothing asked again
ASKED_DECLINED = "second-turn-no-call"  # asked again, ended without a call
DEAD_RETRY = "retry-unobserved"         # model issued >= 2 calls, 1 reached upstream

#: Arms whose runner writes a model request count with defined semantics: one
#: LLM call per request, one tool (asserted by the runner), no handoffs, and a
#: claim of ok=True if and only if the run ended on a final answer. Under those
#: conditions `requests - (1 if ok else 0)` is a LOWER BOUND on the tool calls
#: the model issued (a response may carry parallel calls). Claude Code is not
#: here: its `num_turns` has no defined relation to tool calls, and it is
#: printed as not reconciled rather than silently passed.
MODEL_COUNTED = {"openai", "langgraph"}


def model_side(root: pathlib.Path, tag: str):
    """(requests, claimed_ok) for the chaos pass, from the runner's sidecars.

    The OpenAI runner puts `requests` in `usage` on both paths; the LangGraph
    runner puts it in `from_callback`, because its success-path `usage` is
    summed from messages and carries no request count. Missing means None --
    never a default of 1, which would be the reassuring zero this file exists
    to refuse.
    """
    side = root / f"sidecar-{tag}-chaos.json"
    claim = root / f"sidecar-{tag}-chaos.claim.json"
    if not side.exists() or not claim.exists():
        return None
    s = json.loads(side.read_text())
    usage = s.get("usage") or {}
    for src in (usage, s.get("from_callback") or {}, usage.get("from_callback") or {}):
        if isinstance(src.get("requests"), int):
            return src["requests"], bool(json.loads(claim.read_text()).get("ok"))
    return None


def fail(message: str) -> None:
    print(f"MISMATCH: {message}")
    FAILURES.append(message)


def rows(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


HERE = pathlib.Path(__file__).resolve().parent


def expected_effects(root: pathlib.Path) -> dict:
    """From the gate's own task file, so a changed task cannot make this
    agree with itself."""
    path = root / "tasks-gate-s.json"
    if not path.exists():
        path = HERE / "tasks-gate-s.json"
    tasks = json.loads(path.read_text())["tasks"]
    return {str(t["id"]): int(t["expected_effects"]) for t in tasks}


def scan_facts(path: pathlib.Path) -> tuple:
    """`duplicate_mutations` and `realized_placement`, read for COMPARISON only.

    Nothing below is derived from either. A refusal export -- one with no
    behaviour metrics at all -- is a failure, not a zero.
    """
    export = json.loads(path.read_text())
    dupes = placement = None
    for probe in export.get("probes", []):
        metrics = probe.get("metrics") or {}
        if "duplicate_mutations" in metrics:
            dupes = metrics["duplicate_mutations"]
        if "realized_placement" in metrics and placement is None:
            placement = metrics["realized_placement"]
    return dupes, placement


def replicate(root: pathlib.Path, stack: str, r: int, expect: dict) -> dict:
    tag = f"{stack}-{r}"
    ledger = rows(root / f"calls-{tag}.jsonl")
    state = rows(root / f"state-{tag}.jsonl")
    dupes_reported, placement = scan_facts(root / f"out-{tag}.json")
    exp = expect["t1"]

    pre_path = root / f"pre-state-{tag}.txt"
    pre = int(pre_path.read_text().strip()) if pre_path.exists() else None
    if pre != 0:
        fail(f"{tag}: the state file held {pre} row(s) before the run, not 0")

    events = [x for x in ledger if x.get("tool") != "effects"]
    applied = [x for x in events if x.get("effect") == "applied"]
    if len(applied) != len(state):
        fail(f"{tag}: ledger says {len(applied)} applied, state holds "
             f"{len(state)} rows; the twin disagrees with itself")

    by_gen: dict = {}
    for row in events:
        by_gen.setdefault(row["generation"], []).append(row)
    gens = sorted(by_gen)

    # -- tightening 1 -------------------------------------------------------
    if len(gens) != 2:
        fail(f"{tag}: {len(gens)} generation(s) hold calls {gens}; exactly two "
             f"are expected -- one clean pass and one chaos pass on a fresh "
             f"state. More is contamination, fewer is a pass that never called")
        return {"tag": tag, "ok": False, "placement": placement}

    clean_gen, chaos_gen = gens
    # -- tightening 2 -------------------------------------------------------
    if not clean_gen < chaos_gen:
        fail(f"{tag}: clean generation {clean_gen} is not below chaos "
             f"generation {chaos_gen}")
    # **The precondition, stated as what actually matters.** An earlier version
    # of this check asserted the two generations' KEY STRINGS were disjoint.
    # That was wrong, and phase 1 caught it: a model that derives its key from
    # the task's content sends the SAME string in both passes, and `--key-scope
    # operation` exists precisely so that string is not absorbed across the
    # boundary. A shared string is evidence the scope is working, not evidence
    # it failed.
    #
    # What must hold is that the clean pass's key did not SPEND the chaos
    # window's work: the chaos window's first call has to be `applied`.
    clean_keys = {x.get("idempotency_key") for x in by_gen[clean_gen]} - {None}
    chaos_keys = {x.get("idempotency_key") for x in by_gen[chaos_gen]} - {None}
    shared = sorted(clean_keys & chaos_keys)
    first_chaos = by_gen[chaos_gen][0]
    if first_chaos["effect"] != "applied":
        fail(f"{tag}: the chaos window's FIRST call was "
             f"{first_chaos['effect']}, not applied -- the clean pass's key "
             f"spent this window's work and the operation boundary failed")

    clean_applied = sum(1 for x in by_gen[clean_gen] if x["effect"] == "applied")
    if clean_applied != exp:
        fail(f"{tag}: the clean pass applied {clean_applied}, not {exp}")

    # -- the finding, in two steps, never clamped ---------------------------
    chaos_applied = sum(1 for x in by_gen[chaos_gen] if x["effect"] == "applied")
    duplicates = chaos_applied - exp          # negative means a LOST effect

    if duplicates != dupes_reported:
        fail(f"{tag}: independent {duplicates}, scan reported "
             f"{dupes_reported}")

    chaos_calls = by_gen[chaos_gen]
    keys = [x.get("idempotency_key") for x in chaos_calls]
    retried = len(chaos_calls) > 1           # a retry that REACHED the upstream

    # -- the requests guard -------------------------------------------------
    outcome = None
    model = None
    if stack in MODEL_COUNTED:
        model = model_side(root, tag)
        if model is None:
            fail(f"{tag}: no model-side request count in sidecar-{tag}-chaos"
                 f"[.claim].json; for this arm one upstream call cannot be "
                 f"read as 'no retry', so the replicate has no retry reading")
            return {"tag": tag, "ok": False, "placement": placement}
        requests, claimed_ok = model
        issued_min = requests - (1 if claimed_ok else 0)
        if not retried:
            if requests == 1 and claimed_ok:
                fail(f"{tag}: one model request that ended on a final answer "
                     f"cannot have issued the call the upstream saw; the "
                     f"sidecar and the ledger describe different runs")
            elif requests == 1:
                outcome = NOT_ASKED
            elif issued_min <= 1:
                outcome = ASKED_DECLINED
            else:
                outcome = DEAD_RETRY
        elif issued_min < len(chaos_calls):
            # Not a failure: parallel calls in one response, or a stack-level
            # retry, both put more calls upstream than model turns. Reported,
            # because either would be a finding in its own right.
            outcome = (f"upstream saw {len(chaos_calls)} calls, model issued "
                       f">= {issued_min}: parallel calls or a stack retry")

    if not retried:
        key_behaviour = outcome or "no retry reached the upstream (model side not reconciled)"
    elif keys[0] is None and keys[1] is None:
        key_behaviour = "no key either call"
    elif keys[1] is None:
        key_behaviour = "dropped"
    elif keys[1] == keys[0]:
        key_behaviour = "kept"
    else:
        key_behaviour = "changed"

    note = f"  [same key in both generations: {shared}]" if shared else ""
    if model is not None:
        note += f"  [model: {model[0]} request(s), claimed ok={model[1]}]"
    if retried and outcome:
        note += f"  [{outcome}]"
    print(f"  {tag}: gens {gens}  clean applied {clean_applied}  "
          f"chaos calls {len(chaos_calls)} "
          f"({', '.join(x['effect'] for x in chaos_calls)})  "
          f"independent duplicates {duplicates}, scan said {dupes_reported}  "
          f"[{key_behaviour}]{note}")

    return {"tag": tag, "ok": True, "placement": placement,
            "duplicates": duplicates, "reported": dupes_reported,
            "retried": retried, "key": key_behaviour, "keys": keys,
            "outcome": outcome if not retried else None,
            "reconciled": model is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(HERE / "evidence"))
    ap.add_argument("--stacks", default="claude-code,openai,langgraph")
    ap.add_argument("--replicates", type=int, default=5)
    args = ap.parse_args()

    root = pathlib.Path(args.root).resolve()
    expect = expected_effects(root)
    stacks = [s for s in args.stacks.split(",") if s]

    results: dict = {}
    for stack in stacks:
        print(f"{stack}:")
        results[stack] = [replicate(root, stack, r, expect)
                          for r in range(1, args.replicates + 1)]

    placements = {res["placement"] for arm in results.values() for res in arm}
    print(f"\nrealized placements across every replicate: {sorted(placements)}")
    if len(placements) != 1 or None in placements:
        fail("the realized placements are not all identical; these are not "
             "replicates but separate experiments sharing a seed")

    print("\nper arm -- counts with their denominator, never a rate:")
    for stack, arm in results.items():
        good = [x for x in arm if x.get("ok")]
        dupes = [x["duplicates"] for x in good]
        retries = sum(1 for x in good if x["retried"])
        print(f"  {stack:12} duplicates {dupes}  "
              f"occurred in {sum(1 for d in dupes if d > 0)} of {len(good)}  "
              f"a retry reached the upstream in {retries} of {len(good)}")
        single = [x for x in good if not x["retried"]]
        if single and all(x["reconciled"] for x in single):
            split: dict = {}
            for x in single:
                split[x["outcome"]] = split.get(x["outcome"], 0) + 1
            body = ", ".join(f"{k} {v}" for k, v in sorted(split.items()))
            print(f"  {'':12} of the {len(single)} with one upstream call: {body}")
            if split.get(DEAD_RETRY):
                print(f"  {'':12} ({DEAD_RETRY}: the retry's key never reached "
                      f"the twin and is NOT in the key table below)")
        elif single:
            print(f"  {'':12} one upstream call in {len(single)}; model side "
                  f"not reconciled for this arm -- read as 'no retry reached "
                  f"the upstream', never as 'did not retry'")
        table: dict = {}
        for x in good:
            if x["retried"]:
                table[x["key"]] = table.get(x["key"], 0) + 1
        if table:
            body = ", ".join(f"{k} {v}" for k, v in sorted(table.items()))
            print(f"  {'':12} of the {retries} retry event(s): {body}")
    print("\n  (the key table is reported per arm. Gate S is not powered to "
          "compare it across arms -- DESIGN-GATE-S section 7.3.)")

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} mismatch(es). The gate is not met.")
        return 1
    print("\nAGREES with the tool on every replicate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
