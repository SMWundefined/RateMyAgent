# RateMyAgent report — mcp-git

- **Scanned:** 2026-09-09 04:37 UTC
- **Target:** `stdio://uvx --from mcp-server-git mcp-server-git --repository '/Users/wadoodsm/Silicon Valley/SRE/ratemyagent'` (mcp)
- **Policy:** `production-default` (pass score 75)
- **Duration:** 6.85s across 6 probes
- **Probe tool:** `git_log`
- **Probe arguments:** `{"repo_path": "/Users/wadoodsm/Silicon Valley/SRE/ratemyagent"}`

## Verdict

> FAIL: score 89 meets pass threshold 75, but 1 check failed: recovery rate.
> Biggest gaps: behavior (34/35).

## Actual vs target

| measurement | actual | target | status |
|---|---|---|---|
| recovery rate | 83.3% | 90.0% | **FAIL** ~ |
| p95 latency | 0.04s | 5.00s | pass |
| p99 latency | 0.04s | 10.00s | pass |
| error rate | 0.0% | 5.0% | pass ~ |
| contract crash rate | 0.0% | 0.0% | pass ~ |
| schema violations accepted | 0 | 0 | pass ~ |
| duplicate mutations | 0 | 0 | pass |
| cost per request | - | $0.1000 | n/a ~ |
| retry amplification | - | 2.00x | n/a ~ |

**What these numbers do not establish**

- **retry amplification** -- A server does not retry; this scanner does. The amplification measured describes RateMyAgent, not the target.
- **cost per request** -- the cost probe reported no cost per request, so this threshold could not be evaluated
- **Cost** -- This target reports no token usage. MCP servers do not; the probe is meaningful against LLM targets.
- **recovery rate** -- 6 disrupted operations bounds the failure-to-recover rate at roughly 50% rather than measuring it, and recovery_rate_min is scored from it. Remedy: `--requests or --fault-rate`.
- **Concurrency** -- No saturation up to 5 concurrent requests, which is the configured ceiling rather than the target's limit. The real limit is somewhere above 5. Remedy: `--concurrency`.
- **Contract** -- --tool-args named 'git_log', which this probe does not cover. Every case here used arguments synthesized from the schemas of git_status, git_diff_unstaged, git_diff_staged.
- **schema violations accepted** -- 6 of 18 rejections could not be attributed to a cause: this scanner's error-message table does not cover how this target words its errors. That measures our coverage, not the target's behaviour.
- **error rate** -- Zero failures in 20 requests bounds the error rate at roughly 15% with 95% confidence, not at 0%. Remedy: `--requests`.

## Score breakdown

| dimension | points | note |
|---|---|---|
| latency | 20/20 |  |
| cost | -/15 | not measured against this target |
| concurrency | -/15 | no policy threshold reads it |
| contract | 15/15 |  |
| behavior | 34/35 | recovery rate was 83.3%, policy allows at least 90.0% |
| **total** | **89/100** | capped at 89 from 98: check failed (recovery_rate_min) |

## Phase 1 — Baseline

How the target behaves under normal conditions.

### Latency

p50 0.04s, p95 0.04s, p99 0.04s over 20 requests (0.0% errors)

**Score:** 100/100

| metric | value |
|---|---|
| requests | 20 |
| p50 | 0.04s |
| p95 | 0.04s |
| p99 | 0.04s |
| error rate | 0.0% |
| p99/p50 | 1.2x |

**Limits of this measurement**

- Zero failures in 20 requests bounds the error rate at roughly 15% with 95% confidence, not at 0%. Remedy: `--requests`.

**Findings**

- p95 0.04s and 0.0% errors across 20 requests, with no heavy tail, no unusual call overhead, and no error pattern to report.

### Cost

_Not applicable to this target: target reported no token usage across 20 requests._

### Concurrency

no saturation up to 5 concurrent, sustained 5

**Score:** not scored

| metric | value |
|---|---|
| sustained | 5 |
| latency knee | 4 |
| peak goodput | 27.8/s |

| concurrency | error rate | p95 | goodput |
|---|---|---|---|
| 1 | 0.0% | 0.05s | 25.9/s |
| 2 | 0.0% | 0.09s | 25.6/s |
| 4 | 0.0% | 0.22s | 27.8/s |
| 5 | 0.0% | 0.31s | 27.2/s |

**Limits of this measurement**

- No saturation up to 5 concurrent requests, which is the configured ceiling rather than the target's limit. The real limit is somewhere above 5. Remedy: `--concurrency`.

**Findings**

- Latency knee at 4 concurrent: p95 rose to 0.22s from 0.05s at a single request (4.8x). A target can saturate by getting slow rather than by failing, and this one does.
- Peak goodput is 27.8 successful req/s at 4 concurrent. Past that, added concurrency buys latency and errors rather than completed work.

### Contract

18 edge cases across 3 of 12 tools (5 skipped as mutating, 4 past the 3-tool cap): 15 rejected (9 cleanly, 6 unclassified), 3 accepted, 0 crashed

**Score:** 100/100

| metric | value |
|---|---|
| tools exposed | 12 |
| tools probed | 3 |
| skipped as unsafe | 5 |
| past the cap | 4 |
| edge cases | 18 |
| required fields probed | ['git_diff_staged.repo_path', 'git_diff_unstaged.repo_path', 'git_status.repo_path'] |
| real arguments for | git_log |
| control calls | 18 |
| control calls lost | 0 |
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

**Limits of this measurement**

- --tool-args named 'git_log', which this probe does not cover. Every case here used arguments synthesized from the schemas of git_status, git_diff_unstaged, git_diff_staged.
- 3 of 12 tools were probed, 5 skipped as unsafe to probe. Every contract number here is about those tools, not about the server. Remedy: `--allow-mutating`.
- 6 of 18 rejections could not be attributed to a cause: this scanner's error-message table does not cover how this target words its errors. That measures our coverage, not the target's behaviour.

**Findings**

- Edge-case probing covered 3 of 12 tools: 5 skipped as mutating (git_add, git_checkout, git_commit, git_create_branch, git_reset). Probing calls a tool with deliberately bad input six times, so a write tool would be written to six times. Pass --allow-mutating to include them, against a target you can afford to have written to.
- Nothing crashed the transport and no schema violation was accepted. 9 of 15 rejections were attributed to a cause; the remaining 6 were not, so this is not a clean bill for every case.

## Phase 2 — Fault injection

The same probes against a target we are deliberately breaking.

### Fault injection

13 faults injected, 5/6 operations recovered (83%) within 2 retries, 1.35x call amplification

**Score:** not scored

| metric | value |
|---|---|
| retry budget | 2 |
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

**Limits of this measurement**

- 6 disrupted operations bounds the failure-to-recover rate at roughly 50% rather than measuring it. Remedy: `--fault-rate or --requests`.

**Findings**

- Injected 13 faults across 48 calls (27%): 5 rate_limit, 3 timeout, 3 connection_refused, 2 server_error.
- 1/6 disrupted operations never recovered (83% recovery rate) within 2 retries. These are the calls that would surface to a user as a hard failure.
- Under fault the latency probe saw a 25% error rate, p95 0.04s.

## Phase 3 — Behavior analysis

What the target did once things started failing.

### Behavior

5/6 disrupted operations recovered (83%) within 2 retries, 1.35x amplification (ours), 0 duplicate mutations

**Score:** 96/100

| metric | value |
|---|---|
| operations | 20 |
| attempts | 27 |
| disrupted | 6 |
| recovered | 5 |
| recovery rate | 83.3% |
| retry budget | 2 |
| mean recovery | 0.04s |
| duplicate mutations | 0 |
| stuck loops | 1 |

| final status | operations |
|---|---|
| success | 19 |
| failed | 1 |

**Limits of this measurement**

- 6 disrupted operations bounds the failure-to-recover rate at roughly 50% rather than measuring it, and recovery_rate_min is scored from it. Remedy: `--requests or --fault-rate`.
- A server does not retry; this scanner does. The amplification measured describes RateMyAgent, not the target.

**Findings**

- 1/6 disrupted operations never recovered (83% recovery rate), most often 2 after timeout, 1 after connection_refused. These are the calls a user would experience as a hard failure.
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
