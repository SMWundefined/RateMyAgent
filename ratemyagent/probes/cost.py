"""CostAnalyzer: phase 1, token usage and what it projects to in dollars.

Three questions:

1. How many tokens does a request actually cost?
2. How much of every request is the same bytes resent -- prompt bloat?
3. What does that come to per request, per thousand, per million?

Cost is only projected when the model's price is known. An invented number here
would be worse than none: it would be quoted in a budget.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import TYPE_CHECKING, Any

from ..models import ProbeResult, Response
from .base import Probe, ProbeConfig, ScanContext, percentile

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

#: USD per 1M tokens, (input, output). Anthropic first-party API rates.
#: Bedrock and Vertex are partner-operated and priced separately.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Cache reads cost roughly a tenth of a fresh input token; writes about 1.25x.
CACHE_READ_MULTIPLIER = 0.1

#: A fixed prefix at or above this share of input tokens is worth reporting.
BLOAT_SHARE_THRESHOLD = 0.5
#: ...but only once it is large enough that caching it would actually pay.
BLOAT_MIN_TOKENS = 1024


class CostAnalyzer(Probe):
    """Token usage, prompt bloat, and a dollar projection."""

    name = "cost"
    description = "token usage per request, prompt bloat detection, $/request projection"
    phase = "baseline"

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        responses: list[Response] = []
        for request in target.probe_requests(config.requests):
            if request.timeout_s is None:
                request.timeout_s = config.timeout_s
            try:
                responses.append(await target.invoke(request))
            except Exception as exc:
                from ..targets.base import error_response

                logger.debug("target raised during invoke: %s", exc)
                responses.append(error_response(exc, 0.0))

        pricing = _pricing_for(target, config)
        metrics = _compute_metrics(responses, pricing)

        # Without token usage there is nothing to cost; without a price there is
        # no dollar figure. Either way the probe reports what it saw but is left
        # out of the score rather than counted as a failure.
        applicable = bool(metrics["reported_usage"]) and metrics["cost_per_request"] is not None

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            applicable=applicable,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            sample_count=len(responses),
            error_rate=metrics["error_rate"],
            duration_s=time.perf_counter() - started,
        )


def _pricing_for(target: "Target", config: ProbeConfig) -> dict[str, Any]:
    """Resolve input/output prices, from config override or the model table."""
    extra = config.extra
    if extra.get("price_in") is not None and extra.get("price_out") is not None:
        return {
            "source": "override",
            "model": extra.get("model"),
            "input_per_1m": float(extra["price_in"]),
            "output_per_1m": float(extra["price_out"]),
        }

    info = target.describe()
    model = extra.get("model") or info.metadata.get("model")
    if model and model in ANTHROPIC_PRICING:
        price_in, price_out = ANTHROPIC_PRICING[model]
        return {
            "source": "table",
            "model": model,
            "input_per_1m": price_in,
            "output_per_1m": price_out,
        }

    return {"source": None, "model": model, "input_per_1m": None, "output_per_1m": None}


def _compute_metrics(responses: list[Response], pricing: dict[str, Any]) -> dict[str, Any]:
    total = len(responses)
    successes = [r for r in responses if r.ok]
    failures = total - len(successes)

    inputs = [r.tokens_in for r in successes if r.tokens_in is not None]
    outputs = [r.tokens_out for r in successes if r.tokens_out is not None]

    metrics: dict[str, Any] = {
        "requests": total,
        "successes": len(successes),
        "failures": failures,
        "error_rate": (failures / total) if total else 1.0,
        "reported_usage": bool(inputs or outputs),
        "pricing_source": pricing["source"],
        "model": pricing["model"],
        "input_price_per_1m": pricing["input_per_1m"],
        "output_price_per_1m": pricing["output_per_1m"],
    }

    if not metrics["reported_usage"]:
        metrics.update(
            {
                "mean_input_tokens": None,
                "mean_output_tokens": None,
                "cost_per_request": None,
                "bloat_detected": False,
            }
        )
        return metrics

    mean_in = statistics.fmean(inputs) if inputs else 0.0
    mean_out = statistics.fmean(outputs) if outputs else 0.0

    metrics.update(
        {
            "mean_input_tokens": mean_in,
            "mean_output_tokens": mean_out,
            "p95_input_tokens": percentile(inputs, 95),
            "min_input_tokens": min(inputs) if inputs else None,
            "max_input_tokens": max(inputs) if inputs else None,
            "total_input_tokens": sum(inputs),
            "total_output_tokens": sum(outputs),
            # Above ~10:1 the request is mostly context and barely any answer.
            "io_ratio": (mean_in / mean_out) if mean_out else None,
        }
    )
    metrics.update(_bloat_metrics(inputs, mean_in))
    metrics.update(_cost_metrics(metrics, pricing))
    return metrics


def _bloat_metrics(inputs: list[int], mean_in: float) -> dict[str, Any]:
    """Estimate the fixed prefix every request carries.

    The smallest input seen is an upper bound on what is constant: no request
    can carry less than the shared prefix. It is an estimate, not a
    measurement -- naming it as such matters, because the recommendation it
    drives (cache the prefix) is only worth acting on if it is roughly right.
    """
    if not inputs:
        return {"bloat_detected": False, "static_prefix_tokens": None, "bloat_share": None}

    static = min(inputs)
    share = (static / mean_in) if mean_in else 0.0
    detected = share >= BLOAT_SHARE_THRESHOLD and static >= BLOAT_MIN_TOKENS

    return {
        "static_prefix_tokens": static,
        "variable_tokens": mean_in - static,
        "bloat_share": share,
        "bloat_detected": detected,
    }


def _cost_metrics(metrics: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any]:
    price_in, price_out = pricing["input_per_1m"], pricing["output_per_1m"]
    if price_in is None or price_out is None:
        return {
            "cost_per_request": None,
            "cost_per_1k_requests": None,
            "cacheable_savings_per_request": None,
        }

    cost = (
        metrics["mean_input_tokens"] * price_in
        + metrics["mean_output_tokens"] * price_out
    ) / 1_000_000

    savings = None
    static = metrics.get("static_prefix_tokens")
    if static:
        # Caching the fixed prefix drops it to ~10% of the input rate.
        savings = (static * price_in * (1 - CACHE_READ_MULTIPLIER)) / 1_000_000

    return {
        "cost_per_request": cost,
        "cost_per_1k_requests": cost * 1_000,
        "cost_per_1m_requests": cost * 1_000_000,
        "cacheable_savings_per_request": savings,
    }


def _summarize(metrics: dict[str, Any]) -> str:
    if not metrics["reported_usage"]:
        return f"target reported no token usage across {metrics['requests']} requests"

    tokens = (
        f"{metrics['mean_input_tokens']:.0f} in / "
        f"{metrics['mean_output_tokens']:.0f} out tokens per request"
    )
    cost = metrics["cost_per_request"]
    if cost is None:
        return f"{tokens}, no price known for this model"
    return f"{tokens}, ${cost:.4f}/req, ${metrics['cost_per_1k_requests']:.2f}/1k"


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    if not metrics["reported_usage"]:
        findings.append(
            "The target reported no token usage, so cost cannot be measured. "
            "MCP servers do not report tokens; this probe is meaningful for LLM targets."
        )
        return findings

    if metrics["cost_per_request"] is None:
        findings.append(
            f"No published price for model {metrics['model'] or 'unknown'}, so token counts "
            "are reported without a dollar projection. Pass --price-in and --price-out to "
            "project cost yourself rather than have one guessed."
        )

    if metrics["bloat_detected"]:
        findings.append(
            f"Prompt bloat: about {metrics['static_prefix_tokens']} of "
            f"{metrics['mean_input_tokens']:.0f} input tokens "
            f"({metrics['bloat_share']:.0%}) are identical on every request. "
            "That prefix is a prompt-caching candidate."
        )
        savings = metrics.get("cacheable_savings_per_request")
        if savings:
            findings.append(
                f"Caching that prefix would save roughly ${savings:.4f} per request "
                f"(${savings * 1000:.2f} per 1k), since cache reads bill at about 10% "
                "of the input rate."
            )

    ratio = metrics.get("io_ratio")
    if ratio and ratio > 10:
        findings.append(
            f"Input is {ratio:.0f}x output. The request is mostly context and very little "
            "answer, so cost is dominated by what you send, not what you get back."
        )

    cost = metrics["cost_per_request"]
    if cost is not None and cost >= 0.01:
        findings.append(
            f"At ${cost:.4f} per request this is ${metrics['cost_per_1k_requests']:.2f} "
            f"per thousand and ${metrics['cost_per_1m_requests']:,.0f} per million."
        )

    if not findings:
        findings.append(
            f"No cost problems found: {metrics['mean_input_tokens']:.0f} input tokens per "
            f"request with no significant fixed prefix"
            + (f", ${cost:.4f}/req." if cost is not None else ".")
        )

    return findings
