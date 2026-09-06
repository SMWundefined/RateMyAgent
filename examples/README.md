# Examples

What RateMyAgent's output actually looks like, without installing anything.

| File | What it is |
|---|---|
| [`mcp-server-git.AGENTS.md`](mcp-server-git.AGENTS.md) | Fix guide for a **real, published MCP server** |
| [`mcp-server-git.report.md`](mcp-server-git.report.md) | The full report for that scan |
| [`mock-failing.AGENTS.md`](mock-failing.AGENTS.md) | Fix guide for a deliberately broken target |
| [`mock-failing.report.md`](mock-failing.report.md) | The full report for that scan |
| [`scan_mcp_example.py`](scan_mcp_example.py) | Driving a scan from Python instead of the CLI |
| [`mcp_server_git_repro.py`](mcp_server_git_repro.py) | Standalone probe of the reported `mcp-server-git` crash, using only the MCP SDK |

## The real one

`mcp-server-git` is the official Git MCP server, installed by a lot of people. The scan
saved here scores it **99/100**: `18 edge cases across 3 tools: 15 rejected (9 cleanly,
6 unclassified), 3 accepted, 0 crashed`.

An earlier version of this file reported that half those edge cases crashed the stdio
transport, and said so upstream. That was our bug, not theirs. The record below is kept
deliberately, because how the mistake was made is the more useful example -- and the
"6 unclassified" in the line above is the residue of it, now reported honestly instead
of scored as a crash.

<!-- KEEP: the permanent record of the retraction, not a banner to remove -->
> **Correction (2026-09-05): the crash is ours, not theirs.**
> [`mcp_server_git_repro.py`](mcp_server_git_repro.py) sends the same 18 payloads to the
> same server version using only the MCP SDK, and the stdio transport never dies. All 18
> come back as a well-formed `CallToolResult` with `isError=True`, and a known-good call
> on the same session succeeds after every one of them. The three cases we grade as
> crashes are the three whose rejection message reads `Repository path '...' is outside
> the allowed repository` — text containing none of the substrings
> `_classify_tool_error()` matches, so it falls through to `ErrorKind.UNKNOWN`, which
> `contract.py` counts as a transport crash. Run the script yourself; it exits non-zero
> only if something really does crash.
>
> `@modelcontextprotocol/server-filesystem` 0.2.0 was checked the same way and is the same
> artifact — 18 answered, 0 crashed, session alive throughout. Its unrecognised messages
> are `EISDIR: illegal operation on a directory`, `ENAMETOOLONG: name too long` and
> `ENOENT: no such file or directory`; its recognised ones say `Input validation error`.
> **Resolved in 0.1.4.** Crash detection no longer reads error text: it reads whether the
> target answered at all. Both servers now score 0 crashes, all nine scanned servers were
> re-run side by side, and
> [#4754](https://github.com/modelcontextprotocol/servers/issues/4754) was retracted and
> closed. The rejections we could not attribute to a cause are reported as
> "unclassified" rather than counted against the target.

```bash
# Reproduce it against any repository
ratemyagent scan --target mcp \
  --uri "stdio://uvx mcp-server-git --repository /path/to/repo" \
  --tool git_log --tool-args '{"repo_path": "/path/to/repo"}' \
  --requests 20 --fault-rate 0.3 --seed 42 --output all

# ...and check the crash claim without RateMyAgent in the loop (needs only `pip install mcp`)
python examples/mcp_server_git_repro.py
```

Reported upstream as
[modelcontextprotocol/servers#4754](https://github.com/modelcontextprotocol/servers/issues/4754),
which the correction above retracts. The count varies with the repository you point it at
— 6 of 18 against the repo the server was started in, 9 of 18 against any other — and the
repro script explains why: `repo_path: ""` resolves to `.`, which lands inside the allowed
root in the first case and outside it in the second.

Note what the `--tool-args` are doing. Without them the scanner synthesizes arguments from
the JSON Schema, which are structurally valid but semantically meaningless, and
`mcp-server-git` rejects every one — the same server scores **29/100**. Pass real
arguments before believing a score.

## The synthetic one

`mock-failing` is the built-in `failing` profile: slow, drops requests, crashes on
malformed input, recovers badly. It exists to exercise every finding at once, and it needs
no network, no API key and no server.

```bash
ratemyagent scan --target mock --profile failing --requests 40 --concurrency 16 \
    --fault-rate 0.3 --seed 42 --output all \
    --report-out examples/mock-failing.report.md \
    --agents-md-out examples/mock-failing.AGENTS.md
```

That reproduces it exactly: the mock is deterministic and fault injection is seeded per
operation and attempt, so `--seed 42` replays the same run. Only the timestamp in the
header and `generated_at` in the state block differ.

## Reading them

`AGENTS.md` opens with the verdict and where the score went, then lists what to fix in
severity order — duplicate mutations and crashes before latency and cost. Each section
states what was observed, why it matters in production, the likely root cause, and a fix
you can paste.

Re-running against a *fixed* version of the same target adds a "Since the last scan"
section reporting what moved. The `<!-- ratemyagent-state -->` block at the bottom is what
makes that possible — leave it in place.

`report.md` is the whole scan: actual-vs-target, the score breakdown, then every phase with
its metrics, per-level concurrency numbers, and all findings.
