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
  target type today.

Withheld metrics show as `n/a` rather than disappearing, and amplification is still
reported for context, labelled as the scanner's:

```
  Behavior ....... 6/7 disrupted operations recovered (86%), 1.45x amplification (ours)
  retry amplification        -          2.00x      n/a
```

Scores did not move meaningfully — the point was that 15 of 35 points had no subject, not
that they were producing wrong numbers. Caller strategy becomes scoreable when a target
runs its own retry loop, which is what `AgentTarget` is for.

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

## Faults are transient, not outages

Faults are injected independently per attempt, which models transient failure well and
sustained outages not at all — and a dependency that stays down for a while is the mode
that actually breaks systems. Outage windows are on the v2 roadmap.
