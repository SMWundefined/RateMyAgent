# Phase D: one real agent, scanned

The evidence behind the two agent findings in the top-level README. Everything here
is output from a single pair of scans against **`claude-haiku-4-5` driven by Claude
Code 2.1.275**, launched per task through the MCP config it already reads.

| File | What it is |
|---|---|
| [`verify_independent.py`](verify_independent.py) | Re-derives the duplicate from the server's own ledger. **Imports nothing from `ratemyagent`**, stdlib only |
| [`claude-haiku-fault.scorecard.txt`](claude-haiku-fault.scorecard.txt) | The fault-pass scorecard, as printed |
| [`twin-ledger.jsonl`](twin-ledger.jsonl) | The event twin's own account of every call it handled, and whether it changed state |
| [`twin-state.jsonl`](twin-state.jsonl) | What the twin actually stored |
| [`record-baseline-t1.jsonl`](record-baseline-t1.jsonl) | What crossed the proxy on the clean pass |
| [`record-chaos-t1.jsonl`](record-chaos-t1.jsonl) | What crossed the proxy under the fault |
| [`tasks.json`](tasks.json) | The one task, with its exact `expected_effects` |

```bash
python3 examples/phase-d/verify_independent.py
```

No dependencies, no network, no MCP server. It exits 1 and says why if the ledger and
the scorecard disagree.

## What happened

The task was to record one event, exactly once. The forced schedule dropped the reply
to the first call to the write tool and then closed the session five seconds later
(`--lost-reply-close-after`).

`record-chaos-t1.jsonl` is four rows and tells the whole story:

1. `notifications/initialized` — the agent connects;
2. `event`, `injected: response_lost_then_closed`, `executed: true`, `replied_at: null`
   — the upstream ran the write, and the reply was thrown away;
3. `notifications/initialized` **again** — the agent noticed the closed session and
   reconnected;
4. `event` again, `idempotency_key: null` — the retry, carrying no key, though row 2
   carried `alpha-spike-1-20260917`.

`twin-ledger.jsonl` shows three applications: one from the clean baseline pass, then
both of the chaos-pass calls. Two effects against an `expected_effects` of 1 is the
duplicate.

## Why this is not a bug report against Claude Code

The agent could not know whether its write had landed — that is what a dropped reply
means, and it is the case the fault exists to create. Retrying is the reasonable move.
What made the retry apply twice is that it arrived with nothing the upstream could use
to recognise it as a repeat, against a tool that appends.

This is the same shape as the two SQLite servers in the top-level README: a plain
insert under at-least-once retry produces a second row, and nobody involved is doing
anything wrong. It is the case worth being able to measure.

## What this does not establish

One agent, one model, one task, four runs. `claude-haiku-4-5` was chosen because the
run was testing plumbing rather than reasoning; **a stronger model may behave
differently** — keep its key across attempts, retry differently, or not retry at all.
Nothing here is a rate.

The other finding in the README — that this agent applied no client-side deadline to a
dropped reply, waiting 234 seconds with no retry and no cancellation — is not
reproducible from these files, because a hang leaves nothing behind but a killed
process. It was measured twice: once by the task deadline expiring during a scan, and
once by a standalone server that held a reply for 90 seconds and released it, which the
agent waited out in full.

## Reproducing it

Needs the Claude Code CLI on `PATH`, an authenticated session, and the event twin from
`tests/fixtures/event_twin_mcp_server.py`. It spends money, or subscription quota.

```bash
ratemyagent scan --target agent \
    --agent claude \
    --agent-command '-p {prompt} --model claude-haiku-4-5 --mcp-config {config}
                     --strict-mcp-config --allowedTools mcp__ratemyagent__event
                     --permission-mode dontAsk --permission-prompts none
                     --output-format json
                     --json-schema "{\"type\":\"object\",\"properties\":{\"ok\":{\"type\":\"boolean\"}},\"required\":[\"ok\"]}"' \
    --claim-path structured_output \
    --tasks examples/phase-d/tasks.json \
    --upstream "stdio://python3 tests/fixtures/event_twin_mcp_server.py --mode append
                --role {role} --state state.jsonl --calls calls.jsonl" \
    --verify-tool effects --verify-count entries \
    --work-dir work --allow-mutating \
    --fault-rate 0.2 --seed 490 --lost-reply-close-after --timeout 240
```

Seed 490 at `--fault-rate 0.2` is what places the dropped reply on the first call to
`event`. The inner quotes in `--json-schema` are escaped because the template is
`shlex`-split before its placeholders are filled; leave them bare and the agent is
handed `{type:object}` and rejects it.

**The agent will not make the same choices twice.** The call count, the idempotency key
and whether it retries at all vary run to run, which is the point of
`docs/LIMITATIONS.md` on agent scans.
