"""Present-tense "duplicate mutations cannot be scored" prose carries an expiry token.

Correction prose has a lifetime (PROGRESS section 8b). "No scan can produce a
duplicate-mutation failure" is true until something reads a target's state, and
false the release after. Written without a marker it would outlive its condition
the way the 0.1.4 corrections did: as a false sentence shipped on PyPI.

So each such sentence carries `<!-- DUPLICATES-WITHHELD-UNTIL-ORACLE -->`, and
this is the gate the superseded banners had:

- **the count per tracked surface matches the checklist**, so a marker deleted
  without its sentence fails, and so does one added without the checklist
  knowing;
- **once the CLI grows an effect oracle (`--verify-tool`), any remaining token
  fails.** That is the expiry.

Sentences about what 1.3.0 did are written in the past tense and carry no
token: they stay true. The checklist itself is in `assets/NextSteps.MD`, which
is gitignored; `EXPECTED` is the same list as data, so CI holds it without
`assets/`.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "<!-- DUPLICATES-WITHHELD-UNTIL-ORACLE -->"
ORACLE_FLAG = "--verify-tool"

#: Tracked surfaces and how many tokens each carries.
EXPECTED: dict[str, int] = {
    "README.md": 3,
    "docs/LIMITATIONS.md": 1,
    "docs/PROBES.md": 2,
    "docs/SCANNING.md": 1,
    "docs/POLICY.md": 3,
    "docs/ARCHITECTURE.md": 1,
}
#: Not tracked, so checked only where it exists.
LOCAL: dict[str, int] = {"CLAUDE.md": 1}


def _count(name: str) -> int:
    return (ROOT / name).read_text(encoding="utf-8").count(TOKEN)


def stale_tokens(flags: set[str], counts: dict[str, int]) -> dict[str, int]:
    """Surfaces still carrying the token once an effect oracle exists."""
    if ORACLE_FLAG not in flags:
        return {}
    return {name: count for name, count in counts.items() if count}


def _scan_flags() -> set[str]:
    from ratemyagent.cli import cli

    return {opt for param in cli.commands["scan"].params for opt in getattr(param, "opts", [])}


class TestDuplicatesWithheldToken:
    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_each_tracked_surface_carries_the_checklisted_count(self, name):
        assert _count(name) == EXPECTED[name], (
            f"{name} carries {_count(name)} expiry tokens, the checklist says "
            f"{EXPECTED[name]}. Update the prose, this table and the NextSteps "
            "checklist together."
        )

    @pytest.mark.skipif(not (ROOT / "CLAUDE.md").exists(), reason="CLAUDE.md is not tracked")
    def test_the_local_instruction_file_carries_its_token(self):
        assert _count("CLAUDE.md") == LOCAL["CLAUDE.md"]

    def test_no_token_outlives_an_effect_oracle(self):
        counts = {name: _count(name) for name in EXPECTED}
        assert stale_tokens(_scan_flags(), counts) == {}, (
            f"{ORACLE_FLAG} exists, so duplicate mutations can be measured; the "
            "withheld-metric prose is now false and must be rewritten"
        )

    def test_the_expiry_fires_when_the_flag_exists(self):
        """The deliberate failing case: the check above cannot fail today."""
        counts = {name: _count(name) for name in EXPECTED}
        assert sum(counts.values()) > 0
        assert stale_tokens(_scan_flags() | {ORACLE_FLAG}, counts) == {
            name: count for name, count in counts.items() if count
        }

    def test_flag_introspection_sees_the_scan_command(self):
        """An empty flag set would make the expiry unable to fire, silently."""
        assert "--allow-mutating" in _scan_flags()
