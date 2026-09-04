"""LLMTarget: Anthropic and OpenAI chat completions.

Every test injects a fake client, so no SDK is imported, no key is read, and no
request leaves the process. That is the whole reason LLMTarget takes `client=`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ratemyagent.models import ErrorKind
from ratemyagent.targets import LLMTarget, TargetError
from ratemyagent.targets.llm import DEFAULT_MODELS, PROVIDERS

# -- fake SDK objects, shaped like the real responses -------------------------


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeAnthropicUsage:
    input_tokens: int = 120
    output_tokens: int = 8
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass
class FakeAnthropicMessage:
    content: list[Any] = field(default_factory=lambda: [FakeTextBlock("ok")])
    usage: FakeAnthropicUsage = field(default_factory=FakeAnthropicUsage)
    stop_reason: str = "end_turn"


@dataclass
class FakeOpenAIMessage:
    content: str | None = "ok"


@dataclass
class FakeOpenAIChoice:
    message: FakeOpenAIMessage = field(default_factory=FakeOpenAIMessage)
    finish_reason: str = "stop"


@dataclass
class FakeOpenAIUsage:
    prompt_tokens: int = 130
    completion_tokens: int = 6


@dataclass
class FakeOpenAICompletion:
    choices: list[Any] = field(default_factory=lambda: [FakeOpenAIChoice()])
    usage: FakeOpenAIUsage = field(default_factory=FakeOpenAIUsage)


class FakeAnthropicClient:
    """Mimics `client.messages.create(...)`."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response or FakeAnthropicMessage()
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer: "FakeAnthropicClient") -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Any:
            self._outer.calls.append(kwargs)
            if self._outer._error:
                raise self._outer._error
            return self._outer._response

    async def close(self) -> None:
        self.closed = True


class FakeOpenAIClient:
    """Mimics `client.chat.completions.create(...)`."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response or FakeOpenAICompletion()
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.chat = self._Chat(self)

    class _Chat:
        def __init__(self, outer: "FakeOpenAIClient") -> None:
            self.completions = FakeOpenAIClient._Completions(outer)

    class _Completions:
        def __init__(self, outer: "FakeOpenAIClient") -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Any:
            self._outer.calls.append(kwargs)
            if self._outer._error:
                raise self._outer._error
            return self._outer._response


class FakeAPIError(Exception):
    """Both SDKs expose status_code on API errors."""

    def __init__(self, status_code: int, message: str = "api error") -> None:
        super().__init__(message)
        self.status_code = status_code


# -- tests --------------------------------------------------------------------


class TestConstruction:
    def test_unknown_provider_is_rejected(self):
        with pytest.raises(TargetError, match="unknown provider"):
            LLMTarget("cohere")

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_each_provider_has_a_default_model(self, provider):
        assert LLMTarget(provider).model == DEFAULT_MODELS[provider]

    def test_explicit_model_wins(self):
        assert LLMTarget("anthropic", model="claude-haiku-4-5").model == "claude-haiku-4-5"

    def test_anthropic_defaults_to_opus_5(self):
        assert LLMTarget("anthropic").model == "claude-opus-5"

    def test_provider_is_case_insensitive(self):
        assert LLMTarget("Anthropic").provider == "anthropic"


class TestLifecycle:
    async def test_invoke_before_setup_is_an_error(self):
        target = LLMTarget("anthropic", client=FakeAnthropicClient())
        with pytest.raises(TargetError, match="before setup"):
            await target.invoke(target.sample_request(0))

    async def test_injected_client_is_not_closed_on_teardown(self):
        """The caller owns a client it passed in."""
        client = FakeAnthropicClient()
        target = LLMTarget("anthropic", client=client)

        async with target:
            pass

        assert client.closed is False

    async def test_no_sdk_import_happens_with_an_injected_client(self):
        async with LLMTarget("openai", client=FakeOpenAIClient()) as target:
            assert (await target.invoke(target.sample_request(0))).ok

    def test_describe_reports_provider_and_model(self):
        info = LLMTarget("anthropic", model="claude-sonnet-5").describe()

        assert info.kind == "llm"
        assert info.metadata["provider"] == "anthropic"
        assert info.metadata["model"] == "claude-sonnet-5"
        assert info.uri == "anthropic://claude-sonnet-5"

    def test_no_tool_surface(self):
        """Which is why the contract probe skips LLM targets."""
        assert LLMTarget("anthropic").list_tools() == []

    def test_probe_requests_are_uniquely_labelled(self):
        target = LLMTarget("anthropic")
        assert len({r.label for r in target.probe_requests(5)}) == 5


class TestAnthropic:
    async def test_successful_call_maps_to_a_response(self):
        client = FakeAnthropicClient()
        async with LLMTarget("anthropic", client=client) as target:
            response = await target.invoke(target.sample_request(0))

        assert response.ok is True
        assert response.output == "ok"
        assert response.tokens_in == 120
        assert response.tokens_out == 8
        assert response.latency_s > 0

    async def test_request_shape_matches_the_messages_api(self):
        client = FakeAnthropicClient()
        target = LLMTarget(
            "anthropic", model="claude-opus-5", max_tokens=64,
            system="be terse", client=client,
        )
        async with target:
            await target.invoke(target.sample_request(0))

        call = client.calls[0]
        assert call["model"] == "claude-opus-5"
        assert call["max_tokens"] == 64
        assert call["system"] == "be terse"
        assert call["messages"] == [{"role": "user", "content": target.prompt}]

    async def test_system_is_omitted_when_not_set(self):
        client = FakeAnthropicClient()
        async with LLMTarget("anthropic", client=client) as target:
            await target.invoke(target.sample_request(0))

        assert "system" not in client.calls[0]

    async def test_only_text_blocks_are_collected(self):
        response = FakeAnthropicMessage(
            content=[
                type("Thinking", (), {"type": "thinking", "thinking": "hmm"})(),
                FakeTextBlock("visible"),
            ]
        )
        async with LLMTarget("anthropic", client=FakeAnthropicClient(response)) as target:
            assert (await target.invoke(target.sample_request(0))).output == "visible"

    async def test_a_refusal_is_a_failure_not_a_success(self):
        """stop_reason refusal is an operational outcome the taxonomy should see."""
        response = FakeAnthropicMessage(stop_reason="refusal")
        async with LLMTarget("anthropic", client=FakeAnthropicClient(response)) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.ok is False
        assert result.error_kind is ErrorKind.INVALID_RESPONSE

    async def test_cache_tokens_are_surfaced(self):
        usage = FakeAnthropicUsage(cache_read_input_tokens=900)
        response = FakeAnthropicMessage(usage=usage)
        async with LLMTarget("anthropic", client=FakeAnthropicClient(response)) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.meta["cache_read_input_tokens"] == 900


class TestOpenAI:
    async def test_successful_call_maps_to_a_response(self):
        async with LLMTarget("openai", client=FakeOpenAIClient()) as target:
            response = await target.invoke(target.sample_request(0))

        assert response.ok is True
        assert response.output == "ok"
        assert response.tokens_in == 130
        assert response.tokens_out == 6

    async def test_request_shape_matches_chat_completions(self):
        client = FakeOpenAIClient()
        target = LLMTarget("openai", model="gpt-4o-mini", max_tokens=32, client=client)
        async with target:
            await target.invoke(target.sample_request(0))

        call = client.calls[0]
        assert call["model"] == "gpt-4o-mini"
        assert call["max_tokens"] == 32
        assert call["messages"][-1]["role"] == "user"

    async def test_system_becomes_a_system_message(self):
        client = FakeOpenAIClient()
        target = LLMTarget("openai", system="be terse", client=client)
        async with target:
            await target.invoke(target.sample_request(0))

        assert client.calls[0]["messages"][0] == {"role": "system", "content": "be terse"}

    async def test_content_filter_is_a_failure(self):
        response = FakeOpenAICompletion(
            choices=[FakeOpenAIChoice(finish_reason="content_filter")]
        )
        async with LLMTarget("openai", client=FakeOpenAIClient(response)) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.ok is False

    async def test_empty_choices_do_not_crash(self):
        response = FakeOpenAICompletion(choices=[])
        async with LLMTarget("openai", client=FakeOpenAIClient(response)) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.output == ""


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (429, ErrorKind.RATE_LIMIT),
            (500, ErrorKind.SERVER_ERROR),
            (503, ErrorKind.SERVER_ERROR),
            (408, ErrorKind.TIMEOUT),
            (400, ErrorKind.PROTOCOL),
            (401, ErrorKind.PROTOCOL),
        ],
    )
    async def test_status_codes_map_to_the_taxonomy(self, status, expected):
        client = FakeAnthropicClient(error=FakeAPIError(status))
        async with LLMTarget("anthropic", client=client) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.ok is False
        assert result.error_kind is expected
        assert result.meta["status"] == status

    async def test_errors_never_raise_out_of_invoke(self):
        client = FakeOpenAIClient(error=RuntimeError("connection reset"))
        async with LLMTarget("openai", client=client) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.ok is False
        assert result.error_kind is ErrorKind.CONNECTION

    async def test_unknown_status_falls_back_to_exception_classification(self):
        client = FakeAnthropicClient(error=FakeAPIError(418, "teapot"))
        async with LLMTarget("anthropic", client=client) as target:
            result = await target.invoke(target.sample_request(0))

        assert result.ok is False
        assert result.error_kind is not None


class TestProbesRunAgainstIt:
    """The point of the shared Target ABC: existing probes work unchanged."""

    async def test_latency_probe_works(self):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.latency import LatencyProfiler

        async with LLMTarget("anthropic", client=FakeAnthropicClient()) as target:
            result = await LatencyProfiler().execute(
                target, ProbeConfig(requests=10, warmup=0)
            )

        assert result.metrics["requests"] == 10
        assert result.error_rate == 0.0

    async def test_cost_probe_prices_a_known_model(self):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.cost import CostAnalyzer

        target = LLMTarget("anthropic", model="claude-opus-5", client=FakeAnthropicClient())
        async with target:
            result = await CostAnalyzer().execute(
                target, ProbeConfig(requests=5, warmup=0)
            )

        # 120 in / 8 out at $5 / $25 per 1M.
        assert result.metrics["cost_per_request"] == pytest.approx(
            (120 * 5 + 8 * 25) / 1_000_000
        )
        assert result.applicable is True

    async def test_contract_probe_skips_a_target_with_no_tools(self):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.contract import ContractTester

        async with LLMTarget("openai", client=FakeOpenAIClient()) as target:
            result = await ContractTester().execute(target, ProbeConfig(requests=3))

        assert result.applicable is False
