"""What an idempotency key is absorbed under, and why the default moved (1.7.0).

**The bug this pins was in the fixture, not the tool, and no test could see it.**
Until 1.7.0 the event twin held one global set of applied keys: a key was
absorbed forever once any call had used it. That models a key as belonging to a
*task*. A key belongs to one **operation** -- `careful_agent` has said so in its
own docstring since Phase C -- and running the same task again is a second
operation, whose real work a global scope silently swallows.

Every fixture agent here either mints a key per process (`careful_agent`) or
sends none (`blind_agent`, `no_timeout_agent`), so none of them could produce
the collision, and the whole suite passed either way. It took a real model:
`claude-haiku-4-5` derived its key from the task's own id and payload, sent the
same key in every run, and the scan's clean pass spent it -- four of five runs
then applied nothing and the report still said 100/100 PASS
(`assets/moat/GATE-D.md`).

`stable_key_agent` is that behaviour reduced to a fixture, so the suite can now
see what the suite could not see.

Three things are pinned here:

1. `operation` (the default) lets a later run apply what an earlier run's key
   already used -- the fix;
2. a retry *within* one run is still absorbed -- the behaviour the twin exists
   to show, which the fix must not cost;
3. `global` still behaves the old way, so the flag is live rather than a dead
   branch, and the old behaviour is written down rather than remembered.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent import Policy, scan
from ratemyagent.models import FaultKind
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.behavior import BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.targets import AgentTarget
from ratemyagent.targets.agent import (
    ROLE_AGENT,
    ROLE_ORACLE,
    substitute_role,
)
from ratemyagent.targets.fault_proxy import FaultConfig

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"

#: One lost reply on t1's first call, and the session closed after it, so the
#: agent must decide whether to re-send a write whose outcome it cannot know.
CLOSED = {("t1", "event", 1): FaultKind.RESPONSE_LOST_THEN_CLOSED}


def _upstream(tmp_path: Path, scope: str) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--key-scope", scope,
        "--state", str(tmp_path / "state.jsonl"),
        "--calls", str(tmp_path / "calls.jsonl"),
    ])


def _ledger(tmp_path: Path) -> list[dict]:
    path = tmp_path / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _run(tmp_path: Path, scope: str):
    target = AgentTarget(
        agent_command=shlex.join([sys.executable, str(AGENTS / "stable_key_agent.py")]),
        tasks_path=TASKS,
        upstream=_upstream(tmp_path, scope),
        work_dir=tmp_path / "work",
        allow_mutating=True,
        verify_tool="effects",
        verify_count="entries",
        # Short, so a hang costs the suite seconds rather than minutes.
        timeout_s=25.0,
    )
    injector = FaultInjector(schedule=CLOSED)
    # The session closes a beat after the reply is dropped, which is what turns
    # "the agent is still waiting" into "the agent decided".
    injector._faults = FaultConfig.uniform(
        0.0, (FaultKind.RESPONSE_LOST_THEN_CLOSED,), close_after_s=2.0
    )
    result = await scan(
        target,
        probes=[AgentBaseline(), injector, BehaviorAnalyzer()],
        policy=Policy.default(),
    )
    return result, result.probe("behavior").metrics, _ledger(tmp_path)


@pytest.fixture(scope="module")
def operation(tmp_path_factory):
    return asyncio.run(_run(tmp_path_factory.mktemp("op"), "operation"))


@pytest.fixture(scope="module")
def global_scope(tmp_path_factory):
    return asyncio.run(_run(tmp_path_factory.mktemp("gl"), "global"))


def _t1(ledger):
    return [r for r in ledger if r.get("args", {}).get("id") == "alpha"]


class TestTheAgentSendsOneKeyForEveryRun:
    def test_the_fixture_is_the_shape_the_gate_run_met(self, operation):
        """Otherwise the arms below prove nothing: the collision needs one key."""
        rows = _t1(operation[2])
        keys = {r["idempotency_key"] for r in rows}
        assert keys == {"alpha-first"}, keys
        assert len(rows) >= 3, "expected a clean pass and a disrupted run"


class TestOperationScope:
    def test_the_clean_pass_does_not_spend_the_later_run_s_key(self, operation):
        """The fix. The chaos run's first call applies, as it could not before."""
        rows = _t1(operation[2])
        assert rows[0]["effect"] == "applied"      # clean pass
        assert rows[1]["effect"] == "applied"      # chaos run, first call
        assert operation[1]["effects_by_task"]["t1"] == 1

    def test_the_retry_inside_one_run_is_still_absorbed(self, operation):
        """What the fix must not cost. Same key, same operation, no second effect."""
        rows = _t1(operation[2])
        assert rows[2]["effect"] == "absorbed"
        assert rows[1]["generation"] == rows[2]["generation"]
        assert operation[1]["duplicate_mutations"] == 0

    def test_the_generation_advanced_between_the_two_runs(self, operation):
        rows = _t1(operation[2])
        assert rows[0]["generation"] < rows[1]["generation"]

    def test_nothing_was_lost_and_the_scan_has_a_verdict(self, operation):
        result, metrics, _ = operation
        assert metrics["lost_effects"] == 0
        assert metrics["runs_applied_nothing"] == 0
        assert result.passed is not None


class TestGlobalScopeStillBehavesTheOldWay:
    """The flag is live, and the behaviour it keeps is written down.

    This is the arm that would have been green before 1.7.0 and is the defect
    stated as an executable fact: the clean pass spends the key, the chaos run
    applies nothing, and the tool -- since 1.6.2 -- declines a verdict rather
    than printing PASS over it.
    """

    def test_the_clean_pass_spends_the_key(self, global_scope):
        rows = _t1(global_scope[2])
        assert rows[0]["effect"] == "applied"
        assert [r["effect"] for r in rows[1:]] == ["absorbed"] * len(rows[1:])

    def test_so_the_disrupted_run_applies_nothing(self, global_scope):
        metrics = global_scope[1]
        assert metrics["effects_by_task"]["t1"] == 0
        assert metrics["lost_effects"] >= 1

    def test_and_1_6_2_declines_to_pass_it(self, global_scope):
        result, metrics, _ = global_scope
        assert metrics["runs_applied_nothing"] >= 1
        assert result.passed is None

    def test_the_two_scopes_disagree_about_the_same_agent(self, operation, global_scope):
        """One flag, one fixture, two outcomes. If they ever agree, one of the
        two arms has stopped testing anything."""
        assert operation[1]["effects_by_task"]["t1"] == 1
        assert global_scope[1]["effects_by_task"]["t1"] == 0


class TestTheRoleIsDeclaredNotInferred:
    """Three arms on the operation boundary, and the flag that decides it.

    **What this replaces.** Until 1.7.1 the twin worked its role out by reading
    its own parent process with `ps` and matching a `proxy` token. Any failure
    to read -- `ps` missing, slow, or output the match did not expect --
    returned the empty string, which matched nothing, which resolved to
    `oracle`: the role that advances the boundary. The detection failed *open*,
    into the role that mutates shared state, and it did exactly that on CI's
    3.12 jobs while passing everywhere else
    (`assets/moat/INVESTIGATION-1.7.0.md`).

    So the role is now said out loud by whoever launches the server, and a twin
    that is not told refuses to start. `{role}` in `--upstream` is how an agent
    scan says it, because one authored string is launched twice, once per role.
    """

    def test_the_agent_s_calls_are_served_by_a_twin_told_it_is_the_agent(self, operation):
        rows = _t1(operation[2])
        assert {r["role"] for r in rows} == {"agent"}

    @staticmethod
    def _speak(state, role, reads=3):
        """Drive one twin process directly, and return its exit code."""
        import subprocess

        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}}}),
            *[json.dumps({"jsonrpc": "2.0", "id": 2 + n, "method": "tools/call",
                          "params": {"name": "effects", "arguments": {}}})
              for n in range(reads)],
        ]
        argv = [sys.executable, str(TWIN), "--mode", "append", "--state", str(state)]
        if role is not None:
            argv += ["--role", role]
        return subprocess.run(
            argv, input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=60,
        )

    def test_no_role_exits_non_zero_and_writes_nothing(self, tmp_path):
        """The arm the old guard could not have: a twin that was not told.

        Both files are asserted, not just the counter. A twin that created its
        state file and then refused would have left the store it was pointed at
        in a state nobody asked for, and the counter alone would not show it.
        """
        state = tmp_path / "s.jsonl"
        gen = tmp_path / "s.jsonl.gen"

        done = self._speak(state, role=None)

        assert done.returncode != 0, done.stdout
        assert "--role" in done.stderr, done.stderr
        assert not state.exists(), "a twin that refused still created the state file"
        assert not gen.exists(), "a twin that refused still created the counter"

    def test_an_unrecognised_role_is_refused_and_routed_nowhere(self, tmp_path):
        state = tmp_path / "s.jsonl"
        done = self._speak(state, role="scanner")
        assert done.returncode != 0
        assert "--role" in done.stderr and "invalid choice" in done.stderr
        assert not state.exists()
        assert not (tmp_path / "s.jsonl.gen").exists()

    def test_the_agent_s_copy_never_bumps(self, tmp_path):
        """The old boundary test, adapted: the role is passed, not inferred."""
        state = tmp_path / "s.jsonl"
        gen = tmp_path / "s.jsonl.gen"
        gen.write_text("7")

        done = self._speak(state, role="agent", reads=3)

        assert done.returncode == 0, done.stderr
        assert gen.read_text().strip() == "7", (
            "a twin told it is the agent advanced the operation boundary; the "
            "fixture would be manufacturing duplicates"
        )

    def test_the_oracle_s_copy_bumps_exactly_once_per_process(self, tmp_path):
        """Three reads, one step. The `BUMPED` guard, stated directly.

        The oracle reads twice per task window -- before and after -- and both
        reads land in the same process only when a window is re-read. One bump
        per process is what keeps a window's two ends from counting as two
        operations.
        """
        state = tmp_path / "s.jsonl"
        gen = tmp_path / "s.jsonl.gen"
        gen.write_text("7")

        done = self._speak(state, role="oracle", reads=3)

        assert done.returncode == 0, done.stderr
        assert gen.read_text().strip() == "8", (
            "the oracle's copy should advance the boundary exactly once per "
            "process, however many times it reads"
        )


class TestTheUpstreamIsSubstitutedPerConsumer:
    """`{role}` resolves once per consumer, and only `{role}`.

    Unit arms on `substitute_role`, plus the property that matters most to
    anyone who was scanning before this existed: **a command with no `{role}`
    comes out byte-identical.** That arm deliberately uses a non-twin upstream,
    because the twin is the one server that would notice a change.
    """

    def test_a_command_without_the_placeholder_is_untouched(self):
        """The compatibility promise, on a server that is not the twin."""
        upstream = (
            "stdio://npx -y @modelcontextprotocol/server-memory "
            "--store /tmp/x.jsonl --flag {not_role} --brace }{"
        )
        for role in (ROLE_AGENT, ROLE_ORACLE):
            assert substitute_role(upstream, role) == upstream

    def test_each_consumer_gets_its_own_reading(self):
        upstream = "stdio://python server.py --role {role} --state s.jsonl"
        assert substitute_role(upstream, ROLE_AGENT) == (
            "stdio://python server.py --role agent --state s.jsonl"
        )
        assert substitute_role(upstream, ROLE_ORACLE) == (
            "stdio://python server.py --role oracle --state s.jsonl"
        )

    def test_every_occurrence_resolves(self):
        """No reason to have two, and no reason for the second to survive."""
        assert substitute_role("a {role} b {role}", ROLE_AGENT) == "a agent b agent"

    def test_the_authored_string_is_what_the_target_reports(self, tmp_path):
        """Substituted at consumption, not at storage.

        The report, the export and the AGENTS.md block show what the user
        wrote. Neither substituted form is "the upstream" -- there are two --
        so printing one of them would be a half-truth.
        """
        upstream = _upstream(tmp_path, "operation")
        target = AgentTarget(
            agent_command=shlex.join([sys.executable, str(AGENTS / "careful_agent.py")]),
            tasks_path=TASKS,
            upstream=upstream,
            work_dir=tmp_path / "work",
            allow_mutating=True,
            verify_tool="effects",
            verify_count="entries",
        )
        assert "{role}" in target.upstream
        assert target.upstream == upstream
