"""FaultProxy: the only place faults are injected.

A FaultProxy is a Target that wraps another Target. Probes cannot tell the
difference, which is the point -- phase 1 runs them against the real target and
phase 2 runs the same probes against a wrapped one, so any difference in the
results is attributable to the faults and nothing else.

Injection is deterministic given a seed. The decision for a call depends only on
its trajectory and attempt number, never on a shared cursor, so a run
reproduces whether requests go out sequentially or all at once.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from ..models import (
    ErrorKind,
    FaultKind,
    Invocation,
    Request,
    Response,
    TargetInfo,
    ToolInfo,
    Trajectory,
)
from .base import Target, TargetError

logger = logging.getLogger(__name__)

ALL_FAULTS: tuple[FaultKind, ...] = tuple(FaultKind)


@dataclass
class FaultConfig:
    """What to inject and how often.

    `rates` maps a fault to the probability that any given call gets it. At most
    one fault is injected per call, so the rates are shares of a single draw and
    must sum to at most 1.0 -- a total of 1.0 means every call is faulted.
    """

    rates: dict[FaultKind, float] = field(default_factory=dict)
    seed: int = 1337

    #: Latency reported for an injected timeout. Not actually slept.
    timeout_s: float = 30.0
    #: Latency of a fast rejection (429, 500, refused connection).
    reject_latency_s: float = 0.02
    #: Retry-After hint attached to injected 429s.
    retry_after_s: float = 1.0

    def __post_init__(self) -> None:
        for kind, rate in self.rates.items():
            if not isinstance(kind, FaultKind):
                raise ValueError(f"fault rate key must be a FaultKind, got {kind!r}")
            if not 0.0 <= rate <= 1.0:
                raise ValueError(f"fault rate for {kind.value} must be between 0 and 1")
        if self.total_rate > 1.0:
            raise ValueError(
                f"fault rates sum to {self.total_rate:.3f}; at most one fault is injected "
                "per call, so they cannot exceed 1.0"
            )

    @property
    def total_rate(self) -> float:
        return sum(self.rates.values())

    @property
    def active(self) -> bool:
        return self.total_rate > 0.0

    @classmethod
    def off(cls, **overrides: Any) -> "FaultConfig":
        """A proxy that injects nothing. Useful for isolating proxy overhead."""
        return cls(rates={}, **overrides)

    @classmethod
    def uniform(
        cls,
        rate: float,
        kinds: Sequence[FaultKind] = ALL_FAULTS,
        **overrides: Any,
    ) -> "FaultConfig":
        """Spread `rate` evenly across `kinds`.

        `rate` is the total probability that a call is faulted, not the rate per
        kind, so uniform(0.2) faults one call in five whatever the kind count.
        """
        if not kinds:
            raise ValueError("uniform() needs at least one fault kind")
        if not 0.0 <= rate <= 1.0:
            raise ValueError("rate must be between 0 and 1")
        share = rate / len(kinds)
        return cls(rates={kind: share for kind in kinds}, **overrides)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates": {kind.value: rate for kind, rate in self.rates.items()},
            "total_rate": self.total_rate,
            "seed": self.seed,
            "timeout_s": self.timeout_s,
        }


class FaultProxy(Target):
    """Wraps a Target and injects faults according to a FaultConfig.

    Everything except invoke() delegates to the inner target, including
    probe_requests(), so probes generate the same traffic they would against
    the real thing.

    Lifecycle is delegated but not owned: whoever set up the inner target is
    still responsible for tearing it down. A proxy built around an
    already-running target should not have setup() called on it.
    """

    def __init__(self, inner: Target, faults: FaultConfig | None = None) -> None:
        self.inner = inner
        self.faults = faults or FaultConfig.off()

        self.trajectories: dict[str, Trajectory] = {}
        self.invocations: list[Invocation] = []
        self._attempts: dict[str, int] = {}
        self._started = time.perf_counter()

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        await self.inner.setup()

    async def teardown(self) -> None:
        await self.inner.teardown()

    def describe(self) -> TargetInfo:
        info = self.inner.describe()
        return replace(
            info,
            metadata={
                **info.metadata,
                "fault_injected": True,
                "faults": self.faults.to_dict(),
            },
        )

    def list_tools(self) -> list[ToolInfo]:
        return self.inner.list_tools()

    def sample_request(self, index: int = 0) -> Request:
        return self.inner.sample_request(index)

    def probe_requests(self, count: int, *, offset: int = 0) -> list[Request]:
        return self.inner.probe_requests(count, offset=offset)

    async def invoke(self, request: Request) -> Response:
        key = request.trajectory_key
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt

        fault = self._choose_fault(request, attempt)
        started = time.perf_counter() - self._started

        if fault is None:
            response = await self.inner.invoke(request)
        elif fault is FaultKind.MALFORMED:
            response = self._corrupt(await self.inner.invoke(request))
        else:
            response = self._reject(fault)

        self._record(request, attempt, response, fault, started)
        return response

    # -- injection -----------------------------------------------------------

    def _choose_fault(self, request: Request, attempt: int) -> FaultKind | None:
        """Pick at most one fault for this call.

        Seeded per (trajectory, attempt): every attempt draws independently, so
        a retry can succeed where the first try was faulted. Seeding on the
        request alone would make a faulted call fail forever and no target
        could ever be shown to recover.
        """
        if not self.faults.active:
            return None

        rng = random.Random(f"{self.faults.seed}:{request.trajectory_key}:{attempt}")
        draw = rng.random()

        cumulative = 0.0
        for kind in ALL_FAULTS:
            rate = self.faults.rates.get(kind, 0.0)
            if rate <= 0.0:
                continue
            cumulative += rate
            if draw < cumulative:
                return kind
        return None

    def _reject(self, fault: FaultKind) -> Response:
        """Build a failure without ever reaching the inner target."""
        if fault is FaultKind.TIMEOUT:
            return Response(
                ok=False,
                latency_s=self.faults.timeout_s,
                error=f"injected timeout after {self.faults.timeout_s:.1f}s",
                error_kind=ErrorKind.TIMEOUT,
                meta={"injected": fault.value},
            )

        if fault is FaultKind.RATE_LIMIT:
            return Response(
                ok=False,
                latency_s=self.faults.reject_latency_s,
                error="injected rate limit (HTTP 429)",
                error_kind=ErrorKind.RATE_LIMIT,
                meta={
                    "injected": fault.value,
                    "status": 429,
                    "retry_after_s": self.faults.retry_after_s,
                },
            )

        if fault is FaultKind.SERVER_ERROR:
            return Response(
                ok=False,
                latency_s=self.faults.reject_latency_s,
                error="injected server error (HTTP 500)",
                error_kind=ErrorKind.SERVER_ERROR,
                meta={"injected": fault.value, "status": 500},
            )

        if fault is FaultKind.CONNECTION_REFUSED:
            return Response(
                ok=False,
                latency_s=self.faults.reject_latency_s,
                error="injected dependency unavailability (connection refused)",
                error_kind=ErrorKind.CONNECTION,
                meta={"injected": fault.value},
            )

        raise TargetError(f"{fault} is not a short-circuit fault")

    def _corrupt(self, response: Response) -> Response:
        """Damage a real response.

        Runs after the inner call so the caller still pays the real latency --
        a truncated payload costs just as much to produce as a valid one, and a
        probe that saw malformed responses arrive instantly would be measuring
        an artifact of the harness.
        """
        if not response.ok:
            # Already failed on its own. Corrupting it would overwrite a real
            # observation with a synthetic one.
            return response

        return replace(
            response,
            ok=False,
            output=_truncate(response.output),
            error="injected malformed response (payload truncated)",
            error_kind=ErrorKind.INVALID_RESPONSE,
            meta={**response.meta, "injected": FaultKind.MALFORMED.value},
        )

    # -- recording -----------------------------------------------------------

    def _record(
        self,
        request: Request,
        attempt: int,
        response: Response,
        fault: FaultKind | None,
        started: float,
    ) -> None:
        key = request.trajectory_key
        invocation = Invocation(
            sequence=len(self.invocations),
            op=request.op,
            fingerprint=request.fingerprint,
            trajectory_id=key,
            attempt=attempt,
            ok=response.ok,
            latency_s=response.latency_s,
            started_at=started,
            error_kind=response.error_kind,
            injected=fault,
        )
        self.invocations.append(invocation)
        self.trajectories.setdefault(key, Trajectory(trajectory_id=key)).invocations.append(
            invocation
        )

    # -- reporting -----------------------------------------------------------

    @property
    def injected_count(self) -> int:
        return sum(1 for inv in self.invocations if inv.injected is not None)

    def injected_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for inv in self.invocations:
            if inv.injected is not None:
                counts[inv.injected.value] = counts.get(inv.injected.value, 0) + 1
        return counts

    def reset(self) -> None:
        """Drop recorded history, keeping the fault configuration."""
        self.trajectories.clear()
        self.invocations.clear()
        self._attempts.clear()
        self._started = time.perf_counter()


def _truncate(output: Any) -> Any:
    """Return a plausibly damaged version of a payload."""
    if isinstance(output, str):
        return output[: max(0, len(output) // 2)]
    if isinstance(output, dict):
        keys = list(output)[: max(0, len(output) // 2)]
        return {key: output[key] for key in keys}
    if isinstance(output, (list, tuple)):
        return list(output)[: max(0, len(output) // 2)]
    return None


def wrap(target: Target, faults: Iterable[FaultKind] | FaultConfig | None = None,
         rate: float = 0.2, seed: int = 1337) -> FaultProxy:
    """Convenience constructor: wrap(target, rate=0.2)."""
    if isinstance(faults, FaultConfig):
        return FaultProxy(target, faults)
    kinds = tuple(faults) if faults else ALL_FAULTS
    return FaultProxy(target, FaultConfig.uniform(rate, kinds, seed=seed))
