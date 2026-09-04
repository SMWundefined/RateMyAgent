"""Formatting shared by the scorecard, the markdown report, and AGENTS.md.

The actual-vs-target table, the score breakdown, and the verdict lines are the
same information in all three outputs. They live here so the terminal and the
files cannot drift apart -- an engineer who reads "recovery rate 80.0% vs 90.0%
FAIL" in CI should find the same sentence in the report.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import CheckResult, DimensionScore, ScanResult

#: Human labels for policy keys, so the table reads as prose rather than as
#: configuration.
CHECK_LABELS: dict[str, str] = {
    "p95_latency_ms": "p95 latency",
    "p99_latency_ms": "p99 latency",
    "error_rate_max": "error rate",
    "cost_per_request_max": "cost per request",
    "concurrency_min": "sustained concurrency",
    "contract_crash_rate_max": "contract crash rate",
    "contract_invalid_accepted_max": "schema violations accepted",
    "recovery_rate_min": "recovery rate",
    "retry_amplification_max": "retry amplification",
    "duplicate_mutation_max": "duplicate mutations",
}


@dataclass(frozen=True)
class TargetRow:
    """One line of the actual-vs-target table."""

    label: str
    actual: str
    target: str
    status: str
    passed: bool
    skipped: bool


def format_value(value: float | None, units: str) -> str:
    """Render a metric in the unit an engineer would say out loud."""
    if value is None:
        return "-"
    if units == "ms":
        # Read back in seconds even though SLOs are written in ms: an engineer
        # compares "7.99s vs 5.00s" faster than "7988ms vs 5000ms".
        return f"{value / 1000:.2f}s"
    if units == "rate":
        return f"{value:.1%}"
    if units == "$":
        return f"${value:.4f}"
    if units == "x":
        return f"{value:.2f}x"
    if float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def target_rows(result: ScanResult) -> list[TargetRow]:
    """The actual-vs-target table, failures first."""
    rows = [
        TargetRow(
            label=CHECK_LABELS.get(check.name, check.name),
            actual=format_value(check.observed, check.units),
            target=format_value(check.threshold, check.units),
            status="n/a" if check.skipped else ("pass" if check.passed else "FAIL"),
            passed=check.passed,
            skipped=check.skipped,
        )
        for check in result.checks
    ]
    # Failures first: the reason someone opened this output is at the top.
    return sorted(rows, key=lambda row: (row.skipped, row.passed))


def breakdown_rows(result: ScanResult) -> list[tuple[str, str, str]]:
    """(label, points, note) per dimension, for the score breakdown block."""
    rows = []
    for dim in result.breakdown:
        points = (
            f"{dim.points:.0f}/{dim.weight:.0f}" if dim.measured else f"-/{dim.weight:.0f}"
        )
        rows.append((dim.label, points, dim.note))
    return rows


def verdict_lines(result: ScanResult, *, limit: int = 2) -> list[str]:
    """The two lines an engineer actually reads in CI output."""
    if result.score is None:
        return ["NO SCORE: no policy threshold could be evaluated against this scan."]

    state = "PASS" if result.passed else "FAIL"
    headline = (
        f"{state}: score {result.score:.0f} "
        f"{'meets' if result.passed else 'below'} pass threshold "
        f"{result.pass_score:g}."
    )

    gaps = result.biggest_gaps[:limit]
    if not gaps:
        return [headline]

    described = ", ".join(
        f"{gap.label} ({gap.points:.0f}/{gap.weight:.0f})" for gap in gaps
    )
    return [headline, f"Biggest gaps: {described}."]


def failed_checks(result: ScanResult) -> list[CheckResult]:
    """Checks that ran and did not pass, worst score first."""
    return sorted(result.failed_checks, key=lambda check: check.score)


def measured_dimensions(result: ScanResult) -> list[DimensionScore]:
    return [dim for dim in result.breakdown if dim.measured]


def align(rows: list[tuple[str, ...]], widths: list[int], gap: str = "  ") -> list[str]:
    """Left-align columns to fixed widths, trailing column unpadded."""
    lines = []
    for row in rows:
        cells = [
            str(cell).ljust(widths[i]) if i < len(row) - 1 else str(cell)
            for i, cell in enumerate(row)
        ]
        lines.append(gap.join(cells).rstrip())
    return lines
