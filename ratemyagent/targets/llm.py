"""LLM chat-completions adapter for Anthropic and OpenAI.

Same Target contract as everything else, so every probe already works against
it unchanged. Both SDKs are optional dependencies, imported lazily, so
`import ratemyagent` works without either installed.

Mock-testable by construction: pass `client=` and no SDK is imported and no
network call is made. That is how the whole suite exercises this adapter
without an API key -- see tests/test_targets/test_llm.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import ErrorKind, Request, Response, TargetInfo, ToolInfo
from .base import Target, TargetError, classify_exception

logger = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("anthropic", "openai")

#: Kept small on purpose. This is a probe measuring the round trip, not a
#: production call doing real work, and a scan sends dozens of them.
DEFAULT_MAX_TOKENS = 256

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4o-mini",
}

DEFAULT_PROMPT = "Reply with the single word: ok"

_INSTALL_HINTS: dict[str, str] = {
    "anthropic": "Anthropic support needs the SDK: pip install 'ratemyagent[anthropic]'",
    "openai": "OpenAI support needs the SDK: pip install 'ratemyagent[openai]'",
}


class LLMTarget(Target):
    """A chat-completions endpoint, scanned like any other dependency.

    The probe prompt is deliberately trivial and `max_tokens` deliberately
    small: this measures the operational envelope of the endpoint, not model
    quality. Pass `prompt=` and `max_tokens=` to profile a realistic payload.
    """

    def __init__(
        self,
        provider: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        prompt: str = DEFAULT_PROMPT,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = 30.0,
        client: Any = None,
    ) -> None:
        key = provider.strip().lower()
        if key not in PROVIDERS:
            raise TargetError(
                f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
            )

        self.provider = key
        self.model = model or DEFAULT_MODELS[key]
        self.prompt = prompt
        self.system = system
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

        self._api_key = api_key
        self._client = client
        self._injected = client is not None
        self._connected = False

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        if self._client is None:
            self._client = self._build_client()
        self._connected = True

    async def teardown(self) -> None:
        self._connected = False
        # Only close what we created. An injected client belongs to the caller.
        if self._injected or self._client is None:
            return

        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:  # pragma: no cover - SDK shutdown noise
                logger.debug("closing %s client raised: %s", self.provider, exc)
        self._client = None

    def describe(self) -> TargetInfo:
        return TargetInfo(
            name=f"{self.provider}:{self.model}",
            kind="llm",
            uri=f"{self.provider}://{self.model}",
            capabilities=["chat.completions"],
            metadata={
                "provider": self.provider,
                "model": self.model,
                "max_tokens": self.max_tokens,
                "transport": "https",
            },
        )

    def list_tools(self) -> list[ToolInfo]:
        """No tool surface. The contract probe skips targets that report none."""
        return []

    def sample_request(self, index: int = 0) -> Request:
        return Request(
            op="chat.completions",
            payload={"prompt": self.prompt, "system": self.system},
            timeout_s=self.timeout_s,
            label=f"chat#{index}",
        )

    async def invoke(self, request: Request) -> Response:
        if not self._connected or self._client is None:
            raise TargetError("LLMTarget.invoke() called before setup()")

        prompt = request.payload.get("prompt") or self.prompt
        system = request.payload.get("system") or self.system

        started = time.perf_counter()
        try:
            if self.provider == "anthropic":
                raw = await self._call_anthropic(prompt, system)
            else:
                raw = await self._call_openai(prompt, system)
        except Exception as exc:
            return self._error_response(exc, time.perf_counter() - started)

        latency = time.perf_counter() - started
        return self._to_response(raw, latency)

    # -- provider calls ------------------------------------------------------

    async def _call_anthropic(self, prompt: str, system: str | None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        return await self._client.messages.create(**kwargs)

    async def _call_openai(self, prompt: str, system: str | None) -> Any:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        return await self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
        )

    # -- translation ---------------------------------------------------------

    def _to_response(self, raw: Any, latency: float) -> Response:
        if self.provider == "anthropic":
            text = _anthropic_text(raw)
            usage = getattr(raw, "usage", None)
            tokens_in = getattr(usage, "input_tokens", None)
            tokens_out = getattr(usage, "output_tokens", None)
            meta = {
                "stop_reason": getattr(raw, "stop_reason", None),
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", None
                ),
            }
        else:
            text = _openai_text(raw)
            usage = getattr(raw, "usage", None)
            tokens_in = getattr(usage, "prompt_tokens", None)
            tokens_out = getattr(usage, "completion_tokens", None)
            choices = getattr(raw, "choices", None) or []
            meta = {
                "finish_reason": getattr(choices[0], "finish_reason", None)
                if choices
                else None
            }

        # A refusal or a content filter is a real operational outcome, not a
        # success with odd text. Surfacing it as a failure is what lets the
        # error taxonomy count it.
        refused = meta.get("stop_reason") == "refusal" or meta.get("finish_reason") == (
            "content_filter"
        )

        return Response(
            ok=not refused,
            latency_s=latency,
            output=text,
            error="model declined the request" if refused else None,
            error_kind=ErrorKind.INVALID_RESPONSE if refused else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            meta={k: v for k, v in meta.items() if v is not None},
        )

    def _error_response(self, exc: Exception, latency: float) -> Response:
        """Map an SDK exception onto the failure taxonomy.

        Both SDKs expose `status_code` on API errors, which is more reliable
        than matching on the message text.
        """
        status = getattr(exc, "status_code", None)
        kind = _STATUS_KINDS.get(status) if status is not None else None

        return Response(
            ok=False,
            latency_s=latency,
            error=f"{type(exc).__name__}: {exc}",
            error_kind=kind or classify_exception(exc),
            meta={"status": status} if status is not None else {},
        )

    # -- client construction -------------------------------------------------

    def _build_client(self) -> Any:
        if self.provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise TargetError(_INSTALL_HINTS["anthropic"]) from exc

            kwargs: dict[str, Any] = {"timeout": self.timeout_s, "max_retries": 0}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            return AsyncAnthropic(**kwargs)

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise TargetError(_INSTALL_HINTS["openai"]) from exc

        kwargs = {"timeout": self.timeout_s, "max_retries": 0}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return AsyncOpenAI(**kwargs)


#: HTTP status -> failure taxonomy. Retries are disabled on the SDK clients so
#: that a 429 is reported rather than silently absorbed -- a scan measuring
#: reliability must see the rate limit, not have the SDK paper over it.
_STATUS_KINDS: dict[int, ErrorKind] = {
    408: ErrorKind.TIMEOUT,
    429: ErrorKind.RATE_LIMIT,
    500: ErrorKind.SERVER_ERROR,
    502: ErrorKind.SERVER_ERROR,
    503: ErrorKind.SERVER_ERROR,
    504: ErrorKind.SERVER_ERROR,
    400: ErrorKind.PROTOCOL,
    401: ErrorKind.PROTOCOL,
    403: ErrorKind.PROTOCOL,
    404: ErrorKind.PROTOCOL,
}


def _anthropic_text(raw: Any) -> str:
    """Flatten Anthropic content blocks, keeping only text blocks."""
    blocks = getattr(raw, "content", None) or []
    parts = [
        getattr(block, "text", "")
        for block in blocks
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts)


def _openai_text(raw: Any) -> str:
    choices = getattr(raw, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None) or ""
