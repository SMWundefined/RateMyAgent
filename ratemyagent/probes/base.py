"""Probe ABC plus the statistics helpers probes share.

Probes measure; they no longer judge. Grading moved to `policy.py` in week 4, so
a probe's job ends at producing metrics and findings -- what those are worth is
the policy's decision, and it is configurable per project.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Sequence

from ..models import ProbeResult

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)


@dataclass
class ProbeConfig:
    """Knobs a scan passes to every probe.

    Probes ignore what does not apply to them: the latency profiler reads
    `requests` and `warmup` and pays no attention to `concurrency`, which
    belongs to the concurrency tester.
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


@dataclass
class ScanContext:
    """What earlier phases leave behind for later ones.

    The pipeline is ordered for a reason: phase 3 analyses the trajectories
    phase 2 produced. Rather than let the behaviour probe reach into the fault
    probe, phase 2 deposits its artifacts here and phase 3 reads them, so each
    probe still runs standalone -- it just finds nothing and says so.
    """

    results: list[ProbeResult] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def result(self, probe: str) -> ProbeResult | None:
        for result in self.results:
            if result.probe == probe:
                return result
        return None


class Probe(ABC):
    """One self-contained measurement.

    Probes are independent by contract: each one drives the target itself and
    reports only its own data, so any subset can run in any order.
    """

    name: ClassVar[str]
    description: ClassVar[str] = ""

    #: Which pipeline phase this probe belongs to: baseline, chaos, or behavior.
    phase: ClassVar[str] = "baseline"

    #: Whether phase 2 re-runs this probe against a fault-injected target.
    #: Opt in only where the comparison against phase 1 is meaningful.
    rerun_under_fault: ClassVar[bool] = False

    @abstractmethod
    async def run(
        self, target: "Target", config: ProbeConfig, context: ScanContext | None = None
    ) -> ProbeResult:
        """Collect measurements. Should not raise for target-side failures."""

    async def execute(
        self, target: "Target", config: ProbeConfig, context: ScanContext | None = None
    ) -> ProbeResult:
        """run() with timing and failure containment.

        A probe that raises produces a result carrying the error rather than
        taking down the whole scan. Scoring happens afterwards, in the policy
        engine.
        """
        started = time.perf_counter()
        try:
            result = await self.run(target, config, context)
        except Exception as exc:
            logger.exception("probe %s failed", self.name)
            return ProbeResult(
                probe=self.name,
                phase=self.phase,
                applicable=False,
                summary=f"probe failed: {exc}",
                duration_s=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                error_rate=1.0,
            )

        result.phase = self.phase
        if not result.duration_s:
            result.duration_s = time.perf_counter() - started
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
