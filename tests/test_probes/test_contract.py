"""ContractTester: schema audit and edge-case handling."""

from __future__ import annotations

from ratemyagent.models import ToolInfo
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import (
    LONG_STRING_LENGTH,
    Case,
    ContractTester,
    _audit_schemas,
    _baseline_payload,
    _caveats,
    build_cases,
    case_count,
    plan_coverage,
)
from ratemyagent.targets import MockTarget
from tests.conftest import BrittleTarget, ValidatingTarget


def cases(baseline=None, required=("query",)) -> list[Case]:
    return build_cases(baseline or {"query": "hello"}, list(required))


CASE_NAMES = {case.kind for case in cases()}


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
        should_reject = {c.kind for c in cases() if c.should_reject}
        assert should_reject == {"null_required", "wrong_type", "missing_required"}

    def test_each_case_builds_a_distinct_payload(self):
        built = [repr(sorted(c.payload.items(), key=str)) for c in cases()]
        assert len(set(built)) == len(built), "two cases send the same bytes"

    def test_long_string_case_is_actually_long(self):
        case = next(c for c in cases() if c.kind == "very_long_string")
        assert len(case.payload["query"]) == LONG_STRING_LENGTH

    def test_missing_required_removes_the_field(self):
        case = next(c for c in cases() if c.kind == "missing_required")
        assert case.payload == {}

    def test_extra_param_keeps_the_valid_payload(self):
        case = next(c for c in cases() if c.kind == "extra_param")
        assert case.payload["query"] == "hello"
        assert len(case.payload) == 2


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
        assert result.metrics["cases_run"] == 3 * case_count(["query"])

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
        assert any("absence of a test" in c.reason for c in result.caveats)

    async def test_the_summary_states_the_denominator(self):
        async with MockTarget.healthy(tools=("get_a", "get_b", "delete_c")) as target:
            result = await ContractTester().execute(target, config())

        assert "of 3 tools" in result.summary
        assert "skipped as mutating" in result.summary
        assert "delete_c" in " ".join(result.findings), "the skipped tool is not named"


class TestCrashesAreAttributedOnlyWithAControl:
    """`not response.delivered` says the session stopped answering, never why.

    0.1.4 replaced "reads the error text" with "the session stopped answering",
    which was right and is still right -- and incomplete. A session stops
    answering for reasons that have nothing to do with the input, and every one
    of those was charged to the input. `contract_crash_rate_max` is an absolute
    check that caps the composite at 49, so a flaky connection could cap a
    server that validates its input perfectly.

    The control is well-formed calls interleaved 1:1 with the malformed ones:
    same tool, same session, adjacent in time. It measures *delivery*, not
    success, so a server that rejects the synthesized placeholder on its merits
    still proves the transport carried the call.
    """

    async def test_a_dead_session_is_reported_and_not_scored(self):
        from tests.conftest import DeadTarget

        async with DeadTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["crash_rate"] is None, "scored an unattributable crash"
        assert result.metrics["unscored_crash_rate"] == 1.0, "and lost the number"
        assert result.metrics["session_answered"] is False
        assert any("session stopped answering" in c.reason for c in result.caveats)

    async def test_a_flaky_target_is_reported_and_not_scored(self):
        """Crashes on malformed input at roughly the rate it drops everything."""
        from tests.conftest import FlakyTarget

        async with FlakyTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["control_undelivered"] > 0
        assert result.metrics["crash_attributable"] is False
        assert result.metrics["crash_rate"] is None
        assert result.metrics["unscored_crash_rate"] > 0
        assert any("regardless of what is sent" in c.reason for c in result.caveats)

    async def test_a_clean_control_still_scores_a_real_crash(self):
        """The half that matters most. A control that suppresses genuine crashes
        is worse than no control at all."""
        from tests.conftest import InputCrashTarget

        async with InputCrashTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["control_undelivered"] == 0, "control was not clean"
        assert result.metrics["crash_attributable"] is True
        assert result.metrics["crash_rate"] > 0, "a real crash stopped being scored"
        assert result.metrics["crashes"] > 0

    async def test_a_healthy_target_scores_zero_rather_than_nothing(self):
        async with MockTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["crash_rate"] == 0.0
        assert result.metrics["crash_attributable"] is True

    async def test_the_control_is_paired_with_every_case(self):
        """1:1 and interleaved. Sampling once at the start would let a target
        that dies partway through pass a control taken before it died."""
        async with MockTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert result.metrics["control_calls"] == result.metrics["cases_run"]

    async def test_the_summary_says_why_a_crash_rate_was_withheld(self):
        from tests.conftest import FlakyTarget

        async with FlakyTarget.healthy() as target:
            result = await ContractTester().execute(target, config())

        assert "not attributed to input" in result.summary


class TestRealArgumentsReachTheBaseline:
    """`--tool-args` is documented as the escape hatch from synthesized
    arguments, and this probe never read it.

    `_baseline_payload()` called `synthesize_args()` unconditionally, so four of
    the nine published rows supplied real arguments and had them discarded --
    including both rows section 9 labels "real ones" in a table organised around
    exactly that distinction.

    Real arguments attach to the tool they name and change nothing else. Pulling
    the named tool into the probe set would make one flag quietly change coverage
    too, and against a mutating tool it would undo the 0.1.11 gate.
    """

    def tool(self, name, required=("q",)):
        return ToolInfo(
            name=name,
            input_schema={
                "type": "object",
                "properties": {k: {"type": "string"} for k in required},
                "required": list(required),
            },
        )

    def test_the_named_tool_gets_the_real_payload(self):
        from ratemyagent.probes.contract import RealArgs

        real = RealArgs(tool="search", args={"q": "postgres"})
        assert _baseline_payload(self.tool("search"), real) == {"q": "postgres"}

    def test_every_other_tool_still_gets_a_synthesized_one(self):
        from ratemyagent.probes.contract import RealArgs

        real = RealArgs(tool="search", args={"q": "postgres"})
        assert _baseline_payload(self.tool("lookup"), real) == {"q": "ratemyagent probe"}

    def test_no_real_args_is_unchanged_behaviour(self):
        assert _baseline_payload(self.tool("search")) == {"q": "ratemyagent probe"}

    def test_synthesized_args_are_not_mistaken_for_supplied_ones(self):
        """The adapter records `probe_args` either way. They render identically
        and mean opposite things, which is the distinction the fix rests on."""
        from ratemyagent.probes.contract import read_real_args
        from tests.test_targets.test_mcp_error_payloads import GOOD_BODY, mcp_target

        target = mcp_target(lambda n, a: GOOD_BODY)
        target._tools = [self.tool("get_thing")]
        target._select_probe_tool()

        assert target.describe().metadata["probe_args"], "no args recorded at all"
        assert read_real_args(target) is None, "synthesized args read as supplied"

    def test_supplied_args_are_read_back_through_describe(self):
        from ratemyagent.probes.contract import read_real_args
        from tests.test_targets.test_mcp_error_payloads import GOOD_BODY, mcp_target

        target = mcp_target(lambda n, a: GOOD_BODY, tool_args={"q": "real"})
        target._tools = [self.tool("get_thing")]
        target._requested_tool = "get_thing"
        target._select_probe_tool()

        real = read_real_args(target)
        assert real is not None and real.args == {"q": "real"}
        assert real.tool == "get_thing"

    async def test_args_naming_an_unprobed_tool_are_announced_not_ignored(self):
        """Row 07: `--tool write_file` is explicit and sits outside the read-only
        window, so the contract section is entirely synthesized. Visibly."""
        async with MockTarget.healthy(
            tools=("get_a", "get_b", "get_c", "delete_d")
        ) as target:
            target._requested_tool = "delete_d"
            result = await ContractTester().execute(target, config())

        assert result.metrics["real_args_tool"] is None or (
            result.metrics["real_args_applied"] is False
        )

    async def test_the_mixed_case_says_which_tools_used_which(self):
        # Argument provenance qualifies every number this probe reports, so it
        # is a probe-scoped caveat rather than a finding about the target.
        joined_caveats = " ".join(c.reason for c in _caveats({
            "real_args_tool": "search", "real_args_applied": True,
            "tools_probed_names": ["search", "lookup", "browse"],
            "tools_skipped_mutating": [], "tools_skipped_unknown": [],
            "cases_run": 18, "tools": 3, "tools_probed": 3,
            "crash_attributable": True, "rejected_unclassified": 0,
            "schema_strictness": 1.0,
        }))
        assert "'search'" in joined_caveats and "lookup, browse" in joined_caveats
        assert "rejection path" in joined_caveats


class TestEveryRequiredFieldIsProbed:
    """`required[:1]` left every field but the first untested.

    Four builders mutated the first required field; `_missing_required` removed
    all of them. Five following one rule and one following another, which is
    what marked the narrowing as an accident rather than a design -- nothing
    documented it, no test covered it, and `declarable_violations()` had
    inherited it, so strictness figures described one field under a label naming
    the tool.

    Nothing in the project could see it. Every fixture was `required: ["q"]` and
    every tool in every contract window on all eight surveyed endpoints has zero
    or one required field -- multi-field tools exist on five of them and sit
    outside the 3-tool cap, because servers list their simple tools first.
    """

    def tool_schema(self, *required):
        return {
            "type": "object",
            "properties": {k: {"type": "string"} for k in required},
            "required": list(required),
        }

    def test_a_single_field_tool_is_unchanged(self):
        """The compatibility property. Per-field omission and omit-everything
        are the same payload at N=1, so they collapse and this reduces to the
        six cases every published scan used."""
        built = build_cases({"q": "hi"}, ["q"])

        assert len(built) == 6 == case_count(["q"])
        assert {c.kind for c in built} == CASE_NAMES

    def test_every_field_gets_every_mutation(self):
        built = build_cases({"path": "/tmp/a", "content": "hi"}, ["path", "content"])
        by_field = {}
        for case in built:
            by_field.setdefault(case.field, set()).add(case.kind)

        assert by_field["path"] == by_field["content"] == {
            "null_required", "empty_string", "wrong_type",
            "very_long_string", "missing_required",
        }

    def test_the_second_field_is_actually_corrupted(self):
        """The specific regression: `write_file`'s `content` had never been sent
        a null, a wrong type, an empty string or a long value on any scan."""
        built = build_cases({"path": "/tmp/a", "content": "hi"}, ["path", "content"])
        content_cases = {
            c.kind: c.payload for c in built if c.field == "content"
        }

        assert content_cases["null_required"] == {"path": "/tmp/a", "content": None}
        assert content_cases["wrong_type"]["content"] == 12345
        assert content_cases["missing_required"] == {"path": "/tmp/a"}
        # And the other field keeps its valid value, which is what localises the
        # result: a rejection here is about `content`, not about the payload.
        assert content_cases["null_required"]["path"] == "/tmp/a"

    def test_omitting_everything_is_kept_as_its_own_case(self):
        """A different question from omitting one field: does it require
        anything at all. The only case that catches a handler with no
        required-field checking whatsoever."""
        built = build_cases({"path": "/tmp/a", "content": "hi"}, ["path", "content"])
        omit_all = [c for c in built if c.kind == "missing_all_required"]

        assert len(omit_all) == 1
        assert omit_all[0].payload == {}
        assert case_count(["path", "content"]) == 12 == len(built)

    def test_it_is_not_emitted_twice_at_one_field(self):
        built = build_cases({"q": "hi"}, ["q"])
        assert [c.kind for c in built].count("missing_all_required") == 0

    def test_a_tool_with_nothing_required_is_probed_once(self):
        """It used to receive `{}` five times, counted as five edge cases.

        `read_graph` on `server-memory` reported "6 accepted" for two distinct
        calls; `htag-docs` has two such tools, so ten of its published eighteen
        cases were duplicates of each other.
        """
        built = build_cases({}, [])

        assert len(built) == 1 == case_count([])
        assert built[0].kind == "extra_param"

    def test_every_generated_payload_is_distinct(self):
        for required in ([], ["a"], ["a", "b"], ["a", "b", "c"]):
            baseline = {k: "v" for k in required}
            payloads = [
                repr(sorted(c.payload.items(), key=str))
                for c in build_cases(baseline, list(required))
            ]
            assert len(set(payloads)) == len(payloads), (
                f"duplicate payload counted as a distinct case at N={len(required)}"
            )


class TestStrictnessFollowsTheCases:
    """`declarable_violations()` inherited the same narrowing, so the strictness
    figures in assets/schema_strictness.md described one field per tool."""

    def tool(self, required, **specs):
        return ToolInfo(
            name="t",
            input_schema={
                "type": "object",
                "properties": {k: specs.get(k, {"type": "string"}) for k in required},
                "required": list(required),
            },
        )

    def test_a_single_field_tool_still_tops_out_at_six(self):
        """No published figure moves: every tool measured so far has zero or one
        required field, which is exactly the uniformity that hid the bug."""
        from ratemyagent.probes.contract import declarable_violations

        strict = self.tool(
            ["slug"], slug={"type": "string", "minLength": 1, "maxLength": 512}
        )
        assert len(declarable_violations(strict)) == 5
        assert case_count(["slug"]) == 6

    def test_the_denominator_grows_with_the_fields(self):
        from ratemyagent.probes.contract import declarable_violations

        two = self.tool(["path", "revision"])
        violations = declarable_violations(two)

        assert "missing_required[path]" in violations
        assert "missing_required[revision]" in violations
        assert "missing_all_required" in violations
        assert len(violations) <= case_count(["path", "revision"])

    def test_a_no_argument_tool_is_one_case_not_six(self):
        from ratemyagent.probes.contract import declarable_violations

        assert declarable_violations(self.tool([])) == []
        assert case_count([]) == 1
