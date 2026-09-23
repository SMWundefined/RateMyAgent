"""What the fault table did, as against what it said.

The intended schedule is keyed `(task_id, tool, ordinal)`. An ordinal is only
reached if the agent makes that many calls to that tool in that task, so **a
table entry and a fault that fired are not the same thing** -- and against an
agent that chooses its own calls the gap between them moves from run to run at
one seed.

`chatty_agent` is the fixture with that property, and it is new in 1.6.1: the
other three make a fixed number of calls, so for them the realized placement is
the intended one every run and nothing here could fail.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.outputs.scorecard import render_scorecard
from ratemyagent.policy import Policy
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.behavior import BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector, placement_key, realized_schedule
from ratemyagent.proxy import invocation_rows, read_record
from ratemyagent.scanner import scan
from ratemyagent.targets import AgentTarget

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"

LOST = FaultKind.RESPONSE_LOST
ERROR = FaultKind.SERVER_ERROR

#: One entry on the write, and one on the *third* read. `chatty_agent` counts
#: per task, and a scan launches it twice per task -- once per pass -- so t1's
#: chaos run makes two reads in the first scan and four in the second. The read
#: entry is therefore out of reach on run 1 and fires on run 2: the same shape
#: as the spike's ordinal 7, which the table held and the agent never got to.
SCHEDULE = {("t1", "event", 1): LOST, ("t1", "effects", 3): ERROR}


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _upstream(tmp_path: Path) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--state", str(tmp_path / "state.jsonl"),
        "--calls", str(tmp_path / "calls.jsonl"),
    ])


async def _run(work: Path, agent: str, schedule: dict):
    target = AgentTarget(
        agent_command=agent,
        tasks_path=TASKS,
        upstream=_upstream(work),
        work_dir=work / "work",
        allow_mutating=True,
        verify_tool="effects",
        verify_count="entries",
    )
    result = await scan(
        target,
        probes=[AgentBaseline(), FaultInjector(schedule=schedule), BehaviorAnalyzer()],
        policy=Policy.default(),
    )
    return result, result.probe("fault").metrics


class TestTheUnitItself:
    """`realized_schedule` against rows, with no scan around it."""

    def test_it_counts_ordinals_per_tool_the_way_the_proxy_does(self):
        rows = {
            "t1": [
                {"sequence": 0, "op": "effects", "injected": None},
                {"sequence": 1, "op": "event", "injected": "response_lost"},
                {"sequence": 2, "op": "effects", "injected": "server_error"},
                {"sequence": 3, "op": "event", "injected": None},
            ]
        }
        assert realized_schedule(rows) == [
            {"task_id": "t1", "tool": "event", "ordinal": 1, "fault": "response_lost"},
            {"task_id": "t1", "tool": "effects", "ordinal": 2, "fault": "server_error"},
        ]

    def test_it_reads_in_sequence_order_not_file_order(self):
        """A reconnecting agent's second proxy appends; the record is still
        ordered by `sequence`, and a placement read out of file order would
        credit the fault to the wrong call."""
        rows = {"t1": [
            {"sequence": 2, "op": "event", "injected": "server_error"},
            {"sequence": 0, "op": "event", "injected": None},
            {"sequence": 1, "op": "event", "injected": None},
        ]}
        assert realized_schedule(rows) == [
            {"task_id": "t1", "tool": "event", "ordinal": 3, "fault": "server_error"},
        ]

    def test_a_run_with_no_fault_has_an_empty_placement(self):
        """Not None, and not absent: a run where nothing fired is a placement,
        and it groups with its own kind."""
        rows = {"t1": [{"sequence": 0, "op": "event", "injected": None}]}
        assert realized_schedule(rows) == []
        assert placement_key([]) == ""

    def test_the_key_is_readable_rather_than_hashed(self):
        """Two groups in a report have to show *how* they differed."""
        key = placement_key(realized_schedule({"t1": [
            {"sequence": 0, "op": "event", "injected": "response_lost"},
        ]}))
        assert key == "t1:event#1=response_lost"


class TestAnAgentWhoseCallCountVaries:
    """Two runs of one command, and the fault lands in two different places."""

    @pytest.fixture(scope="class")
    @classmethod
    def two_runs(cls, tmp_path_factory):
        shared = tmp_path_factory.mktemp("chatty")
        tally = shared / "tally"

        async def both():
            out = []
            for index in (1, 2):
                work = shared / f"run{index}"
                work.mkdir()
                out.append(await _run(
                    work, _agent("chatty_agent.py", "--tally", str(tally)), SCHEDULE
                ))
            return out

        return asyncio.run(both())

    def test_the_command_was_identical_and_the_call_counts_were_not(self, two_runs):
        """The premise. If this fails, the rest tests nothing."""
        first, second = two_runs
        assert first[1]["calls"] != second[1]["calls"]

    def test_the_intended_schedule_is_the_same_document_both_runs(self, two_runs):
        """One seed, one table -- which is exactly why a difference below
        cannot be blamed on the schedule."""
        first, second = two_runs
        assert first[1]["intended_schedule"] == second[1]["intended_schedule"]
        assert len(first[1]["intended_schedule"]) == 2

    def test_the_realized_placement_differs(self, two_runs):
        """Mutation: report only the intended table and this cannot fail."""
        first, second = two_runs
        assert first[1]["realized_placement"] != second[1]["realized_placement"]

    def test_the_entry_out_of_reach_on_the_first_run_fires_on_the_second(self, two_runs):
        first, second = two_runs
        assert "effects#3" not in first[1]["realized_placement"]
        assert "effects#3=server_error" in second[1]["realized_placement"]
        # And the write's own entry fired in both: the placement moved, it did
        # not simply grow or vanish.
        assert "event#1=response_lost" in first[1]["realized_placement"]
        assert "event#1=response_lost" in second[1]["realized_placement"]

    def test_the_realized_count_is_not_the_scheduled_count(self, two_runs):
        """`scheduled_faults` says how many were laid out, and on run 1 one of
        them never happened. Reading the first as the second is the error."""
        first, _ = two_runs
        assert first[1]["scheduled_faults"] == 2
        assert len(first[1]["realized_schedule"]) == 1

    def test_it_agrees_with_the_record_on_disk(self, two_runs):
        """Re-derived from the JSONL rather than trusted, because the whole
        point of the record is that it settles this from outside."""
        first, _ = two_runs
        work = Path(first[1]["record_dir"])
        injected = [
            row["injected"]
            for row in invocation_rows(read_record(work / "record-chaos-t1.jsonl"))
            if row.get("injected")
        ]
        assert injected == [entry["fault"] for entry in first[1]["realized_schedule"]]

    def test_the_scorecard_prints_where_they_landed(self, two_runs):
        _, second = two_runs
        printed = " ".join(render_scorecard(second[0]).split())
        assert "Faults realized:" in printed
        assert "t1 effects#3 server_error" in printed
        assert "of 2 scheduled" in printed

    def test_the_export_carries_both(self, two_runs):
        """A consumer comparing two runs needs the placement, not just a count."""
        first, _ = two_runs
        document = json.loads(json.dumps(first[0].to_dict()))
        metrics = next(
            probe["metrics"] for probe in document["probes"] if probe["probe"] == "fault"
        )
        assert metrics["realized_schedule"] == first[1]["realized_schedule"]
        assert metrics["intended_schedule"] == first[1]["intended_schedule"]

    def test_behavior_carries_it_too(self, two_runs):
        """It travels with the numbers it explains, not one probe away."""
        first, _ = two_runs
        behavior = first[0].probe("behavior").metrics
        assert behavior["realized_placement"] == first[1]["realized_placement"]


class TestAFixedShapeAgentIsUnaffected:
    """The three shipped fixtures realize what they were scheduled."""

    def test_careful_realizes_the_table_it_was_given(self, tmp_path):
        result, metrics = asyncio.run(
            _run(tmp_path, _agent("careful_agent.py"), {("t1", "event", 1): LOST})
        )
        assert metrics["realized_placement"] == "t1:event#1=response_lost"
        assert len(metrics["intended_schedule"]) == 1
