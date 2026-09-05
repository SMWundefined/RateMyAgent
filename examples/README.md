# Examples

What RateMyAgent's output actually looks like, without installing anything.

| File | What it is |
|---|---|
| [`mcp-server-git.AGENTS.md`](mcp-server-git.AGENTS.md) | Fix guide for a **real, published MCP server** |
| [`mcp-server-git.report.md`](mcp-server-git.report.md) | The full report for that scan |
| [`mock-failing.AGENTS.md`](mock-failing.AGENTS.md) | Fix guide for a deliberately broken target |
| [`mock-failing.report.md`](mock-failing.report.md) | The full report for that scan |
| [`scan_mcp_example.py`](scan_mcp_example.py) | Driving a scan from Python instead of the CLI |

## The real one

`mcp-server-git` is the official Git MCP server, installed by a lot of people. It scores
**90/100 and passes** — and the scan still finds that half its edge cases crash the stdio
transport instead of returning an error.

That combination is the whole argument for this tool. The server does its job correctly:
p95 latency 0.06s, a zero error rate, full marks on latency and concurrency. An
evaluation asking "can it accomplish the task" gives it a clean bill. Send it an empty
string, an undeclared extra field, or a long string — the three things a model does when
it guesses at an argument — and the connection dies, taking every other in-flight call
with it.

```bash
# Reproduce it against any repository
ratemyagent scan --target mcp \
  --uri "stdio://uvx mcp-server-git --repository /path/to/repo" \
  --tool git_log --tool-args '{"repo_path": "/path/to/repo"}' \
  --requests 20 --fault-rate 0.3 --seed 42 --output all
```

Reported upstream as
[modelcontextprotocol/servers#4754](https://github.com/modelcontextprotocol/servers/issues/4754).
The crash count varies with the repository you point it at — 6 of 18 against a large repo,
9 of 18 against a small one — but it reproduces on every run.

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
