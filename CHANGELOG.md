# Changelog

Release notes live on [GitHub releases](https://github.com/SMWundefined/RateMyAgent/releases);
this file records what is in the tree and not yet released.

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
