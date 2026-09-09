"""ContractTester: phase 1, does the tool honour its own contract?

Two halves:

1. **Schema audit** -- read what the target advertises via list_tools() and
   check the declarations are usable: an object schema, described properties,
   required fields that actually exist.

2. **Edge-case probing** -- send inputs the schema says are invalid and watch
   what happens.

The grading distinction that matters: *rejecting* bad input is correct
behaviour, not a failure. A tool that returns "query must be a string" for a
null is doing its job. Two things are failures -- crashing the transport, and
silently accepting input its own schema forbids. The second is the quieter bug:
nothing looks wrong until the garbage reaches whatever the tool writes to.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..models import Caveat, ErrorKind, ProbeResult, Request, Response, ToolInfo
from .base import Probe, ProbeConfig, ScanContext

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

LONG_STRING_LENGTH = 50_000


#: Transport-level failure kinds, used to *label* a crash once `delivered` has
#: already decided that one happened. Never used to make that decision.
#:
#: It used to be the decision, and that was the bug. UNKNOWN is in here because
#: `classify_exception()` falls back to it for transport deaths it cannot name,
#: which is correct -- but MCPTarget also tagged UNKNOWN onto *delivered* errors
#: whose wording it did not recognise, so correct rejections were graded as dead
#: transports. Two published MCP servers were reported upstream for crashes they
#: do not have. Crash detection now reads `Response.delivered`, which is a fact
#: about whether anything arrived rather than an opinion about what it said.
CRASH_KINDS = frozenset(
    {ErrorKind.CONNECTION, ErrorKind.TIMEOUT, ErrorKind.PROTOCOL, ErrorKind.UNKNOWN}
)

@dataclass(frozen=True)
class EdgeCase:
    """One malformed payload and what it is meant to expose."""

    name: str
    description: str
    build: Callable[[dict[str, Any], list[str]], dict[str, Any]]
    #: True when a schema-honouring tool should refuse this outright.
    should_reject: bool


#: Mutations applied to one required field at a time. Per field, because the
#: question is *which* field the handler fails to validate, and corrupting
#: everything at once cannot answer it -- a handler that checks only the first
#: field still rejects, and the unchecked second field stays invisible.
FIELD_MUTATIONS: tuple[tuple[str, str, Any, bool], ...] = (
    ("null_required", "null in a required field", None, True),
    ("empty_string", "empty string in a required field", "", False),
    ("wrong_type", "integer where a string is declared", 12345, True),
    (
        "very_long_string",
        f"{LONG_STRING_LENGTH:,}-character string",
        "A" * LONG_STRING_LENGTH,
        False,
    ),
)


@dataclass(frozen=True)
class Case:
    """One malformed payload, and which field it corrupts.

    Until 0.1.15 four of these mutated `required[:1]` and `missing_required`
    removed every required field -- five builders following one rule and one
    following another, which is what identified the narrowing as an accident
    rather than a design. On a multi-field tool that left every field but the
    first untested: `write_file`'s `content` had never been sent a null, a wrong
    type, an empty string or a long value on any scan.
    """

    kind: str
    field: str | None
    description: str
    payload: dict[str, Any]
    should_reject: bool

    @property
    def label(self) -> str:
        return f"{self.kind}[{self.field}]" if self.field else self.kind


def build_cases(baseline: dict[str, Any], required: list[str]) -> list[Case]:
    """Every distinct malformed payload for one tool.

    `5N + 2` for two or more required fields, and **6 at N=1**, which is the
    compatibility property: on a single-field tool the per-field omission and
    the omit-everything case are the same payload, so they collapse and this
    reduces exactly to the six cases every published scan used.

    **One at N=0.** A tool with no required fields has nothing to null, empty,
    retype or lengthen, and the old builders returned the untouched baseline for
    all four -- so `list_pages` received `{}` five times and each was counted as
    a separate edge case. Five identical calls, five entries in the outcome
    table, one distinct payload. `read_graph` on `server-memory` reported "6
    accepted" for two distinct calls. Cases are distinct payloads now, so a tool
    with nothing required is probed once, with the undeclared extra field.
    """
    cases: list[Case] = []

    for field in required:
        for kind, description, value, should_reject in FIELD_MUTATIONS:
            cases.append(Case(
                kind=kind,
                field=field,
                description=description,
                payload={**baseline, field: value},
                should_reject=should_reject,
            ))
        cases.append(Case(
            kind="missing_required",
            field=field,
            description="required field omitted",
            payload={k: v for k, v in baseline.items() if k != field},
            should_reject=True,
        ))

    if len(required) >= 2:
        # A different question from omitting one: does it require anything at
        # all? The only case that catches a handler with no required-field
        # checking whatsoever. At N=1 it is byte-identical to the omission
        # above, so it is not emitted twice.
        cases.append(Case(
            kind="missing_all_required",
            field=None,
            description="every required field omitted",
            payload={k: v for k, v in baseline.items() if k not in required},
            should_reject=True,
        ))

    cases.append(Case(
        kind="extra_param",
        field=None,
        description="undeclared extra field",
        payload={**baseline, "ratemyagent_unexpected_field": True},
        should_reject=False,
    ))
    return cases


def case_count(required: list[str]) -> int:
    """How many distinct cases a schema with these required fields produces."""
    n = len(required)
    if n == 0:
        return 1
    if n == 1:
        return 6
    return 5 * n + 2


def _permits_null(spec: dict[str, Any]) -> bool:
    declared = spec.get("type")
    if declared is None and "anyOf" not in spec:
        return True  # nothing declared, so nothing excluded
    if isinstance(declared, list):
        return "null" in declared
    if declared == "null":
        return True
    return any(
        (branch or {}).get("type") == "null" for branch in spec.get("anyOf") or []
    )


def _excludes_integer(spec: dict[str, Any]) -> bool:
    declared = spec.get("type")
    branches = [(b or {}).get("type") for b in spec.get("anyOf") or []]
    types = {declared} if isinstance(declared, str) else set(declared or [])
    types |= {b for b in branches if b}
    return bool(types) and not (types & {"integer", "number"})


def declarable_violations(tool: ToolInfo) -> list[str]:
    """Which of the cases we send this schema actually forbids.

    `accepted_invalid: 0` is only enforcement if the schema declares something to
    enforce. `htag-docs` accepted 12 of 18 edge cases and scored full marks
    because its fields are `anyOf [string, null]` with `default: null` and
    nothing required -- almost nothing we sent was a violation. The zero was the
    absence of rules, not the presence of checking.

    Per field since 0.1.15, mirroring `build_cases()`. It used to read
    `properties[required[0]]` alone, so a three-field tool reported the
    strictness of its first field under a label naming the tool. No published
    figure moved when this changed -- every tool measured so far has zero or one
    required field, which is exactly the uniformity that hid the narrowing.
    """
    schema = tool.input_schema or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    forbidden: list[str] = []
    for field in required:
        spec = properties.get(field) or {}
        forbidden.append(f"missing_required[{field}]")
        if not _permits_null(spec):
            forbidden.append(f"null_required[{field}]")
        if _excludes_integer(spec):
            forbidden.append(f"wrong_type[{field}]")
        if spec.get("minLength", 0) >= 1 or spec.get("enum") or spec.get("pattern"):
            forbidden.append(f"empty_string[{field}]")
        limit = spec.get("maxLength")
        if isinstance(limit, int) and limit < LONG_STRING_LENGTH:
            forbidden.append(f"very_long_string[{field}]")
    if len(required) >= 2:
        forbidden.append("missing_all_required")
    if schema.get("additionalProperties") is False:
        forbidden.append("extra_param")
    return forbidden


def schema_strictness(tools: list[ToolInfo]) -> float | None:
    """Share of the edge cases we send that these schemas actually forbid.

    Reported, never scored. A permissive schema is a design choice, and this
    project has already retracted one public claim made by scoring something
    that was not the target's fault. It earns a threshold when a survey shows it
    predicting real failures, not before.
    """
    if not tools:
        return None
    possible = sum(
        case_count(list((t.input_schema or {}).get('required') or [])) for t in tools
    )
    return sum(len(declarable_violations(t)) for t in tools) / possible if possible else None


class ContractTester(Probe):
    """Audits tool schemas and probes them with malformed input."""

    name = "contract"
    description = "validates tool schemas and probes them with edge-case inputs"
    phase = "baseline"

    async def run(
        self, target: "Target", config: ProbeConfig,
        context: ScanContext | None = None,
    ) -> ProbeResult:
        started = time.perf_counter()

        tools = target.list_tools()
        if not tools:
            return ProbeResult(
                probe=self.name,
                phase=self.phase,
                applicable=False,
                summary="target exposes no tools to check",
                metrics={"tools": 0, "applicable": False, "cases": [], "crashes": 0},
                findings=[
                    "This target exposes no tool surface, so there is no contract to test. "
                    "The contract probe is meaningful for MCP targets."
                ],
                duration_s=time.perf_counter() - started,
            )

        real = read_real_args(target)
        coverage = plan_coverage(
            tools,
            limit=config.extra.get("contract_tool_limit", 3),
            allow_mutating=getattr(target, "allow_mutating", False),
        )
        schema_issues = _audit_schemas(tools)
        cases, control = await self._probe_edges(
            target, coverage.probed, config, real
        )
        metrics = _compute_metrics(
            tools, schema_issues, cases, coverage, control, real
        )
        metrics["schema_strictness"] = schema_strictness(coverage.probed)
        metrics["declarable_by_tool"] = {
            t.name: declarable_violations(t) for t in coverage.probed
        }

        return ProbeResult(
            probe=self.name,
            phase=self.phase,
            summary=_summarize(metrics),
            metrics=metrics,
            findings=_findings(metrics),
            caveats=_caveats(metrics),
            sample_count=len(cases),
            # The observed rate either way: this field describes what the probe
            # saw, not what the policy scored.
            error_rate=(
                metrics["crash_rate"]
                if metrics["crash_rate"] is not None
                else (metrics["unscored_crash_rate"] or 0.0)
            ),
            duration_s=time.perf_counter() - started,
        )

    async def _probe_edges(
        self,
        target: "Target",
        tools: list[ToolInfo],
        config: ProbeConfig,
        real: "RealArgs | None" = None,
    ) -> tuple[list[dict[str, Any]], "Control"]:
        """Send every edge case to every tool we were cleared to touch.

        Each malformed call is paired with a **control** call: the well-formed
        baseline payload, same tool, same session, immediately before. The
        control answers the question the crash test cannot answer alone --
        `not response.delivered` says the session stopped answering, never why.
        A session stops answering for reasons that have nothing to do with the
        input, and without a control every one of those is charged to the input.

        The control measures **delivery, not success**. A well-formed call the
        server rejects on its merits still proves the transport carried it, so
        the control works with synthesized arguments and needs no valid ones.
        That matters: this probe has never read `--tool-args` (see NextSteps),
        so valid arguments are not available to it in this release.

        Interleaved 1:1 rather than sampled at the start, because a target that
        dies partway through would otherwise pass a control taken before it
        died. Same reason the pairs are adjacent: the two calls have to be close
        enough in time that "the target was fine a moment ago" means something.

        `tools` is already the selected set. It used to re-read
        `contract_tool_limit` and re-slice, which is how the caller's idea of
        what was probed and this loop's idea of it could differ -- the same
        shape of bug as the gate this probe was missing.
        """
        results: list[dict[str, Any]] = []
        control_calls = 0
        control_undelivered = 0

        for tool in tools:
            required = list((tool.input_schema or {}).get("required") or [])
            baseline = _baseline_payload(tool, real)

            for case in build_cases(baseline, required):
                control = await _send(
                    target, tool.name, dict(baseline), "control", config
                )
                control_calls += 1
                if not control.delivered:
                    control_undelivered += 1

                response = await _send(
                    target, tool.name, case.payload, case.label, config
                )
                results.append(_classify(tool.name, case, response, required))

        return results, Control(calls=control_calls, undelivered=control_undelivered)


@dataclass(frozen=True)
class Control:
    """Well-formed calls sent alongside the malformed ones, to attribute crashes.

    Two questions, one number:

    - **Liveness.** Did anything come back at all? If no control call was
      delivered, the session was not answering and nothing observed in this
      probe can be attributed to any input. Unconditional, free, and available
      on every path -- which is the point, because the control it replaces
      (`baseline_error_rate` from the latency probe) reached this probe only on
      the default probe ordering and vanished silently on `--probes contract`,
      leaving a crash rate that still printed and still capped the score at 49.
    - **Rate.** What share of well-formed calls also failed to arrive? A target
      dropping a third of everything will drop roughly a third of the malformed
      payloads too, and charging those to the input is a fabricated finding.

    `clean` is the condition for scoring: every control call came back, so a
    non-delivery on a malformed call is a fact about that call.
    """

    calls: int
    undelivered: int

    @property
    def clean(self) -> bool:
        return self.calls > 0 and self.undelivered == 0

    @property
    def alive(self) -> bool:
        """Something answered. The weaker claim, and the one liveness gates on."""
        return self.calls > 0 and self.undelivered < self.calls

    @property
    def undelivered_rate(self) -> float | None:
        return (self.undelivered / self.calls) if self.calls else None


@dataclass(frozen=True)
class Coverage:
    """Which tools this probe is allowed to send garbage to, and why not the rest.

    Edge-case probing invokes a tool with deliberately bad input, six times.
    Against a read-only tool that is a measurement; against a write tool it is
    six writes. The 0.1.6 gate established that rule and applied it to
    `_select_probe_tool` only, so this probe went on sending eighteen payloads
    to whatever the first three tools happened to be -- on
    `@modelcontextprotocol/server-memory` that is `create_entities`,
    `create_relations` and `add_observations`, all three declaring
    `readOnlyHint=false`. The declaration was there on every scan. Nothing read
    it. Fifth time a fix has been applied to one branch and not its twin, and
    the first where the twin was a safety gate rather than a scoring one.

    Eligibility is decided *before* the cap, not after. Taking the first three
    tools and then discarding the unsafe ones would probe nothing at all on
    server-memory while six read-only tools sat further down the list. The
    same order `_select_probe_tool` already uses: find what is safe, then take
    from it.
    """

    probed: list[ToolInfo]
    excluded_mutating: list[str]
    excluded_unknown: list[str]
    eligible: int
    total: int
    limit: int

    @property
    def capped(self) -> int:
        """Eligible tools left unprobed by the cap, not by their mutability.

        Kept apart from the exclusions on purpose: "past the cap" is a knob the
        caller can turn, "skipped as mutating" is a safety decision, and folding
        them into one number would hide which of the two a reader can do
        something about.
        """
        return max(0, self.eligible - len(self.probed))


def plan_coverage(
    tools: list[ToolInfo], limit: int, allow_mutating: bool
) -> Coverage:
    """Pick the tools to probe. Read-only unless the caller opted in."""
    from ..targets.mutability import Mutability, classify

    if allow_mutating:
        eligible = list(tools)
        excluded_mutating: list[str] = []
        excluded_unknown: list[str] = []
    else:
        eligible = []
        excluded_mutating = []
        excluded_unknown = []
        for tool in tools:
            verdict = classify(tool)
            if verdict is Mutability.READ_ONLY:
                eligible.append(tool)
            elif verdict is Mutability.MUTATING:
                excluded_mutating.append(tool.name)
            else:
                # Unclassified is not the same as safe -- the distinction the
                # whole mutability module exists for. `_select_probe_tool`
                # allows an explicitly named unknown tool because naming one is
                # a human choice; there is no explicit selection here, so there
                # is nothing to defer to.
                excluded_unknown.append(tool.name)

    return Coverage(
        probed=eligible[:limit],
        excluded_mutating=excluded_mutating,
        excluded_unknown=excluded_unknown,
        eligible=len(eligible),
        total=len(tools),
        limit=limit,
    )


def _baseline_payload(tool: ToolInfo, real: "RealArgs | None" = None) -> dict[str, Any]:
    """A payload the schema should accept, to mutate from.

    Prefers arguments the user actually supplied. `--tool-args` is documented as
    the escape hatch from synthesized arguments -- the README says so three times
    and section 9 splits `mcp-server-git` into a synthesized row and a real one on
    exactly that distinction -- and this probe called `synthesize_args()`
    unconditionally for its whole life, so four of nine published rows passed real
    arguments and had them discarded.

    Real arguments attach to the tool they name and to nothing else. Pulling the
    named tool into the probe set instead would make `--tool-args` quietly change
    which tools get probed -- one flag doing two things, one of them unstated --
    and against a mutating tool it would undo the 0.1.11 gate. Same
    multiply-rather-than-compose problem as `--allow-mutating` with
    `--contract-tools`.
    """
    from ..targets.mcp import synthesize_args

    if real is not None and real.tool == tool.name:
        return dict(real.args)
    return synthesize_args(tool.input_schema or {})


@dataclass(frozen=True)
class RealArgs:
    """Arguments a human vouched for, and which tool they were vouched for."""

    tool: str
    args: dict[str, Any]


def read_real_args(target: "Target") -> "RealArgs | None":
    """User-supplied `--tool-args`, or None when the payload was invented.

    Read through `describe()` rather than off a private attribute: the adapter
    already publishes `probe_args`, and the only thing missing was whether that
    dict came from a person or from `synthesize_args`. The two render identically
    and mean opposite things.
    """
    try:
        metadata = target.describe().metadata or {}
    except Exception:  # pragma: no cover - describe() is not supposed to raise
        return None

    if metadata.get("probe_args_source") != "user":
        return None
    tool = metadata.get("probe_tool")
    args = metadata.get("probe_args")
    if not tool or not isinstance(args, dict):
        return None
    return RealArgs(tool=tool, args=dict(args))


async def _send(
    target: "Target", tool: str, payload: dict[str, Any], case: str, config: ProbeConfig
) -> Response:
    request = Request(
        op=tool,
        payload=payload,
        timeout_s=config.timeout_s,
        label=f"contract:{tool}:{case}",
    )
    started = time.perf_counter()
    try:
        return await target.invoke(request)
    except Exception as exc:
        from ..targets.base import error_response

        logger.debug("contract probe: %s raised on %s: %s", tool, case, exc)
        return error_response(exc, time.perf_counter() - started)


def _classify(
    tool: str, case: "Case", response: Response, required: list[str]
) -> dict[str, Any]:
    """Decide what the response says about the tool's handling.

    - crashed: nothing came back, so the tool never really answered
    - rejected: an error came back, which is correct for invalid input
    - accepted: it returned success

    The crash test is `not response.delivered` and nothing else. A response
    that arrived proves the transport carried it, whatever it says; only a
    response built from a raised exception means the call never landed.
    """
    crashed = not response.delivered
    rejected = response.delivered and not response.ok
    accepted = response.ok

    # The target rejected it, but our substring table could not say why. That
    # is a fact about this scanner's coverage, not about the target, and it is
    # reported rather than folded into either column: folding it into `crashed`
    # is the retracted bug, and folding it silently into `rejected` hides how
    # much of the table's confidence is real.
    unclassified = rejected and bool(response.meta.get("reason_unclassified"))

    # Only a case the schema forbids can be "wrongly accepted", and only when
    # the tool actually declares required fields to violate.
    wrongly_accepted = accepted and case.should_reject and bool(required)

    return {
        "tool": tool,
        # `case` stays the kind so `outcome_by_case` keeps the shape readers
        # already parse; `field` is additive and is what localises a failure to
        # one argument rather than to a tool.
        "case": case.kind,
        "field": case.field,
        "label": case.label,
        "description": case.description,
        "outcome": "crashed" if crashed else ("rejected" if rejected else "accepted"),
        # Additive: `outcome` keeps its three existing values because
        # `outcome_by_case` feeds a rank table that callers may rely on.
        "reason_unclassified": unclassified,
        "should_reject": case.should_reject,
        "wrongly_accepted": wrongly_accepted,
        "error_kind": response.error_kind.value if response.error_kind else None,
        "latency_s": response.latency_s,
    }


def _strictness_clause(metrics: dict[str, Any]) -> str:
    """Say how much the schema declares, so a zero can be read correctly."""
    strictness = metrics.get("schema_strictness")
    if strictness is None:
        return "."

    declarable = sum(len(v) for v in (metrics.get("declarable_by_tool") or {}).values())
    possible = metrics["cases_run"]
    if strictness >= 0.75:
        return (
            f" -- it declares {declarable} of {possible} testable constraints, so most of "
            "what we sent was genuinely legal input."
        )
    return (
        f". This schema declares only {declarable} of {possible} testable constraints, so "
        "a zero here is the absence of rules rather than enforcement: there was little to "
        "violate."
    )

def _audit_schemas(tools: list[ToolInfo]) -> list[str]:
    """Static problems in the advertised schemas."""
    issues: list[str] = []

    for tool in tools:
        schema = tool.input_schema or {}
        if not schema:
            issues.append(f"{tool.name}: declares no input schema at all")
            continue

        if schema.get("type") not in (None, "object"):
            issues.append(f"{tool.name}: input schema type is {schema.get('type')!r}, not object")

        properties = schema.get("properties") or {}
        required = schema.get("required") or []

        for field in required:
            if field not in properties:
                issues.append(f"{tool.name}: requires {field!r}, which is not in properties")

        for name, spec in properties.items():
            if not isinstance(spec, dict):
                issues.append(f"{tool.name}: property {name!r} is not a schema object")
                continue
            if "type" not in spec and "enum" not in spec and "anyOf" not in spec:
                issues.append(f"{tool.name}: property {name!r} declares no type")

        if not tool.description:
            issues.append(f"{tool.name}: has no description for the model to read")

    return issues


def _compute_metrics(
    tools: list[ToolInfo],
    schema_issues: list[str],
    cases: list[dict[str, Any]],
    coverage: "Coverage",
    control: "Control",
    real: "RealArgs | None" = None,
) -> dict[str, Any]:
    total = len(cases)
    crashes = sum(1 for c in cases if c["outcome"] == "crashed")
    rejected = sum(1 for c in cases if c["outcome"] == "rejected")
    accepted = sum(1 for c in cases if c["outcome"] == "accepted")
    wrongly_accepted = sum(1 for c in cases if c["wrongly_accepted"])
    unclassified = sum(1 for c in cases if c.get("reason_unclassified"))

    by_case: dict[str, str] = {}
    for case in cases:
        # Worst outcome wins when several tools ran the same case.
        rank = {"crashed": 2, "accepted": 1, "rejected": 0}
        current = by_case.get(case["case"])
        if current is None or rank[case["outcome"]] > rank[current]:
            by_case[case["case"]] = case["outcome"]

    # Nothing was asked, so nothing can be concluded from nothing going wrong.
    # Skipping mutating and unclassified tools can empty the probe set outright
    # -- a server whose every tool is a write tool now runs zero edge cases --
    # and `0 crashes, 0 accepted_invalid` over zero cases is a clean bill drawn
    # from an empty sample. That is the rule in section 8b, and this change is
    # exactly the kind that creates a fresh instance of it.
    nothing_asked = total == 0

    # A crash rate is only a statement about the input when well-formed calls to
    # the same tool came back. When they did not, the number is real and its
    # attribution is not, so it is reported and not scored -- the shape used for
    # every other unmeasurable metric here. `contract_crash_rate_max` is an
    # absolute check that caps the composite at 49, so scoring an unattributable
    # crash rate is not a rounding error.
    attributable = control.clean and not nothing_asked
    raw_crash_rate = None if nothing_asked else (crashes / total)

    return {
        "applicable": True,
        "tools": len(tools),
        "tools_probed": len({c["tool"] for c in cases}),
        "tools_eligible": coverage.eligible,
        "tools_skipped_mutating": coverage.excluded_mutating,
        "tools_skipped_unknown": coverage.excluded_unknown,
        "tools_capped": coverage.capped,
        "tools_limit": coverage.limit,
        # Scalars beside the name lists, so the report table can state the whole
        # denominator without making a reader subtract two numbers to find it.
        "tools_skipped_unsafe": (
            len(coverage.excluded_mutating) + len(coverage.excluded_unknown)
        ),
        "full_coverage": len({c["tool"] for c in cases}) == len(tools),
        "cases_run": total,
        "crashes": crashes,
        "crash_rate": raw_crash_rate if attributable else None,
        # Preserved rather than dropped: withholding a number silently is its
        # own small lie, and a reader needs it to judge the control.
        "unscored_crash_rate": None if attributable else raw_crash_rate,
        "crash_attributable": attributable,
        # Which tool, if any, was probed with arguments a human supplied -- and
        # loudly, when those arguments named a tool this probe never touches.
        "real_args_tool": real.tool if real else None,
        "real_args_applied": bool(
            real and any(t.name == real.tool for t in coverage.probed)
        ),
        "tools_probed_names": [t.name for t in coverage.probed],
        # Required fields actually corrupted, one at a time. Before 0.1.15 this
        # was always at most one per tool however many the schema declared, and
        # nothing said so.
        "required_fields_probed": sorted(
            {f"{c['tool']}.{c['field']}" for c in cases if c.get("field")}
        ),
        "control_calls": control.calls,
        "control_undelivered": control.undelivered,
        "control_undelivered_rate": control.undelivered_rate,
        "session_answered": control.alive,
        # `rejected` stays the total, so no existing number moves. Clean
        # rejections are `rejected - rejected_unclassified`.
        "rejected": rejected,
        "rejected_unclassified": unclassified,
        "accepted": accepted,
        "accepted_invalid": None if nothing_asked else wrongly_accepted,
        "schema_issues": schema_issues,
        "outcome_by_case": by_case,
        "cases": cases,
    }


def _summarize(metrics: dict[str, Any]) -> str:
    unclassified = metrics.get("rejected_unclassified", 0)
    if unclassified:
        # Keep the three counts summing to cases_run. "9 rejected cleanly
        # (9 unclassified)" reads as nine rejections when there were eighteen.
        clean = metrics["rejected"] - unclassified
        rejected = f"{metrics['rejected']} rejected ({clean} cleanly, {unclassified} unclassified)"
    else:
        rejected = f"{metrics['rejected']} rejected cleanly"

    if metrics["cases_run"] == 0:
        return _describe_no_coverage(metrics)

    return (
        f"{metrics['cases_run']} edge cases across "
        f"{_coverage_phrase(metrics)}: "
        f"{rejected}, {metrics['accepted']} accepted, "
        f"{metrics['crashes']} crashed{_control_phrase(metrics)}"
    )


def _control_phrase(metrics: dict[str, Any]) -> str:
    """What the control said, whenever it changes how the crash count reads."""
    if metrics.get("crash_attributable"):
        return ""
    if not metrics.get("session_answered"):
        return " (session never answered; crashes not attributed to input)"
    undelivered = metrics.get("control_undelivered") or 0
    calls = metrics.get("control_calls") or 0
    return (
        f" ({undelivered}/{calls} well-formed control calls also failed to "
        "arrive; crashes not attributed to input)"
    )


def _coverage_phrase(metrics: dict[str, Any]) -> str:
    """"3 of 12 tools, 6 skipped as mutating" -- never a bare "3 tools".

    The bare form was read as the server's tool count for the entire life of
    the probe, including in a document whose only purpose was stating
    denominators.
    """
    probed, total = metrics["tools_probed"], metrics["tools"]
    if probed == total:
        return f"{probed} tools" if probed != 1 else "1 tool"

    reasons = []
    if metrics["tools_skipped_mutating"]:
        reasons.append(f"{len(metrics['tools_skipped_mutating'])} skipped as mutating")
    if metrics["tools_skipped_unknown"]:
        reasons.append(
            f"{len(metrics['tools_skipped_unknown'])} skipped as unclassified"
        )
    if metrics["tools_capped"]:
        reasons.append(
            f"{metrics['tools_capped']} past the {metrics['tools_limit']}-tool cap"
        )

    tail = f" ({', '.join(reasons)})" if reasons else ""
    return f"{probed} of {total} tools{tail}"


def _describe_no_coverage(metrics: dict[str, Any]) -> str:
    return (
        f"no edge cases run: none of this target's {metrics['tools']} tools are "
        f"known to be read-only{_no_coverage_tail(metrics)}"
    )


def _no_coverage_tail(metrics: dict[str, Any]) -> str:
    parts = []
    if metrics["tools_skipped_mutating"]:
        parts.append(f"{len(metrics['tools_skipped_mutating'])} mutating")
    if metrics["tools_skipped_unknown"]:
        parts.append(f"{len(metrics['tools_skipped_unknown'])} unclassified")
    return f" ({', '.join(parts)})" if parts else ""


def _caveats(metrics: dict[str, Any]) -> list[Caveat]:
    """What this probe could not establish about the target.

    Seven of them, the largest set of any probe -- which is not a coincidence:
    contract has the most measurement machinery, and machinery is what needs
    qualifying. Three are probe-scoped, because coverage and argument
    provenance qualify every number this probe reports rather than one of them.
    """
    caveats: list[Caveat] = []
    probe = "contract"

    real_tool = metrics.get("real_args_tool")
    probed_names = metrics.get("tools_probed_names") or []
    if real_tool and not metrics.get("real_args_applied"):
        caveats.append(Caveat(
            probe=probe, metrics=(), scope="probe", effect="annotate",
            reason=(
                f"--tool-args named {real_tool!r}, which this probe does not "
                f"cover. Every case here used arguments synthesized from the "
                f"schemas of {', '.join(probed_names) or 'no tools'}."
            ),
        ))
    elif real_tool and [n for n in probed_names if n != real_tool]:
        others = [n for n in probed_names if n != real_tool]
        caveats.append(Caveat(
            probe=probe, metrics=(), scope="probe", effect="annotate",
            reason=(
                f"Only {real_tool!r} used the arguments you supplied; "
                f"{', '.join(others)} used synthesized ones. A tool that rejects "
                "a placeholder rejects every case built on it, so those results "
                "describe the rejection path rather than the handler."
            ),
            remedy="--tool-args for each probed tool",
        ))

    skipped = (metrics.get("tools_skipped_mutating") or []) + (
        metrics.get("tools_skipped_unknown") or []
    )
    if metrics.get("tools_probed", 0) < metrics.get("tools", 0):
        detail = f", {len(skipped)} skipped as unsafe to probe" if skipped else ""
        caveats.append(Caveat(
            probe=probe, metrics=(), scope="probe", effect="annotate",
            reason=(
                f"{metrics['tools_probed']} of {metrics['tools']} tools were "
                f"probed{detail}. Every contract number here is about those "
                "tools, not about the server."
            ),
            remedy="--allow-mutating" if skipped else None,
        ))

    if metrics.get("cases_run") == 0:
        caveats.append(Caveat(
            probe=probe, metrics=("crash_rate", "accepted_invalid"),
            effect="suppress",
            reason=(
                "No edge case was run, so nothing here says how this target "
                "handles bad input. Zero crashes over zero calls is the absence "
                "of a test, not the absence of a problem."
            ),
        ))
    elif not metrics.get("crash_attributable"):
        if not metrics.get("session_answered"):
            reason = (
                f"The session stopped answering: none of the "
                f"{metrics['control_calls']} well-formed control calls came back "
                "either, so the unanswered edge cases are not attributable to "
                "the input."
            )
        else:
            reason = (
                f"{metrics['control_undelivered']} of {metrics['control_calls']} "
                "well-formed control calls also failed to arrive, so this target "
                "drops calls regardless of what is sent."
            )
        caveats.append(Caveat(
            probe=probe, metrics=("crash_rate",), effect="suppress", reason=reason,
        ))

    if metrics.get("rejected_unclassified"):
        caveats.append(Caveat(
            probe=probe, metrics=("accepted_invalid",), effect="annotate",
            reason=(
                f"{metrics['rejected_unclassified']} of {metrics['cases_run']} "
                "rejections could not be attributed to a cause: this scanner's "
                "error-message table does not cover how this target words its "
                "errors. That measures our coverage, not the target's behaviour."
            ),
        ))

    strictness = metrics.get("schema_strictness")
    if strictness is not None and strictness < 0.5 and metrics.get("cases_run"):
        caveats.append(Caveat(
            probe=probe, metrics=("accepted_invalid",), effect="annotate",
            reason=(
                f"These schemas declare only {strictness:.0%} of what the edge "
                "cases test, so most of what was accepted was never forbidden. "
                "A zero here is the absence of rules, not the presence of "
                "checking."
            ),
        ))

    return caveats


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    skipped_mutating = metrics.get("tools_skipped_mutating") or []
    skipped_unknown = metrics.get("tools_skipped_unknown") or []
    if skipped_mutating or skipped_unknown:
        # Name the tools, not just the count. "2 skipped as mutating" tells a
        # reader the number is incomplete; naming `write_file` tells them what
        # was never tested.
        clauses = []
        if skipped_mutating:
            clauses.append(
                f"{len(skipped_mutating)} skipped as mutating "
                f"({', '.join(sorted(skipped_mutating)[:5])})"
            )
        if skipped_unknown:
            clauses.append(
                f"{len(skipped_unknown)} skipped as unclassified "
                f"({', '.join(sorted(skipped_unknown)[:5])})"
            )
        findings.append(
            f"Edge-case probing covered {metrics['tools_probed']} of "
            f"{metrics['tools']} tools: " + "; ".join(clauses) + ". Probing calls a "
            "tool with deliberately bad input six times, so a write tool would be "
            "written to six times. Pass --allow-mutating to include them, against a "
            "target you can afford to have written to."
        )

    if metrics["cases_run"] == 0:
        if metrics["schema_issues"]:
            findings.append(
                f"{len(metrics['schema_issues'])} schema problems found by reading the "
                "declarations, which needs no calls: "
                + "; ".join(metrics["schema_issues"][:5]) + "."
            )
        return findings

    if metrics["crashes"]:
        crashed = [c for c in metrics["cases"] if c["outcome"] == "crashed"]
        worst = ", ".join(sorted({f"{c['case']} ({c['error_kind']})" for c in crashed})[:4])
        findings.append(
            f"{metrics['crashes']}/{metrics['cases_run']} edge cases brought the tool down "
            f"rather than returning an error: {worst}. Malformed input from a model is "
            "normal traffic, not an attack."
        )

    # TODO(0.1.4 group B): move this onto the evidence-caveats channel once the
    # caveat plumbing lands. It is a caveat about our own measurement, not a
    # finding about the target, and it should render beside the contract row
    # rather than in the findings list. Landing it plainly now so group Z does
    # not block on group B.
    if metrics["accepted_invalid"]:
        wrong = [c for c in metrics["cases"] if c["wrongly_accepted"]]
        # Named by field, not just by case kind. "null_required was accepted"
        # tells you a tool is not validating; "null_required[content] was
        # accepted while null_required[path] was rejected" tells you which
        # argument to go and fix, which is the whole reason the cases are
        # generated per field.
        names = ", ".join(sorted({c.get("label") or c["case"] for c in wrong}))
        fields = sorted({c["field"] for c in wrong if c.get("field")})
        localised = (
            f" Every accepted violation is on {fields[0]!r}."
            if len(fields) == 1
            else (f" Affected arguments: {', '.join(fields)}." if fields else "")
        )
        findings.append(
            f"{metrics['accepted_invalid']} inputs the schema forbids were accepted with a "
            f"success response: {names}.{localised} The tool is not validating what it "
            "declares, so invalid data reaches whatever it writes to."
        )

    if metrics["schema_issues"]:
        shown = metrics["schema_issues"][:5]
        findings.append(
            f"{len(metrics['schema_issues'])} schema problems found: " + "; ".join(shown) + "."
        )

    if not metrics["crashes"] and not metrics["accepted_invalid"]:
        unclassified = metrics.get("rejected_unclassified", 0)
        if unclassified:
            # Do not issue a clean bill for cases we could not read. "All N
            # handled cleanly" beside "M could not be attributed to a cause" is
            # the same overclaim this release exists to remove.
            findings.append(
                f"Nothing crashed the transport and no schema violation was accepted. "
                f"{metrics['rejected'] - unclassified} of {metrics['rejected']} rejections "
                f"were attributed to a cause; the remaining {unclassified} were not, so "
                "this is not a clean bill for every case."
            )
        else:
            accepted = metrics["accepted"]
            if accepted:
                # "All N handled cleanly: invalid input was rejected" is false
                # the moment anything was accepted. Say what happened, and say
                # why it was not a violation.
                findings.append(
                    f"{metrics['rejected']} of {metrics['cases_run']} edge cases were "
                    f"rejected and nothing crashed the transport. The other {accepted} "
                    f"were accepted, and none violated this schema{_strictness_clause(metrics)}"
                )
            else:
                findings.append(
                    f"All {metrics['cases_run']} edge cases were handled cleanly across "
                    f"{_coverage_phrase(metrics)}: invalid "
                    "input was rejected with an error and nothing crashed the transport."
                )

    return findings
