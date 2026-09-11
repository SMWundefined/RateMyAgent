# ratemyagent

[![tests](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml/badge.svg)](https://github.com/SMWundefined/RateMyAgent/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![Python](https://img.shields.io/pypi/pyversions/ratemyagent.svg)](https://pypi.org/project/ratemyagent/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Test AI agents like production services.**

---

## A number without its denominator is not a measurement

Four things this project measured about its own output, in the order it found
them.

**Every MCP server we scanned reported zero schema violations accepted.** Nine
scans across seven distinct servers, twenty releases, `accepted_invalid: 0`
every time — a clean bill of health for the ecosystem. Then we counted what
those schemas actually declare. Not one of those nine tools declares
`additionalProperties: false`, and none declares a length bound. **Two of the
eight malformed inputs we send could not have been violations of anything.** The
zero was true in both directions: nobody accepted what their schema forbade, and
for a quarter of the checks no schema forbade anything.

**Then the same survey turned out to be the wrong shape.** Those nine rows are
all stdio servers. The hosted servers in the same corpus declare both
constraints heavily — one declares `additionalProperties: false` on **9 of its 9
tools** — and we had scanned them for weeks without noticing, because the survey
covered one class of target and the conclusion was written about all of them.
Re-measured, that server **enforces what it declares**: the check runs, and its
zero is a measured zero rather than a vacuous one. A sibling's check is still
discarded, and the report says exactly why — the only case that tests undeclared
keys carries a baseline the tool itself rejects.

**Then we found a server that did declare a strict schema, and scored it 49 out
of 100.** It rejected every malformed call correctly, at the protocol layer,
which is the only way a strict schema can be enforced. We recorded 49 of 60
rejections as *crashing the transport*, because our code read "the SDK raised an
exception" as "nothing came back". An otherwise identical server that validated
nothing scored 50. **We were measuring strictness as fragility**, and the
stricter the server, the worse it looked.

**And a fourth we still cannot measure, for a reason that is itself the finding.**
One hosted server declares length bounds on most of its fields — the case the
first finding says we cannot test. At 20 requests our own scan trips its rate
limiter and the connection dies before the scan starts. At 5 requests it
completes: 22 edge cases, 20 rejected cleanly, `accepted_invalid: 0`, `n=5` and
no more, because **hammering a rate limiter to get a better number is the thing
this tool warns you not to do**. That warning is not theoretical. Elsewhere we
sent 585 calls for 200 operations against a dependency already returning 429,
then reported the failure we had deepened.

Every number above was produced by this tool, and each was wrong in the
flattering direction. What each needed was not a better threshold but a stated
denominator: *of what was asked*, *of which servers*, *of what the call actually
did*, *of how many runs*.

That is what this tool is for, and it is why it disagrees with the others. A
scanner that reports a score is easy. A scanner that reports what it could not
measure — and refuses to score it — is the harder and more useful thing, and it
is the only kind whose green result means anything.

RateMyAgent runs your MCP server or agent under load, latency, faults and
dependency failures, and grades what it saw against a policy you control. When
the evidence will not support a number it says so instead of printing one: on a
clean scan of a real MCP server **three of nine policy checks come back `n/a`**
with the reason attached, and on the strict-schema server above, six of nine
did. There are 27 places in the code where a metric can withdraw itself.

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
Probes: 6/6 complete   Duration: 9.6ms
Faults: fault rate 30%, 2 retries -> recovery floor 91.0% (derived, not the policy value)

Phase 1  baseline
  Latency ................ p50 3.36s, p95 7.99s, p99 - over 40 requests (0.0% errors)
  Cost ................... 647 in / 120 out tokens per request, no price known for this model
  Concurrency ............ no saturation up to 16 concurrent, sustained 16
  Contract ............... 15 edge cases across 3 tools: 0 rejected cleanly, 15 accepted, 0 crashed

Phase 2  chaos (fault injection)
  Fault tolerance ........ 20 faults injected, 10/10 operations recovered (100%) within 2 retries, 1.30x call amplification

Phase 3  behavior analysis
  Behavior ............... 10/10 disrupted operations recovered (100%) within 2 retries, 1.30x amplification (ours), 0 duplicate mutations

                             actual     target     status
  p95 latency                7.99s      5.00s      FAIL
  schema violations accepted 9          0          FAIL
  error rate                 0.0%       5.0%       pass ~
  contract crash rate        0.0%       0.0%       pass
  recovery rate              100.0%     91.0%      pass ~
  duplicate mutations        0          0          pass
  p99 latency                -          10.00s     n/a ~
  cost per request           -          $0.1000    n/a ~
  retry amplification        -          2.00x      n/a ~

  ~ recovery rate -- 10/10 disrupted operations recovered, a
    95% interval of 72.2%-100.0%, which spans the 91.0% this
    fault rate produces against a target that never fails.
    recovery_rate_min is scored from it regardless. Remedy:
    --requests or --fault-rate.
  ~ error rate -- Zero failures in 40 requests bounds the
    error rate at roughly 8% with 95% confidence, not at 0%.
    Remedy: --requests.
  ~ 4 caveats on unscored rows (behavior, concurrency, cost,
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

ratemyagent v1.0.1 - pip install ratemyagent - github.com/SMWundefined/RateMyAgent
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
> Also note that probing a *mutating* tool mutates: scanning `write_file` writes files. By
> default both the profiled tool and the contract probe's tools are limited to ones known to
> be read-only; `--allow-mutating` lifts that for both.

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

**Both markdown outputs are written to be handed straight to a coding agent.** That is
what they are for, and it is the workflow this tool is built around:

```bash
ratemyagent scan --target mcp --uri stdio://./server.py --output all
# then, in Claude Code, Codex, Cursor or whatever you use:
#   "Read AGENTS.md and fix what it found."
```

`AGENTS.md` is the fix guide — findings with root causes and copy-pasteable patches.
`REPORT.md` is the evidence behind them — every metric, every probe, and how the scan was
run. Hand over the first to get work done, the second when you want the model to check the
reasoning rather than trust it. They are about 2,300 and 1,700 tokens respectively, so both
fit in any context window with room to spare, and `--output all` writes both plus the
terminal scorecard in one run.

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
- **Contract tester** — audits tool schemas and generates edge cases per *declared field*,
  required and optional. A tool declaring four required fields produces 35 cases; a tool
  that declares nothing produces none, because it forbids nothing to violate
- **Fault injection** — five fault kinds at a configurable rate, deterministic per seed
- **Behavior analysis** — recovery rate and latency, retry amplification, duplicate
  mutations, stuck loops
- **Adapters** — MCP over stdio, Streamable HTTP and SSE, with `--header` for auth;
  Anthropic and OpenAI chat completions; five mock profiles needing none of them
- **Outputs** — terminal scorecard, markdown report, AGENTS.md, JSON export

Every scan reproduces under `--seed`. The test suite needs no network and no key — run
`uv run pytest -q` for the count rather than trusting one written here, which is advice
this file earned by carrying a figure that drifted 200 behind.

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

**`--tool-args` reaches the contract probe as of 0.1.14, and did not before.** Edge cases
for the named tool are now built by mutating the arguments you supplied rather than
placeholders invented from the schema — so `wrong_type` asks whether a valid call is
rejected when one field is corrupted, instead of asking it of a call the server was going to
refuse anyway. The other tools in the contract window still use synthesized arguments, and
the report says which used which.

**The same rule now covers the contract probe, which it did not until 0.1.11.** Edge-case
probing sends a deliberately malformed payload per declared field, so against a write
tool it is six writes — and for seven releases it took the first three tools a server listed,
whatever they were. Contract probing is now limited to tools known to be read-only, and says
what it left out:

```
18 edge cases across 3 of 9 tools (6 skipped as mutating): 8 rejected cleanly, ...
```

Pass `--allow-mutating` to include them, against a target you can afford to have written to.

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

Latency and concurrency carry that straight into the score. **Quote a range from repeated
runs, with the count.** Ten runs of that same server later gave p50 `0.21s (0.20-0.69s,
n=10)` and a composite that did not move at all — so the honest form is a range and an `n`,
not an omission. A row dropped for having variance looks more consistent than the eight
beside it that were each measured once.

**The axis is threshold proximity, not transport.** A composite is stable while the
measurement sits orders of magnitude clear of its limit: p95 across the regression set
spans 0.001s to 0.67s against a 5s threshold, so jitter cannot reach the score. The
100-vs-85 swing above was p95 *crossing* 5s. Three of the six stdio rows reach the network
through the tool they probe, and the one `http://` row does not, so the transport column
does not predict this.

### The scan can cause the failure it reports

**This is a limit on what a scan can claim, not a bug with a workaround.**

RateMyAgent retries a disrupted operation twice, uniformly, across all five
fault kinds. Against a **rate-limited dependency** that is the wrong response,
and it compounds: a measured run against a free-tier API sent **585 calls for
200 operations — 2.92x amplification — while the dependency was already
returning 429**. The scan then reported a 95.5% error rate and 1.5% recovery.

Every one of those numbers is correct. None of them is a fact about the server:
they describe a server *being throttled by this scan*, and the throttling
deepened as the retries continued. The supported claim is **"a target under rate
limiting does not recover within two retries"** — which is true, and is about
retry budgets rather than about that server.

So, before pointing this at anything with a quota:

- **A rate limit will read as a reliability failure.** `error_rate`,
  `recovery_rate` and the concurrency saturation point will all degrade
  together, and the findings will describe a broken dependency.
- **Check the error kinds before believing the score.** `191 rate_limit` in the
  latency findings means the cap was the story. A genuine fault mix looks
  varied.
- **Scale `--requests` to what the quota tolerates**, or use a tier without one.
  A scan that is itself the load is measuring itself.
- **`retry_amplification` is reported and never scored** against a server
  target, because the retry loop is ours. When it is high, read it as a
  statement about what this tool did to your dependency.

**Since 1.0.1 the tool backs off.** A `rate_limit` retry now waits before
retrying — honouring a `Retry-After` hint when there is one, capped at
`--backoff-max` (5s) per retry and `--backoff-budget` (30s) per probe. The
trigger is the error kind, never the presence of a hint, because the case this
exists for relays an upstream 429 as a tool result with no header anywhere.

Measured on `tests/fixtures/rate_limited_mcp_server.py`, a server that refuses
until six seconds have passed and advertises nothing:

| | `--backoff-max 0` (1.0 behaviour) | default |
|---|---|---|
| calls for 12 operations | 48 | 26 |
| retry amplification | 3.00x | 1.17x |
| operations disrupted | 12 | 1 |
| recovered | 0 (0.0%) | 1 (100%) |
| wall clock | 0.27s | 10.21s |

The number that matters is `disrupted`: eleven of twelve operations stopped
failing because the scan stopped causing it. Backing off makes scans slower —
`backoff_waited_s` is reported so the time is attributable — and when the budget
runs out the scan continues without waiting rather than stopping, which is
recorded as a caveat rather than hidden.

**The rest of this section still holds.** The tool does not detect a quota, so a
rate limit still reads as a reliability failure and the advice above about
`--requests`, error kinds and `retry_amplification` is unchanged. Backoff bounds
the damage; it does not tell you the cap is there.

### A latency figure describes the path the call took, not the one its name implies

One scanned server answers a PyPI-backed query in **1.7ms**. It runs a seven-day cache. A
sibling server reaching the same dependency takes 130ms — 76x, same upstream, and the
difference is the most interesting thing either number has to say.

Before quoting a latency figure, ask whether it is physically possible for the distance
claimed. **If a server answers a network-backed query in single-digit milliseconds, the
question is what it is not doing.** The scan cannot detect this for you; it reports what
the call cost, and a cache hit is a real cost to a real caller.

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

  > **`--contract-tools` and `--allow-mutating` multiply rather than compose.** Contract
  > probing sends a deliberately malformed payload per declared field, and `--allow-mutating`
  > removes the read-only filter. Together, `--allow-mutating --contract-tools 12` against
  > `@modelcontextprotocol/server-memory` sends garbage to all nine of its tools including
  > six write tools, and against `server-filesystem` it reaches `write_file` with a valid
  > write and a thousand-character filename. Each flag is reasonable alone; a user setting
  > one is unlikely to be thinking about the other. Whatever ships has to make that
  > combination loud at the point of use, not in a footnote.
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

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Apache rather than MIT for two reasons that matter to anyone putting this in a CI
pipeline: it carries an express patent grant from contributors, and section 5 states
that contributions are offered under the same terms unless you say otherwise. MIT is
silent on both. Equally permissive; less for a legal review to work out.
