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
from dataclasses import dataclass
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
from .targets.base import Target, TargetError

logger = logging.getLogger(__name__)


class ScanTimeout(TargetError):
    """The scan exceeded its wall clock and was abandoned.

    A `TargetError` subclass so both CLI paths already treat it as "the scan did
    not happen" rather than "the target failed" -- those are different outcomes
    and must not share an exit code.
    """


@dataclass
class _Progress:
    """Where the scan had got to, so an expiry can say so.

    A bare kill tells you nothing. "Timed out during phase chaos, probe
    latency" tells you which server call to go and look at.
    """

    phase: str = "setup"
    probe: str | None = None

    def describe(self) -> str:
        if self.probe:
            return f"phase {self.phase}, probe {self.probe}"
        return f"phase {self.phase}"


async def _run_scan(
    target: Target,
    progress: _Progress,
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

            progress.phase, progress.probe = phase, None
            logger.info("phase %s: %s", phase, ", ".join(p.name for p in in_phase))
            if parallel:
                results = await asyncio.gather(
                    *(probe.execute(target, probe_config, context) for probe in in_phase)
                )
                context.results.extend(results)
            else:
                for probe in in_phase:
                    progress.probe = probe.name
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


async def scan(
    target: Target,
    *,
    probes: str | Iterable[str] | Iterable[Probe] | None = None,
    phases: str | Iterable[str] | None = None,
    config: ProbeConfig | None = None,
    policy: Policy | None = None,
    parallel: bool = False,
) -> ScanResult:
    """Run a scan under a wall-clock deadline.

    The deadline lives here rather than in a transport because every transport
    needs one and a scan can stall outside all of them. `MCPTarget` bounds each
    request with `asyncio.wait_for`, yet `mcp-server-fetch` still hung three
    times: entering `stdio_client()` waits for a subprocess handshake before any
    request exists, and closing the exit stack waits for it to go away. Both sit
    outside every per-request timeout. A bound in the engine covers setup,
    probes and teardown for stdio, SSE, HTTP, LLM and mock alike, and is written
    once.
    """
    probe_config = config or ProbeConfig()
    budget = probe_config.scan_budget()
    progress = _Progress()

    started = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _run_scan(
                target, progress, probes=probes, phases=phases,
                config=probe_config, policy=policy, parallel=parallel,
            ),
            timeout=budget,
        )
    except asyncio.CancelledError:
        # `wait_for` normally converts its own expiry into TimeoutError, but the
        # cancellation can escape as CancelledError when it lands inside a
        # nested cancel scope -- the MCP SDK's anyio task groups do this, and a
        # timed-out handshake surfaced as a raw
        # `CancelledError: Cancelled via cancel scope` traceback. That is the
        # bare kill the deadline exists to prevent.
        #
        # Only claim it as ours if the clock says so. A CancelledError raised
        # for any other reason -- Ctrl-C, an enclosing task group -- must
        # propagate untouched rather than be relabelled as a timeout.
        if time.perf_counter() - started < budget:
            raise
        raise ScanTimeout(_timeout_message(budget, progress)) from None
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ScanTimeout(_timeout_message(budget, progress)) from exc


def _timeout_message(budget: float, progress: _Progress) -> str:
    return (
        f"scan exceeded its {budget:.0f}s budget during {progress.describe()} "
        f"and was abandoned. The per-request --timeout does not bound a "
        f"handshake or a teardown, so a server that stops responding stalls "
        f"the scan rather than failing a request. Raise the budget with "
        f"--scan-timeout if the target is legitimately this slow."
    )
