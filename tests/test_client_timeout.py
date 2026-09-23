"""The agent's own read timeout, measured by holding one reply.

Three agents, three outcomes, and they are three different findings:

- `careful_agent` sets a 3s read timeout, so a longer hold makes it **act**;
- the *same* `careful_agent` under a **shorter** hold waits it out -- and that
  is the case the wording has to get right, because an agent with a perfectly
  good 3s deadline that never fired must not be reported as having none;
- `no_timeout_agent` under a hold longer than the task deadline is still
  waiting when the scan kills it -- **the finding**.

The hold is not a fault. Nothing is dropped: the reply is late and then it
arrives, which is the one situation that separates "this client has a deadline"
from "this client has one we never reached".
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

from ratemyagent.outputs.report import render_report
from ratemyagent.outputs.scorecard import render_scorecard
from ratemyagent.policy import Policy
from ratemyagent.probes import agent_deadline
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.proxy import read_record
from ratemyagent.scanner import scan
from ratemyagent.targets import AgentTarget

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _upstream(tmp_path: Path) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--state", str(tmp_path / "state.jsonl"),
    ])


async def _hold_scan(work: Path, agent: str, hold_s: float, timeout_s: float = 30.0):
    target = AgentTarget(
        agent_command=agent,
        tasks_path=TASKS,
        upstream=_upstream(work),
        work_dir=work / "work",
        allow_mutating=True,
        timeout_s=timeout_s,
        hold_reply_s=hold_s,
    )
    result = await scan(target, probes=[AgentBaseline()], policy=Policy.default())
    return result, result.probe("agent_baseline").metrics


class TestTheThreeOutcomes:
    """One fixture each, end to end through a real proxy."""

    def test_an_agent_with_a_short_timeout_acts_and_the_moment_is_recorded(self, tmp_path):
        """`careful_agent` bounds a request at 3s. A 6s hold is past that, so it
        gives up -- and *when* it gave up is the measurement."""
        _, metrics = asyncio.run(_hold_scan(tmp_path, _agent("careful_agent.py"), 6.0))
        assert metrics["client_timeout_outcome"] == agent_deadline.OUTCOME_ACTED
        # Its own timeout, observed rather than asked for. Generous bounds: the
        # number under test is "roughly 3", not the fixture's constant.
        assert 2.0 < metrics["client_timeout_s"] < 5.5
        assert metrics["client_timeout_bound_s"] is None
        assert metrics["client_timeout_hold_s"] == 6.0

    def test_an_agent_that_waits_it_out_is_reported_as_a_bound(self, tmp_path):
        """**The same agent as above**, under a hold its 3s deadline outlasts.

        No deadline was *reached*, which is not the same as no deadline -- and
        this fixture demonstrably has one. The reply arrived and was accepted;
        all the scan learned is a floor, and the report has to say so."""
        _, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("careful_agent.py"), 1.5)
        )
        assert metrics["client_timeout_outcome"] == agent_deadline.OUTCOME_WAITED_OUT
        assert metrics["client_timeout_s"] is None
        assert metrics["client_timeout_bound_s"] == 1.5

    def test_the_timeless_agent_is_still_waiting_at_our_deadline(self, tmp_path):
        """The finding. The hold outlasts the task deadline, so the agent is
        killed mid-wait and the scan says so rather than scoring it."""
        _, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("no_timeout_agent.py"), 9.0, timeout_s=4.0)
        )
        assert metrics["client_timeout_outcome"] == agent_deadline.OUTCOME_NO_DEADLINE
        assert metrics["client_timeout_s"] is None
        # A lower bound on its patience, not a timeout.
        assert metrics["client_timeout_bound_s"] >= 3.0


class TestWhatIsPrinted:
    """The pair a reader needs: the agent's patience and our deadline."""

    def test_the_no_deadline_case_names_itself_and_is_not_scored(self):
        line = agent_deadline.describe({
            "client_timeout_outcome": agent_deadline.OUTCOME_NO_DEADLINE,
            "client_timeout_hold_s": 10.0,
            "client_timeout_bound_s": 12.0,
            "task_deadline_s": 30.0,
        })
        assert "none observed" in line
        assert "not scored" in line
        assert "per-task deadline 30s" in line

    def test_waiting_it_out_is_worded_as_a_lower_bound(self):
        """Never "no deadline": the spike inferred 60s for a stack from an SDK
        constant and was wrong by at least four times."""
        line = agent_deadline.describe({
            "client_timeout_outcome": agent_deadline.OUTCOME_WAITED_OUT,
            "client_timeout_hold_s": 10.0,
            "task_deadline_s": 30.0,
        })
        assert "none under 10s" in line
        assert "lower bound, not a measurement" in line

    def test_the_deadline_travels_with_it_in_both_renderers(self, tmp_path):
        result, _ = asyncio.run(
            _hold_scan(tmp_path, _agent("careful_agent.py"), 6.0)
        )
        card = " ".join(render_scorecard(result).split())
        report = " ".join(render_report(result).split())
        for rendered in (card, report):
            assert "Client timeout" in rendered
            assert "per-task deadline 30s" in rendered

    def test_a_scan_with_no_hold_prints_nothing_new(self, tmp_path):
        """Absent by default: 1.6.0's output is unchanged byte for byte unless
        the flag is passed."""
        async def run():
            target = AgentTarget(
                agent_command=_agent("careful_agent.py"),
                tasks_path=TASKS,
                upstream=_upstream(tmp_path),
                work_dir=tmp_path / "work",
                allow_mutating=True,
            )
            return await scan(target, probes=[AgentBaseline()], policy=Policy.default())

        result = asyncio.run(run())
        assert "client_timeout_outcome" not in result.probe("agent_baseline").metrics
        assert "Client timeout" not in render_scorecard(result)
        assert "Client timeout" not in render_report(result)


class TestWhichBoundWasReached:
    """There is no refusal here, and the reason is the point.

    No single configuration produces all three outcomes. With the deadline
    longer than the hold the reply is always released, so `no_deadline` is
    unreachable; with it shorter, a patient client is always killed first, so
    `waited_out` is. Both are legitimate scans -- the spike's own setup was the
    second -- so what the run owes the reader is **which of the two clocks it
    actually reached**, not a refusal to run one of them.
    """

    def test_a_deadline_bounded_run_says_the_deadline_bound_it(self, tmp_path):
        _, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("no_timeout_agent.py"), 9.0, timeout_s=4.0)
        )
        assert metrics["client_timeout_bound_by"] == agent_deadline.BOUND_BY_DEADLINE
        # And the sentence says so, rather than crediting the hold with a
        # number the hold never reached.
        line = agent_deadline.describe(metrics)
        assert "the scan's own deadline killed it" in line
        assert "rather than at the hold" in line

    def test_a_hold_bounded_run_says_the_hold_bound_it(self, tmp_path):
        _, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("careful_agent.py"), 1.5)
        )
        assert metrics["client_timeout_bound_by"] == agent_deadline.BOUND_BY_HOLD

    def test_an_agent_that_acted_is_bounded_by_neither(self, tmp_path):
        """It gave up on its own, so no clock of ours is the limit."""
        _, metrics = asyncio.run(_hold_scan(tmp_path, _agent("careful_agent.py"), 6.0))
        assert metrics["client_timeout_bound_by"] is None
        # The deadline exceeded the moment it acted, which is what makes the
        # number an observation rather than an artifact of the kill.
        assert metrics["task_deadline_s"] > metrics["client_timeout_s"]


class TestTheMeasurementItself:
    """`measure` against rows, with no agent in the way."""

    def _held(self, **extra):
        return {"kind": "invocation", "sequence": 0, "op": "event",
                "received_at": 1000.0, "replied_at": 1010.0, "held_s": 10.0, **extra}

    def test_a_cancellation_during_the_hold_is_the_moment_it_gave_up(self):
        """MCP's way to abandon a request is a notification, not a call -- so
        the measurement reads the whole record and not `invocation_rows`."""
        rows = [
            self._held(),
            {"kind": "notification", "sequence": 1,
             "method": "notifications/cancelled", "received_at": 1003.0},
        ]
        out = agent_deadline.measure(
            rows, task_outcome="completed", finished_at=1011.0, task_deadline_s=30.0
        )
        assert out["client_timeout_outcome"] == agent_deadline.OUTCOME_ACTED
        assert out["client_timeout_s"] == 3.0

    def test_an_exit_before_the_release_counts_as_acting(self):
        """An agent that errors out and returns never sends anything; its exit
        is the only evidence, and it is on the record's own clock."""
        out = agent_deadline.measure(
            [self._held()], task_outcome="failed",
            finished_at=1004.0, task_deadline_s=30.0,
        )
        assert out["client_timeout_outcome"] == agent_deadline.OUTCOME_ACTED
        assert out["client_timeout_s"] == 4.0

    def test_an_exit_after_the_release_is_waiting_it_out(self):
        out = agent_deadline.measure(
            [self._held()], task_outcome="completed",
            finished_at=1010.2, task_deadline_s=30.0,
        )
        assert out["client_timeout_outcome"] == agent_deadline.OUTCOME_WAITED_OUT
        assert out["client_timeout_bound_s"] == 10.0

    def test_an_abandoned_task_is_the_finding_whatever_else_is_on_the_record(self):
        out = agent_deadline.measure(
            [self._held()], task_outcome="abandoned",
            finished_at=1008.0, task_deadline_s=8.0,
        )
        assert out["client_timeout_outcome"] == agent_deadline.OUTCOME_NO_DEADLINE
        assert out["client_timeout_bound_s"] == 8.0

    def test_a_hold_that_never_reached_a_call_is_unmeasured_not_zero(self):
        """Absence reported as absence. A zero here reads as an agent that gave
        up instantly, which is the 8b shape."""
        out = agent_deadline.measure(
            [{"kind": "invocation", "sequence": 0, "op": "event",
              "received_at": 1000.0, "replied_at": 1000.1, "held_s": None}],
            task_outcome="completed", finished_at=1001.0, task_deadline_s=30.0,
        )
        assert out["client_timeout_outcome"] == agent_deadline.OUTCOME_UNMEASURED
        assert out["client_timeout_s"] is None


class TestTheHoldDoesNotDisturbTheDenominators:
    """The held task runs in its own pass, and the clean counts are untouched."""

    def test_the_clean_call_counts_exclude_the_held_run(self, tmp_path):
        result, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("careful_agent.py"), 6.0)
        )
        # `careful` makes one call per task on the clean path. The held run is
        # a second run of t1 and must not appear in this number, which is the
        # denominator `retry_amplification` divides by.
        assert metrics["clean_calls_per_task"] == {"t1": 1, "t2": 1}
        assert metrics["clean_calls"] == 2

    def test_the_held_run_has_its_own_record(self, tmp_path):
        result, metrics = asyncio.run(
            _hold_scan(tmp_path, _agent("careful_agent.py"), 6.0)
        )
        work = Path(metrics["work_dir"])
        baseline = read_record(work / "record-baseline-t1.jsonl")
        held = read_record(work / f"record-{agent_deadline.DEADLINE_PASS}-t1.jsonl")
        assert baseline and held
        assert all(row.get("held_s") is None for row in baseline)
        assert any(row.get("held_s") == 6.0 for row in held)
