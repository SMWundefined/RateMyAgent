# ratemyagent

[![tests](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml/badge.svg)](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![Python](https://img.shields.io/pypi/pyversions/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Test AI agents like production services.**

Agent evaluation usually asks whether an agent can accomplish a task. RateMyAgent asks
whether it stays reliable when operated like a production service — under load, latency,
faults, and dependency failures.

Think k6 + Chaos Monkey + pytest, but for agents and MCP tools.

---

## The problem

Agent reliability is an active area of work: there are task-success benchmarks,
adversarial suites, and a growing literature on fault injection for ML and agent systems.
The gap this tool addresses is narrower and more practical.

> Existing agent evaluation and observability tools generally do not provide an
> SRE-oriented workflow for systematically injecting operational failures and measuring
> recovery behaviour.

The widely used tools answer adjacent questions. Langfuse and LangSmith *observe*
production. DeepEval and RAGAS check *output quality*. MCP-Scan checks whether a tool is
*malicious*. k6 load-tests HTTP endpoints without modelling what an agent does with the
failures.

The operational question sits between them:

> **What happens when your agent's tools and dependencies fail?**

That question has a specific shape for agents that it does not have for a web service. An
agent retries on its own. It fans out three tool calls in a turn and inherits the p95 of
each. It sends malformed arguments as *normal traffic*, because a model that has just been
told a tool exists guesses at its schema. And when a call times out after the work already
completed, the retry runs the mutation twice.

RateMyAgent breaks your target on purpose and measures what it does next.

## Install

Published on [PyPI](https://pypi.org/project/ratemyagent/). Python 3.10+.

```bash
pip install ratemyagent
```

That gives you the engine, the mock targets, and every output format — enough to run a
full scan without installing anything else. The adapters that talk to real systems need
their SDKs, which are optional so you only pull what you use:

```bash
pip install 'ratemyagent[mcp]'          # scan MCP servers over stdio or SSE (mcp SDK 1.x or 2.x)
pip install 'ratemyagent[anthropic]'    # scan Anthropic chat completions
pip install 'ratemyagent[openai]'       # scan OpenAI chat completions
pip install 'ratemyagent[all]'          # all of the above
```

Prefer uv:

```bash
uv tool install ratemyagent             # as a standalone CLI
uv pip install 'ratemyagent[all]'       # into the current environment
```

### From source

For contributors, or to run against an unreleased change:

```bash
git clone https://github.com/SMWundefined/RateMyAgent.git
cd RateMyAgent

uv venv --python 3.12
uv pip install -e '.[dev]'              # editable, with pytest and ruff

uv run pytest                           # 715 tests, ~1s, no network or API keys
```

See [Contributing](#contributing) before opening a PR.

## 30 seconds, no API key

There is a built-in mock target, so you can see the whole thing work before pointing it at
anything real. No key, no server, no network.

```bash
ratemyagent scan --target mock --profile degraded --requests 40 \
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
  Behavior ............... 10/10 disrupted operations recovered (100%), 1.30x amplification (ours), 0 duplicate mutations

                             actual     target     status
  p95 latency                7.99s      5.00s      FAIL
  schema violations accepted 9          0          FAIL
  p99 latency                8.48s      10.00s     pass
  error rate                 0.0%       5.0%       pass
  contract crash rate        0.0%       0.0%       pass
  recovery rate              100.0%     90.0%      pass
  duplicate mutations        0          0          pass
  cost per request           -          $0.1000    n/a
  retry amplification        -          2.00x      n/a

  Score breakdown:
    latency         16/20     (p95 latency was 7,988ms, policy allows at most 5,000ms)
    cost            -/15      (not measured against this target)
    concurrency     -/15      (no policy threshold reads it)
    contract        8/15      (invalid inputs accepted was 9, policy allows at most 0)
    behavior        35/35

  Score: 84/100  (policy production-default)

Latency findings:
  - p95 7.99s and 0.0% errors across 40 requests, with no
    heavy tail, no unusual call overhead, and no error pattern
    to report. Note that zero failures in 40 requests only
    bounds the error rate at roughly 8% (95% confidence), not
    0%. Raise --requests to tighten it.

Cost findings:
  - No published price for model unknown, so token counts are
    reported without a dollar projection. Pass --price-in and
    --price-out to project cost yourself rather than have one
    guessed.

Concurrency findings:
  - No saturation found up to 16 concurrent requests, the
    configured ceiling. The real limit is above 16, so this is
    a floor set by the test, not a measurement of the target
    -- raise --concurrency to find the actual limit.
  - Peak goodput is 4.4 successful req/s at 16 concurrent.
    Past that, added concurrency buys latency and errors
    rather than completed work.

Contract findings:
  - CRITICAL 9 inputs the schema forbids were accepted with a
    success response: missing_required, null_required,
    wrong_type. The tool is not validating what it declares,
    so invalid data reaches whatever it writes to.

Fault tolerance findings:
  - Injected 20 faults across 93 calls (22%): 6 server_error,
    5 rate_limit, 4 connection_refused, 3 timeout, 2
    malformed.
  - Every one of the 10 disrupted operations recovered within
    2 retries.
  - Under fault the latency probe saw a 20% error rate, p95
    8.03s.

Behavior findings:
  - Every one of the 10 disrupted operations recovered.

9 findings across 6 probes. Run with --output agents-md to generate a fix guide.

FAIL: score 84 meets pass threshold 75, but 2 checks failed: p95 latency, schema violations accepted.
Biggest gaps: contract (8/15), latency (16/20).

ratemyagent v0.1.10 - pip install ratemyagent - github.com/SMWundefined/RateMyAgent
```

Actual sits next to target so the gap is the information. `n/a` means the probe could not
measure this target — those are excluded from the score rather than counted as failures.

The target above is a built-in mock. Before reading a `behavior` row on a real MCP server
the same way, see [Known limitations](#known-limitations): recovery rate and retry
amplification measure the scanner's retry loop, because a server does not retry.

Then point it at something real:

```bash
# A hosted MCP server over Streamable HTTP
ratemyagent scan --target mcp --uri https://api.example.com/mcp \
    --header 'Authorization: Bearer $TOKEN'

# An MCP server over stdio
ratemyagent scan --target mcp --uri stdio://./server.py
ratemyagent scan --target mcp --uri sse://localhost:8080/sse --requests 100

# A chat completions endpoint (this one spends money — keep --requests low)
ratemyagent scan --target llm --provider anthropic --model claude-opus-5 --requests 5
ratemyagent scan --target llm --provider openai --model gpt-4o-mini --requests 5
```

Probing invokes a discovered tool for real, once per request. Pass `--tool` and
`--tool-args` to choose which one; the default is the first tool the server reports.

> **Pass real arguments.** Without `--tool-args`, arguments are synthesized from the tool's
> JSON Schema — correct shape and types, but placeholder values (`"ratemyagent probe"` for
> an unconstrained string). A tool that expects a real path, URL or package name will
> reject all of them, and the scan will accurately measure its *rejection path* rather than
> its behaviour. `mcp-server-git` scores **43/100 on synthesized arguments and 100/100 on
> real ones** -- same server, same repository, same command but for the arguments. The 43 is
> mostly a stated denominator: every request failed, so only the contract dimension measured
> anything at all. The
> scanner warns when it detects this, but the fastest way to avoid it is:
>
> ```bash
> ratemyagent scan --target mcp --uri "stdio://uvx mcp-server-git" \
>   --tool git_log --tool-args '{"repo_path": "/path/to/repo"}'
> ```
>
> Also note that probing a *mutating* tool mutates: scanning `write_file` writes files.

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

**Phase 3 — Behavior analysis.** Reads the trajectory of every operation phase 2
disrupted: did it recover, how long did that take, how many calls did one operation cost,
did anything succeed twice. This is the part that is not a load test — it measures
behaviour under failure, not failure counts.

Whose behaviour depends on the target. Against something that retries — an agent, or a
client wrapping a service — the trajectory is the target's. Against a bare server it is
not: the retry loop belongs to the scanner, so recovery latency and call amplification
describe RateMyAgent rather than the server. What survives that distinction is target
survivability. See [Known limitations](#known-limitations).

<p align="center">
  <img src="docs/architecture.svg" alt="RateMyAgent architecture: the CLI drives a target adapter (MCP server, Anthropic, OpenAI or a mock), which runs through baseline, fault injection and behavior analysis phases into the policy engine" width="680">
</p>

Per-probe detail is in [docs/PROBES.md](docs/PROBES.md), and the contributor-facing
walkthrough of how these pieces fit together is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

**Passing requires both a score above `pass_score` and no failed check.** The composite is
a weighted mean, so a single failure can be averaged down to almost nothing: a recovery
rate of 85.7% against a 90% floor scores 95.2, dilutes across the other behaviour checks,
and costs well under a point. That produced verdicts like `PASS: score 99` printed directly
above a table with `FAIL` in it. The score still summarises; it no longer overrules the
evidence beneath it.

```
FAIL: score 99 meets pass threshold 75, but 1 check failed: recovery rate.
```

**A failed check also caps the score.** The composite is a weighted mean, so one failure
can be averaged down to almost nothing — a recovery rate of 85.7% against a 90% floor used
to cost 0.6 points out of 100. Two ceilings, both set in the policy file:

```yaml
fail_cap: 89           # any check failed
absolute_fail_cap: 49  # duplicate_mutation_max or contract_crash_rate_max
```

The minimum cost of any failure is now "cannot score in the 90s". The caps are ceilings and
never floors: a scan already below them is untouched. When one applies, the score says so,
because the breakdown column sums to the pre-cap figure and a reader adding it up is owed
an explanation of the difference:

```
Score: 89/100  (capped at 89 from 99: check failed (recovery_rate_min); policy production-default)
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

**A scan is bounded by wall clock, not just per request.** `--timeout` bounds one request;
it cannot bound a handshake that never completes or a connection that will not close, both
of which sit outside every request. Those stalls hang a scan indefinitely, which in a
pipeline means a job that runs until the runner kills it and tells you nothing.

`--scan-timeout` bounds the whole run and defaults to a generous budget derived from
`--timeout` and `--requests`. On expiry the scan fails cleanly, names the phase and probe it
was in, and **exits 2** — a scan that never finished is not a target that failed:

```
error: scan exceeded its 2400s budget during phase baseline, probe latency and was
abandoned. The per-request --timeout does not bound a handshake or a teardown ...
```

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

Re-scanning the same file reports movement. Real output, from re-running the generator
over the `failing` mock's guide with the `degraded` mock:

```
## Since the last scan

- The previous guide was for `failing-mock`, not `degraded-mock` -- the comparisons below are between two different targets.
- Score improved from 32 to 91/100.
- P95 latency improved from 46.44s to 5.21s.
- Error rate improved from 36.7% to 0.0%.
- Sustained concurrency improved from 0 to 5.
- Schema violations regressed from 4 to 9.
- Edge-case crashes improved from 2 to 0.
- Recovery rate improved from 27% to 100%.
- Retry amplification improved from 1.63x to 1.13x.
```

The first line is the point: comparing two different targets is usually a mistake, so the
generator says so rather than presenting the deltas as a like-for-like improvement.

**See the real thing without installing:** [`examples/`](examples/) has output from a scan
of the official [`mcp-server-git`](examples/mcp-server-git.AGENTS.md), alongside a
[deliberately broken mock](examples/mock-failing.AGENTS.md) that triggers every finding at
once.

**Check the crash detection yourself:**
[`examples/mcp_server_git_repro.py`](examples/mcp_server_git_repro.py) sends the same
malformed payloads the contract probe sends, using only the MCP SDK, and makes a
known-good call after each one to prove the session is still alive. It needs
`pip install mcp` and nothing from this project, so you can confirm a reported crash is
real without taking our word for it.

```bash
python examples/mcp_server_git_repro.py                    # a throwaway git repo
python examples/mcp_server_git_repro.py --repository .     # your own
```

## Markdown report

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --output report
ratemyagent scan --target mcp --uri stdio://./server.py --output all
```

The whole scan organized by phase, with the actual-vs-target table, the score breakdown,
per-level concurrency numbers, every finding, and the settings needed to reproduce the
run. Example: [`examples/mcp-server-git.report.md`](examples/mcp-server-git.report.md).

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

Every scan reproduces under `--seed`. 715 tests, none of which need a network or a key.

## Probing writes, unless it knows better

Probing calls a tool for real, once per request, and again under fault injection. Against
a read-only tool that is a measurement. Against a write tool it is a hundred writes.

So **auto-selection only picks a tool it can establish is read-only.** It reads
`readOnlyHint` from the server's own tool annotations, falls back to the tool name, and
refuses when neither settles it — a tool nothing classifies is not thereby safe.

```
refusing to auto-select 'create_entities': it declares readOnlyHint=false.

Probing calls the chosen tool once per request, and again under fault
injection. No tool on this server is known to be read-only, so there is
nothing safe to fall back to.

  tools here: create_entities, create_relations, delete_entities, ...

Choose one yourself, and point the scan at something disposable:
  ratemyagent scan ... --tool <name> --allow-mutating
```

Naming a tool yourself is a decision the scanner will respect, but a tool known to change
state still needs `--allow-mutating` as a second key:

```bash
ratemyagent scan --target mcp --uri ... --tool write_file --allow-mutating
```

Point that at something disposable. Every scan reports which tool it called and with what
arguments, in the scorecard header and in the AGENTS.md state block, so a saved result can
always be traced back to what produced it.

**It also refuses when the arguments would be empty.** Synthesized arguments fill a
schema's required fields, and for an array or a string that can mean `[]` — which satisfies
`required` while asking the server to do nothing. A scan built on that call times an empty
round trip and reports low latency, no errors and full marks, none of it about the tool.
`server-memory` scored 100/100 that way on 20 successful no-ops. Pass `--tool-args` and the
question does not arise. A tool that requires nothing at all is unaffected: `{}` is a
complete payload there, not a missing one.

## Transports

| URI | Transport |
|---|---|
| `https://host/mcp` | Streamable HTTP |
| `stdio://./server.py` | stdio subprocess |
| `sse+https://host/sse` | SSE, deprecated by the 2025-06-18 spec |

Bare `http://` and `https://` mean **Streamable HTTP** as of 0.1.7. They used to mean SSE,
which the spec deprecated and replaced — so the only network transport pointed at the dead
one, and every hosted server failed to connect. SSE still works if you ask for it by name.

Credentials go in headers, repeatable:

```bash
ratemyagent scan --target mcp --uri https://api.example.com/mcp \
    --header 'Authorization: Bearer $TOKEN' \
    --tool search --tool-args '{"query": "hello"}'
```

**Header values never reach an artifact.** Reports, JSON exports and the AGENTS.md state
block record header *names* with the values replaced, and strip credentials out of the URI
itself, so a saved scan says whether it was authenticated without saying how. There is no
allowlist of "safe" headers — that judgement only has to be wrong once.

`--header` is http/sse only and `env` is stdio only; passing either to the wrong transport
raises rather than being ignored.

## Known limitations

Two things this version measures less well than the numbers suggest. Both affect scores
you can produce today, so they are stated here rather than in a changelog.

### Caller-strategy metrics are not scored against a bare MCP server

Retry amplification, backoff shape and recovery latency describe **the scanner's own retry
loop**, not the target's. A server does not retry — the client does.

**As of 0.1.9 they are no longer scored against one.** The behaviour dimension is split in
two:

- **Target survivability** — did the session keep answering under fault, and did its state
  survive a retry. Real against a server, and it carries the dimension's full 35 points.
- **Caller strategy** — retry amplification, and later backoff shape and recovery latency.
  Marked inapplicable when the target does not run its own retry loop, which is every
  target type today.

Withheld metrics show as `n/a` rather than disappearing, and amplification is still
reported for context, labelled as the scanner's:

```
  Behavior ....... 6/7 disrupted operations recovered (86%), 1.45x amplification (ours)
  retry amplification        -          2.00x      n/a
```

Scores did not move meaningfully — the point was that 15 of 35 points had no subject, not
that they were producing wrong numbers. Caller strategy becomes scoreable when a target
runs its own retry loop, which is what `AgentTarget` is for.

### Scores under synthesized arguments are not comparable to scores under `--tool-args`

Without `--tool-args`, arguments are synthesized from each tool's JSON Schema: correct
shape and types, placeholder values. A tool that wants a real URL, path or package name
rejects all of them, and the scan then measures its rejection path rather than its work.

The gap is not marginal. From this project's own re-scan of `mcp-server-fetch`:

| Arguments | Score | What was measured |
|---|---|---|
| synthesized | 43/100 | contract only; every request failed |
| `--tool-args '{"url": "https://example.com"}'` | 100/100 | latency, contract, behaviour |

Same server, same command, same seed. The difference is entirely in what we sent it.

The synthesized row is 15 points of 35, not 43 of 100: latency, cost, concurrency and
behaviour all drop out of the denominator, because a run in which nothing succeeded cannot
support a latency profile or a statement about recovery. That is the honest shape of the
number, and it is why it should not be read as "43% as reliable".

**`--tool` and `--tool-args` are the supported path for any number you intend to rely on.**
A synthesized-argument score is useful for a first look and for comparing a target against
itself; it is not a measurement of the server, and it must not be compared against a score
produced with real arguments. The scanner warns when it detects that every synthesized call
is being rejected, but the warning is a hint, not a guarantee.

### Scores against network-dependent targets are not stable across runs

A scan of a server that calls out to the internet measures upstream conditions as much as
the server. `mcp-web-engine` produced p95 **0.72s and 8.01s in the same session**, which
moved its composite from 100 to 85 with no change to the tool and no change to the server.

Latency and concurrency carry that straight into the score. Two runs minutes apart are not
comparable; two runs back to back usually are. **Quote a range from repeated runs, or do
not quote a number at all.** This applies to every `http(s)://` target and every hosted
server — which is most of them.

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

Set up with the [source install](#from-source) above, then:

```bash
uv run pytest          # 715 tests, ~1s, no network or API keys
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
