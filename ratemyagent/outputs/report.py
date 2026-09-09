"""Full markdown report, organized by pipeline phase.

The scorecard is what fits on a terminal; this is the whole scan. Same numbers,
same sentences -- both render from `outputs/common.py` so a report cannot
disagree with the CI line that sent someone to read it.

Order is deliberate: verdict, then the actual-vs-target table, then the score
breakdown, then the phases in pipeline order. Someone opening this after a red
build gets the answer in the first screen and the evidence below it.
"""

from __future__ import annotations

import json
from typing import Any

from ..formatting import format_seconds
from ..models import ProbeResult, ScanResult
from .common import CHECK_LABELS, breakdown_rows, target_rows, verdict_lines

PHASE_TITLES = {
    "baseline": ("Phase 1 — Baseline", "How the target behaves under normal conditions."),
    "chaos": (
        "Phase 2 — Fault injection",
        "The same probes against a target we are deliberately breaking.",
    ),
    "behavior": (
        "Phase 3 — Behavior analysis",
        "What the target did once things started failing.",
    ),
}

PROBE_TITLES = {
    "latency": "Latency",
    "cost": "Cost",
    "concurrency": "Concurrency",
    "contract": "Contract",
    "fault": "Fault injection",
    "behavior": "Behavior",
}

#: Metrics worth a table row per probe, in reading order.
HIGHLIGHTS: dict[str, tuple[tuple[str, str], ...]] = {
    "latency": (
        ("requests", "requests"), ("p50_s", "p50"), ("p95_s", "p95"),
        ("observed_p99_s", "p99"),
        ("error_rate", "error rate"), ("tail_ratio", "p99/p50"),
        ("tool_call_overhead_s", "call overhead"),
    ),
    "cost": (
        ("mean_input_tokens", "mean input tokens"),
        ("mean_output_tokens", "mean output tokens"),
        ("static_prefix_tokens", "fixed prefix"), ("bloat_share", "prefix share"),
        ("cost_per_request", "cost / request"), ("cost_per_1k_requests", "cost / 1k"),
    ),
    "concurrency": (
        ("max_sustained_concurrency", "sustained"), ("saturation_point", "saturates at"),
        ("latency_knee_at", "latency knee"), ("peak_throughput_rps", "peak goodput"),
    ),
    "contract": (
        # `tools` before `tools_probed`: the full count lived in the metrics
        # dict and was rendered nowhere, so every reader of this table saw
        # "tools probed 3" with no idea it was 3 of 12.
        ("tools", "tools exposed"), ("tools_probed", "tools probed"),
        ("tools_skipped_unsafe", "skipped as unsafe"),
        ("tools_capped", "past the cap"),
        ("cases_run", "edge cases"),
        ("required_fields_probed", "required fields probed"),
        ("real_args_tool", "real arguments for"),
        ("control_calls", "control calls"),
        ("control_undelivered", "control calls lost"),
        ("rejected", "rejected cleanly"), ("accepted", "accepted"),
        ("accepted_invalid", "accepted but invalid"), ("crashes", "crashed"),
    ),
    "fault": (
        ("max_retries", "retry budget"),
        ("calls", "calls"), ("injected", "faults injected"),
        ("injection_rate", "injection rate"),
        ("error_rate_under_fault", "error rate under fault"),
    ),
    "behavior": (
        ("trajectories", "operations"), ("attempts", "attempts"),
        ("retry_amplification", "amplification"), ("disrupted", "disrupted"),
        ("recovered", "recovered"), ("recovery_rate", "recovery rate"),
        ("max_retries", "retry budget"),
        ("mean_recovery_latency_s", "mean recovery"),
        ("duplicate_mutations", "duplicate mutations"), ("loops_detected", "stuck loops"),
    ),
}

_SECOND_METRICS = frozenset(
    {"p50_s", "p95_s", "p99_s", "observed_p99_s", "tool_call_overhead_s",
     "mean_recovery_latency_s"}
)
_RATE_METRICS = frozenset(
    {"error_rate", "recovery_rate", "injection_rate", "error_rate_under_fault",
     "bloat_share", "crash_rate"}
)
_MONEY_METRICS = frozenset({"cost_per_request", "cost_per_1k_requests"})


def render_report(result: ScanResult) -> str:
    """The full markdown report."""
    lines: list[str] = [
        f"# RateMyAgent report — {result.target.name}",
        "",
        f"- **Scanned:** {result.started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Target:** `{result.target.uri or result.target.name}` "
        f"({result.target.kind})",
        f"- **Policy:** `{result.policy_name}` (pass score "
        f"{result.pass_score:g})" if result.pass_score is not None
        else f"- **Policy:** `{result.policy_name}`",
        f"- **Duration:** {format_seconds(result.duration_s)} across {len(result.probes)} probes",
        *_probe_call_lines(result),
        "",
        "## Verdict",
        "",
    ]
    lines.extend(f"> {line}" for line in verdict_lines(result))
    lines.append("")

    lines.extend(_actual_vs_target(result))
    lines.extend(_score_breakdown(result))

    for phase in _phases(result):
        title, blurb = PHASE_TITLES.get(phase, (phase.title(), ""))
        lines.extend([f"## {title}", "", blurb, ""] if blurb else [f"## {title}", ""])

        for probe in result.probes:
            if probe.phase == phase:
                lines.extend(_probe_section(probe))

    lines.extend(_appendix(result))
    return "\n".join(lines).rstrip() + "\n"


def _actual_vs_target(result: ScanResult) -> list[str]:
    rows = target_rows(result)
    if not rows:
        return []

    lines = [
        "## Actual vs target",
        "",
        "| measurement | actual | target | status |",
        "|---|---|---|---|",
    ]
    for row in rows:
        status = f"**{row.status}**" if row.status == "FAIL" else row.status
        mark = " ~" if row.caveated else ""
        lines.append(f"| {row.label} | {row.actual} | {row.target} | {status}{mark} |")
    lines.append("")
    lines.extend(_caveat_lines(result))
    return lines


def _caveat_lines(result: ScanResult) -> list[str]:
    """Caveats beside the table they qualify.

    The report is read start to finish rather than skimmed for a verdict, and it
    is one of the two artifacts handed to a coding agent -- so unlike the
    terminal scorecard, nothing is collapsed here. A model reading "recovery
    rate 25%" and reaching for the retry logic is the failure this exists to
    prevent, and it cannot pass `-v`.
    """
    caveats = result.caveats()
    if not caveats:
        return []

    labels = {check.metric: CHECK_LABELS.get(check.name, check.name)
              for check in result.checks}
    order = {"suppress": 0, "inapplicable": 1, "annotate": 2}

    lines = ["**What these numbers do not establish**", ""]
    for caveat in sorted(caveats, key=lambda c: (order.get(c.effect, 3), c.probe)):
        named = [labels[m] for m in caveat.metrics if m in labels]
        subject = ", ".join(named) if named else PROBE_TITLES.get(
            caveat.probe, caveat.probe.title()
        )
        text = f"**{subject}** -- {caveat.reason}"
        if caveat.remedy:
            text += f" Remedy: `{caveat.remedy}`."
        lines.append(f"- {text}")
    lines.append("")
    return lines


def _probe_call_lines(result: ScanResult) -> list[str]:
    """Which tool the probes actually called, and with what.

    Recorded because a saved report otherwise cannot tell you what it measured.
    A `server-memory` scan scored 100/100 on latency that turned out to be 20
    calls to `create_entities` with a synthesized `{"entities": []}` -- creating
    zero entities, sub-millisecond, full marks -- and establishing that required
    re-running the scan, because neither the report nor its state block recorded
    the call. Additive and cheap; the alternative is provenance you have to
    reconstruct.
    """
    metadata = result.target.metadata or {}
    tool = metadata.get("probe_tool")
    if not tool:
        return []

    lines = [f"- **Probe tool:** `{tool}`"]
    args = metadata.get("probe_args")
    if args:
        rendered = json.dumps(args, sort_keys=True)
        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        lines.append(f"- **Probe arguments:** `{rendered}`")
    return lines

def _score_breakdown(result: ScanResult) -> list[str]:
    rows = breakdown_rows(result)
    if not rows:
        return []

    lines = ["## Score breakdown", "", "| dimension | points | note |", "|---|---|---|"]
    for label, points, note in rows:
        lines.append(f"| {label} | {points} | {note or ''} |")

    if result.score is not None:
        note = result.cap_reason or ""
        lines.append(f"| **total** | **{result.score:.0f}/100** | {note} |")
    lines.append("")
    return lines


def _probe_section(probe: ProbeResult) -> list[str]:
    title = PROBE_TITLES.get(probe.probe, probe.probe.title())
    lines = [f"### {title}", ""]

    if probe.error:
        lines.extend([f"This probe failed to run: `{probe.error}`", ""])
        return lines

    if not probe.applicable:
        lines.extend([
            f"_Not applicable to this target: {probe.summary}._",
            "",
        ])
        if probe.findings:
            lines.extend([*(f"- {finding}" for finding in probe.findings), ""])
        return lines

    score = f"{probe.score:.0f}/100" if probe.score is not None else "not scored"
    lines.extend([f"{probe.summary}", "", f"**Score:** {score}", ""])

    rows = _highlight_rows(probe)
    if rows:
        lines.extend(["| metric | value |", "|---|---|"])
        lines.extend(f"| {label} | {value} |" for label, value in rows)
        lines.append("")

    extra = _extra_tables(probe)
    if extra:
        lines.extend(extra)

    if probe.caveats:
        lines.append("**Limits of this measurement**")
        lines.append("")
        for caveat in probe.caveats:
            remedy = f" Remedy: `{caveat.remedy}`." if caveat.remedy else ""
            lines.append(f"- {caveat.reason}{remedy}")
        lines.append("")

    if probe.findings:
        lines.append("**Findings**")
        lines.append("")
        lines.extend(f"- {finding}" for finding in probe.findings)
        lines.append("")

    return lines


def _highlight_rows(probe: ProbeResult) -> list[tuple[str, str]]:
    rows = []
    for key, label in HIGHLIGHTS.get(probe.probe, ()):
        value = probe.metrics.get(key)
        if value is None:
            continue
        rows.append((label, _format_metric(key, value)))
    return rows


def _extra_tables(probe: ProbeResult) -> list[str]:
    """Per-probe detail that does not fit the flat metric table."""
    lines: list[str] = []

    if probe.probe == "concurrency" and probe.metrics.get("levels"):
        lines.extend([
            "| concurrency | error rate | p95 | goodput |",
            "|---|---|---|---|",
        ])
        for level in probe.metrics["levels"]:
            p95 = format_seconds(level["p95_s"]) if level.get("p95_s") else "-"
            goodput = (
                f"{level['throughput_rps']:.1f}/s" if level.get("throughput_rps") else "-"
            )
            lines.append(
                f"| {level['concurrency']} | {level['error_rate']:.1%} | {p95} | {goodput} |"
            )
        lines.append("")

    if probe.probe == "contract" and probe.metrics.get("outcome_by_case"):
        lines.extend(["| edge case | worst outcome |", "|---|---|"])
        for case, outcome in probe.metrics["outcome_by_case"].items():
            mark = "**crashed**" if outcome == "crashed" else outcome
            lines.append(f"| {case} | {mark} |")
        lines.append("")

        issues = probe.metrics.get("schema_issues") or []
        if issues:
            lines.append("**Schema issues**")
            lines.append("")
            lines.extend(f"- {issue}" for issue in issues)
            lines.append("")

    if probe.probe == "fault" and probe.metrics.get("injected_by_kind"):
        lines.extend(["| fault kind | injected |", "|---|---|"])
        for kind, count in sorted(
            probe.metrics["injected_by_kind"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"| {kind} | {count} |")
        lines.append("")

    if probe.probe == "behavior" and probe.metrics.get("final_status_counts"):
        lines.extend(["| final status | operations |", "|---|---|"])
        for status, count in probe.metrics["final_status_counts"].items():
            lines.append(f"| {status} | {count} |")
        lines.append("")

    if probe.probe == "latency" and probe.metrics.get("errors_by_kind"):
        lines.extend(["| error kind | count |", "|---|---|"])
        for kind, count in sorted(
            probe.metrics["errors_by_kind"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"| {kind} | {count} |")
        lines.append("")

    return lines


def _format_metric(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, (int, float)):
        return str(value)
    if key in _SECOND_METRICS:
        return format_seconds(value)
    if key in _RATE_METRICS:
        return f"{value:.1%}"
    if key in _MONEY_METRICS:
        return f"${value:.4f}"
    if key == "retry_amplification":
        return f"{value:.2f}x"
    if key == "tail_ratio":
        return f"{value:.1f}x"
    if key == "peak_throughput_rps":
        return f"{value:.1f}/s"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _appendix(result: ScanResult) -> list[str]:
    config = result.config or {}
    lines = ["## How this scan was run", "", "| setting | value |", "|---|---|"]
    for key in ("requests", "concurrency", "warmup", "timeout_s", "seed", "phases"):
        if key in config:
            value = config[key]
            lines.append(
                f"| {key} | {', '.join(value) if isinstance(value, list) else value} |"
            )

    fault = result.probe("fault")
    if fault and fault.metrics.get("faults"):
        rate = fault.metrics["faults"].get("total_rate")
        if rate is not None:
            lines.append(f"| fault rate | {rate:.0%} |")

    lines.extend([
        "",
        "Reproduce with the same `--seed` to get the same run: fault injection is "
        "seeded per operation and attempt, so a scan replays exactly.",
        "",
    ])
    return lines


def _phases(result: ScanResult) -> list[str]:
    from ..probes import PHASES

    present = {probe.phase for probe in result.probes}
    return [phase for phase in PHASES if phase in present] + sorted(
        present - set(PHASES)
    )
