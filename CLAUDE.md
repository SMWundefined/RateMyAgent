# RateMyAgent - CLAUDE.md

## What this project is

Production reliability testing for AI agents, MCP servers, and LLM-powered tools.

Thesis: Existing agent evaluation asks whether an agent can accomplish a task.
RateMyAgent asks whether the agent remains reliable when operated like a production
service — under load, latency, faults, and dependency failures.

Think: k6 + Chaos Monkey + pytest, but for agents and MCP tools.

Tagline: "Test AI agents like production services."

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
  - Dependency unavailability (connection refused)

Phase 3 — BEHAVIOR ANALYSIS: Study what the target did when things broke.
  - Retry pattern detection (does it retry? how many times?)
  - Retry amplification measurement (1 failure -> N retries)
  - Recovery quality (did it recover? how long? correct result?)
  - Duplicate detection (did it repeat a mutation?)
  - Loop detection (did it get stuck?)
  - Fallback behavior (did it degrade gracefully?)

Then: SCORING against a configurable reliability policy (not hardcoded A-F).

### Entry points

1. CLI: `ratemyagent scan --target mcp --uri stdio://./server.py`
2. CLI: `ratemyagent chaos --target mcp --uri stdio://./server.py --fault timeout --rate 20%`
3. CLI: `ratemyagent ci --policy production.yaml` (exit code 0/1 for CI gates)
4. Python API: `from ratemyagent import scan; results = await scan(target)`

### Target adapters

Each target type implements a common interface:

```
class Target(ABC):
    async def setup() -> None
    async def invoke(request) -> Response
    async def teardown() -> None
    def describe() -> TargetInfo
    def list_tools() -> list[ToolInfo]  # for MCP targets
```

v1 adapters:
- MCPTarget: connects via stdio or SSE, discovers tools, invokes them
- LLMTarget: hits chat completions endpoint (Anthropic or OpenAI)

v2 (NOT NOW): AgentTarget, HTTPTarget, KubernetesTarget

### Fault injection proxy

The fault injector wraps the target adapter with a proxy layer:

```
class FaultProxy(Target):
    """Wraps a real Target and injects faults according to a FaultConfig."""
    def __init__(self, inner: Target, faults: FaultConfig): ...
    async def invoke(self, request) -> Response:
        if should_inject(self.faults):
            return inject_fault(self.faults)  # timeout, 429, 500, etc.
        return await self.inner.invoke(request)
```

This is the key abstraction: probes don't know whether they're talking to a real
target or a fault-injected one. Phase 1 runs against the real target. Phase 2
runs the same probes against the FaultProxy-wrapped target. Phase 3 compares.

### Behavior trajectory

Each invocation records a Trajectory:

```
@dataclass
class Trajectory:
    invocations: list[Invocation]   # every call made
    retries: int                     # retry count
    recovery: bool                   # did it eventually succeed?
    recovery_latency_ms: float       # time from first failure to success
    duplicates: int                  # repeated identical calls
    loops_detected: bool             # same call pattern repeated 3+ times
    final_status: str                # success / failed / abandoned / incorrect
```

This is what separates RateMyAgent from generic load testers. We don't just
measure "did it work?" — we measure "what did it DO when things went wrong?"

### Reliability policy (replaces hardcoded A-F)

```yaml
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
pass_score: 75   # 0-100, below this = CI fail
```

Users define their own policies. Ship with sensible defaults.
Score is 0-100, derived transparently from metric vs threshold distance.

### Outputs

1. Scorecard: terminal-printed reliability score + per-dimension metrics
2. Report: full markdown with all findings, organized by phase
3. AGENTS.md: personalized fix guide. Includes specific findings + recommendations.
   Updates on re-scan with deltas ("latency improved from 3.2s to 1.8s since last scan").
4. CI exit code: 0 if score >= policy.pass_score, 1 otherwise

### CLI interface

```bash
# Full scan
ratemyagent scan --target mcp --uri stdio://./server.py

# Chaos mode (fault injection only, specific scenarios)
ratemyagent chaos --target mcp --uri stdio://./server.py --fault timeout --rate 20%

# CI mode (returns exit code)
ratemyagent ci --target mcp --uri stdio://./server.py --policy production.yaml

# Control probes and output
ratemyagent scan ... --phases baseline,chaos   # skip behavior analysis
ratemyagent scan ... --output scorecard
ratemyagent scan ... --output report
ratemyagent scan ... --output agents-md
ratemyagent scan ... --output all

# Reproducibility
ratemyagent scan ... --seed 42

# Configuration
ratemyagent scan ... --requests 50
ratemyagent scan ... --concurrency 10
ratemyagent scan ... --fault-rate 0.2
```

## Project structure

```
ratemyagent/
  __init__.py
  cli.py                  # click CLI (scan, chaos, ci commands)
  scanner.py              # orchestrates the 3-phase pipeline
  models.py               # Trajectory, ProbeResult, ScanResult, Policy, etc.
  policy.py               # load/validate reliability policies, compute scores
  targets/
    __init__.py
    base.py               # Target ABC
    mcp.py                # MCPTarget
    llm.py                # LLMTarget
    fault_proxy.py         # FaultProxy wrapper
  probes/
    __init__.py
    base.py               # Probe ABC
    latency.py             # Phase 1
    cost.py                # Phase 1
    concurrency.py         # Phase 1
    contract.py            # Phase 1 (MCP tool schema/edge-case testing)
    fault.py               # Phase 2 (orchestrates fault injection)
    behavior.py            # Phase 3 (trajectory analysis)
  outputs/
    __init__.py
    scorecard.py           # terminal output
    report.py              # markdown report
    agents_md.py           # AGENTS.md generator
  policies/
    production-default.yaml
tests/
  conftest.py              # mock targets
  test_models.py
  test_scanner.py
  test_probes/
    test_latency.py
    test_cost.py
    test_contract.py
    test_fault.py
    test_behavior.py
  test_targets/
    test_fault_proxy.py
examples/
  scan_mcp_example.py
```

## Build plan (6 weeks, revised)

Week 1: repo + core engine + target adapter + latency probe + MCP adapter. DONE.
Week 2: fault injection proxy + timeout/429/500 injection + behavior trajectory model.
        This is the centerpiece — get chaos working against a mock MCP server.
Week 3: cost probe + concurrency probe + contract probe. Scorecard terminal output.
Week 4: behavior analysis (retry detection, amplification, recovery grading).
        Policy engine + scoring. CI exit code mode.
Week 5: AGENTS.md generator. Full markdown report.
Week 6: docs, README update, blog post "I chaos-tested MCP servers and here's what broke."

## Coding conventions

- async/await throughout
- Type hints on all public APIs
- No emojis in code
- No print() — use logging or click.echo
- Each probe must work independently
- All probes must have tests that run without API keys (use mock targets)
- The FaultProxy must be the ONLY place faults are injected — probes stay clean

## What NOT to build (scope guard)

- No web dashboard
- No database / persistence beyond JSON + YAML files
- No auto-discovery (`ratemyagent scan .` detecting frameworks)
- No real-time monitoring
- No framework-specific adapters (LangChain, CrewAI etc)
- No security scanning (MCP-Scan covers that)
- No dependency failure graphs (v2)
- No historical trending / regression detection across scans (v2)
- No paid features or SaaS
- No formal reliability model / math notation (build empirical data first)
If you find yourself working on any of these, stop.

## Competitive positioning

DO NOT compete with:
- Langfuse/LangSmith/Opik (observability — they watch production, we test before production)
- MCP-Scan (security — they check if tools are malicious, we check if tools are reliable)
- DeepEval/RAGAS/Promptfoo (evaluation — they check output quality, we check operational behavior)
- Flakestorm (adversarial — they test prompt robustness, we test infrastructure resilience)

DO own:
- "What happens when your agent's tools and dependencies fail?"
- SRE reliability testing layer between generic eval and production deployment
- Fault injection + trajectory-level recovery measurement + CI regression testing