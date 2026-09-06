"""`Response.delivered`: did anything come back, regardless of what it said.

This is the crash signal. It exists because the previous one read error message
text: MCPTarget tagged `ErrorKind.UNKNOWN` onto any `isError` result whose
wording it did not recognise, and the contract probe scored UNKNOWN as a dead
transport. Two published MCP servers were reported upstream for crashes they do
not have.

The rule these tests pin: a Response built from a raised exception is not
delivered; everything else is. Nothing may reach that conclusion by reading a
message.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ratemyagent.models import ErrorKind, Request, Response
from ratemyagent.probes.contract import EDGE_CASES, _classify
from ratemyagent.targets.base import TRANSPORT_KINDS, error_response
from ratemyagent.targets.mock import MockTarget

CASE = EDGE_CASES[0]


class TestTheExceptionFunnel:
    def test_error_response_is_the_only_undelivered_constructor(self):
        assert error_response(ConnectionError("closed"), 0.1).delivered is False

    def test_a_plain_response_is_delivered_by_default(self):
        assert Response(ok=True, latency_s=0.1).delivered is True

    @pytest.mark.parametrize(
        "exc", [ConnectionError("closed"), TimeoutError("slow"), RuntimeError("odd")]
    )
    def test_every_exception_is_undelivered_whatever_its_kind(self, exc):
        """Including RuntimeError, which classifies as UNKNOWN."""
        assert error_response(exc, 0.1).delivered is False


class TestSimulatingTargetsDeclareIt:
    """Mock and FaultProxy cannot observe delivery; they must declare it."""

    @pytest.mark.parametrize("kind", sorted(TRANSPORT_KINDS, key=lambda k: k.value))
    def test_a_simulated_transport_death_is_not_delivered(self, kind):
        target = MockTarget.healthy()
        assert target._failure(kind, 0.1, 1).delivered is False

    @pytest.mark.parametrize("kind", [ErrorKind.SERVER_ERROR, ErrorKind.RATE_LIMIT])
    def test_a_simulated_bad_status_is_delivered(self, kind):
        """A 500 or a 429 is a reply. The connection carried it."""
        target = MockTarget.healthy()
        assert target._failure(kind, 0.1, 1).delivered is True

    async def test_a_corrupted_response_stays_delivered(self):
        """Malformed injection damages a payload that genuinely arrived."""
        from ratemyagent.targets.fault_proxy import FaultProxy

        proxy = FaultProxy(MockTarget.healthy())
        corrupted = proxy._corrupt(Response(ok=True, latency_s=0.1, output={"a": 1}))

        assert corrupted.ok is False
        assert corrupted.delivered is True


class TestClassifyReadsOnlyDelivered:
    """The structural invariant: no error kind can make a delivered call a crash."""

    @pytest.mark.parametrize("kind", list(ErrorKind))
    def test_a_delivered_error_is_a_rejection_for_every_kind(self, kind):
        response = Response(ok=False, latency_s=0.1, error="whatever", error_kind=kind)
        outcome = _classify("t", CASE, response, ["q"])["outcome"]

        assert outcome == "rejected", f"{kind} made a delivered response a crash"

    def test_an_undelivered_call_is_a_crash_even_when_unclassifiable(self):
        response = error_response(RuntimeError("something we cannot name"), 0.1)
        assert response.error_kind is ErrorKind.UNKNOWN
        assert _classify("t", CASE, response, ["q"])["outcome"] == "crashed"

    def test_the_retracted_wording_is_a_rejection(self):
        """The exact text that produced the upstream report."""
        response = Response(
            ok=False,
            latency_s=0.1,
            error="Repository path 'probe' is outside the allowed repository",
            error_kind=ErrorKind.INVALID_RESPONSE,
        )
        assert _classify("t", CASE, response, ["q"])["outcome"] == "rejected"


class TestSerialisation:
    def test_delivered_is_exported_additively(self):
        payload = Response(ok=True, latency_s=0.1).to_dict()
        assert payload["delivered"] is True
        # The pre-existing keys are untouched.
        assert {"ok", "latency_s", "error", "error_kind", "meta"} <= set(payload)

    def test_replace_carries_the_flag(self):
        original = error_response(ConnectionError("x"), 0.1)
        assert replace(original, latency_s=0.2).delivered is False


class TestRealTargetsNeverGuess:
    async def test_a_mock_invoke_reports_delivered(self):
        async with MockTarget.healthy() as target:
            response = await target.invoke(Request(op="search", payload={"query": "x"}))
        assert response.delivered is True
