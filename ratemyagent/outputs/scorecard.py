"""Terminal scorecard.

Renders to a string; printing is the caller's job so the same function serves
the CLI, the tests, and later the markdown report.
"""

from __future__ import annotations

from ..models import ScanResult

WIDTH = 62
_LABELS = {"latency": "Latency", "cost": "Cost", "load": "Load", "fault": "Fault tolerance",
           "reliability": "Reliability", "errors": "Error patterns"}


def render_scorecard(result: ScanResult, *, show_findings: bool = True) -> str:
    """Format a scan as the A-F terminal summary."""
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

    for probe in result.probes:
        label = _LABELS.get(probe.probe, probe.probe.replace("_", " ").title())
        grade = probe.grade.value if probe.grade else "?"
        detail = probe.summary or ""
        leader = "." * max(3, 22 - len(label))
        lines.append(f"  {label} {leader} {grade}  ({detail})" if detail
                     else f"  {label} {leader} {grade}")

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
