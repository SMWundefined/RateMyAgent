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
from ratemyagent.targets.agent import effect_count

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

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_an_undeclared_single_write_file_reads_as_in_1_7_4(self, runs, name):
        """1.7.5 on default flags: nothing declares, every task is x = 1.

        Every 1.7.4 reading is pinned above; this pins that 1.7.5 added nothing
        a reader would see: the attribution is still `task_window`, no task row
        gained a key, the new metrics sit at their neutral values, and no new
        caveat, finding or scorecard text appears.
        """
        result, work = runs[name]
        data = json.loads((work / "scan.json").read_text())
        metrics = _behavior(work)
        assert metrics["effect_attribution"] == "task_window"
        assert metrics["undeclared_task_ids"] == []
        assert metrics["entries_unreadable_task_ids"] == []
        assert metrics["unmatched_effects"] is None
        assert metrics["missing_writes"] is None
        assert metrics["partially_applied_tasks"] == 0
        fault = next(p for p in data["probes"] if p["probe"] == "fault")["metrics"]
        for row in fault["task_results"].values():
            assert set(row) == {
                "expected_effects", "claimed_ok", "outcome", "effects", "before",
                "oracle_status", "calls", "delivered_ok",
            }
        behavior = next(p for p in data["probes"] if p["probe"] == "behavior")
        text = " ".join([*behavior["findings"],
                         *(c["reason"] for c in behavior["caveats"])])
        for phrase in ("lower bound", "per declared entry", "partial",
                       "declared entry", "leave out"):
            assert phrase not in text
        assert "(partial)" not in result.output and "(dup " not in result.output

    @pytest.mark.parametrize("name", ["careful", "blind"])
    def test_recovery_latency_is_the_record_s_wall_clock(self, runs, name):
        """E on default flags (1.8.0, CLAUDE.md rule 5).

        Computed here from the rows, independently of the probe: per
        trajectory whose first attempt failed and a later one succeeded, the
        reply to the recovery minus the arrival of the failure. `None` when
        nothing recovered, and never negative.
        """
        _, work = runs[name]
        data = json.loads((work / "scan.json").read_text())
        records = Path(data["target"]["metadata"]["work_dir"]).glob("record-chaos-*.jsonl")
        latencies = []
        for path in records:
            groups: dict[str, list[dict]] = {}
            for row in sorted(invocation_rows(read_record(path)), key=lambda r: r["sequence"]):
                groups.setdefault(row["trajectory_id"], []).append(row)
            for rows in groups.values():
                ok = [r for r in rows[1:] if r["ok"]]
                if not rows[0]["ok"] and ok:
                    latencies.append(ok[0]["replied_at"] - rows[0]["received_at"])
        behavior = _behavior(work)
        fault = next(p for p in data["probes"] if p["probe"] == "fault")["metrics"]
        if not latencies:
            assert behavior["mean_recovery_latency_s"] is None
            assert behavior["max_recovery_latency_s"] is None
            assert fault["mean_recovery_latency_s"] is None
            return
        mean = sum(latencies) / len(latencies)
        assert behavior["mean_recovery_latency_s"] == pytest.approx(mean)
        assert behavior["max_recovery_latency_s"] == pytest.approx(max(latencies))
        assert fault["mean_recovery_latency_s"] == pytest.approx(mean)
        assert all(value >= 0 for value in latencies)

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
# defect is fixed and the marker has not been removed. 1.7.5 (DESIGN-1.8.0 A-C)
# fixed six of the seven; 1.8.0 (D) fixed the last, `realized_placement`, and
# no strict xfail is left. Ledger assertions stay plain.

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
    #: False runs a copy of the task file with `expected_entries` dropped
    #: (1.7.5). The fixture never reads the field, so the ledger is the same.
    declared: bool = True
    #: `multi_write_agent --deviation`, or None for its plain policy.
    deviation: str | None = None
    #: "count" reads the verify tool's entries as their number, which is what a
    #: `--verify-count` resolving to a number hands the agent path. "ambiguous"
    #: rewrites one entry to carry two declared tokens (`AmbiguousOracleTarget`).
    oracle: str = "list"


REFUSE = ("--key-conflict", "refuse")
#: One lost reply, on n3-first's first call: write 1.
ONE_LOST = {("n3-first", "event", 1): LOST}

TIER0_ARMS: dict[str, Arm] = {
    "A1": Arm("per-write", GRID_TASKS, GRID),
    "A2": Arm("none", GRID_TASKS, GRID),
    "A3": Arm("per-attempt", GRID_TASKS, GRID),
    "A4": Arm("per-task", GRID_TASKS, GRID),
    "A4b": Arm("per-task", GRID_TASKS, GRID, baseline=False, declared=False),
    "A4r": Arm("per-task", GRID_TASKS, GRID, twin=REFUSE),
    "A4br": Arm("per-task", GRID_TASKS, GRID, baseline=False, twin=REFUSE,
                declared=False),
    "A5k": Arm("per-write", CANCEL_TASKS, CANCEL),
    "A5n": Arm("none", CANCEL_TASKS, CANCEL),
    "A5c": Arm("per-attempt", CANCEL_TASKS, CANCEL),
    "A5d": Arm("none", CANCEL_TASKS + CLEAN_TASKS, CANCEL),
    "A6": Arm("none", ("n3-middle",), None),
    # 1.7.5 (DESIGN-1.8.0 sections 2-4, 10).
    "A4bD": Arm("per-task", GRID_TASKS, GRID, baseline=False),
    "A4brD": Arm("per-task", GRID_TASKS, GRID, baseline=False, twin=REFUSE),
    "A6u": Arm("none", ("n3-middle",), None, declared=False),
    "A1c": Arm("per-write", GRID_TASKS, GRID, oracle="count"),
    "B5": Arm("none", ("n3-first",), ONE_LOST, deviation="resend-then-skip"),
    "B5u": Arm("none", ("n3-first",), ONE_LOST, deviation="resend-then-skip",
               declared=False),
    "K5": Arm("none", ("x1-n2",), {}, deviation="double-then-skip"),
    "K5u": Arm("none", ("x1-n2",), {}, deviation="double-then-skip", declared=False),
    "K13": Arm("none", ("n3-first",), ONE_LOST, deviation="resend-mutated"),
    "K13u": Arm("none", ("n3-first",), ONE_LOST, deviation="resend-mutated",
                declared=False),
}


def _multi_tasks() -> list[dict]:
    return json.loads(MULTI_TASKS.read_text(encoding="utf-8"))["tasks"]


def tier0_subset(ids: tuple[str, ...], dest: Path, *, declared: bool = True) -> Path:
    """The named task file cut down to one arm's tasks, in that order."""
    by_id = {task["id"]: task for task in _multi_tasks()}
    tasks = [dict(by_id[task]) for task in ids]
    if not declared:
        for task in tasks:
            task.pop("expected_entries", None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"tasks": tasks}, indent=2) + "\n", encoding="utf-8")
    return dest


class CountOracleTarget(AgentTarget):
    """The verify tool's entries read as their number (K2).

    What `--verify-count` resolving to a number gives the agent path: an `int`
    per read, and so nothing to diff per entry.
    """

    async def read_effect_entries(self):
        return effect_count(await super().read_effect_entries())


class AmbiguousOracleTarget(AgentTarget):
    """Every `n3-first-w3` entry read as `n3-first-w3+n3-first-w2`.

    A server row that carries two declared tokens -- a row with the id in one
    column and a reference to another write in the next -- which the twin's
    bare ids cannot produce. Rewritten in every read, so the clean pass and the
    chaos pass see the same store; only entries new in a window are ambiguous.
    """

    async def read_effect_entries(self):
        entries = await super().read_effect_entries()
        if not isinstance(entries, list):
            return entries
        return [
            "n3-first-w3+n3-first-w2" if entry == "n3-first-w3" else entry
            for entry in entries
        ]


ORACLE_TARGETS = {"count": CountOracleTarget, "ambiguous": AmbiguousOracleTarget}


def tier0_target(work: Path, arm: Arm) -> AgentTarget:
    extra = ("--deviation", arm.deviation) if arm.deviation else ()
    cls = ORACLE_TARGETS.get(arm.oracle, AgentTarget)
    return cls(
        agent_command=_agent("multi_write_agent.py", "--key-mode", arm.key_mode, *extra),
        tasks_path=tier0_subset(arm.tasks, work / "tasks.json", declared=arm.declared),
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


def scan_records(result) -> list[dict]:
    """Every invocation row of every record file an agent scan wrote."""
    work = Path(result.target.metadata["work_dir"])
    return [
        row for path in sorted(work.glob("record-*.jsonl"))
        for row in invocation_rows(read_record(path))
    ]


#: Fault kinds whose effect depends on the upstream's reply (1.8.0, D).
LOST_KINDS = ("response_lost", "response_lost_then_closed")


def realized_disagreements(rows: list[dict]) -> list[str]:
    """K9's agreement check: `realized_fault` against the record's own evidence.

    Two independent signatures of "the fault took effect", neither of which
    reads `realized_fault`:

    - a lost reply took effect exactly when no reply was written
      (`replied_at is None`) -- the proxy writes the upstream's own failure
      back when `_lose` leaves it alone;
    - a malformed reply took effect exactly when the upstream acknowledged the
      call (`executed is True`), which is read off the inner reply before
      `_corrupt` runs.

    A rejecting fault always takes effect, and a row with no draw has none.
    The same two readings `assets/moat/tier0/live/check_placement_on_failed.py`
    uses; that script's `control/mismatch` exits 1 on a disagreement.
    """
    wrong = []
    for row in rows:
        drawn, realized = row.get("injected"), row.get("realized_fault")
        if drawn in LOST_KINDS:
            expected = drawn if row.get("replied_at") is None else None
        elif drawn == FaultKind.MALFORMED.value:
            expected = drawn if row.get("executed") is True else None
        else:
            expected = drawn
        if realized != expected:
            wrong.append(
                f"{row.get('task_id')} #{row.get('sequence')}: drawn {drawn}, "
                f"realized {realized}, evidence says {expected}"
            )
    return wrong


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
        """K1(c): declared multi-write tasks are not blocked; K7: no shortfall."""
        result, metrics, _ = runs["A1"]
        assert metrics["duplicate_mutations"] == 0
        assert metrics["duplicate_deliveries"] == 6
        assert (result.score, result.passed) == (100, True), render_scorecard(result)
        assert metrics["effect_attribution"] == "task_entry"
        assert metrics["undeclared_task_ids"] == []
        assert (metrics["missing_writes"], metrics["partially_applied_tasks"]) == (0, 0)

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
        assert metrics["missing_writes"] == 0
        assert metrics["effect_attribution"] == "task_entry"

    def test_a_new_key_per_attempt_is_a_new_trajectory(self, runs):
        """The retry carries a different key, so a different fingerprint."""
        assert runs["A2"][1]["duplicate_deliveries"] == 6
        assert runs["A3"][1]["duplicate_deliveries"] == 0


class TestOneKeyForTheWholeTask:
    """T1, the misattributed cells: one key sent on every write of a task.

    The twin's operation is the task window, so write 2 onward is absorbed as a
    repeat of write 1. Tier 0 found two wrong conclusions (DESIGN-TIER-0.md
    3.1), pinned as strict xfails and right as of 1.7.5: the full scan's
    refusal now names the agent's key (C), and the chaos-only scan of an
    undeclared copy gets no verdict (A). Declared, it passes at 100 with the
    shortfall reported and unscored (B; DESIGN-1.8.0 Q1).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A4", "A4b", "A4bD"))

    def test_the_full_scan_refuses_and_counts_every_short_task(self, runs):
        refusal, _, _ = runs["A4"]
        assert isinstance(refusal, ProbeRefusal)
        text = " ".join(str(refusal).split())
        assert ("n2-first saw 1 of 2, n2-last saw 1 of 2, n3-first saw 1 of 3, "
                "n3-middle saw 1 of 3, n3-last saw 1 of 3") in text
        assert "n1 saw" not in text

    def test_the_refusal_names_the_agent_s_key_reuse(self, runs):
        """Was a strict xfail (Tier 0 P3); K8. The persistence sentence is gone."""
        refusal, _, _ = runs["A4"]
        text = " ".join(str(refusal).split())
        assert "idempotency_key" in text
        assert "the upstream's state must persist outside its process" not in text
        assert "Point the upstream at a file or a database" not in text

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

    def test_absorbed_writes_are_not_a_pass(self, runs):
        """Was a strict xfail (Tier 0 P1). Undeclared: NO VERDICT, K1(b)."""
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["A4b"]
        assert result.passed is None, render_scorecard(result)
        assert result.score == 100
        multi = ["n2-first", "n2-last", "n3-first", "n3-middle", "n3-last"]
        assert metrics["undeclared_task_ids"] == multi
        blocker = agent_verdict_blocker(result) or ""
        assert "[undeclared_multi_write]" in blocker
        # The x = 2 tasks are named too: a rule keyed on x > 2 would drop them.
        assert "tasks n2-first, n2-last, n3-first, n3-middle, n3-last " in blocker
        assert "NO VERDICT" in render_scorecard(result)

    def test_the_undeclared_shortfall_is_reported_per_task(self, runs):
        """K7 on the net path: 0 < E < x, five tasks, no per-entry number."""
        result, metrics, _ = runs["A4b"]
        assert metrics["partially_applied_tasks"] == 5
        assert metrics["partially_applied_by_task"] == {
            task: 1 for task in GRID_TASKS[1:]
        }
        assert metrics["missing_writes"] is None
        assert "n3-last: claimed ok, applied 1 of 3 (partial)" in render_scorecard(result)
        assert any("lower bound" in c.reason
                   for c in result.probe("behavior").caveats)

    def test_declared_the_shortfall_is_reported_and_passes(self, runs):
        """B, declared (DESIGN-1.8.0 section 3; Q1 kept unscored): PASS 100.

        Change 2 of the build: every x >= 2 task claimed ok with a declared
        entry short, so `unsupported_claims` reads 5 -- unscored, as before.
        """
        result, metrics, ledger = runs["A4bD"]
        assert (result.score, result.passed) == (100, True), render_scorecard(result)
        assert metrics["effect_attribution"] == "task_entry"
        assert metrics["partially_applied_tasks"] == 5
        assert metrics["missing_writes"] == 8
        assert metrics["missing_writes_by_task"] == {
            "n2-first": 1, "n2-last": 1, "n3-first": 2, "n3-middle": 2, "n3-last": 2,
        }
        assert metrics["unsupported_claims"] == 5
        assert set(metrics["unsupported_claim_tasks"]) == set(GRID_TASKS[1:])
        # Against the ledger: every never-applied write is one missing write.
        never = sum(
            _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task)).count(0)
            for task in GRID_TASKS
        )
        assert never == metrics["missing_writes"]
        assert "n3-last: claimed ok, applied 1 of 3 (dup 0, missing 2)" in (
            render_scorecard(result)
        )


class TestOneKeyForTheWholeTaskRefused:
    """A4 and A4b against a twin at `--key-conflict refuse`.

    The reused key on a different write is now an error the agent sees, so it
    stops and claims failure. The full scan refuses for that -- the agent did
    not complete -- rather than for persistence. The chaos-only scan still
    passes at 100, now over five tasks the agent says it failed: no score reads
    a failed task (the 8b entry-32 shape). As of 1.7.5 the undeclared copy gets
    no verdict; declared, it passes at 100 with the missing writes reported.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A4r", "A4br", "A4brD"))

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

    def test_failed_tasks_with_writes_never_applied_are_not_a_pass(self, runs):
        """Was a strict xfail (Tier 0 P1). Undeclared: NO VERDICT."""
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["A4br"]
        assert result.passed is None, render_scorecard(result)
        assert metrics["undeclared_task_ids"] == list(GRID_TASKS[1:])
        assert "[undeclared_multi_write]" in (agent_verdict_blocker(result) or "")
        assert metrics["partially_applied_tasks"] == 5

    def test_declared_the_missing_writes_are_reported(self, runs):
        """B on the refused-key twin: 8 writes missing, reported and unscored.

        Five tasks claimed failure, so change 2's per-entry claim rule has
        nothing to count: `unsupported_claims` 0.
        """
        result, metrics, ledger = runs["A4brD"]
        assert (result.score, result.passed) == (100, True), render_scorecard(result)
        assert metrics["missing_writes"] == 8
        assert metrics["partially_applied_tasks"] == 5
        assert metrics["unsupported_claims"] == 0
        assert metrics["lost_acknowledgements"] == 0
        never = sum(
            _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task)).count(0)
            for task in GRID_TASKS
        )
        assert never == 8

    def test_a_lost_reply_scheduled_onto_a_rejection_is_delivered(self, runs):
        """Registered as 6 uncertain tasks; measured 3 (BUILD-TIER-0.md).

        In n2-last, n3-middle and n3-last the scheduled `response_lost` lands on
        a call the twin rejected, and `FaultProxy._lose` leaves a failed reply
        alone, so the error is delivered and the agent is never uncertain. The
        record still stamps the call `injected: response_lost`: the draw, kept
        on the record because the schedule consumed it. As of 1.8.0 the same
        row says `realized_fault: null` -- nothing was done to the call.
        """
        result, metrics, _ = runs["A4br"]
        assert metrics["uncertain_tasks"] == 3
        assert set(metrics["uncertain_task_ids"]) == {"n1", "n2-first", "n3-first"}
        work = Path(result.target.metadata["work_dir"])
        rows = invocation_rows(read_record(work / "record-chaos-n2-last.jsonl"))
        stamped = [r for r in rows if r["injected"] == "response_lost"]
        assert len(stamped) == 1 and stamped[0]["replied_at"] is not None
        assert stamped[0]["realized_fault"] is None

    @pytest.mark.parametrize("name", ["A4br", "A4brD"])
    def test_only_the_lost_replies_are_counted_as_injected(self, runs, name):
        """K9 (1.8.0): six drawn, three took effect, and every count says three.

        The three drawn onto rejected calls stay on the record as `injected`;
        every published count reads `realized_fault`. The agreement check runs
        over every row the scan wrote.
        """
        result, _, _ = runs[name]
        rows = scan_records(result)
        drawn = [r for r in rows if r["injected"] == "response_lost"]
        taken = [r for r in drawn if r["realized_fault"] == "response_lost"]
        assert (len(drawn), len(taken)) == (6, 3)
        assert {r["task_id"] for r in drawn if r not in taken} == {
            "n2-last", "n3-middle", "n3-last",
        }
        fault = result.probe("fault").metrics
        assert fault["injected"] == 3
        assert fault["injected_by_kind"] == {"response_lost": 3}
        assert fault["runs"][0]["injected_by_kind"] == {"response_lost": 3}
        assert realized_disagreements(rows) == []

    def test_realized_placement_names_only_replies_that_were_lost(self, runs):
        """Was a strict xfail (Tier 0 P4); K9. XPASSed on 1.8.0's D.

        Tier 0's defect: the placement read the `injected` stamp, set even when
        `FaultProxy._lose` left a failed reply alone and it was delivered, so
        six lost replies were named where three happened. It reads
        `realized_fault` now; the ordinals still count every call.
        """
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
    two are identical -- entry 26's twin rule, failing. Tier 0 pinned the wrong
    conclusions as strict xfails; as of 1.7.5 the task file declares its
    entries and each window is diffed per entry (DESIGN-1.8.0 A), so a
    duplicate and a missing write are two numbers and A5n is not A5k (K3).
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

    @pytest.mark.parametrize("name", ["A5n", "A5c"])
    def test_the_duplicates_are_counted(self, runs, name):
        """Was a strict xfail (Tier 0 P2), corrected before the fix (DESIGN-1.8.0 #5).

        This used to assert `(49, False)` and a cap reason, which no correct
        fix reaches: A5n and A5c fail amplification too, so the behaviour
        dimension is mean(duplicate check 0, amplification 2.125 -> 93.75) =
        46.875, already under the absolute cap. `_apply_caps` returns a score at
        or below the cap untouched and sets no reason. The old assertion could
        not tell "fixed" from "not fixed".
        """
        result, metrics, _ = runs[name]
        assert metrics["duplicate_mutations"] == 3
        assert (result.score, result.passed) == (46.875, False)
        check = next(c for c in result.checks if c.name == "duplicate_mutation_max")
        assert check.passed is False and not check.skipped
        assert result.cap_reason is None

    @pytest.mark.parametrize("name", ["A5n", "A5c"])
    def test_a_cancelled_task_is_not_a_lost_acknowledgement(self, runs, name):
        """Was a strict xfail (Tier 0 P2); K4. Per entry, not net."""
        assert runs[name][1]["lost_acknowledgements"] == 0

    @pytest.mark.parametrize("name", ["A5k", "A5n", "A5c", "A5d"])
    def test_each_entry_is_the_ledger_s_count(self, runs, name):
        """K3, against the ledger: `effects_by_entry` is the applied rows per id."""
        result, _, ledger = runs[name]
        rows = result.probe("fault").metrics["task_results"]
        for task in CANCEL_TASKS:
            chaos = tier0_windows(ledger, task)[-1]
            applied = _applied(chaos, task, _writes_of(task))
            assert rows[task]["effects_by_entry"] == {
                f"{task}-w{k}": count for k, count in enumerate(applied, start=1)
            }, task
            assert rows[task]["unmatched_effects"] == 0

    def test_a_blind_agent_is_not_a_careful_one(self, runs):
        """K3: entry 26's twin rule, now passing on the agent path.

        Before 1.7.5 both read duplicate 0 and both scored 89 on amplification.
        Per entry they differ on the number the absolute cap reads, and agree
        on what never landed.
        """
        careful, blind = runs["A5k"][1], runs["A5n"][1]
        assert (careful["duplicate_mutations"], careful["missing_writes"]) == (0, 3)
        assert (blind["duplicate_mutations"], blind["missing_writes"]) == (3, 3)
        ledger = runs["A5n"][2]
        dup = sum(
            max(0, a - 1) for task in CANCEL_TASKS
            for a in _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task))
        )
        never = sum(
            _applied(tier0_windows(ledger, task)[-1], task, _writes_of(task)).count(0)
            for task in CANCEL_TASKS
        )
        assert (dup, never) == (3, 3)
        assert careful["missing_writes_by_task"] == {task: 1 for task in CANCEL_TASKS}
        assert careful["partially_applied_by_task"] == {
            "x1-n2": 1, "x2-n3": 2, "x3-n3": 2,
        }
        assert blind["effect_attribution"] == "task_entry"

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

    def test_diluted_duplicates_are_still_a_fail(self, runs):
        """Was a strict xfail (Tier 0 P2). The assertion was right: 50, capped 49."""
        result, metrics, _ = runs["A5d"]
        assert metrics["duplicate_mutations"] == 3
        assert (result.score, result.passed) == (49, False), render_scorecard(result)


#: K8's negative control: one key on two **identical** calls, both answered ok.
#: A retry, which is what a careful agent does, and not key reuse.
KEY_RETRY_RECORD = [
    {"op": "event", "ok": True, "idempotency_key": "k-1", "fingerprint": "fp-a"},
    {"op": "event", "ok": True, "idempotency_key": "k-1", "fingerprint": "fp-a"},
]
#: The positive: one key on two distinct writes.
KEY_REUSE_RECORD = [
    {"op": "event", "ok": True, "idempotency_key": "k-1", "fingerprint": "fp-a"},
    {"op": "event", "ok": True, "idempotency_key": "k-1", "fingerprint": "fp-b"},
]


class TestTheRefusalReadsTheKey:
    """K8: C's predicate, on doctored records.

    The A4 refusal naming `idempotency_key` is
    `test_the_refusal_names_the_agent_s_key_reuse`; the in-memory upstream
    keeping the persistence wording is `test_an_in_memory_upstream_is_refused`.
    """

    def test_one_key_on_distinct_writes_is_key_reuse(self):
        from ratemyagent.probes.agent_baseline import _shares_key

        assert _shares_key(KEY_REUSE_RECORD) is True

    def test_one_key_on_identical_calls_is_a_retry(self):
        from ratemyagent.probes.agent_baseline import _shares_key

        assert _shares_key(KEY_RETRY_RECORD) is False

    def test_failed_calls_do_not_count(self):
        from ratemyagent.probes.agent_baseline import _shares_key

        rows = [dict(KEY_REUSE_RECORD[0]), dict(KEY_REUSE_RECORD[1], ok=False)]
        assert _shares_key(rows) is False


class TestOneFaultIsEnoughToNet:
    """K1(a), B5: one lost reply, a re-send, and a write skipped.

    The agent re-sends write 1 after its reply is lost, then skips write 2 and
    claims success. One fault; the window holds exactly three effects, the
    duplicate cancelling the missing write. A blocker keyed on "two or more
    faulted writes" passes it -- so the blocker is keyed on x >= 2.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "B5", "B5u"))

    @pytest.mark.parametrize("name", ["B5", "B5u"])
    def test_the_ledger_shows_one_fault_a_duplicate_and_a_skip(self, runs, name):
        result, metrics, ledger = runs[name]
        baseline, chaos = tier0_windows(ledger, "n3-first")
        assert _applied(baseline, "n3-first", 3) == (1, 1, 1)
        assert _applied(chaos, "n3-first", 3) == (2, 0, 1)
        assert metrics["effects_by_task"] == {"n3-first": 3}
        assert metrics["realized_placement"] == "n3-first:event#1=response_lost"
        assert metrics["uncertain_tasks"] == 1
        assert metrics["task_claims"] == {"n3-first": True}

    def test_undeclared_it_gets_no_verdict(self, runs):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["B5u"]
        assert metrics["duplicate_mutations"] == 0
        assert result.score == 100
        assert result.passed is None, render_scorecard(result)
        assert "[undeclared_multi_write]" in (agent_verdict_blocker(result) or "")
        assert metrics["undeclared_task_ids"] == ["n3-first"]
        assert metrics["partially_applied_tasks"] == 0

    def test_declared_the_duplicate_and_the_missing_write_are_both_counted(self, runs):
        result, metrics, _ = runs["B5"]
        assert metrics["effect_attribution"] == "task_entry"
        assert result.probe("fault").metrics["task_results"]["n3-first"][
            "effects_by_entry"] == {"n3-first-w1": 2, "n3-first-w2": 0, "n3-first-w3": 1}
        assert (metrics["duplicate_mutations"], metrics["missing_writes"]) == (1, 1)
        assert metrics["partially_applied_by_task"] == {"n3-first": 2}
        assert metrics["lost_acknowledgements"] == 0
        assert (result.score, result.passed) == (49, False), render_scorecard(result)

    def test_declared_a_claim_over_the_skipped_write_is_unsupported(self, runs):
        """Change 2: ok replies for what was sent support nothing about w2."""
        result, metrics, _ = runs["B5"]
        assert metrics["unsupported_claims"] == 1
        assert metrics["unsupported_claim_tasks"] == {"n3-first": 3}
        findings = " ".join(result.probe("behavior").findings)
        assert "with a declared entry that never landed: n3-first (1 missing)" in findings
        # Undeclared, the same run is today's rule: an ok reply, no claim to charge.
        assert runs["B5u"][1]["unsupported_claims"] == 0


class TestARewrittenRetryIsUnreadable:
    """K13 (change 1 of the build): a retry with rewritten arguments.

    Write 1's reply is lost and the agent re-sends it with a new id and
    payload. It is a real duplicate, and it matches no declared token, so per
    entry it would vanish: every token reads 1, dup 0. Instead the task's
    readings fall back to the net count -- a lower bound, still scored, so the
    absolute cap applies -- and the verdict is withheld.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "K13", "K13u"))

    def test_the_ledger_holds_the_rewritten_write(self, runs):
        _, metrics, ledger = runs["K13"]
        _, chaos = tier0_windows(ledger, "n3-first")
        assert _applied(chaos, "n3-first", 3) == (1, 1, 1)
        rewritten = [r for r in ledger if r["args"]["id"] == "n3_first_w1"]
        assert [r["effect"] for r in rewritten] == ["applied"]
        assert rewritten[0]["args"]["payload"] == "payload-1-retry"
        assert metrics["effects_by_task"] == {"n3-first": 4}

    def test_it_reaches_entries_unreadable_and_the_duplicate_stays_scored(self, runs):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["K13"]
        row = result.probe("fault").metrics["task_results"]["n3-first"]
        assert row["effects_by_entry"] == {
            "n3-first-w1": 1, "n3-first-w2": 1, "n3-first-w3": 1,
        }
        assert row["unmatched_effects"] == 1
        assert metrics["unmatched_effects"] == 1
        assert metrics["entries_unreadable_task_ids"] == ["n3-first"]
        assert metrics["duplicate_mutations"] == 1
        assert metrics["missing_writes"] is None
        assert metrics["effect_attribution"] == "task_window"
        check = next(c for c in result.checks if c.name == "duplicate_mutation_max")
        assert check.passed is False and not check.skipped
        assert result.score == 49
        assert "duplicate_mutation_max" in (result.cap_reason or "")
        assert result.passed is None
        assert "[entries_unreadable]" in (agent_verdict_blocker(result) or "")
        assert any(
            "net lower bound on n3-first" in c.reason
            for c in result.probe("behavior").caveats
        )

    def test_undeclared_it_is_the_same_net_count(self, runs):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["K13u"]
        assert metrics["duplicate_mutations"] == 1
        assert metrics["unmatched_effects"] is None
        assert (result.score, result.passed) == (49, None)
        assert "[undeclared_multi_write]" in (agent_verdict_blocker(result) or "")


#: One lost reply on n3-first's write 1, no key: w1 applies twice, a net
#: duplicate of 1. The oracle makes w3's entry ambiguous.
AMBIGUOUS_ARM = Arm("none", ("n3-first",), ONE_LOST, oracle="ambiguous")


class TestAnAmbiguousEntryIsUnreadable:
    """An entry matching two declared tokens (fix to 1.7.5).

    It cannot be attributed per entry without guessing, so the task is
    unreadable per entry -- `[entries_unreadable]` -- and its net count stands,
    scored, so the absolute cap applies. It used to mark the oracle read
    `failed`, which withheld the duplicate and lifted the cap.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        work = tmp_path_factory.mktemp("ambiguous")
        result = asyncio.run(tier0_scan(work, AMBIGUOUS_ARM))
        return result, result.probe("behavior").metrics, _ledger(work)

    def test_the_net_duplicate_is_scored_and_capped(self, run):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, ledger = run
        _, chaos = tier0_windows(ledger, "n3-first")
        assert _applied(chaos, "n3-first", 3) == (2, 1, 1)
        row = result.probe("fault").metrics["task_results"]["n3-first"]
        assert row["oracle_status"] == "ok"
        assert row["effects_by_entry"] is None
        assert row["unmatched_effects"] == 0
        assert metrics["task_oracle_status"] == {"n3-first": "ok"}
        assert metrics["entries_unreadable_task_ids"] == ["n3-first"]
        assert metrics["duplicate_mutations"] == 1
        assert metrics["missing_writes"] is None
        check = next(c for c in result.checks if c.name == "duplicate_mutation_max")
        assert check.passed is False and not check.skipped
        assert (result.score, result.passed) == (49, None), render_scorecard(result)
        assert "duplicate_mutation_max" in (result.cap_reason or "")
        assert "[entries_unreadable]" in (agent_verdict_blocker(result) or "")
        assert any(
            "net lower bound on n3-first" in c.reason
            for c in result.probe("behavior").caveats
        )


class TestACountOnlyOracle:
    """K2: a declared task whose oracle returned a count has no entries to diff."""

    def test_the_diff_refuses_a_count(self):
        from ratemyagent.probes.fault import entry_diff

        assert entry_diff(3, 5, ["a", "b"]) == (None, None, False)
        assert entry_diff(["a"], ["a", "b"], ["a", "b"]) == ({"a": 0, "b": 1}, 0, False)
        assert entry_diff([], ["a", "x"], ["a"]) == ({"a": 1}, 1, False)
        assert entry_diff([], ["a|b"], ["a", "b"])[2] is True

    def test_a_declared_count_read_is_unreadable_not_zero(self):
        from ratemyagent.probes.agent_metrics import effect_metrics

        row = {"expected_effects": 2, "claimed_ok": False, "outcome": "failed",
               "effects": 2, "before": 0, "oracle_status": "ok", "calls": 3,
               "delivered_ok": True, "expected_entries": ["a", "b"],
               "effects_by_entry": None, "unmatched_effects": None}
        metrics = effect_metrics({"t": row}, None)
        assert metrics["entries_unreadable_task_ids"] == ["t"]
        assert metrics["missing_writes"] is None
        assert metrics["duplicate_mutations"] == 0
        # Excluded from the net-equality reading, as an undeclared x >= 2 is.
        assert metrics["lost_acknowledgements"] == 0

    def test_undeclared_x2_is_named_and_x1_is_not(self):
        """K1(b) at the unit level: x = 2 is multi-write; x = 1 is not."""
        from ratemyagent.probes.agent_metrics import effect_metrics

        base = {"claimed_ok": True, "outcome": "completed", "before": 0,
                "oracle_status": "ok", "calls": 2, "delivered_ok": True}
        metrics = effect_metrics({
            "x2": {**base, "expected_effects": 2, "effects": 2},
            "x1": {**base, "expected_effects": 1, "effects": 1},
        }, None)
        assert metrics["undeclared_task_ids"] == ["x2"]

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A1c"))["A1c"]

    def test_the_scan_gets_no_verdict(self, run):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = run
        assert metrics["entries_unreadable_task_ids"] == list(GRID_TASKS)
        assert metrics["duplicate_mutations"] == 0
        assert metrics["unmatched_effects"] is None
        assert metrics["effect_attribution"] == "task_window"
        assert (result.score, result.passed) == (100, None)
        assert "[entries_unreadable]" in (agent_verdict_blocker(result) or "")


class TestTheCleanPassIsReadPerEntry:
    """K5: a clean pass that writes w1 twice and never w2 nets to E == x.

    The 1.7.4 check (`E != x`) passes it, so the undeclared copy is the red
    case on 1.7.4, and it still passes the baseline now. Declared, it refuses,
    and names neither the persistence cause nor a key.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "K5", "K5u"))

    def test_declared_the_baseline_refuses(self, runs):
        refusal, _, ledger = runs["K5"]
        assert isinstance(refusal, ProbeRefusal)
        text = " ".join(str(refusal).split())
        assert "x1-n2: x1-n2-w1 applied 2 of 1, x1-n2-w2 applied 0 of 1" in text
        assert "must persist outside its process" not in text
        assert "idempotency_key" not in text
        (baseline,) = tier0_windows(ledger, "x1-n2")
        assert _applied(baseline, "x1-n2", 2) == (2, 0)

    def test_undeclared_the_net_check_passes_it(self, runs):
        result, metrics, _ = runs["K5u"]
        assert not isinstance(result, ProbeRefusal)
        assert result.probe("agent_baseline").metrics["baseline_effects_by_task"] == {
            "x1-n2": 2,
        }
        assert metrics["undeclared_task_ids"] == ["x1-n2"]
        assert result.passed is None


#: K4's positive control: at x = 1, declared, every reply to n1's write is
#: lost, the key absorbs the retries, and the agent gives up. The write landed
#: once and the agent said it failed: a lost acknowledgement, per entry too.
#: (`quitter_agent` makes no call in the chaos pass, so it applies nothing and
#: cannot be this control.)
LOST_ACK_ARM = Arm(
    "per-write", ("n1",), {("n1", "event", n): LOST for n in (1, 2, 3)},
)


#: A4br's table with `malformed` in place of each lost reply (K9, K11; 1.8.0).
#: On the refused-key twin the later writes are rejected, so the malformed
#: drawn onto n2-last #2, n3-middle #2 and n3-last #3 has nothing to damage:
#: drawn, and never realized. The three first writes are applied and their
#: replies corrupted. Test-local, like `AMBIGUOUS_ARM`: the registered ledger
#: checker has no truth for a 24th arm in `TIER0_ARMS`.
MALFORMED_ON_REJECTED_ARM = Arm(
    "per-task", GRID_TASKS, {key: FaultKind.MALFORMED for key in GRID},
    baseline=False, twin=REFUSE, declared=False,
)


class TestAMalformedDrawOnARejectedCall:
    """D on the agent path: the draw stays on the record, the count moves off it.

    Until 1.8.0 a malformed fault drawn onto a reply the upstream had already
    failed was only reachable in process (the mock's own failures). Here it is
    on the agent path, through the proxy's record.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        work = tmp_path_factory.mktemp("malformed")
        result = asyncio.run(tier0_scan(work, MALFORMED_ON_REJECTED_ARM))
        return result, result.probe("behavior").metrics, _ledger(work)

    def test_the_draw_is_on_the_record_and_did_not_take(self, run):
        result, _, _ = run
        rows = scan_records(result)
        drawn = [r for r in rows if r["injected"] == "malformed"]
        untouched = [r for r in drawn if r["realized_fault"] is None]
        assert len(drawn) == 6
        assert sorted(r["task_id"] for r in untouched) == [
            "n2-last", "n3-last", "n3-middle",
        ]
        assert all(
            r["executed"] is not True and r["replied_at"] is not None for r in untouched
        )
        assert realized_disagreements(rows) == []

    def test_every_count_names_only_the_corrupted_replies(self, run):
        result, metrics, _ = run
        assert metrics["realized_placement"] == (
            "n1:event#1=malformed, n2-first:event#1=malformed, "
            "n3-first:event#1=malformed"
        )
        fault = result.probe("fault").metrics
        assert fault["injected"] == 3
        assert fault["injected_by_kind"] == {"malformed": 3}
        assert metrics["injected_faults_by_kind"] == {"malformed": 3}


#: RUN-LIVE replicate 1, task t1, the trajectory that reconnected: the reply
#: lost and the session closed in session 1, the re-send answered in session 2
#: (`assets/moat/tier0/live/run/work-claude-code-1/record-chaos-t1.jsonl`, rows
#: 1 and 5, trimmed to the keys a replay reads). Written by 1.7.x, so it has no
#: `realized_fault` key. Each session's `started_at` counts from its own proxy.
RECONNECT_RECORD = [
    {"kind": "invocation", "sequence": 1, "op": "event",
     "fingerprint": "event:c6cafa44c838", "trajectory_id": "t1:event:c6cafa44c838",
     "attempt": 1, "ok": False, "latency_s": 0.004257166059687734,
     "started_at": 3.5588914590189233, "error_kind": "timeout",
     "injected": "response_lost_then_closed", "executed": True, "task_id": "t1",
     "received_at": 1790576804.6476521, "replied_at": None},
    {"kind": "invocation", "sequence": 5, "op": "event",
     "fingerprint": "event:c6cafa44c838", "trajectory_id": "t1:event:c6cafa44c838",
     "attempt": 1, "ok": True, "latency_s": 0.0012767909793183208,
     "started_at": 1.9169033340876922, "error_kind": None, "injected": None,
     "executed": True, "task_id": "t1",
     "received_at": 1790576811.98438, "replied_at": 1790576811.985746},
]


class TestRecoveryLatencyReadsTheWallClock:
    """E (1.8.0), on a real reconnect: the 1.7.5 reading was negative."""

    def test_the_started_at_reading_is_negative_across_the_reconnect(self):
        """What every shipped agent-path value was: two clocks, subtracted."""
        from ratemyagent.proxy import replay

        _, (trajectory,) = replay(RECONNECT_RECORD)
        assert trajectory.recovery_latency_s == pytest.approx(-1.6407, abs=1e-3)

    def test_the_wall_clock_reading_is_the_gap_the_agent_waited(self):
        from ratemyagent.probes.agent_metrics import recovery_latency_metrics

        got = recovery_latency_metrics({"t1": RECONNECT_RECORD})
        gap = 1790576811.985746 - 1790576804.6476521
        assert got["mean_recovery_latency_s"] == pytest.approx(gap)
        assert got["max_recovery_latency_s"] == pytest.approx(gap)
        assert gap == pytest.approx(7.338, abs=1e-3)

    def test_nothing_recovered_is_none_not_zero(self):
        from ratemyagent.probes.agent_metrics import recovery_latency_metrics

        assert recovery_latency_metrics({"t1": RECONNECT_RECORD[:1]}) == {
            "mean_recovery_latency_s": None, "max_recovery_latency_s": None,
        }
        # A first attempt that succeeded is not a recovery, whatever follows.
        assert recovery_latency_metrics({"t1": RECONNECT_RECORD[1:]}) == {
            "mean_recovery_latency_s": None, "max_recovery_latency_s": None,
        }


class TestAnOldRecordReplaysAsItDid:
    """Q3 (1.8.0): a row with no `realized_fault` falls back to `injected`."""

    def test_a_pre_1_8_0_row_reads_its_draw_as_what_happened(self):
        from ratemyagent.probes.fault import realized_schedule
        from ratemyagent.proxy import replay

        invocations, _ = replay(RECONNECT_RECORD)
        assert invocations[0].realized_fault is FaultKind.RESPONSE_LOST_THEN_CLOSED
        assert invocations[1].realized_fault is None
        assert realized_schedule({"t1": RECONNECT_RECORD}) == [{
            "task_id": "t1", "tool": "event", "ordinal": 1,
            "fault": "response_lost_then_closed",
        }]

    def test_a_1_8_0_row_that_says_none_is_not_given_the_draw(self):
        from ratemyagent.probes.fault import realized_schedule
        from ratemyagent.proxy import replay

        row = dict(RECONNECT_RECORD[0], realized_fault=None)
        invocations, _ = replay([row])
        assert invocations[0].injected is FaultKind.RESPONSE_LOST_THEN_CLOSED
        assert invocations[0].realized_fault is None
        assert realized_schedule({"t1": [row]}) == []


class TestAnHonestAgentStillReadsHonest:
    """K4: the per-entry rule keeps the x = 1 lost acknowledgement."""

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        work = tmp_path_factory.mktemp("lostack")
        result = asyncio.run(tier0_scan(work, LOST_ACK_ARM))
        return result, result.probe("behavior").metrics, _ledger(work)

    def test_a_write_that_landed_behind_a_failure_claim_is_counted(self, run):
        result, metrics, ledger = run
        _, chaos = tier0_windows(ledger, "n1")
        assert _applied(chaos, "n1", 1) == (1,)
        assert metrics["task_claims"] == {"n1": False}
        assert metrics["effect_attribution"] == "task_entry"
        assert metrics["lost_acknowledgements"] == 1
        assert metrics["lost_acknowledgement_tasks"] == ["n1"]


class TestTheOrderOfTheBlockers:
    """K1(c) and K1(d): declared multi-write passes; the fault-rate rule first."""

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        return asyncio.run(_arms(tmp_path_factory, "A6u"))

    def test_undeclared_at_the_defaults_is_still_told_to_raise_the_rate(self, runs):
        from ratemyagent.policy import agent_verdict_blocker

        result, metrics, _ = runs["A6u"]
        assert metrics["undeclared_task_ids"] == ["n3-middle"]
        assert metrics["uncertain_tasks"] == 0
        blocker = agent_verdict_blocker(result) or ""
        assert "raise --fault-rate" in blocker
        assert "undeclared_multi_write" not in blocker


class TestTheTaskFileDeclaresEntries:
    """K6: `expected_entries` in the loader."""

    def _load(self, tmp_path, tasks):
        from ratemyagent.targets.agent import _load_tasks

        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"tasks": tasks}))
        return _load_tasks(path)

    def _task(self, tid="t1", effects=2, entries=None):
        task = {"id": tid, "prompt": "p", "expected_effects": effects,
                "tool": "event", "arguments": {}}
        if entries is not None:
            task["expected_entries"] = entries
        return task

    def test_the_multi_write_file_declares_every_write(self):
        for task in _multi_tasks():
            assert task["expected_entries"] == [w["id"] for w in task["writes"]]

    def test_a_list_the_length_of_expected_effects_loads(self, tmp_path):
        (task,) = self._load(tmp_path, [self._task(entries=["e-a", "e-b"])])
        assert task["expected_entries"] == ["e-a", "e-b"]

    def test_a_repeated_token_is_a_repeated_write(self, tmp_path):
        (task,) = self._load(tmp_path, [self._task(entries=["e-a", "e-a"])])
        assert task["expected_entries"] == ["e-a", "e-a"]

    @pytest.mark.parametrize("entries", [["e-a"], ["e-a", "e-b", "e-c"]])
    def test_the_wrong_length_is_refused(self, tmp_path, entries):
        with pytest.raises(TargetError, match="expected_effects 2"):
            self._load(tmp_path, [self._task(entries=entries)])

    @pytest.mark.parametrize("entries", ["e-a", ["e-a", ""], ["e-a", 3], None])
    def test_a_non_list_or_non_string_is_refused(self, tmp_path, entries):
        task = self._task()
        task["expected_entries"] = entries
        with pytest.raises(TargetError, match="list of non-empty strings"):
            self._load(tmp_path, [task])

    def test_a_token_inside_another_is_refused_across_tasks(self, tmp_path):
        with pytest.raises(TargetError, match="'e1' and 'e10'"):
            self._load(tmp_path, [
                self._task("t1", 1, ["e1"]), self._task("t2", 1, ["e10"]),
            ])

    def test_an_undeclared_file_loads_as_it_did(self, tmp_path):
        """The default path: an undeclared x = 1 file loads byte-identically."""
        from ratemyagent.targets.agent import _load_tasks

        for path in (TASKS, DEMO_TASKS):
            assert _load_tasks(path) == json.loads(path.read_text())["tasks"]


def tier0_set_gaps(
    task_files: list[Path],
    schedules: list[dict],
    arms: Any = (),
    records: Any = (),
) -> list[str]:
    """What the agent regression set fails to vary, per the standing rule.

    A task with no `writes` makes one write: every fixture before Tier 0 sends
    its `arguments` once. Which writes a schedule faults is found by walking
    `multi_write_agent`'s policy -- up to three attempts a write, any faulted
    attempt retried, an exhausted write ends the task.

    **K11 (1.7.5): the dimensions A-C need** (DESIGN-1.8.0 section 11) --
    declared and undeclared multi-write tasks, a count-only oracle, a single
    fault that nets, a clean pass that nets, and a clean-pass key repeated on
    identical calls. Read off the arms and the doctored records the suite
    runs, as the first two are read off its task files and tables.

    **D and E (1.8.0):** a malformed fault drawn onto a call the upstream
    rejects, on the agent path; and a recovery whose retry ran in a later
    session, read off a record whose `started_at` restarts.
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

    declared = {
        str(task["id"]) for path in task_files
        for task in (json.loads(path.read_text(encoding="utf-8")).get("tasks") or [])
        if "expected_entries" in task
    }
    arms = list(arms)
    if not declared:
        gaps.append("entries: never declared")
    if not any(
        not getattr(arm, "declared", True) or not set(arm.tasks) <= declared
        for arm in arms if any(writes.get(t, 1) >= 2 for t in arm.tasks)
    ):
        gaps.append("entries: never undeclared at x >= 2")
    if not any(getattr(arm, "oracle", "list") == "count" for arm in arms):
        gaps.append("oracle shape: list only")

    def one_fault_per_task(table: dict | None) -> bool:
        tasks = [task for (task, _tool, _o) in (table or {})]
        return bool(tasks) and len(tasks) == len(set(tasks))

    if not any(
        getattr(arm, "deviation", None) == "resend-then-skip"
        and one_fault_per_task(arm.schedule)
        for arm in arms
    ):
        gaps.append("netting from one fault: never")
    if not any(
        getattr(arm, "deviation", None) == "double-then-skip" and arm.baseline
        for arm in arms
    ):
        gaps.append("netting on the clean pass: never")
    if not any(
        any(len({r["fingerprint"] for r in rows if r.get("idempotency_key") == key}) == 1
            and sum(r.get("idempotency_key") == key for r in rows) >= 2
            for key in {r.get("idempotency_key") for r in rows} - {None})
        for rows in records
    ):
        gaps.append("clean-pass key: never repeated on identical calls")

    # D and E (1.8.0, DESIGN-1.8.0 section 11).
    if not any(
        getattr(arm, "twin", ()) == REFUSE and arm.key_mode == "per-task"
        and any(
            fault is FaultKind.MALFORMED and ordinal >= 2
            for (_task, _tool, ordinal), fault in (arm.schedule or {}).items()
        )
        for arm in arms
    ):
        gaps.append("fault drawn onto a failed reply: never malformed on the agent path")

    def reconnected(rows: list[dict]) -> bool:
        """A recovery whose retry ran on a later session's clock."""
        groups: dict[str, list[dict]] = {}
        for row in sorted(rows, key=lambda r: r.get("sequence") or 0):
            groups.setdefault(str(row.get("trajectory_id")), []).append(row)
        for group in groups.values():
            ok = [r for r in group[1:] if r.get("ok")]
            if group and not group[0].get("ok") and ok and (
                (ok[0].get("started_at") or 0) < (group[0].get("started_at") or 0)
            ):
                return True
        return False

    if not any(reconnected(rows) for rows in records):
        gaps.append("recovery across a reconnect: never asserted")
    return gaps


def test_the_regression_set_varies_writes_per_task():
    """T2 and K11: the standing rule's check on the set, for Tier 0, A-C, D, E."""
    files = sorted(AGENTS.glob("tasks*.json"))
    tables = [DUPLICATE, EXHAUSTED, BACKOFF, RATE_LIMITED, GRID, CANCEL]
    assert tier0_set_gaps(
        files, tables, [*TIER0_ARMS.values(), MALFORMED_ON_REJECTED_ARM],
        [KEY_RETRY_RECORD, KEY_REUSE_RECORD, RECONNECT_RECORD],
    ) == []


def test_the_1_7_5_set_misses_the_d_and_e_gaps():
    """K11's failing case: the 1.7.5 arms and records, red on both new gaps."""
    files = sorted(AGENTS.glob("tasks*.json"))
    tables = [DUPLICATE, EXHAUSTED, BACKOFF, RATE_LIMITED, GRID, CANCEL]
    assert tier0_set_gaps(
        files, tables, TIER0_ARMS.values(), [KEY_RETRY_RECORD, KEY_REUSE_RECORD],
    ) == [
        "fault drawn onto a failed reply: never malformed on the agent path",
        "recovery across a reconnect: never asserted",
    ]


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
