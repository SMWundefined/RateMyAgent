"""AGENTS.md generator: a fix guide for this specific target.

Not a linter dump. Every section has to earn its place by answering four things
the raw scorecard cannot:

1. **What was observed** -- the number, not an opinion.
2. **Why it matters in production** -- the failure this causes downstream, not
   "best practice".
3. **The common root cause** -- weighted toward what AI-generated servers and
   agents actually get wrong, because that is who runs this.
4. **A concrete fix** -- copy-pasteable, naming the tool, endpoint or model that
   exhibited the problem.

A recommendation that says "add input validation" has told the reader nothing
they did not already know. The bar here is that someone can act on the section
without opening another tab.

Re-scans diff against the previous file, so the guide reports movement:
"latency improved from 3.2s to 1.8s since last scan".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..models import ScanResult
from .common import breakdown_rows, target_rows, verdict_lines

logger = logging.getLogger(__name__)

#: Machine-readable state hidden in the file, so a re-scan can diff against it
#: without parsing prose back out of the document.
STATE_OPEN = "<!-- ratemyagent-state"
STATE_CLOSE = "-->"

_STATE_RE = re.compile(
    re.escape(STATE_OPEN) + r"\s*(?P<json>\{.*?\})\s*" + re.escape(STATE_CLOSE),
    re.DOTALL,
)


@dataclass(frozen=True)
class Advice:
    """One section of the guide."""

    key: str
    title: str
    applies: Callable[[ScanResult], bool]
    render: Callable[[ScanResult], str]
    #: Lower sorts first. Correctness issues outrank efficiency ones.
    priority: int = 50


# -- helpers ------------------------------------------------------------------


def _metrics(result: ScanResult, probe: str) -> dict[str, Any]:
    found = result.probe(probe)
    return found.metrics if found and found.applicable else {}


def _target_noun(result: ScanResult) -> str:
    """How to refer to the thing being scanned, in prose."""
    info = result.target
    if info.kind == "mcp":
        return f"your MCP server (`{info.name}`)"
    if info.kind == "llm":
        model = info.metadata.get("model", info.name)
        return f"the `{model}` endpoint"
    return f"the target (`{info.name}`)"


def _model_name(result: ScanResult) -> str | None:
    """The model actually scanned, or None when the scan never learned one.

    The cost probe resolves this while pricing the run (from --model or the
    target's own metadata), so ask it before falling back to the target.
    """
    for source in (_metrics(result, "cost").get("model"),
                   result.target.metadata.get("model")):
        if source:
            return str(source)
    return None


def _probe_tool(result: ScanResult) -> str:
    """The tool that was actually exercised, for a copy-pasteable fix."""
    tool = result.target.metadata.get("probe_tool")
    if tool:
        return str(tool)
    if result.target.capabilities:
        return str(result.target.capabilities[0])
    return "your_tool"


def _worst_offending_tools(result: ScanResult, outcome: str) -> list[str]:
    cases = _metrics(result, "contract").get("cases") or []
    return sorted({c["tool"] for c in cases if c.get("outcome") == outcome})


# -- the advice ---------------------------------------------------------------


def _accepts_invalid(result: ScanResult) -> str:
    metrics = _metrics(result, "contract")
    count = metrics.get("accepted_invalid", 0)
    cases = sorted(
        {c["case"] for c in (metrics.get("cases") or []) if c.get("wrongly_accepted")}
    )
    tools = _worst_offending_tools(result, "accepted") or [_probe_tool(result)]
    tool = tools[0]

    return f"""**FINDING: {count} schema-forbidden inputs accepted**

Sending {", ".join(cases)} to {_target_noun(result)} returned a success response
instead of an error. The affected tools: {", ".join(f"`{t}`" for t in tools)}.

Your tool declares required fields and types in its JSON Schema but does not
enforce them at runtime. This is common in AI-generated MCP servers where the
schema is correct but the handler trusts its input. Every field marked
"required" needs an explicit check before the handler touches the data, because
the calling agent WILL send malformed arguments -- that is normal traffic, not
an attack. A model that has just been told a tool exists guesses at its
arguments, and guesses wrong often.

In production this surfaces far from here: a `None` that should have been
rejected is written to a row, cached, or passed to a downstream service, and the
error appears hours later somewhere that has no idea where the value came from.

Suggested fix for tool "{tool}":

```python
def {tool}(args: dict) -> dict:
    if "query" not in args or not isinstance(args["query"], str):
        return {{"error": "query is required and must be a string"}}
    if not args["query"].strip():
        return {{"error": "query must not be empty"}}
    ...
```

If you already build request models with Pydantic, validate at the boundary
instead and let the framework produce the error:

```python
class {tool.title().replace("_", "")}Args(BaseModel):
    query: str = Field(min_length=1)
```
"""


def _crashes_on_edge_cases(result: ScanResult) -> str:
    metrics = _metrics(result, "contract")
    crashed = [c for c in (metrics.get("cases") or []) if c.get("outcome") == "crashed"]
    kinds = sorted({f"`{c['case']}`" for c in crashed})
    tools = _worst_offending_tools(result, "crashed") or [_probe_tool(result)]

    return f"""**FINDING: {metrics.get('crashes', 0)} edge cases crashed the transport**

{", ".join(kinds)} did not return an error from {_target_noun(result)} -- they
killed the connection. Affected tools: {", ".join(f"`{t}`" for t in tools)}.

An unhandled exception in a tool handler takes down the stdio pipe or the HTTP
connection, not just the one call. Every other in-flight request on that
connection dies with it, and the agent sees a transport failure it cannot
attribute to anything it did. In AI-generated servers this usually traces to a
handler that indexes or parses its arguments directly -- `args["query"].lower()`
on a `None`, or `int(args["limit"])` on a string -- with no try/except anywhere
between the handler and the transport.

Suggested fix for tool "{tools[0]}":

```python
async def {tools[0]}(args: dict) -> dict:
    try:
        return await _do_work(args)
    except Exception as exc:
        # An error response keeps the connection alive; an exception does not.
        logger.exception("{tools[0]} failed")
        return {{"error": f"{tools[0]} failed: {{exc}}"}}
```

The point is not to swallow bugs -- keep logging them -- it is that a bad
argument should cost one call, not the whole session.
"""


def _slow_p95(result: ScanResult) -> str:
    metrics = _metrics(result, "latency")
    p95, p50 = metrics.get("p95_s"), metrics.get("p50_s")
    check = next((c for c in result.checks if c.name == "p95_latency_ms"), None)
    target = (check.threshold / 1000) if check else 5.0
    overhead = metrics.get("tool_call_overhead_s")

    section = f"""**FINDING: p95 latency {p95:.2f}s against a {target:.1f}s target**

Half of the calls to {_target_noun(result)} finish in {p50:.2f}s, but one in
twenty takes {p95:.2f}s or longer.

Latency is the budget every caller above you spends. An agent making three tool
calls in a turn inherits the p95 of each one, so a {p95:.2f}s tool is a
{p95 * 3:.0f}s turn before the model has written a word of the answer -- and
that is the number a user experiences as "it hung".
"""

    if overhead and overhead > 0.2:
        section += f"""
{overhead * 1000:.0f}ms of that is transport and serialization rather than work,
measured as the gap between total latency and the execution time the target
reports for itself. In AI-generated servers this is usually the handler doing
setup per call -- opening a database connection, re-reading a config file,
constructing a client -- that belongs at module scope:

```python
# Once, at import. Not inside the handler.
_client = httpx.AsyncClient(timeout=10.0)
_pool = await asyncpg.create_pool(DSN)
```
"""
    else:
        section += """
Profile before optimizing. If the work itself is genuinely slow, the fix is
usually to return early with a job id and let the agent poll, rather than to
hold a tool call open for seconds at a time.
"""
    return section


def _heavy_tail(result: ScanResult) -> str:
    metrics = _metrics(result, "latency")
    ratio, p50, p99 = metrics["tail_ratio"], metrics["p50_s"], metrics["p99_s"]

    return f"""**FINDING: p99 is {ratio:.1f}x p50 ({p99:.2f}s against {p50:.2f}s)**

The median call to {_target_noun(result)} is fine. The tail is not, and a tail
this heavy is a different bug from "the service is slow" -- something specific
is happening to a minority of calls.

The usual causes, in the order worth checking: a cold start or lazy
initialization on the first call after an idle period; an unbounded retry inside
the handler that turns one slow dependency into several sequential waits; or
lock contention on a shared client that serializes what looks like concurrent
work.

Optimizing the median will not move this number. Log per-call timings with a
correlation id and look only at the slowest 1% -- they will have something in
common that the average call does not.
"""


def _prompt_bloat(result: ScanResult) -> str:
    metrics = _metrics(result, "cost")
    static = metrics.get("static_prefix_tokens", 0)
    mean_in = metrics.get("mean_input_tokens", 0)
    share = metrics.get("bloat_share", 0)
    savings = metrics.get("cacheable_savings_per_request")
    model = _model_name(result)
    # A code sample with a made-up model id is not copy-pasteable. When the scan
    # never learned one, say so and leave the caller's own constant in place.
    model_ref = f"`{model}`" if model else "the model"
    model_literal = f'"{model}"' if model else "MODEL"

    section = f"""**FINDING: {share:.0%} of every request is an unchanging prefix**

About {static:,.0f} of the {mean_in:,.0f} input tokens sent to {model_ref} are
byte-identical on every call.

You are paying full input rate, on every request, for text the provider has
already seen. At scale this is the single largest avoidable line on an LLM bill,
and it is invisible in testing because a hundred requests cost cents.
"""
    if savings:
        section += f"""
Caching that prefix would save roughly ${savings:.4f} per request --
${savings * 1000:.2f} per thousand, ${savings * 1_000_000:,.0f} per million --
because cache reads bill at about 10% of the input rate.
"""

    section += f"""
The root cause in AI-generated code is almost always a system prompt assembled
inside the request function, which means it is rebuilt (and re-sent uncached) on
every call. Mark the stable prefix explicitly:

```python
response = await client.messages.create(
    model={model_literal},
    system=[{{
        "type": "text",
        "text": SYSTEM_PROMPT,          # module-level constant, never rebuilt
        "cache_control": {{"type": "ephemeral"}},
    }}],
    messages=[{{"role": "user", "content": user_input}}],
)
```

Then verify it is working: `response.usage.cache_read_input_tokens` must be
non-zero on the second and later calls. If it stays at zero, something in the
prefix is changing between requests -- a timestamp, a UUID, or a dict serialized
without `sort_keys=True` are the usual culprits.
"""
    return section


def _expensive_requests(result: ScanResult) -> str:
    metrics = _metrics(result, "cost")
    cost = metrics.get("cost_per_request", 0)
    ratio = metrics.get("io_ratio")
    model = _model_name(result)
    against = f" against `{model}`" if model else ""

    section = f"""**FINDING: ${cost:.4f} per request**

That is ${cost * 1000:.2f} per thousand calls and ${cost * 1_000_000:,.0f} per
million{against}.
"""
    if ratio and ratio > 10:
        section += f"""
Input outweighs output {ratio:.0f} to 1, so the bill is dominated by what you
send, not by what the model writes back. Before reaching for a cheaper model,
cut the prompt: retrieved context that is no longer relevant to the current
turn, few-shot examples that a current model no longer needs, and entire
conversation histories replayed when a summary would do.
"""
    return section


def _saturates_early(result: ScanResult) -> str:
    metrics = _metrics(result, "concurrency")
    point = metrics.get("saturation_point")
    sustained = metrics.get("max_sustained_concurrency", 0)

    return f"""**FINDING: saturates at {point} concurrent requests**

{_target_noun(result)} handled {sustained} concurrent callers cleanly and
started failing more than 5% of requests at {point}.

Agents fan out. A single agent turn that calls three tools in parallel, times a
handful of concurrent users, reaches this ceiling faster than a request-per-user
mental model suggests. Past saturation the failures are not graceful -- they
arrive as the timeouts and connection errors that trigger the retry storms the
behaviour phase measures.

The usual cause in AI-generated servers is a single shared client with no pool,
or a synchronous call inside an async handler that blocks the event loop for
everyone:

```python
# Blocks the whole loop - every other concurrent request waits.
data = requests.get(url).json()

# Does not.
async with httpx.AsyncClient() as client:
    data = (await client.get(url)).json()
```

If the ceiling is a genuine capacity limit rather than a bug, make it explicit
with a semaphore and a queue so callers get backpressure instead of errors.
"""


def _poor_recovery(result: ScanResult) -> str:
    metrics = _metrics(result, "behavior")
    rate = metrics.get("recovery_rate") or 0
    unrecovered = metrics.get("unrecovered", 0)
    disrupted = metrics.get("disrupted", 0)
    by_kind = metrics.get("unrecovered_by_fault_kind") or {}
    worst = max(by_kind, key=by_kind.get) if by_kind else "injected faults"

    return f"""**FINDING: {rate:.0%} recovery rate ({unrecovered} of {disrupted} \
operations never came back)**

When calls to {_target_noun(result)} were disrupted, {unrecovered} of them never
succeeded within the retry budget. The fault they most often failed to survive
was `{worst}`.

This is the number that separates a service that degrades from one that drops
work. Every unrecovered operation is a user-visible hard failure -- not a slow
response, a lost one.

The pattern to look for is retrying without regard to what failed. A `429` needs
a wait that respects `Retry-After`; a connection refusal needs a backoff long
enough for the dependency to come back; a `400` should not be retried at all.
AI-generated clients frequently retry all three identically, or set
`max_retries=0` and surface the first failure straight to the user.

```python
for attempt in range(3):
    response = await call()
    if response.ok:
        break
    if response.status == 429:
        await asyncio.sleep(float(response.headers.get("retry-after", 2 ** attempt)))
    elif response.status >= 500:
        await asyncio.sleep(2 ** attempt + random.random())   # jitter
    else:
        break            # 4xx will not fix itself
```
"""


def _retry_amplification(result: ScanResult) -> str:
    metrics = _metrics(result, "behavior")
    amp = metrics.get("retry_amplification", 0)
    attempts = metrics.get("attempts", 0)
    operations = metrics.get("trajectories", 0)
    peak = metrics.get("max_attempts_single_operation", 0)

    return f"""**FINDING: {amp:.2f}x retry amplification**

{attempts} calls were made to complete {operations} operations against
{_target_noun(result)}, peaking at {peak} attempts on a single operation.

Amplification is what turns a partial outage into a total one. When a dependency
starts failing, every caller multiplies its load on the thing that is already
struggling, which is the mechanism behind most cascading failures. The dangerous
property is that it looks fine in testing: with a healthy dependency the retries
never fire.

Two controls, both usually missing from generated code -- a cap on total
attempts, and jitter so that retries from different callers do not arrive
together:

```python
await asyncio.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
```

Above roughly 3 attempts, add a circuit breaker instead of more retries: once a
dependency has failed N times in a window, fail fast for a cooldown rather than
continuing to send traffic it cannot serve.
"""


def _duplicate_mutations(result: ScanResult) -> str:
    metrics = _metrics(result, "behavior")
    count = metrics.get("duplicate_mutations", 0)

    return f"""**FINDING: {count} operations succeeded more than once**

The same call, with identical arguments, completed successfully twice against
{_target_noun(result)}.

If any of those calls mutate state, the retry duplicated the mutation. This is
the failure mode behind double charges, duplicate rows, and the same
notification arriving twice -- and it is invisible to every metric that only
counts errors, because both attempts succeeded.

The specific trap is a timeout after the work completed: the caller never saw
the response, so it retried something that had already happened. No amount of
retry tuning fixes this. The mutation has to be idempotent:

```python
# The caller generates the key once, before the first attempt, and reuses it
# for every retry of the same logical operation.
async def charge(amount: int, idempotency_key: str) -> dict:
    if existing := await store.get(idempotency_key):
        return existing                      # already done; return the same result
    result = await do_charge(amount)
    await store.put(idempotency_key, result)
    return result
```
"""


def _stuck_loops(result: ScanResult) -> str:
    metrics = _metrics(result, "behavior")
    loops = metrics.get("loops_detected", 0)

    return f"""**FINDING: {loops} operations retried to exhaustion**

{loops} operations made three or more attempts against {_target_noun(result)}
without ever succeeding.

Retrying past the point where it can help spends the dependency's remaining
capacity on calls that were never going to land, and it spends the caller's
latency budget too -- the user waits for all of the attempts, then gets the
error anyway. Fail fast and surface a clear error instead of exhausting a retry
budget on a dependency that is down.
"""


ADVICE: tuple[Advice, ...] = (
    # Correctness first: these lose or corrupt work.
    Advice(
        "duplicate_mutations", "Duplicate mutations",
        lambda r: _metrics(r, "behavior").get("duplicate_mutations", 0) > 0,
        _duplicate_mutations, priority=10,
    ),
    Advice(
        "contract_crashes", "Crashes on malformed input",
        lambda r: _metrics(r, "contract").get("crashes", 0) > 0,
        _crashes_on_edge_cases, priority=15,
    ),
    Advice(
        "accepts_invalid", "Unvalidated input",
        lambda r: _metrics(r, "contract").get("accepted_invalid", 0) > 0,
        _accepts_invalid, priority=20,
    ),
    Advice(
        "poor_recovery", "Recovery",
        lambda r: (_metrics(r, "behavior").get("recovery_rate") or 1.0) < 0.9,
        _poor_recovery, priority=25,
    ),
    # Then availability and load.
    Advice(
        "retry_amplification", "Retry amplification",
        lambda r: _metrics(r, "behavior").get("retry_amplification", 0) > 2.0,
        _retry_amplification, priority=30,
    ),
    Advice(
        "stuck_loops", "Exhausted retries",
        lambda r: _metrics(r, "behavior").get("loops_detected", 0) > 0,
        _stuck_loops, priority=35,
    ),
    Advice(
        "saturates", "Concurrency ceiling",
        lambda r: _metrics(r, "concurrency").get("saturation_point") is not None,
        _saturates_early, priority=40,
    ),
    # Then latency and cost.
    Advice(
        "slow_p95", "Latency",
        lambda r: _failed(r, "p95_latency_ms"),
        _slow_p95, priority=45,
    ),
    Advice(
        "heavy_tail", "Latency tail",
        lambda r: (_metrics(r, "latency").get("tail_ratio") or 0) >= 3.0,
        _heavy_tail, priority=50,
    ),
    Advice(
        "prompt_bloat", "Prompt bloat",
        lambda r: bool(_metrics(r, "cost").get("bloat_detected")),
        _prompt_bloat, priority=55,
    ),
    Advice(
        "expensive", "Cost per request",
        lambda r: _failed(r, "cost_per_request_max"),
        _expensive_requests, priority=60,
    ),
)


def _failed(result: ScanResult, check_name: str) -> bool:
    check = next((c for c in result.checks if c.name == check_name), None)
    return bool(check and not check.passed and not check.skipped)


# -- rendering ----------------------------------------------------------------


def render_agents_md(result: ScanResult, previous: str | None = None) -> str:
    """Build the guide, diffed against a previous AGENTS.md when given."""
    prior = read_state(previous) if previous else None

    lines: list[str] = [
        "# AGENTS.md",
        "",
        f"Reliability guide for **{result.target.name}**, generated by RateMyAgent.",
        "",
        f"- Scanned: {result.started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Target: `{result.target.uri or result.target.name}` ({result.target.kind})",
        f"- Policy: `{result.policy_name}`",
        "",
        "## Verdict",
        "",
    ]
    lines.extend(f"> {line}" for line in verdict_lines(result))
    lines.append("")

    deltas = _delta_lines(result, prior)
    if deltas:
        lines.extend(["## Since the last scan", ""])
        lines.extend(f"- {line}" for line in deltas)
        lines.append("")

    lines.extend(_score_table(result))

    sections = [advice for advice in sorted(ADVICE, key=lambda a: a.priority)
                if _safe_applies(advice, result)]

    if not sections:
        lines.extend([
            "## Findings",
            "",
            "Nothing to fix. Every policy threshold this scan could evaluate was met.",
            "",
            "That is a statement about what was measured, not a clean bill of health:",
            "check the skipped rows above for dimensions this scan could not see.",
            "",
        ])
    else:
        lines.extend([f"## {len(sections)} things to fix", ""])
        for index, advice in enumerate(sections, start=1):
            lines.append(f"### {index}. {advice.title}")
            lines.append("")
            lines.append(advice.render(result).strip())
            lines.append("")

    lines.extend(_footer(result))
    return "\n".join(lines).rstrip() + "\n"


def _safe_applies(advice: Advice, result: ScanResult) -> bool:
    """A malformed metric must not take the whole guide down."""
    try:
        return bool(advice.applies(result))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("advice %s failed to evaluate: %s", advice.key, exc)
        return False


def _score_table(result: ScanResult) -> list[str]:
    lines = ["## Where the score went", "", "| dimension | points | note |", "|---|---|---|"]
    for label, points, note in breakdown_rows(result):
        lines.append(f"| {label} | {points} | {note or ''} |")
    lines.extend(["", "| measurement | actual | target | status |", "|---|---|---|---|"])
    for row in target_rows(result):
        lines.append(f"| {row.label} | {row.actual} | {row.target} | {row.status} |")
    lines.append("")
    return lines


def _delta_lines(result: ScanResult, prior: dict[str, Any] | None) -> list[str]:
    """Movement since the previous scan, in the terms the reader cares about."""
    if not prior:
        return []

    lines: list[str] = []

    # Deltas across different targets compare unrelated things. Say so rather
    # than quietly reporting that latency "improved" because this is a
    # different server.
    previous_target = prior.get("target")
    if previous_target and previous_target != result.target.name:
        lines.append(
            f"The previous guide was for `{previous_target}`, not "
            f"`{result.target.name}` -- the comparisons below are between two "
            "different targets."
        )

    previous_policy = prior.get("policy")
    if previous_policy and previous_policy != result.policy_name:
        lines.append(
            f"Scored against a different policy this time (`{previous_policy}` -> "
            f"`{result.policy_name}`), so the score moved for that reason too."
        )

    previous_score = prior.get("score")
    if previous_score is not None and result.score is not None:
        change = result.score - previous_score
        if abs(change) < 0.5:
            lines.append(f"Score unchanged at {result.score:.0f}/100.")
        else:
            direction = "improved" if change > 0 else "dropped"
            lines.append(
                f"Score {direction} from {previous_score:.0f} to {result.score:.0f}/100."
            )

    lines.extend(_metric_deltas(result, prior.get("metrics") or {}))
    return lines


#: (state key, probe, metric, label, formatter, lower_is_better)
_TRACKED: tuple[tuple[str, str, str, str, Callable[[float], str], bool], ...] = (
    ("p95_s", "latency", "p95_s", "p95 latency", lambda v: f"{v:.2f}s", True),
    ("error_rate", "latency", "error_rate", "error rate", lambda v: f"{v:.1%}", True),
    ("cost_per_request", "cost", "cost_per_request", "cost per request",
     lambda v: f"${v:.4f}", True),
    ("max_sustained_concurrency", "concurrency", "max_sustained_concurrency",
     "sustained concurrency", lambda v: f"{v:.0f}", False),
    ("accepted_invalid", "contract", "accepted_invalid", "schema violations",
     lambda v: f"{v:.0f}", True),
    ("crashes", "contract", "crashes", "edge-case crashes", lambda v: f"{v:.0f}", True),
    ("recovery_rate", "behavior", "recovery_rate", "recovery rate",
     lambda v: f"{v:.0%}", False),
    ("retry_amplification", "behavior", "retry_amplification", "retry amplification",
     lambda v: f"{v:.2f}x", True),
    ("duplicate_mutations", "behavior", "duplicate_mutations", "duplicate mutations",
     lambda v: f"{v:.0f}", True),
)


def _metric_deltas(result: ScanResult, previous: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    for key, probe, metric, label, fmt, lower_better in _TRACKED:
        now = _metrics(result, probe).get(metric)
        before = previous.get(key)
        if now is None or before is None:
            continue
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            continue

        if abs(now - before) < 1e-9:
            # Unchanged is worth saying only when it is still a problem.
            if _still_failing(result, metric):
                lines.append(f"{label.capitalize()} unchanged at {fmt(now)}, still failing.")
            continue

        improved = (now < before) if lower_better else (now > before)
        verb = "improved" if improved else "regressed"
        lines.append(f"{label.capitalize()} {verb} from {fmt(before)} to {fmt(now)}.")

    return lines


def _still_failing(result: ScanResult, metric: str) -> bool:
    return any(
        c.metric == metric and not c.passed and not c.skipped for c in result.checks
    )


def _footer(result: ScanResult) -> list[str]:
    state = json.dumps(build_state(result), indent=2, sort_keys=True)
    return [
        "---",
        "",
        "Regenerate with `ratemyagent scan --output agents-md`. The block below lets the "
        "next scan report what changed; leave it in place.",
        "",
        STATE_OPEN,
        state,
        STATE_CLOSE,
    ]


def build_state(result: ScanResult) -> dict[str, Any]:
    """The machine-readable snapshot embedded in the file."""
    metrics: dict[str, Any] = {}
    for key, probe, metric, _, _, _ in _TRACKED:
        value = _metrics(result, probe).get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[key] = value

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": result.target.name,
        "policy": result.policy_name,
        "score": result.score,
        "passed": result.passed,
        "metrics": metrics,
    }


def read_state(document: str) -> dict[str, Any] | None:
    """Recover the snapshot from a previous AGENTS.md, if it has one."""
    match = _STATE_RE.search(document or "")
    if not match:
        return None
    try:
        state = json.loads(match.group("json"))
    except json.JSONDecodeError:
        logger.debug("previous AGENTS.md has an unreadable state block")
        return None
    return state if isinstance(state, dict) else None


def write_agents_md(result: ScanResult, path: str | Path) -> str:
    """Render and write, diffing against whatever is already at `path`."""
    destination = Path(path)
    previous = None
    if destination.exists():
        try:
            previous = destination.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unreadable existing file
            logger.warning("could not read existing %s: %s", destination, exc)

    document = render_agents_md(result, previous)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return document
