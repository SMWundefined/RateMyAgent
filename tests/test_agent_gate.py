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
from pathlib import Path

import pytest
from click.testing import CliRunner

from ratemyagent import Policy, scan
from ratemyagent.cli import cli
from ratemyagent.models import FaultKind
from ratemyagent.outputs import render_scorecard
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.agent_baseline import AgentBaseline
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
