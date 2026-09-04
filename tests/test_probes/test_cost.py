"""CostAnalyzer: token accounting, prompt bloat, and dollar projection."""

from __future__ import annotations

import pytest

from ratemyagent.models import Response
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.cost import ANTHROPIC_PRICING, CostAnalyzer
from ratemyagent.targets import MockTarget
from tests.conftest import ScriptedTarget

# Opus 5 rates, from the model table.
OPUS_IN, OPUS_OUT = ANTHROPIC_PRICING["claude-opus-5"]


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 20, "warmup": 0, "timeout_s": 5.0}
    return ProbeConfig(**{**defaults, **kwargs})


def priced(**extra) -> ProbeConfig:
    return config(extra={"price_in": OPUS_IN, "price_out": OPUS_OUT, **extra})


class TestTokenAccounting:
    async def test_counts_tokens_the_target_reports(self):
        async with MockTarget.healthy(tokens_in=1000, tokens_out=200) as target:
            result = await CostAnalyzer().execute(target, config())

        assert result.metrics["mean_output_tokens"] == 200
        assert 0 < result.metrics["mean_input_tokens"] <= 1000
        assert result.metrics["reported_usage"] is True

    async def test_target_without_usage_is_inapplicable(self):
        """An MCP server reports no tokens. That is not a cost problem."""
        target = ScriptedTarget([Response(ok=True, latency_s=0.1)])
        result = await CostAnalyzer().execute(target, config())

        assert result.metrics["reported_usage"] is False
        assert result.applicable is False
        assert any("reported no token usage" in f for f in result.findings)

    async def test_io_ratio_is_reported(self):
        async with MockTarget.healthy(
            tokens_in=1000, tokens_out=50, static_prompt_tokens=1000
        ) as target:
            result = await CostAnalyzer().execute(target, config())

        assert result.metrics["io_ratio"] == pytest.approx(20.0)


class TestPricing:
    async def test_cost_is_computed_from_the_price_table(self):
        async with MockTarget.healthy(
            tokens_in=1000, tokens_out=100, static_prompt_tokens=1000
        ) as target:
            result = await CostAnalyzer().execute(
                target, config(extra={"model": "claude-opus-5"})
            )

        expected = (1000 * OPUS_IN + 100 * OPUS_OUT) / 1_000_000
        assert result.metrics["cost_per_request"] == pytest.approx(expected)
        assert result.metrics["pricing_source"] == "table"

    async def test_explicit_prices_override_the_table(self):
        async with MockTarget.healthy(
            tokens_in=1000, tokens_out=100, static_prompt_tokens=1000
        ) as target:
            result = await CostAnalyzer().execute(
                target, config(extra={"price_in": 1.0, "price_out": 1.0})
            )

        assert result.metrics["pricing_source"] == "override"
        assert result.metrics["cost_per_request"] == pytest.approx(1100 / 1_000_000)

    async def test_unknown_model_reports_tokens_but_no_dollars(self):
        """Never invent a price. A guessed number would end up in a budget."""
        async with MockTarget.healthy() as target:
            result = await CostAnalyzer().execute(
                target, config(extra={"model": "some-unreleased-model"})
            )

        assert result.metrics["cost_per_request"] is None
        assert result.applicable is False
        assert any("No published price" in f for f in result.findings)

    async def test_per_1k_and_per_1m_projections_scale(self):
        async with MockTarget.healthy(
            tokens_in=1000, tokens_out=100, static_prompt_tokens=1000
        ) as target:
            result = await CostAnalyzer().execute(target, priced())

        cost = result.metrics["cost_per_request"]
        assert result.metrics["cost_per_1k_requests"] == pytest.approx(cost * 1_000)
        assert result.metrics["cost_per_1m_requests"] == pytest.approx(cost * 1_000_000)

    def test_price_table_covers_the_current_models(self):
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            price_in, price_out = ANTHROPIC_PRICING[model]
            assert price_out > price_in > 0


class TestPromptBloat:
    async def test_large_fixed_prefix_is_detected(self):
        async with MockTarget.bloated() as target:
            result = await CostAnalyzer().execute(target, priced())

        assert result.metrics["bloat_detected"] is True
        assert result.metrics["bloat_share"] > 0.9
        assert any("Prompt bloat" in f for f in result.findings)

    async def test_varied_input_is_not_flagged(self):
        """Requests that differ genuinely have no cacheable prefix to report."""
        responses = [
            Response(ok=True, latency_s=0.1, tokens_in=n, tokens_out=100)
            for n in (200, 900, 1600, 2300, 3000)
        ]
        result = await CostAnalyzer().execute(ScriptedTarget(responses), priced())

        assert result.metrics["bloat_detected"] is False

    async def test_small_fixed_prefix_is_not_worth_reporting(self):
        """A 100% fixed prompt of 40 tokens is not a caching opportunity."""
        responses = [Response(ok=True, latency_s=0.1, tokens_in=40, tokens_out=20)] * 5
        result = await CostAnalyzer().execute(ScriptedTarget(responses), priced())

        assert result.metrics["bloat_share"] == pytest.approx(1.0)
        assert result.metrics["bloat_detected"] is False

    async def test_cache_savings_are_projected(self):
        async with MockTarget.bloated() as target:
            result = await CostAnalyzer().execute(target, priced())

        static = result.metrics["static_prefix_tokens"]
        expected = (static * OPUS_IN * 0.9) / 1_000_000
        assert result.metrics["cacheable_savings_per_request"] == pytest.approx(expected)
        assert any("Caching that prefix" in f for f in result.findings)


class TestProbeContract:
    def test_declares_baseline_phase(self):
        assert CostAnalyzer.phase == "baseline"
        assert CostAnalyzer.rerun_under_fault is False

    async def test_runs_without_keys_or_network(self):
        async with MockTarget.healthy() as target:
            result = await CostAnalyzer().execute(target, priced())

        assert result.sample_count == 20
        assert result.metrics["reported_usage"] is True
