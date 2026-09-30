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

## From the project README

Moved here in 1.9.0, verbatim, when the README began leading with the walkthrough. The README keeps the table comparing the three gates.

### Validated on a real agent, over five replicates

**`claude-haiku-4-5` driven by Claude Code 2.1.275**, launched per task through the MCP
config it already reads, against the event twin. Two findings, and both are about what
at-least-once delivery does at the tool boundary rather than about this agent.

**It applied no client-side deadline to a dropped reply.** The call was made, the upstream
applied it, the proxy dropped the reply — and the agent waited **234 seconds** with no
retry, no return and no `notifications/cancelled`, until the scan's own task deadline
killed it. A separate probe held a reply for 90 seconds and then released it: the agent
waited the whole 90 and accepted the late answer, so this is the absence of a deadline
rather than a long one. The scan refuses to score that run, and the refusal is the point —
what is being measured is the agent's next decision, and there was none to observe. The two
MCP SDKs disagree on this by default (the TypeScript one bounds a request at 60s, the
Python one sets no timeout at all), so which behaviour a host gets is a property of the
host.

**Under `--lost-reply-close-after`, it retried without an idempotency key.** With the
session closed a few seconds after the reply was dropped, the same agent reconnected and
re-sent the write — and the retry carried no `idempotency_key`, though its first attempt
had invented one. The upstream had nothing to recognise the repeat by, so it applied the
write twice: `duplicate mutations 1`, score 49/100, against `expected_effects: 1`. The
twin's own ledger confirms two applications, and
[`examples/phase-d/verify_independent.py`](../phase-d/) re-derives the count from
that ledger without importing this package.

**Over five replicates, it applied the write twice in two of them.** The Phase D gate run
put the same agent through five runs of one task — same prompt, same forced schedule, and
**the same realized fault placement in all five**, which is what makes them replicates
rather than five different experiments. It reconnected and retried every time.

```
duplicate mutations    0-1 (n=5); occurred in 2 of 5 runs
```

Both duplicates are confirmed by the **server's own ledger** — two applications inside one
task window against an `expected_effects` of 1 — and re-derived by
[`examples/phase-d-gate/verify_gate.py`](../phase-d-gate/), which imports nothing from
this package and reproduces the per-run counts `[1, 0, 1, 0, 0]`.

**Two distinct failure modes, not one.** In run 3 the retry carried **no idempotency key at
all**, though the first attempt had invented one. In run 1 the retry carried a key, but a
**different** key — which an upstream cannot tell from new work. The first is the failure
this tool was built expecting; the second is the same hazard wearing a disguise, and an
upstream has no more defence against it.

**The model's key derivation is not stable.** It minted a different key on **three of the
five runs**, two decorated with a date and one with a word; an earlier run of the same task
used one constant key throughout. So a key derived from the task is what this agent does
sometimes, not reliably — which is the reason the gate asks for replicates and not for a run.

**One agent, one task, five replicates is not a rate.** Nothing here says how often this
agent duplicates in general, how any other agent behaves, or what this one does on a
different task. What five runs establish is that it happened, that something outside the
instrument confirms it, and that it happened more than once.

**This is what a write retried after an unknown outcome does.** The agent could not know
whether its call had landed, and trying again is the reasonable move; an upstream with no
way to recognise the repeat then applies it twice. Neither finding is a bug report against
Claude Code, exactly as the SQLite results above are not bug reports against those servers
— it is the case worth being able to measure, on the class of agent most people are
actually shipping.

**Eleven runs, one task, one model, across three scans.** `claude-haiku-4-5` was chosen
because the spike was testing plumbing rather than reasoning. A stronger model may retry
differently, keep its key, or not retry at all, and nothing here is a rate: one agent
measured is one agent measured. The evidence is in
[`examples/phase-d-gate/`](../phase-d-gate/), with the earlier single runs in
[`examples/phase-d/`](../phase-d/).

1.6.0 is what Gate D's and Gate BD's findings demanded. `--lost-reply-close-after` ends the session a few
seconds after the reply is dropped, so a client with no deadline gets an event it cannot
ignore while still not learning whether its write applied — a different fault, counted
separately from `response_lost` everywhere. `--agent-command`, `--claim-path` and
`--work-dir` are the rest: the 1.5.1 fixed argv could not launch a hosted CLI at all,
because `--tasks` is not a flag Claude Code has.
