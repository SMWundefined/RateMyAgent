# Changelog

Release notes live on [GitHub releases](https://github.com/SMWundefined/RateMyAgent/releases);
this file records what is in the tree and not yet released.

## 1.7.2 — 2026-09-23: the Gate BD evidence ships, and the gate checkers run in CI

A patch. **No behaviour changes and no output changes**; everything here is evidence,
packaging and test hygiene.

The standing rule is that no finding is published until it is reproduced by a script that
does not import this package. Gate BD's checker satisfied it and lived under gitignored
`assets/`, so the result was reproducible on one machine and **not citable**. It now ships.

### Added

- **`examples/gate-bd/`** — the Gate BD evidence, laid out like `examples/phase-d-gate/`:
  five databases, the five exports, the five scorecards, the per-replicate chaos records,
  the task file and `verify_gate_bd.py`. An applied duplicate in **3 of 5** replicates
  against **unmodified `mcp-sqlite@1.0.9`**, all five realizing the same fault placement,
  re-derived from the databases alone. ~178 KB, sdist only.
  - The README states without softening what the number does and does not mean:
    `mcp-sqlite` applies every insert, so this measures the tool's detection and not the
    agent's key discipline; `--allowedTools` withheld `read_records`, foreclosing
    check-before-retry; the schema was supplied in the prompt. It carries the checker's
    trust table and **does not claim parity with Gate D** — Gate D partitions by a field
    the twin server writes, Gate BD by a fresh store per replicate plus one guarantee the
    scan enforces.
  - The sha256 of each shipped database is in that README, so a reader can confirm the
    shipped copy is the file the gate ran against. Three of the five hashes coincide,
    because replicates with the same outcome produce byte-identical SQLite files; the
    README says so rather than letting five rows imply five fingerprints.
  - The databases are opened `immutable=1`, so running the checker creates no `-wal` or
    `-shm` beside the checked-in evidence. CI asserts that.

- **Both gate checkers now run in CI**, in the `verification-tools` job. Until now
  **neither did** — the standing-rule comment in that job named the repro script and
  stopped there, so the scripts backing the README's two strongest claims were the one
  kind of verifier nothing verified. They are stdlib-only and add about 0.1s.

### Changed

- **Six absence-of-`PASS` assertions tightened**, ahead of a verdict-string change queued
  for its own release. `assert "PASS" not in ...` would also be satisfied by a verdict that
  merely begins with those letters, so each now names the verdict it means: `"PASS:"` for
  the three against scorecard output, `"PASS  score"` for the three against `ci` output,
  whose verdict line carries no colon. Checking `"PASS:"` against `ci` output would have
  been vacuous, which is why the two differ.
- README and `docs/LIMITATIONS.md` cite both gates and say what each establishes that the
  other does not: Gate D varies the agent's idempotency key against a server that can
  absorb it; Gate BD varies nothing but whether the agent retried, against a server this
  project did not write.

## 1.7.1 — 2026-09-22: the twin is told its role, not left to guess it

Fixes the CI failure 1.7.0 shipped with. The event twin worked out whether it was the
agent's copy or the scan's by running `ps` on its own parent process and matching a `proxy`
token — and **every failure to read resolved to `oracle`**, the role that advances the
operation boundary. It failed open, into the role that mutates shared state, and did so on
CI's 3.12 jobs while passing on 3.10, 3.11, 3.13 and locally.
`assets/moat/INVESTIGATION-1.7.0.md` has the diagnosis; the 3.12-only trigger was never
established, and with the inference deleted it no longer needs to be.

**The gate result is unaffected.** `assets/moat/GATE-D2.md` ran on a copy that inferred
*strictly* and exited rather than guessing, and its ledger shows all 24 rows classified
correctly with the generation constant inside every run.

### Changed

- **`--role agent|oracle` on the event twin: required, no default.** Absent or unrecognised
  exits 2 from `parse_args`, before the state is read and before anything is written, and an
  unrecognised value routes to neither role. The twin now reads no ambient signal at any
  `--key-scope`: not its parent process, not its own argv beyond declared flags, not the
  environment.
- **`{role}` in `--upstream`.** An agent scan launches the upstream twice, in two roles that
  must not be confused, from one string the user authored. `{role}` is substituted at each
  point of consumption — `agent` for the copy behind `ratemyagent proxy`, `oracle` for the
  scan's own read of the state. `self.upstream` keeps the authored string, so the report and
  the export show what was asked for rather than one of the two things that ran.

  **A command with no `{role}` is passed through byte-identical**, so every upstream that
  worked before still does. Tested against a non-twin upstream.

### Fixed

- `test_the_generator_exists_and_offers_a_check_mode` no longer skips in CI. It reads only
  `tools/schema_strictness.py`, which is tracked, but was gated on an `assets/` path it never
  opens. The other eight `assets/`-gated tests in that file are untouched; guarding inside
  the body is a separate change.

### Added

- Three arms on the operation boundary where there was one: a twin with no `--role` exits
  non-zero **and leaves both the state file and the counter untouched**; the agent's copy
  never advances the boundary; the oracle's copy advances it exactly once per process across
  repeated reads. The old guard asserted only that the counter had not moved, so a twin that
  created the state file and then refused would have passed it.
- `assets/moat/phase-d/gate2/event_twin_mcp_server.py` carries a frozen-record header naming
  the run it produced, that it is not maintained, and the live fixture. Kept, not deleted:
  it is the measurement script behind a cited result.

## 1.7.0 — 2026-09-22: the Phase D gate, met

**No library code changed in this release.** The tool is byte-identical in behaviour; what
moved is a shipped test fixture's default, the documentation, and the evidence.

The Phase D gate is met (`assets/moat/GATE-D2.md`): five replicates of one task against
`claude-haiku-4-5`, the same realized fault placement in all five, **a duplicate mutation
in 2 of 5**, confirmed by the server's own ledger and re-derived by a stdlib-only script.

### Changed

- **The event twin scopes an idempotency key to one operation, not forever.**
  `tests/fixtures/event_twin_mcp_server.py` gains `--key-scope global|operation`, and
  **`operation` is the new default**. A key belongs to an operation; running the same task
  again is a second operation, and a server that absorbs it is swallowing real work rather
  than recognising a duplicate. `careful_agent` has said exactly that in its own docstring
  since Phase C, and the fixture contradicted it.

  It cost a gate run to notice, because no fixture agent could produce it: they mint a key
  per process or send none. A real model derived its key from the task's own content, the
  scan's clean pass spent it, and four of five runs applied nothing while the report still
  said 100/100 PASS. `--key-scope global` keeps the old behaviour and is exercised by
  `tests/test_key_scope.py`, so the flag is live rather than a dead branch.

  **Nothing in the existing suite moved** — all 1539 pre-existing tests pass unchanged
  under the new default, which is the same reason the defect survived four releases.

### Added

- `tests/fixtures/agents/stable_key_agent.py` — the gate run's agent reduced to a fixture:
  a key that is a function of the task, and one reconnect on a closed session. The only
  fixture whose key is not a function of the process, and therefore the only one that can
  reproduce the collision.
- `tests/test_key_scope.py` (11 tests) — both scopes pinned, the within-run absorption the
  fix must not cost, and the enforcement property: the agent's copy of the twin is spawned
  by `ratemyagent proxy` and can never advance the operation boundary, however often it
  calls the read tool.
- `examples/phase-d-gate/` — the gate evidence: the independent script, the twin's ledger
  and state, the scorecard, the export, two proxy records and the task. Nothing needed
  redacting.
- The twin's ledger rows now carry `pid`, `ts`, `role` and `generation`, so a run can be
  partitioned out of a shared ledger from the server's own file rather than from the
  scanner's records. The ledger stays **one row per `event` call**: an oracle read is not
  written to it, because four consumers read it that way and one uses it as evidence that
  a refusal wrote nothing.

## 1.6.2 — 2026-09-22: a run that applied nothing is not a pass

Built from the Phase D gate run (`assets/moat/GATE-D.md`), which scored **100/100, PASS**
on a scan where four of its five runs applied zero effects for a task whose
`expected_effects` was 1. Nothing in that report was false; the verdict on top of it was.

**No LLM agent was run in this build.** Every assertion here is against the scripted
fixtures in `tests/fixtures/agents/`, with `--swallow-after` reproducing the gate's exact
shape: a twin that absorbs everything after the clean pass. The gate re-run is a separate
session.

### Fixed

- **An agent scan whose runs applied nothing no longer prints PASS.** When every task that
  was meant to apply an effect applied none, the run's effect metrics are arithmetic over
  an empty window — a `duplicate_mutations` of 0 there is the absence of applied writes,
  not evidence the agent was careful. The scan now reports `NO VERDICT` with the reason at
  default verbosity, `passed` is `null`, and `ci` exits 2.

  **A coverage rule, not a penalty.** No score is lowered and no threshold moves;
  `lost_effects` stays the server's fault and stays unscored. It is a fifth condition on the
  agent verdict rule, beside the four already there, and it is the same shape as the two
  nearest: `nothing_completed` (no operation finished) and the uncertainty rule (no task was
  ever disrupted, so no agent could have duplicated). Here the tasks finished, they were
  disrupted, and they left no trace.

  Lands under **Not frozen** — `docs/API-STABILITY.md` names "the agent verdict rule
  `coverage_rule` selects" — so it is a patch. Exit code 2 keeps its meaning: the agent
  verdict rule has used it for a blocked verdict since 1.5.0, and `passed: null` already
  means "scored without a verdict".
- **A probe may now make two statements about the same metric without losing one.**
  `ScanResult.caveats()` deduped on `(metrics, effect)` alone, last write wins, so adding
  this release's carryover caveat silently deleted behaviour's per-task-window attribution
  caveat from every agent scan — both annotate the same three effect metrics. The rule the
  dedupe protects is about two *producers* observing one limit (`fault` and `behavior` both
  emit the thin-sample recovery caveat); two caveats from the same probe are two things it
  meant to say. Dedupe is now across probes and never within one. Caught by the demo
  equivalence diff, not by a test.

### Added

- **A caveat when a run opens its window on state the scan's own clean pass wrote.** One
  clean pass, N chaos runs, one persistent store — so whatever the clean pass applied is
  still there when every later run starts. An agent that derives an idempotency key from the
  task's own content sends the same key in every run, and a server that absorbs a repeated
  key absorbs it across runs too, because the key it matched was spent by the clean pass.
  Every later run then applies nothing for a reason that has nothing to do with the agent.

  That is what the gate run produced, four times in five. The caveat names the mechanism and
  the remedy; **the scan does not repair it**, because isolating state per run means changing
  the user's own task file or state path, and a scanner that rewrote either would be inventing
  the experiment rather than running the one it was handed. Not restricted to `--repeats`:
  the chaos pass is a run after the clean pass at R=1 too.
- **`nothing_applied`, `runs_applied_nothing` and `runs_measured`** on the behaviour probe,
  counted across every run rather than read off the one the trajectory metrics describe.
  `baseline_state_carryover`, `baseline_state_carryover_tasks` and `baseline_effects_applied`
  beside them, and `before` — the store's count when a task's window opened — on an agent task
  row. All unfrozen, on the unfrozen agent path.
- **Docs.** `docs/SCANNING.md` gains two recipes for isolating state per run and states that
  `--repeats` shares one clean pass; `docs/LIMITATIONS.md` gains both limits and the gate
  run's outcome; `docs/API-STABILITY.md` states why this is a patch, item by frozen item.

## 1.6.1 — 2026-09-18: what a scan writes down, and what it declines to score

Verified against the scripted fixtures only. **No LLM agent was run in this build** — the
Phase D gate run is deferred, and everything here is asserted against agents whose source
is in `tests/fixtures/agents/`.

### Fixed

- **`--json-out` no longer publishes the user's home directory.** An agent scan's
  `target.metadata.proxy_command` started with `sys.executable`, so every export from a
  virtualenv — which is every agent scan — carried an absolute path through `$HOME`, in the
  file people paste into issues. The interpreter's directory is now dropped and its name
  kept: `<redacted>/python3.12 -m ratemyagent.cli proxy`. The module invocation and every
  flag stay legible, because they are what says which proxy answered the agent. Relative,
  bare and system-wide interpreters are unchanged. Pre-existing since 1.5.0; found while
  assembling `examples/phase-d/`.
- **`ci --target agent` now honours `--agent-command`, `--claim-path` and `--work-dir`.**
  It validated them and then dropped them on the floor, so `ci` launched 1.5.1's fixed
  argv against a template the user had supplied and had accepted. `scan` always passed
  them; `ci` is the copy that did not. Since 1.6.0. A reconciliation check now derives the
  rule from the command objects and the call sites — every option both commands declare
  reaches `build_target` and `ProbeConfig` from both or from neither — so a flag added
  tomorrow is covered without anyone remembering. A second check asserts `build_target`
  forwards every keyword `AgentTarget` accepts, which is the same defect one layer deeper
  and is how `--agent-kind` was caught being dropped during this build.

### Added

- **`realized_schedule`** — which calls a run actually faulted, in order, reported beside
  the intended table in the JSON and on the scorecard. A schedule entry and a fault that
  fired are not the same thing: an ordinal is only reached if the agent makes that many
  calls to that tool. The three shipped demos schedule 82 entries and realize 8.
- **`--hold-reply [SECONDS]`** — one extra clean-pass task in which the proxy holds the
  first reply and then **sends** it, to measure the agent's own client-side read timeout.
  Three outcomes: it acted at *t*; it waited the hold out (a lower bound, never "no
  deadline"); or it was still waiting at the per-task deadline, which is the finding and is
  not scored. Printed in the report header beside the deadline, so a scan whose deadline is
  shorter than the agent's patience is legible as such. Bare flag holds 10s.
- **`--repeats N`** (default 1) — run the whole task set N times and report a range instead
  of a single value: min–max with the run count and never a mean, an occurrence count for
  anything yes/no, the individual values below n=3, and scoring from the worst observed run.
  Runs are grouped by realized fault placement first, because two runs at one seed that
  faulted different calls are not replicates. The clean pass still runs once. Refused before
  the first agent starts if it cannot fit an explicit `--scan-timeout`, with the arithmetic
  shown.

### Changed

- **Against an LLM agent, `retry_amplification` is reported and not scored, and
  `backoff_shape`, `backoff_growth` and `retry_after_honored` are withheld.** The ratio's
  denominator is the clean-pass call count, measured where no fault was injected, and a
  model chooses its own calls. The timing metrics are wall-clock gaps: a retry loop
  produces a schedule, a model produces an inference round trip, and nothing tells them
  apart. `calls_under_fault` and `clean_path_calls` stay as raw counts.

  Selected by **`--agent-kind scripted|llm`** (default `scripted`), declared and never
  sniffed, for the reason `has_effect_oracle` is: every observable candidate is a threshold
  on a continuum that a loaded machine or a sleeping fixture collapses, and a threshold
  quietly deciding which metrics get scored is `concurrency_min` again. A scripted agent is
  the default and nothing about a scripted scan changes.

### Unchanged, and verified so

Five mock profiles byte-identical across scorecard, report and AGENTS.md; 9/9 section-9
URIs parse the same; the three agent demos gain the realized-placement line and nothing
else moves. The Phase C gate assertions — careful passes, blind capped at 49, optimistic's
unsupported claims — are untouched.

## 1.6.0 — 2026-09-17: the first real agent, and what it demanded

Phase D began. Claude Code 2.1.275 could not be launched by the 1.5.1 agent contract, and
once launched it hung for 234 seconds on a dropped reply with no retry and no cancellation
— so `RESPONSE_LOST` could never produce a measurement against it. This release is both
halves of that.

### Added

- **`FaultKind.RESPONSE_LOST_THEN_CLOSED`** — execute the call, drop the reply, then end
  the session after `--lost-reply-close-after` seconds (default 5). A client with no read
  timeout cannot ignore an end-of-stream, so there is a decision to observe, while the
  call's outcome stays unknowable. Adding a `FaultKind` member is a minor release, which
  is why this is 1.6.0.

  In **neither default set**: a third tuple, `CLOSING_FAULTS`, and the flag *substitutes*
  it for `RESPONSE_LOST` in the opt-in slot rather than adding a seventh kind. The kind
  count stays six, so no cumulative threshold moves and no recorded seed resolves anywhere
  new. It is counted separately from `response_lost` everywhere and never summed.
- **`--agent-command TEMPLATE`** — argv appended to `--agent`, with `{config}`, `{prompt}`,
  `{task_id}` and `{tasks}` placeholders. Defaults to 1.5.1's fixed argv. A supplied
  template must contain `{config}` and `{prompt}` or setup refuses, naming the missing one.
- **`--claim-path PATH`** — read the claim from a dot-separated path inside a single JSON
  document on stdout. A path that finds nothing refuses the scan rather than recording a
  failed task.
- **`--work-dir DIR`** — choose where records, configs and schedules are written. Default
  unchanged.
- The agent scorecard block prints the fault kinds injected and the records directory.

### Changed

- The abandoned-task refusal no longer names Python's `ClientSession` at an agent that may
  not be built on it, and now names the flag that gets past the refusal.

### Unchanged, and verified so

Five mock profiles produce byte-identical scorecards, reports and AGENTS.md; the nine
section-9 URIs parse identically; the three agent demos differ only by the two new output
lines and still draw the same seeded faults.

## 1.5.1 — 2026-09-17: two things that read as a broken target

Both found by re-running gate B against the published 1.5.0. No scan number moves.

### Fixed

- **A `stdio://` path with a space refuses at setup** instead of starting the server with
  one argument too many. Quoting already worked (`"/Users/me/My Files/app.db"`); the
  unquoted form produced the server's own error on every call, so the scan read as a broken
  target rather than a bad URI. The refusal names the path and prints the quoted form, and
  fires only when the joined tokens exist on disk — an argument the run is about to create
  is left alone.
- **A pinned stdio package is no longer redacted as if it were a credential.**
  `stdio://npx -y mcp-sqlite@1.0.9 /path/db` was written down as
  `stdio://npx -y mcp-sqlite:<redacted>@1.0.9 /path/db` in the report header, the JSON
  export and the AGENTS.md state block: the rule is userinfo-before-`@`, and a stdio
  command is a command line, not a URL. It invented a secret rather than hiding one, and
  made the scanned version unreadable. `redact_uri` now rewrites userinfo only on the
  transports that have one (`http`, `https`, `sse`, `sse+http`, `sse+https`). `--env` and
  `--header` redaction is unchanged, and a scoped package (`@modelcontextprotocol/...`)
  renders as written too.
- **A refusal at setup now writes `--json-out`.** Exit 2 with no file left a pipeline
  nothing to read, while `docs/SCANNING.md` read as though every exit-2 case wrote one. The
  document is `refused`, `reason`, `target` and `refused_at`; it is deliberately not a
  `ScanResult`, because a scan that never started is not a scan that measured nothing. No
  frozen field changed.

## 1.5.0 — 2026-09-16: agents (experimental)

Phase C, both halves. Everything on the agent path is
experimental and outside the API freeze (`docs/API-STABILITY.md`); server scans are
byte-identical to 1.4.2 apart from the version line.

### Added

- **`ratemyagent proxy`** — a stdio MCP server holding
  `FaultProxy(MCPTarget(--upstream))`, launched by the *agent* from its own MCP
  config rather than by the scanner. Writes an append-only JSONL record of every
  call, which the scan replays into the same `Trajectory` objects phase 3 has
  always read. Interposition is a new way to fill a trajectory, not a new
  pipeline.
- **`AgentTarget`** (`--target agent`, with `--agent`, `--tasks`, `--upstream`)
  — a `Target` whose `invoke()` runs a *task*. `Response.ok` is the agent's
  **claim**; what happened is in the upstream's state. Sets
  `runs_own_retry_loop = True`, which turns caller-strategy metrics
  (`retry_amplification`) back on for the first time.
- **`Target.injects_out_of_process`** — a class attribute, default `False`, that
  `FaultInjector.run` branches on. No abstract member was added, so no
  third-party subclass breaks.
- **`FaultConfig.schedule` / `FaultConfig.task_id`** — an optional forced fault
  table keyed `(task_id, tool, ordinal)`, consulted at the top of
  `_choose_fault`. `None` for every scan that existed before, so no recorded
  seeded draw moves.
- **`agent_baseline` probe** — runs each task once with no faults, refuses if a
  task cannot complete, and reports the clean-path call count per task, which is
  the denominator retry amplification needs when the retrying is the target's.
- **Fixture agents** (`tests/fixtures/agents/`): `careful`, `blind`,
  `optimistic`, and one deliberately timeout-less variant for the hang test.
  None imports `ratemyagent`.
- **`event_twin_mcp_server.py` accepts `idempotency_key`** in `--mode append`,
  absorbing a repeat and recording `applied` / `absorbed` in its ledger. Mode
  `put` and every existing flag are unchanged.

- **Per-task effect oracle on `--target agent`** — `--verify-tool` is accepted and reads
  the upstream before and after each task on a connection of the scan's own. Tasks run
  strictly one at a time; `AgentTarget` refuses a second in flight.
- **Agent metrics** — `duplicate_mutations` (scored, per task window),
  `retry_amplification` (scored, over the clean-path call count), and, unscored:
  `unsupported_claims`, `lost_effects`, `lost_acknowledgements`, `backoff_shape`,
  `retry_after_honored`. Printed in an "Agent behavior (experimental)" block on the
  scorecard.
- **Agent verdict rule** — an agent scan is judged on behaviour, and only with
  `--verify-tool`, every task's window read, and at least one task with a call whose
  outcome the agent could not know (`uncertain_tasks`, with the ids in
  `uncertain_task_ids`). Otherwise `NO VERDICT`
  with the reason, and `ci` exits 2 (still writing `--json-out`).
- **The baseline checks the oracle** — on a clean task the agent completed with a
  success reply, the verify tool must see exactly `expected_effects`; otherwise the scan
  refuses (exit 2), because the upstream's state is not visible outside its process.
- **`ci --target agent`**, with the same agent flags as `scan`.
- `FaultInjector(schedule=...)` for an explicit forced table.

### Changed

- A re-sent call is attributed to whoever sent it, in the findings and in the summary
  line. Against a service the retry loop is the scanner's; against an agent it is the
  target's.
- **A refused connection reaches an agent as an immediate tool error** marked
  `executed: false`. Timeouts and lost responses stay silent.
- `recovery_rate` is reported and not scored on an agent target, and no recovery floor is
  derived: the retry budget is the agent's.
- `duplicate_opportunities` keeps its shipped meaning (acknowledged deliveries whose reply
  the caller did not see) and appears on server scans only. An agent scan does not export
  it; its count is `uncertain_tasks`.

### Fixed (in the unreleased C1 plumbing)

- The baseline and chaos passes wrote to one record file per task, so the clean call
  became attempt 1 of every chaos trajectory and the chaos schedule started one call late.
  Records, configs and schedules are now per pass.
- The fixture agents' per-read reader thread outlived a timeout and consumed the next
  reply, turning every lost reply into two apparent timeouts.
- The careful fixture derived its idempotency key from the task id alone, so its chaos
  pass was absorbed as a repeat of the baseline and applied nothing.
