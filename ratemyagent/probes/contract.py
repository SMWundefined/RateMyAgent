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

from ..models import ErrorKind, Grade, ProbeResult, Request, Response, ToolInfo
from .base import Probe, ProbeConfig

if TYPE_CHECKING:
    from ..targets.base import Target

logger = logging.getLogger(__name__)

LONG_STRING_LENGTH = 50_000

#: Transport-level failures. The tool did not answer; it fell over.
CRASH_KINDS = frozenset(
    {ErrorKind.CONNECTION, ErrorKind.TIMEOUT, ErrorKind.PROTOCOL, ErrorKind.UNKNOWN}
)

# Max crash rate for each grade, best first.
GRADE_THRESHOLDS: tuple[tuple[Grade, float], ...] = (
    (Grade.A, 0.0001),
    (Grade.B, 0.10),
    (Grade.C, 0.25),
    (Grade.D, 0.50),
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


class ContractTester(Probe):
    """Audits tool schemas and probes them with malformed input."""

    name = "contract"
    description = "validates tool schemas and probes them with edge-case inputs"
    phase = "baseline"

    async def run(self, target: "Target", config: ProbeConfig) -> ProbeResult:
        started = time.perf_counter()

        tools = target.list_tools()
        if not tools:
            return ProbeResult(
                probe=self.name,
                phase=self.phase,
                grade=Grade.C,
                applicable=False,
                summary="target exposes no tools to check",
                metrics={"tools": 0, "applicable": False, "cases": [], "crashes": 0},
                findings=[
                    "This target exposes no tool surface, so there is no contract to test. "
                    "The contract probe is meaningful for MCP targets."
                ],
                duration_s=time.perf_counter() - started,
            )

        schema_issues = _audit_schemas(tools)
        cases = await self._probe_edges(target, tools, config)
        metrics = _compute_metrics(tools, schema_issues, cases)

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

    def grade(self, result: ProbeResult) -> Grade:
        """Graded on crashes first, then on silently swallowing invalid input."""
        if not result.metrics.get("applicable", True):
            return Grade.C

        grade = _grade_crashes(result.metrics.get("crash_rate", 1.0))

        # Accepting input the schema forbids is a real defect, but a quieter one
        # than a crash, so it caps rather than fails.
        if result.metrics.get("accepted_invalid", 0) > 0:
            grade = Grade.worst([grade, Grade.C])
        if result.metrics.get("schema_issues"):
            grade = Grade.worst([grade, Grade.B])
        return grade

    async def _probe_edges(
        self, target: "Target", tools: list[ToolInfo], config: ProbeConfig
    ) -> list[dict[str, Any]]:
        """Send every edge case to every tool we are allowed to touch."""
        limit = config.extra.get("contract_tool_limit", 3)
        results: list[dict[str, Any]] = []

        for tool in tools[:limit]:
            required = list((tool.input_schema or {}).get("required") or [])
            baseline = _baseline_payload(tool)

            for case in EDGE_CASES:
                payload = case.build(baseline, required)
                response = await _send(target, tool.name, payload, case.name, config)
                results.append(_classify(tool.name, case, response, required))

        return results


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

    - crashed: the transport failed, so the tool never really answered
    - rejected: a clean error came back, which is correct for invalid input
    - accepted: it returned success
    """
    crashed = not response.ok and response.error_kind in CRASH_KINDS
    rejected = not response.ok and not crashed
    accepted = response.ok

    # Only a case the schema forbids can be "wrongly accepted", and only when
    # the tool actually declares required fields to violate.
    wrongly_accepted = accepted and case.should_reject and bool(required)

    return {
        "tool": tool,
        "case": case.name,
        "description": case.description,
        "outcome": "crashed" if crashed else ("rejected" if rejected else "accepted"),
        "should_reject": case.should_reject,
        "wrongly_accepted": wrongly_accepted,
        "error_kind": response.error_kind.value if response.error_kind else None,
        "latency_s": response.latency_s,
    }


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
    tools: list[ToolInfo], schema_issues: list[str], cases: list[dict[str, Any]]
) -> dict[str, Any]:
    total = len(cases)
    crashes = sum(1 for c in cases if c["outcome"] == "crashed")
    rejected = sum(1 for c in cases if c["outcome"] == "rejected")
    accepted = sum(1 for c in cases if c["outcome"] == "accepted")
    wrongly_accepted = sum(1 for c in cases if c["wrongly_accepted"])

    by_case: dict[str, str] = {}
    for case in cases:
        # Worst outcome wins when several tools ran the same case.
        rank = {"crashed": 2, "accepted": 1, "rejected": 0}
        current = by_case.get(case["case"])
        if current is None or rank[case["outcome"]] > rank[current]:
            by_case[case["case"]] = case["outcome"]

    return {
        "applicable": True,
        "tools": len(tools),
        "tools_probed": len({c["tool"] for c in cases}),
        "cases_run": total,
        "crashes": crashes,
        "crash_rate": (crashes / total) if total else 0.0,
        "rejected": rejected,
        "accepted": accepted,
        "accepted_invalid": wrongly_accepted,
        "schema_issues": schema_issues,
        "outcome_by_case": by_case,
        "cases": cases,
    }


def _summarize(metrics: dict[str, Any]) -> str:
    return (
        f"{metrics['cases_run']} edge cases across {metrics['tools_probed']} tools: "
        f"{metrics['rejected']} rejected cleanly, {metrics['accepted']} accepted, "
        f"{metrics['crashes']} crashed"
    )


def _findings(metrics: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    if metrics["crashes"]:
        crashed = [c for c in metrics["cases"] if c["outcome"] == "crashed"]
        worst = ", ".join(sorted({f"{c['case']} ({c['error_kind']})" for c in crashed})[:4])
        findings.append(
            f"{metrics['crashes']}/{metrics['cases_run']} edge cases brought the tool down "
            f"rather than returning an error: {worst}. Malformed input from a model is "
            "normal traffic, not an attack."
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
        findings.append(
            f"All {metrics['cases_run']} edge cases were handled cleanly: invalid input was "
            "rejected with an error and nothing crashed the transport."
        )

    return findings


def _grade_crashes(crash_rate: float) -> Grade:
    for grade, ceiling in GRADE_THRESHOLDS:
        if crash_rate < ceiling:
            return grade
    return Grade.F
