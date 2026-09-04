# ratemyagent

Test AI agents like production services.

Existing agent evaluation asks whether an agent can accomplish a task. RateMyAgent asks
whether it stays reliable when operated like a production service -- under load, latency,
faults, and dependency failures.

Think k6 + Chaos Monkey + pytest, but for agents and MCP tools.

## Status

Week 2 of 6. Baseline profiling and fault injection both work today: a scan runs
phase 1 against the real target, then phase 2 against a fault-injected one.
Behavior analysis and policy scoring land in week 4.

## Try it

No API key, no server, and no network needed: scan the built-in mock target.

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run ratemyagent scan --target mock --profile degraded --requests 50 --fault-rate 0.25
```

```
RateMyAgent Scan Results
========================

Target: degraded-mock (mock)
Probes: 2/2 complete   Duration: 0.00s

Phase 1  baseline
  Latency ............... C  (p50 3.00s, p95 7.99s, p99 8.48s over 50 requests (0.0% errors))

Phase 2  chaos (fault injection)
  Fault tolerance ....... A  (22 faults injected, 12/12 operations recovered (100%), 1.26x call amplification)

  Overall: B

Latency findings:
  - p95 latency is 7.99s, which grades C (under 10s). An A
    needs p95 under 2s.

Fault tolerance findings:
  - Injected 22 faults across 114 calls (19%): 8 server_error,
    4 connection_refused, 4 malformed, 3 timeout, 3
    rate_limit.
  - Every one of the 12 disrupted operations recovered within
    2 retries.
  - Under fault the latency probe grades F with a 18% error
    rate.

Not run (4):
  cost         CostAnalyzer, phase 1 baseline (week 3)
  concurrency  ConcurrencyTester, phase 1 baseline (week 3)
  contract     ContractTester, phase 1 baseline (week 3)
  behavior     BehaviorAnalyzer, phase 3 trajectory analysis (week 4)
```

Against a real MCP server over stdio or SSE:

```bash
ratemyagent scan --target mcp --uri stdio://./server.py
ratemyagent scan --target mcp --uri sse://localhost:8080/sse --requests 100
```

Probing invokes a discovered tool for real, once per request. Pass `--tool` and
`--tool-args` to choose which one; the default is the first tool the server reports.

## How a scan works

Three phases, not six independent probes:

1. **Baseline** -- behavior under normal conditions. Latency today; cost, concurrency,
   and MCP contract testing to follow.
2. **Fault injection** -- timeouts, 429s, 500s, malformed responses, schema drift, and
   dependency failures, injected by a proxy the probes cannot see. (week 2)
3. **Behavior analysis** -- what the target *did* when things broke: retries, retry
   amplification, recovery time, duplicate mutations, loops. (week 4)

Results are then scored against a configurable reliability policy, with a CI exit code.

## Working today

- **Latency profiler** -- p50/p95/p99, TTFT, and tool call overhead, with findings that
  distinguish a slow median from a heavy tail
- **Fault injection** -- a `FaultProxy` wraps any target and injects timeouts, 429s, 500s,
  malformed responses, and refused connections at a configurable rate. Probes cannot tell
  they are wrapped, so phase 2 measures the same things as phase 1 under duress
- **Trajectories** -- every attempt at an operation is recorded, yielding retries,
  recovery rate, recovery latency, retry amplification, and duplicate mutations
- **MCP adapter** -- stdio and SSE, tool discovery, arguments synthesized from each
  tool's JSON Schema
- **Mock targets** -- healthy, degraded, and failing profiles, deterministic per seed, so
  the whole test suite runs without API keys
- **Output** -- terminal scorecard and full JSON export (`--json-out run.scan.json`)

Grades are capped when the evidence is thin: a recovery rate measured from two disrupted
operations is not evidence of resilience, and the scorecard says so rather than awarding
an A.

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --fault-rate 0.3
ratemyagent scan --target mock --phases baseline        # skip the chaos phase
```

Run `ratemyagent scan --help` for the full option list, or `ratemyagent probes` to see
which probes this build can run.

## Development

```bash
uv run pytest        # 177 tests, no network or API keys
uv run ruff check .
```

Python 3.10+. Architecture and the six-week plan live in [CLAUDE.md](CLAUDE.md).

## License

MIT
