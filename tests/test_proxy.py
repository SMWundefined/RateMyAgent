"""`ratemyagent proxy`: the FaultProxy seen from the wire.

Phase C moves fault injection across a process boundary, and the interesting
assertions are on **what an agent would see**, not on what a metric says
afterwards. A scan that reports `RESPONSE_LOST` from its own record proves only
that the record says so; the claim is that no reply reached the caller, and the
only place that is true or false is the wire.

So these tests speak JSON-RPC to the proxy directly, and check the upstream's
own ledger rather than the scanner's numbers -- the discipline
`counting_mcp_server.py` was written for.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.proxy import (
    ProxyServer,
    RecordWriter,
    invocation_rows,
    read_record,
    read_schedule,
    replay,
    response_to_wire,
    write_schedule,
)
from ratemyagent.targets.mcp import MCPTarget

ROOT = Path(__file__).resolve().parents[1]
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"


def _upstream(state: Path, *extra: str) -> str:
    return "stdio://" + shlex.join(
        [sys.executable, str(TWIN), "--mode", "append", "--state", str(state), *extra]
    )


async def _target(state: Path) -> MCPTarget:
    target = MCPTarget(_upstream(state), timeout_s=20, allow_mutating=True,
                       probe_traffic=False)
    await target.setup()
    return target


async def _server(tmp_path: Path, schedule=None, task_id: str = "t1"):
    target = await _target(tmp_path / "state.jsonl")
    record = RecordWriter(tmp_path / "record.jsonl", task_id=task_id)
    return target, ProxyServer(target, record=record, schedule=schedule, task_id=task_id)


async def _call(server: ProxyServer, request_id: int, arguments: dict) -> dict | None:
    return await server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "event", "arguments": arguments},
    })


async def _ledger(state: Path) -> list[dict]:
    """What the upstream actually stored, read off its own state file."""
    if not state.exists():
        return []
    return [json.loads(line) for line in state.read_text().splitlines() if line.strip()]


class TestToolsList:
    """Design test 5: the agent sees the upstream's surface, not ours."""

    async def test_tools_list_is_the_upstreams(self, tmp_path):
        target, server = await _server(tmp_path)
        try:
            reply = await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            names = [tool["name"] for tool in reply["result"]["tools"]]
            assert names == [tool.name for tool in target.list_tools()]

            relayed = next(t for t in reply["result"]["tools"] if t["name"] == "event")
            upstream = next(t for t in target.list_tools() if t.name == "event")
            assert relayed["description"] == upstream.description
            assert relayed["inputSchema"] == upstream.input_schema
        finally:
            await target.teardown()

    async def test_the_proxy_adds_no_tool_of_its_own(self, tmp_path):
        """A proxy that advertised itself would change what the agent can do."""
        target, server = await _server(tmp_path)
        try:
            reply = await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert len(reply["result"]["tools"]) == len(target.raw_tools())
        finally:
            await target.teardown()


class TestScheduledFaults:
    """Design tests 6 and 7. The upstream's ledger decides, not our record."""

    async def test_a_scheduled_timeout_never_reaches_upstream(self, tmp_path):
        """Design test 6: a short-circuit fault leaves the ledger flat."""
        state = tmp_path / "state.jsonl"
        schedule = {("t1", "event", 1): FaultKind.TIMEOUT}
        target, server = await _server(tmp_path, schedule=schedule)
        try:
            reply = await _call(server, 1, {"id": "a", "payload": "p"})
            # Nothing arrived: an injected timeout is a transport that died.
            assert reply is None
            assert await _ledger(state) == []
        finally:
            await target.teardown()

    async def test_a_scheduled_response_lost_reaches_upstream(self, tmp_path):
        """Design test 7: the work lands and the reply does not.

        Both halves asserted, because either alone is satisfied by a bug: a
        ledger entry with a reply is an ordinary success, and silence with no
        ledger entry is the timeout above.
        """
        state = tmp_path / "state.jsonl"
        schedule = {("t1", "event", 1): FaultKind.RESPONSE_LOST}
        target, server = await _server(tmp_path, schedule=schedule)
        try:
            reply = await _call(server, 1, {"id": "a", "payload": "p"})
            assert reply is None, "a lost reply must be silence on the wire"
            assert len(await _ledger(state)) == 1, "the upstream did run the call"
        finally:
            await target.teardown()

    async def test_the_session_keeps_serving_after_a_lost_reply(self, tmp_path):
        """A dropped reply is not a dead session.

        The proxy going quiet would turn every RESPONSE_LOST into a transport
        failure, which is the easy case, and destroy the fault.
        """
        schedule = {("t1", "event", 1): FaultKind.RESPONSE_LOST}
        target, server = await _server(tmp_path, schedule=schedule)
        try:
            assert await _call(server, 1, {"id": "a", "payload": "p"}) is None
            second = await _call(server, 2, {"id": "b", "payload": "p"})
            assert second is not None and second["id"] == 2
            assert not (second["result"].get("isError"))
        finally:
            await target.teardown()

    async def test_a_cancelled_notification_after_a_loss_is_recorded(self, tmp_path):
        """The agent giving up is evidence about the agent."""
        schedule = {("t1", "event", 1): FaultKind.RESPONSE_LOST}
        target, server = await _server(tmp_path, schedule=schedule)
        try:
            await _call(server, 1, {"id": "a", "payload": "p"})
            assert await server.handle({
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }) is None
            rows = read_record(tmp_path / "record.jsonl")
            assert any(row.get("method") == "notifications/cancelled" for row in rows)
            # And it is not replayed as a call.
            assert len(invocation_rows(rows)) == 1
        finally:
            await target.teardown()

    async def test_a_key_absent_from_the_schedule_is_no_fault(self, tmp_path):
        """A configured table is the whole answer, not a partial one."""
        schedule = {("t1", "event", 99): FaultKind.TIMEOUT}
        target, server = await _server(tmp_path, schedule=schedule)
        try:
            reply = await _call(server, 1, {"id": "a", "payload": "p"})
            assert reply is not None and not reply["result"].get("isError")
        finally:
            await target.teardown()


class TestIdempotencyKeyPassthrough:
    """Design test 8: the proxy relays; it does not edit."""

    async def test_the_key_arrives_upstream_byte_identical(self, tmp_path):
        state = tmp_path / "state.jsonl"
        calls = tmp_path / "calls.jsonl"
        target = MCPTarget(
            _upstream(state, "--calls", str(calls)),
            timeout_s=20, allow_mutating=True, probe_traffic=False,
        )
        await target.setup()
        record = RecordWriter(tmp_path / "record.jsonl", task_id="t1")
        server = ProxyServer(target, record=record, schedule={}, task_id="t1")
        try:
            await _call(server, 1, {"id": "a", "payload": "p", "idempotency_key": "k-1"})
            ledger = [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
            assert ledger[-1]["idempotency_key"] == "k-1"
            assert ledger[-1]["args"] == {"id": "a", "payload": "p"}
        finally:
            await target.teardown()

    async def test_the_key_is_recorded_beside_the_call(self, tmp_path):
        target, server = await _server(tmp_path, schedule={})
        try:
            await _call(server, 1, {"id": "a", "payload": "p", "idempotency_key": "k-1"})
            row = invocation_rows(read_record(tmp_path / "record.jsonl"))[0]
            assert row["idempotency_key"] == "k-1"
        finally:
            await target.teardown()

    async def test_a_reused_key_is_absorbed_by_the_twin(self, tmp_path):
        """A3, asserted against the fixture's own ledger.

        The twin is the oracle here for `counting_mcp_server`'s reason: the
        claim is about what the server did, and reading it back through our own
        metric would be the metric confirming itself.
        """
        state = tmp_path / "state.jsonl"
        calls = tmp_path / "calls.jsonl"
        target = MCPTarget(
            _upstream(state, "--calls", str(calls)),
            timeout_s=20, allow_mutating=True, probe_traffic=False,
        )
        await target.setup()
        record = RecordWriter(tmp_path / "record.jsonl", task_id="t1")
        server = ProxyServer(target, record=record, schedule={}, task_id="t1")
        try:
            for request_id in (1, 2):
                await _call(server, request_id,
                            {"id": "a", "payload": "p", "idempotency_key": "k-1"})
            ledger = [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
            assert [row["effect"] for row in ledger] == ["applied", "absorbed"]
            assert len(await _ledger(state)) == 1, "the second call changed nothing"
        finally:
            await target.teardown()

    async def test_without_a_key_append_still_appends(self, tmp_path):
        """The twin's existing behaviour is untouched by A3."""
        state = tmp_path / "state.jsonl"
        target, server = await _server(tmp_path, schedule={})
        try:
            for request_id in (1, 2):
                await _call(server, request_id, {"id": "a", "payload": "p"})
            assert len(await _ledger(state)) == 2
        finally:
            await target.teardown()


class TestTheOrdinalSurvivesAReconnect:
    """Amendment A4.

    If the counter lived only in the proxy process, an agent that reconnects --
    which a dropped reply is the most likely thing to provoke -- would restart
    at ordinal 1 and draw the same fault forever.
    """

    async def test_a_second_proxy_continues_the_schedule(self, tmp_path):
        state = tmp_path / "state.jsonl"
        record_path = tmp_path / "record.jsonl"
        # Ordinal 1 is lost; ordinal 2 is clean. A proxy that restarted its
        # counter would lose the second call too.
        schedule = {("t1", "event", 1): FaultKind.RESPONSE_LOST}

        first = await _target(state)
        server = ProxyServer(
            first, record=RecordWriter(record_path, task_id="t1"),
            schedule=schedule, task_id="t1",
        )
        try:
            assert await _call(server, 1, {"id": "a", "payload": "p"}) is None
        finally:
            await first.teardown()

        second = await _target(state)
        resumed = ProxyServer(
            second, record=RecordWriter(record_path, task_id="t1"),
            schedule=schedule, task_id="t1",
        )
        try:
            reply = await _call(resumed, 1, {"id": "a", "payload": "p"})
            assert reply is not None, "the reconnected proxy replayed ordinal 1"
        finally:
            await second.teardown()

    def test_the_counter_is_read_from_the_record(self, tmp_path):
        """Read directly, so the mechanism is pinned and not just its effect."""
        path = tmp_path / "record.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in [
            {"kind": "invocation", "op": "event", "task_id": "t1"},
            {"kind": "invocation", "op": "event", "task_id": "t1"},
            {"kind": "notification", "method": "notifications/cancelled", "task_id": "t1"},
            {"kind": "invocation", "op": "other", "task_id": "t1"},
        ]) + "\n")
        assert RecordWriter(path, task_id="t1").ordinals() == {
            ("t1", "event"): 2, ("t1", "other"): 1,
        }

    def test_sequences_continue_rather_than_restart(self, tmp_path):
        path = tmp_path / "record.jsonl"
        writer = RecordWriter(path, task_id="t1")
        from ratemyagent.models import Invocation

        def row(n):
            return Invocation(sequence=0, op="event", fingerprint=f"f{n}",
                              trajectory_id="t", attempt=1, ok=True,
                              latency_s=0.1, started_at=0.0)

        writer.write(row(1), idempotency_key=None, received_at=1.0)
        again = RecordWriter(path, task_id="t1")
        written = again.write(row(2), idempotency_key=None, received_at=2.0)
        assert written["sequence"] == 1


class TestTheRecordRoundTrips:
    """Design test 3, at the format's own level."""

    def test_rows_replay_into_the_invocations_they_were_written_from(self, tmp_path):
        from ratemyagent.models import ErrorKind, Invocation

        originals = [
            Invocation(sequence=0, op="event", fingerprint="event:aaa",
                       trajectory_id="t1:event:aaa", attempt=1, ok=False,
                       latency_s=0.25, started_at=0.0,
                       error_kind=ErrorKind.TIMEOUT, injected=FaultKind.RESPONSE_LOST,
                       executed=True),
            Invocation(sequence=1, op="event", fingerprint="event:aaa",
                       trajectory_id="t1:event:aaa", attempt=2, ok=True,
                       latency_s=0.1, started_at=0.3, executed=True),
        ]
        writer = RecordWriter(tmp_path / "record.jsonl", task_id="t1")
        for original in originals:
            writer.write(original, idempotency_key="k", received_at=1.0)

        invocations, trajectories = replay(read_record(tmp_path / "record.jsonl"))
        assert invocations == originals
        assert len(trajectories) == 1
        assert trajectories[0].attempts == 2
        assert trajectories[0].recovered is True
        assert trajectories[0].duplicates == 1

    def test_two_operations_stay_two_trajectories(self, tmp_path):
        """Grouping by `op` would fold a task's separate operations into one.

        Retry amplification would then read 3.0x against an agent that retried
        nothing, which is the mutation this asserts against.
        """
        from ratemyagent.models import Invocation

        writer = RecordWriter(tmp_path / "record.jsonl", task_id="t1")
        for index, fingerprint in enumerate(["event:aaa", "event:bbb", "event:ccc"]):
            writer.write(
                Invocation(sequence=index, op="event", fingerprint=fingerprint,
                           trajectory_id=f"t1:{fingerprint}", attempt=1, ok=True,
                           latency_s=0.1, started_at=float(index)),
                idempotency_key=None, received_at=1.0,
            )
        _, trajectories = replay(read_record(tmp_path / "record.jsonl"))
        assert len(trajectories) == 3
        assert all(t.attempts == 1 for t in trajectories)

    def test_a_row_is_readable_before_the_writer_closes(self, tmp_path):
        """Mutation H, named.

        The proxy is the process the fault is being done to: a task deadline
        kills it, and the record has to survive that. A row still sitting in a
        userspace buffer is invisible to every reader, so a killed proxy would
        leave a record that reads as a short run rather than an interrupted
        one -- absence dressed as a measurement.
        """
        from ratemyagent.models import Invocation

        writer = RecordWriter(tmp_path / "record.jsonl", task_id="t1")
        writer.write(
            Invocation(sequence=0, op="event", fingerprint="f", trajectory_id="t",
                       attempt=1, ok=True, latency_s=0.1, started_at=0.0),
            idempotency_key=None, received_at=1.0,
        )
        # Deliberately *not* closed: the assertion is that the row is on disk
        # while the writer is still open, which is what the flush buys.
        assert len(invocation_rows(read_record(tmp_path / "record.jsonl"))) == 1

    def test_a_truncated_tail_does_not_lose_the_rows_before_it(self, tmp_path):
        path = tmp_path / "record.jsonl"
        path.write_text(
            json.dumps({"kind": "invocation", "sequence": 0, "op": "event",
                        "fingerprint": "f", "trajectory_id": "t", "attempt": 1,
                        "ok": True, "latency_s": 0.1, "started_at": 0.0}) + "\n"
            + '{"kind": "invoca'
        )
        assert len(read_record(path)) == 1

    def test_a_missing_file_is_no_rows_and_not_an_error(self, tmp_path):
        """Whether that is legal is the caller's call, never this function's."""
        assert read_record(tmp_path / "nothing.jsonl") == []


class TestTheSchedule:
    def test_a_written_schedule_reads_back_identical(self, tmp_path):
        schedule = {
            ("t1", "event", 1): FaultKind.RESPONSE_LOST,
            ("t1", "event", 3): FaultKind.RATE_LIMIT,
            ("t2", "event", 1): FaultKind.SERVER_ERROR,
        }
        write_schedule(tmp_path / "schedule.json", schedule)
        assert read_schedule(tmp_path / "schedule.json") == schedule

    def test_no_schedule_and_an_empty_one_are_different(self, tmp_path):
        """`None` leaves the seeded draw alone; `{}` forces no faults."""
        assert read_schedule(None) is None
        assert read_schedule(tmp_path / "absent.json") is None
        write_schedule(tmp_path / "empty.json", {})
        assert read_schedule(tmp_path / "empty.json") == {}


class TestTheWireRendering:
    """`delivered` decides silence, and it decides it in one place."""

    @pytest.mark.parametrize("kind", [FaultKind.TIMEOUT, FaultKind.CONNECTION_REFUSED,
                                      FaultKind.RESPONSE_LOST])
    def test_undelivered_faults_are_silence(self, kind):
        from ratemyagent.models import Response
        from ratemyagent.targets.fault_proxy import FaultConfig, FaultProxy

        proxy = FaultProxy(None, FaultConfig.off())  # type: ignore[arg-type]
        if kind is FaultKind.RESPONSE_LOST:
            response = proxy._lose(Response(ok=True, latency_s=0.1, output="ok"))
        else:
            response = proxy._reject(kind)
        assert response.delivered is False
        assert response_to_wire(response) is None

    @pytest.mark.parametrize("kind", [FaultKind.RATE_LIMIT, FaultKind.SERVER_ERROR])
    def test_delivered_faults_arrive_as_tool_errors(self, kind):
        from ratemyagent.targets.fault_proxy import FaultConfig, FaultProxy

        proxy = FaultProxy(None, FaultConfig.off())  # type: ignore[arg-type]
        payload = response_to_wire(proxy._reject(kind))
        assert payload is not None and payload["isError"] is True
        body = json.loads(payload["content"][0]["text"])["error"]
        assert body["status"] in (429, 500)

    def test_a_429_carries_its_retry_after_hint_in_the_body(self):
        """stdio has no headers, so the hint travels in the result or nowhere."""
        from ratemyagent.targets.fault_proxy import FaultConfig, FaultProxy

        proxy = FaultProxy(None, FaultConfig.off())  # type: ignore[arg-type]
        payload = response_to_wire(proxy._reject(FaultKind.RATE_LIMIT))
        body = json.loads(payload["content"][0]["text"])["error"]
        assert body["retry_after_s"] == 1.0

    def test_a_damaged_payload_arrives_damaged(self):
        """Not replaced with a well-formed error: that would hand back the
        structure the fault removed."""
        from ratemyagent.models import Response
        from ratemyagent.targets.fault_proxy import FaultConfig, FaultProxy

        proxy = FaultProxy(None, FaultConfig.off())  # type: ignore[arg-type]
        damaged = proxy._corrupt(Response(ok=True, latency_s=0.1, output="abcdefgh"))
        payload = response_to_wire(damaged)
        assert payload["content"][0]["text"] == "abcd"
        assert payload["isError"] is True
