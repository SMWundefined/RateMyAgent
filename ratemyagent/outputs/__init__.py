"""Scan output renderers.

Week 1 ships the terminal scorecard; the markdown report (week 4) and the
AGENTS.md generator (week 5) land here alongside it.
"""

from __future__ import annotations

from .scorecard import render_scorecard

__all__ = ["render_scorecard"]
