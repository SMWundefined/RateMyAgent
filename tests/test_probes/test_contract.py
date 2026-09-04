"""ContractTester: schema audit and edge-case handling."""

from __future__ import annotations

from ratemyagent.models import ToolInfo
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import (
    EDGE_CASES,
    LONG_STRING_LENGTH,
    ContractTester,
    _audit_schemas,
)
from ratemyagent.targets import MockTarget
from tests.conftest import BrittleTarget, ValidatingTarget

CASE_NAMES = {case.name for case in EDGE_CASES}


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 5, "warmup": 0, "timeout_s": 5.0}
    return ProbeConfig(**{**defaults, **kwargs})


class TestEdgeCaseCoverage:
    def test_the_six_documented_cases_are_present(self):
        assert CASE_NAMES == {
            "null_required",
            "empty_string",
            "wrong_type",
            "very_long_string",
            "missing_required",
            "extra_param",
        }

    def test_cases_that_violate_the_schema_are_marked(self):
        should_reject = {c.name for c in EDGE_CASES if c.should_reject}
        assert should_reject == {"null_required", "wrong_type", "missing_required"}

    def test_each_case_builds_a_distinct_payload(self):
        baseline = {"query": "hello"}
        built = [case.build(baseline, ["query"]) for case in EDGE_CASES]
        assert len({repr(sorted(p.items(), key=str)) for p in built}) == len(EDGE_CASES)

    def test_long_string_case_is_actually_long(self):
        case = next(c for c in EDGE_CASES if c.name == "very_long_string")
        payload = case.build({"query": "hi"}, ["query"])
        assert len(payload["query"]) == LONG_STRING_LENGTH

    def test_missing_required_removes_the_field(self):
        case = next(c for c in EDGE_CASES if c.name == "missing_required")
        assert case.build({"query": "hi"}, ["query"]) == {}

    def test_extra_param_keeps_the_valid_payload(self):
        case = next(c for c in EDGE_CASES if c.name == "extra_param")
        payload = case.build({"query": "hi"}, ["query"])
        assert payload["query"] == "hi"
        assert len(payload) == 2


class TestSchemaAudit:
    def test_a_clean_schema_has_no_issues(self):
        tools = [
            ToolInfo(
                name="search",
                description="searches",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            )
        ]
        assert _audit_schemas(tools) == []

    def test_missing_schema_is_reported(self):
        issues = _audit_schemas([ToolInfo(name="t", description="d", input_schema={})])
        assert any("no input schema" in i for i in issues)

    def test_required_field_absent_from_properties_is_reported(self):
        tools = [
            ToolInfo(
                name="t",
                description="d",
                input_schema={"type": "object", "properties": {}, "required": ["ghost"]},
            )
        ]
        assert any("not in properties" in i for i in _audit_schemas(tools))

    def test_untyped_property_is_reported(self):
        tools = [
            ToolInfo(
                name="t",
                description="d",
                input_schema={"type": "object", "properties": {"q": {}}},
            )
        ]
        assert any("declares no type" in i for i in _audit_schemas(tools))

    def test_missing_description_is_reported(self):
        tools = [
            ToolInfo(
                name="t",
                description=None,
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ]
        assert any("no description" in i for i in _audit_schemas(tools))

    def test_non_object_schema_is_reported(self):
        tools = [ToolInfo(name="t", description="d", input_schema={"type": "array"})]
        assert any("not object" in i for i in _audit_schemas(tools))


class TestProbing:
    async def test_every_case_runs_against_every_probed_tool(self):
        async with MockTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["tools_probed"] == 3
        assert result.metrics["cases_run"] == 3 * len(EDGE_CASES)

    async def test_tool_limit_is_respected(self):
        async with MockTarget.healthy(tools=("a", "b", "c", "d", "e")) as target:
            result = await ContractTester().execute(
                target, config(extra={"contract_tool_limit": 2})
            )

        assert result.metrics["tools_probed"] == 2

    async def test_a_validating_tool_rejects_invalid_input_cleanly(self):
        """Refusing bad input is correct behaviour and must not count as a crash."""
        async with ValidatingTarget() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["crashes"] == 0
        assert result.metrics["accepted_invalid"] == 0
        assert result.metrics["rejected"] > 0

    async def test_a_brittle_tool_is_recorded_as_crashing(self):
        async with BrittleTarget() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["crashes"] > 0
        assert result.metrics["crash_rate"] > 0.25
        assert any("brought the tool down" in f for f in result.findings)

    async def test_a_permissive_tool_is_flagged_for_accepting_invalid_input(self):
        """The quiet bug: nothing errors, but garbage got through."""
        async with MockTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["accepted_invalid"] > 0
        assert any("not validating what it declares" in f for f in result.findings)

    async def test_target_without_tools_is_inapplicable(self):
        class NoTools(MockTarget):
            def list_tools(self):
                return []

        async with NoTools() as target:
            result = await ContractTester().execute(target, config())

        assert result.applicable is False
        assert result.metrics["tools"] == 0
        assert any("no tool surface" in f for f in result.findings)


class TestProbeContract:
    def test_declares_baseline_phase_and_opts_out_of_fault_rerun(self):
        """Injected faults would read as tools crashing on edge-case input."""
        assert ContractTester.phase == "baseline"
        assert ContractTester.rerun_under_fault is False

    async def test_runs_without_keys_or_network(self):
        async with MockTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["cases_run"] > 0
        assert result.sample_count == result.metrics["cases_run"]
