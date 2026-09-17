# Changelog

Release notes live on [GitHub releases](https://github.com/SMWundefined/RateMyAgent/releases);
this file records what is in the tree and not yet released.

## Unreleased — Phase C1: agent interposition, plumbing

Nothing here is on PyPI. The version stays `1.4.2` until Phase C2 completes the
feature; these entries are the half of it that exists.

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

### Changed

- A re-sent call is attributed to whoever sent it. Against a service the retry
  loop is the scanner's and the finding still says so; against an agent it is
  the target's.

### Not yet

Scoring, the per-task effect oracle, `backoff_shape`, `retry_after_honoured`,
`false_claim_rate`, and the careful-versus-blind gate. `--verify-tool` is
refused on `--target agent` until the oracle exists, rather than accepted and
quietly not measured.
