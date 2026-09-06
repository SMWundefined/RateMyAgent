# Probes

Six probes across three phases. Each one measures; the [policy](POLICY.md) decides
whether the numbers are acceptable.

| Probe | Phase | Measures | Weight |
|---|---|---|---|
| [`latency`](#latency) | 1 baseline | p50/p95/p99, TTFT, call overhead | 20 |
| [`cost`](#cost) | 1 baseline | tokens, prompt bloat, $/request | 15 |
| [`concurrency`](#concurrency) | 1 baseline | saturation point, latency knee | 15 |
| [`contract`](#contract) | 1 baseline | schema audit, edge-case handling | 15 |
| [`fault`](#fault) | 2 chaos | injects faults, records trajectories | — |
| [`behavior`](#behavior) | 3 behavior | recovery, amplification, duplicates | 35 |

Run a subset with `--probes latency,cost`. Run one phase with `--phases baseline`.

---

## latency

**What it measures.** Sends N requests **one at a time** and profiles the distribution:
p50/p95/p99, TTFT where the target reports it, and tool call overhead.

Sequential on purpose. This probe answers "how slow is one call when nothing else is in
flight". Concurrent load is the concurrency probe's question, and mixing the two produces
a profile that describes neither.

**Key metrics.** `p50_s` `p95_s` `p99_s` `error_rate` `tail_ratio` `tool_call_overhead_s`
`ttft_p50_s` `ttft_p95_s` `errors_by_kind`

**Percentiles are nearest-rank, not interpolated.** At the sample sizes a scan collects,
an interpolated p99 invents a number no request actually saw.

**Tool call overhead** is only reported when the target reports its own execution time
(`Response.server_time_s`). Without that, everything measured is transport plus work and
indivisible — a number there would be a guess.

**Findings.** Heavy tail (p99 ≥ 3× p50), tool call overhead above 20% of reported
execution time, TTFT above half of total latency, error taxonomy breakdown, small-sample
warning below 20 requests.

**Edge cases.**
- All requests fail → no distribution; `p95_s` is `None` and the policy check skips.
- Failed requests are excluded from latency stats. A 30-second timeout must not be
  averaged in as though it were a real sample.
- A clean run reports what it measured — it does not claim "no problems", because the
  policy owns that verdict.
- Zero failures in N requests is reported as a *bound*, not a rate: the rule of three puts
  the 95% upper bound at 3/N, which at 20 requests is 15%, not 0%.

---

## cost

**What it measures.** Tokens per request, the fixed prefix resent on every call, and a
dollar projection.

**Key metrics.** `mean_input_tokens` `mean_output_tokens` `io_ratio`
`static_prefix_tokens` `bloat_share` `bloat_detected` `cost_per_request`
`cost_per_1k_requests` `cacheable_savings_per_request`

**Prompt bloat** is estimated from `min(input_tokens)` across the run: no request can
carry less than the shared prefix, so the smallest input seen is an upper bound on what
is constant. Flagged only when that prefix is **both** ≥50% of mean input **and** ≥1024
tokens — a 100%-fixed 40-token prompt is not a caching opportunity.

**Pricing is never guessed.** Anthropic list prices ship in `ANTHROPIC_PRICING`. For any
other model the probe reports token counts and says it has no price, unless you pass
`--price-in` / `--price-out`. A made-up number here would end up in somebody's budget.

**Findings.** Prompt bloat with projected cache savings, input:output ratio above 10:1,
per-request/per-1k/per-million cost, missing price.

**Edge cases.**
- Target reports no tokens (every MCP server) → **not applicable**, excluded from the
  score rather than counted as a failure.
- Tokens known but no price → also not applicable; the token findings still print.
- `--price-in`/`--price-out` override the table for models it does not know.

---

## concurrency

**What it measures.** Ramps 1 → 2 → 4 → 8 … up to `--concurrency`, sending a full batch at
each level, and finds where the target stops coping.

**Key metrics.** `saturation_point` `max_sustained_concurrency` `latency_knee_at`
`peak_throughput_rps` `levels` (per-level error rate, p95, goodput)

**Saturation point** is the first level whose error rate crosses **5%**.

**Latency knee** is the first level whose p95 exceeds **3× the level-1 p95**. A target can
saturate by getting slow rather than by failing, and the error-rate rule alone would call
that healthy.

**Goodput, not throughput.** Computed as `concurrency / mean_latency × success_rate` via
Little's law, not from wall clock. Wall-clock throughput against a target reporting
simulated latency measures the machine running the scan, not the target; and scaling by
success rate stops a level that fails fast from posting the best number.

**Findings.** Saturation point with the error rate that triggered it, latency knee with
the multiple, peak goodput and the level it occurred at, early-stop notice.

**Edge cases.**
- The ramp **stops early** past 50% errors — higher levels only measure how fast it can
  refuse.
- No saturation found → the result is a **floor set by the test**, not a measurement of
  the target. The finding says so and tells you to raise `--concurrency`.
- Fails at concurrency 1 → not a concurrency limit; something is broken at any load.

---

## contract

MCP-oriented. Two halves.

**Schema audit.** Reads `list_tools()` and flags declarations that are unusable: no
schema, non-object type, required fields missing from `properties`, untyped properties,
missing descriptions.

**Edge-case probing.** Six payloads per tool:

| Case | Payload | Schema-violating? |
|---|---|---|
| `null_required` | `{"query": None}` | yes |
| `empty_string` | `{"query": ""}` | no |
| `wrong_type` | `{"query": 12345}` | yes |
| `very_long_string` | 50,000 characters | no |
| `missing_required` | `{}` | yes |
| `extra_param` | valid + unknown field | no |

**The grading distinction that matters: rejecting bad input is correct behaviour.** A tool
answering "query must be a string" is doing its job. Two things are failures:

- **Crashing the transport** — a connection, timeout, or protocol error means the tool
  never answered; it fell over. An unhandled exception takes down the whole stdio pipe,
  not just the one call.
- **Silently accepting input its own schema forbids** — the quieter bug. Nothing errors,
  and the garbage reaches whatever the handler writes to.

**Key metrics.** `crashes` `crash_rate` `rejected` `accepted` `accepted_invalid`
`schema_issues` `outcome_by_case` `cases`

**Edge cases.**
- Target exposes no tools (every LLM target) → **not applicable**.
- Probes at most 3 tools by default (`contract_tool_limit` in `ProbeConfig.extra`).
- Sends deliberate garbage, so it runs **last** among the baseline probes — it must not
  colour the measurements before it.

---

## fault

Phase 2. **The only place faults are injected** — probes stay clean.

**What it does.** Wraps the target in a `FaultProxy` and makes two passes:

1. **Degradation** — re-runs the opted-in baseline probes (only latency) through the
   proxy. Any difference from phase 1 is caused by the faults.
2. **Recovery** — sends fresh requests and retries failures up to `max_retries` (2), so
   each logical operation produces a `Trajectory`.

Request labels for the recovery pass start past the degradation pass's, so a retry is
never confused with an unrelated call to the same tool.

**Fault kinds**, spread evenly across `--fault-rate` by default:

| Fault | Reaches the target? | Reported latency |
|---|---|---|
| `timeout` | no | `timeout_s` (30s) |
| `rate_limit` | no | fast, carries `status: 429` + `retry_after_s` |
| `server_error` | no | fast, carries `status: 500` |
| `connection_refused` | no | fast |
| `malformed` | **yes** | **the target's real latency** |

At most one fault per call, so rates are shares of a single draw and must sum to ≤ 1.0.

**Key metrics.** `injected` `injected_by_kind` `injection_rate`
`error_rate_under_fault` `baseline_probes_under_fault`

**This probe has no policy weight.** It is the injector, not a judged dimension — what it
produced is scored under `behavior`.

**Edge cases.**
- `--fault-rate 0` injects nothing and says the phase proved nothing.
- Verified accurate: 0.25 configured → 0.2485 observed over 20,000 calls.
- Retries draw independently, verified at p² for both-attempts-faulted.

---

## behavior

Phase 3. Reads the trajectories phase 2 recorded and answers the question the tool exists
for: **what did the target do when things went wrong?**

Not "did it fail" — a target that returns an error when you inject a 500 is behaving
correctly. The interesting questions are downstream of the failure.

**Key metrics.**

| Metric | Meaning |
|---|---|
| `retry_amplification` | attempts / operations. 1.0 ideal; >2.0 flagged |
| `recovery_rate` | over **disrupted** operations only |
| `mean_recovery_latency_s` | first failure → the success that resolved it |
| `duplicate_mutations` | repeated *successes* — the retried-payment failure mode |
| `loops_detected` | 3+ attempts that never resolved |
| `unrecovered_by_fault_kind` | which injected fault most often ended in permanent failure |

**"Disrupted" means the first attempt failed.** Recovery is only meaningful for those — an
operation that never broke did not recover from anything.

**It sends no traffic of its own.** Everything comes from invocations the proxy already
observed, so it costs nothing and cannot perturb what it is measuring.

**Findings.** Unrecovered operations with the fault that beat them, retry amplification
with peak attempts, slow recovery, duplicate mutations, stuck loops, and a thin-sample
warning below 10 disrupted operations — because `recovery_rate_min` is *scored*, so a
thin sample moves the overall score on almost no evidence.

**Edge cases.**
- No trajectories (phase 2 did not run) → **not applicable**, with a message saying to run
  the chaos phase.
- Nothing disrupted → `recovery_rate` is `None`, the check skips, and the finding says
  recovery behaviour is untested.
- Duplicates and loops are reported **even when nothing was disrupted** — a duplicated
  mutation matters whether or not anything failed first.
