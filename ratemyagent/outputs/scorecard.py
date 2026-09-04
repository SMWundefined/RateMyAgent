"""Terminal scorecard.

Renders to a string; printing is the caller's job so the same function serves
the CLI, the tests, and the markdown report.

The layout puts actual values next to policy targets, because the gap is the
information -- "p95 0.44s / target 5.0s" is actionable and "latency: 100" is
not. The last two lines are the verdict and the biggest gaps, since that is what
an engineer reads off a CI log.
"""

from __future__ import annotations

from ..models import ScanResult
from .common import align, breakdown_rows, target_rows, verdict_lines

WIDTH = 62

_LABELS = {
    "latency": "Latency",
    "cost": "Cost",
    "concurrency": "Concurrency",
    "contract": "Contract",
    "fault": "Fault tolerance",
    "behavior": "Behavior",
}

_PHASE_TITLES = {
    "baseline": "Phase 1  baseline",
    "chaos": "Phase 2  chaos (fault injection)",
    "behavior": "Phase 3  behavior analysis",
}


def render_scorecard(
    result: ScanResult, *, show_findings: bool = True, show_checks: bool = True
) -> str:
    """Format a scan as the terminal summary."""
    target = result.target
    transport = _transport(result)
    descriptor = f"{target.kind} via {transport}" if transport else target.kind

    lines: list[str] = [
        "RateMyAgent Scan Results",
        "=" * 24,
        "",
        f"Target: {target.name} ({descriptor})",
        f"Probes: {_completed(result)}/{len(result.probes)} complete"
        f"   Duration: {result.duration_s:.2f}s",
        "",
    ]

    lines.extend(_phase_block(result))

    if show_checks and result.checks:
        lines.extend(_actual_vs_target(result))
        lines.extend(_score_breakdown(result))

    lines.append(f"  Score: {_score_text(result)}")
    lines.append("")

    if show_findings:
        for probe in result.probes:
            if not probe.findings:
                continue
            lines.append(f"{_LABELS.get(probe.probe, probe.probe)} findings:")
            for finding in probe.findings:
                lines.extend(_wrap(finding))
            lines.append("")

    lines.extend(verdict_lines(result))
    return "\n".join(lines).rstrip() + "\n"


def _phase_block(result: ScanResult) -> list[str]:
    """What ran, grouped by pipeline phase."""
    lines: list[str] = []
    for phase in _phases_present(result):
        lines.append(_PHASE_TITLES.get(phase, phase))
        for probe in result.probes:
            if probe.phase != phase:
                continue
            label = _LABELS.get(probe.probe, probe.probe.replace("_", " ").title())
            lines.append(f"  {label + ' ':.<24} {probe.summary}")
        lines.append("")
    return lines


def _actual_vs_target(result: ScanResult) -> list[str]:
    rows = target_rows(result)
    if not rows:
        return []

    body = [("", "actual", "target", "status")]
    body.extend((f"  {row.label}", row.actual, row.target, row.status) for row in rows)

    return align(body, [28, 10, 10], gap=" ") + [""]


def _score_breakdown(result: ScanResult) -> list[str]:
    rows = breakdown_rows(result)
    if not rows:
        return []

    lines = ["  Score breakdown:"]
    body = [
        (f"    {label}", points, f"({note})" if note else "")
        for label, points, note in rows
    ]
    lines.extend(align(body, [18, 8]))
    lines.append("")
    return lines


def _score_text(result: ScanResult) -> str:
    if result.score is None:
        return "n/a  (no policy threshold could be evaluated)"
    return f"{result.score:.0f}/100  (policy {result.policy_name})"


def _phases_present(result: ScanResult) -> list[str]:
    """Phases that actually produced results, in pipeline order."""
    from ..probes import PHASES

    present = {probe.phase for probe in result.probes}
    ordered = [phase for phase in PHASES if phase in present]
    # Anything unrecognized still gets shown rather than silently dropped.
    return ordered + sorted(present - set(PHASES))


def _transport(result: ScanResult) -> str:
    return str(result.target.metadata.get("transport") or "")


def _completed(result: ScanResult) -> int:
    return sum(1 for probe in result.probes if not probe.failed)


def _wrap(text: str, indent: str = "  - ", continuation: str = "    ") -> list[str]:
    """Wrap a finding to WIDTH without pulling in textwrap's edge cases."""
    words = text.split()
    lines: list[str] = []
    current = indent

    for word in words:
        candidate = f"{current}{word}" if current in (indent, continuation) else f"{current} {word}"
        if len(candidate) > WIDTH and current not in (indent, continuation):
            lines.append(current)
            current = f"{continuation}{word}"
        else:
            current = candidate

    if current.strip():
        lines.append(current)
    return lines
