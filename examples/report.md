# RateMyAgent report — failing-mock

- **Scanned:** 2026-09-04 20:56 UTC
- **Target:** `mock://failing-mock` (mock)
- **Policy:** `production-default` (pass score 75)
- **Duration:** 0.01s across 6 probes

## Verdict

> FAIL: score 30 below pass threshold 75.
> Biggest gaps: latency (0/20), concurrency (0/15).

## Actual vs target

| measurement | actual | target | status |
|---|---|---|---|
| p95 latency | 44.56s | 5.00s | **FAIL** |
| p99 latency | 46.44s | 10.00s | **FAIL** |
| error rate | 32.5% | 5.0% | **FAIL** |
| sustained concurrency | 0 | 5 | **FAIL** |
| contract crash rate | 11.1% | 0.0% | **FAIL** |
| schema violations accepted | 4 | 0 | **FAIL** |
| recovery rate | 19.0% | 90.0% | **FAIL** |
| retry amplification | 1.95x | 2.00x | pass |
| duplicate mutations | 0 | 0 | pass |
| cost per request | - | $0.1000 | n/a |

## Score breakdown

| dimension | points | note |
|---|---|---|
| latency | 0/20 | p95 latency was 44,558ms, policy allows at most 5,000ms |
| cost | -/15 | not measured against this target |
| concurrency | 0/15 | sustained concurrency was 0, policy allows at least 5 |
| contract | 0/15 | contract crash rate was 11.1%, policy allows at most 0.0% |
| behavior | 26/35 | recovery rate was 19.0%, policy allows at least 90.0% |
| **total** | **30/100** | |

## Phase 1 — Baseline

How the target behaves under normal conditions.

### Latency

p50 12.54s, p95 44.56s, p99 46.44s over 40 requests (32.5% errors)

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

**Findings**

- p95 latency is 44.6s. Anything with a 30s client timeout in front of this target will read one call in twenty as a hard failure.
- Heavy tail: p99 (46.44s) is 3.7x p50 (12.54s). Investigate retries, cold starts, or lock contention before optimizing the median.
- 13/40 requests failed (32.5%): 5 server_error, 4 timeout, 4 rate_limit.

### Cost

_Not applicable to this target: 652 in / 120 out tokens per request, no price known for this model._

- No published price for model unknown, so token counts are reported without a dollar projection. Pass --price-in and --price-out to project cost yourself rather than have one guessed.

### Concurrency

saturates at 1 concurrent, sustained 0

**Score:** 0/100

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

**Findings**

- The target exceeded the error threshold at a single concurrent request. This is not a concurrency limit; something is broken at any load.

### Contract

18 edge cases across 3 tools: 5 rejected cleanly, 11 accepted, 2 crashed

**Score:** 0/100

| metric | value |
|---|---|
| tools probed | 3 |
| edge cases | 18 |
| rejected cleanly | 5 |
| accepted | 11 |
| accepted but invalid | 4 |
| crashed | 2 |

| edge case | worst outcome |
|---|---|
| null_required | **crashed** |
| empty_string | accepted |
| wrong_type | **crashed** |
| very_long_string | accepted |
| missing_required | accepted |
| extra_param | accepted |

**Findings**

- 2/18 edge cases brought the tool down rather than returning an error: null_required (timeout), wrong_type (timeout). Malformed input from a model is normal traffic, not an attack.
- 4 inputs the schema forbids were accepted with a success response: missing_required, null_required, wrong_type. The tool is not validating what it declares, so invalid data reaches whatever it writes to.

## Phase 2 — Fault injection

The same probes against a target we are deliberately breaking.

### Fault injection

28 faults injected, 4/21 operations recovered (19%), 1.95x call amplification

**Score:** not scored

| metric | value |
|---|---|
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
- Mean recovery takes 19.2s from first failure to success. That is user-visible even when the retry eventually works.
- Under fault the latency probe saw a 48% error rate, p95 44.56s.

## Phase 3 — Behavior analysis

What the target did once things started failing.

### Behavior

4/21 disrupted operations recovered (19%), 1.95x call amplification, 0 duplicate mutations

**Score:** 74/100

| metric | value |
|---|---|
| operations | 40 |
| attempts | 78 |
| amplification | 1.95x |
| disrupted | 21 |
| recovered | 4 |
| recovery rate | 19.0% |
| mean recovery | 19.22s |
| duplicate mutations | 0 |
| stuck loops | 17 |

| final status | operations |
|---|---|
| success | 23 |
| failed | 17 |

**Findings**

- 17/21 disrupted operations never recovered (19% recovery rate), most often 4 after connection_refused, 4 after timeout, 3 after malformed. These are the calls a user would experience as a hard failure.
- Recovery takes 19.2s on average and up to 36.7s. The retry works, but the caller waits through the whole thing.
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
