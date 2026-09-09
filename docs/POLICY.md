# Policy and scoring

A policy is a YAML file that says what "reliable enough" means for your project. The
scanner measures; the policy decides. That separation is the whole point — the bar is
yours, not the tool's.

```bash
ratemyagent policy                                    # show the shipped defaults
ratemyagent scan --target mock --policy my.yaml       # score against your own
ratemyagent ci   --target mock --policy my.yaml       # ...and gate on it
```

## How a score is built

Every threshold in the policy produces one **check**. Every check produces a 0–100 score
and the sentence explaining it. Checks roll up into per-dimension points, and the points
sum to the overall score.

```
threshold ──> check (0-100) ──> dimension points ──> overall score ──> pass/fail
```

### The curve, in full

- **Meeting a threshold scores 100 for that check.** A threshold is a *limit*, not a
  target. Being ten times under it is not ten times better — it is the same "within
  policy". This keeps the score stable and interpretable.
- **Missing it decays linearly to 0 at twice the limit** (for a `max` threshold) or at
  zero (for a `min` threshold). A near miss and a catastrophe should not score alike.
- **A `max` threshold of 0 is absolute.** Any violation scores 0. That is what
  `duplicate_mutation_max: 0` is for — "never run the same mutation twice" has no partial
  credit.
- **A metric the scan could not produce is skipped, not scored zero.** Missing evidence is
  not a failure. Scoring it as one would punish you for scanning an MCP server that has no
  token costs.

Worked examples, against `p95_latency_ms: 5000`:

| Observed | Score | Why |
|---|---|---|
| 400ms | 100 | within policy |
| 5,000ms | 100 | exactly at the limit is still within it |
| 6,000ms | 80 | 20% over |
| 7,500ms | 50 | halfway to double |
| 10,000ms | 0 | at twice the limit |
| 44,000ms | 0 | past it |

And against `recovery_rate_min: 0.90` (a `min` threshold, so the direction flips):

| Observed | Score | Why |
|---|---|---|
| 1.00 | 100 | beats the floor |
| 0.90 | 100 | exactly at it |
| 0.72 | 80 | 4/5 of the way |
| 0.00 | 0 | nothing |

### Weights: how checks become a score

Each dimension carries a share of the 100 points. A dimension's score is the mean of its
own checks; its points are `score / 100 × weight`.

```
Score breakdown:
  latency         16/20     (p95 latency was 7,988ms, policy allows at most 5,000ms)
  cost            -/15      (not measured against this target)
  concurrency     -/15      (no policy threshold reads it)
  contract         8/15     (invalid inputs accepted was 9, policy allows at most 0)
  behavior        35/35
```

**A dimension the scan could not measure drops out of the denominator** rather than
capping the total. In the example above, cost is unmeasurable (a mock has no published
price), so the score is computed over the 85 points that *were* available rather than out
of 100:

```
(16.02 + 7.50 + 35.00) / 70 × 100 = 84.9  ->  84
```

The displayed points are rounded for reading; the score is computed from the unrounded
values. Adding up the column as printed can land a point either side of the total.

Default weights:

| Dimension | Weight | Why |
|---|---|---|
| `behavior` | 35 | Recovery, amplification and duplicate mutations are the questions this tool exists to answer. A fast target that loses work under failure is not reliable |
| `latency` | 20 | The budget every caller above you spends |
| `cost` | 15 | |
| `concurrency` | 15 | Reported, not scored since 0.1.10. No threshold reads it, so the weight never enters a denominator |
| `contract` | 15 | |

`fault` has no weight on purpose: it is the injector, not a judged dimension. What it
produced is scored under `behavior`.

## Passing is a conjunction

A scan passes when **both** conditions hold: the composite is at or above `pass_score`,
**and** no check failed.

The second half is not redundant. The composite is a weighted mean over dimensions, and a
dimension is the mean of its checks, so one failure can be diluted twice over. A recovery
rate of 85.7% against a 90% floor scores 95.2 on the curve, averages with two passing
behaviour checks to 98.4, and lands as 34.4 of 35 points — a total cost of about 0.6 out of
100. Before 0.1.6 that produced `PASS: score 99` printed above a table containing `FAIL`,
which is a report arguing with itself.

Reliability is closer to a conjunction than an average: a service that is fast, cheap and
loses 14% of its retries is not 99% reliable. The score still answers "how far off", and
the verdict answers "is anything wrong".

```
FAIL: score 99 meets pass threshold 75, but 1 check failed: recovery rate.
```

Note that the score is unchanged by this — only the verdict. A scan that reported PASS
before 0.1.6 can report FAIL after it with an identical number.

## The shipped default, threshold by threshold

`ratemyagent/policies/production-default.yaml`, `pass_score: 75`. Defaults for a tool or
model endpoint that something else depends on in production.

### Phase 1 — baseline

**`p95_latency_ms: 5000`** — 95% of calls answer within 5 seconds.
Deliberately loose for an LLM-backed service. The number that matters is what it means
upstream: an agent making three tool calls inherits the p95 of each, so a 5s tool is a
15-second turn before the model writes a word.
*Reads `latency.p95_s`.*

**`p99_latency_ms: 10000`** — the tail stays under 10 seconds.
Set to twice p95 rather than a round number, so a heavy tail fails this before it fails
p95. A p99 far above p95 is a different bug from "the service is slow".
*Reads `latency.p99_s`.*

**`error_rate_max: 0.05`** — under 5% of calls fail unprompted.
This is the *baseline* error rate, with no faults injected. Anything failing 1 call in 20
on a good day has no headroom for a bad one.
*Reads `latency.error_rate`.*

**`cost_per_request_max: 0.10`** — 10 cents per call.
Only scored when the model's price is known; skipped otherwise. Generous on purpose —
it is a runaway-cost tripwire, not a budget. Tighten it hard if you know your numbers.
*Reads `cost.cost_per_request`.*

**`concurrency_min`** — **retired in 0.1.10. Still accepted in a policy file, with a
warning, but no longer scored.**
It passed only when the ramp reached the configured ceiling, which means it compared
`--concurrency` against itself: scanning with the default ceiling of 5 could only ever
prove 5, and scanning a healthy server with `--concurrency 3` failed it and capped the
composite at 89 — while the probe's own finding said the ceiling was "a floor set by the
test, not a measurement of the target". Saturation point, latency knee and goodput are
still reported as findings. A ramp that genuinely saturates *below* its ceiling would be a
real measurement and could be scored, as a differently named check, once a server produces
one.

**`contract_invalid_accepted_max: 0`** — the tool rejects what its own schema forbids,
**on every required field**. Cases are generated per field since 0.1.15, so a tool that
validates its first argument and ignores its second now fails this check instead of passing
it.
Scored, with its denominator stated rather than assumed. Edge-case probing covers at most
three read-only tools, so on a twelve-tool server a `0` means "none of the three tools
probed accepted a violation", and every surface that prints the number prints the coverage
beside it. Two alternatives were measured across the nine-server set before settling on
this: skipping the pass at partial coverage turned out to be arithmetically incapable of
changing any score, and dropping the dimension entirely scored a server 0 for a scan in
which every edge case was correctly rejected. See PROGRESS section 11.
*Reads `contract.accepted_invalid`.*

**`contract_crash_rate_max: 0.0`** — malformed input never kills the transport.
Scored only against a clean control: well-formed calls interleaved with the malformed ones
must all have been delivered. A target that drops calls regardless of input reports its
crash rate and is not scored on it — this is an absolute check that caps the composite at
49, and capping a server for a flaky connection would be a fabricated finding.
Absolute. An unhandled exception in a handler takes down the connection, and every other
in-flight request with it. Returning an error is always available.
*Reads `contract.crash_rate`.*

**`contract_invalid_accepted_max: 0`** — never accept input your own schema forbids.
Absolute, and the quieter of the two contract rules. Nothing errors; the garbage just
reaches whatever the handler writes to, and surfaces hours later somewhere unrelated.
*Reads `contract.accepted_invalid`.*

### Phase 3 — what it did when things broke

**`recovery_rate_min: 0.90`** — 90% of disrupted operations come back **within the
scanner's retry budget of 2**. The budget is what makes this a measurement rather than a
ratio: the same target scores differently at one retry than at ten. It is hardcoded
(`FaultInjector.max_retries`), no flag reaches it, and since 0.1.12 every surface that
prints a recovery rate prints the budget beside it. Changing it would change what the
metric means and break comparability with published scores, so it is deliberately not a
knob.
The number that separates a service that degrades from one that drops work. Every
unrecovered operation is a user-visible hard failure — not a slow response, a lost one.
*Reads `behavior.recovery_rate`.*

**`retry_amplification_max: 2.0`** — a failure costs at most 2 calls, not 10.
Amplification is what turns a partial outage into a total one: every caller multiplies its
load on the thing already struggling. The dangerous property is that it looks fine in
testing, because with a healthy dependency the retries never fire.
*Reads `behavior.retry_amplification`.*

**`duplicate_mutation_max: 0`** — never run the same mutation twice.
Absolute, and the only threshold where a single violation scores zero. This is the failure
mode behind double charges and duplicate rows, and it is invisible to every metric that
counts errors, because both attempts *succeeded*.
*Reads `behavior.duplicate_mutations`.*

## Writing your own

Start from the shipped file and delete what you do not want scored — **every threshold is
optional**, and an absent one simply does not count.

```yaml
name: my-service
description: >
  Tighter than the default on latency and recovery, looser on cost.

thresholds:
  p95_latency_ms: 2000
  error_rate_max: 0.01
  recovery_rate_min: 0.99
  retry_amplification_max: 1.5
  duplicate_mutation_max: 0

weights:
  latency: 30
  behavior: 50
  contract: 20

pass_score: 85
```

```bash
ratemyagent ci --target mcp --uri stdio://./server.py --policy my-service.yaml
```

**Validation is strict, and fails loudly.** An unknown threshold key is an error listing
the valid ones — a typo that silently stopped scoring something would be worse than a
crash. Non-numeric values, negative thresholds, a `pass_score` outside 0–100, and a
policy with no thresholds at all are all rejected.

Weights are optional; omit them and the defaults apply. They do not need to sum to 100 —
the score normalizes over whatever was measured — but keeping them at 100 makes the
breakdown read as points out of a total.

### Available thresholds

| Key | Direction | Reads |
|---|---|---|
| `p95_latency_ms` | max | `latency.p95_s` |
| `p99_latency_ms` | max | `latency.p99_s` |
| `error_rate_max` | max | `latency.error_rate` |
| `cost_per_request_max` | max | `cost.cost_per_request` |
| `concurrency_min` | min | *retired in 0.1.10 — accepted, warned about, not scored* |
| `contract_crash_rate_max` | max | `contract.crash_rate` |
| `contract_invalid_accepted_max` | max | `contract.accepted_invalid` |
| `recovery_rate_min` | min | `behavior.recovery_rate` |
| `retry_amplification_max` | max | `behavior.retry_amplification` |
| `duplicate_mutation_max` | max | `behavior.duplicate_mutations` |

`ratemyagent policy` prints this for whatever policy you pass, including which keys are
unset and therefore not scored.

## Reading the result

```
                             actual     target     status
  p95 latency                7.99s      5.00s      FAIL
  schema violations accepted 9          0          FAIL
  error rate                 0.0%       5.0%       pass
  cost per request           -          $0.1000    n/a
```

`n/a` means the check was skipped — the probe could not measure it against this target.
Skipped checks never count as failures and never move the score.

The last two lines of any scorecard are the verdict:

```
FAIL: score 31 below pass threshold 75.
Biggest gaps: latency (0/20), contract (4/15).
```

`ratemyagent ci` exits **0** on pass, **1** on fail, and **2** when the scan could not run
at all — a broken scanner is not a failing target, and a gate that cannot tell them apart
is not worth having in a pipeline.

## A caveat worth knowing

`recovery_rate_min` is scored from however many operations phase 2 actually disrupted. At
the default 20 requests and a 0.2 fault rate that is roughly 4 — and 4 samples is not a
rate, it is an anecdote. The behaviour probe says so in its findings, but the check still
scores. For a CI gate you rely on, use enough requests that the number means something:

```bash
ratemyagent ci --target mcp --uri stdio://./server.py --requests 120 --fault-rate 0.25
```
