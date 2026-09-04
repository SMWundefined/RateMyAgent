"""Probe ABC plus the statistics helpers probes share."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Sequence

from ..models import Grade, ProbeResult

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)


@dataclass
class ProbeConfig:
    """Knobs a scan passes to every probe.

    Probes ignore what does not apply to them: the latency profiler reads
    `requests` and `warmup` and pays no attention to `concurrency`, which
    belongs to the load tester.
    """

    requests: int = 20
    concurrency: int = 5
    timeout_s: float = 30.0
    warmup: int = 1
    seed: int = 1337
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "concurrency": self.concurrency,
            "timeout_s": self.timeout_s,
            "warmup": self.warmup,
            "seed": self.seed,
            "extra": dict(self.extra),
        }


class Probe(ABC):
    """One self-contained measurement.

    Probes are independent by contract: each one drives the target itself and
    grades only its own data, so any subset can run in any order.
    """

    name: ClassVar[str]
    description: ClassVar[str] = ""

    #: Which pipeline phase this probe belongs to: baseline, chaos, or behavior.
    phase: ClassVar[str] = "baseline"

    @abstractmethod
    async def run(self, target: "Target", config: ProbeConfig) -> ProbeResult:
        """Collect measurements. Should not raise for target-side failures."""

    @abstractmethod
    def grade(self, result: ProbeResult) -> Grade:
        """Turn a result's metrics into a letter grade."""

    async def execute(self, target: "Target", config: ProbeConfig) -> ProbeResult:
        """run() + grade(), with timing and failure containment.

        A probe that raises produces an F-graded result carrying the error
        rather than taking down the whole scan.
        """
        started = time.perf_counter()
        try:
            result = await self.run(target, config)
        except Exception as exc:
            logger.exception("probe %s failed", self.name)
            return ProbeResult(
                probe=self.name,
                grade=Grade.F,
                phase=self.phase,
                summary=f"probe failed: {exc}",
                duration_s=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                error_rate=1.0,
            )

        result.phase = self.phase
        if not result.duration_s:
            result.duration_s = time.perf_counter() - started
        if result.grade is None:
            result.grade = self.grade(result)
        return result


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. `pct` is 0-100.

    Nearest-rank rather than interpolated: with the sample sizes a scan
    collects, an interpolated p99 invents a number no request actually saw.
    """
    if not values:
        return None
    if not 0 <= pct <= 100:
        raise ValueError("percentile must be between 0 and 100")

    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-len(ordered) * pct // 100)))
    return ordered[int(rank) - 1]
