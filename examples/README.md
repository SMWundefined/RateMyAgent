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
6 unclassified), 3 accepted, 0 crashed`. It passes, and the interesting number is the
`3 accepted` -- three inputs its schema forbids that the handler took anyway.

An earlier release scored the same server 91/100 with a 33% "contract crash rate". That
was a bug in this scanner's error classifier, not in the server; it was
[retracted upstream](https://github.com/modelcontextprotocol/servers/issues/4754) and
fixed in 0.1.4. [`mcp_server_git_repro.py`](mcp_server_git_repro.py) is what settled it,
and it is still the fastest way to check a crash report against any server without
trusting this tool.

```bash
# Reproduce it against any repository
ratemyagent scan --target mcp \
  --uri "stdio://uvx mcp-server-git --repository /path/to/repo" \
  --tool git_log --tool-args '{"repo_path": "/path/to/repo"}' \
  --requests 20 --fault-rate 0.3 --seed 42 --output all

# ...and check the crash claim without RateMyAgent in the loop (needs only `pip install mcp`)
python examples/mcp_server_git_repro.py
```

Note what the `--tool-args` are doing. Without them the scanner synthesizes arguments from
the JSON Schema: structurally valid, semantically meaningless. `mcp-server-git` rejects
every one, and the same server scores **38/100**. Pass real arguments before believing a
score — see [Known limitations](../README.md#known-limitations).

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
