"""Terminal scorecard.

Renders to a string; printing is the caller's job so the same function serves
the CLI, the tests, and later the markdown report.

The score is shown with the checks that produced it. A single number nobody can
take apart is a number nobody will act on, so every threshold that ran gets a
line saying what was required, what was seen, and what it scored.
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


def render_scorecard(
    result: ScanResult, *, show_findings: bool = True, show_checks: bool = True
) -> str:
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
            lines.append(_probe_line(probe))

    lines.extend(["", _headline(result), ""])

    if show_checks and result.checks:
        lines.extend(_check_table(result))

    if show_findings:
        for probe in result.probes:
            if not probe.findings:
                continue
            lines.append(f"{_LABELS.get(probe.probe, probe.probe)} findings:")
            for finding in probe.findings:
                lines.extend(_wrap(finding))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _probe_line(probe) -> str:
    label = _LABELS.get(probe.probe, probe.probe.replace("_", " ").title())
    leader = "." * max(3, 22 - len(label))

    # A probe with no score either could not run against this target or has no
    # policy threshold reading it. Either way a number would be a fiction.
    if not probe.applicable:
        score = "  n/a"
    elif probe.score is None:
        score = "    -"
    else:
        score = f"{probe.score:5.1f}"

    detail = probe.summary or ""
    return f"  {label} {leader} {score}  ({detail})" if detail else f"  {label} {leader} {score}"


def _headline(result: ScanResult) -> str:
    if result.score is None:
        return "  Score: n/a  (no policy threshold could be evaluated)"

    verdict = ""
    if result.passed is not None and result.pass_score is not None:
        state = "PASS" if result.passed else "FAIL"
        verdict = f"  [{state}, policy requires {result.pass_score:g}]"
    return f"  Score: {result.score:.1f}/100{verdict}"


def _check_table(result: ScanResult) -> list[str]:
    """One line per policy threshold, worst first so failures lead."""
    lines = [f"Policy checks ({result.policy_name}):"]

    ran = [c for c in result.checks if not c.skipped]
    skipped = [c for c in result.checks if c.skipped]

    for check in sorted(ran, key=lambda c: c.score):
        mark = "ok  " if check.passed else "FAIL"
        lines.append(f"  {mark} {check.name:<32} {check.score:5.1f}  {check.reason}")

    for check in skipped:
        lines.append(f"  --   {check.name:<32}    -   {check.reason}")

    lines.append("")
    lines.append(
        "  n/a = probe could not measure this target;  "
        "- = no policy threshold reads it"
    )
    lines.append("")
    return lines


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
