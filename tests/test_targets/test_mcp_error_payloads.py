"""Error payloads returned inside *successful* MCP responses.

The MCP spec has `isError` for a failed tool call, but FastMCP-based servers
routinely return a normal result whose body is an error object. Counting those
as successes made a scan where every call was rejected report a 0% error rate,
with latency measuring the rejection path instead of the work.

Found against a real server: `pypi-query-mcp-server` answered every synthesized
call with `{"error": "Invalid package name: 'ratemyagent probe'"}` and
`isError` unset, and the scan scored it 98/100.
"""

from __future__ import annotations

import json
import logging

import pytest

from ratemyagent.models import ErrorKind, Request
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import CRASH_KINDS, ContractTester
from ratemyagent.probes.latency import LatencyProfiler
from ratemyagent.targets import MCPTarget
from ratemyagent.targets.mcp import (
    ERROR_PAYLOAD_KEYS,
    ERROR_PAYLOAD_WARN_AFTER,
    _classify_delivered_error,
    _error_payload,
    _payload_message,
    _sdk_attr,
)

# -- fakes --------------------------------------------------------------------


class FakeBlock:
    """A text content block, shaped like the SDK's."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeResult:
    """A CallToolResult in either SDK dialect.

    The SDK renamed `isError` to `is_error` in 2.0. `shape="v1"` spells it the
    old way, `shape="v2"` the new way, and neither defines the other attribute
    -- which is the point: a reader that only knows one spelling must be caught
    by the other, not silently handed a default.
    """

    def __init__(self, text: str, is_error: bool = False, shape: str = "v1") -> None:
        self.content = [FakeBlock(text)]
        if shape == "v1":
            self.isError = is_error
        elif shape == "v2":
            self.is_error = is_error
        else:
            raise ValueError(f"unknown SDK shape {shape!r}")


class FakeSession:
    """Stands in for an MCP ClientSession.

    `responder` maps the arguments it was called with to a body string, so a
    test can return an error payload for synthesized args and real output for
    valid ones -- exactly the shape of the bug.
    """

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        body = self._responder(name, args)
        if isinstance(body, FakeResult):
            return body
        return FakeResult(body)


def mcp_target(responder, *, tool_args=None, probe_args=None) -> MCPTarget:
    """An MCPTarget wired to a fake session, with no network or subprocess."""
    target = MCPTarget("stdio://./fake.py", tool=tool_args and "t", tool_args=tool_args)
    target._session = FakeSession(responder)
    target._probe_tool = "get_package_info"
    target._probe_args = probe_args if probe_args is not None else {"package_name": "probe"}
    return target


class FakeTool:
    """A Tool in either SDK dialect: `inputSchema` in 1.x, `input_schema` in 2.x."""

    def __init__(self, name: str, shape: str = "v1") -> None:
        self.name = name
        self.description = f"{name} tool"
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        if shape == "v1":
            self.inputSchema = schema
        elif shape == "v2":
            self.input_schema = schema
        else:
            raise ValueError(f"unknown SDK shape {shape!r}")


ERROR_BODY = json.dumps({"error": "Invalid package name: 'ratemyagent probe'",
                         "error_type": "InvalidPackageNameError"})
GOOD_BODY = json.dumps({"name": "requests", "version": "2.32.3"})


# -- detection ----------------------------------------------------------------


class TestErrorPayloadDetection:
    @pytest.mark.parametrize("key", ERROR_PAYLOAD_KEYS)
    def test_every_documented_key_is_detected(self, key):
        assert _error_payload(json.dumps({key: "something went wrong"})) is not None

    def test_the_real_world_body_is_detected(self):
        assert _error_payload(ERROR_BODY) is not None

    def test_a_normal_body_is_not(self):
        assert _error_payload(GOOD_BODY) is None

    @pytest.mark.parametrize("body", [
        '{"error": null}',
        '{"error": false}',
        '{"error": ""}',
        '{"error": []}',
        '{"error": {}}',
    ])
    def test_an_empty_error_key_is_not_a_failure(self, body):
        """Plenty of tools include the key unconditionally."""
        assert _error_payload(body) is None

    @pytest.mark.parametrize("body", ["", "plain text", "not json {", "42", "null"])
    def test_non_json_bodies_are_ignored(self, body):
        assert _error_payload(body) is None

    def test_an_error_inside_a_json_list_is_found(self):
        assert _error_payload('[{"error": "boom"}]') is not None

    def test_a_list_without_errors_is_clean(self):
        assert _error_payload('[{"name": "requests"}, {"name": "httpx"}]') is None

    def test_the_offending_mapping_is_returned(self):
        payload = _error_payload(ERROR_BODY)
        assert payload["error_type"] == "InvalidPackageNameError"


class TestDeliveredErrorClassification:
    def test_a_generic_payload_is_invalid_response_not_unknown(self):
        """UNKNOWN would make the contract probe call this a crash."""
        assert _classify_delivered_error('{"error": "nope"}') is ErrorKind.INVALID_RESPONSE

    def test_it_never_returns_a_crash_kind(self):
        for body in ('{"error":"nope"}', '{"error":"weird"}', '{"error_code": 1}'):
            assert _classify_delivered_error(body) not in CRASH_KINDS

    def test_recognisable_causes_still_classify(self):
        assert _classify_delivered_error('{"error":"rate limit"}') is ErrorKind.RATE_LIMIT
        assert _classify_delivered_error('{"error":"timed out"}') is ErrorKind.TIMEOUT
        assert _classify_delivered_error('{"error":"500 boom"}') is ErrorKind.SERVER_ERROR

    def test_an_unrecognised_isError_rejection_is_not_unknown(self):
        """The 0.1.4 regression: this text reached the isError branch unwrapped."""
        assert _classify_delivered_error(
            "Repository path 'x' is outside the allowed repository"
        ) is ErrorKind.INVALID_RESPONSE

    @pytest.mark.parametrize(
        "text",
        [
            # Measured against the two servers this scanner wrongly accused.
            "Repository path 'x' is outside the allowed repository",  # mcp-server-git
            "EISDIR: illegal operation on a directory, read",         # server-filesystem
            "ENAMETOOLONG: name too long, realpath '/x/AAAA'",
            "ENOENT: no such file or directory, open '/x/probe'",
        ],
    )
    def test_a_real_rejection_is_never_a_crash_kind(self, text):
        assert _classify_delivered_error(text) not in CRASH_KINDS

    def test_the_message_quotes_the_error(self):
        message = _payload_message(_error_payload(ERROR_BODY))
        assert "Invalid package name" in message


# -- the fix ------------------------------------------------------------------


class TestInvokeMarksThemAsFailures:
    async def test_an_error_payload_is_a_failure(self):
        target = mcp_target(lambda n, a: ERROR_BODY)
        response = await target.invoke(target.sample_request(0))

        assert response.ok is False
        assert response.error_kind is ErrorKind.INVALID_RESPONSE
        assert response.meta["error_payload"] is True
        assert "Invalid package name" in response.error

    async def test_a_normal_response_is_still_a_success(self):
        target = mcp_target(lambda n, a: GOOD_BODY)
        response = await target.invoke(target.sample_request(0))

        assert response.ok is True
        assert response.error_kind is None
        assert "error_payload" not in response.meta

    async def test_is_error_still_takes_precedence(self):
        target = mcp_target(lambda n, a: FakeResult("boom", is_error=True))
        response = await target.invoke(target.sample_request(0))

        assert response.ok is False
        assert response.meta.get("error_payload") is None

    async def test_the_body_is_still_returned_as_output(self):
        """The payload is evidence; do not swallow it."""
        target = mcp_target(lambda n, a: ERROR_BODY)
        response = await target.invoke(target.sample_request(0))

        assert "InvalidPackageNameError" in response.output

    async def test_latency_is_still_measured(self):
        target = mcp_target(lambda n, a: ERROR_BODY)
        assert (await target.invoke(target.sample_request(0))).latency_s > 0


class TestEffectOnProbes:
    """The point of the fix: the numbers stop lying."""

    async def test_the_latency_probe_now_reports_a_real_error_rate(self):
        target = mcp_target(lambda n, a: ERROR_BODY)
        result = await LatencyProfiler().execute(
            target, ProbeConfig(requests=20, warmup=0)
        )

        assert result.error_rate == 1.0
        assert result.metrics["errors_by_kind"] == {"invalid_response": 20}

    async def test_a_healthy_server_is_unaffected(self):
        target = mcp_target(lambda n, a: GOOD_BODY)
        result = await LatencyProfiler().execute(
            target, ProbeConfig(requests=20, warmup=0)
        )

        assert result.error_rate == 0.0

    async def test_the_contract_probe_reads_them_as_rejections_not_crashes(self):
        """A tool returning a structured error is validating, not falling over."""
        target = mcp_target(lambda n, a: ERROR_BODY)
        target._tools = [FakeTool("get_package_info")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        assert result.applicable is True
        assert result.metrics["crashes"] == 0
        assert result.metrics["rejected"] == result.metrics["cases_run"]

    async def test_an_unrecognised_isError_rejection_is_not_a_crash(self):
        """The 0.1.4 regression, end to end.

        mcp-server-git answers malformed input with this exact wording. It
        matches nothing in the substring table, so before the fix every one of
        these was graded a dead stdio transport and the server was reported
        upstream for a crash it does not have.
        """
        target = mcp_target(
            lambda n, a: FakeResult(
                "Repository path 'probe' is outside the allowed repository '/tmp/r'",
                is_error=True,
            )
        )
        target._tools = [FakeTool("git_status")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        assert result.metrics["crashes"] == 0
        assert result.metrics["crash_rate"] == 0.0
        assert result.metrics["rejected"] == result.metrics["cases_run"]
        assert not any("brought the tool down" in f for f in result.findings)

    async def test_an_unrecognised_rejection_is_reported_as_unclassified(self):
        """Counted as a rejection, and the coverage gap is visible rather than hidden."""
        target = mcp_target(
            lambda n, a: FakeResult(
                "Repository path 'probe' is outside the allowed repository '/tmp/r'",
                is_error=True,
            )
        )
        target._tools = [FakeTool("git_status")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        assert result.metrics["rejected_unclassified"] == result.metrics["cases_run"]
        assert "unclassified" in result.summary
        # Our coverage gap, not the target's behaviour: a caveat.
        assert any("could not be attributed to a cause" in c.reason for c in result.caveats)

    async def test_a_recognised_rejection_is_not_flagged_unclassified(self):
        """'Input validation error' is in the table, so coverage is real here."""
        target = mcp_target(
            lambda n, a: FakeResult(
                "Input validation error: 'q' is a required property", is_error=True
            )
        )
        target._tools = [FakeTool("git_status")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        assert result.metrics["rejected_unclassified"] == 0
        assert result.metrics["crashes"] == 0
        assert "unclassified" not in result.summary


# -- SDK major versions -------------------------------------------------------


class TestSdkFieldRenames:
    """One test per SDK major. The rename that shipped as a scoring bug.

    `getattr(result, "isError", False)` returns the default rather than raising
    when the attribute is gone, so under `mcp>=2` every failed call read as a
    success and `mcp-server-git` scored 100/100 with "18 accepted". These pin
    the isError branch to both spellings; a future rename must fail here.
    """

    @pytest.mark.parametrize("shape", ["v1", "v2"])
    async def test_the_iserror_branch_fires_under_both_shapes(self, shape):
        target = mcp_target(
            lambda n, a: FakeResult("tool blew up", is_error=True, shape=shape)
        )
        response = await target.invoke(target.sample_request())

        assert response.ok is False, f"isError not read under SDK shape {shape}"
        assert response.delivered is True

    @pytest.mark.parametrize("shape", ["v1", "v2"])
    async def test_a_success_stays_a_success_under_both_shapes(self, shape):
        target = mcp_target(lambda n, a: FakeResult(GOOD_BODY, shape=shape))
        response = await target.invoke(target.sample_request())

        assert response.ok is True

    @pytest.mark.parametrize("shape", ["v1", "v2"])
    def test_the_input_schema_is_read_under_both_shapes(self, shape):
        """A missed rename here empties every synthesized payload instead."""
        target = mcp_target(lambda n, a: GOOD_BODY)
        target._tools = [FakeTool("search", shape=shape)]

        assert target.list_tools()[0].input_schema["required"] == ["q"]

    @pytest.mark.parametrize("shape", ["v1", "v2"])
    async def test_the_contract_probe_sees_errors_under_both_shapes(self, shape):
        """The end-to-end consequence: 18 accepted vs 18 rejected."""
        target = mcp_target(
            lambda n, a: FakeResult(
                "Input validation error: bad", is_error=True, shape=shape
            )
        )
        target._tools = [FakeTool("search", shape=shape)]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        assert result.metrics["accepted"] == 0, f"errors invisible under {shape}"
        assert result.metrics["rejected"] == result.metrics["cases_run"]

    def test_sdk_attr_raises_nothing_but_returns_none_on_an_unknown_shape(self):
        """No default unless one is correct: None is visible, False was not."""
        assert _sdk_attr(object(), "is_error", "isError") is None


# -- the warning --------------------------------------------------------------


class TestInvalidArgumentWarning:
    async def _run(self, target, count: int) -> None:
        for index in range(count):
            await target.invoke(target.sample_request(index))

    async def test_it_warns_when_every_synthesized_call_is_rejected(self, caplog):
        target = mcp_target(lambda n, a: ERROR_BODY)
        with caplog.at_level(logging.WARNING):
            await self._run(target, ERROR_PAYLOAD_WARN_AFTER)

        assert "synthesized arguments are likely invalid" in caplog.text
        assert "--tool-args" in caplog.text

    async def test_it_stays_quiet_below_the_threshold(self, caplog):
        target = mcp_target(lambda n, a: ERROR_BODY)
        with caplog.at_level(logging.WARNING):
            await self._run(target, ERROR_PAYLOAD_WARN_AFTER - 1)

        assert "synthesized arguments" not in caplog.text

    async def test_it_warns_only_once(self, caplog):
        target = mcp_target(lambda n, a: ERROR_BODY)
        with caplog.at_level(logging.WARNING):
            await self._run(target, ERROR_PAYLOAD_WARN_AFTER * 3)

        assert caplog.text.count("synthesized arguments") == 1

    async def test_one_good_response_suppresses_it(self, caplog):
        """The arguments clearly work; the rejections are the server's business."""
        calls = {"n": 0}

        def responder(name, args):
            calls["n"] += 1
            return GOOD_BODY if calls["n"] == 1 else ERROR_BODY

        target = mcp_target(responder)
        with caplog.at_level(logging.WARNING):
            await self._run(target, ERROR_PAYLOAD_WARN_AFTER * 2)

        assert "synthesized arguments" not in caplog.text

    async def test_it_stays_quiet_when_the_user_supplied_arguments(self, caplog):
        """Then the errors are about their values, not our synthesis."""
        target = mcp_target(
            lambda n, a: ERROR_BODY,
            tool_args={"package_name": "requests"},
            probe_args={"package_name": "requests"},
        )
        with caplog.at_level(logging.WARNING):
            await self._run(target, ERROR_PAYLOAD_WARN_AFTER * 2)

        assert "synthesized arguments" not in caplog.text

    async def test_contract_probe_traffic_does_not_trigger_it(self, caplog):
        """That probe sends malformed input on purpose."""
        target = mcp_target(lambda n, a: ERROR_BODY)

        with caplog.at_level(logging.WARNING):
            for index in range(ERROR_PAYLOAD_WARN_AFTER * 2):
                await target.invoke(
                    Request(op="get_package_info",
                            payload={"package_name": None},   # not the synthesized args
                            label=f"contract:t:{index}")
                )

        assert "synthesized arguments" not in caplog.text


# -- stateless servers --------------------------------------------------------


class FakeListing:
    def __init__(self, *names: str) -> None:
        self.tools = [FakeTool(n) for n in names]


class FakeServerInfo:
    def __init__(self, name: str, version: str = "1.0") -> None:
        self.name = name
        self.version = version


class FakeInit:
    def __init__(self, name: str) -> None:
        self.serverInfo = FakeServerInfo(name)


class HandshakeSession:
    """A session whose initialize() can be made to fail, as stateless cores do."""

    def __init__(self, *, init_error: Exception | None = None,
                 listing_error: Exception | None = None) -> None:
        self._init_error = init_error
        self._listing_error = listing_error
        self.initialize_calls = 0
        self.list_tools_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        self.initialize_calls += 1
        if self._init_error:
            raise self._init_error
        return FakeInit("Declared Server Name")

    async def list_tools(self):
        self.list_tools_calls += 1
        if self._listing_error:
            raise self._listing_error
        return FakeListing("web_search", "fetch_url")

    async def call_tool(self, name, args):
        return FakeResult(GOOD_BODY)


def patch_mcp(monkeypatch, session: HandshakeSession) -> None:
    """Swap the SDK's transport and session for fakes. No subprocess, no network."""
    import contextlib

    import mcp
    import mcp.client.stdio

    @contextlib.asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        # `errlog` is real: MCPTarget passes a captured stderr file so a probe
        # can report a server that degraded rather than failed. A double that
        # does not accept it fails every stdio test the moment that ships.
        yield ("read", "write")

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", lambda read, write: session)


#: The exact message a 2026-07-28 stateless core returns.
STATELESS_ERROR = RuntimeError(
    "Method 'initialize' not supported in MCP 2026-07-28 stateless core."
)


class TestStatelessServers:
    """A server that refuses `initialize` but serves tools is usable.

    Found against `uvx mcp-web-engine`: initialize raised, list_tools returned
    three tools, and the adapter refused to scan it at all.
    """

    async def test_a_refused_handshake_does_not_stop_the_scan(self, monkeypatch):
        session = HandshakeSession(init_error=STATELESS_ERROR)
        patch_mcp(monkeypatch, session)

        target = MCPTarget("stdio://uvx fake-server")
        await target.setup()

        assert session.initialize_calls == 1
        assert session.list_tools_calls == 1
        assert [t.name for t in target.list_tools()] == ["web_search", "fetch_url"]

    async def test_the_refused_handshake_is_recorded(self, monkeypatch):
        session = HandshakeSession(init_error=STATELESS_ERROR)
        patch_mcp(monkeypatch, session)

        target = MCPTarget("stdio://uvx fake-server")
        await target.setup()

        assert target.describe().metadata["handshake"] is False

    async def test_the_name_falls_back_to_the_uri(self, monkeypatch):
        """Without serverInfo there is nothing else to call it."""
        session = HandshakeSession(init_error=STATELESS_ERROR)
        patch_mcp(monkeypatch, session)

        target = MCPTarget("stdio://uvx fake-server")
        await target.setup()

        assert "fake-server" in target.describe().name

    async def test_a_successful_handshake_is_still_used(self, monkeypatch):
        session = HandshakeSession()
        patch_mcp(monkeypatch, session)

        target = MCPTarget("stdio://uvx fake-server")
        await target.setup()
        info = target.describe()

        assert info.metadata["handshake"] is True
        assert info.name == "Declared Server Name"
        assert info.metadata["server_version"] == "1.0"

    async def test_probing_works_after_a_refused_handshake(self, monkeypatch):
        session = HandshakeSession(init_error=STATELESS_ERROR)
        patch_mcp(monkeypatch, session)

        target = MCPTarget("stdio://uvx fake-server")
        await target.setup()
        response = await target.invoke(target.sample_request(0))

        assert response.ok is True

    async def test_a_failing_list_tools_still_aborts(self, monkeypatch):
        """list_tools is the real gate: no tools means nothing to scan."""
        from ratemyagent.targets import TargetError

        session = HandshakeSession(
            init_error=STATELESS_ERROR,
            listing_error=RuntimeError("connection closed"),
        )
        patch_mcp(monkeypatch, session)

        with pytest.raises(TargetError, match="could not connect"):
            await MCPTarget("stdio://uvx fake-server").setup()
