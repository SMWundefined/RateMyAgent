# API stability

What 1.0 promises, what it does not, and how something moves from the second
list to the first.

Semantic versioning applies to everything under **Frozen**. Breaking one of
those needs a major version. Everything under **Not frozen** can change in a
patch release, and the sections below say why each one is there.

---

## Frozen

### Entry points

| Surface | Promise |
|---|---|
| `scan(target, *, probes, phases, config, policy)` | signature and return type |
| `MCPTarget`, `LLMTarget`, `MockTarget`, `FaultProxy` | constructor keywords, `Target` interface |
| `ProbeConfig` | field names, types, defaults, and `max_retries >= 1` |
| `Policy`, `Policy.default()`, `evaluate()` | threshold keys, weights keys, cap semantics |
| CLI flags on `scan` and `ci` | names and meanings |
| Exit codes | `0` pass, `1` policy failure, `2` the scan did not complete |

### Result shapes

`ScanResult`, `ProbeResult`, `CheckResult`, `Caveat`, `Request`, `Response`,
`Invocation`, `Trajectory`, `TargetInfo`, `ToolInfo` — field names and types, and
the keys their `to_dict()` produces.

Five fields worth naming because they are recent and load-bearing:

- **`Caveat.scope`** — `"metric"` or `"probe"`. Without it a consumer cannot
  tell a caveat about one number from one about the whole probe.
- **`CheckResult.threshold_source`** — `"policy"`, or the metric a threshold was
  derived from. The only way a JSON consumer can tell a derived recovery floor
  from the policy literal, and two scans at different fault rates are not
  comparable without it.

- **`DimensionScore.not_scored`** (1.1.0) — `None` when the dimension was
  scored, otherwise one of `"not_selected"`, `"phase_excluded"`, `"did_not_run"`,
  `"not_applicable"`, `"no_threshold"`. Two of those are facts about the target
  and three about the command, and before 1.1.0 all five rendered as one of three
  English sentences in `note`. A consumer telling "your `--probes` excluded it"
  from "your target cannot measure it" had to substring-match prose. `note` stays
  as the human sentence and is derived from this value, so the two cannot drift.
- **`ScanResult.graded_weight`** (1.1.0) — total weight of the dimensions the
  policy actually asks about, and the denominator `score` is a percentage of. Not
  derivable from the breakdown: a dimension reports `no_threshold` only if its
  probe ran, so a scan that skipped it cannot tell policy silence from its own
  omission. A consumer comparing two scans under different policies needs it for
  the same reason `threshold_source` exists.
- **`ScanResult.passed`** (1.4.1) — `true`, `false`, or `null`. `null` means
  the scan was scored without a verdict: no policy, or a requested measurement
  (`--verify-tool`) that did not complete. Treat anything other than `true` as
  not passed; `passed is False` alone misses the unmeasured case.

- **`Invocation.executed`** (1.3.0) — `True` the target returned success to the
  proxy before any injected damage, `False` the proxy refused the call without
  forwarding it, **`None` unknown**. Distinct from `ok`, which records only what
  the *caller* saw. 1.3.0 described `True` as "the target ran the call"; it is an
  acknowledgement, and says nothing about what the call changed. The name stays
  because the field is frozen. `None` is the load-bearing value and the reason
  this is not a boolean: a timeout from a real server may have completed the work
  and lost the reply, or never started, and the caller cannot tell.
  `Trajectory.duplicates` reads it with `is True`, so unknown is never counted.
  Its sum is published unscored as the metric `duplicate_deliveries` (1.3.1): an
  unfrozen key carrying the value of this frozen field, which is why publishing
  it needed no promotion.
- **`Trajectory.duplicates`** is delivery-based — calls the target acknowledged more than once in one operation, as observed by the scanner — and already was as of 1.3.0; it has never measured applied effects.

`Caveat.effect` is frozen too: `"suppress"`, `"annotate"`, `"inapplicable"`. It
has a consumer and an invariant test asserting, in both directions, that a
suppressed metric is `None` and a skipped check has a caveat.

### Packaging

Extras are **not** on this list and are not frozen — but `[mcp]`, `[anthropic]`, `[openai]`
and `[all]` keep resolving, because an install line in someone's CI file is a promise in
practice. `mcp` moved into the core dependencies in 1.3.2 and `[mcp]` stayed as an empty
extra for exactly that reason.

### Enumerations

`ErrorKind` and `FaultKind` members. Adding a member is a minor release;
removing or renaming one is major. `FaultKind.RESPONSE_LOST_THEN_CLOSED` was
added under that rule, which is why it shipped in **1.6.0** rather than in a
patch.

**Enum membership is not injection-set membership.** `FaultKind.RESPONSE_LOST`
(1.3.0) is in the enum and deliberately not in `ALL_FAULTS`: `FaultConfig.uniform`
divides the total rate by the kind count and `FaultProxy._choose_fault` walks
cumulative thresholds, so a sixth member in the default set moves every boundary
and re-assigns every seeded draw in every recorded scan. A new kind is placed in
`ALL_FAULTS`, `OPT_IN_FAULTS` or `CLOSING_FAULTS` on purpose, and a test asserts
the three partition the enum.

`CLOSING_FAULTS` (1.6.0) holds kinds in **neither** default set.
`RESPONSE_LOST_THEN_CLOSED` is its only member. A third tuple rather than a
seventh opt-in kind for the same arithmetic: the opt-in set is added wholesale
when a scan is cleared to mutate, so a seventh member there would divide every
agent scan's rate by seven and move every boundary again.
`--lost-reply-close-after` therefore **substitutes** the closing kind for
`RESPONSE_LOST` in the opt-in slot rather than adding it, which leaves the kind
count at six and every recorded draw where it was.

### The eleven scored metric names

`ProbeResult.metrics` is a bare dict and most of it is not frozen. **The names a
`ThresholdSpec` reads are**, because the score depends on them:

```
p95_s   p99_s   error_rate   cost_per_request   max_sustained_concurrency
crash_rate   accepted_invalid   recovery_rate   retry_amplification
duplicate_mutations   recovery_floor
```

Eleven, counted from `THRESHOLD_SPECS` rather than estimated — the first draft
of this document said twelve.

---

## Not frozen

### `ProbeResult.metrics`, apart from those eleven

Everything else in that dict is **reportable but unfrozen**. It is where a probe
puts what it saw, and five keys landed there in one week as diagnostic
byproducts of a single investigation: `unscored_crash_rate`, `recovery_ci`,
`unscored_recovery_rate`, `accepted_invalid_evidence_thin`, `jsonrpc_code`.

Those are useful and should keep being emitted. They should not be a contract.
`unscored_crash_rate` in particular exists to show a number the scan *declined
to score* — freezing it would make the shape of our own uncertainty a promise.

### `Response.meta`

A debugging channel, and explicitly not part of the JSON contract even though it
appears in the export.

Its keys today are `injected`, `status`, `retry_after_s`, `in_flight`,
`simulated`, `reason_unclassified`, `jsonrpc_code`. Which appear depends on
which fault fired and which transport answered, so a consumer cannot read it
without branching on things nothing promises.

**The decisive reason is `jsonrpc_code`.** It exists because the MCP SDK
surfaces a delivered protocol error by raising an exception that carries the
code. That is an SDK implementation detail. Freezing `meta` would pin this
project to it, and the same SDK has already renamed a client function and
changed a yielded tuple's arity between majors.

### Experimental: the agent path (1.5.0, extended in 1.6.0)

**Not frozen, and not promised to survive a minor release in its current shape.** Phase D
is under way and has already changed it: 1.6.0 added the launch contract below after the
first real agent could not be launched by the 1.5.1 one at all.

- `AgentTarget`, its constructor keywords, and `Target.injects_out_of_process`
- `--target agent`, `--agent`, `--tasks` and `--upstream` on `scan` and `ci`
- `--agent-command`, `--claim-path`, `--work-dir` and `--lost-reply-close-after` (1.6.0),
  and `FaultConfig.close_after_s`
- `ratemyagent proxy`, its flags and the `RMA_*` environment variables
- the record format (JSONL rows), the schedule file, the task file, and the MCP config
  handed to the agent
- `FaultConfig.schedule`, `FaultConfig.task_id`, and `FaultProxy(ordinals=...)`
- the agent metrics: `unsupported_claims`, `lost_acknowledgements`, `backoff_shape`,
  `backoff_growth`, `retry_after_honored`, `effect_attribution`, `task_oracle_status`,
  `effects_by_task`, `uncertain_tasks`, `uncertain_task_ids`, `baseline_effects_by_task`,
  and the rest of what the behaviour probe adds on this path
- `coverage_rule` and `agent_kind` in `TargetInfo.metadata`, and the agent verdict rule
  `coverage_rule` selects
- `--hold-reply` (1.6.1), the `deadline` pass, the schedule file's `hold_s`, and the
  `client_timeout_*` metrics
- `realized_schedule`, `realized_placement` and `intended_schedule` on the fault probe,
  and `held_s` on a record row
- `ratemyagent.probes.repeats` in its entirety
- `nothing_applied`, `runs_applied_nothing`, `runs_measured`, `baseline_state_carryover`,
  `baseline_state_carryover_tasks` and `baseline_effects_applied` on the behaviour probe, and
  `before` on an agent task row (1.6.2)

Two frozen names are reused rather than invented. `duplicate_mutations` and
`retry_amplification` keep their names, policy keys and cap semantics; on an agent target
the first is attributed per task window rather than per `{op_id}`, and the second is
divided by the clean-path call count. Each carries an unfrozen companion saying so —
`effect_attribution` and `amplification_denominator` — because a changed meaning under a
frozen name has to be visible to a consumer rather than inferred.

**Unscoring one of the eleven on one target type is a patch, and 1.6.1 is the precedent.**
On an **LLM** agent `retry_amplification` is reported and not scored, and three unfrozen
timing metrics are withheld. Nothing frozen moves: the name is still emitted in
`ProbeResult.metrics`, the policy key `retry_amplification_max` is unchanged, and the
dimension reports `not_applicable` — one of `DimensionScore.not_scored`'s frozen values,
which exists for this. The rule it lands under is the first line of **Not frozen**:
everything there can change in a patch release, and the agent path is there in full,
"not promised to survive a minor release in its current shape". The shipped precedent for
the behaviour is older than the agent path — `CALLER_STRATEGY_METRICS` has marked
`retry_amplification` inapplicable on `--target mcp` since 0.1.9 — so a target type where
this frozen name goes unscored is the existing design rather than a new category.

**Adding a condition to the agent verdict rule is a patch, and 1.6.2 is the instance.** That
rule is named under Not frozen two paragraphs above — "the agent verdict rule `coverage_rule`
selects" — so a fifth condition on it changes nothing frozen. 1.6.2 declines a verdict when
every task that was meant to apply an effect applied none in some run. Checked against the
frozen list, one item at a time:

- **Exit codes.** `2` keeps its meaning. The agent verdict rule has exited 2 for a blocked
  verdict since 1.5.0 and `verify_not_measured` since 1.4.1, both under exit 2's documented
  reading — the scan did not produce the measurement it was asked for, so it did not complete.
  A new reason to reach an existing code is not a new code.
- **`ScanResult.passed`.** Already `true | false | null`, and `null` is already documented as
  "scored without a verdict". This adds a case to `null`, not a value.
- **The eleven scored metric names.** Untouched. Nothing is scored, no threshold moves, and
  `duplicate_mutations` keeps its policy key and its cap. `lost_effects` was unscored before
  this release and is unscored after it — the rule declines to certify, it does not charge.
- **`Policy`, `evaluate()`, cap semantics.** Untouched. The verdict is decided after scoring
  and does not reach into it.

`nothing_applied`, `runs_applied_nothing`, `runs_measured`, `baseline_state_carryover` and the
task row's `before` are new keys on the agent path, which is unfrozen in full.

The same reading covers `TargetInfo.metadata`'s **contents**. The field, and the `metadata`
key `to_dict()` produces, are frozen; what sits inside is not, and the one agent-path
metadata key this document names — `coverage_rule` — is listed under Not frozen. 1.5.1
changed what a frozen field *contains* in a patch, when `redact_uri` stopped rewriting
`stdio://`; 1.6.1's redaction of `proxy_command` is the same move on the same argument.

`duplicate_opportunities` is **not** reused. It keeps its shipped server meaning — calls
the target acknowledged whose reply the caller did not see — and an agent scan does not
export it. The agent path's count of tasks with a call whose outcome was unknown is
`uncertain_tasks`, a different quantity under a different name.

### `ProbeConfig.extra`

**Keys in `extra` are not individually frozen. The ones backed by a CLI flag are
stable because the flag is.**

| key | flag | status |
|---|---|---|
| `fault_rate` | `--fault-rate` | stable via the flag |
| `model` | `--model` | stable via the flag |
| `price_in`, `price_out` | `--price-in`, `--price-out` | stable via the flag |
| `contract_tool_limit` | none | **provisional** |
| `requests_per_level` | none | **provisional** |

`contract_tool_limit` is deliberately outside the freeze. Raising it changes
which tools are probed, so those rows stop being comparable with the ones
already published, and `--contract-tools` is on the v1.1 roadmap with a
documented hazard. Freezing the `extra` spelling would freeze something we
intend to replace.

### Everything else

Anything named with a leading underscore, module layout under
`ratemyagent.probes` and `ratemyagent.targets`, the wording of findings and
caveats, log messages, and the exact numbers in any example.

---

## Promotion: how something becomes frozen

An escape hatch with no procedure means nothing is frozen. This is the
procedure.

### What triggers it

Promotion is **required** when any one of these is true. It is not a judgement
call:

1. **The value reaches published output.** If it is printed in a scorecard, a
   report, an AGENTS.md, or a release note, a user can read it and will rely on
   it. `max_retries` was promoted for exactly this: the header prints
   `2 retries` and the floor computed from it.
2. **The score depends on it.** Anything a `ThresholdSpec` reads, or that
   changes a threshold. This is what makes the eleven metric names frozen and
   `recovery_floor` one of them.
3. **A consumer outside this repository asks for it in writing**, in an issue,
   and no frozen field already carries the information.

Two things that do **not** trigger promotion: internal convenience, and "it has
been stable for a while". Stability is a property of the past.

### Who decides

The maintainer, in the issue or PR that requests it. There is one maintainer, so
this rule exists to make the decision *visible* rather than to distribute it: a
promotion is a comment saying which trigger fired.

### How it lands

1. **Lift it into a named field.** A promoted value does not become "the
   `meta` key is now frozen" — it becomes a field on the dataclass, with a
   type, exported by `to_dict()`. This is what already happened twice:
   `reason_unclassified` rides in `meta` and is read from a named
   `cases[].reason_unclassified`; `Caveat.scope` became a field rather than a
   meta key.
2. **Add it to this document** in the same commit as the field. A frozen field
   not listed here is not frozen, and this file is the list.
3. **Add a test that fails if the field disappears from `to_dict()`.** Freezing
   a shape nothing asserts is how it thaws quietly.
4. **Ship it in a minor release.** Adding a frozen field is additive: `1.1.0`,
   not `1.0.1`. The old key stays in `meta` for one minor version so consumers
   have a release in which both work.

### How something is removed

A frozen field is removed in a major release, after one minor release in which
it is still populated and documented as deprecated. Nothing is removed silently
because it "was never really used" — that judgement is exactly what the freeze
exists to take away from us.

---

## Why this document is short

Three things in this project were, at various points, described as stable
because nobody had changed them lately: `concurrency_min` scored against its own
flag for ten releases, `MIN_DISRUPTED_FOR_CONFIDENCE` was named for a guarantee
it stopped providing at week 4, and a docstring claimed `Response.delivered` was
"a fact about whether anything arrived" for thirteen.

A short frozen list that is true is worth more than a long one that describes
intent. Everything not on it can change, and saying so plainly is the point.
