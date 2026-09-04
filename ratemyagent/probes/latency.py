"""LatencyProfiler: end-to-end latency distribution, TTFT, and call overhead."""

from __future__ import annotations

import logging
import statistics
import time
from typing import TYPE_CHECKING, Any

from ..models import Grade, ProbeResult, Response
from .base import Probe, ProbeConfig, percentile

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

# (max p95 seconds, max error rate) for each grade, best first.
GRADE_THRESHOLDS: tuple[tuple[Grade, float, float], ...] = (
    (Grade.A, 2.0, 0.01),
    (Grade.B, 5.0, 0.03),
    (Grade.C, 10.0, 0.05),
    (Grade.D, 30.0, 0.10),
)


class LatencyProfiler(Probe):
    """Send N requests one at a time and profile the result.

    Sequential on purpose: this probe answers "how slow is one call when
    nothing else is in flight". Concurrent load is the load tester's question,
    and mixing the two produces a latency profile that describes neither.
    """

    name = "latency"
    description = "p50/p95/p99 end-to-end latency, TTFT, and tool call overhead"
    phase = "baseline"

    async def run(self, target: "Target", config: ProbeConfig) -> ProbeResult:
        started = time.perf_counter()

        if config.warmup > 0:
            for request in target.probe_requests(config.warmup):
                await self._safe_invoke(target, request, config)

        responses: list[Response] = []
        for request in target.probe_requests(config.requests, offset=config.warmup):
            responses.append(await self._safe_invoke(target, request, config))

        metrics = _compute_metrics(responses)
        duration = time.perf_counter() - started

        return ProbeResult(
            probe=self.name,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics, config),
            sample_count=len(responses),
            error_rate=metrics["error_rate"],
            duration_s=duration,
        )

    def grade(self, result: ProbeResult) -> Grade:
        p95 = result.metrics.get("p95_s")
        error_rate = result.metrics.get("error_rate", 1.0)

        if p95 is None:
            return Grade.F

        return Grade.worst([_grade_p95(p95), _grade_error_rate(error_rate)])

    async def _safe_invoke(
        self, target: "Target", request: "Any", config: ProbeConfig
    ) -> Response:
        if request.timeout_s is None:
            request.timeout_s = config.timeout_s

        started = time.perf_counter()
        try:
            return await target.invoke(request)
        except Exception as exc:
            # A well-behaved target returns a failed Response instead of raising,
            # but one bad adapter should not end the profile.
            from ..targets.base import error_response

            logger.debug("target raised during invoke: %s", exc)
            return error_response(exc, time.perf_counter() - started)


def _compute_metrics(responses: list[Response]) -> dict[str, Any]:
    total = len(responses)
    successes = [r for r in responses if r.ok]
    failures = [r for r in responses if not r.ok]

    latencies = [r.latency_s for r in successes]
    ttfts = [r.ttft_s for r in successes if r.ttft_s is not None]
    server_times = [r.server_time_s for r in successes if r.server_time_s is not None]

    errors_by_kind: dict[str, int] = {}
    for failure in failures:
        key = failure.error_kind.value if failure.error_kind else "unknown"
        errors_by_kind[key] = errors_by_kind.get(key, 0) + 1

    metrics: dict[str, Any] = {
        "requests": total,
        "successes": len(successes),
        "failures": len(failures),
        "error_rate": (len(failures) / total) if total else 1.0,
        "p50_s": percentile(latencies, 50),
        "p95_s": percentile(latencies, 95),
        "p99_s": percentile(latencies, 99),
        "min_s": min(latencies) if latencies else None,
        "max_s": max(latencies) if latencies else None,
        "mean_s": statistics.fmean(latencies) if latencies else None,
        "stdev_s": statistics.stdev(latencies) if len(latencies) > 1 else None,
        "ttft_p50_s": percentile(ttfts, 50),
        "ttft_p95_s": percentile(ttfts, 95),
        "errors_by_kind": errors_by_kind,
    }

    # Overhead is only honest when the target reports its own execution time;
    # otherwise everything measured is transport plus work, indivisible.
    server_p50 = percentile(server_times, 50)
    if server_p50 is not None and metrics["p50_s"] is not None:
        metrics["server_time_p50_s"] = server_p50
        metrics["tool_call_overhead_s"] = max(0.0, metrics["p50_s"] - server_p50)
    else:
        metrics["server_time_p50_s"] = None
        metrics["tool_call_overhead_s"] = None

    if metrics["p50_s"] and metrics["p99_s"]:
        metrics["tail_ratio"] = metrics["p99_s"] / metrics["p50_s"]
    else:
        metrics["tail_ratio"] = None

    return metrics


def _summarize(metrics: dict[str, Any]) -> str:
    if metrics["p95_s"] is None:
        return f"all {metrics['requests']} requests failed"
    return (
        f"p50 {metrics['p50_s']:.2f}s, p95 {metrics['p95_s']:.2f}s, "
        f"p99 {metrics['p99_s']:.2f}s over {metrics['requests']} requests "
        f"({metrics['error_rate']:.1%} errors)"
    )


def _findings(metrics: dict[str, Any], config: ProbeConfig) -> list[str]:
    findings: list[str] = []

    if metrics["p95_s"] is None:
        findings.append(
            f"Every one of the {metrics['requests']} requests failed. "
            "No latency distribution could be measured."
        )
        findings.extend(_error_findings(metrics))
        return findings

    p95 = metrics["p95_s"]
    if p95 >= 30.0:
        findings.append(f"p95 latency is {p95:.1f}s, at or past the 30s floor for a D.")
    elif p95 >= 2.0:
        target_grade, target_p95 = next(
            (grade, limit) for grade, limit, _ in GRADE_THRESHOLDS if p95 < limit
        )
        findings.append(
            f"p95 latency is {p95:.2f}s, which grades {target_grade.value} "
            f"(under {target_p95:.0f}s). An A needs p95 under 2s."
        )

    tail_ratio = metrics["tail_ratio"]
    if tail_ratio and tail_ratio >= 3.0:
        findings.append(
            f"Heavy tail: p99 ({metrics['p99_s']:.2f}s) is {tail_ratio:.1f}x p50 "
            f"({metrics['p50_s']:.2f}s). Investigate retries, cold starts, or lock contention "
            "before optimizing the median."
        )

    overhead = metrics["tool_call_overhead_s"]
    server_p50 = metrics["server_time_p50_s"]
    if overhead is not None and server_p50 and overhead > 0.2 * server_p50:
        findings.append(
            f"Tool call overhead is {overhead * 1000:.0f}ms on top of {server_p50:.2f}s "
            "of reported execution time. That gap is transport and serialization, not work."
        )

    if metrics["ttft_p95_s"] is not None and metrics["p95_s"]:
        ttft_share = metrics["ttft_p95_s"] / metrics["p95_s"]
        if ttft_share > 0.5:
            findings.append(
                f"p95 TTFT is {metrics['ttft_p95_s']:.2f}s, {ttft_share:.0%} of total latency. "
                "Most of the wait is before the first token, so streaming will not hide it."
            )

    findings.extend(_error_findings(metrics))

    if metrics["requests"] < 20:
        findings.append(
            f"Only {metrics['requests']} requests sampled; p99 is not meaningful below ~100. "
            "Re-run with --requests 100 before trusting the tail."
        )

    if not findings:
        clean = (
            f"No latency problems found: p95 {p95:.2f}s and "
            f"{metrics['error_rate']:.1%} errors across {metrics['requests']} requests."
        )
        # Zero observed failures is an upper bound, not a measurement. The rule
        # of three puts the 95% bound at 3/n, which at default sample sizes is
        # far looser than the 0% shown above.
        if not metrics["failures"] and metrics["requests"] < 100:
            bound = 3 / metrics["requests"]
            clean += (
                f" Note that zero failures in {metrics['requests']} requests only bounds the"
                f" error rate at roughly {bound:.0%} (95% confidence), not 0%."
                " Raise --requests to tighten it."
            )
        findings.append(clean)

    return findings


def _error_findings(metrics: dict[str, Any]) -> list[str]:
    if not metrics["failures"]:
        return []

    breakdown = ", ".join(
        f"{count} {kind}" for kind, count in sorted(
            metrics["errors_by_kind"].items(), key=lambda item: -item[1]
        )
    )
    return [
        f"{metrics['failures']}/{metrics['requests']} requests failed "
        f"({metrics['error_rate']:.1%}): {breakdown}."
    ]


def _grade_p95(p95: float) -> Grade:
    for grade, max_p95, _ in GRADE_THRESHOLDS:
        if p95 < max_p95:
            return grade
    return Grade.F


def _grade_error_rate(error_rate: float) -> Grade:
    for grade, _, max_error_rate in GRADE_THRESHOLDS:
        if error_rate < max_error_rate:
            return grade
    return Grade.F
