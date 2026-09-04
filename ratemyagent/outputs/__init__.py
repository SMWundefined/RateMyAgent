"""Scan output renderers.

Three views of the same scan: the terminal scorecard, the full markdown report,
and the AGENTS.md fix guide. All three render their shared blocks from
`common.py`, so the CI line, the report, and the guide cannot disagree.
"""

from __future__ import annotations

from .agents_md import build_state, read_state, render_agents_md, write_agents_md
from .common import breakdown_rows, target_rows, verdict_lines
from .report import render_report
from .scorecard import render_scorecard

__all__ = [
    "breakdown_rows",
    "build_state",
    "read_state",
    "render_agents_md",
    "render_report",
    "render_scorecard",
    "target_rows",
    "verdict_lines",
    "write_agents_md",
]
