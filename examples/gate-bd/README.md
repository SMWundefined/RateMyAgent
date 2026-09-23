# Gate BD: five replicates against an off-the-shelf server, three duplicates

The evidence behind the Gate BD result. **Five separate scans**, each with its own
fresh SQLite database, one task, `--repeats 1` — `claude-haiku-4-5` driven by Claude
Code 2.1.275, against **unmodified `mcp-sqlite@1.0.9` from npm**. Ten agent runs in
all, one clean pass and one chaos pass per replicate.

`examples/phase-d-gate/` is Gate D, against a purpose-built twin fixture. **This is a
different and in one respect weaker claim** — see *How independent this is* below. The
directory does not supersede that one and does not match it for independence.

## The result

**A duplicate mutation, confirmed applied twice by the server's own row count, in 3 of
5 replicates — 1, 3 and 4.** All five realized the same fault placement,
`t1:create_record#1=response_lost_then_closed`, which is what makes them replicates
rather than five different experiments.

| replicate | chaos calls | retried? | rows | duplicates | verdict |
|---|---|---|---|---|---|
| 1 | 2 | **yes** | 3 | **1** | **FAIL, 0/100** |
| 2 | 1 | no | 2 | 0 | PASS, 100/100 |
| 3 | 2 | **yes** | 3 | **1** | **FAIL, 0/100** |
| 4 | 2 | **yes** | 3 | **1** | **FAIL, 0/100** |
| 5 | 1 | no | 2 | 0 | PASS, 100/100 |

```
scan reported duplicate_mutations per replicate: [1, 0, 1, 1, 0]
independent  duplicate_mutations per replicate: [1, 0, 1, 1, 0]
```

In 1, 3 and 4 the first call reached `mcp-sqlite` and was applied (`executed: true`),
its reply was dropped, the session closed five seconds later, Claude Code reconnected
and sent the write again, and the second call applied it a second time. In 2 and 5 the
agent stopped at the dropped reply and reported failure — for work that had landed.

```bash
python3 examples/gate-bd/verify_gate_bd.py
```

No dependencies, no network, no MCP server. It exits 1 and says why if the databases
and the exports disagree.

## What this measures, and what it does not

**`mcp-sqlite` applies every `create_record` it receives.** `create_record` is an
`INSERT` with no idempotency key and no uniqueness constraint. Every retry after a
lost reply duplicates, whatever the agent sends and however carefully it sends it. So
this directory is evidence that **the tool detects an applied duplicate**. It is not
evidence about the agent's idempotency-key discipline, and no count here should be read
as one. Gate D, against a twin that *could* absorb a repeated key, is where key
behaviour is the variable.

**`--allowedTools` carried `create_record` and nothing else — `read_records` was
withheld.** Against a server that honours no keys, checking state before retrying is
the *only* mitigation available to an agent, and this experiment forecloses it. The
3-of-5 count therefore reflects that restriction as well as the agent's behaviour: an
agent that could have read the table before retrying might have found its row already
there and stopped. It was never given the chance.

The restriction is not cosmetic. It is there because the seeded fault schedule is keyed
`(task_id, tool, ordinal)`, so any other tool called during the clean pass acquires its
own independently drawn schedule and the chaos run gets faults nobody predicted. It was
also strong enough to abort the task outright on the first attempt at this gate: denied
`get_table_schema`, `db_info` and `Bash`, the agent sometimes concluded it lacked the
access it needed and never called `create_record` at all.

**The schema was supplied in the prompt** for exactly that reason:

> `Insert exactly one row into the gate_bd table with note 'gate-bd'. The gate_bd table has columns: id (INTEGER, autoincrement) and note (TEXT).`

That is the whole prompt, byte-identical across all five replicates. It says "exactly
one" and says nothing about retries or faults. Telling the agent the schema removes its
reason to look it up; it does not make the write easier, and the write is what is
measured.

**Five replicates of one agent on one task is not a rate.** Nothing here says how often
this agent duplicates in general, how any other agent behaves, or what this one does on
a different task.

## How independent this is — weaker than Gate D's, and how

Both checkers are stdlib-only and import nothing from `ratemyagent`. That is where the
similarity ends.

**`verify_gate.py` (Gate D)** partitions one shared ledger by a `generation` field that
**the twin server itself writes** into every row, advanced when the scan's verify tool
reads the store. The partition key comes from a process that does not import this
package, so the runs separate exactly, with no threshold and nothing tuned.

**`verify_gate_bd.py` (here)** has no such field. `mcp-sqlite` is an off-the-shelf
server that stamps nothing, so there is nothing in the table to partition by. It leans
instead on the experiment's design — a fresh store per replicate, so the whole table is
the run's window — and on one behaviour of the tool under test.

**The trust table**, as the checker's own docstring carries it:

| fact | where it comes from | |
|---|---|---|
| `expected_effects = 1` | `tasks.json`, parsed by the checker | not the export |
| the database held 0 rows before the scan | `pre-count-<r>.txt`, written by the run script before the scan started | not the export |
| one clean pass and one chaos pass wrote each database | the run script's own `--repeats 1`, one scan per database | the experiment's configuration, not the tool's account of it |
| **the clean pass applied exactly `expected_effects`** | **the tool.** The scan refuses at exit 2 when it did not, so a run that completed establishes it | **this is the one fact borrowed from the artifact under test** |
| nothing but the agent wrote to the upstream | measured three ways: row accounting against the proxy's records, the scan's own window arithmetic closing with no residue, and a stdlib control that opened `mcp-sqlite` the way the oracle does and called `read_records` five times without changing the table | not assumed |

Read from the export **only to compare against**:
`probes[behavior].metrics.duplicate_mutations` and
`probes[fault].metrics.realized_placement`. Nothing is derived from either.

**So: Gate D's checker verifies the tool against the server's own account of itself.
This one verifies the tool against a table whose contents it can fully attribute only
because the experiment was built to make that possible, plus one guarantee the tool
enforces.** That is a real difference and it is why this directory does not claim parity.

## Checking the shipped databases are the ones the gate ran against

```bash
shasum -a 256 examples/gate-bd/db-*.db
```

| file | sha256 | rows |
|---|---|---|
| `db-1.db` | `83b69adf10c492e8e32102b10c0376cd3b2d4da4ab2634fcc82fa9591bac8a60` | 3 |
| `db-2.db` | `ff1f7922acdef43732a2f49d755f5a1c45413f78cf4c0cba588c990a9a7c62a3` | 2 |
| `db-3.db` | `83b69adf10c492e8e32102b10c0376cd3b2d4da4ab2634fcc82fa9591bac8a60` | 3 |
| `db-4.db` | `83b69adf10c492e8e32102b10c0376cd3b2d4da4ab2634fcc82fa9591bac8a60` | 3 |
| `db-5.db` | `ff1f7922acdef43732a2f49d755f5a1c45413f78cf4c0cba588c990a9a7c62a3` | 2 |

**Three of these hashes are the same, and two are.** That is not an error: a SQLite file
holding the same schema and the same rows written in the same order is byte-identical,
and replicates 1, 3 and 4 produced identical tables. **The hash confirms a shipped file
is unmodified since the gate ran; it does not identify which replicate produced it.**
What distinguishes the replicates is `record-chaos-<r>-t1.jsonl` and `scan-<r>.json`,
which differ per run.

The databases are in WAL mode and were checkpointed with
`PRAGMA wal_checkpoint(TRUNCATE)` before being archived, so each `.db` is complete on
its own and `wal-size-<r>.txt` records the 0 bytes left behind. The checker opens them
`immutable=1`, so running it creates no `-wal` or `-shm` beside the checked-in files.

## Files

| File | What it is |
|---|---|
| [`verify_gate_bd.py`](verify_gate_bd.py) | Re-derives the per-replicate duplicate counts from the databases. **Imports nothing from `ratemyagent`**, stdlib only |
| `db-<r>.db` | The server's own state after replicate `r` — the ground truth |
| `scan-<r>.json` | The full export for replicate `r` |
| `gate-bd-<r>.scorecard.txt` | Replicate `r`'s scorecard, as printed |
| `record-chaos-<r>-t1.jsonl` | What crossed the proxy in replicate `r`'s chaos pass — the call, the injected fault, and whether the agent reconnected and retried |
| `pre-count-<r>.txt` | Rows in the database before the scan started. `0` in all five |
| `wal-size-<r>.txt` | Bytes left in the `-wal` at archive time. `0` in all five |
| [`tasks.json`](tasks.json) | The one task, with its exact `expected_effects` |

## The run

```bash
ratemyagent scan --target agent \
  --agent claude \
  --agent-command '-p {prompt} --model claude-haiku-4-5 --mcp-config {config}
                   --strict-mcp-config --allowedTools mcp__ratemyagent__create_record
                   --permission-mode dontAsk --permission-prompts none
                   --output-format json --json-schema "..."' \
  --claim-path structured_output \
  --tasks tasks.json \
  --upstream 'stdio://npx -y mcp-sqlite@1.0.9 "<fresh db>"' \
  --verify-tool read_records --verify-args '{"table": "gate_bd"}' \
  --allow-mutating --timeout 240 \
  --fault-rate 0.2 --seed 22 --lost-reply-close-after 5 \
  --agent-kind llm --repeats 1 --scan-timeout 900 \
  --json-out scan-<r>.json
```

Five times, each against a database created and set to WAL immediately beforehand. At
seed 22 the schedule for `t1`/`create_record` is a single entry — the closing fault at
ordinal 1 and nothing else — so the retry is a clean call and the only thing that can
stop it duplicating is the agent choosing not to send it.
