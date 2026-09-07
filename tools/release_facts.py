#!/usr/bin/env python3
"""Print the scan facts for release notes, derived from the section 9 table.

Release bodies and commit messages are drafted from conversation, and
conversation drifts. "Nine servers" became "eight published servers" in one
place, "twelve" in another, and -- two sessions after the phrasing was
standardised -- "ten servers re-scanned" in the 0.1.8 release body, written by
the same person who had just standardised it.

No test can reach a release body: it is not in the repository. The durable fix
is not a wider phrase check, it is not writing the sentence by hand. Run this
and paste what it prints.

    python tools/release_facts.py

Reads `assets/PROGRESS.md`, which is gitignored working material, so this is a
local authoring aid rather than something CI can enforce.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "assets" / "PROGRESS.md"
#: The two leading columns are stable; the comparison columns are renamed
#: every release ("Before/After", then "0.1.8/0.1.9"). Keying off those
#: made this tool crash the first time section 9 was updated -- better
#: than a wrong answer, but avoidable.
HEADER = "| Server | Arguments |"


def table_rows(text: str) -> list[str]:
    # Anchored to line start. Unanchored `index()` matched the sentence in
    # section 8b that *quotes* this header while explaining the rule, which is
    # the fourth time a checker here has failed to tell its subject from a
    # mention of its subject.
    match = re.search(r"^" + re.escape(HEADER), text, re.M)
    if match is None:
        raise SystemExit(f"no table starting {HEADER!r} found in {PROGRESS}")
    start = match.start()
    block = text[start : text.index("\n\n", start)]
    return [line for line in block.splitlines() if line.startswith("| `")]


def main() -> int:
    if not PROGRESS.exists():
        print(f"{PROGRESS} not found; assets/ is gitignored", file=sys.stderr)
        return 2

    rows = table_rows(PROGRESS.read_text())
    servers = {row.split("|")[1].strip() for row in rows}
    marked_new = [r for r in rows if "(new)" in r or "**(new)**" in r]

    scans, distinct = len(rows), len(servers)
    print(f"scans:            {scans}")
    print(f"distinct servers: {distinct}")
    print(f"rows marked new:  {len(marked_new)}")
    print()
    print("Paste one of these rather than writing it:")
    print(f'  "{scans} scans across {distinct} distinct servers"')
    print(f'  "re-scanned all {scans} rows"')
    print()
    print("Not:")
    print(f'  "{scans} servers"        <- {scans} is the row count, not the server count')
    print(f'  "{distinct} scans"          <- {distinct} is the server count')
    return 0


if __name__ == "__main__":
    sys.exit(main())
