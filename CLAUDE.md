# RateMyAgent - CLAUDE.md

## What this project is

Production reliability testing for AI agents, MCP servers, and LLM-powered tools.

Thesis: Existing agent evaluation asks whether an agent can accomplish a task.
RateMyAgent asks whether the agent remains reliable when operated like a production
service — under load, latency, faults, and dependency failures.

Think: k6 + Chaos Monkey + pytest, but for agents and MCP tools.

Tagline: "Test AI agents like production services."

## Who uses this and why

Primary user: a developer who built an agent, MCP server, or LLM-powered tool
(often with AI assistance) and needs to know if it's production-ready before
deploying. They are NOT an SRE — they're the person the SRE would interrogate.

The majority of MCP servers, agents, and plugins in 2026 are AI-generated code.
This creates systematic reliability gaps that AI reviewing AI tends to miss:

- Schema declared but never enforced at runtime (tool says "required" but handler trusts input)
- No backpressure (accepts requests until it falls over, no concurrency limits)
- No graceful degradation (one dependency failure = total failure)
- No resource awareness (default configs, no connection pooling, no limits)
- Massive system prompts repeated verbatim on every call (no caching, massive cost)
- Retry logic that amplifies failures instead of absorbing them
- Broad exception handling that swallows errors instead of classifying them
- No cleanup on failure (connections leak, state left inconsistent)
- Verbose logging that costs money at scale

RateMyAgent catches these through BEHAVIOR testing, not code review. The probes
don't need to read the source — bad behavior is bad behavior regardless of who
wrote it. The contract probe finds unenforced schemas. The cost probe finds prompt
bloat. The concurrency probe finds missing backpressure. The fault injector finds
missing retry/backoff/circuit-breaker logic.

## Tech stack

- Python 3.10+
- httpx for async HTTP
- click for CLI
- mcp SDK for MCP server adapter
- anthropic / openai SDKs as optional deps for LLM adapter
- pytest + pytest-asyncio for testing
- No frontend for v1 - markdown output only

## Architecture

### Three-phase scan pipeline

The scan is NOT six independent probes. It flows in three phases:

Phase 1 — BASELINE: Measure how the target behaves under normal conditions.
  - Latency profiler (p50/p95/p99, TTFT, tool call overhead)
  - Cost analyzer (token usage, prompt bloat, $/req projection)
  - Concurrency tester (ramp 1->N, find saturation point)
  - Contract tester (MCP-specific: schema validation, edge-case fuzzing)

Phase 2 — FAULT INJECTION (the centerpiece): Break things systematically.
  - Timeout injection (configurable % of requests delayed)
  - Rate limit injection (429 responses)
  - Server error injection (500/503)
  - Malformed response injection (truncated, wrong schema)
  - Schema drift (tool description changes between calls)
  - Connection refused / dependency unavailability

Phase 3 — BEHAVIOR ANALYSIS: Study what the target did when things broke.
  - Retry pattern detection (does it retry? how many times?)
  - Retry amplification measurement (1 failure -> N retries)
  - Recovery quality (did it recover? how long? correct result?)
  - Duplicate detection (did it repeat a mutation?)
  - Loop detection (did it get stuck?)
  - Fallback behavior (did it degrade gracefully?)

Then: SCORING against a configurable reliability policy (not hardcoded A-F).

### Key design decisions already made

- FaultProxy wraps the Target adapter, not individual probes. Probes are unaware
  of whether they are hitting a real target or a chaos-wrapped one. This is the
  core abstraction — do not break it.
- Probes opt into rerun_under_fault. Only latency reruns under fault injection.
  Concurrency and contract produce misleading data under fault (concurrency finds
  the fault rate as saturation, contract reads injected errors as tool crashes).
- ProbeResult.applicable exists. Probes that do not apply to a target type (cost
  on MCP with no token reporting, contract on LLM targets) show n/a and are
  excluded from scoring. Do not grade a target on capabilities it does not have.
- Grading caps on thin evidence. Below 10 disrupted operations, fault tolerance
  caps at C with a finding explaining why. Do not report high confidence from
  small samples.
- Statistical honesty in findings. "Zero failures in 30 requests only bounds the
  error rate at roughly 10%, not 0%." Always state the confidence bound. Never
  claim certainty from insufficient data.
- Pricing is never guessed. If a model pricing is unknown, report tokens and
  say "no price known." A made-up number ends up in someone's budget.
- Mock targets report simulated latency instead of sleeping. A 50-request profile
  at 3s/request completes instantly while the probe arithmetic stays real.
- Fault injection is seeded per (trajectory, attempt), not per request. Seeding
  on request alone makes a faulted call fail forever — no target could be shown
  to recover. Both-attempts-faulted probability is verified at p^2.
- Malformed fault injection still calls the real target and pays its latency. A
  probe seeing malformed responses arrive instantly would measure the harness.
  It also refuses to corrupt an already-failed response.

### Entry points

1. CLI: ratemyagent scan --target mcp --uri stdio://./server.py
2. CLI: ratemyagent chaos --target mcp --uri stdio://./server.py --fault timeout --rate 20%
3. CLI: ratemyagent ci --policy production.yaml (exit code 0/1 for CI gates)
4. Python API: from ratemyagent import scan; results = await scan(target)

### Target adapters

Each target type implements a common interface:

    class Target(ABC):
        async def setup() -> None
        async def invoke(request) -> Response
        async def teardown() -> None
        def describe() -> TargetInfo
        def list_tools() -> list[ToolInfo]  # for MCP targets

v1 adapters:
- MCPTarget: connects via stdio or SSE, discovers tools, invokes them
- LLMTarget: Anthropic + OpenAI, max_retries=0 so the scan sees 429s
- MockTarget: healthy/degraded/failing/saturating/bloated profiles
- FaultProxy: wraps any Target, injects faults at configurable rates

v2 (NOT NOW): AgentTarget, HTTPTarget, KubernetesTarget

### Fault injection proxy

    class FaultProxy(Target):
        """Wraps a real Target and injects faults per FaultConfig.
        Composable: a proxy can wrap a proxy.
        Everything except invoke() delegates to the inner target,
        including probe_requests(), so phase 2 generates identical
        traffic to phase 1."""

### Behavior trajectory

Each invocation records a Trajectory with derived properties (not stored fields,
so it cannot go stale):

    @dataclass
    class Trajectory:
        invocations: list[Invocation]

        # All derived as properties:
        retries: int
        recovered: bool
        recovery_latency_s: float
        duplicates: int            # repeated successes = dangerous duplicate mutations
        loops_detected: bool       # same call pattern 3+ times
        final_status: str          # success / failed / abandoned / incorrect

### Reliability policy (replaces A-F in week 4)

    # production-default.yaml
    name: production-default
    thresholds:
      p95_latency_ms: 5000
      p99_latency_ms: 10000
      error_rate_max: 0.05
      recovery_rate_min: 0.90
      retry_amplification_max: 2.0
      duplicate_mutation_max: 0
      cost_per_request_max: 0.10
      concurrency_min: 5
    pass_score: 75

Users define their own policies. Ship with sensible defaults.
Score is 0-100, derived transparently from metric vs threshold distance.
A-F Grade enum is kept through week 3, swapped to policy scoring in week 4.

### Outputs

1. Scorecard: terminal-printed reliability score + per-dimension metrics
2. Report: full markdown with all findings, organized by phase
3. AGENTS.md: personalized fix guide (see dedicated section below)
4. CI exit code: 0 if score >= policy.pass_score, 1 otherwise

### AGENTS.md generator (week 5 — the killer feature)

This is NOT a generic linter output. It must be calibrated to the patterns that
AI-generated code specifically gets wrong, because that is the user base.

Bad output:

    FINDING: 9 schema-forbidden inputs accepted
    RECOMMENDATION: Add input validation

Good output:

    FINDING: 9 schema-forbidden inputs accepted

    Your tool declares required fields and types in its JSON Schema but does not
    enforce them at runtime. This is common in AI-generated MCP servers where
    the schema is correct but the handler trusts its input. Every field marked
    "required" needs an explicit check before the handler touches the data,
    because the calling agent WILL send malformed arguments — that is normal
    traffic, not an attack.

    Suggested fix for tool "search_database":

        if "query" not in args or not isinstance(args["query"], str):
            return {"error": "query is required and must be a string"}

Each finding should:
- State what was observed (data, not opinion)
- Explain WHY this matters in production (not just "best practice")
- Name the common root cause (especially AI-generated code patterns)
- Give a concrete, copy-pasteable fix when possible
- Reference the specific tool/endpoint/model that exhibited the problem

The AGENTS.md updates on re-scan with deltas:
"Latency improved from 3.2s to 1.8s since last scan. Contract still failing:
9 schema violations remain."

### CLI interface

    # Full scan
    ratemyagent scan --target mcp --uri stdio://./server.py

    # Mock targets (no API key, no network, no MCP server needed)
    ratemyagent scan --target mock --profile healthy --requests 30
    ratemyagent scan --target mock --profile degraded --requests 50
    ratemyagent scan --target mock --profile failing --requests 40
    ratemyagent scan --target mock --profile saturating --requests 24 --concurrency 32

    # Cost with manual pricing
    ratemyagent scan --target mock --profile bloated --probes cost --price-in 5 --price-out 25

    # Chaos mode
    ratemyagent chaos --target mcp --uri stdio://./server.py --fault timeout --rate 20%

    # CI mode
    ratemyagent ci --target mcp --uri stdio://./server.py --policy production.yaml

    # Control
    ratemyagent scan ... --phases baseline,chaos
    ratemyagent scan ... --probes latency,cost,contract
    ratemyagent scan ... --output scorecard|report|agents-md|all
    ratemyagent scan ... --seed 42
    ratemyagent scan ... --requests 50 --concurrency 10 --fault-rate 0.2
    ratemyagent scan ... -v

    # List probes
    ratemyagent probes

## Project structure

    ratemyagent/
      __init__.py
      cli.py                  # click CLI (scan, chaos, ci, probes commands)
      scanner.py              # orchestrates the 3-phase pipeline
      models.py               # Trajectory, Invocation, ProbeResult, ScanResult, etc.
      policy.py               # load/validate reliability policies, compute scores
      targets/
        __init__.py
        base.py               # Target ABC + exception taxonomy classifier
        mcp.py                # MCPTarget (stdio/SSE)
        llm.py                # LLMTarget (Anthropic/OpenAI, max_retries=0)
        mock.py               # healthy/degraded/failing/saturating/bloated profiles
        fault_proxy.py        # FaultProxy wrapper
      probes/
        __init__.py
        base.py               # Probe ABC, ProbeConfig, percentiles, failure containment
        latency.py            # Phase 1: p50/p95/p99, TTFT, overhead, statistical bounds
        cost.py               # Phase 1: tokens, bloat, $/req, caching recommendations
        concurrency.py        # Phase 1: ramp 1->N, saturation, goodput, early stop >50%
        contract.py           # Phase 1: schema audit + 6 edge cases per tool
        fault.py              # Phase 2: reruns baseline through FaultProxy, recovery
        behavior.py           # Phase 3: trajectory analysis
      outputs/
        __init__.py
        scorecard.py          # terminal output with per-probe breakdown
        report.py             # full markdown report (week 5)
        agents_md.py          # AGENTS.md generator (week 5)
      policies/
        production-default.yaml
    tests/
      conftest.py             # mock targets, fixtures
      test_models.py
      test_scanner.py
      test_probes/
        test_latency.py
        test_cost.py
        test_concurrency.py
        test_contract.py
        test_fault.py
        test_behavior.py
      test_targets/
        test_fault_proxy.py
        test_llm.py
    examples/
      scan_mcp_example.py

## Build plan (6 weeks)

Week 1: core engine + target adapter + latency probe + MCP adapter. DONE. (177 tests)
Week 2: FaultProxy + fault injection + trajectory model. DONE. (301 tests)
Week 3: cost + concurrency + contract probes + LLM adapter. DONE. (425 tests)
Week 4: behavior analysis + policy engine (A-F -> 0-100) + CI mode. IN PROGRESS.
Week 5: AGENTS.md generator + full markdown report.
Week 6: docs, README, blog post.

## Coding conventions

- async/await throughout
- Type hints on all public APIs
- No emojis in code
- No print() — use logging or click.echo
- Each probe must work independently
- All probes must have tests that run without API keys (use mock targets)
- The FaultProxy must be the ONLY place faults are injected — probes stay clean
- Error handling: classify into ErrorKind taxonomy, never broad except Exception
- Resource cleanup: async with or explicit finally for Target connections
- Mock targets must have realistic failure modes, not patterns probes look for

## Code quality checks (apply to this codebase too)

This tool is itself AI-generated. Guard against the same patterns it detects:

1. Check every except block. Each should catch a specific exception type and
   classify it into ErrorKind. Broad except Exception that swallows errors
   is the number one AI-code anti-pattern.

2. Verify resource cleanup. If a scan crashes mid-run, does it close MCP
   connections? Check for async with context managers on Target, or explicit
   finally blocks in the scanner.

3. Verify mock realism. Mocks should not have patterns that probes are specifically
   tuned to detect. The saturating mock should fail in ways a real server fails
   (connection pool exhaustion, increasing latency then errors), not in ways
   that happen to trigger the probe grading thresholds.

4. Check for input validation in the CLI. Does --requests accept 0? Does
   --fault-rate accept 2.0? Edge cases in CLI args should be caught with clear
   error messages.

## What NOT to build (scope guard)

- No web dashboard
- No database / persistence beyond JSON + YAML files
- No auto-discovery (ratemyagent scan . detecting frameworks)
- No real-time monitoring or continuous scanning
- No framework-specific adapters (LangChain, CrewAI etc)
- No security scanning (MCP-Scan covers that)
- No dependency failure graphs (v2)
- No historical trending / regression detection across scans (v2)
- No burst/outage-window fault mode (v2 — current faults are independent per attempt)
- No timeout-after-completion fault for duplicate mutation testing (v2)
- No infrastructure/deployment testing (GCP config, K8s manifests, autoscaling)
- No paid features or SaaS
- No formal reliability model / math notation
If you find yourself working on any of these, stop.

## Competitive positioning

DO NOT compete with:
- Langfuse/LangSmith/Opik — they observe production, we test BEFORE production
- MCP-Scan — they check if tools are malicious, we check if tools are reliable
- DeepEval/RAGAS/Promptfoo — they check output quality, we check operational behavior
- Flakestorm — they test prompt robustness, we test infrastructure resilience
- k6/Locust — they load-test HTTP endpoints, we understand agent-specific failure modes

DO own:
- "What happens when your agents tools and dependencies fail?"
- SRE reliability testing layer between generic eval and production deployment
- Fault injection + trajectory-level recovery measurement + CI regression testing
- Actionable findings calibrated to AI-generated code patterns