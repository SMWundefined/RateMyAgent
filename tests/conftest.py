"""Shared mock targets and fixtures.

Nothing in the suite touches a network or an API key.
"""

from __future__ import annotations

import re
from typing import Sequence

import pytest

from ratemyagent.models import ErrorKind, Request, Response, TargetInfo, ToolInfo  # noqa: F401
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget, Target

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Drop SGR escapes so an assertion reads the words, not the colours."""
    return _ANSI.sub("", text)


def verdict_tail(text: str) -> list[str]:
    """The scorecard's closing lines, with the byline and colour removed.

    The verdict has to stay the last thing an engineer reads in a CI log. The
    byline sits below it, so tests assert against the lines above the byline
    rather than the literal end of the output.
    """
    lines = [line for line in strip_ansi(text).strip().splitlines()]
    while lines and (not lines[-1].strip() or lines[-1].startswith("ratemyagent v")):
        lines.pop()
    return lines


class ScriptedTarget(Target):
    """Replays an exact list of responses, cycling if a probe asks for more.

    MockTarget draws from a distribution, which is right for end-to-end tests
    but useless when a test needs to assert that a specific p95 comes out of a
    specific set of latencies. This target makes the arithmetic checkable.
    """

    def __init__(self, responses: Sequence[Response], *, name: str = "scripted") -> None:
        if not responses:
            raise ValueError("ScriptedTarget needs at least one response")
        self._responses = list(responses)
        self._name = name
        self.calls: list[Request] = []
        self.setup_calls = 0
        self.teardown_calls = 0

    @classmethod
    def from_latencies(
        cls,
        latencies: Sequence[float],
        *,
        failures: int = 0,
        server_time_ratio: float = 0.95,
        **kwargs,
    ) -> "ScriptedTarget":
        """Successes at the given latencies, plus `failures` timed-out requests.

        The default server_time_ratio leaves only 5% overhead, below the
        latency probe's reporting threshold, so tests that are not about
        overhead do not trip the finding.
        """
        responses = [
            Response(
                ok=True,
                latency_s=value,
                ttft_s=value * 0.3,
                server_time_s=value * server_time_ratio,
            )
            for value in latencies
        ]
        responses.extend(
            Response(
                ok=False,
                latency_s=30.0,
                error="scripted timeout",
                error_kind=ErrorKind.TIMEOUT,
            )
            for _ in range(failures)
        )
        return cls(responses, **kwargs)

    async def setup(self) -> None:
        self.setup_calls += 1

    async def teardown(self) -> None:
        self.teardown_calls += 1

    def describe(self) -> TargetInfo:
        return TargetInfo(name=self._name, kind="scripted", capabilities=["run"])

    def list_tools(self) -> list[ToolInfo]:
        return [ToolInfo(name="run")]

    def sample_request(self, index: int = 0) -> Request:
        return Request(op="run", label=f"run#{index}")

    async def invoke(self, request: Request) -> Response:
        response = self._responses[len(self.calls) % len(self._responses)]
        self.calls.append(request)
        return response


class ExplodingTarget(MockTarget):
    """Raises instead of returning a failed Response, to test containment."""

    async def invoke(self, request: Request) -> Response:
        raise RuntimeError("connection reset by peer")


class ValidatingTarget(MockTarget):
    """A well-behaved tool: refuses input its schema forbids, cleanly.

    This is what the contract probe should grade well. Rejecting bad input is
    correct behaviour, not a failure.
    """

    async def invoke(self, request: Request) -> Response:
        problem = _schema_violation(request.payload)
        if problem:
            return Response(
                ok=False,
                latency_s=0.01,
                error=f"invalid arguments: {problem}",
                error_kind=ErrorKind.INVALID_RESPONSE,
            )
        return await super().invoke(request)


class BrittleTarget(MockTarget):
    """A tool that falls over on malformed input instead of refusing it.

    Raises rather than returning an error, which is what a real server does
    when an unhandled exception kills the connection.
    """

    async def invoke(self, request: Request) -> Response:
        if _schema_violation(request.payload):
            raise ConnectionError("server closed the connection unexpectedly")
        return await super().invoke(request)


def _schema_violation(payload: dict) -> str | None:
    """Check a payload against the mock tools' declared schema.

    The mock advertises {"query": {"type": "string"}} with query required.
    """
    if "query" not in payload:
        return "missing required field 'query'"
    value = payload["query"]
    if value is None:
        return "'query' must not be null"
    if not isinstance(value, str):
        return f"'query' must be a string, got {type(value).__name__}"
    return None


@pytest.fixture
def healthy_target() -> MockTarget:
    return MockTarget.healthy()


@pytest.fixture
def degraded_target() -> MockTarget:
    return MockTarget.degraded()


@pytest.fixture
def failing_target() -> MockTarget:
    return MockTarget.failing()


@pytest.fixture
def config() -> ProbeConfig:
    """Small and warmup-free, so tests assert on exactly the requests they set up."""
    return ProbeConfig(requests=10, warmup=0, timeout_s=5.0)
