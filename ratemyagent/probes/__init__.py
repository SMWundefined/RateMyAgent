"""Probe registry.

Probes land here as they are built; anything in PLANNED is spec'd in CLAUDE.md
but not implemented yet, and asking for one by name says so rather than
silently scanning less than you asked for.
"""

from __future__ import annotations

from typing import Iterable

from .base import Probe, ProbeConfig, ScanContext, percentile
from .behavior import BehaviorAnalyzer
from .concurrency import ConcurrencyTester
from .contract import ContractTester
from .cost import CostAnalyzer
from .fault import FaultInjector
from .latency import LatencyProfiler

PROBES: dict[str, type[Probe]] = {
    LatencyProfiler.name: LatencyProfiler,
    CostAnalyzer.name: CostAnalyzer,
    ConcurrencyTester.name: ConcurrencyTester,
    ContractTester.name: ContractTester,
    FaultInjector.name: FaultInjector,
    BehaviorAnalyzer.name: BehaviorAnalyzer,
}

# Phases and weeks track the revised build plan in CLAUDE.md.
PLANNED: dict[str, str] = {}

#: Order probes appear in within a phase. Latency first because every other
#: baseline number reads against it; contract last because it sends deliberate
#: garbage and should not colour the measurements before it.
PROBE_ORDER: tuple[str, ...] = (
    "latency", "cost", "concurrency", "contract", "fault", "behavior",
)

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


def _ordered(names: Iterable[str]) -> list[str]:
    """Pipeline order first, then anything unrecognized, alphabetically."""
    wanted = list(names)
    known = [name for name in PROBE_ORDER if name in wanted]
    return known + sorted(set(wanted) - set(known))


def resolve_probes(spec: str | Iterable[str] | None = None) -> list[Probe]:
    """Turn "latency,cost", ["latency"], "all", or None into probe instances."""
    if spec is None or spec == "all":
        return [PROBES[name]() for name in _ordered(PROBES)]

    names = spec.split(",") if isinstance(spec, str) else list(spec)
    resolved = [get_probe(name) for name in names if name.strip()]
    if not resolved:
        raise KeyError("no probes selected")
    return resolved


def baseline_probes() -> list[Probe]:
    """Fresh instances of every phase 1 probe, in pipeline order."""
    names = [name for name, cls in PROBES.items() if cls.phase == "baseline"]
    return [PROBES[name]() for name in _ordered(names)]


def fault_rerun_probes() -> list[Probe]:
    """Baseline probes worth re-running against a fault-injected target.

    Not every phase 1 probe survives the trip. Under injected faults the
    concurrency ramp would report the fault rate as its saturation point, and
    the contract probe would read injected transport errors as tools crashing
    on edge-case input -- both actively misleading rather than merely noisy.
    A probe opts in with `rerun_under_fault`.
    """
    names = [name for name, cls in PROBES.items() if cls.rerun_under_fault]
    return [PROBES[name]() for name in _ordered(names)]


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
    "PROBE_ORDER",
    "BehaviorAnalyzer",
    "ConcurrencyTester",
    "ContractTester",
    "CostAnalyzer",
    "FaultInjector",
    "LatencyProfiler",
    "Probe",
    "ProbeConfig",
    "ScanContext",
    "available_probes",
    "baseline_probes",
    "fault_rerun_probes",
    "get_probe",
    "percentile",
    "probes_in_phase",
    "resolve_phases",
    "resolve_probes",
]
