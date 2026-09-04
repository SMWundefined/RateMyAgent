# ratemyagent

Test AI agents like production services.

Existing agent evaluation asks whether an agent can accomplish a task. RateMyAgent asks
whether it stays reliable when operated like a production service -- under load, latency,
faults, and dependency failures.

Think k6 + Chaos Monkey + pytest, but for agents and MCP tools.

## Status

Week 4 of 6. All three phases run, and results are scored 0-100 against a
configurable reliability policy with a CI exit code. The markdown report and the
AGENTS.md generator land in week 5.

## Try it

No API key, no server, and no network needed: scan the built-in mock target.

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run ratemyagent scan --target mock --profile healthy --requests 60 \
    --concurrency 16 --fault-rate 0.3
```

```
RateMyAgent Scan Results
========================

Target: healthy-mock (mock)
Probes: 6/6 complete   Duration: 0.01s

Phase 1  baseline
  Latency ............... 100.0  (p50 0.35s, p95 0.44s, p99 0.45s over 60 requests (0.0% errors))
  Cost ..................   n/a  (614 in / 120 out tokens per request, no price known for this model)
  Concurrency ........... 100.0  (no saturation up to 16 concurrent, sustained 16)
  Contract ..............  50.0  (18 edge cases across 3 tools: 0 rejected cleanly, 18 accepted, 0 crashed)

Phase 2  chaos (fault injection)
  Fault tolerance .......     -  (39 faults injected, 21/22 operations recovered (95%), 1.45x call amplification)

Phase 3  behavior analysis
  Behavior .............. 100.0  (21/22 disrupted operations recovered (95%), 1.45x call amplification, 0 duplicate mutations)

  Score: 88.9/100  [PASS, policy requires 75]

Policy checks (production-default):
  FAIL contract_invalid_accepted_max      0.0  invalid inputs accepted was 9, policy allows at most 0
  ok   p95_latency_ms                   100.0  p95 latency was 435ms, policy allows at most 5,000ms
  ok   error_rate_max                   100.0  error rate was 0.0%, policy allows at most 5.0%
  ok   concurrency_min                  100.0  sustained concurrency was 16, policy allows at least 5
  ok   recovery_rate_min                100.0  recovery rate was 95.5%, policy allows at least 90.0%
  ok   retry_amplification_max          100.0  retry amplification was 1.45x, policy allows at most 2.00x
  ok   duplicate_mutation_max           100.0  duplicate mutations was 0, policy allows at most 0
  --   cost_per_request_max                -   skipped: the cost probe reported no cost per request

  n/a = probe could not measure this target;  - = no policy threshold reads it
```

Every number is auditable: each policy threshold reports what was required, what was
seen, and what it scored. `n/a` means the probe could not measure this target -- here, no
published price for a mock -- and those are left out of the score rather than counted as
failures.

Against a real MCP server over stdio or SSE:

```bash
ratemyagent scan --target mcp --uri stdio://./server.py
ratemyagent scan --target mcp --uri sse://localhost:8080/sse --requests 100
```

Probing invokes a discovered tool for real, once per request. Pass `--tool` and
`--tool-args` to choose which one; the default is the first tool the server reports.

Against a chat completions endpoint (this one spends money -- keep `--requests` low):

```bash
uv pip install -e '.[anthropic]'   # or '.[openai]', or '.[all]'

ratemyagent scan --target llm --provider anthropic --model claude-opus-5 --requests 5
ratemyagent scan --target llm --provider openai --model gpt-4o-mini --requests 5
```

## How a scan works

Three phases, not six independent probes:

1. **Baseline** -- behavior under normal conditions: latency, cost, concurrency, and
   MCP contract testing.
2. **Fault injection** -- timeouts, 429s, 500s, malformed responses, and dependency
   failures, injected by a proxy the probes cannot see.
3. **Behavior analysis** -- what the target *did* when things broke: retries, retry
   amplification, recovery time, duplicate mutations, loops.

Results are then scored 0-100 against a configurable reliability policy.

## Working today

- **Latency profiler** -- p50/p95/p99, TTFT, and tool call overhead, with findings that
  distinguish a slow median from a heavy tail
- **Cost analyzer** -- tokens per request, prompt-bloat detection (the fixed prefix
  resent on every call, and what caching it would save), and a $/request projection.
  Prices are never guessed: an unknown model gets token counts and a note, or you
  pass `--price-in` / `--price-out`
- **Concurrency tester** -- ramps 1->N, reports the saturation point where errors cross
  5%, and a latency knee for targets that saturate by slowing rather than failing
- **Contract tester** -- audits tool schemas and sends six edge-case payloads per tool
  (null, empty string, wrong type, 50k-character string, missing required, extra field).
  Rejecting invalid input counts as correct; crashing the transport and *silently
  accepting* input the schema forbids do not
- **Fault injection** -- a `FaultProxy` wraps any target and injects timeouts, 429s, 500s,
  malformed responses, and refused connections at a configurable rate. Probes cannot tell
  they are wrapped, so phase 2 measures the same things as phase 1 under duress
- **Trajectories** -- every attempt at an operation is recorded, yielding retries,
  recovery rate, recovery latency, retry amplification, and duplicate mutations
- **MCP adapter** -- stdio and SSE, tool discovery, arguments synthesized from each
  tool's JSON Schema
- **LLM adapter** -- Anthropic and OpenAI chat completions behind the same interface, so
  every probe works against them unchanged. SDK retries are disabled on purpose: a scan
  measuring reliability has to see the 429, not have the SDK absorb it
- **Mock targets** -- healthy, degraded, failing, saturating, and bloated profiles,
  deterministic per seed, so the whole test suite runs without API keys
- **Behavior analysis** -- reads phase 2's trajectories and reports what the target *did*:
  retry amplification, recovery rate and latency, duplicate mutations, stuck loops. Sends
  no traffic of its own, so it cannot perturb what it measures
- **Policy scoring** -- thresholds in YAML, a 0-100 score out. Probes measure; the policy
  judges, so the bar is per-project rather than baked into each probe
- **CI gate** -- `ratemyagent ci` exits 0 on pass, 1 on fail, and 2 when the scan could
  not run at all, because a broken scanner is not a failing target
- **Output** -- terminal scorecard and full JSON export (`--json-out run.scan.json`)

Findings call out thin evidence rather than letting it pass quietly. A recovery rate
measured from two disrupted operations is not evidence of resilience, and a concurrency
result from a ceiling of 5 is a floor set by the test, not a measurement of the target.

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --fault-rate 0.3
ratemyagent scan --target mock --phases baseline        # skip chaos and behavior
ratemyagent scan --target mock --probes cost,concurrency
```

## Scoring and CI

Thresholds live in a YAML policy. Meeting one scores 100 for that check; missing it decays
to 0 at twice the limit. A metric the scan could not produce is skipped, not scored zero.
The overall score is the mean of every check that ran.

```yaml
# my-policy.yaml
name: my-service
thresholds:
  p95_latency_ms: 3000
  error_rate_max: 0.02
  recovery_rate_min: 0.95
  retry_amplification_max: 1.5
  duplicate_mutation_max: 0
pass_score: 80
```

```bash
ratemyagent policy                                  # show the shipped defaults
ratemyagent ci --target mcp --uri stdio://./server.py --policy my-policy.yaml
echo $?                                             # 0 pass, 1 fail, 2 scan broke
```

`ratemyagent ci` prints each failed check with the number that failed it, so a red build
says which threshold moved rather than just that the score dropped.

Run `ratemyagent scan --help` for the full option list, or `ratemyagent probes` to see
which probes this build can run.

## Development

```bash
uv run pytest        # 418 tests, no network or API keys
uv run ruff check .
```

Python 3.10+. Architecture and the six-week plan live in [CLAUDE.md](CLAUDE.md).

## License

MIT
