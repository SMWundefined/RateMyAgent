"""RateMyAgent: production reliability testing for AI agents, MCP servers, and LLM tools.

    from ratemyagent import MockTarget, scan

    result = await scan(MockTarget.healthy())
    print(result.overall_grade)
"""

from __future__ import annotations

from .models import (
    ErrorKind,
    FaultKind,
    Grade,
    Invocation,
    ProbeResult,
    Request,
    Response,
    ScanResult,
    TargetInfo,
    ToolInfo,
    Trajectory,
)
from .probes import FaultInjector, Probe, ProbeConfig, available_probes, get_probe
from .scanner import PHASES, scan
from .targets import (
    FaultConfig,
    FaultProxy,
    MCPTarget,
    MockTarget,
    Target,
    build_target,
)

__version__ = "0.1.0"

__all__ = [
    "ErrorKind",
    "FaultConfig",
    "FaultInjector",
    "FaultKind",
    "FaultProxy",
    "Grade",
    "Invocation",
    "MCPTarget",
    "MockTarget",
    "PHASES",
    "Probe",
    "ProbeConfig",
    "ProbeResult",
    "Request",
    "Response",
    "ScanResult",
    "Target",
    "TargetInfo",
    "ToolInfo",
    "Trajectory",
    "__version__",
    "available_probes",
    "build_target",
    "get_probe",
    "scan",
]
