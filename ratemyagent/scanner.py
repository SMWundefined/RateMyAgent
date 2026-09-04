"""Scan orchestration: set up a target, run the phase pipeline, score it.

The pipeline is ordered, not a bag of probes. Phase 1 measures the target as it
is; phase 2 measures the same things with faults injected; phase 3 reads what
phase 2 recorded and reports what the target *did*. Running them out of order,
or phase 3 without phase 2, gives you numbers with nothing behind them.

Scoring happens once, at the end, against a policy. Probes measure; the policy
judges.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from .models import ScanResult, TargetInfo
from .policy import Policy, evaluate
from .probes import (
    PHASES,
    Probe,
    ProbeConfig,
    ScanContext,
    probes_in_phase,
    resolve_phases,
    resolve_probes,
)
from .targets.base import Target

logger = logging.getLogger(__name__)


async def scan(
    target: Target,
    *,
    probes: str | Iterable[str] | Iterable[Probe] | None = None,
    phases: str | Iterable[str] | None = None,
    config: ProbeConfig | None = None,
    policy: Policy | None = None,
    parallel: bool = False,
) -> ScanResult:
    """Run the phase pipeline against a target and score the result.

    Phases always run in pipeline order, whatever order they were requested in.
    Within a phase, probes are sequential by default. `parallel=True` runs a
    phase's probes concurrently, but they share one target, so a concurrent run
    measures the probes interfering with each other -- a latency profile taken
    while another probe saturates the same server is not a latency profile.

    `policy` defaults to the shipped production-default. Pass one loaded from
    your own YAML to score against your own thresholds.
    """
    selected = _as_probes(probes)
    active_phases = resolve_phases(phases)
    probe_config = config or ProbeConfig()
    active_policy = policy or Policy.default()

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    context = ScanContext()

    await target.setup()
    try:
        info = target.describe()

        for phase in active_phases:
            in_phase = probes_in_phase(selected, phase)
            if not in_phase:
                continue

            logger.info("phase %s: %s", phase, ", ".join(p.name for p in in_phase))
            if parallel:
                results = await asyncio.gather(
                    *(probe.execute(target, probe_config, context) for probe in in_phase)
                )
                context.results.extend(results)
            else:
                for probe in in_phase:
                    context.results.append(
                        await probe.execute(target, probe_config, context)
                    )
    finally:
        await target.teardown()

    result = ScanResult(
        target=info,
        probes=context.results,
        started_at=started_at,
        duration_s=time.perf_counter() - started,
        config={
            **probe_config.to_dict(),
            "parallel": parallel,
            "phases": active_phases,
            "policy": active_policy.name,
        },
    )
    return evaluate(result, active_policy)


def _as_probes(
    probes: str | Iterable[str] | Iterable[Probe] | None,
) -> list[Probe]:
    if probes is None or isinstance(probes, str):
        return resolve_probes(probes)

    collected = list(probes)
    if collected and all(isinstance(item, Probe) for item in collected):
        return collected  # type: ignore[return-value]
    return resolve_probes(collected)  # type: ignore[arg-type]


__all__ = ["PHASES", "Policy", "ProbeConfig", "ScanResult", "TargetInfo", "scan"]
