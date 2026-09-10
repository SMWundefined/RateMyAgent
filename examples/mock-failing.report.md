# RateMyAgent report — failing-mock

- **Scanned:** 2026-09-10 03:49 UTC
- **Target:** `mock://failing-mock` (mock)
- **Policy:** `production-default` (pass score 75)
- **Duration:** 0.01s across 6 probes
- **Fault conditions:** fault rate 30%, 2 retries -> recovery floor 91.0% (derived, not the policy value)

## Verdict

> FAIL: score 30 below pass threshold 75.
> Biggest gaps: latency (0/20), contract (0/15).

## Actual vs target

| measurement | actual | target | status |
|---|---|---|---|
| p95 latency | 44.56s | 5.00s | **FAIL** |
| error rate | 32.5% | 5.0% | **FAIL** |
| contract crash rate | 13.3% | 0.0% | **FAIL** |
| schema violations accepted | 6 | 0 | **FAIL** |
| recovery rate | 19.0% | 91.0% | **FAIL** |
| duplicate mutations | 0 | 0 | pass |
| p99 latency | - | 10.00s | n/a ~ |
| cost per request | - | $0.1000 | n/a ~ |
| retry amplification | - | 2.00x | n/a ~ |

**What these numbers do not establish**

- **retry amplification** -- A server does not retry; this scanner does. The amplification measured describes RateMyAgent, not the target.
- **cost per request** -- No published price for model unknown, so tokens are reported without a dollar projection. An invented rate would end up in someone's budget. Remedy: `--price-in and --price-out`.
- **p99 latency** -- p99 is the maximum of 27 samples, which estimates the 96% percentile rather than the 99th. Nearest-rank p99 is the maximum for any sample below 100, so it is reported and not scored. Remedy: `--requests 100`.
- **Concurrency** -- The ramp stopped at 8 concurrent with the target failing more than half of all requests; higher levels would only have measured how fast it can refuse.

## Score breakdown

| dimension | points | note |
|---|---|---|
| latency | 0/20 | p95 latency was 44,558ms, policy allows at most 5,000ms |
| cost | -/15 | not measured against this target |
| concurrency | -/15 | no policy threshold reads it |
| contract | 0/15 | contract crash rate was 13.3%, policy allows at most 0.0% |
| behavior | 21/35 | recovery rate was 19.0%, policy allows at least 91.0% |
| **total** | **30/100** |  |

## Phase 1 — Baseline

How the target behaves under normal conditions.

### Latency

p50 12.54s, p95 44.56s, p99 - over 40 requests (32.5% errors)

**Score:** 0/100

| metric | value |
|---|---|
| requests | 40 |
| p50 | 12.54s |
| p95 | 44.56s |
| p99 | 46.44s |
| error rate | 32.5% |
| p99/p50 | 3.7x |
| call overhead | 1.00s |

| error kind | count |
|---|---|
| server_error | 5 |
| timeout | 4 |
| rate_limit | 4 |

**Limits of this measurement**

- p99 is the maximum of 27 samples, which estimates the 96% percentile rather than the 99th. Nearest-rank p99 is the maximum for any sample below 100, so it is reported and not scored. Remedy: `--requests 100`.

**Findings**

- p95 latency is 44.56s. Anything with a 30s client timeout in front of this target will read one call in twenty as a hard failure.
- Heavy tail: p99 (46.44s) is 3.7x p50 (12.54s). Investigate retries, cold starts, or lock contention before optimizing the median.
- 13/40 requests failed (32.5%): 5 server_error, 4 timeout, 4 rate_limit.

### Cost

_Not applicable to this target: 652 in / 120 out tokens per request, no price known for this model._

- No cost problems found: 652 input tokens per request with no significant fixed prefix.

### Concurrency

saturates at 1 concurrent, sustained 0

**Score:** not scored

| metric | value |
|---|---|
| sustained | 0 |
| saturates at | 1 |
| peak goodput | 0.2/s |

| concurrency | error rate | p95 | goodput |
|---|---|---|---|
| 1 | 35.0% | 44.56s | 0.0/s |
| 2 | 35.0% | 42.75s | 0.1/s |
| 4 | 40.0% | 32.79s | 0.2/s |
| 8 | 50.0% | 42.55s | 0.2/s |

**Limits of this measurement**

- The ramp stopped at 8 concurrent with the target failing more than half of all requests; higher levels would only have measured how fast it can refuse.

**Findings**

- The target exceeded the error threshold at a single concurrent request. This is not a concurrency limit; something is broken at any load.

### Contract

15 edge cases across 3 tools: 6 rejected cleanly, 7 accepted, 2 crashed

**Score:** 0/100

| metric | value |
|---|---|
| tools exposed | 3 |
| tools probed | 3 |
| skipped as unsafe | 0 |
| past the cap | 0 |
| edge cases | 15 |
| required fields probed | ['echo.query', 'search.query', 'summarize.query'] |
| control calls | 15 |
| control calls lost | 0 |
| rejected cleanly | 6 |
| accepted | 7 |
| accepted but invalid | 6 |
| crashed | 2 |

| edge case | worst outcome |
|---|---|
| null_required | accepted |
| wrong_type | accepted |
| empty_string | **crashed** |
| very_long_string | **crashed** |
| missing_required | accepted |

**Findings**

- 2/15 edge cases brought the tool down rather than returning an error: empty_string (timeout), very_long_string (timeout). Malformed input from a model is normal traffic, not an attack.
- 6 inputs the schema forbids were accepted with a success response: missing_required[query], null_required[query], wrong_type[query]. Every accepted violation is on 'query'. The tool is not validating what it declares, so invalid data reaches whatever it writes to.

## Phase 2 — Fault injection

The same probes against a target we are deliberately breaking.

### Fault injection

28 faults injected, 4/21 operations recovered (19%) within 2 retries, 1.95x call amplification

**Score:** not scored

| metric | value |
|---|---|
| retry budget | 2 |
| calls | 119 |
| faults injected | 28 |
| injection rate | 23.5% |
| error rate under fault | 63.0% |

| fault kind | injected |
|---|---|
| connection_refused | 9 |
| rate_limit | 7 |
| malformed | 4 |
| server_error | 4 |
| timeout | 4 |

**Findings**

- Injected 28 faults across 119 calls (24%): 9 connection_refused, 7 rate_limit, 4 malformed, 4 server_error, 4 timeout.
- 17/21 disrupted operations never recovered (19% recovery rate) within 2 retries. These are the calls that would surface to a user as a hard failure.
- Mean recovery takes 19.22s from first failure to success. That is user-visible even when the retry eventually works.
- Under fault the latency probe saw a 48% error rate, p95 44.56s.

## Phase 3 — Behavior analysis

What the target did once things started failing.

### Behavior

4/21 disrupted operations recovered (19%) within 2 retries, 1.95x amplification (ours), 0 duplicate mutations

**Score:** 60/100

| metric | value |
|---|---|
| operations | 40 |
| attempts | 78 |
| disrupted | 21 |
| recovered | 4 |
| recovery rate | 19.0% |
| retry budget | 2 |
| mean recovery | 19.22s |
| duplicate mutations | 0 |
| stuck loops | 17 |

| final status | operations |
|---|---|
| success | 23 |
| failed | 17 |

**Limits of this measurement**

- A server does not retry; this scanner does. The amplification measured describes RateMyAgent, not the target.

**Findings**

- 17/21 disrupted operations never recovered (19% recovery rate), most often 4 after connection_refused, 4 after timeout, 3 after malformed. These are the calls a user would experience as a hard failure.
- Recovery takes 19.22s on average and up to 36.75s. The retry works, but the caller waits through the whole thing.
- 17 operations made three or more attempts without ever succeeding. Retrying past the point where it can help spends the dependency's capacity on calls that were never going to land.

## How this scan was run

| setting | value |
|---|---|
| requests | 40 |
| concurrency | 16 |
| warmup | 1 |
| timeout_s | 30.0 |
| seed | 42 |
| phases | baseline, chaos, behavior |
| fault rate | 30% |

Reproduce with the same `--seed` to get the same run: fault injection is seeded per operation and attempt, so a scan replays exactly.
