"""Scan orchestration: set up a target, run the phase pipeline, aggregate.

The pipeline is ordered, not a bag of probes. Phase 1 measures the target as it
is; phase 2 measures the same things with faults injected; phase 3 (week 4)
compares the two. Running them out of order, or phase 2 without phase 1, gives
you numbers with nothing to compare against.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from .models import ScanResult, TargetInfo
from .probes import PHASES, Probe, ProbeConfig, probes_in_phase, resolve_phases, resolve_probes
from .targets.base import Target

logger = logging.getLogger(__name__)


async def scan(
    target: Target,
    *,
    probes: str | Iterable[str] | Iterable[Probe] | None = None,
    phases: str | Iterable[str] | None = None,
    config: ProbeConfig | None = None,
    parallel: bool = False,
) -> ScanResult:
    """Run the phase pipeline against a target and return the aggregate result.

    Phases always run in pipeline order, whatever order they were requested in.
    Within a phase, probes are sequential by default. `parallel=True` runs a
    phase's probes concurrently, but they share one target, so a concurrent run
    measures the probes interfering with each other -- a latency profile taken
    while another probe saturates the same server is not a latency profile.
    Turn it on when wall clock matters more than clean numbers.
    """
    selected = _as_probes(probes)
    active_phases = resolve_phases(phases)
    probe_config = config or ProbeConfig()

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()

    await target.setup()
    try:
        info = target.describe()
        results = []

        for phase in active_phases:
            in_phase = probes_in_phase(selected, phase)
            if not in_phase:
                continue

            logger.info(
                "phase %s: %s", phase, ", ".join(probe.name for probe in in_phase)
            )
            if parallel:
                results.extend(
                    await asyncio.gather(
                        *(probe.execute(target, probe_config) for probe in in_phase)
                    )
                )
            else:
                for probe in in_phase:
                    results.append(await probe.execute(target, probe_config))
    finally:
        await target.teardown()

    return ScanResult(
        target=info,
        probes=results,
        started_at=started_at,
        duration_s=time.perf_counter() - started,
        config={
            **probe_config.to_dict(),
            "parallel": parallel,
            "phases": active_phases,
        },
    )


def _as_probes(
    probes: str | Iterable[str] | Iterable[Probe] | None,
) -> list[Probe]:
    if probes is None or isinstance(probes, str):
        return resolve_probes(probes)

    collected = list(probes)
    if collected and all(isinstance(item, Probe) for item in collected):
        return collected  # type: ignore[return-value]
    return resolve_probes(collected)  # type: ignore[arg-type]


__all__ = ["PHASES", "ProbeConfig", "ScanResult", "TargetInfo", "scan"]
