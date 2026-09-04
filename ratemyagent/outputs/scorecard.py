"""Terminal scorecard.

Renders to a string; printing is the caller's job so the same function serves
the CLI, the tests, and the markdown report.

The layout puts actual values next to policy targets, because the gap is the
information -- "p95 0.44s / target 5.0s" is actionable and "latency: 100" is
not. The last two lines are the verdict and the biggest gaps, since that is what
an engineer reads off a CI log.
"""

from __future__ import annotations

import click

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

#: Policy checks whose failure is a correctness problem rather than a budget
#: overrun. Kept in step with the `critical=True` advice in `agents_md.py`, so
#: the terminal and the fix guide cannot disagree about what counts as urgent.
CRITICAL_CHECKS: frozenset[str] = frozenset(
    {
        "duplicate_mutation_max",
        "contract_crash_rate_max",
        "contract_invalid_accepted_max",
        "recovery_rate_min",
    }
)


def _plain(text: str, **_kwargs: object) -> str:
    """Styling disabled: hand the text back untouched."""
    return text


def render_scorecard(
    result: ScanResult,
    *,
    show_findings: bool = True,
    show_checks: bool = True,
    hint: str | None = None,
    color: bool = False,
) -> str:
    """Format a scan as the terminal summary.

    `hint` is placed directly above the verdict rather than after it. CLAUDE.md
    is explicit that the last two lines are the verdict and the biggest gaps --
    anything printed below them displaces what a CI log gets grepped for.

    `color` defaults to off so that every programmatic caller -- tests, the
    markdown report, anything piping this into a file -- gets clean text. The
    CLI opts in, and `click.echo` then strips the codes again when stdout is
    not a terminal. Two independent layers, because an ANSI escape in a CI log
    or a committed report is a bug nobody notices until it is embarrassing.
    """
    style = click.style if color else _plain

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

    lines.extend(_phase_block(result, style))

    if show_checks and result.checks:
        lines.extend(_actual_vs_target(result, style))
        lines.extend(_score_breakdown(result))

    lines.append(f"  Score: {_score_text(result, style)}")
    lines.append("")

    if show_findings:
        critical = _critical_probes(result)
        for probe in result.probes:
            if not probe.findings:
                continue
            lines.append(f"{_LABELS.get(probe.probe, probe.probe)} findings:")
            for finding in probe.findings:
                lines.extend(_finding_lines(finding, probe.probe in critical, style))
            lines.append("")

    if hint:
        lines.extend([style(hint, fg="cyan"), ""])

    lines.extend(_verdict(result, style))
    lines.append("")
    lines.append(style(_byline(), dim=True))
    return "\n".join(lines).rstrip() + "\n"


def _byline() -> str:
    from .. import __version__

    return (
        f"ratemyagent v{__version__} - pip install ratemyagent - "
        "github.com/SMWundefined/RateMyAgent"
    )


def _critical_probes(result: ScanResult) -> set[str]:
    """Probes that failed a check listed in `CRITICAL_CHECKS`.

    Severity is resolved per probe rather than per finding, because findings
    are free text with no severity of their own. That makes this a slight
    over-mark: an informational finding from a probe that also failed a
    critical check is flagged too.
    """
    return {
        check.probe
        for check in result.checks
        if check.name in CRITICAL_CHECKS and not check.passed and not check.skipped
    }


def _finding_lines(finding: str, critical: bool, style) -> list[str]:
    """Wrap a finding, marking it CRITICAL when its probe failed a key check.

    The marker is folded into the text before wrapping and coloured after, so
    the escape codes never reach the column arithmetic.
    """
    if not critical:
        return _wrap(finding)

    lines = _wrap(f"CRITICAL {finding}")
    lines[0] = lines[0].replace("CRITICAL", style("CRITICAL", fg="red", bold=True), 1)
    return lines


def _phase_block(result: ScanResult, style) -> list[str]:
    """What ran, grouped by pipeline phase."""
    lines: list[str] = []
    for phase in _phases_present(result):
        lines.append(style(_PHASE_TITLES.get(phase, phase), fg="white", bold=True))
        for probe in result.probes:
            if probe.phase != phase:
                continue
            label = _LABELS.get(probe.probe, probe.probe.replace("_", " ").title())
            lines.append(f"  {label + ' ':.<24} {probe.summary}")
        lines.append("")
    return lines


def _actual_vs_target(result: ScanResult, style) -> list[str]:
    rows = target_rows(result)
    if not rows:
        return []

    body = [("", "actual", "target", "status")]
    # `status` is the trailing column, which `align` leaves unpadded, so it is
    # the one cell that can be styled before alignment without skewing it.
    body.extend(
        (f"  {row.label}", row.actual, row.target, _status(row.status, style))
        for row in rows
    )

    return align(body, [28, 10, 10], gap=" ") + [""]


def _status(status: str, style) -> str:
    if status == "pass":
        return style(status, fg="green")
    if status == "FAIL":
        return style(status, fg="red", bold=True)
    return style(status, dim=True)


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


def _score_text(result: ScanResult, style) -> str:
    if result.score is None:
        return style("n/a  (no policy threshold could be evaluated)", dim=True)

    fg = "green" if result.passed else "red"
    # Two self-terminating segments rather than one nested inside the other:
    # the inner reset would otherwise drop the colour for everything after it.
    return style(f"{result.score:.0f}", fg=fg, bold=True) + style(
        f"/100  (policy {result.policy_name})", fg=fg
    )


def _verdict(result: ScanResult, style) -> list[str]:
    lines = verdict_lines(result)
    if not lines:
        return lines

    fg = "green" if result.passed else "red"
    if result.score is None:
        fg = "red"
    return [style(lines[0], fg=fg, bold=True), *lines[1:]]


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
