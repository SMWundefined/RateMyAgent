"""A run that applied nothing is not a run an agent can be passed on (1.6.2).

**The run this exists for.** The Phase D gate run (`assets/moat/GATE-D.md`)
scored **100/100, PASS** on a scan in which four of its five runs applied zero
effects for a task whose `expected_effects` was 1. Nothing in it was wrong:
the upstream absorbed the writes, `lost_effects` correctly said so and was
correctly left unscored, and `duplicate_mutations` was correctly 0. But a
`duplicate_mutations` of 0 over a window in which nothing landed is arithmetic
over an empty set, and the report put PASS on top of it.

This is the same shape as two rules already here, which is the argument for it
being a rule rather than a special case:

- `nothing_completed` -- no operation finished, so what did not go wrong is not
  evidence (`probes/behavior.py`);
- `agent_verdict_blocker` condition 3 -- no task was ever uncertain, so no agent
  could have duplicated.

Here: the tasks finished, they were uncertain, and they left no trace. One step
along the same line.

**A coverage rule, not a penalty.** `lost_effects` stays the server's fault and
stays unscored; no threshold moves and no score is lowered. `TestItIsCoverage`
is the arm that holds that line.

Every arm runs a real scripted agent against the real twin fixture, and
`--swallow-after 2` is the gate's exact shape: the clean pass applies both
tasks, and from then on every call is acknowledged and nothing is applied.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli
from ratemyagent.outputs import render_scorecard
from ratemyagent.outputs.common import verdict_lines
from ratemyagent.policy import agent_verdict_blocker
from tests.conftest import strip_ansi
from tests.test_agent_gate import (
    DUPLICATE,
    TASKS,
    _agent,
    _gate,
    _upstream,
)

SWALLOW = ("--swallow-after", "2")


@pytest.fixture(scope="module")
def swallowed(tmp_path_factory):
    """The gate's shape: a twin that absorbs everything after the baseline."""
    work = tmp_path_factory.mktemp("swallowed")
    result, metrics = asyncio.run(
        _gate(work, _agent("careful_agent.py"), DUPLICATE, twin=SWALLOW)
    )
    return result, metrics, work


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    """The control: the same agent and schedule against a twin that applies."""
    work = tmp_path_factory.mktemp("applied")
    result, metrics = asyncio.run(
        _gate(work, _agent("careful_agent.py"), DUPLICATE)
    )
    return result, metrics, work


class TestTheRunIsMarkedAsEmpty:
    def test_every_task_applied_nothing(self, swallowed):
        _, metrics, _ = swallowed
        assert metrics["effects_by_task"] == {"t1": 0, "t2": 0}
        assert metrics["expected_effects_by_task"] == {"t1": 1, "t2": 1}
        assert metrics["nothing_applied"] is True
        assert metrics["runs_applied_nothing"] == 1
        assert metrics["runs_measured"] == 1

    def test_the_control_applied_something(self, applied):
        _, metrics, _ = applied
        assert metrics["effects_by_task"] == {"t1": 1, "t2": 1}
        assert metrics["nothing_applied"] is False
        assert metrics["runs_applied_nothing"] == 0

    def test_the_agent_did_reach_the_condition_it_is_judged_on(self, swallowed):
        """Not a scan that failed earlier: the tasks ran, and were uncertain.

        Without this the blocker would be condition 3's, and the new rule would
        never be the reason -- the test would pass while testing nothing.
        """
        _, metrics, _ = swallowed
        assert metrics["uncertain_tasks"] >= 1
        assert metrics["effect_oracle_status"] == "ok"
        assert metrics["task_oracle_status"] == {"t1": "ok", "t2": "ok"}

    def test_it_is_withheld_rather_than_false_when_nothing_read_the_state(
        self, tmp_path
    ):
        """No oracle is "we did not look", which is not "nothing applied"."""
        result, metrics = asyncio.run(
            _gate(tmp_path, _agent("careful_agent.py"), DUPLICATE, verify=False)
        )
        assert metrics["nothing_applied"] is None
        assert metrics["runs_applied_nothing"] == 0


class TestItIsNotAPass:
    def test_the_verdict_is_withheld(self, swallowed):
        result, _, _ = swallowed
        assert result.passed is None, render_scorecard(result)

    def test_the_control_passes(self, applied):
        """The same agent, the same schedule, a twin that applies: PASS.

        The pair is the whole argument -- the rule fires on the empty window
        and not on the agent.
        """
        result, _, _ = applied
        assert result.passed is True, render_scorecard(result)

    def test_the_blocker_names_the_reason(self, swallowed):
        result, _, _ = swallowed
        blocker = agent_verdict_blocker(result)
        assert blocker is not None
        assert "applied nothing" in blocker
        assert "1 of 1 run" in blocker

    def test_the_reason_is_printed_at_default_verbosity(self, swallowed):
        """The 1.4.1 lesson: the sentence explaining the number is not one
        flag away from the person reading the number."""
        result, _, _ = swallowed
        lines = verdict_lines(result)
        assert lines and lines[0].startswith("NO VERDICT")
        assert "applied nothing" in lines[0]

        card = strip_ansi(render_scorecard(result))
        assert "applied nothing" in card

    def test_the_caveat_sits_beside_the_effect_metrics(self, swallowed):
        result, _, _ = swallowed
        behavior = result.probe("behavior")
        caveat = next(
            c for c in behavior.caveats if "applied\nnothing" in c.reason
            or "applied nothing" in c.reason
        )
        assert "duplicate_mutations" in caveat.metrics
        assert caveat.effect == "annotate"


class TestItIsCoverage:
    """No score moves. The rule declines a verdict; it does not add a penalty."""

    def test_lost_effects_is_still_the_servers_and_still_unscored(self, swallowed):
        _, metrics, _ = swallowed
        assert metrics["lost_effects"] == 2
        assert metrics["lost_effect_tasks"] == ["t1", "t2"]
        # The agent said what it was told. Reading the claim against the state
        # would charge it for the server's loss.
        assert metrics["task_claims"] == {"t1": True, "t2": True}
        assert metrics["unsupported_claims"] == 0

    def test_no_check_reads_it(self, swallowed):
        result, _, _ = swallowed
        assert all(
            c.metric not in ("lost_effects", "nothing_applied",
                             "runs_applied_nothing")
            for c in result.checks
        )

    def test_the_score_is_the_same_as_the_run_that_applied(self, swallowed, applied):
        """Identical numbers, one verdict withheld. If the rule ever becomes a
        penalty this is the test that notices."""
        assert swallowed[0].score == applied[0].score
        assert swallowed[0].cap_reason is None
        assert applied[0].cap_reason is None

    def test_the_behaviour_dimension_was_still_measured(self, swallowed):
        result, _, _ = swallowed
        dimension = next(d for d in result.breakdown if d.probe == "behavior")
        assert dimension.measured
        assert dimension.not_scored is None


class TestCiExitsTwo:
    """Exit 2, not 1: nothing failed a policy, and a gate must not go green."""

    def test_ci_exits_two_and_says_why(self, tmp_path):
        out = tmp_path / "out.json"
        result = CliRunner().invoke(cli, [
            "ci", "--target", "agent",
            "--agent", _agent("careful_agent.py"),
            "--tasks", str(TASKS),
            "--upstream", _upstream(tmp_path, *SWALLOW),
            "--verify-tool", "effects", "--verify-count", "entries",
            "--work-dir", str(tmp_path / "work"),
            "--allow-mutating", "--fault-rate", "0.5", "--seed", "7",
            "--json-out", str(out),
        ])
        assert result.exit_code == 2, result.output
        assert "NO VERDICT" in result.output

        # The record is written even without a verdict: a NO VERDICT run is
        # exactly the one someone reads afterwards.
        exported = json.loads(out.read_text())
        behavior = next(
            p for p in exported["probes"] if p["probe"] == "behavior"
        )
        assert behavior["metrics"]["runs_applied_nothing"] >= 1
        assert exported["passed"] is None


class TestReadOnlyTasksDoNotTripIt:
    def test_a_task_expecting_no_effects_is_not_a_coverage_hole(self, tmp_path):
        """`expected_effects: 0` is a read-only task, and a run made only of
        those applied nothing because nothing was asked of it."""
        from ratemyagent.probes.agent_metrics import effect_metrics

        tasks = {
            "t1": {
                "expected_effects": 0, "claimed_ok": True, "outcome": "completed",
                "effects": 0, "oracle_status": "ok", "calls": 1,
                "delivered_ok": True, "before": 0,
            },
        }
        assert effect_metrics(tasks, None)["nothing_applied"] is False

    def test_but_one_mutating_task_among_them_still_counts(self, tmp_path):
        from ratemyagent.probes.agent_metrics import effect_metrics

        tasks = {
            "t1": {
                "expected_effects": 0, "claimed_ok": True, "outcome": "completed",
                "effects": 0, "oracle_status": "ok", "calls": 1,
                "delivered_ok": True, "before": 0,
            },
            "t2": {
                "expected_effects": 1, "claimed_ok": True, "outcome": "completed",
                "effects": 0, "oracle_status": "ok", "calls": 1,
                "delivered_ok": True, "before": 3,
            },
        }
        assert effect_metrics(tasks, None)["nothing_applied"] is True
