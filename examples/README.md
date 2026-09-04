# Examples

What RateMyAgent's output actually looks like, without installing anything.

| File | What it is |
|---|---|
| [`AGENTS.md`](AGENTS.md) | The fix guide, generated from a deliberately broken target |
| [`report.md`](report.md) | The full markdown report for the same scan |
| [`scan_mcp_example.py`](scan_mcp_example.py) | Driving a scan from Python instead of the CLI |

## How these were generated

Both files come from one scan of the built-in `failing` mock profile — a target that is
slow, drops requests, crashes on malformed input and does not recover well. It exists to
exercise every finding at once, so the examples show the tool having plenty to say.

```bash
ratemyagent scan --target mock --profile failing --requests 40 --concurrency 16 \
    --fault-rate 0.3 --seed 42 --output all \
    --report-out examples/report.md --agents-md-out examples/AGENTS.md
```

That command reproduces them exactly: the mock is deterministic and fault injection is
seeded per operation and attempt, so `--seed 42` replays the same run. The only thing that
varies is the timestamp in the header and the `generated_at` field in the state block at
the bottom of `AGENTS.md`.

## Reading them

`AGENTS.md` opens with the verdict and where the score went, then lists what to fix in
severity order — duplicate mutations and crashes before latency and cost. Each section
states what was observed, why it matters in production, the likely root cause, and a fix
you can paste.

Re-running the command against a *fixed* version of the same target adds a
"Since the last scan" section reporting what moved. The `<!-- ratemyagent-state -->` block
at the bottom is what makes that possible — leave it in place.

`report.md` is the whole scan: actual-vs-target, the score breakdown, then every phase with
its metrics, per-level concurrency numbers, and all findings.
