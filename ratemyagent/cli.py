"""click CLI: `ratemyagent scan ...`."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import click

from . import __version__
from .models import ScanResult
from .outputs import render_scorecard
from .probes import PHASES, PLANNED, ProbeConfig, available_probes, resolve_phases, resolve_probes
from .scanner import scan as run_scan
from .targets import PLANNED_KINDS, TargetError, build_target

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

IMPLEMENTED_OUTPUTS = frozenset({"scorecard"})
PLANNED_OUTPUTS = {"report": "full markdown report (week 4)", "agents-md": "AGENTS.md (week 5)"}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="ratemyagent")
def cli() -> None:
    """SRE reliability scanner for AI agents, MCP servers, and LLM tools."""


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option(
    "--target",
    "target_kind",
    type=click.Choice(["mcp", "llm", "mock"]),
    required=True,
    help="What to scan. 'mock' runs against a built-in synthetic target, no server needed.",
)
@click.option("--uri", help="MCP endpoint: stdio://./server.py or sse://host:port/sse")
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), help="LLM provider.")
@click.option("--model", help="LLM model id.")
@click.option("--tool", help="MCP tool to probe. Defaults to the first tool discovered.")
@click.option("--tool-args", help="JSON object of arguments for --tool.")
@click.option(
    "--profile",
    type=click.Choice(["healthy", "degraded", "failing"]),
    default="healthy",
    show_default=True,
    help="Behavior of the mock target.",
)
@click.option(
    "--probes",
    "probe_spec",
    default="all",
    show_default=True,
    help=f"Comma-separated probes, or 'all'. Available: {', '.join(available_probes())}.",
)
@click.option(
    "--phases",
    "phase_spec",
    default="all",
    show_default=True,
    help=f"Comma-separated pipeline phases, or 'all'. Order is fixed: {', '.join(PHASES)}.",
)
@click.option(
    "--fault-rate",
    type=float,
    default=0.2,
    show_default=True,
    help="Share of calls the chaos phase faults, spread across all fault kinds.",
)
@click.option(
    "--output",
    type=click.Choice(["scorecard", "report", "agents-md", "all"]),
    default="scorecard",
    show_default=True,
    help="Output format.",
)
@click.option("--requests", "request_count", type=int, default=20, show_default=True,
              help="Requests per probe.")
@click.option("--concurrency", type=int, default=5, show_default=True,
              help="Max concurrent requests for the load tester.")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request timeout in seconds.")
@click.option("--warmup", type=int, default=1, show_default=True,
              help="Unmeasured requests sent before profiling.")
@click.option("--seed", type=int, default=1337, show_default=True,
              help="Seed for reproducible runs.")
@click.option("--json-out", type=click.Path(dir_okay=False, path_type=Path),
              help="Also write the full result as JSON. Name it *.scan.json to keep it "
                   "out of git.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def scan(
    target_kind: str,
    uri: str | None,
    provider: str | None,
    model: str | None,
    tool: str | None,
    tool_args: str | None,
    profile: str,
    probe_spec: str,
    phase_spec: str,
    fault_rate: float,
    output: str,
    request_count: int,
    concurrency: int,
    timeout: float,
    warmup: int,
    seed: int,
    json_out: Path | None,
    verbose: bool,
) -> None:
    """Scan a target and print a scorecard.

    \b
    Examples:
      ratemyagent scan --target mock --profile degraded
      ratemyagent scan --target mcp --uri stdio://./server.py
      ratemyagent scan --target mcp --uri stdio://./server.py --probes latency --requests 100
    """
    _configure_logging(verbose)

    if target_kind == "llm":
        raise click.UsageError(f"--target llm is not implemented yet: {PLANNED_KINDS['llm']}")
    if target_kind == "mcp" and not uri:
        raise click.UsageError("--target mcp needs --uri, e.g. --uri stdio://./server.py")
    if request_count < 1:
        raise click.UsageError("--requests must be at least 1")
    if warmup < 0:
        raise click.UsageError("--warmup cannot be negative")
    if not 0.0 <= fault_rate <= 1.0:
        raise click.UsageError("--fault-rate must be between 0 and 1")

    formats = IMPLEMENTED_OUTPUTS if output == "all" else {output}
    unsupported = formats - IMPLEMENTED_OUTPUTS
    if unsupported:
        name = sorted(unsupported)[0]
        raise click.UsageError(f"--output {name} is not implemented yet: {PLANNED_OUTPUTS[name]}")

    try:
        probes = resolve_probes(probe_spec)
        phases = resolve_phases(phase_spec)
    except KeyError as exc:
        raise click.UsageError(str(exc).strip("'")) from exc

    try:
        target = build_target(
            target_kind,
            uri=uri,
            tool=tool,
            tool_args=_parse_tool_args(tool_args),
            timeout_s=timeout,
            profile=profile,
            seed=seed,
        )
    except TargetError as exc:
        raise click.UsageError(str(exc)) from exc

    config = ProbeConfig(
        requests=request_count,
        concurrency=concurrency,
        timeout_s=timeout,
        warmup=warmup,
        seed=seed,
        extra={"fault_rate": fault_rate},
    )

    try:
        result = asyncio.run(
            run_scan(target, probes=probes, phases=phases, config=config)
        )
    except TargetError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(render_scorecard(result))

    if json_out:
        _write_json(result, json_out)
        click.echo(f"Wrote {json_out}")


@cli.command("probes")
def list_probes() -> None:
    """List the probes this build can run."""
    click.echo("Available:")
    for name in available_probes():
        from .probes import get_probe

        click.echo(f"  {name:<12} {get_probe(name).description}")

    click.echo("\nPlanned:")
    for name, note in PLANNED.items():
        click.echo(f"  {name:<12} {note}")


def _parse_tool_args(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"--tool-args must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise click.UsageError("--tool-args must be a JSON object")
    return parsed


def _write_json(result: ScanResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Console script entry point."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
