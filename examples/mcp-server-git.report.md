# RateMyAgent report — mcp-git

- **Scanned:** 2026-09-05 00:40 UTC
- **Target:** `stdio://uvx mcp-server-git --repository /tmp/demo-repo` (mcp)
- **Policy:** `production-default` (pass score 75)
- **Duration:** 9.38s across 6 probes

## Verdict

> PASS: score 90 meets pass threshold 75.
> Biggest gaps: contract (8/15), behavior (34/35).

## Actual vs target

| measurement | actual | target | status |
|---|---|---|---|
| contract crash rate | 50.0% | 0.0% | **FAIL** |
| recovery rate | 83.3% | 90.0% | **FAIL** |
| p95 latency | 0.06s | 5.00s | pass |
| p99 latency | 0.09s | 10.00s | pass |
| error rate | 0.0% | 5.0% | pass |
| sustained concurrency | 5 | 5 | pass |
| schema violations accepted | 0 | 0 | pass |
| retry amplification | 1.35x | 2.00x | pass |
| duplicate mutations | 0 | 0 | pass |
| cost per request | - | $0.1000 | n/a |

## Score breakdown

| dimension | points | note |
|---|---|---|
| latency | 20/20 |  |
| cost | -/15 | not measured against this target |
| concurrency | 15/15 |  |
| contract | 8/15 | contract crash rate was 50.0%, policy allows at most 0.0% |
| behavior | 34/35 | recovery rate was 83.3%, policy allows at least 90.0% |
| **total** | **90/100** | |

## Phase 1 — Baseline

How the target behaves under normal conditions.

### Latency

p50 0.05s, p95 0.06s, p99 0.09s over 20 requests (0.0% errors)

**Score:** 100/100

| metric | value |
|---|---|
| requests | 20 |
| p50 | 0.05s |
| p95 | 0.06s |
| p99 | 0.09s |
| error rate | 0.0% |
| p99/p50 | 2.0x |

**Findings**

- p95 0.06s and 0.0% errors across 20 requests, with no heavy tail, no unusual call overhead, and no error pattern to report. Note that zero failures in 20 requests only bounds the error rate at roughly 15% (95% confidence), not 0%. Raise --requests to tighten it.

### Cost

_Not applicable to this target: target reported no token usage across 20 requests._

- The target reported no token usage, so cost cannot be measured. MCP servers do not report tokens; this probe is meaningful for LLM targets.

### Concurrency

no saturation up to 5 concurrent, sustained 5

**Score:** 100/100

| metric | value |
|---|---|
| sustained | 5 |
| latency knee | 4 |
| peak goodput | 23.5/s |

| concurrency | error rate | p95 | goodput |
|---|---|---|---|
| 1 | 0.0% | 0.05s | 22.4/s |
| 2 | 0.0% | 0.11s | 21.7/s |
| 4 | 0.0% | 0.26s | 23.2/s |
| 5 | 0.0% | 0.34s | 23.5/s |

**Findings**

- No saturation found up to 5 concurrent requests, the configured ceiling. The real limit is above 5, so this is a floor set by the test, not a measurement of the target -- raise --concurrency to find the actual limit.
- Latency knee at 4 concurrent: p95 rose to 0.26s from 0.05s at a single request (5.3x). A target can saturate by getting slow rather than by failing, and this one does.
- Peak goodput is 23.5 successful req/s at 5 concurrent. Past that, added concurrency buys latency and errors rather than completed work.

### Contract

18 edge cases across 3 tools: 9 rejected cleanly, 0 accepted, 9 crashed

**Score:** 50/100

| metric | value |
|---|---|
| tools probed | 3 |
| edge cases | 18 |
| rejected cleanly | 9 |
| accepted | 0 |
| accepted but invalid | 0 |
| crashed | 9 |

| edge case | worst outcome |
|---|---|
| null_required | rejected |
| empty_string | **crashed** |
| wrong_type | rejected |
| very_long_string | **crashed** |
| missing_required | rejected |
| extra_param | **crashed** |

**Findings**

- 9/18 edge cases brought the tool down rather than returning an error: empty_string (unknown), extra_param (unknown), very_long_string (unknown). Malformed input from a model is normal traffic, not an attack.

## Phase 2 — Fault injection

The same probes against a target we are deliberately breaking.

### Fault injection

13 faults injected, 5/6 operations recovered (83%), 1.35x call amplification

**Score:** not scored

| metric | value |
|---|---|
| calls | 48 |
| faults injected | 13 |
| injection rate | 27.1% |
| error rate under fault | 27.1% |

| fault kind | injected |
|---|---|
| rate_limit | 5 |
| timeout | 3 |
| connection_refused | 3 |
| server_error | 2 |

**Findings**

- Injected 13 faults across 48 calls (27%): 5 rate_limit, 3 timeout, 3 connection_refused, 2 server_error.
- 1/6 disrupted operations never recovered (83% recovery rate) within 2 retries. These are the calls that would surface to a user as a hard failure.
- Only 6 operations were disrupted, which bounds the failure-to-recover rate at roughly 50% rather than measuring it. Raise --fault-rate or --requests before trusting the recovery number.
- Under fault the latency probe saw a 25% error rate, p95 0.05s.

## Phase 3 — Behavior analysis

What the target did once things started failing.

### Behavior

5/6 disrupted operations recovered (83%), 1.35x call amplification, 0 duplicate mutations

**Score:** 98/100

| metric | value |
|---|---|
| operations | 20 |
| attempts | 27 |
| amplification | 1.35x |
| disrupted | 6 |
| recovered | 5 |
| recovery rate | 83.3% |
| mean recovery | 0.04s |
| duplicate mutations | 0 |
| stuck loops | 1 |

| final status | operations |
|---|---|
| success | 19 |
| failed | 1 |

**Findings**

- 1/6 disrupted operations never recovered (83% recovery rate), most often 2 after timeout, 1 after connection_refused. These are the calls a user would experience as a hard failure.
- Only 6 operations were disrupted, which bounds the failure-to-recover rate at roughly 50% rather than measuring it. The recovery_rate_min policy check is scored from this number, so raise --requests or --fault-rate before trusting it in CI.
- 1 operations made three or more attempts without ever succeeding. Retrying past the point where it can help spends the dependency's capacity on calls that were never going to land.

## How this scan was run

| setting | value |
|---|---|
| requests | 20 |
| concurrency | 5 |
| warmup | 1 |
| timeout_s | 30.0 |
| seed | 42 |
| phases | baseline, chaos, behavior |
| fault rate | 30% |

Reproduce with the same `--seed` to get the same run: fault injection is seeded per operation and attempt, so a scan replays exactly.
