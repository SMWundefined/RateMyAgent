"""The Phase C gate: scripted agents, one twin, one forced schedule.

Every arm runs a real agent, which launches a real proxy, which talks to the
real twin fixture in `--mode append`. Every agent gets the same tasks and the
same forced schedule, so a difference between two scans is a difference
between the agents and nothing else.

**The reason is asserted against the twin's own ledger**, not only against our
metric. A metric confirming itself is the 1.3.0 failure: the twin records, for
every call it received, the idempotency key and whether the call was `applied`
or `absorbed`, and that file is written by a process that does not import this
package.

The schedules are explicit tables rather than `--fault-rate` draws, so each arm
gets the fault it is about. The one arm that uses the seeded draw is the
default-flags arm, which is there to run what a user runs.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ratemyagent import Policy, scan
from ratemyagent.cli import cli
from ratemyagent.models import FaultKind
from ratemyagent.outputs import render_scorecard
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.base import ProbeRefusal
from ratemyagent.probes.behavior import BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector, recovery_op_ids
from ratemyagent.proxy import invocation_rows, read_record
from ratemyagent.targets import AgentTarget, TargetError

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"
DEMO_TASKS = AGENTS / "tasks-demo.json"

LOST = FaultKind.RESPONSE_LOST
ERROR = FaultKind.SERVER_ERROR
LIMIT = FaultKind.RATE_LIMIT

#: One lost reply on t1's first call. Both agents retry; only a key saves the
#: upstream from applying it twice.
DUPLICATE = {("t1", "event", 1): LOST}
#: Every attempt at t1 fails with a delivered error, and nothing is applied.
EXHAUSTED = {("t1", "event", n): ERROR for n in (1, 2, 3)}
#: Two delivered failures in a row on each task: two waits to compare.
BACKOFF = {(t, "event", n): ERROR for t in ("t1", "t2") for n in (1, 2)}
#: One rate limit per task, each with the injected 1s hint.
RATE_LIMITED = {("t1", "event", 1): LIMIT, ("t2", "event", 1): LIMIT}


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _upstream(tmp_path: Path, *extra: str) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--state", str(tmp_path / "state.jsonl"),
        "--calls", str(tmp_path / "calls.jsonl"),
        *extra,
    ])


def _ledger(tmp_path: Path) -> list[dict]:
    """The twin's own account of every call it received."""
    path = tmp_path / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _target(tmp_path: Path, agent: str, *, verify: bool = True, tasks: Path = TASKS,
            twin: tuple[str, ...] = (), cls: type = AgentTarget) -> AgentTarget:
    return cls(
        agent_command=agent,
        tasks_path=tasks,
        upstream=_upstream(tmp_path, *twin),
        work_dir=tmp_path / "work",
        allow_mutating=True,
        verify_tool="effects" if verify else None,
        verify_count="entries" if verify else None,
    )


async def _gate(tmp_path: Path, agent: str, schedule: dict, **kwargs):
    target = _target(tmp_path, agent, **kwargs)
    result = await scan(
        target,
        probes=[AgentBaseline(), FaultInjector(schedule=schedule), BehaviorAnalyzer()],
        policy=Policy.default(),
    )
    return result, result.probe("behavior").metrics


class TestDuplicateMutations:
    """Design tests 9 and 13: the same lost reply, two agents, two outcomes."""

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        async def both():
            out = {}
            for name in ("careful_agent.py", "blind_agent.py"):
                work = tmp_path_factory.mktemp(name.split("_")[0])
                result, metrics = await _gate(work, _agent(name), DUPLICATE)
                out[name.split("_")[0]] = (result, metrics, _ledger(work), work)
            return out
        return asyncio.run(both())

    def test_careful_applies_nothing_twice_and_passes(self, runs):
        result, metrics, _, _ = runs["careful"]
        assert metrics["duplicate_mutations"] == 0
        assert metrics["effects_by_task"] == {"t1": 1, "t2": 1}
        assert result.passed is True, render_scorecard(result)

    def test_blind_applies_one_twice_and_is_capped(self, runs):
        result, metrics, _, _ = runs["blind"]
        assert metrics["duplicate_mutations"] == 1
        assert metrics["effects_by_task"] == {"t1": 2, "t2": 1}
        assert metrics["duplicate_mutation_tasks"] == {"t1": 2}
        assert result.score == 49
        assert result.passed is False
        assert "duplicate_mutation_max" in (result.cap_reason or "")

    def test_the_reason_is_in_the_twins_ledger(self, runs):
        """Careful: one key, applied then absorbed. Blind: no key, applied twice."""
        careful = [row for row in runs["careful"][2] if row["args"]["id"] == "alpha"]
        blind = [row for row in runs["blind"][2] if row["args"]["id"] == "alpha"]
        # Two per agent in the chaos pass, one each from the baseline before it.
        assert [row["effect"] for row in careful] == ["applied", "applied", "absorbed"]
        chaos_keys = {row["idempotency_key"] for row in careful[1:]}
        assert len(chaos_keys) == 1 and None not in chaos_keys
        # The baseline run is a different operation, so it carries another key.
        assert careful[0]["idempotency_key"] not in chaos_keys

        assert [row["effect"] for row in blind] == ["applied", "applied", "applied"]
        assert {row["idempotency_key"] for row in blind} == {None}

    def test_the_two_agents_saw_the_same_faults(self, runs):
        """The schedule is identical, so the calls are too: the only difference
        the scan reports is the one the ledger explains."""
        def shape(work):
            rows = invocation_rows(read_record(work / "work" / "record-chaos-t1.jsonl"))
            return [(row["attempt"], row["ok"], row["injected"]) for row in rows]
        assert shape(runs["careful"][3]) == shape(runs["blind"][3])
        assert shape(runs["careful"][3]) == [(1, False, "response_lost"), (2, True, None)]

    def test_amplification_is_scored_against_the_clean_path(self, runs):
        """Mutation G: withheld when the target does not run its own loop."""
        for name in ("careful", "blind"):
            result, metrics, _, _ = runs[name]
            assert metrics["retry_amplification"] == pytest.approx(1.5)
            assert metrics["clean_path_calls"] == 2
            assert metrics["calls_under_fault"] == 3
            check = next(c for c in result.checks if c.name == "retry_amplification_max")
            assert not check.skipped

    def test_attribution_is_per_task_window(self, runs):
        for name in ("careful", "blind"):
            metrics = runs[name][1]
            assert metrics["effect_attribution"] == "task_window"
            assert metrics["task_oracle_status"] == {"t1": "ok", "t2": "ok"}

    def test_recovery_is_reported_and_not_scored(self, runs):
        result, metrics, _, _ = runs["careful"]
        assert metrics["recovery_rate"] is None
        assert metrics["recovery_floor"] is None
        assert metrics["unscored_recovery_rate"] == 1.0
        check = next(c for c in result.checks if c.name == "recovery_rate_min")
        assert check.skipped

    def test_the_verdict_is_on_the_scorecard(self, runs):
        careful = render_scorecard(runs["careful"][0])
        blind = render_scorecard(runs["blind"][0])
        assert "PASS: score 100" in careful
        assert "FAIL: score 49" in blind
        assert "Agent behavior (experimental)" in careful
        assert "t1: claimed ok, applied 2 of 1" in blind


class TestUnsupportedClaims:
    """Every attempt at t1 fails. Only the optimistic agent says it worked."""

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        async def all_three():
            out = {}
            for name in ("careful_agent.py", "blind_agent.py", "optimistic_agent.py"):
                work = tmp_path_factory.mktemp(name.split("_")[0])
                out[name.split("_")[0]] = await _gate(work, _agent(name), EXHAUSTED)
            return out
        return asyncio.run(all_three())

    def test_optimistic_claims_what_the_record_does_not_show(self, runs):
        metrics = runs["optimistic"][1]
        assert metrics["unsupported_claims"] == 1
        # E_t beside it: the claim was false, and nothing was applied either.
        assert metrics["unsupported_claim_tasks"] == {"t1": 0}

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_honest_agents_make_no_unsupported_claim(self, runs, name):
        metrics = runs[name][1]
        assert metrics["unsupported_claims"] == 0
        assert metrics["task_claims"] == {"t1": False, "t2": True}
        # Failure claimed, nothing applied: not a lost acknowledgement.
        assert metrics["lost_acknowledgements"] == 0

    def test_it_is_printed_and_not_scored(self, runs):
        result = runs["optimistic"][0]
        text = render_scorecard(result)
        assert re.search(r"unsupported claims\s+1\s+unscored", text)
        assert all(c.metric != "unsupported_claims" for c in result.checks)


class TestLostEffectsAreTheServers:
    """`--swallow-after 2`: the clean pass applies both tasks, and from then on
    every call is acknowledged and nothing is applied.

    A server that swallows from the first call is refused at baseline -- it
    cannot be told from one whose state the oracle cannot see (below) -- so the
    loss has to begin after the clean pass. The agents are told success and say
    success. That is the server's fault,
    and a measurement that charged the agent for it would be reading the claim
    against the state instead of against the record.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        async def both():
            out = {}
            for name in ("careful_agent.py", "blind_agent.py"):
                work = tmp_path_factory.mktemp(name.split("_")[0])
                out[name.split("_")[0]] = await _gate(
                    work, _agent(name), DUPLICATE, twin=("--swallow-after", "2"),
                )
            return out
        return asyncio.run(both())

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_the_server_lost_every_effect(self, runs, name):
        metrics = runs[name][1]
        assert metrics["lost_effects"] == 2
        assert metrics["lost_effect_tasks"] == ["t1", "t2"]
        assert metrics["effects_by_task"] == {"t1": 0, "t2": 0}

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_and_the_agent_is_not_charged_for_it(self, runs, name):
        """Mutation M: reading the claim against E_t == 0 fails here."""
        metrics = runs[name][1]
        assert metrics["task_claims"] == {"t1": True, "t2": True}
        assert metrics["unsupported_claims"] == 0


class TestTiming:
    """`retry_after_honored` and `backoff_shape`, from wall clock in the record."""

    @pytest.fixture(scope="class")
    @classmethod
    def limited(cls, tmp_path_factory):
        async def both():
            out = {}
            for name in ("careful_agent.py", "blind_agent.py"):
                work = tmp_path_factory.mktemp(name.split("_")[0])
                out[name.split("_")[0]] = await _gate(work, _agent(name), RATE_LIMITED)
            return out
        return asyncio.run(both())

    @pytest.fixture(scope="class")
    @classmethod
    def backoff(cls, tmp_path_factory):
        async def three():
            out = {}
            for label, command in (
                ("careful", _agent("careful_agent.py")),
                # A constant delay: still blind, and long enough to measure.
                ("blind", _agent("blind_agent.py", "--sleep", "0.15")),
                ("blind_nosleep", _agent("blind_agent.py")),
            ):
                work = tmp_path_factory.mktemp(label)
                out[label] = await _gate(work, command, BACKOFF)
            return out
        return asyncio.run(three())

    def test_careful_honours_the_hint(self, limited):
        metrics = limited["careful"][1]
        assert metrics["retry_after_retries"] == 2
        assert metrics["retry_after_honored"] == 1.0

    def test_blind_does_not(self, limited):
        """Mutation F: counting any wait as honored gives 1.0 here."""
        metrics = limited["blind"][1]
        assert metrics["retry_after_retries"] == 2
        assert metrics["retry_after_honored"] == 0.0

    def test_the_hint_convention_is_caveated(self, limited):
        result = limited["blind"][0]
        caveats = [c for c in result.caveats() if "retry_after_honored" in c.metrics]
        assert any("tool error body" in c.reason for c in caveats)
        assert "a convention a real client may not read" in " ".join(
            render_scorecard(result).split()
        )

    def test_careful_backs_off_growing(self, backoff):
        metrics = backoff["careful"][1]
        assert metrics["backoff_shape"] == "growing"
        # 0.05s then 0.2s: a ratio of four, far from any noise.
        assert metrics["backoff_growth"] > 2.5
        assert metrics["backoff_gap_pairs"] == 2

    def test_blind_with_a_fixed_delay_is_flat(self, backoff):
        metrics = backoff["blind"][1]
        assert metrics["backoff_shape"] == "flat"
        assert 0.67 < metrics["backoff_growth"] < 1.5

    def test_gaps_below_resolution_are_not_a_shape(self, backoff):
        """No delay at all: the gaps are pipe overhead, and a ratio of two
        noises is not a backoff. n/a, not flat and not 1.0."""
        metrics = backoff["blind_nosleep"][1]
        assert metrics["backoff_shape"] is None
        assert metrics["backoff_growth"] is None

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_no_rate_limit_means_nothing_to_honour(self, backoff, name):
        """The empty-set arm: n/a, never 1.0 over zero retries."""
        result, metrics = backoff[name]
        assert metrics["retry_after_retries"] == 0
        assert metrics["retry_after_honored"] is None
        assert re.search(r"retry-after honored\s+n/a", render_scorecard(result))


class TestTheVerdictRule:
    """D4: an agent scan gets a verdict only with every task's effects read."""

    async def test_no_verify_tool_is_no_verdict(self, tmp_path):
        result, metrics = await _gate(
            tmp_path, _agent("blind_agent.py"), DUPLICATE, verify=False,
        )
        assert metrics["effect_oracle_status"] == "absent"
        assert metrics["duplicate_mutations"] is None
        assert result.passed is None
        # Unsupported claims do not need the oracle; they are read off the record.
        assert metrics["unsupported_claims"] == 0
        text = render_scorecard(result)
        assert "NO VERDICT: no --verify-tool" in text
        # `"PASS:"` rather than `"PASS"`: the scorecard verdict is
        # `PASS: score N`, and the loose form would also be satisfied by
        # `PASS, UNRECONCILED`, queued for its own release. Tightened in 1.7.2
        # ahead of it.
        assert "PASS:" not in text

    async def test_one_unread_task_is_no_verdict(self, tmp_path):
        """Mutation N: a rule that ignored the oracle status would pass this."""

        class FailsAroundT2(AgentTarget):
            reads = 0

            async def read_effect_entries(self):
                type(self).reads += 1
                # Reads 1-4 are the baseline's two windows (F2); 5 and 6 are
                # t1's chaos window; 7 opens t2's.
                if type(self).reads == 7:
                    return None
                return await super().read_effect_entries()

        result, metrics = await _gate(
            tmp_path, _agent("careful_agent.py"), DUPLICATE, cls=FailsAroundT2,
        )
        assert metrics["task_oracle_status"] == {"t1": "ok", "t2": "failed"}
        assert metrics["duplicate_mutations"] is None
        assert result.passed is None
        assert "NO VERDICT: the verify tool did not read the upstream around task t2" in (
            render_scorecard(result)
        )

    def test_ci_exits_two_without_a_verify_tool(self, tmp_path):
        result = CliRunner().invoke(cli, [
            "ci", "--target", "agent",
            "--agent", _agent("careful_agent.py"),
            "--tasks", str(TASKS),
            "--upstream", _upstream(tmp_path),
            "--allow-mutating",
        ])
        assert result.exit_code == 2, result.output
        assert "NO VERDICT" in result.output
        assert "no --verify-tool" in result.output

    def test_a_missing_expected_effects_exits_two(self, tmp_path):
        tasks = tmp_path / "tasks.json"
        tasks.write_text(json.dumps({"tasks": [
            {"id": "t1", "prompt": "p", "tool": "event",
             "arguments": {"id": "a", "payload": "p"}},
        ]}))
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", _agent("careful_agent.py"),
            "--tasks", str(tasks), "--upstream", _upstream(tmp_path),
            "--allow-mutating", "--verify-tool", "effects", "--verify-count", "entries",
        ])
        assert result.exit_code == 2, result.output
        assert "expected_effects" in result.output
        assert not (tmp_path / "state.jsonl").exists()


class TestSequentialOnly:
    async def test_a_second_task_in_flight_is_refused(self, tmp_path):
        target = _target(tmp_path, _agent("careful_agent.py"), verify=False)
        await target.setup()
        try:
            outcomes = await asyncio.gather(
                target.invoke(target.sample_request(0)),
                target.invoke(target.sample_request(1)),
                return_exceptions=True,
            )
        finally:
            await target.teardown()
        refused = [o for o in outcomes if isinstance(o, TargetError)]
        assert len(refused) == 1
        assert "one task at a time" in str(refused[0])


class TestNoOpIdOnTheAgentPath:
    """The "fifth namespace" of design §g.3 does not exist.

    The agent path generates no op id: the agent writes its own arguments, the
    proxy relays them byte-identical, and neither `AgentTarget` nor the proxy's
    `MCPTarget(probe_traffic=False)` derives one. Nothing is registered, so
    nothing can collide with the constructor salt or any phase's namespace.
    """

    async def test_nothing_is_registered(self, tmp_path):
        target = _target(tmp_path, _agent("careful_agent.py"))
        assert not hasattr(target, "op_id")
        assert target.uses_op_id is False
        assert recovery_op_ids(target, ProbeConfig()) == {}

    async def test_the_upstream_receives_the_task_arguments_verbatim(self, tmp_path):
        await _gate(tmp_path, _agent("blind_agent.py"), DUPLICATE)
        tasks = {t["arguments"]["id"]: t["arguments"]
                 for t in json.loads(TASKS.read_text())["tasks"]}
        for row in _ledger(tmp_path):
            assert row["args"] == tasks[row["args"]["id"]]
            assert row["idempotency_key"] is None


class TestTheOracleMustSeeTheAgentsUpstream:
    """F2: a clean, acknowledged task the oracle cannot see refuses at baseline.

    The agent's proxy starts its own copy of a stdio upstream per task and the
    oracle reads through another. An in-memory upstream gives every copy an
    empty store, so without this check the chaos pass would count zero effects
    for every task -- no duplicates for blind, lost effects for everyone -- and
    score it.
    """

    def _scan(self, tmp_path, *twin):
        return CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", _agent("blind_agent.py"),
            "--tasks", str(TASKS),
            "--upstream", "stdio://" + shlex.join(
                [sys.executable, str(TWIN), "--mode", "append",
                 "--role", "{role}", *twin]
            ),
            "--allow-mutating", "--verify-tool", "effects", "--verify-count", "entries",
            "--fault-rate", "0.7",
        ])

    def test_an_in_memory_upstream_is_refused(self, tmp_path):
        result = self._scan(tmp_path)
        assert result.exit_code == 2, result.output
        assert (
            "the verify tool does not see the effects the agent's upstream "
            "applied; the upstream's state must persist outside its process"
        ) in " ".join(result.output.split())
        assert "t1 saw 0 of 1" in result.output
        assert "Score:" not in result.output

    def test_a_server_that_never_applies_is_refused_the_same_way(self, tmp_path):
        """Indistinguishable from outside, and the message names both."""
        result = self._scan(tmp_path, "--state", str(tmp_path / "s.jsonl"),
                            "--swallow-every", "1")
        assert result.exit_code == 2, result.output
        assert "acknowledged work it did not apply" in " ".join(result.output.split())


def _ci(work: Path, name: str, tasks: Path, *extra: str):
    """`ci --target agent` with only the flags an agent verdict requires."""
    return CliRunner().invoke(cli, [
        "ci", "--target", "agent",
        "--agent", _agent(name),
        "--tasks", str(tasks),
        "--upstream", _upstream(work),
        "--allow-mutating",
        "--verify-tool", "effects", "--verify-count", "entries",
        "--json-out", str(work / "scan.json"),
        *extra,
    ])


def _behavior(work: Path) -> dict:
    data = json.loads((work / "scan.json").read_text())
    return next(p for p in data["probes"] if p["probe"] == "behavior")["metrics"]


class TestTheFullGateOnDefaultFlags:
    """The standing rule. Nothing beyond the required flags.

    Default seed, fault rate, warmup, concurrency and requests. At those
    defaults the seeded schedule over the demo task file puts no silent fault
    on any call either agent makes, so neither had an outcome it could not
    know. The honest result is NO VERDICT and exit 2 -- not a PASS for careful
    and not a PASS for blind, which is what 1.5.0 as first staged printed.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        out = {}
        for name in ("careful_agent.py", "blind_agent.py"):
            work = tmp_path_factory.mktemp(name.split("_")[0])
            out[name.split("_")[0]] = (_ci(work, name, DEMO_TASKS), work)
        return out

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_no_opportunity_means_no_verdict(self, runs, name):
        result, work = runs[name]
        assert result.exit_code == 2, result.output
        assert (
            "NO VERDICT  no task had a call whose outcome was unknown; "
            "raise --fault-rate." in result.output
        )
        # `ci` output, so `"PASS  score"` -- its verdict line carries no colon.
        assert "PASS  score" not in result.output
        metrics = _behavior(work)
        assert metrics["uncertain_tasks"] == 0
        assert "duplicate_opportunities" not in metrics
        assert metrics["duplicate_mutations"] == 0
        assert metrics["effect_oracle_status"] == "ok"

    def test_the_flags_were_the_defaults(self, runs):
        data = json.loads((runs["careful"][1] / "scan.json").read_text())
        assert data["config"]["seed"] == 1337
        assert data["config"]["requests"] == 20
        assert data["config"]["warmup"] == 1
        assert data["config"]["concurrency"] == 5
        assert data["config"]["extra"]["fault_rate"] == 0.2
        assert data["passed"] is None


class TestTheFullGateAtFaultRate07:
    """The same gate with `--fault-rate 0.7`, and nothing else changed.

    At 0.7 the default seed puts two lost replies on t10, so both agents have
    an outcome they cannot know, and the verdict separates them.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        out = {}
        for name in ("careful_agent.py", "blind_agent.py"):
            work = tmp_path_factory.mktemp(name.split("_")[0])
            out[name.split("_")[0]] = (
                _ci(work, name, DEMO_TASKS, "--fault-rate", "0.7"), work,
            )
        return out

    def test_careful_passes(self, runs):
        result, work = runs["careful"]
        assert result.exit_code == 0, result.output
        metrics = _behavior(work)
        assert metrics["duplicate_mutations"] == 0
        assert metrics["uncertain_tasks"] >= 1

    def test_blind_fails_on_a_duplicate_the_ledger_shows(self, runs):
        result, work = runs["blind"]
        assert result.exit_code == 1, result.output
        data = json.loads((work / "scan.json").read_text())
        duplicates = _behavior(work)["duplicate_mutations"]
        assert duplicates > 0
        assert data["score"] == 49

        # The ledger agrees, task by task: every extra applied call in the
        # chaos pass is one the metric counted.
        applied: dict[str, int] = {}
        for row in _ledger(work):
            if row.get("effect") == "applied":
                applied[row["args"]["id"]] = applied.get(row["args"]["id"], 0) + 1
        # One applied call per task came from the baseline pass.
        extra = sum(max(0, count - 2) for count in applied.values())
        assert extra == duplicates


# -- Tier 0: tasks of more than one write ------------------------------------
#
# Every task above makes one write, so every task window above holds one
# operation. `assets/moat/tier0/DESIGN-TIER-0.md` asks what the window does
# with two or three: it is a net count, `len(after) - len(before)`, and at
# N >= 2 a duplicated write and a write that never landed cancel inside it.
#
# The arms are data, shared with the maintainer's runner in `assets/moat/tier0/`
# so the suite and the recorded runs cannot drift. Every effect assertion is
# made against the twin's own ledger as well as against the metric. Where the
# tool's conclusion disagrees with the ledger, the test asserts the correct
# conclusion and is a strict xfail naming the defect: it turns red the day the
# defect is fixed and the marker has not been removed. P1-P4 of the design are
# what would fix them. Ledger assertions stay plain.

MULTI_TASKS = AGENTS / "tasks-multi-write.json"
GRID_TASKS = ("n1", "n2-first", "n2-last", "n3-first", "n3-middle", "n3-last")
CANCEL_TASKS = ("x1-n2", "x2-n3", "x3-n3")
CLEAN_TASKS = ("n1-clean-1", "n1-clean-2", "n1-clean-3", "n1-clean-4")

#: One lost reply per task, on the first attempt at write p. Ordinals count every
#: call to the tool in the task, retries included, so with no earlier fault the
#: first attempt at write p is ordinal p.
GRID = {
    ("n1", "event", 1): LOST,
    ("n2-first", "event", 1): LOST,
    ("n2-last", "event", 2): LOST,
    ("n3-first", "event", 1): LOST,
    ("n3-middle", "event", 2): LOST,
    ("n3-last", "event", 3): LOST,
}
#: A lost reply on one write, then every attempt at the last write refused. The
#: ordinals carry the lost write's retry: x1's second write starts at 3.
CANCEL = {
    ("x1-n2", "event", 1): LOST,
    **{("x1-n2", "event", n): ERROR for n in (3, 4, 5)},
    ("x2-n3", "event", 1): LOST,
    **{("x2-n3", "event", n): ERROR for n in (4, 5, 6)},
    ("x3-n3", "event", 2): LOST,
    **{("x3-n3", "event", n): ERROR for n in (4, 5, 6)},
}


@dataclass(frozen=True)
class Arm:
    """One configuration of the Tier 0 plan (`DESIGN-TIER-0.md` 4.2)."""

    key_mode: str
    tasks: tuple[str, ...]
    #: The forced table. None means the seeded draw, which only the defaults
    #: arm uses.
    schedule: dict | None
    #: False runs the chaos pass with no clean pass in front of it.
    baseline: bool = True
    #: Extra twin flags.
    twin: tuple[str, ...] = ()
    #: `ProbeConfig` fields this arm sets. Empty means `scan()` is given no
    #: config at all, which is what the defaults arm must be.
    config: dict[str, Any] = field(default_factory=dict)


REFUSE = ("--key-conflict", "refuse")

TIER0_ARMS: dict[str, Arm] = {
    "A1": Arm("per-write", GRID_TASKS, GRID),
    "A2": Arm("none", GRID_TASKS, GRID),
    "A3": Arm("per-attempt", GRID_TASKS, GRID),
    "A4": Arm("per-task", GRID_TASKS, GRID),
    "A4b": Arm("per-task", GRID_TASKS, GRID, baseline=False),
    "A4r": Arm("per-task", GRID_TASKS, GRID, twin=REFUSE),
    "A4br": Arm("per-task", GRID_TASKS, GRID, baseline=False, twin=REFUSE),
    "A5k": Arm("per-write", CANCEL_TASKS, CANCEL),
    "A5n": Arm("none", CANCEL_TASKS, CANCEL),
    "A5c": Arm("per-attempt", CANCEL_TASKS, CANCEL),
    "A5d": Arm("none", CANCEL_TASKS + CLEAN_TASKS, CANCEL),
    "A6": Arm("none", ("n3-middle",), None),
}


def _multi_tasks() -> list[dict]:
    return json.loads(MULTI_TASKS.read_text(encoding="utf-8"))["tasks"]


def tier0_subset(ids: tuple[str, ...], dest: Path) -> Path:
    """The named task file cut down to one arm's tasks, in that order."""
    by_id = {task["id"]: task for task in _multi_tasks()}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"tasks": [by_id[task] for task in ids]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


def tier0_target(work: Path, arm: Arm) -> AgentTarget:
    return AgentTarget(
        agent_command=_agent("multi_write_agent.py", "--key-mode", arm.key_mode),
        tasks_path=tier0_subset(arm.tasks, work / "tasks.json"),
        upstream=_upstream(work, *arm.twin),
        work_dir=work / "work",
        allow_mutating=True,
        verify_tool="effects",
        verify_count="entries",
    )


async def tier0_scan(work: Path, arm: Arm):
    """Run one arm. Raises `ProbeRefusal` when the scan refuses."""
    probes: list = [FaultInjector(schedule=arm.schedule), BehaviorAnalyzer()]
    if arm.baseline:
        probes.insert(0, AgentBaseline())
    return await scan(
        tier0_target(work, arm),
        probes=probes,
        policy=Policy.default(),
        config=ProbeConfig(**arm.config) if arm.config else None,
    )


def _outcome(row: dict) -> str:
    if row.get("status"):
        return row["status"]
    if row.get("swallowed"):
        return "swallowed"
    return row["effect"]


def tier0_windows(ledger: list[dict], task: str) -> list[dict[str, dict[str, int]]]:
    """The task's ledger rows, one dict per window in generation order.

    Each window maps a write's id to how many rows had each outcome. A task's
    ids are its own (`<task>-w<k>`), and every call inside one window is served
    in one generation, so generation order is pass order.
    """
    windows: dict[int, dict[str, dict[str, int]]] = {}
    for row in ledger:
        event = row["args"]["id"]
        if not event.startswith(f"{task}-w"):
            continue
        counts = windows.setdefault(row["generation"], {}).setdefault(event, {})
        counts[_outcome(row)] = counts.get(_outcome(row), 0) + 1
    return [windows[generation] for generation in sorted(windows)]


def _applied(window: dict[str, dict[str, int]], task: str, writes: int) -> tuple:
    return tuple(
        window.get(f"{task}-w{k}", {}).get("applied", 0) for k in range(1, writes + 1)
    )


def _writes_of(task: str) -> int:
    return next(len(t["writes"]) for t in _multi_tasks() if t["id"] == task)


async def _arms(factory, *names: str) -> dict[str, tuple]:
    """Run arms one after another: (result or refusal, behaviour metrics, ledger)."""
    out = {}
    for name in names:
        work = factory.mktemp(name)
        try:
            result = await tier0_scan(work, TIER0_ARMS[name])
        except ProbeRefusal as exc:
            out[name] = (exc, None, _ledger(work))
            continue
        out[name] = (result, result.probe("behavior").metrics, _ledger(work))
    return out


#: Per write, how many times the chaos pass applied it (DESIGN-TIER-0.md 3.1).
GRID_APPLIED = {
    "per-write": {"n1": (1,), "n2-first": (1, 1), "n2-last": (1, 1),
                  "n3-first": (1, 1, 1), "n3-middle": (1, 1, 1), "n3-last": (1, 1, 1)},
    "none": {"n1": (2,), "n2-first": (2, 1), "n2-last": (1, 2),
             "n3-first": (2, 1, 1), "n3-middle": (1, 2, 1), "n3-last": (1, 1, 2)},
}
GRID_APPLIED["per-attempt"] = GRID_APPLIED["none"]
GRID_PLACEMENT = (
    "n1:event#1=response_lost, n2-first:event#1=response_lost, "
    "n2-last:event#2=response_lost, n3-first:event#1=response_lost, "
    "n3-middle:event#2=response_lost, n3-last:event#3=response_lost"
)


class TestMultiWriteGrid:
    """T1, the requested grid: one lost reply per task, N = 1..3, by key mode.

    Correct everywhere it runs: a duplicated write is counted once against the
    task that made it. The count cannot say *which* write -- the placement in
    the record can, and nothing joins the two.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A1", "A2", "A3"))

    @pytest.mark.parametrize("name", ["A1", "A2", "A3"])
    def test_the_ledger_applied_what_the_grid_says(self, runs, name):
        _, _, ledger = runs[name]
        expected = GRID_APPLIED[TIER0_ARMS[name].key_mode]
        for task, applied in expected.items():
            baseline, chaos = tier0_windows(ledger, task)
            assert _applied(baseline, task, len(applied)) == (1,) * len(applied)
            assert _applied(chaos, task, len(applied)) == applied, task

    @pytest.mark.parametrize("name", ["A1", "A2", "A3"])
    def test_the_window_count_is_the_ledger_net(self, runs, name):
        _, metrics, ledger = runs[name]
        for task in GRID_TASKS:
            chaos = tier0_windows(ledger, task)[-1]
            assert metrics["effects_by_task"][task] == sum(
                _applied(chaos, task, _writes_of(task))
            ), task

    @pytest.mark.parametrize("name", ["A1", "A2", "A3"])
    def test_every_arm_saw_the_same_faults(self, runs, name):
        _, metrics, _ = runs[name]
        assert metrics["realized_placement"] == GRID_PLACEMENT
        assert metrics["uncertain_tasks"] == 6
        assert metrics["clean_path_calls"] == 14
        assert metrics["calls_under_fault"] == 20
        assert metrics["retry_amplification"] == pytest.approx(20 / 14)

    def test_a_key_per_write_passes(self, runs):
        result, metrics, _ = runs["A1"]
        assert metrics["duplicate_mutations"] == 0
        assert metrics["duplicate_deliveries"] == 6
        assert (result.score, result.passed) == (100, True), render_scorecard(result)

    @pytest.mark.parametrize("name", ["A2", "A3"])
    def test_no_key_or_a_new_key_duplicates_once_per_task(self, runs, name):
        result, metrics, _ = runs[name]
        assert metrics["duplicate_mutations"] == 6
        assert metrics["duplicate_mutation_tasks"] == {
            "n1": 2, "n2-first": 3, "n2-last": 3,
            "n3-first": 4, "n3-middle": 4, "n3-last": 4,
        }
        assert (result.score, result.passed) == (49, False)
        assert "duplicate_mutation_max" in (result.cap_reason or "")

    def test_a_new_key_per_attempt_is_a_new_trajectory(self, runs):
        """The retry carries a different key, so a different fingerprint."""
        assert runs["A2"][1]["duplicate_deliveries"] == 6
        assert runs["A3"][1]["duplicate_deliveries"] == 0


class TestOneKeyForTheWholeTask:
    """T1, the misattributed cells: one key sent on every write of a task.

    The twin's operation is the task window, so write 2 onward is absorbed as a
    repeat of write 1. Today's conclusions are wrong in two ways
    (DESIGN-TIER-0.md 3.1): the full scan refuses and blames the upstream's
    persistence, and the chaos-only scan passes at 100 over eight writes that
    never landed. **Each wrong conclusion is a strict xfail asserting the right
    one**; P3 and P1 are what would turn them green. What the ledger shows, and
    what the tool counts correctly, stay plain tests.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A4", "A4b"))

    def test_the_full_scan_refuses_and_counts_every_short_task(self, runs):
        refusal, _, _ = runs["A4"]
        assert isinstance(refusal, ProbeRefusal)
        text = " ".join(str(refusal).split())
        assert ("n2-first saw 1 of 2, n2-last saw 1 of 2, n3-first saw 1 of 3, "
                "n3-middle saw 1 of 3, n3-last saw 1 of 3") in text
        assert "n1 saw" not in text

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P3): the baseline refusal blames the "
        "upstream's persistence when the record shows one idempotency_key on "
        "distinct writes -- the agent's own writes were absorbed as repeats"
    ))
    def test_the_refusal_names_the_agent_s_key_reuse(self, runs):
        refusal, _, _ = runs["A4"]
        text = " ".join(str(refusal).split())
        assert "idempotency_key" in text
        assert "the upstream's state must persist outside its process" not in text

    def test_the_ledger_shows_the_agent_s_own_writes_absorbed(self, runs):
        _, _, ledger = runs["A4"]
        for task in GRID_TASKS:
            (window,) = tier0_windows(ledger, task)
            writes = _writes_of(task)
            assert _applied(window, task, writes) == (1,) + (0,) * (writes - 1)
            absorbed = sum(c.get("absorbed", 0) for c in window.values())
            assert absorbed == writes - 1, task

    def test_the_chaos_only_scan_passes_over_absorbed_writes(self, runs):
        result, metrics, ledger = runs["A4b"]
        assert metrics["effects_by_task"] == {task: 1 for task in GRID_TASKS}
        for key in ("duplicate_mutations", "lost_effects", "lost_acknowledgements",
                    "unsupported_claims"):
            assert metrics[key] == 0, key
        assert metrics["uncertain_tasks"] == 6
        assert metrics["retry_amplification"] is None
        never = sum(
            _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task)).count(0)
            for task in GRID_TASKS
        )
        assert never == 8
        assert "n3-last: claimed ok, applied 1 of 3" in render_scorecard(result)

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P1): a chaos-only scan passes at 100 "
        "while 8 of the task set's writes were absorbed and never applied; no "
        "reading covers 0 < E < expected_effects"
    ))
    def test_absorbed_writes_are_not_a_pass(self, runs):
        result, _, _ = runs["A4b"]
        assert result.passed is not True, render_scorecard(result)


class TestOneKeyForTheWholeTaskRefused:
    """A4 and A4b against a twin at `--key-conflict refuse`.

    The reused key on a different write is now an error the agent sees, so it
    stops and claims failure. The full scan refuses for that -- the agent did
    not complete -- rather than for persistence. The chaos-only scan still
    passes at 100, now over five tasks the agent says it failed: no score reads
    a failed task (the 8b entry-32 shape).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A4r", "A4br"))

    def test_the_full_scan_refuses_on_incomplete_tasks(self, runs):
        refusal, _, ledger = runs["A4r"]
        assert isinstance(refusal, ProbeRefusal)
        text = " ".join(str(refusal).split())
        assert ("5 of 6 tasks did not complete with no faults injected "
                "(n2-first, n2-last, n3-first, n3-middle, n3-last)") in text
        rejected = sum(r.get("status") == "rejected" for r in ledger)
        applied = sum(r.get("effect") == "applied" for r in ledger)
        assert (applied, rejected) == (6, 15)

    def test_the_chaos_only_scan_counts_the_failed_tasks(self, runs):
        result, metrics, ledger = runs["A4br"]
        assert metrics["task_claims"] == {
            "n1": True, **{task: False for task in GRID_TASKS[1:]},
        }
        assert metrics["effects_by_task"] == {task: 1 for task in GRID_TASKS}
        assert metrics["duplicate_mutations"] == 0
        assert metrics["lost_acknowledgements"] == 0
        rejected = sum(r.get("status") == "rejected" for r in ledger)
        assert rejected == 15

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P1): a chaos-only scan passes at 100 "
        "with 5 of 6 tasks claimed failed and 8 writes never applied"
    ))
    def test_failed_tasks_with_writes_never_applied_are_not_a_pass(self, runs):
        result, _, _ = runs["A4br"]
        assert result.passed is not True, render_scorecard(result)

    def test_a_lost_reply_scheduled_onto_a_rejection_is_delivered(self, runs):
        """Registered as 6 uncertain tasks; measured 3 (BUILD-TIER-0.md).

        In n2-last, n3-middle and n3-last the scheduled `response_lost` lands on
        a call the twin rejected, and `FaultProxy._lose` leaves a failed reply
        alone, so the error is delivered and the agent is never uncertain. The
        record still stamps the call `injected: response_lost`: a fact about
        the record, asserted here. What `realized_placement` concludes from it
        is the xfail below.
        """
        result, metrics, _ = runs["A4br"]
        assert metrics["uncertain_tasks"] == 3
        assert set(metrics["uncertain_task_ids"]) == {"n1", "n2-first", "n3-first"}
        work = Path(result.target.metadata["work_dir"])
        rows = invocation_rows(read_record(work / "record-chaos-n2-last.jsonl"))
        stamped = [r for r in rows if r["injected"] == "response_lost"]
        assert len(stamped) == 1 and stamped[0]["replied_at"] is not None

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect: realized_placement reads the record's `injected` stamp, "
        "which is set even when FaultProxy._lose left a failed reply alone and "
        "it was delivered -- six lost replies named where three happened"
    ))
    def test_realized_placement_names_only_replies_that_were_lost(self, runs):
        _, metrics, _ = runs["A4br"]
        assert metrics["realized_placement"] == (
            "n1:event#1=response_lost, n2-first:event#1=response_lost, "
            "n3-first:event#1=response_lost"
        )


#: Per write, chaos-pass applications for the cancellation cells (3.2).
CANCEL_APPLIED = {
    "per-write": {"x1-n2": (1, 0), "x2-n3": (1, 1, 0), "x3-n3": (1, 1, 0)},
    "none": {"x1-n2": (2, 0), "x2-n3": (2, 1, 0), "x3-n3": (1, 2, 0)},
}
CANCEL_APPLIED["per-attempt"] = CANCEL_APPLIED["none"]


class TestADuplicateAndAMissingWriteCancel:
    """T1, two faults in one task: a write duplicated, the last one refused.

    The window nets the duplicate against the missing write (DESIGN-TIER-0.md
    3.2), so no-key reads `duplicate_mutations` 0 and the finding says the work
    was applied honestly; a key per write reads 0 too. On every scored row the
    two are identical -- entry 26's twin rule, failing. **The wrong conclusions
    are strict xfails asserting the right ones**; P2 is what would turn them
    green. The ledger, the window-equals-net invariant and everything the tool
    already gets right stay plain tests.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A5k", "A5n", "A5c", "A5d"))

    @pytest.mark.parametrize("name", ["A5k", "A5n", "A5c", "A5d"])
    def test_the_ledger_applied_what_the_cells_say(self, runs, name):
        _, metrics, ledger = runs[name]
        for task, applied in CANCEL_APPLIED[TIER0_ARMS[name].key_mode].items():
            chaos = tier0_windows(ledger, task)[-1]
            assert _applied(chaos, task, len(applied)) == applied, task
            assert metrics["effects_by_task"][task] == sum(applied)

    @pytest.mark.parametrize("name", ["A5k", "A5n", "A5c"])
    def test_every_agent_failed_every_task_and_amplified(self, runs, name):
        _, metrics, _ = runs[name]
        assert metrics["task_claims"] == {task: False for task in CANCEL_TASKS}
        assert metrics["retry_amplification"] == pytest.approx(17 / 8)

    def test_a_key_per_write_duplicates_nothing_and_fails_on_amplification(self, runs):
        """A5k's ledger holds no duplicate, so 0 and 89 are right for it."""
        result, metrics, _ = runs["A5k"]
        assert metrics["duplicate_mutations"] == 0
        assert metrics["lost_acknowledgements"] == 0
        assert (result.score, result.passed) == (89, False)
        assert "retry_amplification_max" in (result.cap_reason or "")

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P2): the task window nets a duplicated "
        "write against a write that never landed, so 3 duplicates in the ledger "
        "read duplicate_mutations 0 and the absolute cap never applies"
    ))
    @pytest.mark.parametrize("name", ["A5n", "A5c"])
    def test_the_duplicates_are_counted(self, runs, name):
        result, metrics, _ = runs[name]
        assert metrics["duplicate_mutations"] == 3
        assert (result.score, result.passed) == (49, False)
        assert "duplicate_mutation_max" in (result.cap_reason or "")

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P2): lost_acknowledgements compares net "
        "counts, so an agent whose duplicate cancelled a missing write is "
        "reported as having applied its work honestly"
    ))
    @pytest.mark.parametrize("name", ["A5n", "A5c"])
    def test_a_cancelled_task_is_not_a_lost_acknowledgement(self, runs, name):
        assert runs[name][1]["lost_acknowledgements"] == 0

    def test_diluted_below_the_amplification_limit(self, runs):
        """A5d: four clean single-write tasks bring amplification to 1.75,
        with three duplicates in the ledger."""
        _, metrics, ledger = runs["A5d"]
        assert metrics["retry_amplification"] == pytest.approx(21 / 12)
        duplicates = sum(
            max(0, applied - 1)
            for task in CANCEL_TASKS
            for applied in _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task))
        )
        assert duplicates == 3

    @pytest.mark.xfail(strict=True, reason=(
        "Tier 0 defect (DESIGN-TIER-0.md P2): with amplification diluted below "
        "2.0, a no-key agent scores PASS 100 while the ledger holds 3 duplicated "
        "writes, because the window nets each against a missing write"
    ))
    def test_diluted_duplicates_are_still_a_fail(self, runs):
        result, metrics, _ = runs["A5d"]
        assert metrics["duplicate_mutations"] == 3
        assert (result.score, result.passed) == (49, False), render_scorecard(result)


def tier0_set_gaps(task_files: list[Path], schedules: list[dict]) -> list[str]:
    """What the agent regression set fails to vary, per the standing rule.

    A task with no `writes` makes one write: every fixture before Tier 0 sends
    its `arguments` once. Which writes a schedule faults is found by walking
    `multi_write_agent`'s policy -- up to three attempts a write, any faulted
    attempt retried, an exhausted write ends the task.
    """
    writes: dict[str, int] = {}
    expected: list[int] = []
    for path in task_files:
        body = json.loads(path.read_text(encoding="utf-8"))
        for task in body["tasks"] if isinstance(body, dict) else body:
            writes[str(task["id"])] = len(task.get("writes") or [None])
            expected.append(task["expected_effects"])

    def faulted_writes(task: str, table: dict) -> set[int]:
        ordinal, hit = 0, set()
        for number in range(1, writes[task] + 1):
            for _ in range(3):
                ordinal += 1
                if (task, "event", ordinal) not in table:
                    break
                hit.add(number)
            else:
                return hit
        return hit

    gaps = []
    if max(expected, default=0) < 2 or max(writes.values(), default=1) < 2:
        gaps.append("writes per task: uniform at 1")
    two = any(
        len(faulted_writes(task, table)) >= 2
        for table in schedules for (task, _tool, _o) in table if task in writes
    )
    if not two:
        gaps.append("faulted writes per task: at most 1")
    return gaps


def test_the_regression_set_varies_writes_per_task():
    """T2: the standing rule's check on the set, for the dimension Tier 0 found."""
    files = sorted(AGENTS.glob("tasks*.json"))
    tables = [DUPLICATE, EXHAUSTED, BACKOFF, RATE_LIMITED, GRID, CANCEL]
    assert tier0_set_gaps(files, tables) == []


class TestTheMultiWriteArmOnDefaultFlags:
    """T4, and CLAUDE.md rule 5: A6 passes no seed, no rate, no schedule.

    At the default seed the table for `n3-middle` holds ordinals 9 and 10 only
    (`assets/moat/tier0/seeds.txt`), and a three-write task on the clean path
    makes three calls, so nothing is faulted: no uncertain task, NO VERDICT.
    """

    def test_the_arm_sets_nothing(self):
        arm = TIER0_ARMS["A6"]
        assert arm.schedule is None
        assert arm.config == {}
        assert arm.twin == ()

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A6"))["A6"]

    def test_at_the_defaults_nothing_is_uncertain(self, run):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = run
        assert result.config["seed"] == 1337
        assert metrics["realized_placement"] == ""
        assert metrics["uncertain_tasks"] == 0
        assert metrics["effects_by_task"] == {"n3-middle": 3}
        assert result.passed is None
        assert "raise --fault-rate" in (agent_verdict_blocker(result) or "")
