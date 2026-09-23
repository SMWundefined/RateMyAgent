# Limitations, and why the tool declines to score

What this version measures less well than its numbers suggest, and the findings that
taught the tool to say "I could not measure this" instead of printing a number. Every
item here affects scores you can produce today, so read it before relying on one.

The [README](../README.md#known-limitations) carries a one-line summary of each.

---

## A number without its denominator is not a measurement

Four things this project measured about its own output, in the order it found
them.

**Every MCP server we scanned reported zero schema violations accepted.** Nine
scans across seven distinct servers, twenty releases, `accepted_invalid: 0`
every time — a clean bill of health for the ecosystem. Then we counted what
those schemas actually declare. Not one of those nine tools declares
`additionalProperties: false`, and none declares a length bound. **Two of the
eight malformed inputs we send could not have been violations of anything.** The
zero was true in both directions: nobody accepted what their schema forbade, and
for a quarter of the checks no schema forbade anything.

**Then the same survey turned out to be the wrong shape.** Those nine rows are
all stdio servers. The hosted servers in the same corpus declare both
constraints heavily — one declares `additionalProperties: false` on **9 of its 9
tools** — and we had scanned them for weeks without noticing, because the survey
covered one class of target and the conclusion was written about all of them.
Re-measured, that server **enforces what it declares**: the check runs, and its
zero is a measured zero rather than a vacuous one. A sibling's check is still
discarded, and the report says exactly why — the only case that tests undeclared
keys carries a baseline the tool itself rejects.

**Then we found a server that did declare a strict schema, and scored it 49 out
of 100.** It rejected every malformed call correctly, at the protocol layer,
which is the only way a strict schema can be enforced. We recorded 49 of 60
rejections as *crashing the transport*, because our code read "the SDK raised an
exception" as "nothing came back". An otherwise identical server that validated
nothing scored 50. **We were measuring strictness as fragility**, and the
stricter the server, the worse it looked.

**And a fourth we still cannot measure, for a reason that is itself the finding.**
One hosted server declares length bounds on most of its fields — the case the
first finding says we cannot test. At 20 requests our own scan trips its rate
limiter and the connection dies before the scan starts. At 5 requests it
completes: 22 edge cases, 20 rejected cleanly, `accepted_invalid: 0`, `n=5` and
no more, because **hammering a rate limiter to get a better number is the thing
this tool warns you not to do**. That warning is not theoretical. Elsewhere we
sent 585 calls for 200 operations against a dependency already returning 429,
then reported the failure we had deepened.

Every number above was produced by this tool, and each was wrong in the
flattering direction. What each needed was not a better threshold but a stated
denominator: *of what was asked*, *of which servers*, *of what the call actually
did*, *of how many runs*.

That is what this tool is for, and it is why it disagrees with the others. A
scanner that reports a score is easy. A scanner that reports what it could not
measure — and refuses to score it — is the harder and more useful thing, and it
is the only kind whose green result means anything.

When the evidence will not support a number the tool says so instead of printing
one: on a clean scan of a real MCP server **three of nine policy checks come back
`n/a`** with the reason attached, and on the strict-schema server above, six of nine
did. There are 27 places in the code where a metric can withdraw itself.

---

## Caller-strategy metrics are not scored against a bare MCP server

Retry amplification, backoff shape and recovery latency describe **the scanner's own retry
loop**, not the target's. A server does not retry — the client does.

**As of 0.1.9 they are no longer scored against one.** The behaviour dimension is split in
two:

- **Target survivability** — did the session keep answering under fault, and did its state
  survive a retry. Real against a server, and it carries the dimension's full 35 points.
- **Caller strategy** — retry amplification, and later backoff shape and recovery latency.
  Marked inapplicable when the target does not run its own retry loop, which is every
  service target.

Withheld metrics show as `n/a` rather than disappearing, and amplification is still
reported for context, labelled as the scanner's:

```
  Behavior ....... 6/7 disrupted operations recovered (86%), 1.45x amplification (ours)
  retry amplification        -          2.00x      n/a
```

Scores did not move meaningfully — the point was that 15 of 35 points had no subject, not
that they were producing wrong numbers. Caller strategy becomes scoreable when a target
runs its own retry loop, which is what `AgentTarget` is for — and since 1.5.0 retry
amplification is scored against one, below.

## Duplicate mutations need a state oracle, and one flag turns it on

**Without `--verify-tool`, `duplicate_mutations` is `n/a`.** A duplicated mutation is an
effect applied twice, and effects live in the target's state. Left to itself the scan sees
which calls it delivered and what came back: when it drops or damages a reply and retries,
it can count the re-send — reported as `duplicate deliveries (ours)`, never scored — but it
cannot tell a tool that applied the call twice from an idempotent tool that absorbed the
repeat, because both answer the same way.

**With `--verify-tool` (1.4.0) it is measured.** A read-only tool reports the target's own
state before and after the retried operations, `{op_id}` in `--tool-args` makes each
operation distinguishable, and effects are counted **per operation**. See
[SCANNING.md](SCANNING.md#counting-what-a-mutating-tool-applied). Four things still withhold
the metric, and each says which: no oracle (`absent`), no `{op_id}` (`unattributed`), state
left by a previous run of the same seed (`stale`), or a verify call that did not answer
(`failed`). A failed read is never scored as a zero.

**1.4.1: withheld no longer means green.** `stale` is caught at setup now, before the scan
writes anything, and exits 2. `stale` or `failed` reached any other way stops the verdict
reading PASS and makes `ci` exit 2. The 1.4.0 gap was worse than a missing number: the
withheld metric lifted the cap it exists to apply, so a rerun against dirty state printed
**100/100 PASS** while the target applied a duplicate it could no longer count. Absence
read as presence, in the one place this release was built to prevent it.

**1.3.0 scored that count, and it capped correct targets at 49.** Measured on 2026-09-15
with a twin fixture — a keyed `put` and an appending `append`, identical to the scanner in
every visible respect — given the same faults, both reported 2 duplicate mutations and both
scored 49/100. The `put` had applied nothing twice.

It was not confined to write tools. An injected `malformed` fault damages a reply the target
had already produced, and the retry counted as a second execution. Re-scanned under 1.3.0,
the section 9 regression set lost **six of its seven completed rows to the 49 cap, five of
them read-only** — tools where a repeated call changes nothing by definition. 1.2.0 counted
repeated successes and never fired. 1.3.1 withholds the metric.

**An aggregate count would not have been enough.** Effects minus successes lets two errors
cancel: one operation applied twice and one acknowledged but never applied give the same
number as a clean run. The count is per operation for that reason, and without `{op_id}` to
attribute it, both metrics stay `n/a` rather than reporting a total that can hide a pair.

**The count covers the retried operations only.** `duplicate_mutations` and `lost_effects`
are measured across the recovery pass, which is where a retry can apply work twice. Effects
applied in any other phase — the preflight call at setup, the baseline probes, the
degradation pass — are **not counted**, and every scan says so in a caveat.

That scope was assumed and then contradicted by measurement. The design claimed the recovery
pass was the only place a duplicate could arise; ground truth on the twin fixture found one
outside it, because **the preflight call and the first baseline operation carry the same
`{op_id}`**, so a content-addressed effect is applied twice before the window opens. The
window is stated rather than widened: counting the baseline would fold a probe's own traffic
into a number that is supposed to be about retries. A scan that needs the whole-run figure
has to read the target's state itself.

## A synthesized-argument scan refuses rather than scoring

Without `--tool-args`, arguments are synthesized from each tool's JSON Schema: correct
shape and types, placeholder values. A tool that wants a real URL, path or package name
rejects all of them, and every probe downstream then measures that rejection.

**Until 1.1.0 the scan scored it anyway.** Three servers — `mcp-server-fetch`,
`mcp-server-git` and `firecrawl-mcp` — each published `all 20 requests failed`,
`something is broken at any load`, and **43/100**. Three languages, three domains, three
different error messages, one number, and the number described this scanner.

1.1.0 sends one preflight call at setup. If the server rejects the synthesized payload,
the scan refuses:

```
error: refusing to scan: 'fetch' rejected the arguments this scan synthesized for it,
so latency, concurrency, fault, behavior would measure that rejection rather than the
target.

Pass --tool-args with arguments the tool accepts, or scan only the probes that do not
depend on the payload:
  --probes contract
```

Exit 2. With `--tool-args '{"url": "https://example.com"}'` the same server scans normally
and scores 89.

Two things this does not fix. Arguments you pass yourself are never overridden — a
rejected `--tool-args` warns and lets the affected probes withhold, because you vouched
for the payload. And a tool that *accepts* a placeholder measures something shallow rather
than nothing: `"ratemyagent probe"` is a legal string, and a server that echoes it back
will be profiled on a trivial call. Seeding synthesis from the schema's `examples`,
`default` or `enum` is the fix for that half and is not built.

**`--tool` and `--tool-args` remain the supported path for any number you intend to rely
on.** How to pass them is in [SCANNING.md](SCANNING.md#pass-real-arguments).

## Scores against network-dependent targets are not stable across runs

A scan of a server that calls out to the internet measures upstream conditions as much as
the server. `mcp-web-engine` produced p95 **0.72s and 8.01s in the same session**, which
moved its composite from 100 to 85 with no change to the tool and no change to the server.

Latency and concurrency carry that straight into the score. **Quote a range from repeated
runs, with the count.** Ten runs of that same server later gave p50 `0.21s (0.20-0.69s,
n=10)` and a composite that did not move at all — so the honest form is a range and an `n`,
not an omission. A row dropped for having variance looks more consistent than the eight
beside it that were each measured once.

**The axis is threshold proximity, not transport.** A composite is stable while the
measurement sits orders of magnitude clear of its limit: p95 across the regression set
spans 0.001s to 0.67s against a 5s threshold, so jitter cannot reach the score. The
100-vs-85 swing above was p95 *crossing* 5s. Three of the six stdio rows reach the network
through the tool they probe, and the one `http://` row does not, so the transport column
does not predict this.

## The scan can cause the failure it reports

**This is a limit on what a scan can claim, not a bug with a workaround.**

RateMyAgent retries a disrupted operation twice, uniformly, across all five
fault kinds. Against a **rate-limited dependency** that is the wrong response,
and it compounds: a measured run against a free-tier API sent **585 calls for
200 operations — 2.92x amplification — while the dependency was already
returning 429**. The scan then reported a 95.5% error rate and 1.5% recovery.

Every one of those numbers is correct. None of them is a fact about the server:
they describe a server *being throttled by this scan*, and the throttling
deepened as the retries continued. The supported claim is **"a target under rate
limiting does not recover within two retries"** — which is true, and is about
retry budgets rather than about that server.

So, before pointing this at anything with a quota:

- **A rate limit will read as a reliability failure.** `error_rate`,
  `recovery_rate` and the concurrency saturation point will all degrade
  together, and the findings will describe a broken dependency.
- **Check the error kinds before believing the score.** `191 rate_limit` in the
  latency findings means the cap was the story. A genuine fault mix looks
  varied.
- **Scale `--requests` to what the quota tolerates**, or use a tier without one.
  A scan that is itself the load is measuring itself.
- **`retry_amplification` is reported and never scored** against a server
  target, because the retry loop is ours. When it is high, read it as a
  statement about what this tool did to your dependency.

**Since 1.0.1 the tool backs off.** A `rate_limit` retry now waits before
retrying — honouring a `Retry-After` hint when there is one, capped at
`--backoff-max` (5s) per retry and `--backoff-budget` (30s) per probe. The
trigger is the error kind, never the presence of a hint, because the case this
exists for relays an upstream 429 as a tool result with no header anywhere.

Measured on `tests/fixtures/rate_limited_mcp_server.py`, a server that refuses
until six seconds have passed and advertises nothing:

| | `--backoff-max 0` (1.0 behaviour) | default |
|---|---|---|
| calls for 12 operations | 48 | 26 |
| retry amplification | 3.00x | 1.17x |
| operations disrupted | 12 | 1 |
| recovered | 0 (0.0%) | 1 (100%) |
| wall clock | 0.27s | 10.21s |

The number that matters is `disrupted`: eleven of twelve operations stopped
failing because the scan stopped causing it. Backing off makes scans slower —
`backoff_waited_s` is reported so the time is attributable — and when the budget
runs out the scan continues without waiting rather than stopping, which is
recorded as a caveat rather than hidden.

**The rest of this section still holds.** The tool does not detect a quota, so a
rate limit still reads as a reliability failure and the advice above about
`--requests`, error kinds and `retry_amplification` is unchanged. Backoff bounds
the damage; it does not tell you the cap is there.

## A latency figure describes the path the call took, not the one its name implies

One scanned server answers a PyPI-backed query in **1.7ms**. It runs a seven-day cache. A
sibling server reaching the same dependency takes 130ms — 76x, same upstream, and the
difference is the most interesting thing either number has to say.

Before quoting a latency figure, ask whether it is physically possible for the distance
claimed. **If a server answers a network-backed query in single-digit milliseconds, the
question is what it is not doing.** The scan cannot detect this for you; it reports what
the call cost, and a cache hit is a real cost to a real caller.

## Agent scans are experimental, and narrow

`--target agent` (1.5.0) is outside the API freeze, and what it can conclude is bounded in
five ways.

**The upstream's state has to live outside its process.** The agent's proxy starts its own
copy of a stdio server per task and the verify tool starts another, so an in-memory server
shows the oracle an empty store every time. The clean pass refuses rather than count those
zeros: a task the agent completed with a success reply must show its `expected_effects`
to the verify tool. The same refusal fires for a server that persists and silently drops
every write — from outside, the two are the same observation — so a server that loses
work *from the first call* cannot be measured for lost effects; one that starts losing it
later can.

**Tasks run strictly one at a time, and the target refuses a second in flight.** Effects
are counted by reading the server's state before and after each task; with two tasks in
the window each count contains the other's, and nothing afterwards separates them. So an
agent that runs work concurrently is scanned one task at a time, which is not how it runs.
Within a task, the two reads say *how many* effects landed and not *which attempt* applied
the extra one.

**The Retry-After hint travels in the tool error body.** Stdio has no headers, so a relayed
429 carries `retry_after_s` inside the error JSON. That is a convention, not a standard a
client is bound to parse: `retry_after_honored` of 0% can mean the hint was ignored or
never seen. It is reported and not scored for that reason, and `n/a` when no retry followed
a hinted rate limit — never 100% over nothing. `backoff_shape` is `n/a` when fewer than two
consecutive waits after a delivered failure were long enough to measure (20ms): a ratio of
two pipe-overhead gaps is noise, not a schedule. Against an **LLM** agent both are
withheld outright rather than reported — see below.

**One real agent has now been scanned, and it hangs.** Three scripted fixtures validate the
measurement and none of them reads a prompt. Beyond them, Phase D has scanned exactly one
real agent: Claude Code 2.1.275, driven per task through the MCP config it already reads. It
completed the task through the proxy on the clean pass. On a dropped reply it waited **234
seconds** without retrying, cancelling or returning, and the task died on the scan's deadline
rather than the agent's. A separate probe held a reply for 90 seconds and then released it:
the agent waited the whole 90 and accepted the late answer.

So an agent with no read timeout still hangs under `RESPONSE_LOST`, the scan still kills it
at the task deadline and exits 2 rather than scoring it, and **that refusal is the right
output** — the measurement is the agent's next decision, and there was none to observe.
`--lost-reply-close-after` (1.6.0) is how to get a measurement out of such an agent: it ends
the session a few seconds after the reply is dropped, so the client is handed an event it
cannot ignore while still learning nothing about whether its write applied. It is a
different fault rather than a fixed one, and the two are counted separately everywhere.

What is not known is how common this is. One agent is not a rate.

**Over five replicates the same agent applied the write twice in two of them.** The Phase D
gate re-run (`assets/moat/GATE-D2.md`) put Claude Code on `claude-haiku-4-5` through five
runs of one task under `RESPONSE_LOST_THEN_CLOSED`, all five faulting the same call. It
reconnected and retried every time. `duplicate mutations 0-1 (n=5); occurred in 2 of 5 runs`,
both confirmed by the server's own ledger and re-derived by a stdlib-only script
(`examples/phase-d-gate/`).

**Two distinct failure modes.** In one run the retry carried no idempotency key at all; in
another it carried a *different* key, which an upstream cannot tell from new work. And the
model's key derivation is not stable: it minted a different key on three of the five runs,
where an earlier run of the same task used one key throughout.

Five runs of one agent on one task is still not a rate. The first attempt at this gate
(`assets/moat/GATE-D.md`) is the run that found both of the limits above — four of its five
runs applied nothing, because the scan's clean pass had already spent the agent's key, and
the report scored it 100/100 PASS anyway.

**And the same question against a server this project did not write.** Gate BD
(`examples/gate-bd/`) put the same model through five separate scans against unmodified
`mcp-sqlite@1.0.9`, a fresh database each: **an applied duplicate in 3 of 5**, same
placement in all five, re-derived from the databases by a stdlib-only script.

The two gates bound different things and neither replaces the other. Against the twin, a
repeated idempotency key is absorbed, so a duplicate is a statement about **the agent's
key discipline**. Against `mcp-sqlite`, `create_record` is an `INSERT` honouring no key,
so every retry duplicates and the only variable left is **whether the agent retried at
all** — which it did in three runs of five. A duplicate count from Gate BD is therefore
evidence that the tool sees an applied effect on a third-party server, and is *not*
evidence about how carefully the agent sent it.

Two limits specific to Gate BD, stated because they bound its number. `--allowedTools`
carried `create_record` alone, withholding `read_records` — so check-before-retry, the only
mitigation available against a server that honours no keys, was foreclosed by the
experiment rather than declined by the agent. And the table's schema was given in the
prompt, because denying the agent its schema lookup made it abort the task outright on the
first attempt at the gate, in five runs of nine.

Its checker is also independent in a weaker sense than Gate D's, and `examples/gate-bd/`
carries the trust table rather than implying parity: Gate D partitions one ledger by a
`generation` field the twin server writes, while Gate BD relies on a fresh store per
replicate plus one behaviour of the tool under test — that the scan refuses unless the
clean pass applied exactly `expected_effects`.

**A single run of a model supports fewer claims than it looks like it does.** Everything
above was measured against scripted fixtures, whose call sequence is a constant of their
source. A model chooses its own calls, and three consequences follow that the outputs now
state rather than leave to be noticed.

*The schedule is a table; what a run does with it is not the same document.* Entries are
keyed `(task_id, tool, ordinal)`, and an ordinal is only reached if the agent makes that
many calls to that tool. `realized_schedule` (1.6.1) reports which calls were actually
faulted, in order, beside the intended table — the three shipped demos schedule 82 entries
and realize 8. Against an agent whose call count varies, **one seed faults a different call
between runs**, so two runs at that seed are two experiments sharing a random number rather
than two replicates. Repeats are grouped by realized placement for exactly that reason.

*Three metrics do not survive a model.* On an LLM agent, `retry_amplification` is reported
and not scored: its denominator is the clean-pass call count, measured where no fault was
injected at all, and two clean runs of one task can differ by a `tools/list` the model felt
like making — a reported 1.6x may be entirely a chattier pass. `calls_under_fault` and
`clean_path_calls` are published as raw counts, because calls that happened are facts.
`backoff_shape`, `backoff_growth` and `retry_after_honored` are **withheld**: they read
wall-clock gaps between attempts, and when a model produces the gap it is an inference
round trip rather than a schedule. `growing` would be reported for a model whose second
response was simply longer than its first.

*A range over repeats is not a range over replicates unless the faults landed in the same
place.* `--repeats N` runs the whole task set N times and reports min-max with the run count,
an occurrence count for anything yes/no, the individual values below n=3, and a score taken
from the **worst** observed run. Runs whose realized placement differs are reported as
separate groups rather than folded into one range. Two limits worth knowing: **the clean pass
still runs once**, so every repeat shares one call-count denominator and the spread you see is
the numerator's; and the fault probe's own headline numbers describe the first run, with the
per-run detail in `runs` and the grouped ranges on the behaviour probe.

*The fixture's key scope was the other half of this, and it changed in 1.7.0.* The shipped
event twin used to absorb an idempotency key forever once used, which models a key as
belonging to a *task*; it belongs to an **operation**, and running the same task again is a
second operation whose real work a global scope swallows. `--key-scope operation` is now the
default. If you drive your own upstream, the same question applies to it: a server that
absorbs a repeat across two separate runs of a task is not deduplicating, it is losing
writes, and a scan against it will report effects that never landed.

*The scan's own clean pass is part of the state every later run starts from.* One clean pass,
N chaos runs, one persistent store — so whatever the clean pass applied is still there when
repeat 1 opens its window, and when repeat 5 does. An agent that derives an idempotency key
from the task's own content sends the same key in every run, and a server that absorbs a
repeated key absorbs it across runs too, because the key it matched was spent by the clean
pass. Every later run then applies nothing, and `duplicate_mutations` reads 0 for a reason
that has nothing to do with the agent. **This is measured, not hypothetical** — it is what the
Phase D gate run produced, four times in five.

The scan names it in a caveat when the clean pass applied something and a later run's window
opened on a non-empty store. It does **not** repair it: isolating state per run means a fresh
store per run or a task payload that differs per run, both changes to your own fixture, and a
scanner that rewrote either would be inventing the experiment rather than running the one it
was handed. `docs/SCANNING.md` has both recipes.

*A run that applied nothing is not scored as a pass.* If every task that was meant to apply an
effect applied none, the effect metrics for that run are arithmetic over an empty window, and
a `duplicate_mutations` of 0 is the absence of applied writes rather than evidence of care.
The scan reports `NO VERDICT` with the reason at default verbosity and `ci` exits 2. A
coverage rule and not a penalty: no score moves, and `lost_effects` remains the server's fault
and remains unscored. The gate run that prompted it scored **100/100 PASS** while four of its
five runs applied nothing.

*Which kind of agent it is, you tell us.* `--agent-kind scripted|llm`, declared and never
inferred.
Every observable candidate — per-task wall clock, inter-call gaps, whether repeats differ —
is a threshold on a continuum that a loaded machine, a fixture that sleeps, or a small local
model collapses, and a threshold quietly deciding which metrics get scored is the defect
this project retired `concurrency_min` for. A scripted agent is the default, and nothing
about a scripted scan changed.

**The seeded schedule is sparse where it matters.** Each `(task, tool, ordinal)` is drawn
independently at `--fault-rate`, so at the default 20% most first calls are clean and a
small task file may see no fault at all. A scan in which no call went unanswered gets
`NO VERDICT` and `ci` exits 2: nothing tested whether the agent could apply a write twice.
The ten-task demo file draws no such fault at the default seed and rate, and one at
`--fault-rate 0.7`.

## Faults are transient, not outages

Faults are injected independently per attempt, which models transient failure well and
sustained outages not at all — and a dependency that stays down for a while is the mode
that actually breaks systems. Outage windows are on the v2 roadmap.
