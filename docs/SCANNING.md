# Scanning real targets

The detail behind pointing RateMyAgent at a real MCP server: choosing what it calls,
keeping it away from write tools, authenticating, bounding a run, and working with the
generated guides. For the quick version, see the [README](../README.md).

---

## Pass real arguments

Probing invokes a discovered tool for real, once per request. Pass `--tool` and
`--tool-args` to choose which one; the default is the first read-only tool the server
reports.

Without `--tool-args`, arguments are synthesized from the tool's JSON Schema — correct
shape and types, but placeholder values (`"ratemyagent probe"` for an unconstrained
string). A tool that expects a real path, URL or package name will reject all of them.

**Since 1.1.0 the scan refuses rather than scoring that.** One preflight call asks the
server whether the payload is usable; when it is not, the scan stops at setup and exits 2,
naming the tool and quoting the server's own error. Before 1.1.0 it published a composite
instead — `mcp-server-git` scored **43/100 on synthesized arguments and 100/100 on real
ones**, same server, same repository, same command but for the arguments, and the 43 was a
fact about this scanner rather than about the server. `--probes contract` still runs,
because that probe builds its own baseline per tool. See
[LIMITATIONS.md](LIMITATIONS.md#a-synthesized-argument-scan-refuses-rather-than-scoring).

The scanner warns when it detects this, but the fastest way to avoid it is to pass
arguments yourself:

```bash
ratemyagent scan --target mcp --uri "stdio://uvx mcp-server-git" \
  --tool git_log --tool-args '{"repo_path": "/path/to/repo"}'
```

**`--tool-args` reaches the contract probe as of 0.1.14, and did not before.** Edge cases
for the named tool are now built by mutating the arguments you supplied rather than
placeholders invented from the schema — so `wrong_type` asks whether a valid call is
rejected when one field is corrupted, instead of asking it of a call the server was going to
refuse anyway. The other tools in the contract window still use synthesized arguments, and
the report says which used which.

**It also refuses when the arguments would be empty.** Synthesized arguments fill a
schema's required fields, and for an array or a string that can mean `[]` — which satisfies
`required` while asking the server to do nothing. A scan built on that call times an empty
round trip and reports low latency, no errors and full marks, none of it about the tool.
`server-memory` scored 100/100 that way on 20 successful no-ops. Pass `--tool-args` and the
question does not arise. A tool that requires nothing at all is unaffected: `{}` is a
complete payload there, not a missing one.

## Probing writes, unless it knows better

Probing calls a tool for real, once per request, and again under fault injection. Against
a read-only tool that is a measurement. Against a write tool it is a hundred writes.

So **auto-selection only picks a tool it can establish is read-only.** It reads
`readOnlyHint` from the server's own tool annotations, falls back to the tool name, and
refuses when neither settles it — a tool nothing classifies is not thereby safe.

```
refusing to auto-select 'create_entities': it declares readOnlyHint=false.

Probing calls the chosen tool once per request, and again under fault
injection. No tool on this server is known to be read-only, so there is
nothing safe to fall back to.

  tools here: create_entities, create_relations, delete_entities, ...

Choose one yourself, and point the scan at something disposable:
  ratemyagent scan ... --tool <name> --allow-mutating
```

Naming a tool yourself is a decision the scanner will respect, but a tool known to change
state still needs `--allow-mutating` as a second key:

```bash
ratemyagent scan --target mcp --uri ... --tool write_file --allow-mutating
```

Point that at something disposable. Every scan reports which tool it called and with what
arguments, in the scorecard header and in the AGENTS.md state block, so a saved result can
always be traced back to what produced it.

**The same rule covers the contract probe, which it did not until 0.1.11.** Edge-case
probing sends a deliberately malformed payload per declared field, so against a write
tool it is several writes — and for seven releases it took the first three tools a server
listed, whatever they were. Contract probing is now limited to tools known to be read-only,
and says what it left out:

```
18 edge cases across 3 of 9 tools (6 skipped as mutating): 8 rejected cleanly, ...
```

Pass `--allow-mutating` to include them, against a target you can afford to have written to.
It also enables the opt-in `response_lost` fault, which drops a reply the tool has already
produced, so the call is sent again. On its own the scan reports how many calls it re-sent,
as its own, and `duplicate_mutations` stays `n/a`. Add `--verify-tool` to measure what those
re-sends applied.

## Counting what a mutating tool applied

```bash
ratemyagent scan --target mcp --uri "stdio://npx -y @modelcontextprotocol/server-memory" \
    --env MEMORY_FILE_PATH=/tmp/rma-memory.jsonl \
    --tool create_entities --allow-mutating \
    --tool-args '{"entities": [{"name": "{op_id}", "entityType": "probe", "observations": []}]}' \
    --verify-tool read_graph --verify-count entities
```

**`--env MEMORY_FILE_PATH` is not optional here.** Probing a write tool writes, once per
request and again under fault injection, so without it the scan fills the default knowledge
graph with probe entities. Point every mutating example at something disposable.

Three flags, and each is doing one thing:

- **`--verify-tool`** names a **read-only** tool that reports the target's state. The scan
  calls it before and after the retried operations. A tool that is not known to be read-only
  is refused, including one nothing classifies: an oracle that writes would change the number
  it exists to define.
- **`--verify-count`** is a dotted path to the **list** of entries in that tool's result —
  `entities` for `read_graph`. The scan counts entries whose JSON contains an operation's id.
- **`{op_id}`** in `--tool-args` is substituted per operation with a value like
  `rma-9f31c0a84b7e`, derived from `--seed` so a scan still replays exactly. Put it in an
  argument the server stores.

It needs `--allow-mutating` and a `--tool` that changes state; a read-only probe tool is
refused, because a zero there would mean "nothing was asked" rather than "no duplicates".

**What each outcome means.** `duplicate_mutations` counts, per operation, effects beyond the
first; `lost_effects` counts operations the caller saw succeed that applied nothing, and is
reported without being scored. The metric is withheld, with the reason attached, when there
is no oracle, when `--tool-args` carries no `{op_id}`, when the verify call does not answer,
and when **state from a previous run of the same seed is already present** — ids come from
the seed, so re-running an identical command against a persistent target reports `stale`
rather than a pass. Change `--seed` or clear the target's state.

**Since 1.4.1 that collision refuses at setup instead.** The scan reads the verify tool
once at setup, before its own preflight call, asks whether the ids it is about to register
are already there, and **exits 2 without writing anything at all** if they are:

```
error: refusing to scan: 20 of the ids this scan would register are already in the
target's state, so a count of what this run applies cannot be separated from what the
last one did.

  seed 5, already present: rma-14e34105fe3b, rma-1c8770648c2b, ...

Use a different --seed, or clear the target's state.
```

The mid-scan `stale` reading stays as the backstop, for state that arrives after setup.
And a scan that ends in `stale` or `failed` **never prints PASS**, whatever the composite
says: a withheld `duplicate_mutations` lifts the cap it exists to apply, so the verdict
reads `not passed: --verify-tool was requested and did not measure`, the reason prints at
default verbosity, and `ci` exits 2.

Effects that match no operation this scan sent are reported as `unattributed`, never counted,
and they caveat both metrics: something else is writing and the window is not clean.

> **A planned flag multiplies with this one.** `--contract-tools`, on the v1.1 roadmap,
> raises contract coverage above the default three tools. With `--allow-mutating` the two
> multiply rather than compose: `--allow-mutating --contract-tools 12` against
> `@modelcontextprotocol/server-memory` sends garbage to all nine of its tools including
> six write tools, and against `server-filesystem` it reaches `write_file` with a valid
> write and a thousand-character filename. Each flag is reasonable alone; a user setting
> one is unlikely to be thinking about the other. Whatever ships has to make that
> combination loud at the point of use, not in a footnote.

## Transports and credentials

| URI | Transport |
|---|---|
| `https://host/mcp` | Streamable HTTP |
| `stdio://./server.py` | stdio subprocess |
| `sse+https://host/sse` | SSE, deprecated by the 2025-06-18 spec |

Bare `http://` and `https://` mean **Streamable HTTP** as of 0.1.7. They used to mean SSE,
which the spec deprecated and replaced — so the only network transport pointed at the dead
one, and every hosted server failed to connect. SSE still works if you ask for it by name.

Credentials go in headers, repeatable:

```bash
ratemyagent scan --target mcp --uri https://api.example.com/mcp \
    --header 'Authorization: Bearer $TOKEN' \
    --tool search --tool-args '{"query": "hello"}'
```

A stdio server takes credentials through `--env KEY=VALUE`, also repeatable. **The parent
environment is not inherited:** the MCP SDK copies only `HOME`, `LOGNAME`, `PATH`, `SHELL`,
`TERM` and `USER` into the child, so a key exported in your shell never reaches the server,
and it may degrade to an unauthenticated mode without failing.

**Header and env values never reach an artifact.** Reports, JSON exports and the AGENTS.md
state block record header *names* with the values replaced, and strip credentials out of
the URI itself, so a saved scan says whether it was authenticated without saying how. There
is no allowlist of "safe" headers — that judgement only has to be wrong once.

`--header` is http/sse only and `--env` is stdio only; passing either to the wrong transport
raises rather than being ignored.

## The LLM adapter is experimental

`--target llm` builds Anthropic or OpenAI chat completions with `max_retries=0`, so the
scan sees 429s rather than the SDK's retries. **It has never been run against a live
API.** Both SDKs were installed and verified to accept the exact keyword arguments sent,
and response parsing is tested only against fakes shaped like the documented objects.

One known gap sits in that untested path: an OpenAI structured-output refusal puts its
text in `message.refusal` with `content=None` and `finish_reason == "stop"`, which the
adapter records as `ok=True` with empty output — a failure counted clean. Any number from
an LLM scan is unverified until that round trip is run.

The supported targets are `--target mcp` and `--target mock`. The README and the roadmap
are MCP-first for this reason.

## Bounding a scan

**A scan is bounded by wall clock, not just per request.** `--timeout` bounds one request;
it cannot bound a handshake that never completes or a connection that will not close, both
of which sit outside every request. Those stalls hang a scan indefinitely, which in a
pipeline means a job that runs until the runner kills it and tells you nothing.

`--scan-timeout` bounds the whole run and defaults to a generous budget derived from
`--timeout` and `--requests`. On expiry the scan fails cleanly, names the phase and probe it
was in, and **exits 2** — a scan that never finished is not a target that failed:

```
error: scan exceeded its 2400s budget during phase baseline, probe latency and was
abandoned. The per-request --timeout does not bound a handshake or a teardown ...
```

`ci` writes nothing and never prompts. Nothing in the tool does — it stays pipeable.

## Working with AGENTS.md and the report

Re-scanning into the same `AGENTS.md` reports movement. Real output, from re-running the
generator over the `failing` mock's guide with the `degraded` mock:

```
## Since the last scan

- The previous guide was for `failing-mock`, not `degraded-mock` -- the comparisons below are between two different targets.
- Score improved from 32 to 91/100.
- P95 latency improved from 46.44s to 5.21s.
- Error rate improved from 36.7% to 0.0%.
- Sustained concurrency improved from 0 to 5.
- Schema violations regressed from 4 to 9.
- Edge-case crashes improved from 2 to 0.
- Recovery rate improved from 27% to 100%.
- Retry amplification improved from 1.63x to 1.13x.
```

The first line is the point: comparing two different targets is usually a mistake, so the
generator says so rather than presenting the deltas as a like-for-like improvement.

Sections are ordered by severity — crashes and unvalidated input before latency and cost
— so the first thing you read is the thing most worth fixing.

**Check the crash detection yourself:**
[`examples/mcp_server_git_repro.py`](../examples/mcp_server_git_repro.py) sends the same
malformed payloads the contract probe sends, using only the MCP SDK, and makes a
known-good call after each one to prove the session is still alive. It needs
`pip install mcp` and nothing from this project, so you can confirm a reported crash is
real without taking our word for it.

```bash
python examples/mcp_server_git_repro.py                    # a throwaway git repo
python examples/mcp_server_git_repro.py --repository .     # your own
```
