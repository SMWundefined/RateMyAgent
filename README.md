# ratemyagent

[![tests](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml/badge.svg)](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![Python](https://img.shields.io/pypi/pyversions/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Test AI agents like production services.**

RateMyAgent is a reliability scanner for MCP servers, AI agents and LLM endpoints. Most
agent evaluation asks whether an agent *can do the task*. This asks whether it **stays
reliable when operated like a production service** — under load, slow dependencies, rate
limits, server errors, malformed replies and dropped connections.

NOTE: Read-only tools, STAGING rather than production: there's no dry-run yet. Expanding capabilities soon.

Point it at a target and it:

1. **Measures a baseline** — latency distribution, token cost, where concurrency
   saturates, and whether the tools enforce their own JSON Schema.
2. **Injects faults** — timeouts, 429s, 500s, malformed replies and refused connections,
   through a proxy the target cannot see.
3. **Studies what happened** — did each disrupted operation recover, how many calls did
   it cost, did anything run twice.

Then it scores the result 0–100 against a YAML policy you control, gates CI with an exit
code, and writes an `AGENTS.md` fix guide you can hand straight to a coding agent.

It is built for the developer who wrote an MCP server or agent — often with AI help — and
wants to know whether it is ready before something depends on it. It tests behaviour, not
source: a schema that is declared but never enforced, missing backpressure, or a retry
loop that amplifies failures all show up in what the target does.

**When the evidence cannot support a number, it says so instead of printing one.** An
unmeasurable check comes back `n/a` with the reason attached and leaves the score, rather
than counting as a zero or a pass. [Why that matters](docs/LIMITATIONS.md#a-number-without-its-denominator-is-not-a-measurement).

## What it measures, and what it refuses to score

Five dimensions, weighted to 100. Each can decline.

| dimension | measures | declines when |
|---|---|---|
| **latency** 20 | p50/p95/p99, TTFT, call overhead | p99 below 100 requests — nearest-rank makes it the sample maximum |
| **cost** 15 | tokens, prompt bloat, $/request | the model's price is unknown. A guessed rate ends up in someone's budget |
| **concurrency** 15 | ramp to saturation, goodput, latency knee | always. Measured and reported, never scored — the old check compared `--concurrency` against itself |
| **contract** 15 | schema audit, one edge case per declared field | no case ran, or the crashes cannot be attributed to the input rather than to a dead session |
| **behavior** 35 | recovery, retry amplification, duplicate mutations, loop detection | the retry loop being measured is ours, not the target's — see [Known limitations](#known-limitations) |

The recovery threshold is **derived, not fixed**. The fault injector produces
`1 - fault_rate ** retries` against a target that never fails — 96% at the
default rate — so a fixed floor would grade the flag rather than the target.
Every report header states the rate and the floor it implies, because two scans
at different rates are not comparable.

**Targets:** MCP over stdio, Streamable HTTP and SSE; Anthropic and OpenAI chat
completions; five built-in mock profiles that need none of them. **Outputs:** terminal
scorecard, markdown report, AGENTS.md, JSON. Every scan reproduces under `--seed`.
Per-probe detail is in [docs/PROBES.md](docs/PROBES.md).

## Install

Published on [PyPI](https://pypi.org/project/ratemyagent/). Python 3.10+.

```bash
pip install ratemyagent
```

That gives you the engine, the mock targets, and every output format — enough to run a
full scan without installing anything else. The adapters that talk to real systems need
their SDKs, which are optional so you only pull what you use:

```bash
pip install 'ratemyagent[mcp]'          # scan MCP servers (mcp SDK 1.x or 2.x)
pip install 'ratemyagent[anthropic]'    # scan Anthropic chat completions
pip install 'ratemyagent[openai]'       # scan OpenAI chat completions
pip install 'ratemyagent[all]'          # all of the above
```

With uv: `uv tool install ratemyagent` for a standalone CLI, or
`uv pip install 'ratemyagent[all]'` into the current environment.

## 30 seconds, no API key

There is a built-in mock target, so you can see the whole thing work before pointing it at
anything real. No key, no server, no network.

```bash
ratemyagent scan --target mock --profile degraded --requests 40 \
    --concurrency 16 --fault-rate 0.3
```

<details>
<summary>Full output</summary>

```
RateMyAgent Scan Results
========================

Target: degraded-mock (mock)
Probes: 6/6 complete   Duration: 9.8ms
Faults: fault rate 30%, 2 retries -> recovery floor 91.0% (derived, not the policy value)

Phase 1  baseline
  Latency ................ p50 3.36s, p95 7.99s, p99 - over 40 requests (0.0% errors)
  Cost ................... 647 in / 120 out tokens per request, no price known for this model
  Concurrency ............ no saturation up to 16 concurrent, sustained 16
  Contract ............... 15 edge cases across 3 tools: 0 rejected cleanly, 15 accepted, 0 crashed

Phase 2  chaos (fault injection)
  Fault tolerance ........ 20 faults injected, 10/10 operations recovered (100%) within 2 retries, 1.30x call amplification

Phase 3  behavior analysis
  Behavior ............... 10/10 disrupted operations recovered (100%) within 2 retries, 1.30x amplification (ours), duplicate mutations not scored (nothing could have duplicated)

                             actual     target     status
  p95 latency                7.99s      5.00s      FAIL
  schema violations accepted 9          0          FAIL
  error rate                 0.0%       5.0%       pass ~
  contract crash rate        0.0%       0.0%       pass
  recovery rate              100.0%     91.0%      pass ~
  p99 latency                -          10.00s     n/a ~
  cost per request           -          $0.1000    n/a ~
  retry amplification        -          2.00x      n/a ~
  duplicate mutations        -          0          n/a ~

  ~ recovery rate -- 10/10 disrupted operations recovered, a
    95% interval of 72.2%-100.0%, which spans the 91.0% this
    fault rate produces against a target that never fails.
    recovery_rate_min is scored from it regardless. Remedy:
    --requests or --fault-rate.
  ~ error rate -- Zero failures in 40 requests bounds the
    error rate at roughly 8% with 95% confidence, not at 0%.
    Remedy: --requests.
  ~ 5 caveats on unscored rows (behavior, concurrency, cost,
    latency) -- -v to show.

  Score breakdown:
    latency         14/20     (p95 latency was 7,988ms, policy allows at most 5,000ms)
    cost            -/15      (not measured against this target)
    concurrency     -/15      (no policy threshold reads it)
    contract        8/15      (invalid inputs accepted was 9, policy allows at most 0)
    behavior        35/35

  Score: 81/100  (policy production-default)

Latency findings:
  - p95 7.99s and 0.0% errors across 40 requests, with no
    heavy tail, no unusual call overhead, and no error pattern
    to report.

Cost findings:
  - No cost problems found: 647 input tokens per request with
    no significant fixed prefix.

Concurrency findings:
  - Peak goodput is 4.4 successful req/s at 16 concurrent.
    Past that, added concurrency buys latency and errors
    rather than completed work.

Contract findings:
  - CRITICAL 9 inputs the schema forbids were accepted with a
    success response: missing_required[query],
    null_required[query], wrong_type[query]. Every accepted
    violation is on 'query'. The tool is not validating what
    it declares, so invalid data reaches whatever it writes
    to.

Fault tolerance findings:
  - Injected 20 faults across 93 calls (22%): 6 server_error,
    5 rate_limit, 4 connection_refused, 3 timeout, 2
    malformed.
  - Every one of the 10 disrupted operations recovered within
    2 retries.
  - Under fault the latency probe saw a 20% error rate, p95
    8.03s.

Behavior findings:
  - Every one of the 10 disrupted operations recovered within
    2 retries. That budget is the scanner's, not the target's,
    and is not configurable.

8 findings across 6 probes. Run with --output agents-md to generate a fix guide.

FAIL: score 81 meets pass threshold 75, but 2 checks failed: p95 latency, schema violations accepted.
Biggest gaps: contract (8/15), latency (14/20).

ratemyagent v1.3.0 - pip install ratemyagent - github.com/SMWundefined/RateMyAgent
```

</details>

How to read it:

- **Actual sits next to target** so the gap is the information.
- **`n/a`** means the probe could not measure this target. It is excluded from the score
  rather than counted as a failure.
- **`~`** marks a caveat about how strong the evidence is — here, that 10 disrupted
  operations cannot tell this target apart from the injector's own 91% floor.
- **The last two lines are the verdict**, because that is what a CI log gets searched
  for. A score above the pass threshold with a failed check is still a FAIL.

The target above is a built-in mock. Before reading a `behavior` row on a real MCP server
the same way, see [Known limitations](#known-limitations).

## Scan a real target

```bash
# An MCP server over stdio -- pass the tool and real arguments
ratemyagent scan --target mcp --uri "stdio://uvx mcp-server-git" \
    --tool git_log --tool-args '{"repo_path": "/path/to/repo"}'

# A hosted MCP server over Streamable HTTP
ratemyagent scan --target mcp --uri https://api.example.com/mcp \
    --header 'Authorization: Bearer $TOKEN'

# A chat completions endpoint (this one spends money — keep --requests low)
ratemyagent scan --target llm --provider anthropic --model claude-opus-5 --requests 5
```

Four things to know before scanning something real:

- **Pass `--tool` and `--tool-args`.** Probing calls a tool for real, once per request.
  Without real arguments, placeholders are synthesized from the schema, and if the server
  rejects them the scan refuses and exits 2 rather than scoring the rejection.
- **Probing a write tool writes.** Auto-selection only picks a tool it can establish is
  read-only; a tool that changes state needs `--allow-mutating`. Point that at something
  disposable.
- **Credentials:** `--header` for http/sse, `--env KEY=VALUE` for a stdio server — the MCP
  SDK does not pass your shell environment to the child process. Values are redacted in
  every report and export.
- **A rate-limited dependency reads as an unreliable one.** The scan becomes part of the
  load. Scale `--requests` to the quota.

Transports: `https://host/mcp` (Streamable HTTP), `stdio://./server.py`, and
`sse+https://host/sse` (deprecated by the 2025-06-18 spec).

The detail behind each of these — the refusal messages, the read-only gate, credential
redaction, `--scan-timeout` — is in [docs/SCANNING.md](docs/SCANNING.md).

## How a scan works

Three phases, in order. Phase 2 needs phase 1 to compare against; phase 3 reads what phase
2 recorded.

**Phase 1 — Baseline.** Latency, cost, concurrency and contract probes measure the target
as it is. These are the numbers everything else is compared against.

**Phase 2 — Fault injection.** A `FaultProxy` wraps the target and injects faults at a
configurable rate. Probes cannot tell they are wrapped, so the same probes run against a
sabotaged target and any difference is attributable to the faults.

**Phase 3 — Behavior analysis.** Reads the trajectory of every operation phase 2
disrupted: did it recover, how long did that take, how many calls did one operation cost,
did anything run twice. This is the part that is not a load test — it measures behaviour
under failure, not failure counts.

Against something that retries — an agent, or a client wrapping a service — the trajectory
is the target's. Against a bare server the retry loop belongs to the scanner, so what gets
scored there is target survivability.

<p align="center">
  <img src="docs/architecture.svg" alt="RateMyAgent architecture: the CLI drives a target adapter (MCP server, Anthropic, OpenAI or a mock), which runs through baseline, fault injection and behavior analysis phases into the policy engine" width="680">
</p>

The contributor-facing walkthrough is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Scoring

Results are scored 0–100 against a YAML policy. Probes measure; the policy decides.

- Meeting a threshold scores **100** for that check — a threshold is a limit, not a target.
- Missing it decays **linearly to 0 at twice the limit**, so a near miss and a catastrophe
  do not score alike.
- A metric the scan could not produce is **skipped, not zeroed**. Missing evidence is not
  a failure.
- **Passing requires both** a score at or above `pass_score` **and** no failed check. A
  failed check also caps the score at 89, or at 49 for a contract crash or a duplicate
  mutation, so one failure cannot be averaged away.

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

Every threshold is optional, and an unknown key is an error rather than silently unscored.
Full reference, including the shipped default explained threshold by threshold and how the
caps work: [docs/POLICY.md](docs/POLICY.md).

## CI integration

```bash
ratemyagent ci --target mcp --uri stdio://./server.py --policy production.yaml
echo $?     # 0 pass, 1 fail, 2 the scan could not run
```

Exit code 2 matters: a broken scanner is not a failing target, and a gate that cannot tell
them apart is not worth having in a pipeline. Failed checks are printed individually, and
`--scan-timeout` bounds the whole run so a hung handshake fails cleanly instead of burning
the job's time limit.

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

## AGENTS.md and the report

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --output all
# then, in Claude Code, Codex, Cursor or whatever you use:
#   "Read AGENTS.md and fix what it found."
```

**Both markdown outputs are written to be handed straight to a coding agent.**
`AGENTS.md` is the fix guide — findings with root causes and copy-pasteable patches.
`REPORT.md` is the evidence behind them — every metric, every probe, and how the scan was
run, organized by phase. Hand over the first to get work done, the second when you want
the model to check the reasoning rather than trust it. They are about 2,300 and 1,700
tokens, so both fit in any context window.

Each finding states what was observed, why it matters in production, the root cause —
weighted toward what AI-generated servers actually get wrong — and a fix naming the tool
that failed:

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

Re-scanning into the same file reports what changed since the last scan. Real output is in
[`examples/`](examples/): a scan of the official `mcp-server-git`
([AGENTS.md](examples/mcp-server-git.AGENTS.md), [report](examples/mcp-server-git.report.md))
and a [deliberately broken mock](examples/mock-failing.AGENTS.md) that triggers every
finding at once.

## Known limitations

Each of these affects scores you can produce today. The full account, with the
measurements behind each one, is in [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — read it
before relying on a number.

- **Retry behaviour is not scored against a bare MCP server.** A server does not retry;
  the scanner does, so retry amplification describes RateMyAgent. It is reported, marked
  `n/a`, and becomes scoreable with an `AgentTarget`.
- **Synthesized arguments are shallow.** A scan refuses when the server rejects them, but
  a tool that *accepts* a placeholder is profiled on a trivial call.
- **Network-backed targets vary run to run.** Quote a range from repeated runs, with the
  count.
- **The scan can cause the failure it reports.** Against a rate limiter, retries add load.
  Since 1.0.1 the scan backs off and honours `Retry-After`, but it does not detect a quota.
- **A latency figure describes the path the call took.** A cache hit is a real cost, not a
  round trip to the dependency the tool's name implies.
- **Faults are transient.** Each attempt is faulted independently; sustained outages are
  not modelled.

## API stability

**1.0 means the frozen surface will not break without a major version.** It does
not mean the findings are finished — the section above says plainly what this
tool still cannot measure.

Frozen: `scan()`, the target adapters, `ProbeConfig`, `Policy`, the result
shapes (`ScanResult`, `ProbeResult`, `CheckResult`, `Caveat`), `ErrorKind` and
`FaultKind` members, the CLI flags, the exit codes, and the eleven metric names
a policy threshold reads.

Not frozen: the rest of `ProbeResult.metrics`, `Response.meta`, and
`ProbeConfig.extra` keys with no CLI flag behind them. Each is a reporting
channel rather than a contract, and anything a consumer comes to depend on gets
promoted to a named field by a written procedure rather than by habit.

**[`docs/API-STABILITY.md`](docs/API-STABILITY.md) is the full statement**,
including the promotion rules and why each unfrozen thing is unfrozen. It ships
in the sdist. Wheels carry no docs directory, so from a wheel this section and
the file at the matching git tag are the reference.

## Roadmap

- **v1.1** — `ratemyagent chaos` for targeted single-fault scenarios; streaming TTFT for
  LLM targets; `--contract-tools` to raise contract coverage above the default three
  (with a hazard noted in [docs/SCANNING.md](docs/SCANNING.md#probing-writes-unless-it-knows-better))
- **v2** — sustained outage windows; `AgentTarget` wrapping a Python script; historical
  trending across scans

Deliberately out of scope: web dashboards, continuous monitoring, framework-specific
adapters, security scanning, and anything requiring a database.

## Contributing

```bash
git clone https://github.com/SMWundefined/RateMyAgent.git
cd RateMyAgent

uv venv --python 3.12
uv pip install -e '.[dev]'              # editable, with pytest and ruff

uv run pytest -q                        # prints the count; no network or API keys
uv run ruff check .
```

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — it covers the `Target`
interface, the `FaultProxy`, the trajectory model, and the policy engine, including the
parts that are load-bearing and the reasoning behind them.

House rules, in short:

- Every probe needs tests that run without API keys, a network, or an MCP server. Use the
  mock targets in `tests/conftest.py`.
- Probes measure, the policy judges. A probe that emits a verdict is a bug.
- The `FaultProxy` is the only place faults are injected.
- No interactive prompts. This is an SRE tool; it has to stay pipeable.
- Say what you measured, not what you would like to be true. Findings call out thin
  evidence rather than letting it pass quietly.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Apache rather than MIT because it carries an express patent grant from contributors, and
section 5 states that contributions are offered under the same terms unless you say
otherwise. MIT is silent on both. Equally permissive; less for a legal review to work out.
