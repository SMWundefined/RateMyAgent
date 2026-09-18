"""MCPTarget URI parsing and argument synthesis.

These are the parts worth testing without a live server; connecting is covered
by examples/scan_mcp_example.py against a real MCP process.
"""

from __future__ import annotations

import sys

import pytest

from ratemyagent.models import Request
from ratemyagent.targets import MCPTarget, TargetError
from ratemyagent.targets.mcp import _parse_uri, synthesize_args


class TestParseUri:
    def test_stdio_python_script_gets_current_interpreter(self):
        transport, spec = _parse_uri("stdio://./server.py")
        assert transport == "stdio"
        assert spec == [sys.executable, "./server.py"]

    def test_stdio_non_python_command_is_left_alone(self):
        assert _parse_uri("stdio://node build/mcp.js")[1] == ["node", "build/mcp.js"]

    def test_stdio_respects_quoting(self):
        assert _parse_uri("stdio://node 'build/my server.js'")[1] == [
            "node",
            "build/my server.js",
        ]

    def test_an_unquoted_path_with_a_space_is_refused_before_the_server_starts(
        self, tmp_path,
    ):
        """1.5.1. The split would hand the server two arguments for one path.

        Before this, `mcp-sqlite` opened `/Users/wadoodsm/Silicon` and answered
        `Table "records" does not exist` to every call, so a URI mistake read as
        a broken server (gate B, 2026-09-17).
        """
        db = tmp_path / "a b" / "records.db"
        db.parent.mkdir()
        db.write_text("")

        with pytest.raises(TargetError) as exc:
            _parse_uri(f"stdio://npx -y mcp-sqlite@1.0.9 {db}")

        message = str(exc.value)
        assert "one path with a space in it" in message
        assert str(db) in message
        # The remedy is the quoted form, spelled out rather than described.
        assert f'"{db}"' in message

    def test_the_quoted_form_the_refusal_prints_is_the_one_that_works(self, tmp_path):
        db = tmp_path / "a b" / "records.db"
        db.parent.mkdir()
        db.write_text("")

        spec = _parse_uri(f'stdio://npx -y mcp-sqlite@1.0.9 "{db}"')[1]
        assert spec == ["npx", "-y", "mcp-sqlite@1.0.9", str(db)]

    def test_an_argument_the_run_will_create_is_left_alone(self, tmp_path):
        """No proof, no refusal: the joined text does not exist either.

        A `--state` file that is not there yet is the common case, and guessing
        at it would refuse working commands.
        """
        missing = tmp_path / "not" / "there yet.jsonl"
        spec = _parse_uri(f"stdio://node build/mcp.js --state {missing}")[1]
        assert spec == ["node", "build/mcp.js", "--state",
                        str(missing.parent / "there"), "yet.jsonl"]

    def test_an_existing_unquoted_path_without_spaces_is_unchanged(self, tmp_path):
        db = tmp_path / "records.db"
        db.write_text("")
        assert _parse_uri(f"stdio://node build/mcp.js {db}")[1] == [
            "node", "build/mcp.js", str(db),
        ]

    def test_stdio_passes_script_arguments_through(self):
        assert _parse_uri("stdio://./server.py --port 9000")[1] == [
            sys.executable,
            "./server.py",
            "--port",
            "9000",
        ]

    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("sse://localhost:8080/sse", "http://localhost:8080/sse"),
            ("sse+https://example.com/sse", "https://example.com/sse"),
            ("sse+http://example.com/sse", "http://example.com/sse"),
        ],
    )
    def test_explicit_sse_forms_normalize_to_a_url(self, uri, expected):
        """`sse+` is how you ask for the deprecated transport on purpose."""
        transport, spec = _parse_uri(uri)
        assert (transport, spec) == ("sse", [expected])

    @pytest.mark.parametrize("uri", [
        "https://example.com/mcp",
        "http://localhost:3001/mcp",
        "https://example.com/sse",   # path shape does not decide the transport
    ])
    def test_bare_http_is_streamable_http(self, uri):
        """Changed in 0.1.7. It used to mean SSE, which the 2025-06-18 spec
        deprecated -- so the only network transport pointed at the dead one and
        every hosted server failed with a TaskGroup error."""
        assert _parse_uri(uri) == ("http", [uri])

    @pytest.mark.parametrize("uri", ["stdio://", "stdio://   ", "ftp://host", "server.py", ""])
    def test_unusable_uris_are_rejected(self, uri):
        with pytest.raises(TargetError):
            _parse_uri(uri)


class TestSynthesizeArgs:
    def test_only_required_fields_are_filled(self):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        }
        assert synthesize_args(schema) == {"query": "ratemyagent probe"}

    def test_no_required_fields_yields_empty_args(self):
        assert synthesize_args({"properties": {"q": {"type": "string"}}}) == {}

    def test_empty_schema_yields_empty_args(self):
        assert synthesize_args({}) == {}

    def test_declared_default_wins(self):
        schema = {"properties": {"q": {"type": "string", "default": "hi"}}, "required": ["q"]}
        assert synthesize_args(schema) == {"q": "hi"}

    def test_enum_uses_first_member(self):
        schema = {"properties": {"mode": {"enum": ["fast", "slow"]}}, "required": ["mode"]}
        assert synthesize_args(schema) == {"mode": "fast"}

    def test_numeric_minimum_is_respected(self):
        schema = {
            "properties": {"n": {"type": "integer", "minimum": 5}},
            "required": ["n"],
        }
        assert synthesize_args(schema) == {"n": 5}

    @pytest.mark.parametrize(
        "declared,expected",
        [("string", "ratemyagent probe"), ("integer", 1), ("number", 1.0),
         ("boolean", False), ("array", []), ("object", {})],
    )
    def test_each_json_type_gets_a_minimal_value(self, declared, expected):
        schema = {"properties": {"v": {"type": declared}}, "required": ["v"]}
        assert synthesize_args(schema) == {"v": expected}

    def test_nullable_union_type_picks_the_non_null_branch(self):
        schema = {"properties": {"v": {"type": ["null", "integer"]}}, "required": ["v"]}
        assert synthesize_args(schema) == {"v": 1}

    def test_nested_object_recurses_into_required_fields(self):
        schema = {
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": {"term": {"type": "string"}},
                    "required": ["term"],
                }
            },
            "required": ["filter"],
        }
        assert synthesize_args(schema) == {"filter": {"term": "ratemyagent probe"}}

    def test_required_field_missing_from_properties_still_gets_a_value(self):
        assert synthesize_args({"required": ["mystery"]}) == {"mystery": "ratemyagent probe"}


class TestLifecycle:
    def test_bad_uri_fails_at_construction(self):
        with pytest.raises(TargetError):
            MCPTarget("ftp://nope")

    async def test_invoke_before_setup_is_an_error(self):
        target = MCPTarget("stdio://./server.py")
        with pytest.raises(TargetError):
            await target.invoke(Request(op="anything"))

    def test_sample_request_before_setup_is_an_error(self):
        with pytest.raises(TargetError):
            MCPTarget("stdio://./server.py").sample_request(0)

    async def test_teardown_without_setup_is_safe(self):
        await MCPTarget("stdio://./server.py").teardown()

    def test_describe_before_setup_reports_the_uri(self):
        info = MCPTarget("stdio://./server.py").describe()
        assert info.kind == "mcp"
        assert info.capabilities == []
        assert info.metadata["transport"] == "stdio"
