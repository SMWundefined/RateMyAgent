# Changelog

Release notes live on [GitHub releases](https://github.com/SMWundefined/RateMyAgent/releases);
this file records what is in the tree and not yet released.

## 1.9.0 — 2026-09-30: your agent, your database, 15 minutes

A **minor** release for the agent path, which is outside the API freeze. It adds two
flags, a runner for each of two agent stacks, and the first README that leads with the
agent path. **Nothing frozen moves**: exit codes, `ScanResult.passed`, the score, the
eleven metric names and every seeded draw are unchanged. Service-path output is
unchanged. Agent-path output changes in the ways stated below.

1.8.0 was committed but not published separately; its changes ship in 1.9.0.

### Added

- **The walkthrough**, first in the README and in `docs/SCANNING.md`. It takes a
  stranger's own agent and database-backed MCP server to an agent-path verdict in six
  steps, with a table of every `NO VERDICT` reason and what to do about it.
- **`--verify-command CMD`** (`scan`, `ci`; `--target agent` only). It is an effect
  oracle for a store no MCP tool reads. It runs as `/bin/sh -c` on the string as typed,
  with the scan's whole environment, stdin `/dev/null`, and its own process group, which
  a timeout kills. The read must exit 0 and print a non-negative integer (a count) or a
  JSON list (the entries); with `--verify-count`, an object holding one at that path.
  **Anything else is a failed read, never a zero**, and empty output is not `[]`. At
  setup the command runs twice and refuses (exit 2) when a read fails, printing the exit
  code, the last 500 bytes of stderr and the first stdout line. It also refuses when the
  two readings differ, which is the stand-in for a read-only check a command cannot have.
  A mid-scan failure is the task's `failed` window, which means NO VERDICT. It is
  mutually exclusive with `--verify-tool`, and `--verify-args` is refused beside it. A
  list feeds `expected_entries` unchanged. The export records
  `verify_command_digest` (first word and a sha256 prefix), never the text.
- **`--key-path PATH`** (`scan`, `ci`; `--target agent` only) and **`retry_keys`** on the
  behaviour probe. The proxy reads the retry key at a dotted path (default
  `idempotency_key`; only a string counts) and records it in the row's
  `idempotency_key` field. It also records **`operation_fingerprint`**, the call's
  fingerprint with the key removed. `retry_keys` classifies every retry that reached the
  proxy against its operation's first call: `kept`, `changed` or `no_key`. It is
  `null` ("not read") when no call in the scan carried a key, the clean pass included.
  It is **report only** and printed as `retry keys (reached the proxy)`; a zero prints
  `no retry reached the proxy`. Under
  `--repeats` it is summed over the runs, never averaged. The path travels to the proxy
  as the schedule file's `key_path`, written only when the flag is given.
  `trajectory_id` is unchanged.
- **The `tools_list` record row and `key_path_declared`** on the behaviour probe. The
  proxy records the `tools/list` listing it served, once per record, as
  `{"kind": "tools_list", "tools": [{"name", "inputSchema"}]}`. `key_path_declared`
  reads it for each tool a recorded call named: `declared` when the schema has a
  property at every segment of the key path, `absent` when it does not, `null` when no
  listing was seen (an agent that never listed, or a record from before this row). It
  is read off the schema only; no property name is taken as key-like. When no call sent
  a key, the `retry keys` line now says why from it: `not read: create_order takes a
  key at idempotency_key; no call sent one` (no `--key-path` hint), or `not read:
  create_order takes no key at options.key, so its retries cannot be deduplicated by
  key; pass --key-path if it takes one elsewhere`. With no listing, the text is as
  before. Report only.
- **`suggested_seed`** on the fault probe. When no run had an uncertain task, it is the
  first seed above `--seed` (up to 10,000) whose forced schedule drops the reply to
  some task's first clean-path call. It uses the same draw that builds the schedule,
  and the NO VERDICT reason names it: `...; raise --fault-rate, or --seed 1431 drops the
  reply to t1's first create_order call.` It is a message, never a behaviour.
- **`examples/runners/run_openai_agents.py` and `run_langgraph.py`**, derived from the
  Gate S runners. You edit one function. Each has a PEP 723 block pinned to Gate S's
  versions, and neither imports `ratemyagent`. **`--scripted`** runs either with a
  scripted model and no key: `agents.testing.ScriptedModel`, or a `langchain_core`
  fake chat model whose `bind_tools` returns itself. It never builds a real model.
- **CI job `example-runners`**, one leg per runner, at $0. A key in the environment fails
  the job. Each leg runs Gate S's task, seed and close (with an `idempotency_key` added)
  and asserts exit 0, a non-empty record, a parsed claim, `lost_acknowledgements == 1`,
  no retry reaching the proxy, and `PASS, UNRECONCILED`.
- `tests/fixtures/orders_mcp_server.py`, the walkthrough's server: `create_order` writes
  into a stdlib `sqlite3` file.

### Changed

- **`PASS, UNRECONCILED`.** When an agent scan passes and `lost_acknowledgements` or
  `unsupported_claims` is nonzero in any run, the verdict line reads `PASS, UNRECONCILED:
  score N meets pass threshold 75, but the agent's claims and the upstream's state
  disagree: lost acknowledgements 1 (t1).` The second line explains the two readings.
  `ci` prints `PASS, UNRECONCILED  score ...; lost acknowledgements 1 (t1)` and **still
  exits 0**. `passed` and the score are unchanged, and a FAIL or NO VERDICT is never
  relabelled. Re-rendered from the shipped Gate S exports, all ten SDK replicates and
  Claude Code replicates 3 and 5 now read `PASS, UNRECONCILED`. The six PASS-absence
  assertions tightened in 1.7.2 now name this form as well.
- **The agent's stderr is logged on every exit without a result line**, at WARNING, as
  its **last** 4,000 characters. That includes a task killed at its deadline, which used
  to log nothing because a cancelled `communicate()` discarded what it had read. The
  refusal that follows (no calls recorded, abandoned, did not complete, or no claim at
  `--claim-path`) quotes the last lines. A run with a result still logs the first 2,000
  characters at INFO, as before.
- **A `--work-dir` that already holds records is refused** (exit 2), naming them. The
  proxy continues a record so that a reconnecting agent keeps its place, which meant a
  second scan into the same directory started its fault schedule where the first
  stopped. Its clean-call counts also included the first scan's calls. Found building
  the walkthrough: step 6's re-run at the suggested seed realized no fault. The default
  work directory is new on every scan and is unaffected.
- **`--agent` goes through the stdio space rule**: an unquoted path with a space that
  exists when rejoined is refused, with the quoted form printed.
- Messages that name the oracle name the one in use: `no --verify-tool or
  --verify-command`, `the verify command did not read the upstream around task t1`, the
  clean-pass persistence refusal, and `ci`'s `NOT MEASURED` line.
- `repeat_by_group` entries gain `values`, every run's value per metric, which
  `PASS, UNRECONCILED` reads.
- The README's Gate D, BD and S narrative moved, verbatim, into each gate's
  `examples/*/README.md`. The README keeps the comparison table and links.
- **The walkthrough's step 6 changes the payload as well as the work dir**
  (`rma-probe-1` to `rma-probe-2`). `app.db` keeps step 5's rows, and a server that
  honours idempotency keys absorbs a key the agent reuses from the first scan, so an
  agent that builds its key from the prompt had its re-run's clean pass apply nothing,
  and the scan refused it as a persistence problem. The named seed still applies: it
  places the fault by task, tool and call number, not by content.
- **The clean-pass persistence refusal names key reuse across scans as a possible
  cause** when a task applied nothing and its record shows a key on a call answered
  with success. It is worded as a possibility from what the record holds, and printed
  beside the persistence advice, never instead of it. The refusal still exits 2.
- **The NO VERDICT table's "applied nothing" row states a limit instead of advising a
  loop.** It said to change the payload again, and for an agent that derives its key
  from the task's content that yields the same NO VERDICT: the new scan's chaos pass
  re-sends the key its own clean pass stored. The row now reads: an agent that derives
  its key from the task's content is absorbed by a key-honouring server on the chaos
  pass, so this walkthrough cannot measure it; this is a known limit (state isolation),
  not a fault in the agent. `docs/LIMITATIONS.md` says the same.
- **`testpaths = ["tests"]`**, so a bare `pytest` from the repository root collects the
  suite and nothing else. An unpacked sdist under `assets/` brought its own `tests/` and
  stopped collection.

### Shipped gate evidence: archived, not regenerated

`examples/phase-d-gate/`, `examples/gate-bd/` and `examples/gate-s/` keep their evidence
exactly as shipped. Their READMEs gain the prose that left the top-level README. Read by
1.9.0, with nothing re-run:

- **Gate S's verdicts.** The ten SDK replicates and Claude Code replicates 3 and 5 render
  `PASS, UNRECONCILED` (`lost acknowledgements 1 (t1)`). Claude Code 2 stays `PASS`, and 1
  and 4 stay `FAIL`. All fifteen re-score to the exported `passed` and score.
- **`retry_keys` on the Tier 0 RUN-LIVE records.** Those rows predate
  `operation_fingerprint`, so they group on `fingerprint`. The five replicates, summed as
  the runs of one scan, give `kept 1, changed 0, no key 4`, the count RUN-LIVE §3 reports.

### Unchanged, stated

- Exit codes, `ScanResult.passed`, the score, the eleven metric names, `ALL_FAULTS`, and
  every seeded draw and forced schedule.
- The service path: `examples/mock-failing.*`, regenerated from its documented command,
  differs only in its timestamps.
- `trajectory_id`, and so recovery and amplification grouping. Without `--key-path`, a
  record row's `idempotency_key` holds exactly what 1.8.0 wrote, and the schedule file is
  1.8.0's byte for byte. Every row gains `operation_fingerprint`. A record gains one
  `tools_list` row when the agent lists tools, numbered with the other rows; it is never
  replayed as a call.
- The verdict rule. `PASS, UNRECONCILED` relabels a pass and blocks nothing.
  `suggested_seed` only extends the existing "no task had a call whose outcome was
  unknown" reason.

## 1.8.0 — 2026-09-29: fault counts say what took effect; agent recovery latency reads the wall clock

A **minor** release, for one reason: `Invocation.realized_fault` is a new field on a
frozen result shape (additive, promoted under trigger 1 of `docs/API-STABILITY.md`).
Nothing is removed or renamed, no enum member or injection set moves, so no seeded draw
moves. **No score moves on any target.** Recorded output changes, and every change is
stated below.

### Added

- **`Invocation.realized_fault`** (frozen): the fault that **took effect** on the call.
  `None` when nothing was drawn, and also when a `malformed` or lost-reply fault was
  drawn onto a reply the target had already failed: `FaultProxy` leaves such a reply
  alone, so the fault was on the record and never happened. A rejecting fault (timeout,
  rate limit, server error, refused connection) always takes effect. Set in
  `FaultProxy.invoke`, the one place that sees both the draw and the inner reply, and
  written to every agent-path record row. A test fails if it leaves `to_dict()`.
- **Caveat `recovery_latency_withheld`** (a handle for docs and tests; `Caveat` has no
  label field), `suppress`, on the fault and behaviour probes of an `--agent-kind llm`
  scan.

### Changed

- **Every count of faults injected reads `realized_fault`**, not the draw:
  `injected`, `injected_by_kind` and `injection_rate` on the fault probe (both paths,
  and `runs[]` on the agent path), so the report's fault table and fault-summary line
  and the scorecard's "Faults injected"; `realized_schedule` and `realized_placement`
  (so "Faults realized" and the repeat grouping); and the behaviour probe's
  `injected_faults_by_kind`, `unrecovered_by_fault_kind` and its "most often" finding.
  A realized placement's ordinals still count every call, so no position moves.
- **`Invocation.injected` is documented as the draw.** Its values are unchanged; its
  docstring said "what we did to this call", which was untrue in exactly the case
  above. The draw stays because the schedule consumed it. `Trajectory.injected_faults`
  stays the draw too.
- **`examples/mock-failing.*` regenerated.** The shipped command drew one `malformed`
  onto the failing mock's own timeout (`search#61`, attempt 1): **28 faults injected
  becomes 27**, malformed 4 becomes 3, the injection rate 23.5% becomes 22.7%, and the
  "most often" finding ends "3 after rate_limit" instead of "3 after malformed". The
  score, every check and the recovery table are unchanged. `mock-failing.AGENTS.md`
  changed only in its timestamps.
- **Agent-path recovery latency reads the record's wall clock.**
  `mean_recovery_latency_s` (fault and behaviour probes) and `max_recovery_latency_s`
  (behaviour) are, per trajectory whose first attempt failed and a later one
  succeeded, `replied_at` of the recovery minus `received_at` of the failure. They used
  `started_at`, which counts from the start of the proxy process that served the call —
  and `ratemyagent proxy` starts one per session, so a retry after a reconnect was
  timed on a second clock. **Every non-null agent-path value shipped in `examples/`
  was negative**: 10 exports, both probes, −2.58 s to −8.39 s. Neither is scored, and
  both findings that read the value are gated on `> 5 s`, so the defect printed false
  table cells and never a false finding. On a scripted agent the corrected value can
  now reach the fault probe's "Mean recovery takes …" finding.
- **Withheld against an LLM agent** (`--agent-kind llm`): both keys are `None`, with
  `recovery_latency_withheld`, in the fault probe as well as the behaviour probe (they
  join `LLM_WITHHELD_METRICS`). After a lost reply the gap is the session closing plus
  a model turn, an inference round trip rather than a recovery a retry policy controls.
- **Record replay of a row with no `realized_fault`** — any record written before
  1.8.0 — reads it as its `injected` stamp, the reading every earlier output used, so
  replaying an old record reproduces its old output. It is exact on all shipped
  evidence: under `examples/`, 29 record files hold 28 stamped rows, and every one
  took effect.
- CI (`.github/workflows/test.yml`): both jobs run on `ubuntu-24.04` rather than
  `ubuntu-latest`, and each ends with a step that fails the job if it used more than
  80% of its `timeout-minutes`.
- `pytest-xdist` is added to the `[dev]` extra for local use (`pytest -n 4 --dist
  loadscope`). **CI still runs the suite serially**: parallel runs flaked 2 in 7
  locally, and stay out of CI until the agent's stderr is logged when a task is
  killed at its deadline, so the next such failure can be diagnosed.
- Test fixtures: the scripted agents' `initialize` handshake reads under its own
  `HANDSHAKE_TIMEOUT_S` (10 s) rather than the agent's tool-call read timeout
  (3 s). A proxy start slower than 3 s on a loaded machine timed the handshake
  out, the agent exited before its first call, and the scan reported a task with no
  calls. Tool-call reads are unchanged.

### Shipped gate evidence: archived, not regenerated

`examples/phase-d-gate/`, `examples/gate-s/` and `examples/gate-bd/` are left exactly
as shipped, **negative recovery latencies included**. Re-scanned under 1.8.0 they
would show:

- the 10 negative `mean_recovery_latency_s` values (and the behaviour probe's max)
  **`null`, with `recovery_latency_withheld`**: every one of those scans is
  `agent_kind: llm`;
- fault counts and realized placements **unchanged**: every stamped fault in those
  records took effect (no lost reply was delivered anyway, and none is `malformed`);
- scores and verdicts unchanged. Every task there is x = 1, so 1.7.5's multi-write
  rules do not reach it either.

### Unchanged, stated

- `recovery_rate`, `recovery_floor`, `duplicate_mutations` and every other scored
  metric: none reads a fault stamp or a recovery latency.
- The server path's recovery latency (`--target mcp|llm|mock`): one proxy per scan and
  one clock. The five mock profiles export the same JSON as 1.7.5 apart from
  timestamps, the concurrency ramp's `wall_s`, and recovery latencies that differ
  between two runs of 1.7.5 itself; except `failing`, whose fault counts move as above
  whenever a `malformed` lands on one of the mock's own failures.
- `Trajectory.recovery_latency_s` (frozen, not exported) keeps its formula.
- The last Tier 0 strict xfail, `test_realized_placement_names_only_replies_that_were_lost`,
  passes and is a plain test. No strict xfail remains.

## 1.7.5 — 2026-09-28: multi-write tasks are read per entry, or get no verdict

A patch, on the agent path only, which is outside the API freeze. **Changes recorded
output and one metric's meaning** on agent scans; every change is stated below. A
service scan (`--target mcp|llm|mock`) is unchanged: the five mock profiles export the
same JSON as 1.7.4 apart from timestamps and two wall-clock readings (the concurrency
ramp's `wall_s` and the recovery latencies), which differ between two 1.7.4 runs by the
same amount.

Found by Tier 0 (`tests/test_agent_gate.py`, the multi-write arms): an agent task's
effects are counted as one net number per task window, `after - before`. At two or
more writes per task a duplicated write and a write that never landed cancel inside
it, **with one fault** — a re-send of write 1 followed by a skipped write 2 reads as
clean. A no-key agent scored PASS 100 with three duplicates in the ledger.

### Added

- **`expected_entries`, optional, per task in the task file.** The tokens the task's
  effects should show in the verify tool's entries, one per effect (a list read as a
  multiset; its length must equal `expected_effects`). A token matches an entry by
  containment in the entry's JSON, the rule `{op_id}` already uses, so the loader
  refuses a file where one token is a proper substring of another. Absent means the
  1.7.4 reading, unchanged.
- **Per-entry readings** where a task declares its entries and the verify tool
  returns them. `duplicate_mutations` sums, per entry, the applications over the
  declared count; **`missing_writes`** (`missing_writes_by_task`) sums the
  shortfall. The two are never netted against each other. The fault probe's task row
  gains `effects_by_entry` (`{token: count}`), `expected_entries` and a per-task
  `unmatched_effects`, on declared tasks only. `effect_attribution` reads
  **`task_entry`** when every multi-write task was read per entry.
- **Two agent verdict blockers**, after the existing ones (so "raise --fault-rate"
  still comes first), exit 2 through `ci` as every blocker does. Message text only;
  the task lists are metric keys:
  - `[undeclared_multi_write]`: a task with `expected_effects >= 2` declares no
    entries (`undeclared_task_ids`);
  - `[entries_unreadable]`: a declared task whose window could not be read per entry
    (`entries_unreadable_task_ids`): the verify tool returned a count, **or** an
    applied effect matched no declared token (`unmatched_effects > 0`) — which is
    what a retry with rewritten arguments produces, a real duplicate that per-entry
    matching would otherwise drop — **or** one applied entry matched two or more
    declared tokens, which no rule can attribute without guessing. The read itself
    succeeded, so the task's oracle status stays `ok` and its row carries no
    `effects_by_entry`.
  - Either way the task's readings fall back to the net count, kept **as a lower
    bound and still scored**, so a duplicate it does see still caps the score; a
    caveat says so (handle `duplicates_lower_bound`).
- **`partially_applied_tasks`** / `partially_applied_by_task`: tasks where some writes
  landed and some did not (`0 < E < x` on the net path; per entry, a missing entry
  beside a landed one, which also catches a netted task). **Unscored, and no verdict
  reads it.** A finding and the scorecard's per-task line report it. A declared task
  whose writes an agent's own shared key absorbed, claimed ok on ok replies, still
  scores **PASS 100** with its missing writes printed.
- `missing_writes` and `partially_applied_tasks` join the repeat ranges (unscored,
  so a range and never a worst run).
- `multi_write_agent.py --deviation resend-then-skip|double-then-skip|resend-mutated`
  (test fixture), and every task in `tasks-multi-write.json` now declares
  `expected_entries`.

### Changed

- **`unsupported_claims` counts more on a declared task (meaning change).** It used
  to count a success claim over a record with no successful reply. On a task read per
  entry it now also counts a success claim when a declared entry is short
  (`c_t < x_t`): the agent's successful replies were for what it sent, and support
  nothing about what it did not send. Still unscored. Undeclared and x = 1 tasks
  read exactly as before. The finding names which rule each task met.
- **`lost_acknowledgements` is per entry at two or more effects.** A declared task
  counts only when every entry landed exactly as declared. An undeclared or
  unreadable multi-write task is **left out** (a net equality is also a duplicate
  beside a missing write), and a finding says which tasks were left out.
- **The clean pass is checked per entry** on declared tasks: a clean run that writes
  one entry twice and skips another has `E == x` and used to pass; it now refuses.
- **The clean-pass refusal reads the record before it names a cause.** When one
  `idempotency_key` was sent on distinct writes, all answered ok, the refusal says the
  agent's key absorbed its own writes and drops the persistence advice; a key repeated
  on identical calls is a retry and is not called reuse. Only the argument named
  `idempotency_key` is visible to the proxy. Other tasks keep the 1.7.4 wording, and a
  mixed set names each cause by task. Still exit 2.
- The per-entry window caveat says "per declared entry"; the net caveat now says that
  at two or more writes which *write* is not visible either.

### Unchanged, stated

- **Shipped gate evidence is unaffected.** Every task in `examples/phase-d-gate/`,
  `gate-s/` and `gate-bd/` is x = 1, so neither blocker, the per-entry readings nor
  the `unsupported_claims` change can reach it. Archived, not regenerated.
- `duplicate_mutations` keeps its name, policy key and absolute cap. No threshold,
  weight or cap moves; no frozen name, value or enum member changes.
- Fault stamps (`injected`, `realized_placement`) and agent recovery latency are 1.8.0,
  not here. The strict xfail `test_realized_placement_names_only_replies_that_were_lost`
  stays.

## 1.7.4 — 2026-09-25: the "no calls recorded" refusal reads the record before it gives advice

**Changes recorded output**, which is why it is its own release: the text of one
refusal. What is refused, and when, is unchanged. The exit code is still 2, and the
message still starts `no calls recorded for task …`.

### Changed

- **The refusal for a task with no call on its record now says what the record
  holds, and names a cause only when the disk supports it.** It used to say the record
  "is empty or missing" and point at the config's `env` block whatever was on disk.
  In Gate BD's 2026-09-24 replication it refused on a chaos record holding a
  `notifications/initialized` row. The proxy wrote that row, so the record path had
  demonstrably arrived. The message still called the file empty and blamed the env
  block. `RecordWriter` creates a record on its first row and the scan never creates
  one, so the record itself can prove the path arrived. One helper,
  `explain_unrecorded` in `ratemyagent/proxy.py`, now serves both refusal sites (the
  clean pass and the fault pass):
  - **missing**: `there is no record at …`. The env block is advised, **unless**
    another pass's record for the same task holds rows, in which case the same config
    template carried the path and the env block is ruled out.
  - **rows, none a call**: `holds no tool calls -- 1 row(s): notifications/initialized
    --`. The proxy wrote them, so this is not the env block. The agent connected and
    made no tool call. **No env-block advice.**
  - **present, no readable row**: zero bytes leaves the env block as one candidate
    among several, and says so. Bytes without a readable row mean a proxy with the
    path was cut off mid-write. Another pass's rows rule the env block out in either
    case.
  - Every branch states the rows found and never says "empty or missing". When the
    scan holds the agent's `Response`, the message carries the agent's own account:
    `The agent exited 1 and reported failure: …`, or that it was killed at the
    deadline.
- The agent target's no-result-line `Response` now carries `exit_code` in `meta`, as the
  claimed path already did, so the refusal can report how the agent ended.

### Added

- `tests/test_unrecorded_refusal.py`: every branch, asserted on the text the user reads,
  including the replication's case end to end through the CLI and the clean-pass site
  through the probe. The predicate that detects env-block advice is shown to fire on
  the 1.7.3 message verbatim, so the suppression assertions cannot pass vacuously. A
  mutation that restores the env advice in the rows-no-calls branch fails three of
  them.
- `tests/fixtures/agents/quitter_agent.py`: connects, makes no tool call, and reports
  failure in a chosen pass. It is the replication's replicate 2 as a fixture.

## 1.7.3 — 2026-09-24: the Gate S evidence ships, and the twin shows the agent one tool

A patch. **No change to the package's behaviour or output.** One behaviour change to a
test fixture, stated below, plus evidence, checkers and docs.

### Added

- **`examples/gate-s/`** — Gate S: one model (`claude-haiku-4-5`), one task, one fault, one
  credential, three agent stacks (Claude Code 2.1.281, OpenAI Agents SDK 0.22.3, LangGraph
  1.2.12), five replicates each. **A retry reached the upstream in 3 of 5 Claude Code
  replicates and 0 of 5 in each SDK arm.** The README leads with the mechanism, and the
  exact tests come second, because the count alone is weak:
  - The SDK zeros are properties of the code at these versions and defaults, read from
    source and confirmed without a model. The OpenAI Agents SDK never reopens its one stdio
    session, so **no retry reaches the upstream, unconditionally**; that the model is never
    asked again additionally depends on `cache_tools_list` at its default and the run's
    120 s read-deadline override. LangGraph's default `ToolNode` re-raises the transport
    error.
  - 3/5 against either SDK arm's 0/5 is **p = 0.17**; pooled, **p = 0.022**.
  - Both SDK arms scored 100/100 on every replicate **by never retrying**, and the README
    says that is not care.
  - Shipped: per-replicate twin ledgers and state, scan exports **as captured** (absolute
    paths included, so the hashes attest to the files the runs wrote), chaos records, and
    each stack's own per-pass sidecar. Also the twin as each run saw it, the task file, and
    the sha256 of every ledger and state file. 171 files, about 540 KB, in the sdist
    only (+72 KB compressed); nothing in the wheel.
  - **`confounded/`** — the first Claude Code run, unscored. The twin then advertised its
    read tools to the agent, and one arm saw a surface the others did not (see Changed).
  - **`runners/`** — the scripts that drove the three stacks, shipped as they ran, **to
    re-run and not to re-check**. The claim is about third-party code, and a reader who
    cannot see the runner cannot tell whether a zero is the stack's or the harness's.
- **Two stdlib-only checkers**, importing nothing from this package.
  `verify_gate_s.py` re-derives every duplicate count from the twin's ledgers and
  reconciles each SDK replicate against the runner's own model-request count: an
  invocation count is blind when the transport dies, so one upstream call is not read as
  "no retry" unless the model was demonstrably not asked again. `fisher_contrasts.py`
  recomputes the exact tests, and **exits 1 if any published count or p-value differs
  from what the evidence gives**.
- **CI**: all three checker invocations run in `verification-tools`, before the existing
  clean-tree assertion.
- **`tests/test_gate_s_checker.py`** — the checker's sixteen seeded cases, eight of which
  must exit 1, each asserted by exit code **and** reason. Also the shipped evidence, the
  confounded run, the exact tests, and the README's hash table against the files. "Known
  to fail" now runs in CI rather than on one machine.
- **`tests/test_twin_tool_surface.py`** — the twin's tool surface per role, with a
  deliberate failing case: the check must fail against a copy of the twin with the fix
  reverted.

### Changed

- **The event twin's agent copy advertises `event` only** (`tests/fixtures/`). Under
  `--role oracle`, all three tools, as before. **Advertising only:** a read tool called by
  name is still served, and still never advances the operation boundary from the agent's
  copy. The read tools exist for the scan's own oracle, and the agent never needs them.
  Advertising them confounded Gate S: two SDK runners filtered the agent's tools to
  `event`, but Claude Code's `--tools ""` removes only its built-ins, so one arm's model
  saw both reads and was denied them at use time, in 4 of 5 chaos runs, after a lost reply.
  **Unconditional, with no flag.** An experiment that needs the agent to *see* reads gets a
  flag written for it, whose semantics also cover permitting them. The full suite passes
  unchanged under the fix. `chatty_agent` still calls `effects` by name, a realism gap
  noted rather than fixed here.
- `pyproject.toml`: `E501` is ignored for `examples/gate-s/runners/*` only, because they
  are shipped byte-for-byte as they ran.
- README and `docs/LIMITATIONS.md` cite Gate S beside Gates D and BD, with its limits:
  mechanisms at these versions rather than rates, a weak test at five replicates, a blind
  invocation count, no cross-stack key comparison, and no number carried into Gate BD.

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
