"""Scan an MCP server from Python.

    python examples/scan_mcp_example.py                       # built-in mock, no deps
    python examples/scan_mcp_example.py stdio://./server.py   # a real MCP server

The mock path needs nothing installed beyond ratemyagent itself, so this file
doubles as a smoke test.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from ratemyagent import MCPTarget, MockTarget, ProbeConfig, Target, scan
from ratemyagent.outputs import render_scorecard


def build_target(uri: str | None) -> Target:
    if uri is None:
        logging.info("no URI given, scanning the built-in degraded mock target")
        return MockTarget.degraded()
    # Pass tool=... to profile something other than the first discovered tool:
    # probing invokes it for real, once per request.
    return MCPTarget(uri, timeout_s=30.0)


async def main(uri: str | None) -> int:
    target = build_target(uri)

    result = await scan(
        target,
        probes="latency",
        config=ProbeConfig(requests=25, warmup=2, timeout_s=30.0, seed=42),
    )

    click.echo(render_scorecard(result))

    latency = result.probe("latency")
    if latency is not None:
        click.echo(f"p95: {latency.metrics['p95_s']:.3f}s")
        click.echo(f"error rate: {latency.error_rate:.1%}")

    # Non-zero exit on a failing grade; the real CI gate arrives in week 4.
    return 0 if result.overall_grade.points >= 2 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
