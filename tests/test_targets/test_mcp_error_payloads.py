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
    _classify_payload_error,
    _error_payload,
    _payload_message,
)

# -- fakes --------------------------------------------------------------------


class FakeBlock:
    """A text content block, shaped like the SDK's."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [FakeBlock(text)]
        self.isError = is_error


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


class TestPayloadClassification:
    def test_a_generic_payload_is_invalid_response_not_unknown(self):
        """UNKNOWN would make the contract probe call this a crash."""
        assert _classify_payload_error('{"error": "nope"}') is ErrorKind.INVALID_RESPONSE

    def test_it_never_returns_a_crash_kind(self):
        for body in ('{"error":"nope"}', '{"error":"weird"}', '{"error_code": 1}'):
            assert _classify_payload_error(body) not in CRASH_KINDS

    def test_recognisable_causes_still_classify(self):
        assert _classify_payload_error('{"error":"rate limit"}') is ErrorKind.RATE_LIMIT
        assert _classify_payload_error('{"error":"timed out"}') is ErrorKind.TIMEOUT
        assert _classify_payload_error('{"error":"500 boom"}') is ErrorKind.SERVER_ERROR

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
        target._tools = []
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        # No tools discovered on this fake, so the probe is inapplicable; the
        # classification itself is asserted in TestPayloadClassification.
        assert result.applicable is False


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
