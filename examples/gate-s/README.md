# Gate S — one fault, three agent stacks

**Same model, same task, same fault, same credential; the agent stack is the only thing
that varies.** `claude-haiku-4-5` records one event through the event twin while the
proxy drops the reply to its first call and closes the session five seconds later
(`t1:event#1=response_lost_then_closed`, seed 254). Five replicates per stack:

| stack | version | how it was driven |
|---|---|---|
| Claude Code | 2.1.281 | `--bare`, `--tools ""`, `--strict-mcp-config`, `--allowedTools mcp__ratemyagent__event` |
| OpenAI Agents SDK | 0.22.3 | `MCPServerStdio` + `LitellmModel`, `max_turns=8` |
| LangGraph | 1.2.12 (`langgraph-prebuilt` 1.1.0) | `create_react_agent` + `langchain-mcp-adapters` 0.3.1, `recursion_limit=16` |

`mcp` 1.30.0 in the SDK arms. Scanned with `ratemyagent` 1.7.2.

## The result, and what it rests on

> **At these versions and defaults, Claude Code's client reconnects and re-sends after
> the close — in 3 of 5 replicates here (95% CI 0.23–0.88). Neither SDK client can, by
> construction, and none did in 10 replicates.**

That sentence rests on a **mechanism** first and a count second, in that order, because
the count alone is weak (below). Each SDK arm's zero is a property of its code at these
versions, read from the installed source and confirmed without a model:

- **OpenAI Agents SDK — two claims, of different strength.**
  - **No retry reaches the upstream. Unconditional.** The SDK holds one stdio session per
    `connect()`; nothing in it reopens that session; `MCPServerManager.reconnect()`
    exists and `Runner` never calls it. A retry, if the model issued one, would go into
    the dead session and nowhere else.
  - **The model never gets another turn — conditional on `cache_tools_list` at its SDK
    default (`False`) and on our 120 s read-deadline override** (the default is 5 s,
    which races the proxy's own 5 s close; overridden so every replicate sees the same
    fault). Under both, the tool's `McpError` *is* formatted into a model-visible tool
    result, and the run then dies on turn 2's per-turn `list_tools` against the closed
    session with a bare `anyio.ClosedResourceError` — before the model is called. Every
    OpenAI replicate's claim reads `ClosedResourceError: `, and its sidecar reads
    `requests: 1`. Confirmed with the SDK's own `agents.testing.ScriptedModel` through
    the real `Runner` and proxy, no API call: the formatted tool result was produced
    1 ms before the escape, and the traceback runs `run.py:1484` →
    `server.list_tools` → `send_request` → `ClosedResourceError`.
- **LangGraph.** `ToolNode`'s default `handle_tool_errors` re-raises every exception that
  is not an argument-validation error; the adapter re-raises transport failures by design.
  The `McpError` leaves the graph and the model is never called again. The same default
  handler object is installed by `langchain.agents.create_agent` (langchain 1.4.2), so
  this holds for the non-deprecated entry point too.

The runners are shipped (`runners/`) so a reader can check that no zero above is ours:
each catches only what has already left its stack, and none disables a stack handler.

### Then the exact tests

Two-sided Fisher exact, counts from the twin's ledgers — reproduced by
`fisher_contrasts.py`, which exits 1 if any number here differs from what the shipped
evidence gives:

| contrast | retry reached the upstream | p |
|---|---|---|
| Claude Code vs OpenAI | 3/5 vs 0/5 | **0.1667** |
| Claude Code vs LangGraph | 3/5 vs 0/5 | **0.1667** |
| Claude Code vs both SDK arms pooled | 3/5 vs 0/10 | **0.0220** |
| *confounded run* vs OpenAI / LangGraph | 5/5 vs 0/5 | 0.0079 |
| *confounded run* vs pooled | 5/5 vs 0/10 | 0.0003 |
| *confounded run* vs the arm of record | 5/5 vs 3/5 | 0.4444 |

**Read with the mechanism, not instead of it.** The test treats each SDK arm's 0/5 as a
sample from a rate that could have been non-zero; the source says it could not. Against
either SDK arm alone the contrast is p = 0.17 — five replicates cannot do better — and
only pooling clears 0.05, which treats two arms that reach zero by *different*
mechanisms as one population (defensible for "does a retry reach the upstream", and a
choice). Wilson 95%: Claude Code 3/5 → [0.23, 0.88]; each SDK arm 0/5 → [0, 0.43];
pooled 0/10 → [0, 0.28].

## Per replicate

From the proxy's record, the twin's ledger and each stack's own accounting. Full rows:
`evidence/`.

| replicate | model side | upstream calls | key on each call | twin | claim | score |
|---|---|---|---|---|---|---|
| claude-code-1 | 5 turns | 2 | none · none | applied · applied | ok | 0 FAIL |
| claude-code-2 | 5 turns | 2 | `gate-s-gate-1-event` · same | applied · **absorbed** | ok | 100 PASS |
| claude-code-3 | 4 turns | 1 | none | applied | not ok — *"Unable to confirm if event was recorded exactly once"* | 100 PASS |
| claude-code-4 | 5 turns | 2 | none · `gate-s-gate-1-20260924` | applied · applied | ok | 0 FAIL |
| claude-code-5 | 4 turns | 1 | none | applied | not ok — *"Failed to record event"* | 100 PASS |
| openai-1…5 | 1 request each | 1 | a key each (3 distinct strings) | applied | not ok, `ClosedResourceError` | 100 PASS ×5 |
| langgraph-1…5 | 1 request each | 1 | `gate-s-gate-1` | applied | not ok, `McpError: Connection closed` | 100 PASS ×5 |

All fifteen: one realized placement; `uncertain_tasks 1`; `lost_effects 0`; pre-state 0;
model pin asserted from the response on both passes (`claude-haiku-4-5`, the SDK arms
reporting the dated snapshot `…-20251001`). `permission_denials` empty on all ten Claude
Code passes.

**Every duplicate sits on a replicate where a retry reached the upstream.**
claude-code-2 is the one replicate where a real model kept its key across the retry and
the twin absorbed it.

### Three things this table must not be read as

- **A 100/100 PASS is not care.** Both SDK arms scored 100 on every replicate by never
  retrying: the write landed, the caller reported failure, and
  `lost_acknowledgements: 1` names it every time. claude-code-3 and -5 are the same shape.
- **One upstream call does not by itself mean "did not retry".** When the transport dies,
  the proxy's record goes blind with it; the OpenAI arm could send a retry nothing
  downstream sees. The checker therefore reconciles each SDK replicate against the
  runner's own model-request count: all ten read **`no-second-turn`** (1 request, run
  ended without a final answer), none `retry-unobserved`. Claude Code has no defined
  request count and is printed as *not reconciled*.
- **The key column does not compare stacks.** Retry events per arm: Claude Code **3**
  (kept 1, changed 1, no key either call 1), OpenAI **0**, LangGraph **0**. Gate S was
  not powered for a cross-stack key comparison and makes none.

## What this does not claim

- Anything about "the frameworks": one task, one model, one fault, one pinned version and
  configuration per stack. The OpenAI arm's "no second turn" additionally carries two
  named settings.
- That the read-tool confound (below) raised Claude Code's duplicate count: 5/5 vs 2/5,
  p = 0.17. Not settled; deliberately not pursued, because it measures our configuration.
- Comparability with Gate BD's Claude Code arm: Gate S ran `--bare` and `--tools ""`,
  Gate BD neither. No number is carried between them.
- An independent spend total: the sidecars' own accounting ($0.3134 for the whole gate),
  cross-checked per run against Claude Code's `total_cost_usd`; the provider's usage
  report needs an admin key.

## `confounded/` — the first Claude Code run, unscored

As phase 2 first ran it, before the fix. The twin's *agent* copy advertised its two read
tools (`effects`, `effects_array`); the SDK runners filter to `event`, but Claude Code's
`--tools ""` empties only its built-ins, so its model saw both reads and was **denied
them at use time in 4 of 5 chaos passes** — each time after a lost reply. The arms were
not seeing the same surface, so this run is not the arm of record. It ships as evidence
of one qualitative thing: **after a lost reply, this model reaches for a read tool
unprompted.** It retried in 5 of 5, sent no key on any chaos call, and duplicated in 5
of 5. The fix — the twin's agent copy now advertises `event` only — is in
`tests/fixtures/event_twin_mcp_server.py` and pinned by
`tests/test_twin_tool_surface.py`.

## `runners/` — to re-run, not to re-check

The checkers need nothing in `runners/`. It is there because Gate S's claim is about
three third-party stacks, and a reader who cannot see how each was driven cannot tell
whether a zero is the stack's or the harness's. See `runners/README.md`.

## `twin/`

The twin as each run saw it, so the fixture can change without changing what these
files attest to:

```
fad404e0cdb8837e90480c6e1ff8be710a6537782ad4afab7a732a4f10e48817  twin/event_twin_mcp_server.phase2-as-run.py
e286306431fac3a693778b8c39b79a5458f5786ceadd05bc44bb20aff4b85c7f  twin/event_twin_mcp_server.cc-rerun-fixed.py
```

`phase2-as-run` ran the SDK arms and `confounded/`; `cc-rerun-fixed` ran the Claude Code
arm of record. They differ only in what `tools/list` shows the agent's copy; by AST, the
fixed copy is the same code as the shipped `tests/fixtures/` twin, docstrings aside.

## Re-checking

```bash
python3 examples/gate-s/verify_gate_s.py                  # the arms of record
python3 examples/gate-s/verify_gate_s.py --root examples/gate-s/confounded --stacks claude-code
python3 examples/gate-s/fisher_contrasts.py
```

Stdlib only; neither imports `ratemyagent`. All three run in CI, and the checker's
failure branches — sixteen seeded cases, eight of which must exit 1 — run in the suite
(`tests/test_gate_s_checker.py`).

**What the checker trusts, and where each fact comes from:**

| fact | source | not from |
|---|---|---|
| `expected_effects = 1` | `tasks-gate-s.json`, parsed directly | the scan export |
| which pass each call belongs to | the `generation` the twin stamps on every ledger row | the scanner |
| applied or absorbed, and the key each call carried | the twin's ledger (`calls-*.jsonl`) | the record, the model's claim |
| the state was empty before the run | `pre-state-*.txt`, written before the scan | the scan |
| model requests and the claim, SDK arms | the runner's sidecar | the scan |
| `duplicate_mutations`, `realized_placement` | the scan export — **for comparison only**; nothing is derived from it | — |

The partition is exact: one scan per replicate against a fresh state, so each ledger
holds exactly two generations (clean, chaos), asserted.

**Exports are shipped as captured**, absolute paths included, so the hashes attest to
the files the runs wrote.

## Hashes

Every ledger and state file, as shipped. **Several state files share a hash** —
replicates with the same outcome write byte-identical rows — so a hash proves a file is
unmodified, not which replicate wrote it; the ledgers, which carry pids and timestamps,
are all distinct. `tests/test_gate_s_checker.py` asserts this table against the files.

```
2c364d4651d878be5cd4783f1d8a3e5dfd54c4938773044249062aee9bf4d6af  evidence/calls-claude-code-1.jsonl
e760fa166efdab5cc9864263e9bb6c9aefae0a64d9e9cf96a2c057845b3a7e66  evidence/state-claude-code-1.jsonl
8cef1ef30281ec5a3b634fd672b3c364e86cc93f7551f034518a4311701c6398  evidence/calls-claude-code-2.jsonl
6a29735be105471b9aa2bb36ec84db044a612629f6ba293afacef0e4f2e6bc8e  evidence/state-claude-code-2.jsonl
33e712673593907ad33aa208d7aa24cbd9da58416d38988f6fe21aa2d0aa3dcf  evidence/calls-claude-code-3.jsonl
84ffb87f8d76ede64b7dc0538d6757c9bccc28031c14aad484045282280556f1  evidence/state-claude-code-3.jsonl
aa5b1513e1ecd01b70770b710d0ce31b4778dcf5abdedeff0750816bd68324e8  evidence/calls-claude-code-4.jsonl
aa6879bd01ad20207ac4433c092c22984c16512c562e9f6462d33fb4a5c20bdc  evidence/state-claude-code-4.jsonl
9ea6370bb620b58c8b896ef4b84d5f53179a1c157a1acc1700ceec9cf4508953  evidence/calls-claude-code-5.jsonl
faff7170e691cd2076c8f99e4e078c83730e6bc363ab56f36ba3cc9bdd172e9a  evidence/state-claude-code-5.jsonl
dc49520a510195089f1040ce188079d1d1c677e2d443989ba609fa63bb8851cc  evidence/calls-langgraph-1.jsonl
dd928b806e5106cffbfcf3aa35caafbbeb9bca808b2d269e42d5e64ea779c05a  evidence/state-langgraph-1.jsonl
7147dcd1eab7b3f35afecdbe25cffcb482d285a367ffb4621ca2fef237b27b14  evidence/calls-langgraph-2.jsonl
348722b6e57ad03744f59ff8ac223bfd9bad8aeff8e08967a2bdfa9e77d7b4df  evidence/state-langgraph-2.jsonl
a31278f51b14916af6930e55705e5a8d0d05e1865a501260bf5503c2416155aa  evidence/calls-langgraph-3.jsonl
a1a70737b38e2c7df21625a2108286b3fe24fed21f3cdab59e1b70b619c16a82  evidence/state-langgraph-3.jsonl
d7e5648bf1553377b71b538df73b44347ece8e41ed4323980c6e47f431bc0039  evidence/calls-langgraph-4.jsonl
a1a70737b38e2c7df21625a2108286b3fe24fed21f3cdab59e1b70b619c16a82  evidence/state-langgraph-4.jsonl
c75afb5f4d94edc33069efb8e1b0290ea25c4a0f4bf134e0a21aaa50edddcac6  evidence/calls-langgraph-5.jsonl
a1a70737b38e2c7df21625a2108286b3fe24fed21f3cdab59e1b70b619c16a82  evidence/state-langgraph-5.jsonl
b2caca58930db9178f0171f07faed045e0604a6673eb8a331597f76f35f620cc  evidence/calls-openai-1.jsonl
cbe9a5d5f3af4b949370cd0a9f9774c889ec402e51ab367adb80301bd7f2e48d  evidence/state-openai-1.jsonl
8f5386a554551f579f33325c4149905b563a3b44c387323dd3ba889401b97773  evidence/calls-openai-2.jsonl
1deeef35c841c89e579a07f8dcdd5557ec054fc4cd05580973d2ddd1f32e3cf3  evidence/state-openai-2.jsonl
cb0e4dce92b049f35378e13390e77dc7ea0fd1e92a63d3af5d14d53330a23449  evidence/calls-openai-3.jsonl
a1a70737b38e2c7df21625a2108286b3fe24fed21f3cdab59e1b70b619c16a82  evidence/state-openai-3.jsonl
7ee496763807348effadee421a6b9857ebb574bf973942a9e93abb786dbeaffb  evidence/calls-openai-4.jsonl
a1a70737b38e2c7df21625a2108286b3fe24fed21f3cdab59e1b70b619c16a82  evidence/state-openai-4.jsonl
5d77264825b0959e2a6960bd9e13c2800702bb5421754bece2d84ae89c2da692  evidence/calls-openai-5.jsonl
dd928b806e5106cffbfcf3aa35caafbbeb9bca808b2d269e42d5e64ea779c05a  evidence/state-openai-5.jsonl
b930714f565041576947c3cd005b0405e58199c256bea16e76e0b6435f5b3868  confounded/calls-claude-code-1.jsonl
e760fa166efdab5cc9864263e9bb6c9aefae0a64d9e9cf96a2c057845b3a7e66  confounded/state-claude-code-1.jsonl
ea9e575dfe16c2f52ab43ca8b6968b324e1f7dcc32bdd2df61d3b4c50119606b  confounded/calls-claude-code-2.jsonl
abb1f7ee7870ceff8637ade8e576c90b20894d4f551ee73f849072dfa03e900c  confounded/state-claude-code-2.jsonl
ea5455c3fc2efcd03cce1dbb7aadc45ab92534eed9cbc05fc17026383d8d11aa  confounded/calls-claude-code-3.jsonl
e760fa166efdab5cc9864263e9bb6c9aefae0a64d9e9cf96a2c057845b3a7e66  confounded/state-claude-code-3.jsonl
cd5f4ef6e8eac2cb4e5b22f2f5d00e655a37a06d35293a3def0009aaf4be5f44  confounded/calls-claude-code-4.jsonl
e760fa166efdab5cc9864263e9bb6c9aefae0a64d9e9cf96a2c057845b3a7e66  confounded/state-claude-code-4.jsonl
14a3761926e0eb789be6409d5df316cfd37d220104329abf61dbf5194d738e76  confounded/calls-claude-code-5.jsonl
e760fa166efdab5cc9864263e9bb6c9aefae0a64d9e9cf96a2c057845b3a7e66  confounded/state-claude-code-5.jsonl
```

## Layout

- `evidence/` — the arms of record, per replicate `<stack>-<r>`: `calls-` (twin ledger),
  `state-` (twin state), `pre-state-`, `out-` (scan export, as captured),
  `record-chaos-…-t1.jsonl` (the proxy's chaos-pass record), and `sidecar-…` (the
  runner's per-pass account; for Claude Code, the CLI's own JSON)
- `confounded/` — the same, for the first Claude Code run
- `twin/`, `runners/`, `tasks-gate-s.json` (byte-identical to the copy every replicate ran)
- `verify_gate_s.py`, `fisher_contrasts.py` — the checkers
