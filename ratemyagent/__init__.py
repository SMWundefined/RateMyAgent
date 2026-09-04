"""RateMyAgent: an SRE reliability scanner for AI agents, MCP servers, and LLM tools.

    from ratemyagent import MockTarget, scan

    result = await scan(MockTarget.healthy())
    print(result.overall_grade)
"""

from __future__ import annotations

from .models import (
    ErrorKind,
    Grade,
    ProbeResult,
    Request,
    Response,
    ScanResult,
    TargetInfo,
    ToolInfo,
)
from .probes import Probe, ProbeConfig, available_probes, get_probe
from .scanner import scan
from .targets import MCPTarget, MockTarget, Target, build_target

__version__ = "0.1.0"

__all__ = [
    "ErrorKind",
    "Grade",
    "MCPTarget",
    "MockTarget",
    "Probe",
    "ProbeConfig",
    "ProbeResult",
    "Request",
    "Response",
    "ScanResult",
    "Target",
    "TargetInfo",
    "ToolInfo",
    "__version__",
    "available_probes",
    "build_target",
    "get_probe",
    "scan",
]
