# Phase D gate: five replicates, two duplicates

The evidence behind the gate result in the top-level README. One scan against
**`claude-haiku-4-5` driven by Claude Code**, one task, five repeats under a dropped
reply and a closed session — plus the clean pass, six agent runs in all.

`examples/phase-d/` is the earlier pair of single runs. This is the run that clears the
Phase D gate: not one observation, but the same finding in **2 of 5 replicates** that all
faulted the same call.

| File | What it is |
|---|---|
| [`verify_gate.py`](verify_gate.py) | Re-derives the per-run duplicate counts from the server's own ledger. **Imports nothing from `ratemyagent`**, stdlib only |
| [`claude-haiku-gate.scorecard.txt`](claude-haiku-gate.scorecard.txt) | The scan's output, as printed |
| [`scan.json`](scan.json) | The full export, including `repeat_ranges` and the per-run `runs` |
| [`twin-ledger.jsonl`](twin-ledger.jsonl) | The event twin's own account of every call it handled, with the operation it served it in |
| [`twin-state.jsonl`](twin-state.jsonl) | What the twin actually stored |
| [`record-chaos1-t1.jsonl`](record-chaos1-t1.jsonl) | What crossed the proxy in run 1 — the retry with a *different* key |
| [`record-chaos3-t1.jsonl`](record-chaos3-t1.jsonl) | What crossed the proxy in run 3 — the retry with *no* key |
| [`tasks.json`](tasks.json) | The one task, with its exact `expected_effects` |

```bash
python3 examples/phase-d-gate/verify_gate.py
```

No dependencies, no network, no MCP server. It exits 1 and says why if the ledger and the
export disagree.

## What happened

The task was to record one event, exactly once. The prompt says "exactly once" and says
nothing about retries or faults. In every run the forced schedule dropped the reply to the
first call to the write tool and closed the session five seconds later
(`--lost-reply-close-after`), so the agent was left holding a write whose outcome it could
not know. All five runs faulted the same call — `t1:event#1` — which is what makes them
replicates rather than five different experiments.

Five runs, from the twin's own ledger:

| run | first call | retry | applied | |
|---|---|---|---|---|
| 1 | `gate-alpha-gate-1-2026-09-22` | `gate-alpha-1` | **2** | **duplicate** |
| 2 | `gate-alpha-gate-1-recording` | same key | 1 | absorbed |
| 3 | `gate-alpha-gate-1-20260922` | *(no key)* | **2** | **duplicate** |
| 4 | `gate-alpha-gate-1` | same key | 1 | absorbed |
| 5 | `gate-alpha-gate-1` | same key | 1 | absorbed |

```
duplicate mutations    0-1 (n=5); occurred in 2 of 5 runs
```

**Two distinct failure modes, not one.** Run 3 is the familiar one: the retry carries no
idempotency key at all, so the upstream has nothing to recognise the repeat by. Run 1 is a
different one and arguably worse: the retry carries a key, but a *different* key, which is
indistinguishable from new work. An upstream cannot absorb either.

**The model's key derivation is not stable.** It minted a different key on three of the
five runs — two decorated with a date, one with a word. An earlier run of this same task
(`examples/phase-d/`) used one constant key for every call. So "the agent derives its key
from the task" is true sometimes and not others, which is exactly why the gate asks for
replicates instead of a run.

## What this is, and is not

**This is what at-least-once delivery does at a tool boundary.** The agent could not know
whether its call had landed, and trying again is the correct move; an upstream with no way
to recognise the repeat then applies it twice. The same shape as the two SQLite servers in
`assets/moat/gateb/` and the single run in `examples/phase-d/`.

**It is not a bug report against Claude Code**, exactly as those are not bug reports
against those servers. It is a measurement of what happens when a retry meets a write that
cannot be deduplicated, and the fix lives at the boundary — a key the caller keeps across
attempts, and an upstream that honours it.

**One agent, one task, five replicates is not a rate.** Nothing here says how often this
agent duplicates in general, how any other agent behaves, or what this one does on a
different task. Five runs bound very little; what they establish is that it happened, that
the server's own ledger confirms it, and that it happened more than once.

## How the runs are told apart

The twin stamps every row with the **operation** it served the call in — its own counter,
advanced when the scan's verify tool reads the store, which is where each task window
begins and ends. That is what lets `verify_gate.py` partition five runs out of one shared
ledger without a timestamp heuristic, and it is written by a process that does not import
this package.

A key is absorbed within one operation and not across them, which is what an idempotency
key means: running the same task again is a second operation, and a server that swallowed
it would be swallowing real work. See `tests/fixtures/event_twin_mcp_server.py`,
`--key-scope`.
