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
        sys.executable, str(TWIN), "--mode", "append",
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


class TestTheAgentCannotMoveTheBoundary:
    """Enforced by process structure, not by the agent's restraint.

    The agent's copy of the twin is spawned by `ratemyagent proxy`; every other
    copy is the oracle's. If the agent could advance the generation -- by
    calling the read tool itself, which `--allowedTools` permits -- its own
    retry would land in a fresh operation and the twin would apply it: a
    duplicate manufactured by the instrument.
    """

    def test_the_agent_s_calls_are_served_by_a_proxy_spawned_twin(self, operation):
        rows = _t1(operation[2])
        assert {r["role"] for r in rows} == {"agent"}

    def test_a_proxy_parented_twin_never_bumps(self, tmp_path):
        import subprocess

        state = tmp_path / "s.jsonl"
        gen = tmp_path / "s.jsonl.gen"
        gen.write_text("7")
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}}}),
            *[json.dumps({"jsonrpc": "2.0", "id": 2 + n, "method": "tools/call",
                          "params": {"name": "effects", "arguments": {}}})
              for n in range(3)],
        ]
        # argv carries a bare `proxy`, which is what the twin matches on.
        script = (
            "import subprocess, sys;"
            f"sys.exit(subprocess.run([{sys.executable!r}, {str(TWIN)!r}, '--mode',"
            f" 'append', '--state', {str(state)!r}],"
            f" input={chr(10).join(lines) + chr(10)!r}, text=True).returncode)"
        )
        subprocess.run([sys.executable, "-c", script, "proxy"], timeout=60)
        assert gen.read_text().strip() == "7", (
            "an agent-side twin advanced the operation boundary; the fixture "
            "would be manufacturing duplicates"
        )
