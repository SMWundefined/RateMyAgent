"""RateMyAgent: production reliability testing for AI agents, MCP servers, and LLM tools.

    from ratemyagent import MockTarget, scan

    result = await scan(MockTarget.healthy())
    print(result.score, result.passed)
"""

from __future__ import annotations

from .models import (
    CheckResult,
    ErrorKind,
    FaultKind,
    Invocation,
    ProbeResult,
    Request,
    Response,
    ScanResult,
    TargetInfo,
    ToolInfo,
    Trajectory,
)
from .policy import Policy, PolicyError
from .probes import (
    BehaviorAnalyzer,
    ConcurrencyTester,
    ContractTester,
    CostAnalyzer,
    FaultInjector,
    Probe,
    ProbeConfig,
    available_probes,
    get_probe,
)
from .scanner import PHASES, scan
from .targets import (
    FaultConfig,
    FaultProxy,
    LLMTarget,
    MCPTarget,
    MockTarget,
    Target,
    build_target,
)

__version__ = "0.1.2"

__all__ = [
    "BehaviorAnalyzer",
    "CheckResult",
    "ConcurrencyTester",
    "ContractTester",
    "CostAnalyzer",
    "ErrorKind",
    "FaultConfig",
    "FaultInjector",
    "FaultKind",
    "FaultProxy",
    "Invocation",
    "LLMTarget",
    "MCPTarget",
    "MockTarget",
    "PHASES",
    "Policy",
    "PolicyError",
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
