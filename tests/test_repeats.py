"""Reporting R runs of one task set.

The four rules under test are presentation rules, and they are not style
choices -- each exists because the obvious alternative states something the
runs do not support:

- a **mean** of four runs is a number with no denominator attached;
- a **range** over two points is two points with a dash in it;
- a count without an **occurrence** is a duplicate averaged into invisibility;
- a score from the **central** run tells a user the failure is smaller than it is.

The end-to-end arms drive the machinery with real scan metrics. `chatty_agent`
is used where runs have to differ, because it is the one fixture whose call
count varies between runs.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.policy import THRESHOLD_SPECS, Policy
from ratemyagent.probes import repeats
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.behavior import BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.probes.repeats import RunRecord
from ratemyagent.scanner import scan
from ratemyagent.targets import AgentTarget

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"

DIRECTIONS = {spec.metric: spec.direction for spec in THRESHOLD_SPECS}

REPORTED = ("duplicate_mutations", "unsupported_claims", "retry_amplification")


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _runs(*values: int, placement: str = "t1:event#1=response_lost") -> list[RunRecord]:
    return [
        RunRecord(metrics={"duplicate_mutations": v}, placement=placement)
        for v in values
    ]


class TestThePresentationRules:
    """n=1, n=2 and n=5, which is where each rule shows itself."""

    def test_n1_prints_the_value_and_calls_it_one_run(self):
        group = repeats.summarize(_runs(1), ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        assert rendered.startswith("1 (n=1;")
        assert "too few for a range" in rendered
        assert "-" not in rendered.split("(")[0]

    def test_n2_prints_both_values_and_no_range(self):
        """Mutation: render a range here and this fails. Two points with a dash
        between them is not a spread."""
        group = repeats.summarize(_runs(0, 2), ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        assert rendered.startswith("0, 2 (n=2;")
        assert "too few for a range" in rendered
        assert "0-2" not in rendered

    def test_n5_prints_a_range_with_the_run_count(self):
        group = repeats.summarize(_runs(0, 0, 2, 1, 0), ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        assert rendered.startswith("0-2 (n=5)")

    def test_n5_carries_the_occurrence_count_for_a_yes_no_metric(self):
        """"0-2" alone says nothing about how often it happened, which for a
        duplicate is the whole finding."""
        group = repeats.summarize(_runs(0, 0, 2, 1, 0), ("duplicate_mutations",))[0]
        assert "occurred in 2 of 5 runs" in group.metrics["duplicate_mutations"].render()

    def test_an_unvarying_metric_says_every_run_rather_than_a_dash_range(self):
        group = repeats.summarize(_runs(0, 0, 0, 0, 0), ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        assert rendered.startswith("0 (n=5; every run)")
        assert "occurred in 0 of 5 runs" in rendered

    @pytest.mark.parametrize("n", [1, 2, 5])
    def test_no_rendering_at_any_n_is_ever_a_mean(self, n):
        """The rule stated as a property rather than per-case. A mean of these
        values is 1.4, and it must appear nowhere."""
        values = [0, 3, 1, 4, 0][:n]
        group = repeats.summarize(_runs(*values), ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        mean = sum(values) / len(values)
        assert f"{mean:.1f}" not in rendered or mean == int(mean)
        assert "mean" not in rendered and "avg" not in rendered

    def test_a_withheld_run_is_counted_and_not_read_as_zero(self):
        """`None` is "we could not tell", and a range that folded it in as 0
        would report a clean run that never happened."""
        runs = _runs(2, 1)
        runs.append(RunRecord(metrics={"duplicate_mutations": None},
                              placement=runs[0].placement))
        group = repeats.summarize(runs, ("duplicate_mutations",))[0]
        rendered = group.metrics["duplicate_mutations"].render()
        assert "1-2 (n=3)" in rendered
        assert "1 withheld" in rendered
        assert "occurred in 2 of 2 runs" in rendered


class TestScoringTakesTheWorstRun:
    def test_the_worst_run_is_the_one_scored(self):
        group = repeats.summarize(
            _runs(0, 0, 2, 0, 0), ("duplicate_mutations",), directions=DIRECTIONS
        )[0]
        assert repeats.scored_metrics(group) == {"duplicate_mutations": 2}

    def test_direction_decides_which_end_is_worst(self):
        """`recovery_rate` is a min threshold: the worst run is the lowest."""
        runs = [
            RunRecord(metrics={"recovery_rate": r}, placement="p") for r in (0.9, 0.4, 1.0)
        ]
        group = repeats.summarize(runs, ("recovery_rate",), directions=DIRECTIONS)[0]
        assert repeats.scored_metrics(group) == {"recovery_rate": 0.4}

    def test_a_metric_with_no_known_direction_is_withheld_not_guessed(self):
        """There is no worst without a direction, and picking one arbitrarily
        is how a metric gets scored backwards."""
        group = repeats.summarize(_runs(0, 5, 1), ("duplicate_mutations",))[0]
        assert group.metrics["duplicate_mutations"].worst is None
        assert repeats.scored_metrics(group) == {}

    def test_the_report_says_it_scored_the_worst(self):
        """Mutation: score the mean instead and this sentence becomes false."""
        group = repeats.summarize(
            _runs(0, 0, 2, 0, 0), ("duplicate_mutations",), directions=DIRECTIONS
        )[0]
        said = repeats.describe_scoring(group)
        assert "worst of 5 runs" in said
        assert "not the average" in said


class TestGroupingByRealizedPlacement:
    """Runs that faulted different calls are not replicates of one experiment."""

    def test_runs_with_different_placements_are_reported_separately(self):
        runs = [
            *_runs(0, 0, placement="t1:event#1=response_lost"),
            *_runs(2, placement="t1:event#2=response_lost"),
        ]
        groups = repeats.summarize(runs, ("duplicate_mutations",))
        assert [group.n for group in groups] == [2, 1]
        assert groups[0].placement != groups[1].placement

    def test_the_split_is_explained_rather_than_left_to_be_noticed(self):
        runs = [
            *_runs(0, 0, placement="t1:event#1=response_lost"),
            *_runs(2, placement="t1:event#2=response_lost"),
        ]
        said = repeats.describe_placements(repeats.summarize(runs, ("duplicate_mutations",)))
        assert "did not all fault the same calls" in said
        assert "2x [t1:event#1=response_lost]" in said
        assert "spread of the schedule, not of the agent" in said

    def test_one_placement_needs_no_explanation(self):
        groups = repeats.summarize(_runs(0, 1, 0), ("duplicate_mutations",))
        assert repeats.describe_placements(groups) is None

    def test_a_run_where_nothing_fired_groups_with_its_own_kind(self):
        runs = [*_runs(0, placement=""), *_runs(1, placement="t1:event#1=response_lost")]
        groups = repeats.summarize(runs, ("duplicate_mutations",))
        assert len(groups) == 2
        assert "no fault reached" in repeats.describe_placements(groups)


class TestAgainstRealScans:
    """Three real runs of a real agent, summarized the way a report would."""

    @pytest.fixture(scope="class")
    @classmethod
    def three(cls, tmp_path_factory):
        shared = tmp_path_factory.mktemp("repeats")
        tally = shared / "tally"
        schedule = {("t1", "event", 1): FaultKind.RESPONSE_LOST,
                    ("t1", "effects", 3): FaultKind.SERVER_ERROR}

        async def run_all():
            out = []
            for index in range(3):
                work = shared / f"run{index}"
                work.mkdir()
                target = AgentTarget(
                    agent_command=_agent("chatty_agent.py", "--tally", str(tally)),
                    tasks_path=TASKS,
                    upstream="stdio://" + shlex.join([
                        sys.executable, str(TWIN), "--mode", "append",
                        "--state", str(work / "state.jsonl"),
                    ]),
                    work_dir=work / "work",
                    allow_mutating=True,
                    verify_tool="effects",
                    verify_count="entries",
                )
                result = await scan(
                    target,
                    probes=[AgentBaseline(), FaultInjector(schedule=schedule),
                            BehaviorAnalyzer()],
                    policy=Policy.default(),
                )
                behavior = result.probe("behavior").metrics
                out.append(RunRecord(
                    metrics=behavior,
                    placement=behavior.get("realized_placement", ""),
                ))
            return out

        return asyncio.run(run_all())

    def test_the_runs_are_split_by_where_the_fault_actually_landed(self, three):
        """The premise of the whole section: one seed, one table, and the
        placement still differs because the agent's call count did."""
        groups = repeats.summarize(three, REPORTED, directions=DIRECTIONS)
        assert len(groups) > 1, [run.placement for run in three]
        assert repeats.describe_placements(groups) is not None

    def test_every_run_is_accounted_for_in_exactly_one_group(self, three):
        groups = repeats.summarize(three, REPORTED, directions=DIRECTIONS)
        assert sum(group.n for group in groups) == 3

    def test_each_group_renders_without_a_mean(self, three):
        groups = repeats.summarize(three, REPORTED, directions=DIRECTIONS)
        for group in groups:
            for metric in REPORTED:
                rendered = group.metrics[metric].render()
                assert "mean" not in rendered
                assert f"n={group.n}" in rendered
