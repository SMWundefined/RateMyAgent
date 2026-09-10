"""Probe ABC plus the statistics helpers probes share.

Probes measure; they no longer judge. Grading moved to `policy.py` in week 4, so
a probe's job ends at producing metrics and findings -- what those are worth is
the policy's decision, and it is configurable per project.
"""

from __future__ import annotations

import logging
import math
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
    #: Wall clock for the whole scan, distinct from `timeout_s`, which bounds one
    #: request. `timeout_s` cannot bound a scan: a stdio server that never
    #: completes its handshake hangs inside `stdio_client()` before any request
    #: exists to time out, which is how `mcp-server-fetch` hung three times and
    #: had to be killed by hand. None derives one from the request budget.
    scan_timeout_s: float | None = None
    #: Retries a disrupted operation gets before it counts as unrecovered.
    #:
    #: Promoted from a `FaultInjector` constructor default at the 1.0 freeze. It
    #: was internal while it only defined the metric; it stopped being internal
    #: when the derived recovery floor shipped, because the floor is
    #: `1 - fault_rate ** max_retries` and every report header now prints both.
    #: A number that appears in published output and sets the threshold a target
    #: is graded against cannot be reachable only by importing the probe class.
    #:
    #: **Minimum 1, enforced.** At 0 the floor is `1 - r**0 = 0` and every
    #: target passes trivially -- a knob that silently disables the check it
    #: parameterises. That is an argument for a constraint, not for hiding it.
    max_retries: int = 2
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_retries < 1:
            raise ValueError(
                f"max_retries must be at least 1, got {self.max_retries}. At 0 "
                "the derived recovery floor is 1 - fault_rate**0 = 0, which "
                "every target clears without recovering from anything."
            )

    def scan_budget(self) -> float:
        """Seconds the whole scan may take before it is abandoned.

        Deliberately generous. This exists to turn an unbounded hang into a
        clean, attributable failure -- not to enforce a performance target, which
        is what the policy thresholds are for. A scan that legitimately needs
        longer should raise it explicitly rather than have it guessed tighter.
        """
        if self.scan_timeout_s is not None:
            return self.scan_timeout_s
        # Probes rerun under fault injection and the concurrency ramp repeats
        # the request budget, so the real call count is several times
        # `requests`. Four is head-room, not a measurement.
        return max(60.0, self.timeout_s * max(self.requests, 1) * 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "concurrency": self.concurrency,
            "scan_timeout_s": self.scan_budget(),
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


def wilson_interval(successes: int, trials: int, z: float = 1.959963985) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Default z is 95%.

    Wilson rather than normal-approximation: at the sample sizes a scan
    produces, `p +/- z*sqrt(p(1-p)/n)` is wrong in exactly the cases that
    matter. It gives a zero-width interval at k = n -- so 5 of 5 recoveries
    would read as "100%, certainly" -- and can put the bound above 1. Wilson
    stays inside [0, 1] and keeps width at the boundary, which is what makes
    5/5 legible as "somewhere between 57% and 100%".
    """
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return (max(0.0, center - half), min(1.0, center + half))


def recovery_floor(fault_rate: float | None, max_retries: int | None) -> float | None:
    """The recovery rate the injector produces against a target that never fails.

    An operation is disrupted when its first attempt draws a fault, probability
    `r`. It recovers when at least one of its `max_retries` retries does not, so
    a target that is perfectly healthy still records `1 - r**max_retries`. That
    number is a property of the flags, not of the target:

        r = 0.2, 2 retries -> 96%      r = 0.3, 2 retries -> 91%
        r = 0.5, 2 retries -> 75%      r = 0.9, 2 retries -> 19%

    Scoring against a fixed 0.90 therefore grades `--fault-rate`. At r = 0.2 the
    floor is *below* the arithmetic and every healthy target passes with room to
    spare; above r = 0.316 it is unreachable and every target fails, however good
    it is. Deriving it means the threshold asks the only question that is about
    the target: did it do worse than the injector alone would explain?

    Returns None when either input is unknown, which is the signal to fall back
    to the policy's literal value rather than invent one.
    """
    if fault_rate is None or max_retries is None:
        return None
    if not 0.0 <= fault_rate <= 1.0 or max_retries < 0:
        return None
    return 1.0 - fault_rate**max_retries
