"""Formatting shared by the scorecard, the markdown report, and AGENTS.md.

The actual-vs-target table, the score breakdown, and the verdict lines are the
same information in all three outputs. They live here so the terminal and the
files cannot drift apart -- an engineer who reads "recovery rate 80.0% vs 90.0%
FAIL" in CI should find the same sentence in the report.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..formatting import format_seconds
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
    #: True when a caveat qualifies this number. Deliberately not a severity:
    #: a caveat is orthogonal to pass/fail, and a passing row is exactly where
    #: one matters most -- "0 duplicate mutations" over zero completed
    #: operations is a green row that means nothing.
    caveated: bool = False


def format_value(value: float | None, units: str) -> str:
    """Render a metric in the unit an engineer would say out loud."""
    if value is None:
        return "-"
    if units == "ms":
        # Policies are written in ms; read back at whatever scale is legible, so
        # a 5s threshold shows as "5.00s" and a sub-millisecond p95 does not
        # collapse to "0.00s".
        return format_seconds(value / 1000)
    if units == "rate":
        return f"{value:.1%}"
    if units == "$":
        return f"${value:.4f}"
    if units == "x":
        return f"{value:.2f}x"
    if float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def caveats_by_metric(result: ScanResult) -> dict[str, list]:
    """Caveats indexed by the metric they qualify.

    A caveat naming no metric qualifies its whole probe, and is indexed under
    every metric that probe supplied a check for -- "no faults were injected"
    is about all of them, not about one.
    """
    index: dict[str, list] = {}
    by_probe: dict[str, set[str]] = {}
    for check in result.checks:
        by_probe.setdefault(check.probe, set()).add(check.metric)

    for caveat in result.caveats():
        targets = (
            tuple(sorted(by_probe.get(caveat.probe, ())))
            if caveat.scope == "probe"
            else caveat.metrics
        )
        for metric in targets:
            index.setdefault(metric, []).append(caveat)
    return index


def target_rows(result: ScanResult) -> list[TargetRow]:
    """The actual-vs-target table, failures first."""
    qualified = caveats_by_metric(result)
    rows = [
        TargetRow(
            label=CHECK_LABELS.get(check.name, check.name),
            actual=format_value(check.observed, check.units),
            target=format_value(check.threshold, check.units),
            status="n/a" if check.skipped else ("pass" if check.passed else "FAIL"),
            passed=check.passed,
            skipped=check.skipped,
            caveated=check.metric in qualified,
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
    failed = [c for c in result.checks if not c.passed and not c.skipped]

    if not result.passed and result.score >= (result.pass_score or 0):
        # Above the threshold but failing a check. Saying "score 99 below pass
        # threshold 75" here would be false, and saying nothing about the checks
        # is how the verdict came to disagree with the table under it.
        names = ", ".join(CHECK_LABELS.get(c.name, c.name) for c in failed[:3])
        more = f" and {len(failed) - 3} more" if len(failed) > 3 else ""
        headline = (
            f"FAIL: score {result.score:.0f} meets pass threshold "
            f"{result.pass_score:g}, but {len(failed)} "
            f"{'check' if len(failed) == 1 else 'checks'} failed: {names}{more}."
        )
    else:
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


def fault_conditions(result: ScanResult) -> str | None:
    """One line naming the fault rate and the recovery floor it implies.

    In every header, because it is the fact that decides whether two scans can
    be put beside each other. `recovery_rate` is scored against
    `1 - fault_rate**max_retries` -- the rate the injector produces against a
    target that never fails -- so a scan at `--fault-rate 0.2` is graded against
    96% and one at 0.3 against 91%. Those two numbers are not comparable, and
    before this line nothing in any artifact said so: both printed "recovery
    rate ... 90.0%" and looked like the same measurement.

    Returns None when no fault phase ran, which is not the same as a rate of
    zero and should not print as one.
    """
    behavior = result.probe("behavior")
    if behavior is None:
        return None

    rate = behavior.metrics.get("fault_rate")
    retries = behavior.metrics.get("max_retries")
    floor = behavior.metrics.get("recovery_floor")
    if rate is None or retries is None:
        return None

    text = f"fault rate {rate:.0%}, {retries} retries"
    if floor is not None:
        text = f"{text} -> recovery floor {floor:.1%} (derived, not the policy value)"

    # Only when backoff actually fired. A policy line on every scan is noise on
    # the ones where nothing waited, and the claim it qualifies -- that
    # "recovered" may now mean "recovered after a wait" -- is only weaker when
    # a wait happened.
    fault = result.probe("fault")
    waited = (fault.metrics.get("backoff_waited_s") or 0.0) if fault else 0.0
    if waited:
        text = (
            f"{text}; waited {waited:.1f}s on rate limits, so 'recovered' here "
            "can mean recovered after a wait"
        )
    return text
