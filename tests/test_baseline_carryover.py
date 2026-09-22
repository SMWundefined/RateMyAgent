"""The scan's own clean pass is an input to every run after it (1.6.2).

**The run this exists for.** In the Phase D gate run
(`assets/moat/GATE-D.md` section 5) the agent invented an idempotency key
derived from the task's own id and payload, so it sent the *same* key in every
run. The upstream absorbs a repeated key -- that is the behaviour Phase C added
to tell a careful agent from a blind one -- and the key it matched had been
spent by the scan's own clean pass. Four of five runs then applied nothing, and
`duplicate_mutations` read 0 for a reason that had nothing to do with the
agent's care.

The tool knew both halves and said neither: it ran the clean pass, so it knew
what that pass applied, and the oracle reads the store before each task, so it
knew the window opened on a store that was not empty.

**Reported, never repaired.** Isolating state per run means changing the user's
own fixture -- a fresh state file, or a task payload that differs per run -- and
a scanner that silently rewrote either would be inventing the experiment rather
than running the one it was handed. `TestItDoesNotFixIt` holds that line.

**Not restricted to `--repeats`.** The chaos pass is a second run of the task
set at R=1 too. A rule written to fire only above R=1 would be fitted to the
shape of the run that found it rather than to the category, which is
`BUILD-1.6.1b.md` section 2's lesson.
"""

from __future__ import annotations

import asyncio

import pytest

from ratemyagent.probes.behavior import _baseline_state_carryover
from tests.test_agent_gate import DUPLICATE, _agent, _gate


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    work = tmp_path_factory.mktemp("carryover")
    result, metrics = asyncio.run(
        _gate(work, _agent("careful_agent.py"), DUPLICATE)
    )
    return result, metrics


def _caveat(result):
    return next(
        (c for c in result.probe("behavior").caveats if "clean pass" in c.reason),
        None,
    )


class TestItIsReported:
    def test_the_carryover_is_recorded(self, run):
        _, metrics = run
        assert metrics["baseline_state_carryover"] is True
        assert metrics["baseline_state_carryover_tasks"] == ["t1", "t2"]
        assert metrics["baseline_effects_applied"] == 2

    def test_at_r_equals_one_as_well(self, run):
        """The chaos pass is a run after the clean pass at R=1 too."""
        _, metrics = run
        assert metrics.get("repeats") in (None, 1)
        assert metrics["baseline_state_carryover"] is True

    def test_the_caveat_names_the_mechanism(self, run):
        result, _ = run
        caveat = _caveat(result)
        assert caveat is not None
        assert "idempotency key derives from the task's content" in caveat.reason
        assert "absorbs it across runs" in caveat.reason
        assert "duplicate_mutations" in caveat.metrics

    def test_it_says_what_to_do_about_it(self, run):
        result, _ = run
        assert "isolate the upstream's state per run" in (_caveat(result).remedy or "")


class TestItDoesNotFixIt:
    def test_the_verdict_and_score_are_untouched(self, run):
        """A caveat, not a rule. This run applied its effects and passes."""
        result, metrics = run
        assert result.passed is True
        assert metrics["effects_by_task"] == {"t1": 1, "t2": 1}
        assert result.cap_reason is None

    def test_no_check_reads_it(self, run):
        result, _ = run
        assert all(
            c.metric not in (
                "baseline_state_carryover", "baseline_effects_applied",
            )
            for c in result.checks
        )


class TestTheBeforeCountIsACount:
    def test_it_is_a_number_not_the_oracle_s_reply_shape(self, run):
        """One oracle answers with a list and another with a number. A consumer
        branching on which would be reading the verify tool rather than the
        state."""
        _, metrics = run
        # Reached through the recorded rows the probe kept for the chosen run.
        assert metrics["baseline_effects_applied"] == 2


class TestTheRule:
    """Unit arms, because the interesting cases cost an agent run each."""

    @staticmethod
    def _run(before, effects=1):
        return {"tasks": {"t1": {"before": before, "effects": effects}}}

    def test_it_needs_the_clean_pass_to_have_applied_something(self):
        assert _baseline_state_carryover([self._run(3)], None, {}) == {}
        assert _baseline_state_carryover([self._run(3)], None, {"t1": 0}) == {}

    def test_it_needs_a_window_that_opened_on_something(self):
        assert _baseline_state_carryover([self._run(0)], None, {"t1": 1}) == {}

    def test_an_unread_window_is_not_a_carryover(self):
        """`before: None` is "we could not tell", which is not "it was empty"
        and is not "it was full" either."""
        assert _baseline_state_carryover([self._run(None)], None, {"t1": 1}) == {}

    def test_it_fires_across_repeats_and_names_each_task_once(self):
        runs = [
            {"tasks": {"t1": {"before": 1}, "t2": {"before": 2}}},
            {"tasks": {"t1": {"before": 3}, "t2": {"before": 4}}},
        ]
        out = _baseline_state_carryover(runs, None, {"t1": 1, "t2": 1})
        assert out["baseline_state_carryover"] is True
        assert out["baseline_state_carryover_tasks"] == ["t1", "t2"]
        assert out["baseline_effects_applied"] == 2

    def test_it_falls_back_to_the_chosen_run_when_there_are_no_repeat_records(self):
        chosen = {"t1": {"before": 2, "effects": 1}}
        out = _baseline_state_carryover(None, chosen, {"t1": 1})
        assert out["baseline_state_carryover"] is True
