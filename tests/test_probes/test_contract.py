"""ContractTester: schema audit and edge-case handling."""

from __future__ import annotations

from ratemyagent.models import ToolInfo
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import (
    EDGE_CASES,
    LONG_STRING_LENGTH,
    ContractTester,
    _audit_schemas,
    plan_coverage,
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
            # Names say nothing here; the mock's readOnlyHint is what makes them
            # eligible, which is the ordering this probe is supposed to use.
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


class TestMutatingToolsAreNotProbed:
    """Probing calls a tool with bad input six times. Against a write tool that
    is six writes.

    The 0.1.6 gate established this and guarded `_select_probe_tool` only. This
    probe kept sending eighteen payloads to whatever the first three tools
    happened to be. On `@modelcontextprotocol/server-memory` those are
    `create_entities`, `create_relations` and `add_observations` -- all three
    declaring `readOnlyHint=false` on every scan since the probe existed.

    Nothing was created, and not because anything stopped it: `entities` is an
    array, so synthesis filled it with `[]` and the three accepted calls asked
    the server to create nothing. The vacuous-argument bug was covering for the
    safety bug. On `server-filesystem`, `write_file` takes two strings and sits
    at index 4 -- one position outside the cap from writing a file named
    `ratemyagent probe`, and another named with a thousand A's, on every scan.
    """

    def tool(self, name, read_only=None):
        return ToolInfo(
            name=name,
            input_schema={"type": "object", "properties": {"q": {"type": "string"}},
                          "required": ["q"]},
            read_only=read_only,
        )

    def plan(self, tools, limit=3, allow_mutating=False):
        return plan_coverage(tools, limit=limit, allow_mutating=allow_mutating)

    def test_a_declared_write_tool_is_never_probed(self):
        coverage = self.plan([
            self.tool("create_entities", read_only=False),
            self.tool("read_graph", read_only=True),
        ])

        assert [t.name for t in coverage.probed] == ["read_graph"]
        assert coverage.excluded_mutating == ["create_entities"]

    def test_an_unclassified_tool_is_skipped_too(self):
        """Unclassified is not the same as safe. `_select_probe_tool` allows an
        explicitly named unknown tool because naming one is a human choice;
        there is no explicit selection here to defer to."""
        coverage = self.plan([self.tool("frobnicate"), self.tool("get_thing")])

        assert [t.name for t in coverage.probed] == ["get_thing"]
        assert coverage.excluded_unknown == ["frobnicate"]

    def test_eligibility_is_decided_before_the_cap(self):
        """The server-memory shape: three write tools first, read-only below.

        Cap-then-filter probes nothing at all here while six safe tools sit
        further down the list. Filter-then-cap keeps coverage at three. Getting
        this backwards turns a safety fix into a coverage outage.
        """
        coverage = self.plan([
            self.tool("create_entities", read_only=False),
            self.tool("create_relations", read_only=False),
            self.tool("add_observations", read_only=False),
            self.tool("read_graph", read_only=True),
            self.tool("search_nodes", read_only=True),
            self.tool("open_nodes", read_only=True),
        ])

        assert [t.name for t in coverage.probed] == [
            "read_graph", "search_nodes", "open_nodes"
        ]
        assert coverage.capped == 0

    def test_allow_mutating_restores_the_old_behaviour(self):
        tools = [self.tool("create_entities", read_only=False), self.tool("get_x")]
        coverage = self.plan(tools, allow_mutating=True)

        assert [t.name for t in coverage.probed] == ["create_entities", "get_x"]
        assert coverage.excluded_mutating == []

    def test_the_cap_and_the_skip_are_counted_separately(self):
        """One is a knob the caller can turn; the other is a safety decision."""
        coverage = self.plan(
            [self.tool("get_a"), self.tool("get_b"), self.tool("get_c"),
             self.tool("get_d"), self.tool("delete_e", read_only=False)],
            limit=2,
        )

        assert len(coverage.probed) == 2
        assert coverage.capped == 2
        assert coverage.excluded_mutating == ["delete_e"]

    async def test_a_write_only_server_runs_no_cases_and_says_so(self):
        """And must not report a clean bill from an empty sample.

        Skipping write tools can empty the probe set outright, and `0 crashes,
        0 accepted_invalid` over zero cases is exactly the absence-of-evidence
        defect this project has now found six times. A safety fix that creates
        a seventh would not be a fix.
        """
        async with MockTarget.healthy(tools=("write_file", "delete_all")) as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["cases_run"] == 0
        assert result.metrics["accepted_invalid"] is None, "scored a pass on no evidence"
        assert result.metrics["crash_rate"] is None
        assert "says nothing" in " ".join(result.findings)

    async def test_the_summary_states_the_denominator(self):
        async with MockTarget.healthy(tools=("get_a", "get_b", "delete_c")) as target:
            result = await ContractTester().execute(target, config())

        assert "of 3 tools" in result.summary
        assert "skipped as mutating" in result.summary
        assert "delete_c" in " ".join(result.findings), "the skipped tool is not named"
