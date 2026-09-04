"""ConcurrencyTester: phase 1, ramp 1 -> N and find where it breaks.

Sends a fixed batch of requests at each concurrency level, doubling until the
ceiling. The saturation point is the first level where the error rate crosses
5% -- the level at which adding traffic stops buying throughput and starts
buying failures.

Two things a naive load test gets wrong and this one does not:

- Latency at level 1 is the reference. Reporting "p95 was 9s at level 32" means
  nothing without "p95 was 0.3s at level 1" next to it.
- A target can saturate without erroring, by simply getting slower. That is why
  a latency knee is reported even when the error rate never crosses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..models import ProbeResult, Response
from .base import Probe, ProbeConfig, ScanContext, percentile

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

#: The error rate that defines saturation, from the spec.
SATURATION_ERROR_RATE = 0.05

#: A level whose p95 exceeds this multiple of the level-1 p95 has hit a knee.
LATENCY_KNEE_MULTIPLE = 3.0

class ConcurrencyTester(Probe):
    """Ramps concurrency and reports where the target stops coping."""

    name = "concurrency"
    description = "ramps concurrency 1->N and finds the saturation point"
    phase = "baseline"

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        levels = _ladder(config.concurrency)
        per_level = max(1, config.extra.get("requests_per_level", config.requests))

        results: list[dict[str, Any]] = []
        offset = 0
        for level in levels:
            requests = target.probe_requests(per_level, offset=offset)
            offset += per_level

            measurement = await _run_level(target, requests, level, config)
            results.append(measurement)
            logger.info(
                "concurrency %d: %.1f%% errors, p95 %.3fs",
                level,
                measurement["error_rate"] * 100,
                measurement["p95_s"] or 0.0,
            )

            # Once it is failing outright, higher levels only cost time.
            if measurement["error_rate"] >= 0.5:
                logger.info("stopping ramp early at level %d", level)
                break

        metrics = _compute_metrics(results, levels)

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            sample_count=sum(r["requests"] for r in results),
            error_rate=metrics["max_error_rate"],
            duration_s=time.perf_counter() - started,
        )


def _ladder(ceiling: int) -> list[int]:
    """1, 2, 4, 8 ... up to and including the ceiling."""
    if ceiling < 1:
        raise ValueError("concurrency ceiling must be at least 1")

    levels = []
    level = 1
    while level < ceiling:
        levels.append(level)
        level *= 2
    levels.append(ceiling)
    return sorted(set(levels))


async def _run_level(
    target: "Target", requests: list[Any], level: int, config: ProbeConfig
) -> dict[str, Any]:
    """Send `requests` with at most `level` in flight at once."""
    semaphore = asyncio.Semaphore(level)

    async def one(request: Any) -> Response:
        if request.timeout_s is None:
            request.timeout_s = config.timeout_s
        async with semaphore:
            started = time.perf_counter()
            try:
                return await target.invoke(request)
            except Exception as exc:
                from ..targets.base import error_response

                return error_response(exc, time.perf_counter() - started)

    wall_start = time.perf_counter()
    responses = list(await asyncio.gather(*(one(request) for request in requests)))
    wall = time.perf_counter() - wall_start

    successes = [r for r in responses if r.ok]
    latencies = [r.latency_s for r in successes]
    failures = len(responses) - len(successes)

    # Throughput from Little's law (concurrency / service time) rather than from
    # wall clock. Wall clock measures how fast the harness got through the
    # batch, which against a target reporting simulated latency is a number
    # about this machine, not about the target.
    mean_latency = (sum(latencies) / len(latencies)) if latencies else None
    # Goodput, not throughput: scaled by the success rate so a level that is
    # mostly erroring cannot post the best number by failing quickly.
    success_rate = (len(successes) / len(responses)) if responses else 0.0
    throughput = (level / mean_latency * success_rate) if mean_latency else None

    return {
        "concurrency": level,
        "requests": len(responses),
        "successes": len(successes),
        "failures": failures,
        "error_rate": failures / len(responses) if responses else 1.0,
        "p50_s": percentile(latencies, 50),
        "p95_s": percentile(latencies, 95),
        "mean_s": mean_latency,
        "wall_s": wall,
        "throughput_rps": throughput,
    }


def _compute_metrics(results: list[dict[str, Any]], levels: list[int]) -> dict[str, Any]:
    saturation = next(
        (r["concurrency"] for r in results if r["error_rate"] > SATURATION_ERROR_RATE),
        None,
    )

    clean = [r["concurrency"] for r in results if r["error_rate"] <= SATURATION_ERROR_RATE]
    max_sustained = max(clean) if clean else 0

    baseline_p95 = results[0]["p95_s"] if results else None
    knee = None
    if baseline_p95:
        knee = next(
            (
                r["concurrency"]
                for r in results
                if r["p95_s"] and r["p95_s"] > baseline_p95 * LATENCY_KNEE_MULTIPLE
            ),
            None,
        )

    throughputs = [r["throughput_rps"] for r in results if r["throughput_rps"]]

    return {
        "levels_tested": [r["concurrency"] for r in results],
        "levels_planned": levels,
        "saturation_point": saturation,
        "max_sustained_concurrency": max_sustained,
        "latency_knee_at": knee,
        "baseline_p95_s": baseline_p95,
        "peak_throughput_rps": max(throughputs) if throughputs else None,
        "max_error_rate": max((r["error_rate"] for r in results), default=1.0),
        "saturation_threshold": SATURATION_ERROR_RATE,
        "levels": results,
    }


def _summarize(metrics: dict[str, Any]) -> str:
    sustained = metrics["max_sustained_concurrency"]
    saturation = metrics["saturation_point"]

    if saturation is None:
        top = metrics["levels_tested"][-1] if metrics["levels_tested"] else 0
        return f"no saturation up to {top} concurrent, sustained {sustained}"
    return f"saturates at {saturation} concurrent, sustained {sustained}"


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    sustained = metrics["max_sustained_concurrency"]
    saturation = metrics["saturation_point"]
    threshold = int(SATURATION_ERROR_RATE * 100)

    if sustained == 0:
        findings.append(
            "The target exceeded the error threshold at a single concurrent request. "
            "This is not a concurrency limit; something is broken at any load."
        )
        return findings

    if saturation is not None:
        level = next(r for r in metrics["levels"] if r["concurrency"] == saturation)
        findings.append(
            f"Saturation point is {saturation} concurrent requests, where the error rate "
            f"reached {level['error_rate']:.0%} (threshold {threshold}%). "
            f"The highest level it handled cleanly was {sustained}."
        )
    else:
        top = metrics["levels_tested"][-1]
        findings.append(
            f"No saturation found up to {top} concurrent requests, the configured ceiling. "
            f"The real limit is above {top}, so this is a floor set by the test, not a "
            "measurement of the target -- raise --concurrency to find the actual limit."
        )

    knee = metrics["latency_knee_at"]
    if knee is not None:
        level = next(r for r in metrics["levels"] if r["concurrency"] == knee)
        findings.append(
            f"Latency knee at {knee} concurrent: p95 rose to {level['p95_s']:.2f}s from "
            f"{metrics['baseline_p95_s']:.2f}s at a single request "
            f"({level['p95_s'] / metrics['baseline_p95_s']:.1f}x). A target can saturate by "
            "getting slow rather than by failing, and this one does."
        )

    peak = metrics["peak_throughput_rps"]
    if peak:
        best = max(metrics["levels"], key=lambda r: r["throughput_rps"] or 0)
        findings.append(
            f"Peak goodput is {peak:.1f} successful req/s at {best['concurrency']} "
            "concurrent. Past that, added concurrency buys latency and errors rather "
            "than completed work."
        )

    if len(metrics["levels_tested"]) < len(metrics["levels_planned"]):
        findings.append(
            f"The ramp stopped early at {metrics['levels_tested'][-1]} concurrent; "
            "the target was failing more than half of all requests, so higher levels "
            "would only have measured how fast it can refuse."
        )

    return findings
