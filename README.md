# ratemyagent

**Test AI agents like production services.**

Existing agent evaluation asks whether an agent can accomplish a task. RateMyAgent asks
whether it stays reliable when operated like a production service — under load, latency,
faults, and dependency failures.

Think k6 + Chaos Monkey + pytest, but for agents and MCP tools.

---

## The problem

Everyone is shipping MCP servers and agent tools. Almost nobody is testing them the way
they test the rest of their infrastructure.

The tools that exist answer different questions. Langfuse and LangSmith *observe*
production. DeepEval and RAGAS check *output quality*. MCP-Scan checks whether a tool is
*malicious*. k6 load-tests HTTP endpoints without understanding what an agent does with
the failures.

None of them answer the operational one:

> **What happens when your agent's tools and dependencies fail?**

That question has a specific shape for agents that it does not have for a web service. An
agent retries on its own. It fans out three tool calls in a turn and inherits the p95 of
each. It sends malformed arguments as *normal traffic*, because a model that has just been
told a tool exists guesses at its schema. And when a call times out after the work already
completed, the retry runs the mutation twice.

RateMyAgent breaks your target on purpose and measures what it does next.

## Install

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'          # add '.[mcp]', '.[anthropic]', '.[openai]', or '.[all]'
```

Python 3.10+.

## 30 seconds, no API key

There is a built-in mock target, so you can see the whole thing work before pointing it at
anything real. No key, no server, no network.

```bash
uv run ratemyagent scan --target mock --profile degraded --requests 40 \
    --concurrency 16 --fault-rate 0.3
```

```
RateMyAgent Scan Results
========================

Target: degraded-mock (mock)
Probes: 6/6 complete   Duration: 0.01s

Phase 1  baseline
  Latency ................ p50 3.36s, p95 7.99s, p99 8.48s over 40 requests (0.0% errors)
  Cost ................... 647 in / 120 out tokens per request, no price known for this model
  Concurrency ............ no saturation up to 16 concurrent, sustained 16
  Contract ............... 18 edge cases across 3 tools: 0 rejected cleanly, 18 accepted, 0 crashed

Phase 2  chaos (fault injection)
  Fault tolerance ........ 20 faults injected, 10/10 operations recovered (100%), 1.30x call amplification

Phase 3  behavior analysis
  Behavior ............... 10/10 disrupted operations recovered (100%), 1.30x call amplification, 0 duplicate mutations

                             actual     target     status
  p95 latency                7.99s      5.00s      FAIL
  schema violations accepted 9          0          FAIL
  p99 latency                8.48s      10.00s     pass
  error rate                 0.0%       5.0%       pass
  sustained concurrency      16         5          pass
  recovery rate              100.0%     90.0%      pass
  retry amplification        1.30x      2.00x      pass
  duplicate mutations        0          0          pass
  cost per request           -          $0.1000    n/a

  Score breakdown:
    latency         16/20     (p95 latency was 7,988ms, policy allows at most 5,000ms)
    cost            -/15      (not measured against this target)
    concurrency     15/15
    contract        8/15      (invalid inputs accepted was 9, policy allows at most 0)
    behavior        35/35

  Score: 86/100  (policy production-default)

9 findings across 6 probes. Run with --output agents-md to generate a fix guide.

PASS: score 86 meets pass threshold 75.
Biggest gaps: contract (8/15), latency (16/20).
```

Actual sits next to target so the gap is the information. `n/a` means the probe could not
measure this target — those are excluded from the score rather than counted as failures.

Then point it at something real:

```bash
# An MCP server over stdio or SSE
ratemyagent scan --target mcp --uri stdio://./server.py
ratemyagent scan --target mcp --uri sse://localhost:8080/sse --requests 100

# A chat completions endpoint (this one spends money — keep --requests low)
ratemyagent scan --target llm --provider anthropic --model claude-opus-5 --requests 5
ratemyagent scan --target llm --provider openai --model gpt-4o-mini --requests 5
```

Probing invokes a discovered tool for real, once per request. Pass `--tool` and
`--tool-args` to choose which one; the default is the first tool the server reports.

## How a scan works

Three phases, in order. Phase 2 needs phase 1 to compare against; phase 3 reads what phase
2 recorded.

**Phase 1 — Baseline.** Measures the target as it is: latency distribution, token cost and
prompt bloat, the concurrency level where it saturates, and whether its tools honour their
own JSON Schema. These are the numbers everything else is compared against.

**Phase 2 — Fault injection.** A `FaultProxy` wraps the target and injects timeouts, 429s,
500s, malformed responses and refused connections at a configurable rate. Probes cannot
tell they are wrapped, so the same probes run against a sabotaged target and any
difference is attributable to the faults.

**Phase 3 — Behavior analysis.** Reads the trajectory of every operation phase 2 disrupted
and reports what the target *did*: did it recover, how long did that take, how many calls
did one operation cost, did anything succeed twice. This is the part that is not a load
test — it measures behaviour under failure, not failure counts.

Per-probe detail is in [docs/PROBES.md](docs/PROBES.md).

## Scoring

Results are scored 0–100 against a YAML policy. Probes measure; the policy decides.

- Meeting a threshold scores **100** for that check — a threshold is a limit, not a target.
- Missing it decays **linearly to 0 at twice the limit**, so a near miss and a catastrophe
  do not score alike.
- A metric the scan could not produce is **skipped, not zeroed**. Missing evidence is not
  a failure.

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
ratemyagent policy                                    # show the shipped defaults
ratemyagent scan --target mock --policy my-policy.yaml
```

Every threshold is optional, and validation is strict — an unknown key is an error listing
the valid ones, because a typo that silently stopped scoring something is worse than a
crash. Full reference, including the shipped default explained threshold by threshold:
[docs/POLICY.md](docs/POLICY.md).

## CI integration

```bash
ratemyagent ci --target mcp --uri stdio://./server.py --policy production.yaml
echo $?     # 0 pass, 1 fail, 2 the scan could not run
```

Exit code 2 matters: a broken scanner is not a failing target, and a gate that cannot tell
them apart is not worth having in a pipeline. Failed checks are printed individually, so a
red build says which threshold moved rather than that the score dropped.

`ci` writes nothing and never prompts. Nothing in the tool does — it stays pipeable.

```yaml
# .github/workflows/reliability.yml
name: reliability

on: [push, pull_request]

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"

      - run: uv pip install --system '.[mcp]'

      - name: Reliability gate
        run: |
          ratemyagent ci \
            --target mcp --uri stdio://./server.py \
            --policy production.yaml \
            --requests 120 --concurrency 16 --fault-rate 0.25 \
            --json-out scan.json

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: reliability-scan
          path: scan.json
```

Use enough requests that the numbers mean something. `recovery_rate` from the default 20
requests is measured over roughly 4 disrupted operations, which is an anecdote rather than
a rate.

## AGENTS.md

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --output agents-md
# AGENTS.md written to AGENTS.md (7 recommendations, 3 critical)
```

A fix guide for *your* target. Each finding states what was observed, why it matters in
production, the root cause — weighted toward what AI-generated servers actually get wrong
— and a copy-pasteable fix naming the tool that failed:

> **FINDING: 9 schema-forbidden inputs accepted**
>
> Your tool declares required fields and types in its JSON Schema but does not enforce
> them at runtime. This is common in AI-generated MCP servers where the schema is correct
> but the handler trusts its input. Every field marked "required" needs an explicit check
> before the handler touches the data, because the calling agent WILL send malformed
> arguments — that is normal traffic, not an attack.
>
> Suggested fix for tool "search_database":
>
> ```python
> if "query" not in args or not isinstance(args["query"], str):
>     return {"error": "query is required and must be a string"}
> ```

Sections are ordered by severity — duplicate mutations and crashes before latency and cost
— so the first thing you read is the thing most worth fixing.

Re-scanning the same file reports movement:

```
## Since the last scan

- Score improved from 33 to 91/100.
- P95 latency improved from 44.22s to 0.44s.
- Schema violations regressed from 4 to 9.
```

**See the real thing without installing:** [`examples/AGENTS.md`](examples/AGENTS.md) and
[`examples/report.md`](examples/report.md), both generated from a scan of the deliberately
broken mock profile.

## Markdown report

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --output report
ratemyagent scan --target mcp --uri stdio://./server.py --output all
```

The whole scan organized by phase, with the actual-vs-target table, the score breakdown,
per-level concurrency numbers, every finding, and the settings needed to reproduce the
run. Example: [`examples/report.md`](examples/report.md).

## What it can do today

- **Latency profiler** — p50/p95/p99, TTFT, tool call overhead, heavy-tail detection
- **Cost analyzer** — tokens per request, prompt-bloat detection and what caching it would
  save, $/request. Prices are never guessed
- **Concurrency tester** — ramps 1→N, finds the saturation point and the latency knee
- **Contract tester** — audits tool schemas and sends six edge-case payloads per tool
- **Fault injection** — five fault kinds at a configurable rate, deterministic per seed
- **Behavior analysis** — recovery rate and latency, retry amplification, duplicate
  mutations, stuck loops
- **Adapters** — MCP over stdio and SSE; Anthropic and OpenAI chat completions; five mock
  profiles for testing without any of them
- **Outputs** — terminal scorecard, markdown report, AGENTS.md, JSON export

Every scan reproduces under `--seed`. 500 tests, none of which need a network or a key.

## Roadmap

- **v1.1** — `ratemyagent chaos` for targeted single-fault scenarios; streaming TTFT for
  LLM targets
- **v2** — sustained outage windows (current faults are independent per attempt, which
  models transient failure well and outages not at all); timeout-after-completion faults
  to exercise duplicate mutations properly; `AgentTarget` wrapping a Python script;
  historical trending across scans

Deliberately out of scope: web dashboards, continuous monitoring, framework-specific
adapters, security scanning, and anything requiring a database.

## Contributing

```bash
uv run pytest          # 500 tests, ~1s, no network or API keys
uv run ruff check .
```

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — it is written for contributors
and covers the `Target` interface, the `FaultProxy`, the trajectory model, and the policy
engine, including the parts that are load-bearing and the reasoning behind them.

House rules, in short:

- Every probe needs tests that run without API keys, a network, or an MCP server. Use the
  mock targets in `tests/conftest.py`.
- Probes measure, the policy judges. A probe that emits a verdict is a bug.
- The `FaultProxy` is the only place faults are injected.
- No interactive prompts. This is an SRE tool; it has to stay pipeable.
- Say what you measured, not what you would like to be true. Findings call out thin
  evidence rather than letting it pass quietly.

## License

MIT
