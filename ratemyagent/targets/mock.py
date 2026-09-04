"""In-process mock target.

Exists so every probe has tests that run without API keys, a network, or an MCP
server. Behavior is derived from the request label rather than a shared cursor,
so a mock produces the same run whether requests go out one at a time or all at
once -- which keeps the load tester's results reproducible too.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Sequence

from ..models import ErrorKind, Request, Response, TargetInfo, ToolInfo
from .base import Target, TargetError

DEFAULT_TOOLS: tuple[str, ...] = ("echo", "search", "summarize")


class MockTarget(Target):
    """A synthetic target with tunable latency and failure behavior.

    Latency is *simulated*: invoke() reports a drawn value in Response and only
    really sleeps `latency_s * sleep_scale` seconds. With the default
    sleep_scale of 0.0 a 200-request probe finishes instantly while still
    profiling as though every call took real time, so tests stay fast without
    faking out the probe's arithmetic.
    """

    def __init__(
        self,
        *,
        name: str = "mock-target",
        tools: Sequence[str] = DEFAULT_TOOLS,
        latency_s: float = 0.4,
        jitter_s: float = 0.15,
        tail_multiplier: float = 4.0,
        tail_probability: float = 0.05,
        error_rate: float = 0.0,
        error_kinds: Sequence[ErrorKind] = (ErrorKind.TIMEOUT,),
        ttft_ratio: float | None = 0.35,
        # 8% transport overhead: realistic for a local server, and below the
        # latency probe's 20% reporting threshold so the default profiles do
        # not raise an overhead finding they were not built to demonstrate.
        server_time_ratio: float = 0.92,
        tokens_in: int = 850,
        tokens_out: int = 120,
        seed: int = 1337,
        sleep_scale: float = 0.0,
    ) -> None:
        if not tools:
            raise ValueError("mock target needs at least one tool")
        if not 0.0 <= error_rate <= 1.0:
            raise ValueError("error_rate must be between 0 and 1")
        if error_rate > 0 and not error_kinds:
            raise ValueError("error_rate > 0 requires at least one error kind")

        self.name = name
        self.tools = tuple(tools)
        self.latency_s = latency_s
        self.jitter_s = jitter_s
        self.tail_multiplier = tail_multiplier
        self.tail_probability = tail_probability
        self.error_rate = error_rate
        self.error_kinds = tuple(error_kinds)
        self.ttft_ratio = ttft_ratio
        self.server_time_ratio = server_time_ratio
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.seed = seed
        self.sleep_scale = sleep_scale

        self.calls: list[Request] = []
        self._connected = False

    # -- canned profiles used across the test suite --------------------------

    @classmethod
    def healthy(cls, **overrides: Any) -> "MockTarget":
        """Fast and reliable. Should grade A."""
        defaults: dict[str, Any] = {
            "name": "healthy-mock",
            "latency_s": 0.35,
            "jitter_s": 0.1,
            "tail_probability": 0.0,
            "error_rate": 0.0,
        }
        return cls(**{**defaults, **overrides})

    @classmethod
    def degraded(cls, **overrides: Any) -> "MockTarget":
        """Slow with a heavy tail, but not failing. Grades C.

        Latency alone drives the grade: the worst possible draw is
        4.0 * 2.2 = 8.8s, keeping p95 inside the C band on any sample.

        The error rate is deliberately zero. Error grading is coarse at small
        sample sizes -- at the default 20 requests a single failure is already
        5%, which the spec grades D -- so any non-zero rate here would flip this
        profile's grade from run to run. Failure behavior belongs to the
        `failing` profile, and from week 2 to the FaultProxy.
        """
        defaults: dict[str, Any] = {
            "name": "degraded-mock",
            "latency_s": 3.0,
            "jitter_s": 1.0,
            "tail_multiplier": 2.2,
            "tail_probability": 0.15,
            "error_rate": 0.0,
            "error_kinds": (ErrorKind.TIMEOUT, ErrorKind.RATE_LIMIT),
        }
        return cls(**{**defaults, **overrides})

    @classmethod
    def failing(cls, **overrides: Any) -> "MockTarget":
        """Slow and mostly broken. Should grade F."""
        defaults: dict[str, Any] = {
            "name": "failing-mock",
            "latency_s": 12.0,
            "jitter_s": 4.0,
            "tail_multiplier": 3.0,
            "tail_probability": 0.3,
            "error_rate": 0.35,
            "error_kinds": (
                ErrorKind.TIMEOUT,
                ErrorKind.SERVER_ERROR,
                ErrorKind.RATE_LIMIT,
            ),
        }
        return cls(**{**defaults, **overrides})

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        self._connected = True

    async def teardown(self) -> None:
        self._connected = False

    def describe(self) -> TargetInfo:
        return TargetInfo(
            name=self.name,
            kind="mock",
            uri=f"mock://{self.name}",
            capabilities=list(self.tools),
            metadata={
                "latency_s": self.latency_s,
                "error_rate": self.error_rate,
                "seed": self.seed,
                "simulated": True,
            },
        )

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=tool,
                description=f"simulated {tool} tool",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
            for tool in self.tools
        ]

    def sample_request(self, index: int = 0) -> Request:
        tool = self.tools[index % len(self.tools)]
        return Request(
            op=tool,
            payload={"query": f"probe request {index}"},
            label=f"{tool}#{index}",
        )

    async def invoke(self, request: Request) -> Response:
        if not self._connected:
            raise TargetError("MockTarget.invoke() called before setup()")

        self.calls.append(request)
        rng = random.Random(f"{self.seed}:{request.label or request.op}")

        latency = self._draw_latency(rng)
        if self.sleep_scale:
            await asyncio.sleep(latency * self.sleep_scale)

        if rng.random() < self.error_rate:
            kind = rng.choice(self.error_kinds)
            return Response(
                ok=False,
                latency_s=latency,
                error=f"simulated {kind.value} from {self.name}",
                error_kind=kind,
                meta={"simulated": True},
            )

        return Response(
            ok=True,
            latency_s=latency,
            ttft_s=latency * self.ttft_ratio if self.ttft_ratio else None,
            server_time_s=latency * self.server_time_ratio,
            output={"tool": request.op, "echo": request.payload},
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            meta={"simulated": True},
        )

    def _draw_latency(self, rng: random.Random) -> float:
        latency = self.latency_s + rng.uniform(-self.jitter_s, self.jitter_s)
        if self.tail_probability and rng.random() < self.tail_probability:
            latency *= self.tail_multiplier
        return max(0.001, latency)
