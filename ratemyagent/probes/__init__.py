"""Probe registry.

Probes land here as they are built; anything in PLANNED is spec'd in CLAUDE.md
but not implemented yet, and asking for one by name says so rather than
silently scanning less than you asked for.
"""

from __future__ import annotations

from typing import Iterable

from .base import Probe, ProbeConfig, percentile
from .fault import FaultInjector
from .latency import LatencyProfiler

PROBES: dict[str, type[Probe]] = {
    LatencyProfiler.name: LatencyProfiler,
    FaultInjector.name: FaultInjector,
}

# Phases and weeks track the revised build plan in CLAUDE.md.
PLANNED: dict[str, str] = {
    "cost": "CostAnalyzer, phase 1 baseline (week 3)",
    "concurrency": "ConcurrencyTester, phase 1 baseline (week 3)",
    "contract": "ContractTester, phase 1 baseline (week 3)",
    "behavior": "BehaviorAnalyzer, phase 3 trajectory analysis (week 4)",
}

#: Pipeline order. A scan runs phases in this sequence.
PHASES: tuple[str, ...] = ("baseline", "chaos", "behavior")


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
    """Turn "latency,fault", ["latency"], "all", or None into probe instances."""
    if spec is None or spec == "all":
        return [cls() for _, cls in sorted(PROBES.items())]

    names = spec.split(",") if isinstance(spec, str) else list(spec)
    resolved = [get_probe(name) for name in names if name.strip()]
    if not resolved:
        raise KeyError("no probes selected")
    return resolved


def baseline_probes() -> list[Probe]:
    """Fresh instances of every phase 1 probe.

    The fault injector re-runs these against a wrapped target, so they must be
    new instances rather than the ones the scan is already driving.
    """
    return [cls() for cls in PROBES.values() if cls.phase == "baseline"]


def probes_in_phase(probes: Iterable[Probe], phase: str) -> list[Probe]:
    return [probe for probe in probes if probe.phase == phase]


def resolve_phases(spec: str | Iterable[str] | None = None) -> list[str]:
    """Turn "baseline,chaos" or None into an ordered, validated phase list."""
    if spec is None or spec == "all":
        return list(PHASES)

    names = spec.split(",") if isinstance(spec, str) else list(spec)
    requested = {name.strip().lower() for name in names if name.strip()}

    unknown = requested - set(PHASES)
    if unknown:
        raise KeyError(
            f"unknown phase {sorted(unknown)[0]!r}; expected one of {', '.join(PHASES)}"
        )
    if not requested:
        raise KeyError("no phases selected")
    return [phase for phase in PHASES if phase in requested]


__all__ = [
    "PHASES",
    "PLANNED",
    "PROBES",
    "FaultInjector",
    "LatencyProfiler",
    "Probe",
    "ProbeConfig",
    "available_probes",
    "baseline_probes",
    "get_probe",
    "percentile",
    "probes_in_phase",
    "resolve_phases",
    "resolve_probes",
]
