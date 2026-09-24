#!/usr/bin/env python3
"""Re-derive Gate S's exact tests from the shipped ledgers, and check the README.

Stdlib only; imports nothing from `ratemyagent`.

    python3 examples/gate-s/fisher_contrasts.py

**The counts come from the twin's own ledgers**, not from any export: a retry
"reached the upstream" when the chaos generation holds more than one `event`
call. **The p-values are two-sided Fisher exact** -- the sum of the probabilities
of every table with the same margins that is no more likely than the observed
one -- computed here, not copied.

**It exits 1 if any count or p-value differs from what the README publishes**
(`PUBLISHED` below, which the README's table states). A script that only printed
would be a calculator; this is a check that the published numbers are the ones
the evidence gives.

**Read the README's mechanism section before these numbers.** Each SDK arm's
0 of 5 is not a sample from a rate that could have been non-zero: at these
versions and defaults neither SDK client can put a retry on the wire after the
close. The test treats it as a sample anyway, which is why the per-arm p is
0.167 and why that number is not what the gate rests on.
"""

import json
import pathlib
import sys
from math import comb

HERE = pathlib.Path(__file__).resolve().parent

#: What the README's exact-test table states. (retries, n) per arm, and the
#: two-sided p for each contrast, to four places.
PUBLISHED = {
    "counts": {
        "claude-code": (3, 5),
        "openai": (0, 5),
        "langgraph": (0, 5),
        "claude-code (confounded)": (5, 5),
    },
    "p": {
        "claude-code vs openai": 0.1667,
        "claude-code vs langgraph": 0.1667,
        "claude-code vs openai+langgraph": 0.0220,
        "claude-code (confounded) vs openai": 0.0079,
        "claude-code (confounded) vs langgraph": 0.0079,
        "claude-code (confounded) vs openai+langgraph": 0.0003,
        "claude-code (confounded) vs claude-code": 0.4444,
    },
}


def retried(root: pathlib.Path, arm: str, r: int) -> bool:
    path = root / f"calls-{arm}-{r}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    events = [x for x in rows if x.get("tool") != "effects"]
    chaos = max(x["generation"] for x in events)
    return sum(1 for x in events if x["generation"] == chaos) > 1


def count(root: pathlib.Path, arm: str) -> tuple:
    return sum(retried(root, arm, r) for r in range(1, 6)), 5


def fisher(a: int, n1: int, b: int, n2: int) -> float:
    k, n = a + b, n1 + n2

    def p(x: int) -> float:
        return comb(k, x) * comb(n - k, n1 - x) / comb(n, n1)

    observed = p(a)
    lo, hi = max(0, k - n2), min(n1, k)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9))


def main() -> int:
    evidence, confounded = HERE / "evidence", HERE / "confounded"
    counts = {
        "claude-code": count(evidence, "claude-code"),
        "openai": count(evidence, "openai"),
        "langgraph": count(evidence, "langgraph"),
        "claude-code (confounded)": count(confounded, "claude-code"),
    }
    pooled = (counts["openai"][0] + counts["langgraph"][0],
              counts["openai"][1] + counts["langgraph"][1])
    ps = {}
    for cc in ("claude-code", "claude-code (confounded)"):
        ps[f"{cc} vs openai"] = fisher(*counts[cc], *counts["openai"])
        ps[f"{cc} vs langgraph"] = fisher(*counts[cc], *counts["langgraph"])
        ps[f"{cc} vs openai+langgraph"] = fisher(*counts[cc], *pooled)
    ps["claude-code (confounded) vs claude-code"] = fisher(
        *counts["claude-code (confounded)"], *counts["claude-code"])

    failures = []
    print("retry reached the upstream (from the twin ledgers):")
    for arm, (k, n) in counts.items():
        mark = "" if (k, n) == PUBLISHED["counts"][arm] else "   <-- README says " \
            f"{PUBLISHED['counts'][arm][0]}/{PUBLISHED['counts'][arm][1]}"
        if mark:
            failures.append(arm)
        print(f"  {arm:26} {k}/{n}{mark}")
    print("two-sided Fisher exact:")
    for label, p in ps.items():
        want = PUBLISHED["p"][label]
        mark = "" if round(p, 4) == want else f"   <-- README says {want}"
        if mark:
            failures.append(label)
        print(f"  {label:46} p = {p:.4f}{mark}")

    if failures:
        print(f"\nFAILED: {len(failures)} number(s) differ from the README.")
        return 1
    print("\nEvery count and p-value matches the README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
