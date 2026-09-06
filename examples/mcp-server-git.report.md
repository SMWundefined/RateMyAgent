# RateMyAgent report — mcp-git

- **Scanned:** 2026-09-06 23:01 UTC
- **Target:** `stdio://uvx --from mcp-server-git mcp-server-git --repository /Users/wadoodsm/Silicon\ Valley/SRE/ratemyagent` (mcp)
- **Policy:** `production-default` (pass score 75)
- **Duration:** 8.09s across 6 probes
- **Probe tool:** `git_log`
- **Probe arguments:** `{"repo_path": "/Users/wadoodsm/Silicon Valley/SRE/ratemyagent"}`

## Verdict

> FAIL: score 99 meets pass threshold 75, but 1 check failed: recovery rate.
> Biggest gaps: behavior (34/35).

## Actual vs target

| measurement | actual | target | status |
|---|---|---|---|
| recovery rate | 83.3% | 90.0% | **FAIL** |
| p95 latency | 0.04s | 5.00s | pass |
| p99 latency | 0.05s | 10.00s | pass |
| error rate | 0.0% | 5.0% | pass |
| sustained concurrency | 5 | 5 | pass |
| contract crash rate | 0.0% | 0.0% | pass |
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
| contract | 15/15 |  |
| behavior | 34/35 | recovery rate was 83.3%, policy allows at least 90.0% |
| **total** | **99/100** | |

## Phase 1 — Baseline

How the target behaves under normal conditions.

### Latency

p50 0.04s, p95 0.04s, p99 0.05s over 20 requests (0.0% errors)

**Score:** 100/100

| metric | value |
|---|---|
| requests | 20 |
| p50 | 0.04s |
| p95 | 0.04s |
| p99 | 0.05s |
| error rate | 0.0% |
| p99/p50 | 1.2x |

**Findings**

- p95 0.04s and 0.0% errors across 20 requests, with no heavy tail, no unusual call overhead, and no error pattern to report. Note that zero failures in 20 requests only bounds the error rate at roughly 15% (95% confidence), not 0%. Raise --requests to tighten it.

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
| peak goodput | 24.9/s |

| concurrency | error rate | p95 | goodput |
|---|---|---|---|
| 1 | 0.0% | 0.05s | 23.5/s |
| 2 | 0.0% | 0.11s | 22.9/s |
| 4 | 0.0% | 0.24s | 24.9/s |
| 5 | 0.0% | 0.36s | 23.7/s |

**Findings**

- No saturation found up to 5 concurrent requests, the configured ceiling. The real limit is above 5, so this is a floor set by the test, not a measurement of the target -- raise --concurrency to find the actual limit.
- Latency knee at 4 concurrent: p95 rose to 0.24s from 0.05s at a single request (4.8x). A target can saturate by getting slow rather than by failing, and this one does.
- Peak goodput is 24.9 successful req/s at 4 concurrent. Past that, added concurrency buys latency and errors rather than completed work.

### Contract

18 edge cases across 3 tools: 15 rejected (9 cleanly, 6 unclassified), 3 accepted, 0 crashed

**Score:** 100/100

| metric | value |
|---|---|
| tools probed | 3 |
| edge cases | 18 |
| rejected cleanly | 15 |
| accepted | 3 |
| accepted but invalid | 0 |
| crashed | 0 |

| edge case | worst outcome |
|---|---|
| null_required | rejected |
| empty_string | accepted |
| wrong_type | rejected |
| very_long_string | rejected |
| missing_required | rejected |
| extra_param | rejected |

**Findings**

- 6/18 rejections could not be attributed to a cause: this scanner's error-message table does not cover how this target words its errors. They are counted as rejections, not crashes. This measures our coverage, not the target's behaviour.
- Nothing crashed the transport and no schema violation was accepted. 9 of 15 rejections were attributed to a cause; the remaining 6 were not, so this is not a clean bill for every case.

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
