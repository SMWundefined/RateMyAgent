"""Target adapters and the factory the CLI builds them with."""

from __future__ import annotations

from typing import Any

from .agent import AgentTarget
from .base import Target, TargetError, classify_exception, error_response
from .fault_proxy import FaultConfig, FaultProxy, wrap
from .llm import LLMTarget
from .mcp import MCPTarget
from .mock import MockTarget

TARGET_KINDS: tuple[str, ...] = ("mcp", "llm", "mock", "agent")
PLANNED_KINDS: dict[str, str] = {}


def build_target(kind: str, **kwargs: Any) -> Target:
    """Construct a target by kind name.

    Unknown extras are dropped rather than passed through, so the CLI can hand
    over every option it parsed without each adapter having to accept all of them.
    """
    key = kind.strip().lower()

    if key == "mcp":
        uri = kwargs.get("uri")
        if not uri:
            raise TargetError("--target mcp needs --uri, e.g. stdio://./server.py")
        return MCPTarget(
            uri,
            tool=kwargs.get("tool"),
            tool_args=kwargs.get("tool_args"),
            timeout_s=kwargs.get("timeout_s", 30.0),
            env=kwargs.get("env"),
            headers=kwargs.get("headers"),
            allow_mutating=kwargs.get("allow_mutating", False),
            verify_tool=kwargs.get("verify_tool"),
            verify_args=kwargs.get("verify_args"),
            verify_count=kwargs.get("verify_count"),
        )

    if key == "agent":
        missing = [
            flag for flag, value in (
                ("--agent", kwargs.get("agent_command")),
                ("--tasks", kwargs.get("tasks_path")),
                ("--upstream", kwargs.get("upstream")),
            ) if not value
        ]
        if missing:
            raise TargetError(
                f"--target agent needs {', '.join(missing)}. The agent is the "
                "target, the tasks are the traffic, and --upstream is the MCP "
                "server the proxy sits in front of -- deliberately not --uri, "
                "which names the target."
            )
        return AgentTarget(
            agent_command=kwargs["agent_command"],
            tasks_path=kwargs["tasks_path"],
            upstream=kwargs["upstream"],
            timeout_s=kwargs.get("timeout_s", 30.0),
            allow_mutating=kwargs.get("allow_mutating", False),
            agent_argv=kwargs.get("agent_argv"),
            claim_path=kwargs.get("claim_path"),
            work_dir=kwargs.get("work_dir"),
            verify_tool=kwargs.get("verify_tool"),
            verify_args=kwargs.get("verify_args"),
            verify_count=kwargs.get("verify_count"),
        )

    if key == "llm":
        provider = kwargs.get("provider")
        if not provider:
            raise TargetError(
                "--target llm needs --provider anthropic or --provider openai"
            )

        # Only forward what was actually supplied, so LLMTarget's own defaults
        # apply to everything else.
        optional = {
            name: kwargs[name]
            for name in ("model", "api_key", "prompt", "system", "max_tokens")
            if kwargs.get(name) is not None
        }
        return LLMTarget(provider, timeout_s=kwargs.get("timeout_s", 30.0), **optional)

    if key == "mock":
        profile = (kwargs.get("profile") or "healthy").lower()
        factories = {
            "healthy": MockTarget.healthy,
            "degraded": MockTarget.degraded,
            "failing": MockTarget.failing,
            "saturating": MockTarget.saturating,
            "bloated": MockTarget.bloated,
        }
        factory = factories.get(profile)
        if factory is None:
            raise TargetError(
                f"unknown mock profile {profile!r}; "
                f"expected one of {', '.join(factories)}"
            )
        return factory(seed=kwargs.get("seed", 1337))

    if key in PLANNED_KINDS:
        raise TargetError(f"target {key!r} is not implemented yet: {PLANNED_KINDS[key]}")

    raise TargetError(f"unknown target kind {kind!r}; expected one of {', '.join(TARGET_KINDS)}")


__all__ = [
    "AgentTarget",
    "FaultConfig",
    "FaultProxy",
    "LLMTarget",
    "MCPTarget",
    "MockTarget",
    "PLANNED_KINDS",
    "TARGET_KINDS",
    "Target",
    "TargetError",
    "build_target",
    "classify_exception",
    "error_response",
    "wrap",
]
