"""The withheld-metric expiry token is spent, and must stay spent.

1.3.1 withheld `duplicate_mutations` on every scan and said so in prose across
seven surfaces. Each of those sentences was true only until something could read
a target's state, so each carried
`<!-- DUPLICATES-WITHHELD-UNTIL-ORACLE -->` and this file failed the build once
`--verify-tool` existed.

`--verify-tool` shipped in 1.4.0. The gate did its job: it went red, the twelve
sentences were rewritten, and the tokens came out. What remains is the other
half of the rule -- *a correction that is not tied to the condition that
justified it will decay into a wrong claim* -- pointed the other way. A token
reappearing means someone has written "duplicate mutations cannot be scored"
again, which is now false, so the count must stay at zero.

Kept rather than deleted, because deleting a gate the moment it passes is how
the banner checks came back a second time (PROGRESS section 8b).
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "<!-- DUPLICATES-WITHHELD-UNTIL-ORACLE -->"
ORACLE_FLAG = "--verify-tool"

#: Every surface that carried one, plus the two that document the metric now.
SURFACES = (
    "README.md",
    "docs/LIMITATIONS.md",
    "docs/PROBES.md",
    "docs/SCANNING.md",
    "docs/POLICY.md",
    "docs/ARCHITECTURE.md",
)
#: Not tracked, so checked only where it exists.
LOCAL = ("CLAUDE.md",)


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _scan_flags() -> set[str]:
    from ratemyagent.cli import cli

    return {opt for param in cli.commands["scan"].params for opt in getattr(param, "opts", [])}


class TestTheTokenIsSpent:
    def test_the_oracle_flag_exists(self):
        """The condition that retired the token. If this fails, the tokens
        should come back rather than the prose staying as it is."""
        assert ORACLE_FLAG in _scan_flags()

    @pytest.mark.parametrize("name", SURFACES)
    def test_no_tracked_surface_carries_the_token(self, name):
        assert TOKEN not in _text(name), (
            f"{name} claims duplicate mutations cannot be scored. They can, "
            f"with {ORACLE_FLAG}: rewrite the sentence rather than re-adding "
            "the marker."
        )

    @pytest.mark.parametrize("name", LOCAL)
    def test_the_local_instruction_file_is_clear_too(self, name):
        if not (ROOT / name).exists():
            pytest.skip(f"{name} is not tracked")
        assert TOKEN not in _text(name)

    def test_the_surfaces_exist(self):
        """A haystack that quietly shrinks to nothing is a gate that cannot
        fail -- the same weakness the banned-phrase gate had."""
        missing = [name for name in SURFACES if not (ROOT / name).exists()]
        assert not missing, f"tracked surfaces have moved: {missing}"

    @pytest.mark.parametrize("name", SURFACES)
    def test_each_surface_is_actually_read(self, name):
        assert _text(name).strip(), f"{name} is empty, so reading it proves nothing"
