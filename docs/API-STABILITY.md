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

Four fields worth naming because they are recent and load-bearing:

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

`Caveat.effect` is frozen too: `"suppress"`, `"annotate"`, `"inapplicable"`. It
has a consumer and an invariant test asserting, in both directions, that a
suppressed metric is `None` and a skipped check has a caveat.

### Enumerations

`ErrorKind` and `FaultKind` members. Adding a member is a minor release;
removing or renaming one is major.

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
