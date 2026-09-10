"""A JSON-RPC error is a reply, not a dead transport.

The largest defect found since the retracted crash finding, and it is that
finding one layer down.

0.1.13 moved crash detection off error wording onto `Response.delivered`, and
recorded the change as reading *"a fact about whether anything arrived rather
than an opinion about what it said"*. `delivered` was never that fact. It is set
from the one site that builds a `Response` out of a raised exception, so it
means "the SDK raised rather than returned". Those agree for every transport
death and diverge for exactly one case: a server that validates a malformed call
and answers with `{"error": {"code": -32602, ...}}`.

The consequence was an inverted metric. A server declaring
`additionalProperties: false` and enforcing it -- correct, and the only
behaviour available to a strict schema -- was recorded as crashing on every
malformed input, and scored **worse than a server that validates nothing**: 49
against 50 on otherwise identical stubs, the strict one capped under
`absolute_fail_cap` for a fault it does not have.

It survived thirteen releases because the case could not arise: no server in the
corpus declares `additionalProperties: false`, and every fixture returns a
`result` carrying an error rather than a JSON-RPC error.
"""

from __future__ import annotations

import pytest

from ratemyagent.models import ErrorKind
from ratemyagent.targets.base import error_response, jsonrpc_error_code


class _ErrorData:
    def __init__(self, code, message="boom"):
        self.code, self.message = code, message


class _McpErrorLike(Exception):
    """Shaped like `mcp.shared.exceptions.McpError`, without the import."""

    def __init__(self, code, message="boom"):
        super().__init__(message)
        self.error = _ErrorData(code, message)


class TestTheSignSeparatesAReplyFromADeath:
    @pytest.mark.parametrize("code", [-32602, -32600, -32601, -32603, -32700])
    def test_negative_jsonrpc_codes_are_replies(self, code):
        assert jsonrpc_error_code(_McpErrorLike(code)) == code

    def test_connection_closed_is_a_death_despite_being_negative(self):
        """-32000 is the transport going away, however it is spelled."""
        assert jsonrpc_error_code(_McpErrorLike(-32000)) is None

    def test_positive_codes_are_the_sdks_own_transport_failures(self):
        """A read timeout raises `McpError(code=408)` -- an HTTP status reused.

        Nothing arrived from the server, so it belongs on the crash path.
        """
        assert jsonrpc_error_code(_McpErrorLike(408)) is None

    @pytest.mark.parametrize("exc", [
        ConnectionError("refused"),
        TimeoutError("slow"),
        RuntimeError("no code here"),
    ])
    def test_an_exception_with_no_code_is_a_death(self, exc):
        assert jsonrpc_error_code(exc) is None

    def test_a_bool_is_not_a_code(self):
        """`True` is an `int` in Python, and would read as code 1."""
        assert jsonrpc_error_code(_McpErrorLike(True)) is None


class TestErrorResponseStillMarksRealDeaths:
    @pytest.mark.parametrize("exc", [ConnectionError("refused"), TimeoutError("slow")])
    def test_a_transport_death_is_undelivered(self, exc):
        assert error_response(exc, 0.1).delivered is False


class TestTheAdapterDeliversAJsonRpcError:
    def _response(self, code, message="Invalid params: bad"):
        from ratemyagent.targets.mcp import MCPTarget

        target = MCPTarget("stdio://echo hi")
        return target._delivered_jsonrpc_error(_McpErrorLike(code, message), code, 0.1)

    def test_invalid_params_is_delivered_and_read_as_a_rejection(self):
        """The case the whole defect turned on."""
        response = self._response(-32602)

        assert response.delivered is True, (
            "a JSON-RPC error arrived over the connection; recording it as "
            "undelivered is what made strict servers look like they crash"
        )
        assert response.ok is False
        assert response.error_kind is ErrorKind.INVALID_RESPONSE
        assert response.meta["jsonrpc_code"] == -32602

    def test_the_code_beats_the_substring_table(self):
        """-32602 is a rejection whatever prose the server chose.

        Wording the table does not recognise lands in `rejected_unclassified`,
        which is the coverage gap that became a crash count in the first place.
        The spec-defined code has no such gap.
        """
        response = self._response(-32602, message="ungrammatical server prose")
        assert response.meta["reason_unclassified"] is False

    def test_an_unrecognised_code_still_falls_back_to_the_wording(self):
        response = self._response(-32603, message="wording nobody recognises")
        assert response.delivered is True
        assert response.meta["reason_unclassified"] is True


class TestTheAdapterDispatchesEndToEnd:
    """`invoke()` must actually route a JSON-RPC error to the delivered path.

    Every test above calls `_delivered_jsonrpc_error` directly, which proves the
    helper and not the dispatch. A mutation reverting `invoke()` to
    `error_response()` for every exception survived the whole suite -- the
    "does the gate use the helper" gap, again -- so this drives the real adapter
    against a real stdio server that rejects at the protocol layer.

    That fixture had to be written for this: no existing one rejects that way,
    which is why the defect was invisible for thirteen releases.
    """

    @staticmethod
    def _uri(*extra: str) -> str:
        import shlex
        import sys

        parts = [sys.executable, "tests/fixtures/strict_mcp_server.py", *extra]
        return "stdio://" + shlex.join(parts)

    async def _invoke(self, payload, *extra):
        from ratemyagent.models import Request
        from ratemyagent.targets.mcp import MCPTarget

        target = MCPTarget(
            self._uri(*extra), tool="get_page",
            tool_args={"url": "https://example.com"}, timeout_s=20,
        )
        await target.setup()
        try:
            return await target.invoke(
                Request(op="get_page", payload=payload, label="case")
            )
        finally:
            await target.teardown()

    async def test_a_rejected_call_is_delivered(self):
        response = await self._invoke({"url": "https://example.com", "zz": True})

        assert response.ok is False
        assert response.delivered is True, (
            "invoke() sent this to error_response(), which marks every raised "
            "exception undelivered. The contract probe reads that as a crash."
        )
        assert response.meta.get("jsonrpc_code") == -32602

    async def test_a_valid_call_still_succeeds(self):
        response = await self._invoke({"url": "https://example.com"})
        assert response.ok is True and response.delivered is True

    @pytest.mark.parametrize("label,extra,crashed,rejected", [
        ("strict", (), 0, 4),
        ("permissive", ("--permissive",), 0, 0),
    ])
    async def test_enforcing_a_schema_is_not_recorded_as_crashing(
        self, label, extra, crashed, rejected
    ):
        """The inversion, through the probe that computes the number.

        Before the fix the strict server recorded **4 of 6 crashed**, a 66.7%
        crash rate, and scored 49 under `absolute_fail_cap` -- below the 50 of a
        permissive twin that validates nothing. Now it rejects cleanly and
        crashes nothing.

        Driven through `ContractTester` rather than `scan()`: the scanner's
        wall-clock deadline opens a cancel scope that does not survive
        pytest-asyncio's task structure with a stdio target. The CLI path is
        unaffected and is covered by the CI stdio scan.
        """
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.contract import ContractTester
        from ratemyagent.targets.mcp import MCPTarget

        target = MCPTarget(
            self._uri(*extra), tool="get_page",
            tool_args={"url": "https://example.com"}, timeout_s=20,
        )
        await target.setup()
        try:
            result = await ContractTester().execute(
                target, ProbeConfig(requests=5, warmup=0)
            )
        finally:
            await target.teardown()

        assert result.metrics["crashes"] == crashed, (
            f"{label}: {result.metrics['crashes']} cases recorded as crashing "
            "the transport. A JSON-RPC rejection is a reply, not a death."
        )
        assert result.metrics["rejected"] == rejected
        assert result.metrics["crash_rate"] == 0.0
        assert result.metrics["rejected_unclassified"] == 0, (
            "the -32602 code names the cause; nothing should land in the "
            "unclassified bucket that became a crash count in the first place"
        )
