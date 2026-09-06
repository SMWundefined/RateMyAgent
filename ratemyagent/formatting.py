"""Rendering helpers shared by probes and outputs.

Deliberately dependency-free and top-level: probes must not import from
`outputs`, and outputs must not import from `probes`, so a helper both need
lives above both.
"""

from __future__ import annotations


def format_seconds(seconds: float | None) -> str:
    """A duration at a precision a reader can act on.

    `{:.2f}s` renders anything under 5ms as "0.00s", which is how a scan of a
    local stdio server reported `p95 0.00s` for a real 0.73ms measurement. That
    is unreadable, and worse, indistinguishable from a missing value -- a reader
    cannot tell "too fast to show" from "never measured", and the second is a
    reason to distrust the whole row.

    Seconds stay seconds down to 10ms, then it switches to milliseconds:

        7.9884  -> "7.99s"       0.4423 -> "0.44s"
        0.0073  -> "7.3ms"       0.00073 -> "0.73ms"

    The 10ms cutoff is where `{:.2f}s` stops carrying information, and not a
    moment sooner. An earlier attempt switched to milliseconds below one second,
    which reads fine alone but wrecks the actual-vs-target table: that column
    exists so a reader can compare "0.44s" against a "5.00s" threshold at a
    glance, and "442ms vs 5.00s" makes them do unit conversion to find out
    whether they passed.
    """
    if seconds is None:
        return "-"
    if seconds >= 0.01:
        return f"{seconds:.2f}s"
    if seconds <= 0:
        return "0ms"

    ms = seconds * 1000
    return f"{ms:.1f}ms" if ms >= 1 else f"{ms:.2f}ms"
