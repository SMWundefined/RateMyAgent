"""Gate S's checkers, run as shipped: known to pass, and known to fail.

`examples/gate-s/verify_gate_s.py` re-derives Gate S's duplicate counts from the
twin's own ledgers and `fisher_contrasts.py` re-derives its exact tests; both are
stdlib-only and import nothing from this package. CI runs them on the shipped
evidence. **This file is what makes "known to fail" run in CI too**: the sixteen
seeded cases the checker was hardened against lived on one machine, and a checker
whose failure branches run nowhere is a checker that agrees with everything.

Each case asserts the exit code AND the reason, so a case cannot pass by failing
for a different reason than the one it was built to provoke.

Also pinned: the README's sha256 table matches the shipped ledgers and state
files, and covers every one of them.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "examples" / "gate-s"
CHECKER = GATE / "verify_gate_s.py"
CONTRASTS = GATE / "fisher_contrasts.py"
TASKS = json.loads((GATE / "tasks-gate-s.json").read_text())
PLACEMENT = "t1:event#1=response_lost_then_closed"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    # `python3`-equivalent: the interpreter, not the project environment's entry
    # points, and nothing from `ratemyagent` on the path the script could import.
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        capture_output=True, text=True, timeout=60,
    )


# -- the seeded cases ---------------------------------------------------------


def call(gen, key, effect):
    return {"tool": "event", "role": "agent", "generation": gen,
            "idempotency_key": key, "effect": effect,
            "id": "gate-s", "payload": "gate-1"}


def state(gen, key):
    return {"id": "gate-s", "payload": "gate-1", "gen": gen, "idempotency_key": key}


def export(dupes, placement=PLACEMENT):
    probes = [{"name": "fault", "metrics": {"realized_placement": placement}}]
    if dupes is not None:
        probes.append({"name": "behavior", "metrics": {
            "duplicate_mutations": dupes, "realized_placement": placement}})
    return {"probes": probes}


def openai_side(requests):
    return {"stack": "openai-agents", "usage": {"requests": requests}}


ONE_CALL = dict(ledger=[call(2, "k", "applied"), call(4, "k", "applied")],
                states=[state(2, "k"), state(4, "k")], exp=export(0))

#: The phase 0 positive control's own ledger rows (`careful_agent`, a real scan,
#: seed 254): applied, then applied and absorbed on one key in the chaos window.
POSITIVE_CONTROL = dict(
    ledger=[
        {"pid": 93860, "ts": 1790223991.866432, "role": "agent", "generation": 2,
         "tool": "event", "args": {"id": "gate-s", "payload": "gate-1"},
         "idempotency_key": "idem-t1-0c9af5e80185", "effect": "applied", "changed": True},
        {"pid": 93871, "ts": 1790223992.364128, "role": "agent", "generation": 4,
         "tool": "event", "args": {"id": "gate-s", "payload": "gate-1"},
         "idempotency_key": "idem-t1-06e63d6a76f9", "effect": "applied", "changed": True},
        {"pid": 93871, "ts": 1790223995.422414, "role": "agent", "generation": 4,
         "tool": "event", "args": {"id": "gate-s", "payload": "gate-1"},
         "idempotency_key": "idem-t1-06e63d6a76f9", "effect": "absorbed", "changed": False},
    ],
    states=[state(2, "idem-t1-0c9af5e80185"), state(4, "idem-t1-06e63d6a76f9")],
    exp=export(0),
)

#: (name, stack, spec, expected exit, a string the output must contain)
CASES = [
    # -- must PASS ------------------------------------------------------------
    ("healthy-duplicate", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "applied"),
                call(4, "k-b", "applied")],
        states=[state(2, "k-clean"), state(4, "k-a"), state(4, "k-b")], exp=export(1)),
     0, "[changed]"),
    ("positive-control", "pc", POSITIVE_CONTROL, 0, "[kept]"),
    ("shared-key-legal", "x", dict(
        ledger=[call(2, "k-same", "applied"), call(4, "k-same", "applied")],
        states=[state(2, "k-same"), state(4, "k-same")], exp=export(0)),
     0, "same key in both generations"),
    ("shared-key-kept", "x", dict(
        ledger=[call(2, "k-same", "applied"), call(4, "k-same", "applied"),
                call(4, "k-same", "absorbed")],
        states=[state(2, "k-same"), state(4, "k-same")], exp=export(0)),
     0, "[kept]"),
    ("guard-not-asked", "openai", dict(**ONE_CALL, sidecar=openai_side(1),
                                       claim={"ok": False, "error": "ClosedResourceError: "}),
     0, "[no-second-turn]"),
    ("guard-dead-retry", "openai", dict(**ONE_CALL, sidecar=openai_side(3),
                                        claim={"ok": True}),
     0, "[retry-unobserved]"),
    ("guard-declined", "openai", dict(**ONE_CALL, sidecar=openai_side(2),
                                      claim={"ok": True}),
     0, "[second-turn-no-call]"),
    ("guard-langgraph-callback", "langgraph", dict(
        **ONE_CALL, sidecar={"stack": "langgraph", "usage": {"input_tokens": 1},
                             "from_callback": {"requests": 2}},
        claim={"ok": True}),
     0, "[second-turn-no-call]"),
    # -- must FAIL ------------------------------------------------------------
    ("disagreement", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "applied"),
                call(4, "k-a", "absorbed")],
        states=[state(2, "k-clean"), state(4, "k-a")], exp=export(1)),
     1, "independent 0, scan reported 1"),
    ("negative-count", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "absorbed")],
        states=[state(2, "k-clean")], exp=export(0)),
     1, "independent -1, scan reported 0"),
    ("pre-state", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "applied"),
                call(4, "k-a", "absorbed")],
        states=[state(2, "k-clean"), state(4, "k-a")], exp=export(0), pre=3),
     1, "held 3 row(s) before the run"),
    ("third-generation", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "applied"),
                call(6, "k-c", "applied")],
        states=[state(2, "k-clean"), state(4, "k-a"), state(6, "k-c")], exp=export(0)),
     1, "3 generation(s) hold calls"),
    ("refusal-export", "x", dict(
        ledger=[call(2, "k-clean", "applied"), call(4, "k-a", "applied"),
                call(4, "k-a", "absorbed")],
        states=[state(2, "k-clean"), state(4, "k-a")], exp=export(None)),
     1, "scan reported None"),
    ("boundary-failed", "x", dict(
        ledger=[call(2, "k-same", "applied"), call(4, "k-same", "absorbed")],
        states=[state(2, "k-same")], exp=export(0)),
     1, "FIRST call was absorbed"),
    ("guard-missing-sidecar", "openai", dict(**ONE_CALL),
     1, "cannot be read as 'no retry'"),
    ("guard-inconsistent", "openai", dict(**ONE_CALL, sidecar=openai_side(1),
                                          claim={"ok": True}),
     1, "describe different runs"),
]


def _write_case(root: Path, stack: str, spec: dict) -> None:
    tag = f"{stack}-1"
    (root / "tasks-gate-s.json").write_text(json.dumps(TASKS))
    (root / f"calls-{tag}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in spec["ledger"]))
    (root / f"state-{tag}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in spec["states"]))
    (root / f"out-{tag}.json").write_text(json.dumps(spec["exp"]))
    (root / f"pre-state-{tag}.txt").write_text(f"{spec.get('pre', 0)}\n")
    if "sidecar" in spec:
        (root / f"sidecar-{tag}-chaos.json").write_text(json.dumps(spec["sidecar"]))
    if "claim" in spec:
        (root / f"sidecar-{tag}-chaos.claim.json").write_text(json.dumps(spec["claim"]))


def test_there_are_sixteen_cases_and_half_must_fail():
    assert len(CASES) == 16
    assert sum(1 for c in CASES if c[3] == 1) == 8


@pytest.mark.parametrize(("name", "stack", "spec", "code", "reason"), CASES,
                         ids=[c[0] for c in CASES])
def test_seeded_case(tmp_path, name, stack, spec, code, reason):
    _write_case(tmp_path, stack, spec)
    proc = _run(CHECKER, "--root", str(tmp_path), "--stacks", stack, "--replicates", "1")
    out = proc.stdout + proc.stderr
    assert proc.returncode == code, f"{name}: exit {proc.returncode}, want {code}\n{out}"
    assert reason in out, f"{name}: {reason!r} not in output\n{out}"
    if code == 1:
        assert "The gate is not met." in out
    else:
        assert "AGREES with the tool on every replicate." in out


# -- the shipped evidence -----------------------------------------------------


def test_the_arms_of_record_agree():
    proc = _run(CHECKER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "claude-code  duplicates [1, 0, 0, 1, 0]" in out
    assert "a retry reached the upstream in 3 of 5" in out
    assert "no-second-turn 5" in out
    assert out.count("a retry reached the upstream in 0 of 5") == 2
    assert f"['{PLACEMENT}']" in out


def test_the_confounded_run_agrees():
    proc = _run(CHECKER, "--root", str(GATE / "confounded"), "--stacks", "claude-code")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "duplicates [1, 1, 1, 1, 1]" in proc.stdout


def test_the_published_exact_tests_are_the_evidence():
    proc = _run(CONTRASTS)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Every count and p-value matches the README." in proc.stdout


def test_the_readme_hashes_match_every_ledger_and_state_file():
    readme = (GATE / "README.md").read_text()
    table = dict(
        (path, digest)
        for digest, path in re.findall(r"^([0-9a-f]{64})  ((?:evidence|confounded)/\S+)$",
                                       readme, flags=re.M)
    )
    shipped = sorted(
        str(p.relative_to(GATE))
        for sub in ("evidence", "confounded")
        for p in (GATE / sub).iterdir()
        if p.name.startswith(("calls-", "state-"))
    )
    assert sorted(table) == shipped, (
        "the README's hash table does not list exactly the shipped files"
    )
    for path, digest in table.items():
        assert hashlib.sha256((GATE / path).read_bytes()).hexdigest() == digest, path
