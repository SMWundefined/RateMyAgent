"""Terminal scorecard.

Renders to a string; printing is the caller's job so the same function serves
the CLI, the tests, and later the markdown report.
"""

from __future__ import annotations

from ..models import ScanResult

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


def render_scorecard(result: ScanResult, *, show_findings: bool = True) -> str:
    """Format a scan as the terminal summary, grouped by pipeline phase."""
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
    ]

    for phase in _phases_present(result):
        lines.extend(["", _PHASE_TITLES.get(phase, phase)])
        for probe in result.probes:
            if probe.phase != phase:
                continue
            label = _LABELS.get(probe.probe, probe.probe.replace("_", " ").title())
            # An inapplicable probe shows n/a, not a letter: it is excluded from
            # the overall grade, and printing a C next to one that counts would
            # imply it weighed in.
            if not probe.applicable:
                grade = "n/a"
            else:
                grade = probe.grade.value if probe.grade else "?"
            leader = "." * max(3, 22 - len(label))
            detail = probe.summary or ""
            lines.append(
                f"  {label} {leader} {grade}  ({detail})" if detail
                else f"  {label} {leader} {grade}"
            )

    lines.extend(["", f"  Overall: {result.overall_grade.value}", ""])

    if show_findings:
        for probe in result.probes:
            if not probe.findings:
                continue
            lines.append(f"{_LABELS.get(probe.probe, probe.probe)} findings:")
            for finding in probe.findings:
                lines.extend(_wrap(finding))
            lines.append("")

    skipped = _not_run(result)
    if skipped:
        lines.append(f"Not run ({len(skipped)}):")
        lines.extend(f"  {name:<12} {note}" for name, note in skipped)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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


def _not_run(result: ScanResult) -> list[tuple[str, str]]:
    from ..probes import PLANNED

    ran = {probe.probe for probe in result.probes}
    return [(name, note) for name, note in PLANNED.items() if name not in ran]


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
