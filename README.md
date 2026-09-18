# ratemyagent

[![tests](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml/badge.svg)](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![Python](https://img.shields.io/pypi/pyversions/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Test MCP servers like production services.**

RateMyAgent is a reliability scanner for **MCP servers**. Most evaluation asks whether a
tool *can do the task*. This asks whether it **stays reliable when operated like a
production service** — under load, slow dependencies, rate limits, server errors,
malformed replies and dropped connections.

**Experimental:** an LLM adapter for Anthropic and OpenAI chat completions exists but has
never been run against a live API ([details](docs/SCANNING.md#the-llm-adapter-is-experimental)),
and an agent adapter has been validated against scripted agents only — see
[Agents (experimental)](#agents-experimental).

NOTE: Read-only tools, STAGING rather than production: there's no dry-run yet. Expanding capabilities soon.

Point it at a target and it:

1. **Measures a baseline** — latency distribution, token cost, where concurrency
   saturates, and whether the tools enforce their own JSON Schema.
2. **Injects faults** — timeouts, 429s, 500s, malformed replies and refused connections,
   through a proxy the target cannot see.
3. **Studies what happened** — did each disrupted operation recover, and how many calls
   did it cost.

Then it scores the result 0–100 against a YAML policy you control, gates CI with an exit
code, and writes an `AGENTS.md` fix guide you can hand straight to a coding agent.

It is built for the developer who wrote an MCP server — often with AI help — and
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
| **behavior** 35 | recovery, retry amplification, duplicate mutations, loop detection | the retry loop being measured is ours, not the target's; and duplicate mutations without `--verify-tool`, which is what reads the target's state — see [Known limitations](#known-limitations) |

The recovery threshold is **derived, not fixed**. The fault injector produces
`1 - fault_rate ** retries` against a target that never fails — 96% at the
default rate — so a fixed floor would grade the flag rather than the target.
Every report header states the rate and the floor it implies, because two scans
at different rates are not comparable.

**Targets:** MCP over stdio, Streamable HTTP and SSE, plus five built-in mock profiles
that need no server. The LLM adapter is experimental, as above. **Outputs:** terminal
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
Probes: 6/6 complete   Duration: 0.01s
Faults: fault rate 30%, 2 retries -> recovery floor 91.0% (derived, not the policy value)

Phase 1  baseline
  Latency ................ p50 3.36s, p95 7.99s, p99 - over 40 requests (0.0% errors)
  Cost ................... 647 in / 120 out tokens per request, no price known for this model
  Concurrency ............ no saturation up to 16 concurrent, sustained 16
  Contract ............... 15 edge cases across 3 tools: 0 rejected cleanly, 15 accepted, 0 crashed

Phase 2  chaos (fault injection)
  Fault tolerance ........ 20 faults injected, 10/10 operations recovered (100%) within 2 retries, 1.30x call amplification

Phase 3  behavior analysis
  Behavior ............... 10/10 disrupted operations recovered (100%) within 2 retries, 1.30x amplification (ours), 0 duplicate deliveries (ours)

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

ratemyagent v1.6.0 - pip install ratemyagent - github.com/SMWundefined/RateMyAgent
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

**Scan servers you run, or have permission to test.** A scan is load: it calls a tool once
per request, ramps concurrency, injects faults and retries what fails.

```bash
# Your own MCP server, over stdio
ratemyagent scan --target mcp --uri "stdio://./server.py" \
    --tool search --tool-args '{"query": "hello"}'

# A server you run, over Streamable HTTP
ratemyagent scan --target mcp --uri http://localhost:3001/mcp \
    --header 'Authorization: Bearer $TOKEN'
```

Every HTTP request carries `User-Agent: ratemyagent/<version> (+<repo url>)` so the traffic
is identifiable in an access log, unless you pass your own `--header 'User-Agent: ...'`.

Five things to know before scanning something real:

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
- **The concurrency ramp is more than half the traffic.** At the defaults it walks 1, 2, 4
  and 5 concurrent and sends `--requests` at each level: 80 of the 140 calls a `--requests
  20` scan made against a real server, counted from that server's own state. `--requests 20
  --probes latency,contract,fault,behavior` drops it, and costs nothing in the score — no
  policy threshold reads concurrency, so it is reported and never graded.

Transports: `https://host/mcp` (Streamable HTTP), `stdio://./server.py`, and
`sse+https://host/sse` (deprecated by the 2025-06-18 spec).

The detail behind each of these — the refusal messages, the read-only gate, credential
redaction, `--scan-timeout` — is in [docs/SCANNING.md](docs/SCANNING.md).

**Validated on two SQLite MCP servers.** `--verify-tool` was run against
`npx mcp-sqlite@1.0.9` (`create_record` / `read_records`) and
`npx mcp-server-sqlite-npx@0.8.0` (`write_query` / `read_query`), each writing an
`{op_id}` into a throwaway database. Both reported `duplicate_mutations` from the
database's own contents — one operation applied twice on each, two on one arm — and a
stdlib-only script that re-derives the ids and counts the rows agreed with every number.
This is what a plain insert does under at-least-once retry: the reply was dropped after
the row was written, the caller retried, and a second row appeared. Neither server is
doing anything wrong, and neither finding is a bug report against them — an insert with
no idempotency key behaves exactly this way, which is why it is the case worth being able
to measure.

## Agents (experimental)

New in 1.5.0, extended in 1.6.0, and not frozen. **Phase D is in progress**: 1.6.0 is what
came back from pointing the scan at a real agent for the first time. `--target agent` scans
an **agent** rather than a server:
the agent is launched per task with an MCP config pointing at `ratemyagent proxy`, which
sits in front of the real MCP server, injects faults from a forced schedule and records
every call. With `--verify-tool` the scan reads the server's state before and after each
task, one task at a time, and joins what the agent **claimed** with what the record and
the state show:

| metric | reads | scored |
|---|---|---|
| duplicate mutations | effects above the task's `expected_effects` | yes, absolute (cap 49) |
| retry amplification | calls under fault over the clean-path calls | yes |
| unsupported claims | agent said success, the record holds no successful reply | no |
| lost effects | the server replied success and applied nothing — the server's fault | no |
| lost acknowledgements | agent said failure, the work was applied | no, report only |
| backoff shape, retry-after honored | wall-clock gaps between attempts | no |

An agent scan gets a verdict only with `--verify-tool`, every task's state read, and at
least one task where a call went unanswered — otherwise nothing tested whether the agent
could apply a write twice. Short of that it prints `NO VERDICT` with the reason, and `ci`
exits 2. The server's state must persist outside its process (a file or a database): the
agent and the verify tool each start their own copy of a stdio server, and a scan whose
verify tool cannot see a clean task's write refuses before the faulted pass.

**Validated against scripted agents only**: three fixtures that never import this package
— `careful` (one idempotency key per operation, growing backoff, honours the hint),
`blind` (no key, no wait) and `optimistic` (blind, then claims success anyway) — against a
server twin whose own ledger records every call as applied or absorbed. Under one forced
schedule careful passes, blind is capped at 49 for a duplicate the ledger confirms, and
only optimistic makes unsupported claims. From a checkout:

```bash
ratemyagent scan --target agent \
    --agent "python tests/fixtures/agents/careful_agent.py" \
    --tasks tests/fixtures/agents/tasks-demo.json \
    --upstream "stdio://python tests/fixtures/event_twin_mcp_server.py --mode append --state /tmp/rma-demo.jsonl" \
    --verify-tool effects --verify-count entries \
    --allow-mutating --fault-rate 0.7
```

Swap `careful_agent.py` for `blind_agent.py` or `optimistic_agent.py` and nothing else.

### Validated on one real agent

**`claude-haiku-4-5` driven by Claude Code 2.1.275**, launched per task through the MCP
config it already reads, against the event twin. Two findings, and both are about what
at-least-once delivery does at the tool boundary rather than about this agent.

**It applied no client-side deadline to a dropped reply.** The call was made, the upstream
applied it, the proxy dropped the reply — and the agent waited **234 seconds** with no
retry, no return and no `notifications/cancelled`, until the scan's own task deadline
killed it. A separate probe held a reply for 90 seconds and then released it: the agent
waited the whole 90 and accepted the late answer, so this is the absence of a deadline
rather than a long one. The scan refuses to score that run, and the refusal is the point —
what is being measured is the agent's next decision, and there was none to observe. The two
MCP SDKs disagree on this by default (the TypeScript one bounds a request at 60s, the
Python one sets no timeout at all), so which behaviour a host gets is a property of the
host.

**Under `--lost-reply-close-after`, it retried without an idempotency key.** With the
session closed a few seconds after the reply was dropped, the same agent reconnected and
re-sent the write — and the retry carried no `idempotency_key`, though its first attempt
had invented one. The upstream had nothing to recognise the repeat by, so it applied the
write twice: `duplicate mutations 1`, score 49/100, against `expected_effects: 1`. The
twin's own ledger confirms two applications, and
[`examples/phase-d/verify_independent.py`](examples/phase-d/) re-derives the count from
that ledger without importing this package.

**This is what a write retried after an unknown outcome does.** The agent could not know
whether its call had landed, and trying again is the reasonable move; an upstream with no
way to recognise the repeat then applies it twice. Neither finding is a bug report against
Claude Code, exactly as the SQLite results above are not bug reports against those servers
— it is the case worth being able to measure, on the class of agent most people are
actually shipping.

**Four runs, one task, one model.** `claude-haiku-4-5` was chosen because the spike was
testing plumbing rather than reasoning. A stronger model may retry differently, keep its
key, or not retry at all, and nothing here is a rate: one agent measured is one agent
measured.

1.6.0 is what those findings demanded. `--lost-reply-close-after` ends the session a few
seconds after the reply is dropped, so a client with no deadline gets an event it cannot
ignore while still not learning whether its write applied — a different fault, counted
separately from `response_lost` everywhere. `--agent-command`, `--claim-path` and
`--work-dir` are the rest: the 1.5.1 fixed argv could not launch a hosted CLI at all,
because `--tasks` is not a flag Claude Code has.

What an agent must accept and print is in
[docs/SCANNING.md](docs/SCANNING.md#scanning-an-agent-experimental), and what this cannot
tell you is in
[docs/LIMITATIONS.md](docs/LIMITATIONS.md#agent-scans-are-experimental-and-narrow).

## How a scan works

Three phases, in order. Phase 2 needs phase 1 to compare against; phase 3 reads what phase
2 recorded.

**Phase 1 — Baseline.** Latency, cost, concurrency and contract probes measure the target
as it is. These are the numbers everything else is compared against.

**Phase 2 — Fault injection.** A `FaultProxy` wraps the target and injects faults at a
configurable rate. Probes cannot tell they are wrapped, so the same probes run against a
sabotaged target and any difference is attributable to the faults.

**Phase 3 — Behavior analysis.** Reads the trajectory of every operation phase 2
disrupted: did it recover, how long did that take, how many calls did one operation cost.
This is the part that is not a load test — it measures behaviour under failure, not failure
counts.

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
  mutation, so one failure cannot be averaged away. A duplicate-mutation failure needs
  `--verify-tool`; without it the check is skipped rather than passed.

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

**`ci` is the gate; `scan` is not.** `scan` prints `FAIL` and still exits 0 — it reports,
and a reporting command that exits non-zero breaks every pipeline that runs it for the
artifact. Only `ci` turns the verdict into an exit code, so a gate that greps `scan`'s
output for `FAIL` is not a gate. Both exit 2 when the scan could not run at all, which
since 1.4.1 includes a run refused because the target still holds the ids this seed would
write, and a run whose `--verify-tool` was requested but did not measure.

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
  `n/a`, and is scored only against `--target agent`, where the loop is the agent's.
- **Agent scans are experimental and narrow.** Tasks run one at a time, and the Retry-After
  hint reaches an agent in the tool error body, a convention a real client may not read. One
  real agent has been scanned; it has no read timeout, so a dropped reply hangs it until the
  task deadline unless `--lost-reply-close-after` ends the session for it.
- **Duplicate mutations need `--verify-tool`.** Left to itself the scan counts calls it
  re-sent after dropping a reply and reports that as its own; it cannot see whether the
  target applied them twice. Pass a read-only tool that reports the target's state, with
  `{op_id}` in `--tool-args`, and the metric is measured per operation
  ([how](docs/SCANNING.md#counting-what-a-mutating-tool-applied)). Without it the check is
  skipped, not passed — and since 1.4.1 a scan whose oracle was requested but did not
  measure never prints PASS, and `ci` exits 2.
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
`ProbeConfig.extra` keys with no CLI flag behind them. The whole agent path
— `AgentTarget`, `--target agent` and its flags, `ratemyagent proxy`, the record format
and the agent metrics — is experimental and outside the freeze, and 1.6.0 changed it.
`FaultKind.RESPONSE_LOST_THEN_CLOSED` is the exception: `FaultKind` members *are* frozen,
adding one is a minor release, and that is why this is 1.6.0. Each is a reporting
channel rather than a contract, and anything a consumer comes to depend on gets
promoted to a named field by a written procedure rather than by habit.

**[`docs/API-STABILITY.md`](docs/API-STABILITY.md) is the full statement**,
including the promotion rules and why each unfrozen thing is unfrozen. It ships
in the sdist. Wheels carry no docs directory, so from a wheel this section and
the file at the matching git tag are the reference.

## Roadmap

- **Next** — `ratemyagent chaos` for targeted single-fault scenarios; `--contract-tools` to
  raise contract coverage above the default three (with a hazard noted in
  [docs/SCANNING.md](docs/SCANNING.md#probing-writes-unless-it-knows-better)); a dry-run
  mode, which needs `--verify-tool` as its evidence that nothing was applied
- **Agents** — Phase C is done in 1.5.0: `AgentTarget`, the proxy, per-task effect
  counting, and a gate passed against scripted agents. **Phase D is in progress.** 1.6.0
  ships what the first real-agent spike demanded: a launch contract a hosted CLI can
  actually satisfy (`--agent-command`, `--claim-path`, `--work-dir`) and
  `RESPONSE_LOST_THEN_CLOSED`, without which an agent that sets no read timeout cannot be
  scored at all. Its gate is one finding on a real agent, reproduced by a script that does
  not import this package — and a hang counts, now that a hang is the first thing the tool
  found.
- **v2** — sustained outage windows; historical trending across scans

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
