"""`RESPONSE_LOST_THEN_CLOSED`, on the wire and in the arithmetic.

The Phase D spike established the problem this fault exists for: Claude Code
held a dropped reply for 234 seconds without retrying, cancelling or returning,
so the task died on the scan's deadline and the scan -- correctly -- refused to
score it. `RESPONSE_LOST` cannot measure a client that never gives up, because
the measurement *is* the client's next decision.

Four properties, each with its own test, and three mutations that must fail:

1. the upstream executed the call, the client got no reply, and the session
   closed after the hold;
2. a client that retries on a closed connection produces a duplicate the twin's
   own ledger confirms;
3. the effect oracle counts the window normally, so the task completes rather
   than being abandoned;
4. `RESPONSE_LOST` is unchanged -- the session stays up and serving.

The ledger, not the metric, is ground truth for (2). A metric confirming itself
is the 1.3.0 failure (PROGRESS section 8b, entry 26).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.probes.base import ProbeConfig
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.proxy import read_close_after, read_record, write_schedule
from ratemyagent.targets.fault_proxy import (
    ALL_FAULTS,
    CLOSING_FAULTS,
    DEFAULT_CLOSE_AFTER_S,
    FAULT_ORDER,
    OPT_IN_FAULTS,
    FaultConfig,
)

FIXTURES = Path(__file__).parent / "fixtures"
TWIN = FIXTURES / "event_twin_mcp_server.py"

#: Short enough to keep the suite quick, long enough that the close is plainly a
#: second event and not part of the first. The default is five seconds; these
#: tests pass their own so a slow machine does not make the suite slow too.
HOLD_S = 1.0


# -- the sets ---------------------------------------------------------------


class TestTheSetsStillPartitionTheEnum:
    def test_three_tuples_partition_faultkind(self):
        """The 1.3.0 rule, now over three tuples instead of two.

        A new member has to be placed on purpose. This is the test that makes
        "on purpose" mean something: add a member and forget the placement and
        this fails rather than the member quietly joining a default set.
        """
        assert set(ALL_FAULTS) | set(OPT_IN_FAULTS) | set(CLOSING_FAULTS) == set(FaultKind)
        assert not set(ALL_FAULTS) & set(OPT_IN_FAULTS)
        assert not set(ALL_FAULTS) & set(CLOSING_FAULTS)
        assert not set(OPT_IN_FAULTS) & set(CLOSING_FAULTS)

    def test_the_closing_kind_is_in_no_default_set(self):
        """MUTATION: adding it to `ALL_FAULTS` must fail here."""
        assert FaultKind.RESPONSE_LOST_THEN_CLOSED not in ALL_FAULTS
        assert FaultKind.RESPONSE_LOST_THEN_CLOSED not in OPT_IN_FAULTS

    def test_the_canonical_order_keeps_the_first_six_boundaries(self):
        """Closing kinds sort last, so no existing cumulative threshold moves."""
        assert FAULT_ORDER[:5] == ALL_FAULTS
        assert FAULT_ORDER[5:6] == OPT_IN_FAULTS
        assert FAULT_ORDER[6:] == CLOSING_FAULTS

    def test_substitution_keeps_the_kind_count_at_six(self):
        """The whole reason it is a substitution and not an addition.

        Six kinds either way means `uniform()` divides by the same number and
        `_choose_fault` walks the same thresholds, so a recorded seed resolves
        to the same *slot*. Seven would move every boundary in every agent scan
        ever recorded -- the objection that kept `RESPONSE_LOST` out of
        `ALL_FAULTS` in the first place.
        """
        probe = FaultInjector()
        target = type("T", (), {"allow_mutating": True})()

        plain = probe._faults_from(ProbeConfig(extra={"fault_rate": 0.3}), target)
        closing = probe._faults_from(
            ProbeConfig(extra={"fault_rate": 0.3, "lost_reply_close_after": HOLD_S}),
            target,
        )

        assert len(plain.rates) == len(closing.rates) == 6
        assert list(plain.rates.values()) == list(closing.rates.values())
        assert FaultKind.RESPONSE_LOST in plain.rates
        assert FaultKind.RESPONSE_LOST not in closing.rates
        assert FaultKind.RESPONSE_LOST_THEN_CLOSED in closing.rates
        assert closing.close_after_s == HOLD_S

    def test_without_the_flag_nothing_about_the_config_changes(self):
        """The default-flags arm. No flag, no closing kind, no `close_after_s`."""
        probe = FaultInjector()
        target = type("T", (), {"allow_mutating": True})()
        faults = probe._faults_from(ProbeConfig(), target)

        assert set(faults.rates) == set(ALL_FAULTS) | set(OPT_IN_FAULTS)
        assert faults.close_after_s is None
        assert "close_after_s" not in faults.to_dict()


# -- the schedule file -------------------------------------------------------


class TestTheScheduleCarriesTheHold:
    def test_a_schedule_without_a_hold_is_byte_identical_to_1_5_1(self, tmp_path):
        """A scan that does not use the fault writes what it always wrote."""
        path = tmp_path / "schedule.json"
        write_schedule(path, {("t1", "event", 1): FaultKind.RESPONSE_LOST})
        assert json.loads(path.read_text()) == {
            "entries": [
                {"task_id": "t1", "tool": "event", "ordinal": 1,
                 "fault": "response_lost"}
            ]
        }
        assert read_close_after(path) is None

    def test_the_hold_round_trips(self, tmp_path):
        path = tmp_path / "schedule.json"
        write_schedule(
            path,
            {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            close_after_s=HOLD_S,
        )
        assert read_close_after(path) == HOLD_S


# -- the wire ----------------------------------------------------------------


class ProxyClient:
    """A JSON-RPC client over a `ratemyagent proxy` this test launched.

    Deliberately not the MCP SDK. The property under test is what an arbitrary
    client observes -- specifically end-of-stream -- and a client library that
    translated that into an exception of its own would put its own behaviour
    between the test and the thing being measured.
    """

    def __init__(self, env: dict[str, str], upstream: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ratemyagent.cli", "proxy", "--upstream", upstream],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, text=True, bufsize=1,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> int:
        self._id += 1
        body: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(body) + "\n")
        self.process.stdin.flush()
        return self._id

    def readline(self) -> str:
        assert self.process.stdout is not None
        return self.process.stdout.readline()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.send(method, params)
        return json.loads(self.readline())

    def handshake(self) -> None:
        self.call("initialize", {"protocolVersion": "2025-06-18"})
        self.call("tools/list")

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()


@pytest.fixture
def twin(tmp_path):
    """The event twin, appending, with state and a ledger outside its process."""
    state = tmp_path / "state.jsonl"
    calls = tmp_path / "calls.jsonl"
    # Quoted: a `stdio://` command splits on whitespace, and both the
    # interpreter and this checkout can sit under a path with a space in it.
    # The adapter refuses an unquoted one rather than silently mis-splitting,
    # so this is the form it asks for.
    return (
        f'stdio://"{sys.executable}" "{TWIN}" --mode append '
        f'--state "{state}" --calls "{calls}"',
        state,
        calls,
    )


def _env(tmp_path, monkeypatch, schedule_name="schedule.json"):
    import os

    return {
        **os.environ,
        "RMA_PROXY_RECORD": str(tmp_path / "record.jsonl"),
        "RMA_PROXY_SCHEDULE": str(tmp_path / schedule_name),
        "RMA_TASK_ID": "t1",
    }


class TestOnTheWire:
    def test_executed_no_reply_then_the_session_closes(self, tmp_path, twin, monkeypatch):
        """Property 1, all three halves of it, from the client's side.

        The upstream's ledger says the call ran. The client gets no reply. The
        stream then ends -- which is the whole point, and the thing a plain
        `RESPONSE_LOST` would never deliver.
        """
        upstream, state, calls = twin
        write_schedule(
            tmp_path / "schedule.json",
            {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            close_after_s=HOLD_S,
        )
        client = ProxyClient(_env(tmp_path, monkeypatch), upstream)
        try:
            client.handshake()
            client.send("tools/call", {"name": "event",
                                       "arguments": {"id": "alpha", "payload": "p"}})
            started = time.monotonic()
            # The next line the client reads is end-of-stream, not a reply.
            line = client.readline()
            waited = time.monotonic() - started
        finally:
            client.close()

        assert line == "", f"expected end-of-stream, got a reply: {line!r}"
        assert waited >= HOLD_S * 0.5, (
            f"the stream ended after {waited:.2f}s, which is not a hold"
        )
        # The upstream ran it. `changed: true` is the twin's own ledger.
        ledger = [json.loads(row) for row in calls.read_text().splitlines()]
        assert [row["effect"] for row in ledger] == ["applied"]
        assert len(state.read_text().splitlines()) == 1

    def test_response_lost_leaves_the_session_up(self, tmp_path, twin, monkeypatch):
        """Property 4. MUTATION: closing on plain `RESPONSE_LOST` must fail here.

        The 1.5.1 behaviour, unchanged: the reply is dropped and the *next*
        request is answered normally.
        """
        upstream, state, calls = twin
        write_schedule(
            tmp_path / "schedule.json",
            {("t1", "event", 1): FaultKind.RESPONSE_LOST},
        )
        client = ProxyClient(_env(tmp_path, monkeypatch), upstream)
        try:
            client.handshake()
            client.send("tools/call", {"name": "event",
                                       "arguments": {"id": "alpha", "payload": "p"}})
            # No reply for that id, ever. The session is still usable, so a
            # second call is answered -- which is what "stays up" means.
            second = client.call("tools/call", {"name": "event",
                                                "arguments": {"id": "beta", "payload": "p"}})
        finally:
            client.close()

        assert second["result"]["content"], "the session stopped serving"
        assert "error" not in second
        assert len(state.read_text().splitlines()) == 2

    def test_the_client_is_not_sent_an_error(self, tmp_path, twin, monkeypatch):
        """MUTATION: sending an error instead of closing must fail here.

        An error body is a *delivered reply*. It tells the client the call did
        not run, or at least that something came back -- and the entire value of
        a lost reply is that the client has no evidence either way. Turning the
        close into an error turns the hard case into the easy one.
        """
        upstream, _state, _calls = twin
        write_schedule(
            tmp_path / "schedule.json",
            {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            close_after_s=HOLD_S,
        )
        client = ProxyClient(_env(tmp_path, monkeypatch), upstream)
        try:
            client.handshake()
            request_id = client.send(
                "tools/call", {"name": "event", "arguments": {"id": "alpha", "payload": "p"}}
            )
            line = client.readline()
        finally:
            client.close()

        assert line == "", (
            f"the proxy answered request {request_id} instead of staying silent: {line!r}"
        )

    def test_the_record_names_which_kind_fired(self, tmp_path, twin, monkeypatch):
        """MUTATION: folding the two kinds into one count must fail here.

        `injected` is the only thing that distinguishes them downstream, because
        both produce the same `Response` shape.
        """
        upstream, _state, _calls = twin
        write_schedule(
            tmp_path / "schedule.json",
            {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            close_after_s=HOLD_S,
        )
        client = ProxyClient(_env(tmp_path, monkeypatch), upstream)
        try:
            client.handshake()
            client.send("tools/call", {"name": "event",
                                       "arguments": {"id": "alpha", "payload": "p"}})
            client.readline()
        finally:
            client.close()

        rows = [
            row for row in read_record(tmp_path / "record.jsonl")
            if row.get("kind", "invocation") == "invocation"
        ]
        assert len(rows) == 1
        assert rows[0]["injected"] == "response_lost_then_closed"
        assert rows[0]["injected"] != FaultKind.RESPONSE_LOST.value
        # Executed, and never answered.
        assert rows[0]["executed"] is True
        assert rows[0]["replied_at"] is None

    def test_a_client_that_retries_on_close_applies_it_twice(
        self, tmp_path, twin, monkeypatch
    ):
        """Property 2, with the twin's ledger as ground truth.

        A client that reconnects and retries -- which is what a closed
        connection should provoke -- sends the same mutation again. On the
        appending twin, with no idempotency key, that applies twice. The ledger
        says so; no metric is consulted.
        """
        upstream, state, calls = twin
        write_schedule(
            tmp_path / "schedule.json",
            {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            close_after_s=HOLD_S,
        )
        arguments = {"id": "alpha", "payload": "p"}
        env = _env(tmp_path, monkeypatch)

        first = ProxyClient(env, upstream)
        try:
            first.handshake()
            first.send("tools/call", {"name": "event", "arguments": arguments})
            assert first.readline() == ""       # the session went away
        finally:
            first.close()

        # The retry: a new session, the same call. The record's ordinals carry
        # over, so this is ordinal 2 and draws no fault.
        second = ProxyClient(env, upstream)
        try:
            second.handshake()
            reply = second.call("tools/call", {"name": "event", "arguments": arguments})
        finally:
            second.close()

        assert "result" in reply
        ledger = [json.loads(row) for row in calls.read_text().splitlines()]
        assert [row["effect"] for row in ledger] == ["applied", "applied"], ledger
        assert len(state.read_text().splitlines()) == 2, (
            "the twin's own state says the mutation landed once, so there is no "
            "duplicate for the oracle to find"
        )


class TestTheDefaultHold:
    def test_the_default_is_used_when_no_value_is_given(self, tmp_path, twin, monkeypatch):
        """A schedule naming the kind but no hold still closes, at the default."""
        assert DEFAULT_CLOSE_AFTER_S == 5.0
        config = FaultConfig(
            rates={},
            schedule={("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED},
            task_id="t1",
        )
        assert config.close_after_s is None
        # The proxy falls back to the module default rather than to "never".
        from ratemyagent.models import Response
        from ratemyagent.targets.fault_proxy import FaultProxy

        proxy = FaultProxy.__new__(FaultProxy)
        proxy.faults = config
        lost = proxy._lose(
            Response(ok=True, latency_s=0.0, output="x"),
            FaultKind.RESPONSE_LOST_THEN_CLOSED,
        )
        assert lost.meta["close_after_s"] == DEFAULT_CLOSE_AFTER_S
        assert lost.meta["injected"] == "response_lost_then_closed"
        assert lost.delivered is False


# -- end to end, through a whole scan ----------------------------------------


class TestAWholeScan:
    """Property 3, and the counts, through `scan()` rather than the wire.

    The agent is `no_timeout_agent`: no read timeout, so under `RESPONSE_LOST`
    it hangs and the task is abandoned. That is the Claude Code shape the Phase
    D spike measured, and it is the case this fault exists to make measurable.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        import asyncio
        import shlex

        from ratemyagent.policy import Policy
        from ratemyagent.probes.agent_baseline import AgentBaseline
        from ratemyagent.probes.behavior import BehaviorAnalyzer
        from ratemyagent.scanner import scan
        from ratemyagent.targets.agent import AgentTarget

        agents = FIXTURES / "agents"

        async def one(kind: FaultKind, close_after: float | None):
            work = tmp_path_factory.mktemp(kind.value)
            upstream = "stdio://" + shlex.join([
                sys.executable, str(TWIN), "--mode", "append",
                "--state", str(work / "state.jsonl"),
                "--calls", str(work / "calls.jsonl"),
            ])
            target = AgentTarget(
                agent_command=shlex.join(
                    [sys.executable, str(agents / "no_timeout_agent.py")]
                ),
                tasks_path=agents / "tasks.json",
                upstream=upstream,
                work_dir=work / "work",
                allow_mutating=True,
                verify_tool="effects",
                verify_count="entries",
                # Short, so a hang costs the suite seconds rather than minutes.
                timeout_s=25.0,
            )
            probe = FaultInjector(schedule={("t1", "event", 1): kind})
            if close_after is not None:
                probe._faults = FaultConfig.uniform(
                    0.0, (kind,), close_after_s=close_after
                )
            from ratemyagent.probes.base import ProbeRefusal
            refusal = None
            result = None
            try:
                result = await scan(
                    target,
                    probes=[AgentBaseline(), probe, BehaviorAnalyzer()],
                    policy=Policy.default(),
                )
            except ProbeRefusal as exc:
                # The control arm is *expected* to end here: a hang is a scan
                # that did not finish, and 1.5.1 already refuses rather than
                # scoring one. Captured instead of raised so the comparison
                # between the two kinds is the thing the tests read.
                refusal = str(exc)
            ledger = [
                json.loads(row)
                for row in (work / "calls.jsonl").read_text().splitlines() if row.strip()
            ]
            return result, ledger, refusal

        async def both():
            return {
                "closed": await one(FaultKind.RESPONSE_LOST_THEN_CLOSED, HOLD_S),
                "silent": await one(FaultKind.RESPONSE_LOST, None),
            }

        return asyncio.run(both())

    def test_the_closing_fault_lets_the_task_finish(self, runs):
        """Property 3: the oracle counts the window, so nothing is abandoned."""
        result, _ledger, refusal = runs["closed"]
        assert refusal is None, f"the closing fault still hung the agent: {refusal}"
        outcomes = result.probe("fault").metrics["task_outcomes"]
        assert "abandoned" not in outcomes.values(), outcomes

    def test_the_silent_fault_still_hangs_the_same_agent(self, runs):
        """The control, and the reason the new kind exists at all.

        Same agent, same schedule position, same everything but the kind. This
        one is abandoned, so the scan refuses -- which is what 1.5.1 did against
        Claude Code and why the spike could not produce a measurement.
        """
        _result, _ledger, refusal = runs["silent"]
        assert refusal is not None, "expected the hang to stop the scan"
        assert "abandoned" in refusal
        # And the message points at the way out, rather than at a Python class
        # this agent is not built on.
        assert "--lost-reply-close-after" in refusal
        assert "ClientSession" not in refusal

    def test_the_two_kinds_are_counted_separately(self, runs):
        """MUTATION: folding `injected_by_kind` into one key must fail here.

        The record already names the kind per row; this is the *metric*, which
        is what the scorecard prints and what a JSON consumer reads. Both have
        to keep them apart, because a client may retry one and not the other and
        a single total would make the two runs look identical.
        """
        result, _ledger, _refusal = runs["closed"]
        by_kind = result.probe("fault").metrics["injected_by_kind"]
        assert by_kind.get("response_lost_then_closed"), by_kind
        assert "response_lost" not in by_kind, (
            f"the two lost-reply kinds were folded into one count: {by_kind}"
        )

    def test_the_ledger_confirms_the_retry_applied_twice(self, runs):
        """Ground truth for the duplicate, from the twin and not from a metric."""
        _result, ledger, _refusal = runs["closed"]
        applied = [row for row in ledger if row["effect"] == "applied"]
        # One from the clean baseline pass, two from the chaos pass: the call
        # that was executed-then-dropped, and the blind retry after the close.
        assert len(applied) >= 3, ledger
