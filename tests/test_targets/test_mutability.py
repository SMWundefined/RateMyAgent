"""Mutating-tool detection, and the gate that stops a scan writing 100 times.

The bug: auto-selection took the first tool the server listed. On
`@modelcontextprotocol/server-memory` that is `create_entities`, so a scan with
`--requests 100 --fault-rate 0.3` wrote to a live knowledge graph a hundred
times and said so in a WARNING. The server had published `readOnlyHint=False`
on that tool in its discovery response.
"""

from __future__ import annotations

import logging

import pytest

from ratemyagent.models import ToolInfo
from ratemyagent.targets import TargetError
from ratemyagent.targets.mutability import (
    Mutability,
    classify,
    classify_by_name,
    describe_refusal,
    read_only_tools,
    tokenize,
)
from tests.test_targets.test_mcp_error_payloads import GOOD_BODY, mcp_target


def tool(name: str, read_only: bool | None = None, destructive: bool | None = None):
    return ToolInfo(
        name=name,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}},
                      "required": ["q"]},
        read_only=read_only,
        destructive=destructive,
    )


class TestNameHeuristic:
    @pytest.mark.parametrize("name", [
        "create_entities", "delete_file", "write_file", "update-record",
        "createEntities", "add_observations", "run_command", "send_email",
    ])
    def test_mutating_verbs(self, name):
        assert classify_by_name(name) is Mutability.MUTATING

    @pytest.mark.parametrize("name", [
        "git_status", "read_graph", "search_nodes", "list_directory",
        "get_package_info", "git_diff_unstaged", "check_package_exists",
    ])
    def test_read_only_verbs(self, name):
        """`git_status` matters: the verb is the second token, not the first."""
        assert classify_by_name(name) is Mutability.READ_ONLY

    @pytest.mark.parametrize("name", ["echo", "frobnicate", "xyzzy", ""])
    def test_anything_else_is_unknown_not_safe(self, name):
        assert classify_by_name(name) is Mutability.UNKNOWN

    def test_mutating_wins_a_tie(self):
        """`get_or_create` does both; the dangerous reading is the correct one."""
        assert classify_by_name("get_or_create") is Mutability.MUTATING

    def test_tokenize_handles_every_separator_style(self):
        for name in ("create_entities", "create-entities", "createEntities"):
            assert tokenize(name) == ["create", "entities"]


class TestAnnotationsWin:
    def test_a_declared_hint_beats_the_name(self):
        assert classify(tool("echo", read_only=True)) is Mutability.READ_ONLY
        assert classify(tool("get_thing", read_only=False)) is Mutability.MUTATING

    def test_absent_hints_fall_back_to_the_name(self):
        assert classify(tool("delete_thing")) is Mutability.MUTATING
        assert classify(tool("list_things")) is Mutability.READ_ONLY

    def test_none_is_not_false(self):
        """The distinction the whole design rests on."""
        assert classify(tool("frobnicate", read_only=None)) is Mutability.UNKNOWN
        assert classify(tool("frobnicate", read_only=False)) is Mutability.MUTATING

    def test_a_disagreement_is_logged_not_silently_resolved(self, caplog):
        with caplog.at_level(logging.WARNING):
            verdict = classify(tool("delete_everything", read_only=True))

        assert verdict is Mutability.READ_ONLY
        assert "declares readOnlyHint=True" in caplog.text
        assert "name suggests mutating" in caplog.text


class TestRefusalMessage:
    def test_it_names_the_tool_the_reason_and_the_way_forward(self):
        tools = [tool("create_entities", read_only=False), tool("add_observations")]
        message = describe_refusal(tools, tools[0])

        assert "create_entities" in message
        assert "readOnlyHint=false" in message
        assert "--allow-mutating" in message

    def test_an_unknown_tool_says_so_rather_than_implying_danger(self):
        tools = [tool("frobnicate")]
        assert "not the same as a safe one" in describe_refusal(tools, tools[0])

    def test_read_only_tools_are_listed_when_present(self):
        tools = [tool("create_entities", read_only=False), tool("read_graph")]
        assert [t.name for t in read_only_tools(tools)] == ["read_graph"]


class TestSelectionGate:
    """End to end through MCPTarget, which is where the damage happened."""

    def _target(self, *tools, **kwargs):
        target = mcp_target(lambda n, a: GOOD_BODY, **kwargs)
        target._tools = list(tools)
        return target

    def test_auto_select_skips_a_mutating_tool_for_a_read_only_one(self, caplog):
        target = self._target(tool("create_entities", read_only=False),
                              tool("read_graph", read_only=True))
        with caplog.at_level(logging.INFO):
            target._select_probe_tool()

        assert target._probe_tool == "read_graph"
        assert "create_entities" in caplog.text

    def test_auto_select_refuses_when_nothing_is_known_read_only(self):
        target = self._target(tool("create_entities", read_only=False),
                              tool("add_observations", read_only=False))
        with pytest.raises(TargetError, match="refusing to auto-select"):
            target._select_probe_tool()

    def test_auto_select_refuses_unknown_too(self):
        """The half that is easy to get wrong: unclassified is not safe."""
        target = self._target(tool("frobnicate"), tool("xyzzy"))
        with pytest.raises(TargetError, match="refusing to auto-select"):
            target._select_probe_tool()

    def test_explicit_mutating_tool_needs_allow_mutating(self):
        target = self._target(tool("create_entities", read_only=False),
                              tool_args={"q": "x"})
        target._requested_tool = "create_entities"
        with pytest.raises(TargetError, match="--allow-mutating"):
            target._select_probe_tool()

    def test_allow_mutating_permits_it(self):
        target = self._target(tool("create_entities", read_only=False),
                              tool_args={"q": "x"})
        target._requested_tool = "create_entities"
        target.allow_mutating = True
        target._select_probe_tool()

        assert target._probe_tool == "create_entities"

    def test_an_explicit_unknown_tool_is_allowed_but_warns(self, caplog):
        """Naming a tool is a human choice; auto-selection is not."""
        target = self._target(tool("frobnicate"), tool_args={"q": "x"})
        target._requested_tool = "frobnicate"
        with caplog.at_level(logging.WARNING):
            target._select_probe_tool()

        assert target._probe_tool == "frobnicate"
        assert "not known to be read-only" in caplog.text

    def test_the_classification_is_recorded_for_the_report(self):
        target = self._target(tool("read_graph", read_only=True))
        target._select_probe_tool()

        assert target.describe().metadata["probe_tool_mutability"] == "read_only"
