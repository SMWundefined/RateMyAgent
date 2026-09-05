# Architecture

For contributors. If you want to *use* RateMyAgent, start with the [README](../README.md).

## The shape of the thing

```
cli.py ──> scanner.scan(target, probes, phases, config, policy)
                │
                │  target.setup()
                ▼
        ┌───────────────────────────────────────────────┐
        │ phase 1  baseline   latency, cost,            │
        │                     concurrency, contract     │
        │ phase 2  chaos      fault  ──> FaultProxy     │
        │ phase 3  behavior   behavior                  │
        └───────────────────────────────────────────────┘
                │  target.teardown()
                ▼
        policy.evaluate(result, policy)   ── scores it
                │
                ▼
        outputs/  scorecard · report · agents_md
```

Four ideas carry the whole design. Everything else is detail.

### 1. One `Target` interface, everything else is a probe

Probes never know what they are talking to. `MCPTarget`, `LLMTarget`, `MockTarget` and
`FaultProxy` all satisfy the same ABC, so a probe written once works against a local
MCP server, a hosted model, a synthetic fixture, or a sabotaged version of any of them.

```python
class Target(ABC):
    async def setup() -> None
    async def invoke(request: Request) -> Response
    async def teardown() -> None
    def describe() -> TargetInfo
    def list_tools() -> list[ToolInfo]
    def sample_request(index) -> Request          # representative traffic
    def probe_requests(count, offset=0) -> list[Request]
```

`probe_requests()` is the piece that makes this work. Probes do not invent payloads --
they ask the target for traffic that makes sense for it. The latency probe has no idea
whether it is timing an MCP tool call or a chat completion.

**Invoke never raises for target-side failures.** A failed call comes back as
`Response(ok=False, error_kind=...)`. Only a target that cannot be used at all raises
`TargetError`. This is what lets a probe measure an error rate instead of dying at the
first 500.

### 2. The `FaultProxy` is a `Target`

This is the centerpiece, and the reason phase 2 works at all:

```python
class FaultProxy(Target):
    def __init__(self, inner: Target, faults: FaultConfig): ...
```

Everything except `invoke()` delegates to `inner` — including `probe_requests()`, so a
wrapped target generates *identical traffic* to an unwrapped one. Phase 2 re-runs the
phase 1 probes against the proxy, and any difference in the numbers is attributable to
the faults and nothing else. It composes: a proxy can wrap a proxy.

Two decisions inside it are load-bearing:

**Injection is seeded per `(trajectory, attempt)`, not per request.**

```python
rng = random.Random(f"{seed}:{request.trajectory_key}:{attempt}")
```

If it were seeded per request, a faulted call would fail identically on every retry and
no target could ever be shown to recover — the entire recovery metric would read zero
for everyone. Because `attempt` is in the seed, each retry draws independently while the
whole run still reproduces exactly under a given `--seed`, in any call order.

**Malformed injection still calls the target and pays its real latency.** A truncated
payload costs as much to produce as a valid one; if malformed responses arrived instantly
a probe would be measuring the harness. It also refuses to corrupt an already-failed
response, which would overwrite a real observation with a synthetic one.

The other four faults short-circuit before reaching the target, because a timeout or a
refused connection means the target never saw the call.

### 3. Trajectories, not just error counts

Every call through the proxy is recorded as an `Invocation`. Invocations sharing a
`trajectory_key` form a `Trajectory` — one logical operation and all its attempts.

```python
@dataclass
class Trajectory:
    trajectory_id: str
    invocations: list[Invocation]

    # all derived, none stored:
    attempts / retries / failures
    recovered            # a success after a failure
    recovery_latency_s   # first failure -> the success that resolved it
    duplicates           # repeated *successes* = duplicate mutations
    loops_detected       # 3+ attempts, never resolved
    final_status
```

**Everything is a property, nothing is a stored field.** Append an invocation and every
derived value updates. A trajectory cannot go stale, which matters because it is built
incrementally while the probe runs.

`duplicates` counts repeated *successes* rather than repeated attempts. That is the
dangerous case: a retry that succeeds twice ran the same mutation twice. Failures
repeating are just retries.

Identity comes from two properties on `Request`:

- `fingerprint` — hash of op + payload, deliberately **excluding** `label`. Two retries of
  one operation must share a fingerprint or duplicate detection sees nothing.
- `trajectory_key` — `trajectory_id or label or op`, groups attempts of one operation.

### 4. Probes measure, the policy judges

Probes have no `grade()` method. They produce metrics and findings; `policy.py` decides
what those are worth. That separation is what makes the bar configurable per project
instead of baked into each probe — and it arrived in week 4 by deleting an A–F `Grade`
enum that had spread through every probe.

A probe that emits a verdict is a bug. There was one: the latency probe said "No latency
problems found" while the policy check directly above it reported that same p95 as
failing. Findings state what was measured; the policy states whether it is acceptable.

## The phase pipeline

`scanner.scan()` groups probes by `Probe.phase` and runs the phases in fixed order,
whatever order they were requested in. Phase 3 analyses what phase 2 recorded, so running
them out of order produces numbers with nothing behind them.

Phases hand data forward through a `ScanContext`:

```python
@dataclass
class ScanContext:
    results: list[ProbeResult]
    artifacts: dict[str, Any]      # "trajectories", "invocations", "fault_config"
```

Phase 2 deposits its trajectories; phase 3 reads them. Neither probe reaches into the
other, so both still run standalone — the behaviour probe run alone finds nothing and
says so rather than inventing numbers.

Two subtleties worth knowing before you change this:

- **Only the recovery pass's trajectories cross over.** The fault probe's degradation
  pass sends one-shot probe traffic; counting those as operations drags retry
  amplification toward 1.0 and hides real disruption.
- **Not every baseline probe is re-run under fault.** A probe opts in with
  `rerun_under_fault`. Only latency does. Under injected faults the concurrency ramp
  reports the fault rate as its saturation point, and the contract probe reads injected
  transport errors as tools crashing on edge-case input — both actively misleading rather
  than merely noisy.

## The policy engine

`policy.py` holds `THRESHOLD_SPECS`, a wiring table from policy key to probe metric:

```python
ThresholdSpec("p95_latency_ms", "latency", "p95_s", "max", "p95 latency", "ms", 1000)
#              policy key       probe      metric  dir    label          unit  scale
```

`scale` converts the probe's unit into the policy's — probes measure seconds, SLOs are
written in milliseconds.

Scoring is deliberately simple and fully documented in [POLICY.md](POLICY.md). Each
threshold produces a `CheckResult` carrying what was required, what was seen, and the
sentence explaining the number. A score nobody can take apart is a score nobody acts on.

Two rules that are easy to get wrong if you touch this:

- **A metric the scan could not produce is skipped, not scored zero.** Missing evidence is
  not a failure. Scoring it as one would punish someone for scanning an MCP server that
  has no token costs.
- **A probe that did not run produces an `unmeasured_check` on the `ScanResult`**, not a
  placeholder `ProbeResult`. `result.probes` stays an honest list of what actually ran.

## Outputs

Three renderings of one scan: terminal scorecard, markdown report, AGENTS.md fix guide.
The blocks they share — the actual-vs-target table, the score breakdown, the verdict
lines — live in `outputs/common.py` so they cannot drift. There is a test asserting the
report and the scorecard carry identical verdict lines.

The AGENTS.md generator is a registry of `Advice` entries with `applies(result)` and
`render(result)`. They key off **metrics**, not off finding strings, so rewording a
finding cannot silently drop a section. Sections carry an explicit `critical` flag rather
than inferring it from `priority`, so reordering the guide cannot change what counts as
critical.

## Determinism

Everything reproduces under `--seed`. Two mechanisms:

- **`MockTarget`** derives behaviour from the request label, not from a shared cursor, so
  a run is identical whether requests go out sequentially or all at once.
- **`FaultProxy`** seeds per `(trajectory, attempt)` as described above.

`MockTarget` also reports *simulated* latency instead of sleeping. A 50-request profile
of a 3-second target finishes instantly while the probe's arithmetic stays real — which
is why 559 tests run in under a second.

The one thing that does not reproduce is wall-clock `Duration:`, and a test learned that
the hard way by comparing full CLI output between two seeded runs.

## Where to add things

| You want to add | Do this |
|---|---|
| A new probe | Subclass `Probe`, set `name`/`phase`, register in `probes/__init__.py`, add to `PROBE_ORDER` |
| A new target type | Subclass `Target`, add to `targets/__init__.py`'s `build_target()` |
| A new fault | Add to `FaultKind`, map it in `_FAULT_TO_ERROR`, handle it in `FaultProxy._reject()` or `_corrupt()` |
| A new policy threshold | Add a `ThresholdSpec`; the probe must already emit the metric |
| A new AGENTS.md section | Add an `Advice` to `ADVICE` with an `applies` predicate keyed off metrics |

Every probe needs tests that run without API keys, a network, or an MCP server. Use the
mock targets in `tests/conftest.py`.

## Known limitations

These are deliberate, and documented so nobody rediscovers them as bugs:

- **Synthesized arguments are structurally valid, not semantically valid.** When you do
  not pass `--tool-args`, the scanner builds arguments from the tool's JSON Schema:
  the right shape, the right types, but placeholder values like `"ratemyagent probe"` for
  any unconstrained string. A server whose tool expects a real path, URL, or package name
  will reject every call, and the scan will faithfully measure its *rejection path* — a
  low score that says nothing about the tool's actual reliability.

  The scanner warns when this looks like it is happening. **Pass `--tool` and
  `--tool-args` with realistic values before trusting a score.** Seeding synthesis from
  the schema's `examples` / `default` / `enum` is the obvious fix and is not done yet.
- **Faults are transient only** — injected independently per attempt, so retries almost
  always succeed. Sustained outages are not modelled, and that is the mode that actually
  breaks systems.
- **`duplicate_mutations` is structurally reachable but rare in practice**, because the
  canonical generator — a timeout *after* the work completed — is not implemented; the
  timeout fault short-circuits before reaching the target.
- **`loops_detected` is not independent of `recovery_rate`** while `max_retries` is 2: an
  operation that exhausts its retries is one that did not recover. It becomes a distinct
  signal once the target retries internally.
