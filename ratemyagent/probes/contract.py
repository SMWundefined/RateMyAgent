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

from ..models import ErrorKind, ProbeResult, Request, Response, ToolInfo
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


def _null_required(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {**payload, **{key: None for key in required[:1]}} if required else {**payload}


def _empty_string(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {**payload, **{key: "" for key in required[:1]}} if required else {**payload}


def _wrong_type(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {**payload, **{key: 12345 for key in required[:1]}} if required else {**payload}


def _very_long_string(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    long = "A" * LONG_STRING_LENGTH
    return {**payload, **{key: long for key in required[:1]}} if required else {**payload}


def _missing_required(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in required}


def _extra_param(payload: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {**payload, "ratemyagent_unexpected_field": True}


EDGE_CASES: tuple[EdgeCase, ...] = (
    EdgeCase("null_required", "null in a required field", _null_required, True),
    EdgeCase("empty_string", "empty string in a required field", _empty_string, False),
    EdgeCase("wrong_type", "integer where a string is declared", _wrong_type, True),
    EdgeCase(
        "very_long_string",
        f"{LONG_STRING_LENGTH:,}-character string",
        _very_long_string,
        False,
    ),
    EdgeCase("missing_required", "required field omitted", _missing_required, True),
    EdgeCase("extra_param", "undeclared extra field", _extra_param, False),
)


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
    """Which of our edge cases this schema actually forbids.

    `accepted_invalid: 0` is only enforcement if the schema declares something to
    enforce. `htag-docs` accepted 12 of 18 edge cases and scored full marks
    because its fields are `anyOf [string, null]` with `default: null` and
    nothing required -- almost nothing we sent was a violation. The zero was the
    absence of rules, not the presence of checking.
    """
    schema = tool.input_schema or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    first = (properties.get(required[0]) or {}) if required else {}

    forbidden: list[str] = []
    if required:
        forbidden.append("missing_required")
        if not _permits_null(first):
            forbidden.append("null_required")
        if _excludes_integer(first):
            forbidden.append("wrong_type")
        if first.get("minLength", 0) >= 1 or first.get("enum") or first.get("pattern"):
            forbidden.append("empty_string")
        limit = first.get("maxLength")
        if isinstance(limit, int) and limit < LONG_STRING_LENGTH:
            forbidden.append("very_long_string")
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
    possible = len(tools) * len(EDGE_CASES)
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

        coverage = plan_coverage(
            tools,
            limit=config.extra.get("contract_tool_limit", 3),
            allow_mutating=getattr(target, "allow_mutating", False),
        )
        schema_issues = _audit_schemas(tools)
        cases = await self._probe_edges(target, coverage.probed, config)
        metrics = _compute_metrics(tools, schema_issues, cases, coverage)
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
            sample_count=len(cases),
            error_rate=metrics["crash_rate"],
            duration_s=time.perf_counter() - started,
        )

    async def _probe_edges(
        self, target: "Target", tools: list[ToolInfo], config: ProbeConfig
    ) -> list[dict[str, Any]]:
        """Send every edge case to every tool we were cleared to touch.

        `tools` is already the selected set. It used to re-read
        `contract_tool_limit` and re-slice, which is how the caller's idea of
        what was probed and this loop's idea of it could differ -- the same
        shape of bug as the gate this probe was missing.
        """
        results: list[dict[str, Any]] = []

        for tool in tools:
            required = list((tool.input_schema or {}).get("required") or [])
            baseline = _baseline_payload(tool)

            for case in EDGE_CASES:
                payload = case.build(baseline, required)
                response = await _send(target, tool.name, payload, case.name, config)
                results.append(_classify(tool.name, case, response, required))

        return results


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


def _baseline_payload(tool: ToolInfo) -> dict[str, Any]:
    """A payload the schema should accept, to mutate from."""
    from ..targets.mcp import synthesize_args

    return synthesize_args(tool.input_schema or {})


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
    tool: str, case: EdgeCase, response: Response, required: list[str]
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
        "case": case.name,
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
        "crash_rate": None if nothing_asked else (crashes / total),
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
        f"{rejected}, {metrics['accepted']} accepted, {metrics['crashes']} crashed"
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
        findings.append(
            "No edge case was run, so this scan says nothing about how this target "
            "handles bad input. Zero crashes and zero accepted violations here are "
            "the absence of a test, not the absence of a problem."
        )
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
    if metrics.get("rejected_unclassified"):
        findings.append(
            f"{metrics['rejected_unclassified']}/{metrics['cases_run']} rejections could "
            "not be attributed to a cause: this scanner's error-message table does not "
            "cover how this target words its errors. They are counted as rejections, not "
            "crashes. This measures our coverage, not the target's behaviour."
        )

    if metrics["accepted_invalid"]:
        wrong = [c for c in metrics["cases"] if c["wrongly_accepted"]]
        names = ", ".join(sorted({c["case"] for c in wrong}))
        findings.append(
            f"{metrics['accepted_invalid']} inputs the schema forbids were accepted with a "
            f"success response: {names}. The tool is not validating what it declares, so "
            "invalid data reaches whatever it writes to."
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
