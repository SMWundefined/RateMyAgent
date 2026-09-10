"""LatencyProfiler: end-to-end latency distribution, TTFT, and call overhead."""

from __future__ import annotations

import logging
import statistics
import time
from typing import TYPE_CHECKING, Any

from ..formatting import format_seconds
from ..models import Caveat, ProbeResult, Response
from .base import Probe, ProbeConfig, ScanContext, percentile

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

class LatencyProfiler(Probe):
    """Send N requests one at a time and profile the result.

    Sequential on purpose: this probe answers "how slow is one call when
    nothing else is in flight". Concurrent load is the load tester's question,
    and mixing the two produces a latency profile that describes neither.
    """

    name = "latency"
    description = "p50/p95/p99 end-to-end latency, TTFT, and tool call overhead"
    phase = "baseline"
    # Latency under fault against latency clean is the core phase 1/2 comparison.
    rerun_under_fault = True

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        if config.warmup > 0:
            for request in target.probe_requests(config.warmup):
                await self._safe_invoke(target, request, config)

        responses: list[Response] = []
        for request in target.probe_requests(config.requests, offset=config.warmup):
            responses.append(await self._safe_invoke(target, request, config))

        metrics = _compute_metrics(responses)
        duration = time.perf_counter() - started

        # Phase 3 needs the *baseline* error rate to know whether recovery was
        # measurable at all: nothing can recover when nothing worked to begin
        # with. Recorded only on the unfaulted run -- the fault phase reruns this
        # probe, and its error rate is the injection, not the target.
        if context is not None and self.phase == "baseline":
            context.artifacts.setdefault("baseline_error_rate", metrics["error_rate"])

        return ProbeResult(
            probe=self.name,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics, config),
            caveats=_caveats(metrics) + _degraded_path_caveats(target, metrics),
            sample_count=len(responses),
            error_rate=metrics["error_rate"],
            duration_s=duration,
        )

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
        # Reported as `observed_p99_s` and scored as `p99_s`, and below
        # P99_MIN_SAMPLE the second is None. See the derivation on P99_MIN_SAMPLE.
        "observed_p99_s": percentile(latencies, 99),
        "p99_s": (
            percentile(latencies, 99) if len(latencies) >= P99_MIN_SAMPLE else None
        ),
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

    if metrics["p50_s"] and metrics["observed_p99_s"]:
        metrics["tail_ratio"] = metrics["observed_p99_s"] / metrics["p50_s"]
    else:
        metrics["tail_ratio"] = None

    return metrics


def _summarize(metrics: dict[str, Any]) -> str:
    if metrics["p95_s"] is None:
        return f"all {metrics['requests']} requests failed"
    return (
        f"p50 {format_seconds(metrics['p50_s'])}, p95 {format_seconds(metrics['p95_s'])}, "
        f"p99 {format_seconds(metrics['p99_s'])} over {metrics['requests']} requests "
        f"({metrics['error_rate']:.1%} errors)"
    )


#: Below this many successful samples, p99 is reported but not scored.
#:
#: **100 is exact, not a rule of thumb, and it follows from nearest-rank.**
#: `percentile()` uses nearest rank deliberately, so a reported p99 is a request
#: that actually happened rather than an interpolated invention. Nearest rank
#: for percentile *p* over *n* samples is `ceil(n*p/100)`, so:
#:
#:     n=20:   p99 = rank 20 of 20  -- the maximum
#:     n=99:   p99 = rank 99 of 99  -- still the maximum
#:     n=100:  p99 = rank 99 of 100 -- the first n where it is not
#:
#: The smallest n satisfying `ceil(0.99n) < n` is exactly 100. Below it, "p99"
#: *is* the sample maximum by construction, and the maximum of n samples is a
#: point estimate of the `n/(n+1)` quantile -- so at the default 20 requests the
#: number scored against a 10-second p99 threshold is an estimate of the **95th**
#: percentile, two ranks from its own name.
#:
#: Not scored rather than warned about. A number that cannot be measured leaves
#: the denominator, which is what this project already does for cost without a
#: price, concurrency with no threshold reading it, recovery at 100% baseline
#: error, and duplicate mutations over zero operations. Warning instead would
#: put a caveat on nearly every scan, and a note that fires every time carries
#: no signal.
#:
#: The check this replaced fired below **20** while its own text said "not
#: meaningful below ~100" -- so every default scan scored p99 from the maximum of
#: twenty samples and was told nothing. The guard disagreed with its own stated
#: criterion.
P99_MIN_SAMPLE = 100


def _degraded_path_caveats(target: "Target", metrics: dict[str, Any]) -> list[Caveat]:
    """The server said something on the way up, and answered anyway.

    A target that *fails* to start is loud and gets a `TargetError`. A target
    that **degrades** is silent in every number this scan produces: it connects,
    serves a smaller or different surface, answers every call, and scores well
    on a code path nobody asked for.

    Third instance. `pypi-query-mcp-server` scored 98/100 measuring the wrong
    code path; `htag` reported a strictness figure over tools it was not really
    exercising; and `firecrawl-mcp` prints a keyless-mode banner and then serves
    a reduced tool set perfectly happily -- 25 tools rather than 27, and the
    difference is invisible unless you already knew to look.

    So the general form, rather than a check for any one of them: **the server
    wrote to stderr while connecting, and the baseline still succeeded.** Both
    halves matter. Output with a failed baseline is just the error, already
    reported; output with a working baseline is the case where nothing else will
    ever mention it.

    A caveat and never a failure. Servers write to stderr for entirely ordinary
    reasons -- version notices, npm chatter, log lines -- and grading a target
    on its logging would be its own bad measurement. This says "look", not
    "wrong".
    """
    text = getattr(target, "setup_stderr", "")
    if not text or metrics.get("error_rate") == 1.0:
        return []

    first = " ".join(text.split())[:220]
    return [Caveat(
        probe="latency",
        metrics=(),
        scope="probe",
        effect="annotate",
        reason=(
            "The server wrote to stderr while starting up and then answered "
            "normally, so this scan may be measuring a degraded path rather "
            f"than the one you meant to test. It said: {first}"
        ),
        remedy="--env for a stdio server's credentials, or --header for http/sse",
    )]


def _caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """Limits of this profile, kept out of the findings list."""
    caveats: list[Caveat] = []
    requests = metrics["requests"]

    if metrics["error_rate"] == 1.0:
        caveats.append(Caveat(
            probe="latency", metrics=("p95_s", "p99_s"), effect="suppress",
            reason=(
                f"All {requests} requests failed, so there is no latency "
                "distribution to report."
            ),
            remedy="--tool-args, or a target that answers",
        ))
        return caveats

    if metrics.get("p99_s") is None and metrics.get("observed_p99_s") is not None:
        samples = metrics["successes"]
        caveats.append(Caveat(
            probe="latency", metrics=("p99_s",), effect="suppress",
            reason=(
                f"p99 is the maximum of {samples} samples, which estimates the "
                f"{samples / (samples + 1):.0%} percentile rather than the 99th. "
                f"Nearest-rank p99 is the maximum for any sample below "
                f"{P99_MIN_SAMPLE}, so it is reported and not scored."
            ),
            remedy=f"--requests {P99_MIN_SAMPLE}",
        ))

    if not metrics["failures"]:
        bound = 3 / requests
        caveats.append(Caveat(
            probe="latency", metrics=("error_rate",), effect="annotate",
            reason=(
                f"Zero failures in {requests} requests bounds the error rate at "
                f"roughly {bound:.0%} with 95% confidence, not at 0%."
            ),
            remedy="--requests",
        ))

    return caveats


def _findings(metrics: dict[str, Any], config: ProbeConfig) -> list[str]:
    findings: list[str] = []

    if metrics["p95_s"] is None:
        findings.append(
            f"Every one of the {metrics['requests']} requests failed."
        )
        findings.extend(_error_findings(metrics))
        return findings

    p95 = metrics["p95_s"]
    if p95 >= 30.0:
        findings.append(
            f"p95 latency is {format_seconds(p95)}. Anything with a 30s client timeout in front of "
            "this target will read one call in twenty as a hard failure."
        )

    tail_ratio = metrics["tail_ratio"]
    if tail_ratio and tail_ratio >= 3.0:
        findings.append(
            f"Heavy tail: p99 ({format_seconds(metrics['observed_p99_s'])}) is "
            f"{tail_ratio:.1f}x p50 ({format_seconds(metrics['p50_s'])}). Investigate "
            "retries, cold starts, or lock contention before optimizing the median."
        )

    overhead = metrics["tool_call_overhead_s"]
    server_p50 = metrics["server_time_p50_s"]
    if overhead is not None and server_p50 and overhead > 0.2 * server_p50:
        findings.append(
            f"Tool call overhead is {format_seconds(overhead)} on top of "
            f"{format_seconds(server_p50)} of reported execution time. That gap is "
            "transport and serialization, not work."
        )

    if metrics["ttft_p95_s"] is not None and metrics["p95_s"]:
        ttft_share = metrics["ttft_p95_s"] / metrics["p95_s"]
        if ttft_share > 0.5:
            findings.append(
                f"p95 TTFT is {format_seconds(metrics['ttft_p95_s'])}, "
                f"{ttft_share:.0%} of total latency. Most of the wait is before the "
                "first token, so streaming will not hide it."
            )

    findings.extend(_error_findings(metrics))

    if not findings:
        # Deliberately not "no problems found": the probe measures, the policy
        # decides. Claiming health here would contradict a FAILED policy check
        # sitting directly above it in the scorecard.
        clean = (
            f"p95 {format_seconds(p95)} and {metrics['error_rate']:.1%} errors across "
            f"{metrics['requests']} requests, with no heavy tail, no unusual call "
            "overhead, and no error pattern to report."
        )
        # The confidence bound that used to be glued on here is a caveat about
        # the sample, not a finding about the target: see _caveats().
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
