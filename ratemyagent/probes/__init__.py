"""Probe registry.

Probes land here as they are built; anything in PLANNED is spec'd in CLAUDE.md
but not implemented yet, and asking for one by name says so rather than
silently scanning less than you asked for.
"""

from __future__ import annotations

from typing import Iterable

from .base import Probe, ProbeConfig, percentile
from .latency import LatencyProfiler

PROBES: dict[str, type[Probe]] = {
    LatencyProfiler.name: LatencyProfiler,
}

# Phases and weeks track the revised build plan in CLAUDE.md.
PLANNED: dict[str, str] = {
    "fault": "FaultInjector, phase 2 chaos (week 2)",
    "cost": "CostAnalyzer, phase 1 baseline (week 3)",
    "concurrency": "ConcurrencyTester, phase 1 baseline (week 3)",
    "contract": "ContractTester, phase 1 baseline (week 3)",
    "behavior": "BehaviorAnalyzer, phase 3 trajectory analysis (week 4)",
}


def available_probes() -> list[str]:
    return sorted(PROBES)


def get_probe(name: str) -> Probe:
    """Instantiate one probe by name."""
    key = name.strip().lower()
    if key in PROBES:
        return PROBES[key]()
    if key in PLANNED:
        raise KeyError(f"probe {name!r} is not implemented yet: {PLANNED[key]}")
    raise KeyError(f"unknown probe {name!r}; available: {', '.join(available_probes())}")


def resolve_probes(spec: str | Iterable[str] | None = None) -> list[Probe]:
    """Turn "latency,cost", ["latency"], "all", or None into probe instances."""
    if spec is None or spec == "all":
        return [cls() for _, cls in sorted(PROBES.items())]

    names = spec.split(",") if isinstance(spec, str) else list(spec)
    resolved = [get_probe(name) for name in names if name.strip()]
    if not resolved:
        raise KeyError("no probes selected")
    return resolved


__all__ = [
    "PLANNED",
    "PROBES",
    "LatencyProfiler",
    "Probe",
    "ProbeConfig",
    "available_probes",
    "get_probe",
    "percentile",
    "resolve_probes",
]
