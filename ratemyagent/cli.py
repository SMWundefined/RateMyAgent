"""click CLI: `ratemyagent scan ...`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from . import __version__
from .models import ScanResult
from .outputs import render_report, render_scorecard, write_agents_md
from .outputs.agents_md import applicable_advice
from .policy import (
    DEFAULT_POLICY_PATH,
    Policy,
    PolicyError,
    agent_verdict_blocker,
    verify_not_measured,
)
from .probes import (
    AGENT_PROBES,
    PHASES,
    PLANNED,
    SERVICE_ONLY_PROBES,
    ProbeConfig,
    available_probes,
    resolve_phases,
    resolve_probes,
)
from .proxy import serve
from .scanner import scan as run_scan
from .targets import TargetError, build_target
from .targets.agent import RECORD_ENV, SCHEDULE_ENV, TASK_ENV
from .targets.fault_proxy import DEFAULT_CLOSE_AFTER_S

logger = logging.getLogger(__name__)

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

IMPLEMENTED_OUTPUTS = frozenset({"scorecard", "report", "agents-md"})
PLANNED_OUTPUTS: dict[str, str] = {}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="ratemyagent")
def cli() -> None:
    """SRE reliability scanner for AI agents, MCP servers, and LLM tools."""


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option(
    "--target",
    "target_kind",
    type=click.Choice(["mcp", "llm", "mock", "agent"]),
    required=True,
    help="What to scan. 'mock' runs against a built-in synthetic target, no server needed. "
         "'agent' scans an agent process through a proxy it launches: see --agent, "
         "--tasks and --upstream.",
)
@click.option(
    "--uri",
    help="MCP endpoint: https://host/mcp (Streamable HTTP), stdio://./server.py, "
         "or sse://host:port/sse (deprecated by the 2025-06-18 spec, still works).",
)
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), help="LLM provider.")
@click.option("--model", help="LLM model id.")
@click.option("--tool", help="MCP tool to probe. Defaults to the first tool discovered.")
@click.option("--tool-args", help="JSON object of arguments for --tool.")
@click.option(
    "--agent", "agent_command", metavar="CMD",
    help="The command that launches the agent under test. --target agent only.",
)
@click.option(
    "--tasks", "tasks_path", type=click.Path(dir_okay=False, path_type=Path),
    help="JSON file of tasks: id, prompt, expected_effects, tool, arguments. "
         "--target agent only. Each task is one operation; --requests does not "
         "multiply them.",
)
@click.option(
    "--upstream", metavar="URI",
    help="The MCP server the proxy sits in front of, in --uri's syntax. "
         "--target agent only. Deliberately not --uri: for an agent scan the "
         "target is the agent, and reusing --uri would make a frozen flag mean "
         "two things.",
)
@click.option(
    "--agent-command", "agent_argv", metavar="TEMPLATE",
    help="Argv appended to --agent, with {config}, {prompt}, {task_id} and "
         "{tasks} placeholders. --target agent only. Defaults to 1.5.1's fixed "
         "argv, so a scan that omits this builds the command it always did. A "
         "template you supply must contain {config} and {prompt}.",
)
@click.option(
    "--claim-path", metavar="PATH",
    help="Dot-separated path to the claim object inside a single JSON document "
         "on the agent's stdout, e.g. structured_output. --target agent only. "
         "Omit it and the claim is read from the last JSON line, as before.",
)
@click.option(
    "--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path),
    help="Where per-task records, configs and schedules are written. --target "
         "agent only. Defaults to a new directory under the system temp, which "
         "is wiped on restart; name one you keep if the records are evidence.",
)
@click.option(
    "--lost-reply-close-after", "lost_reply_close_after",
    type=float, is_flag=False, flag_value=str(DEFAULT_CLOSE_AFTER_S), default=None,
    metavar="SECONDS",
    help="Close the session this many seconds after a reply is dropped, instead "
         "of leaving the agent waiting. --target agent only. Swaps "
         "RESPONSE_LOST for RESPONSE_LOST_THEN_CLOSED; the kind count and every "
         "seeded draw are unchanged. Bare flag uses "
         f"{DEFAULT_CLOSE_AFTER_S:g}s. Needed against a client that sets no "
         "read timeout, which otherwise waits until the task deadline.",
)
@click.option(
    "--profile",
    type=click.Choice(["healthy", "degraded", "failing", "saturating", "bloated"]),
    default="healthy",
    show_default=True,
    help="Behavior of the mock target.",
)
@click.option("--price-in", type=float,
              help="USD per 1M input tokens, overriding the built-in price table.")
@click.option("--price-out", type=float,
              help="USD per 1M output tokens, overriding the built-in price table.")
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
    "--max-retries", type=int, default=2, show_default=True,
    help="Retries a disrupted operation gets before it counts as unrecovered. "
         "Also sets the recovery floor it is graded against, which is "
         "1 - fault_rate ** max_retries. Minimum 1.",
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
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Reliability policy YAML. Defaults to the shipped production-default.")
@click.option("--json-out", type=click.Path(dir_okay=False, path_type=Path),
              help="Also write the full result as JSON. Name it *.scan.json to keep it "
                   "out of git.")
@click.option("--report-out", type=click.Path(dir_okay=False, path_type=Path),
              default="REPORT.md", show_default=True,
              help="Where --output report writes the markdown report.")
@click.option("--agents-md-out", type=click.Path(dir_okay=False, path_type=Path),
              default="AGENTS.md", show_default=True,
              help="Where --output agents-md writes. An existing file is diffed "
                   "against, so the guide reports what changed.")
@click.option(
    "--allow-mutating", is_flag=True,
    help="Permit probing a tool that changes state. Probing calls it once per "
         "request and again under fault injection, so point it at something "
         "disposable.",
)
@click.option(
    "--verify-tool", "verify_tool",
    help="A READ-ONLY tool that reports the server's state, so applied effects "
         "can be counted. Requires --allow-mutating. --target mcp: called before "
         "and after the retried operations, with a state-changing --tool and "
         "{op_id} in --tool-args to count per operation. --target agent: "
         "called on the --upstream before and after each task, whose state "
         "must persist outside its "
         "process; required for a verdict. Without it, duplicate mutations stay "
         "n/a: the scan sees deliveries, not effects.",
)
@click.option(
    "--verify-args", "verify_args",
    help="JSON object of arguments for --verify-tool. Defaults to {}.",
)
@click.option(
    "--verify-count", "verify_count", metavar="PATH",
    help="Dotted path to the entries inside the verify tool's result, e.g. "
         "'entities'. Must resolve to a list when --tool-args carries {op_id}; "
         "a bare number is aggregate mode, where effects cannot be attributed "
         "to an operation and both metrics stay n/a.",
)
@click.option(
    "--backoff-max", "backoff_max", type=float, default=5.0, show_default=True,
    help="Longest a single retry waits after a rate limit. A Retry-After hint "
         "is honoured up to this ceiling; 0 disables waiting.",
)
@click.option(
    "--backoff-budget", "backoff_budget", type=float, default=30.0, show_default=True,
    help="Total seconds a scan spends waiting on rate limits. When exhausted, "
         "retries continue without waiting and the report says how many did.",
)
@click.option(
    "--env", "env_vars", multiple=True, metavar="KEY=VALUE",
    help="Environment variable for a stdio:// server, repeatable. "
         "THE PARENT ENVIRONMENT IS NOT INHERITED: the MCP SDK copies only "
         "HOME, LOGNAME, PATH, SHELL, TERM and USER into the child, so a "
         "credential exported in your shell does not reach the server and it "
         "may degrade to an unauthenticated mode without failing. Values are "
         "redacted wherever a scan is written down.",
)
@click.option(
    "--header", "headers", multiple=True, metavar="'Key: Value'",
    help="Header sent on every request, e.g. 'Authorization: Bearer ...'. "
         "Repeatable. http/sse only, and redacted in reports and JSON.",
)
@click.option(
    "--scan-timeout", "scan_timeout", type=float, default=None,
    help="Wall clock for the whole scan, in seconds. Distinct from --timeout, "
         "which bounds one request. Defaults to a generous budget derived from "
         "--timeout and --requests; it exists to turn a hang into a clean error.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def scan(
    target_kind: str,
    uri: str | None,
    provider: str | None,
    model: str | None,
    tool: str | None,
    tool_args: str | None,
    agent_command: str | None,
    tasks_path: Path | None,
    upstream: str | None,
    agent_argv: str | None,
    claim_path: str | None,
    work_dir: Path | None,
    lost_reply_close_after: float | None,
    verify_tool: str | None,
    verify_args: str | None,
    verify_count: str | None,
    headers: tuple[str, ...],
    env_vars: tuple[str, ...],
    backoff_max: float,
    backoff_budget: float,
    scan_timeout: float | None,
    allow_mutating: bool,
    profile: str,
    price_in: float | None,
    price_out: float | None,
    probe_spec: str,
    phase_spec: str,
    fault_rate: float,
    max_retries: int,
    output: str,
    request_count: int,
    concurrency: int,
    timeout: float,
    warmup: int,
    seed: int,
    policy_path: Path | None,
    json_out: Path | None,
    report_out: Path,
    agents_md_out: Path,
    verbose: bool,
) -> None:
    """Scan a target and report on it.

    \b
    Examples:
      ratemyagent scan --target mock --profile degraded
      ratemyagent scan --target mcp --uri stdio://./server.py
      ratemyagent scan --target mcp --uri stdio://./server.py --probes latency --requests 100
      ratemyagent scan --target mock --output agents-md
      ratemyagent scan --target mock --output all
    """
    _configure_logging(verbose)

    if target_kind == "llm" and not provider:
        raise click.UsageError(
            "--target llm needs --provider anthropic or --provider openai"
        )
    if target_kind == "mcp" and not uri:
        raise click.UsageError("--target mcp needs --uri, e.g. --uri stdio://./server.py")

    probe_spec = _agent_probe_spec(
        target_kind, probe_spec,
        agent_command=agent_command, tasks_path=tasks_path, upstream=upstream,
        agent_argv=agent_argv, claim_path=claim_path, work_dir=work_dir,
        lost_reply_close_after=lost_reply_close_after,
    )

    if request_count < 1:
        raise click.UsageError("--requests must be at least 1")
    if warmup < 0:
        raise click.UsageError("--warmup cannot be negative")
    if not 0.0 <= fault_rate <= 1.0:
        raise click.UsageError("--fault-rate must be between 0 and 1")

    formats = set(IMPLEMENTED_OUTPUTS) if output == "all" else {output}
    unsupported = formats - IMPLEMENTED_OUTPUTS
    if unsupported:
        name = sorted(unsupported)[0]
        raise click.UsageError(f"--output {name} is not implemented yet: {PLANNED_OUTPUTS[name]}")

    try:
        probes = resolve_probes(probe_spec)
        phases = resolve_phases(phase_spec)
    except KeyError as exc:
        raise click.UsageError(str(exc).strip("'")) from exc

    policy = _load_policy(policy_path)

    try:
        target = build_target(
            target_kind,
            uri=uri,
            tool=tool,
            tool_args=_parse_tool_args(tool_args),
            verify_tool=verify_tool,
            verify_args=_parse_tool_args(verify_args),
            verify_count=verify_count,
            headers=_parse_headers(headers),
            env=_parse_env(env_vars),
            allow_mutating=allow_mutating,
            timeout_s=timeout,
            profile=profile,
            provider=provider,
            model=model,
            seed=seed,
            agent_command=agent_command,
            tasks_path=tasks_path,
            upstream=upstream,
            agent_argv=agent_argv,
            claim_path=claim_path,
            work_dir=work_dir,
        )
    except TargetError as exc:
        raise click.UsageError(str(exc)) from exc

    if max_retries < 1:
        raise click.UsageError(
            "--max-retries must be at least 1. At 0 the derived recovery floor "
            "is 1 - fault_rate**0 = 0, which every target clears."
        )

    config = ProbeConfig(
        requests=request_count,
        concurrency=concurrency,
        timeout_s=timeout,
        scan_timeout_s=scan_timeout,
        warmup=warmup,
        seed=seed,
        max_retries=max_retries,
        backoff_max_s=backoff_max,
        backoff_budget_s=backoff_budget,
        extra={
            "fault_rate": fault_rate,
            "model": model,
            "price_in": price_in,
            "price_out": price_out,
            "lost_reply_close_after": lost_reply_close_after,
        },
    )

    try:
        result = asyncio.run(
            run_scan(target, probes=probes, phases=phases, config=config, policy=policy)
        )
    except TargetError as exc:
        # Exit 2, matching `ci`. A ClickException exits 1, which is the code for
        # "the target failed its policy" -- a scan that never completed is a
        # different outcome and CI must be able to tell them apart.
        if json_out:
            _write_refusal_json(target, exc, json_out)
            click.echo(f"Wrote {json_out}")
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc

    if "scorecard" in formats:
        # The hint sits inside the scorecard so the verdict stays the last two
        # lines; printing it afterwards would displace what CI greps for.
        hint = None if "agents-md" in formats else _agents_md_hint(result)
        click.echo(
            render_scorecard(result, hint=hint, color=True, show_all_caveats=verbose),
            nl=False,
        )

    if "report" in formats:
        _write_text(render_report(result), report_out)
        click.echo(f"Report written to {report_out}")

    if "agents-md" in formats:
        # Diffs against whatever is already there, so a re-scan reports movement.
        write_agents_md(result, agents_md_out)
        click.echo(_agents_md_summary(result, agents_md_out))

    if json_out:
        _write_json(result, json_out)
        click.echo(f"Wrote {json_out}")


@cli.command("ci", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--target", "target_kind", type=click.Choice(["mcp", "llm", "mock", "agent"]),
    required=True,
    help="What to scan. 'agent' is experimental: see --agent, --tasks and --upstream.",
)
@click.option("--agent", "agent_command", metavar="CMD",
              help="The command that launches the agent under test. --target agent only.")
@click.option("--tasks", "tasks_path", type=click.Path(dir_okay=False, path_type=Path),
              help="JSON task file. --target agent only.")
@click.option("--agent-command", "agent_argv", metavar="TEMPLATE",
              help="Argv appended to --agent, with {config}, {prompt}, {task_id} "
                   "and {tasks} placeholders. --target agent only.")
@click.option("--claim-path", metavar="PATH",
              help="Dot-separated path to the claim object in a single JSON "
                   "document on stdout. --target agent only.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path),
              help="Where records, configs and schedules are written. "
                   "--target agent only.")
@click.option("--lost-reply-close-after", "lost_reply_close_after", type=float,
              is_flag=False, flag_value=str(DEFAULT_CLOSE_AFTER_S), default=None,
              metavar="SECONDS",
              help="Close the session this many seconds after a dropped reply. "
                   "--target agent only.")
@click.option("--upstream", metavar="URI",
              help="The MCP server the proxy sits in front of. --target agent only.")
@click.option(
    "--uri",
    help="MCP endpoint: https://host/mcp (Streamable HTTP), stdio://./server.py, "
         "or sse://host:port/sse (deprecated by the 2025-06-18 spec, still works).",
)
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), help="LLM provider.")
@click.option("--model", help="LLM model id.")
@click.option("--tool", help="MCP tool to probe.")
@click.option("--tool-args", help="JSON object of arguments for --tool.")
@click.option(
    "--allow-mutating", is_flag=True,
    help="Permit probing a tool that changes state.",
)
@click.option(
    "--verify-tool", "verify_tool",
    help="A READ-ONLY tool reporting the server's state, for counting applied "
         "effects. Requires --allow-mutating. --target mcp: with a state-changing "
         "--tool. --target agent: read on --upstream around each task; required "
         "for a verdict.",
)
@click.option("--verify-args", "verify_args", help="JSON arguments for --verify-tool.")
@click.option(
    "--verify-count", "verify_count", metavar="PATH",
    help="Dotted path to the entries in the verify tool's result.",
)
@click.option(
    "--profile",
    type=click.Choice(["healthy", "degraded", "failing", "saturating", "bloated"]),
    default="healthy", show_default=True, help="Behavior of the mock target.",
)
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Reliability policy YAML. Defaults to the shipped production-default.")
@click.option("--requests", "request_count", type=int, default=20, show_default=True)
@click.option("--concurrency", type=int, default=5, show_default=True)
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.option("--fault-rate", type=float, default=0.2, show_default=True)
@click.option("--max-retries", type=int, default=2, show_default=True)
@click.option("--seed", type=int, default=1337, show_default=True)
@click.option("--price-in", type=float, help="USD per 1M input tokens.")
@click.option("--price-out", type=float, help="USD per 1M output tokens.")
@click.option("--json-out", type=click.Path(dir_okay=False, path_type=Path),
              help="Also write the full result as JSON.")
@click.option(
    "--backoff-max", "backoff_max", type=float, default=5.0, show_default=True,
    help="Longest a single retry waits after a rate limit. A Retry-After hint "
         "is honoured up to this ceiling; 0 disables waiting.",
)
@click.option(
    "--backoff-budget", "backoff_budget", type=float, default=30.0, show_default=True,
    help="Total seconds a scan spends waiting on rate limits. When exhausted, "
         "retries continue without waiting and the report says how many did.",
)
@click.option(
    "--env", "env_vars", multiple=True, metavar="KEY=VALUE",
    help="Environment variable for a stdio:// server, repeatable. "
         "THE PARENT ENVIRONMENT IS NOT INHERITED: the MCP SDK copies only "
         "HOME, LOGNAME, PATH, SHELL, TERM and USER into the child, so a "
         "credential exported in your shell does not reach the server and it "
         "may degrade to an unauthenticated mode without failing. Values are "
         "redacted wherever a scan is written down.",
)
@click.option(
    "--header", "headers", multiple=True, metavar="'Key: Value'",
    help="Header sent on every request, e.g. 'Authorization: Bearer ...'. "
         "Repeatable. http/sse only, and redacted in reports and JSON.",
)
@click.option(
    "--scan-timeout", "scan_timeout", type=float, default=None,
    help="Wall clock for the whole scan, in seconds. Distinct from --timeout, "
         "which bounds one request. Defaults to a generous budget derived from "
         "--timeout and --requests; it exists to turn a hang into a clean error.",
)
@click.option("--quiet", is_flag=True, help="Print only the verdict line.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def ci(
    target_kind: str,
    agent_command: str | None,
    tasks_path: Path | None,
    upstream: str | None,
    agent_argv: str | None,
    claim_path: str | None,
    work_dir: Path | None,
    lost_reply_close_after: float | None,
    uri: str | None,
    provider: str | None,
    model: str | None,
    tool: str | None,
    tool_args: str | None,
    allow_mutating: bool,
    verify_tool: str | None,
    verify_args: str | None,
    verify_count: str | None,
    headers: tuple[str, ...],
    env_vars: tuple[str, ...],
    backoff_max: float,
    backoff_budget: float,
    scan_timeout: float | None,
    profile: str,
    policy_path: Path | None,
    request_count: int,
    concurrency: int,
    timeout: float,
    fault_rate: float,
    max_retries: int,
    seed: int,
    price_in: float | None,
    price_out: float | None,
    json_out: Path | None,
    quiet: bool,
    verbose: bool,
) -> None:
    """Run a full scan and exit non-zero if it misses the policy.

    Exit codes: 0 the score met pass_score, 1 it did not, 2 the scan could not
    run at all -- or, for --target agent, it ran without a verdict. A gate that
    cannot distinguish "your agent regressed" from "the scanner broke" is not a
    gate worth having in a pipeline.

    \b
    Examples:
      ratemyagent ci --target mock --profile healthy
      ratemyagent ci --target mcp --uri stdio://./server.py --policy production.yaml
    """
    _configure_logging(verbose)

    if target_kind == "llm" and not provider:
        raise click.UsageError("--target llm needs --provider anthropic or --provider openai")
    if target_kind == "mcp" and not uri:
        raise click.UsageError("--target mcp needs --uri, e.g. --uri stdio://./server.py")

    probe_spec = _agent_probe_spec(
        target_kind, None,
        agent_command=agent_command, tasks_path=tasks_path, upstream=upstream,
        agent_argv=agent_argv, claim_path=claim_path, work_dir=work_dir,
        lost_reply_close_after=lost_reply_close_after,
    )
    policy = _load_policy(policy_path)

    # Bound before the try so a refusal record can name the target even when
    # `build_target` is what raised.
    target = None
    try:
        target = build_target(
            target_kind, uri=uri, tool=tool, timeout_s=timeout, profile=profile,
            provider=provider, model=model, seed=seed,
            agent_command=agent_command, tasks_path=tasks_path, upstream=upstream,
            tool_args=_parse_tool_args(tool_args),
            allow_mutating=allow_mutating,
            verify_tool=verify_tool,
            verify_args=_parse_tool_args(verify_args),
            verify_count=verify_count,
            headers=_parse_headers(headers),
            env=_parse_env(env_vars),
        )
        config = ProbeConfig(
            requests=request_count, concurrency=concurrency, timeout_s=timeout,
            scan_timeout_s=scan_timeout, seed=seed, max_retries=max_retries,
            backoff_max_s=backoff_max, backoff_budget_s=backoff_budget,
            extra={
                "fault_rate": fault_rate, "model": model,
                "price_in": price_in, "price_out": price_out,
                "lost_reply_close_after": lost_reply_close_after,
            },
        )
        result = asyncio.run(
            run_scan(target, probes=probe_spec, config=config, policy=policy)
        )
    except (TargetError, PolicyError) as exc:
        # Exit 2: the scan never happened, which is not the same as a failing
        # target and should not be reported as one.
        if json_out:
            _write_refusal_json(target, exc, json_out)
            click.echo(f"Wrote {json_out}")
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc

    if not quiet:
        click.echo(render_scorecard(result, color=True), nl=False)

    if result.score is None:
        click.echo(
            f"FAIL  no policy threshold in {policy.name} could be evaluated against "
            "this scan",
            err=True,
        )
        raise SystemExit(1)

    blocker = agent_verdict_blocker(result)
    if blocker is not None:
        # An agent scan without a verdict (C2). Exit 2 for 1.4.1's reason:
        # nothing failed a policy, the measurement a verdict needs did not
        # happen, and a gate must not go green -- or red -- on that.
        # The scan completed, so its record is written: a NO VERDICT is exactly
        # the run someone will want to read afterwards.
        if json_out:
            _write_json(result, json_out)
        click.echo(f"NO VERDICT  {blocker}", err=True)
        raise SystemExit(2)

    unmeasured = verify_not_measured(result)
    if unmeasured is not None:
        # Exit 2, not 1 (1.4.1). Exit 1 is documented as "policy failure", and
        # nothing failed a policy here: `duplicate_mutations` was withheld, so
        # the check never ran. A scan that did not perform the measurement it
        # was asked for did not complete, which is exit 2's meaning -- and it is
        # the code that keeps a gate from going green on an unmeasured oracle.
        status, reason = unmeasured
        click.echo(
            f"NOT MEASURED  --verify-tool was requested and did not measure "
            f"({status}): {reason}",
            err=True,
        )
        raise SystemExit(2)

    verdict = "PASS" if result.passed else "FAIL"
    click.echo(
        f"{verdict}  score {result.score:.1f}/100  "
        f"(policy {policy.name} requires {policy.pass_score:g})"
    )

    if not result.passed:
        for check in result.failed_checks:
            click.echo(f"  failed: {check.name} -- {check.reason}", err=True)

    if json_out:
        _write_json(result, json_out)

    raise SystemExit(0 if result.passed else 1)


@cli.command("policy")
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Policy YAML to show. Defaults to the shipped production-default.")
def show_policy(policy_path: Path | None) -> None:
    """Show a policy and what each threshold reads."""
    from .policy import SPECS_BY_NAME

    policy = _load_policy(policy_path)
    click.echo(f"{policy.name}  (pass_score {policy.pass_score:g})")
    if policy.description:
        click.echo(f"  {policy.description.strip()}")
    click.echo("\nThresholds:")
    for spec in policy.specs:
        value = policy.thresholds[spec.name]
        click.echo(
            f"  {spec.name:<32} {value:<10g} {spec.direction:<4} "
            f"<- {spec.probe}.{spec.metric}"
        )

    unset = [name for name in SPECS_BY_NAME if name not in policy.thresholds]
    if unset:
        click.echo("\nNot set (not scored):")
        for name in unset:
            click.echo(f"  {name}")


@cli.command("proxy", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--upstream", required=True, metavar="URI",
    help="The MCP server to sit in front of, in --uri's syntax: "
         "stdio://./server.py, https://host/mcp, sse://host/sse.",
)
@click.option(
    "--record", "record_path", metavar="PATH",
    help="Where to append the call record, one JSON row per call. Defaults to "
         "$RMA_PROXY_RECORD, then to a temp file whose path is logged.",
)
@click.option(
    "--schedule", "schedule_path", metavar="PATH",
    help="Forced fault table, written by the scan. Defaults to "
         "$RMA_PROXY_SCHEDULE. Without one, no faults are injected.",
)
@click.option(
    "--task-id", "task_id", metavar="ID",
    help="Which task these calls belong to. Defaults to $RMA_TASK_ID.",
)
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request timeout against the upstream, in seconds.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging, on stderr.")
def proxy(
    upstream: str,
    record_path: str | None,
    schedule_path: str | None,
    task_id: str | None,
    timeout: float,
    verbose: bool,
) -> None:
    """Sit between an agent and an MCP server, injecting faults and recording.

    A stdio MCP server on its own stdin/stdout, holding
    FaultProxy(MCPTarget(--upstream)) underneath. **The agent launches this, not
    the scanner**: it is a peer of `scan` and `ci`, named in the agent's own MCP
    config, which is the only channel that reaches a subprocess the agent
    spawned.

    Options fall back to the environment because that config's `env` block is
    how a scan passes them: the MCP SDK copies six named variables into a stdio
    child and drops everything else, so RMA_PROXY_RECORD travels in the block or
    not at all.

    \b
    Example, as an MCP config entry:
      {"mcpServers": {"ratemyagent": {
          "command": "ratemyagent",
          "args": ["proxy", "--upstream", "stdio://./server.py"],
          "env": {"RMA_PROXY_RECORD": "/tmp/run/record-t1.jsonl",
                  "RMA_PROXY_SCHEDULE": "/tmp/run/schedule.json",
                  "RMA_TASK_ID": "t1"}}}}
    """
    # stderr, never stdout: stdout is the MCP wire. A log line written there
    # would be a protocol error the agent reports as a broken server.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    record = record_path or os.environ.get(RECORD_ENV)
    if not record:
        handle, record = tempfile.mkstemp(prefix="ratemyagent-record-", suffix=".jsonl")
        os.close(handle)
        logger.warning("no --record and no %s; recording to %s", RECORD_ENV, record)

    try:
        raise SystemExit(asyncio.run(serve(
            upstream=upstream,
            record_path=record,
            schedule_path=schedule_path or os.environ.get(SCHEDULE_ENV),
            task_id=task_id or os.environ.get(TASK_ENV),
            timeout_s=timeout,
        )))
    except TargetError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc


@cli.command("probes")
def list_probes() -> None:
    """List the probes this build can run."""
    click.echo("Available:")
    for name in available_probes():
        from .probes import get_probe

        click.echo(f"  {name:<12} {get_probe(name).description}")

    if PLANNED:
        click.echo("\nPlanned:")
        for name, note in PLANNED.items():
            click.echo(f"  {name:<12} {note}")


def _agent_probe_spec(
    target_kind: str,
    probe_spec: str | None,
    *,
    agent_command: str | None,
    tasks_path: Path | None,
    upstream: str | None,
    agent_argv: str | None = None,
    claim_path: str | None = None,
    work_dir: Path | None = None,
    lost_reply_close_after: float | None = None,
) -> str | None:
    """Validate the agent-only flags and pick the probe set, for `scan` and `ci`.

    Refused, never ignored. The precedent is `--header` on stdio: a flag that is
    silently dropped is a flag the user believes they set, and here it would be
    one they believe configured the scan's traffic.

    `--verify-tool` is accepted on an agent target as of C2, where it brackets
    each task. It is required for a verdict rather than for the scan: without it
    the scan still runs, reports, and says NO VERDICT.
    """
    agent_only = [
        flag for flag, value in (
            ("--agent", agent_command), ("--tasks", tasks_path), ("--upstream", upstream),
            ("--agent-command", agent_argv), ("--claim-path", claim_path),
            ("--work-dir", work_dir),
            ("--lost-reply-close-after", lost_reply_close_after),
        ) if value is not None and value != ""
    ]
    if target_kind != "agent":
        if agent_only:
            raise click.UsageError(
                f"{', '.join(agent_only)} {'is' if len(agent_only) == 1 else 'are'} "
                f"--target agent only, and --target {target_kind} was given."
            )
        return probe_spec

    if probe_spec not in (None, "all"):
        refused = [
            name for name in probe_spec.split(",")
            if name.strip().lower() in SERVICE_ONLY_PROBES
        ]
        if refused:
            raise click.UsageError(
                f"--probes {', '.join(sorted(refused))} does not apply to "
                f"--target agent: latency measures task wall clock, cost has "
                f"no tokens to count, concurrency destroys per-task "
                f"attribution, and contract fuzzes a tool surface an agent "
                f"does not have. The agent probe set is "
                f"{', '.join(AGENT_PROBES)}."
            )
        return probe_spec
    return ",".join(AGENT_PROBES)


def _agents_md_summary(result: ScanResult, path: Path) -> str:
    """What was written, and how much of it needs attention first."""
    sections = applicable_advice(result)
    critical = sum(1 for advice in sections if advice.critical)

    detail = f"{len(sections)} recommendation{'' if len(sections) == 1 else 's'}"
    if critical:
        detail += f", {critical} critical"
    return f"AGENTS.md written to {path} ({detail})"


def _agents_md_hint(result: ScanResult) -> str | None:
    """Point at the fix guide when there is something to fix.

    Printed rather than prompted: an SRE tool has to stay pipeable and
    non-blocking, so this never asks a question.
    """
    findings = sum(len(probe.findings) for probe in result.probes)
    probes = sum(1 for probe in result.probes if probe.findings)
    if not findings:
        return None

    # Only point at the guide when it would actually have something to say. A
    # clean scan still emits informational findings, and sending someone to a
    # file that reads "nothing to fix" wastes their time.
    if not applicable_advice(result):
        return None

    return (
        f"{findings} finding{'' if findings == 1 else 's'} across "
        f"{probes} probe{'' if probes == 1 else 's'}. "
        "Run with --output agents-md to generate a fix guide."
    )


def _load_policy(path: Path | None) -> Policy:
    """Load a policy file, or the shipped default when none is given."""
    try:
        return Policy.load(path or DEFAULT_POLICY_PATH)
    except PolicyError as exc:
        raise click.UsageError(str(exc)) from exc


def _parse_env(raw: tuple[str, ...]) -> dict[str, str] | None:
    """Turn repeated `--env KEY=VALUE` into a dict.

    Only `KEY=VALUE`. There is deliberately no `--env-from KEY` forwarding a
    variable by name from the parent: forwarding by name is one keystroke from
    forwarding by pattern, and the SDK's six-variable allowlist exists to stop a
    scan handing every credential in the shell to whatever subprocess a `--uri`
    names. Naming the value is the friction, and that is the point.
    """
    if not raw:
        return None

    env: dict[str, str] = {}
    for item in raw:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise click.BadParameter(f"{item!r} is not KEY=VALUE", param_hint="--env")
        env[name.strip()] = value
    return env


def _parse_headers(raw: tuple[str, ...]) -> dict[str, str] | None:
    """Turn repeated `--header 'Key: Value'` into a dict."""
    if not raw:
        return None

    headers: dict[str, str] = {}
    for item in raw:
        name, separator, value = item.partition(":")
        if not separator or not name.strip():
            raise click.BadParameter(
                f"{item!r} is not 'Key: Value'", param_hint="--header"
            )
        headers[name.strip()] = value.strip()
    return headers

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


def _write_text(document: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def _write_json(result: ScanResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


def _write_refusal_json(target: Any, exc: Exception, path: Path) -> None:
    """The record of a scan that refused before it ran (1.5.1).

    `--json-out` used to be written only when a scan produced a `ScanResult`, so
    a stale-state refusal at setup -- exit 2, nothing measured -- left a CI job
    with an exit code and no file, while `docs/SCANNING.md` read as though every
    exit-2 case wrote one. Found in the 1.5.0 gate B re-run.

    **Deliberately not a `ScanResult`.** An empty result with a null score is a
    scan that measured nothing, and this is a scan that never started; writing
    one would put those two on the same shape. This document carries the three
    things there are: that it refused, why, and what it was pointed at. No
    frozen field changes, and nothing here is frozen either -- a reader tells it
    from a scan by `refused`, which a scan export never has.
    """
    document: dict[str, Any] = {
        "refused": True,
        "reason": str(exc),
        "target": None,
        "refused_at": datetime.now(timezone.utc).isoformat(),
    }
    if target is not None:
        try:
            document["target"] = target.describe().to_dict()
        except Exception:  # noqa: BLE001 - a refusal must not fail on its own record
            logger.debug("target could not describe itself for the refusal record")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Console script entry point, and the last guarantee of the exit contract.

    **Any unexpected exception becomes exit 2 with one line.** The contract is
    0 pass, 1 the target failed its policy, 2 the scan did not complete, and
    until now it was enforced only at three `except TargetError` sites. Anything
    that escaped those reached the user as a traceback -- and under a shell that
    reads the exit code, as *exit 1*, which says the target failed a policy it
    never got measured against.

    Four cancel-scope escapes reached a user that way (0.1.8, 0.1.16, and twice
    on 2026-09-10), and each was fixed at the call site that happened to be
    failing. None asked why an escape reaches a user at all. Per-site handling
    gives a *good* message; this gives *a* message, for the site nobody has
    enumerated yet.

    `BaseException`, not `Exception`, because `CancelledError` is the whole
    reason this exists. `KeyboardInterrupt` is let through deliberately -- a
    user pressing Ctrl-C does not need a diagnosis -- and `SystemExit` carries
    the codes the commands set on purpose.
    """
    try:
        cli()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 - the point is to catch anything
        logger.debug("unhandled exception", exc_info=True)
        click.echo(
            f"error: the scan did not complete: {type(exc).__name__}: {exc}\n"
            "This is a bug in ratemyagent rather than a result about the "
            "target. Re-run with -v for the traceback.",
            err=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
