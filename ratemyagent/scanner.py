"""Scan orchestration: set up a target, run probes against it, aggregate."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from .models import ScanResult, TargetInfo
from .probes import Probe, ProbeConfig, resolve_probes
from .targets.base import Target

logger = logging.getLogger(__name__)


async def scan(
    target: Target,
    *,
    probes: str | Iterable[str] | Iterable[Probe] | None = None,
    config: ProbeConfig | None = None,
    parallel: bool = False,
) -> ScanResult:
    """Run probes against a target and return the aggregate result.

    Probes are sequential by default. They are written to run concurrently and
    `parallel=True` will do it, but sharing one target means a concurrent run
    measures the probes interfering with each other -- a latency profile taken
    while the load tester saturates the same server is not a latency profile.
    Turn it on when wall clock matters more than clean numbers.
    """
    selected = _as_probes(probes)
    probe_config = config or ProbeConfig()

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()

    await target.setup()
    try:
        info = target.describe()
        logger.info(
            "scanning %s (%s) with %s",
            info.name,
            info.kind,
            ", ".join(p.name for p in selected),
        )

        if parallel:
            results = list(
                await asyncio.gather(*(p.execute(target, probe_config) for p in selected))
            )
        else:
            results = [await probe.execute(target, probe_config) for probe in selected]
    finally:
        await target.teardown()

    return ScanResult(
        target=info,
        probes=results,
        started_at=started_at,
        duration_s=time.perf_counter() - started,
        config={**probe_config.to_dict(), "parallel": parallel},
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


__all__ = ["ProbeConfig", "ScanResult", "TargetInfo", "scan"]
