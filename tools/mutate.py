#!/usr/bin/env python3
"""Mutation harness. Counts what was collected, not just what failed.

**Why this is a file and not a throwaway script.** It was written ad hoc four
times in one day and needed the same correction three of them: a mutation that
introduces a `SyntaxError` makes pytest collect *nothing*, the summary contains
no "N failed", and a harness reading failure counts concludes **0 failed --
mutant survived**. Every one of those readings was wrong in the same direction.

That is absence-read-as-presence, in the harness whose job is validating fixes
for absence-read-as-presence. The correction is the same one this project keeps
arriving at from other directions: **check the denominator.** A kill is only a
kill if the suite still ran.

Three verdicts, and the third is the one that was missing:

  KILLED    the suite collected as many tests as the baseline, and some failed
  SURVIVED  the suite collected as many tests as the baseline, and none failed
  INVALID   collection changed -- the mutant broke the module rather than the
            behaviour, and says nothing either way

Usage:

    from tools.mutate import Mutant, run_matrix
    run_matrix([Mutant("A", "what it does", path, old, new), ...])
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Golden-file gates fail on any output change, so they kill every mutant and
#: tell you nothing about the logic under test. Counted separately.
GOLDEN = ("readme_block", "agents_md_example", "report_example")


@dataclass
class Mutant:
    tag: str
    label: str
    path: pathlib.Path
    old: str
    new: str


@dataclass
class Result:
    tag: str
    label: str
    verdict: str
    collected: int
    killers: list[str] = field(default_factory=list)
    note: str = ""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        ["uv", "run", "pytest", *args, "--color=no", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def collected_count(scope: list[str] | None = None) -> int:
    """How many tests pytest can see. The denominator this harness forgot."""
    out = _run([*(scope or []), "--collect-only", "-q"]).stdout
    match = re.search(r"(\d+) tests? collected", out)
    if match:
        return int(match.group(1))
    # A collection error prints no count at all, which is the case that used to
    # read as a pass.
    return 0


def _clear_pycache() -> None:
    for cache in ROOT.rglob("__pycache__"):
        for compiled in cache.glob("*.pyc"):
            compiled.unlink()


def run_matrix(
    mutants: list[Mutant], *, quiet: bool = False, scope: list[str] | None = None
) -> list[Result]:
    """`scope` narrows which tests run, e.g. `["tests/test_backoff.py"]`.

    The denominator is taken over the same scope, so the collection guard still
    holds. Narrowing is a real need -- a matrix over one feature spends minutes
    re-running a suite that cannot fail -- but it is also how a matrix comes to
    prove less than it looks: a mutant killed only within its own file has not
    been shown to leave the rest of the suite alone. State the scope when
    reporting a scoped run.
    """
    baseline = collected_count(scope)
    if baseline == 0:
        raise SystemExit("baseline collects no tests; fix the tree first")
    if not quiet:
        print(f"baseline: {baseline} tests collected\n")

    backups: dict[pathlib.Path, str] = {}
    for mutant in mutants:
        backups.setdefault(mutant.path, mutant.path.read_text())

    def restore() -> None:
        for path, text in backups.items():
            path.write_text(text)
        _clear_pycache()

    results: list[Result] = []
    try:
        for mutant in mutants:
            restore()
            text = mutant.path.read_text()
            hits = text.count(mutant.old)
            if hits != 1:
                results.append(Result(
                    mutant.tag, mutant.label, "INVALID", baseline,
                    note=f"anchor matches {hits} times, not once",
                ))
                continue

            mutant.path.write_text(text.replace(mutant.old, mutant.new))
            collected = collected_count(scope)
            if collected != baseline:
                results.append(Result(
                    mutant.tag, mutant.label, "INVALID", collected,
                    note=(
                        f"collection changed {baseline} -> {collected}; the "
                        "mutant broke the module, not the behaviour"
                    ),
                ))
                continue

            proc = _run([*(scope or []), "-q", "--no-header"])
            failed = sorted({
                m.split("::")[-1]
                for m in re.findall(r"^FAILED (\S+)", proc.stdout, re.M)
            })
            targeted = [f for f in failed if not any(g in f for g in GOLDEN)]
            verdict = "KILLED" if targeted else "SURVIVED"
            note = ""
            if not targeted and failed:
                note = f"only golden gates fired ({len(failed)}), which prove nothing"
            results.append(Result(mutant.tag, mutant.label, verdict, collected,
                                  targeted, note))
    finally:
        restore()

    if not quiet:
        _report(results, baseline)
    return results


def _report(results: list[Result], baseline: int) -> None:
    for r in results:
        mark = {"KILLED": "", "SURVIVED": "  *** SURVIVED ***",
                "INVALID": "  *** INVALID ***"}[r.verdict]
        print(f"{r.tag}  {r.label[:46]:48} {r.verdict:9}{mark}")
        if r.note:
            print(f"      {r.note}")
        for killer in r.killers[:2]:
            print(f"      - {killer}")

    killed = sum(1 for r in results if r.verdict == "KILLED")
    invalid = [r for r in results if r.verdict == "INVALID"]
    survived = [r for r in results if r.verdict == "SURVIVED"]
    print(f"\n{killed}/{len(results)} killed, "
          f"{len(survived)} survived, {len(invalid)} invalid")
    if invalid:
        print("An INVALID mutant is not a passing one. Fix the mutation and rerun.")


def self_check() -> int:
    """Prove the harness catches its own failure mode.

    A syntactically broken mutant must report INVALID, never SURVIVED. This is
    the deliberate failing case the rule two paragraphs into section 8b asks
    for, applied to the tool that checks everything else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = ROOT / "ratemyagent" / "__init__.py"
        shutil.copy(target, pathlib.Path(tmp) / "backup")
        broken = Mutant(
            "SELF", "syntactically broken mutant", target,
            "from __future__ import annotations",
            "from __future__ import annotations\nthis is not python",
        )
        results = run_matrix([broken], quiet=True)
        shutil.copy(pathlib.Path(tmp) / "backup", target)

    verdict = results[0].verdict
    print(f"self-check: a broken mutant reports {verdict}")
    if verdict != "INVALID":
        print("FAIL: the harness would read a collection error as a result")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(self_check())
