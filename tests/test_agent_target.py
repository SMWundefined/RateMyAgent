"""AgentTarget: the target whose answer is a claim.

Every other adapter answers a call. This one runs a *task* and reports what the
agent said happened, which is not the same kind of fact -- and the tests below
are mostly about keeping the two apart.

The end-to-end arms launch a real agent, which launches a real proxy, which
talks to the real twin fixture. Nothing is mocked in that chain on purpose:
Phase C's whole claim is that the MCP boundary is the only thing between the
scanner and the agent, and a mocked boundary would be the one thing that cannot
test it.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli
from ratemyagent.models import FaultKind
from ratemyagent.probes import ProbeConfig, ScanContext
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.base import ProbeRefusal
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.proxy import invocation_rows, read_record, replay
from ratemyagent.targets import AgentTarget, TargetError, build_target
from ratemyagent.targets.base import Target
from ratemyagent.targets.fault_proxy import FaultConfig

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"


def _agent(name: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name)])


def _upstream(state: Path) -> str:
    return "stdio://" + shlex.join(
        [sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
         "--state", str(state)]
    )


def _target(tmp_path: Path, agent: str = "careful_agent.py", **kwargs) -> AgentTarget:
    return AgentTarget(
        agent_command=_agent(agent),
        tasks_path=TASKS,
        upstream=_upstream(tmp_path / "state.jsonl"),
        work_dir=tmp_path / "work",
        timeout_s=kwargs.pop("timeout_s", 30.0),
        allow_mutating=kwargs.pop("allow_mutating", True),
        **kwargs,
    )


class _EnvlessAgentTarget(AgentTarget):
    """A1 undone: the config names the proxy but carries no `env` block.

    This is the shape the amendment is about. Exporting `RMA_PROXY_RECORD` in
    the scan's own environment reaches the proxy through nothing, because the
    MCP SDK copies six named variables into a stdio child and drops the rest --
    so a build that forgot the block would still run, still inject, still write
    to the upstream, and still produce an empty record. Every downstream number
    would then be about a task nobody watched.
    """

    def _write_config(self, task_id: str):
        path = self.work_dir / f"mcp-{task_id}.json"
        command, *args = self.proxy_command
        path.write_text(json.dumps({"mcpServers": {"ratemyagent": {
            "command": command,
            "args": [*args, "--upstream", self.upstream],
        }}}, indent=2) + "\n", encoding="utf-8")
        return path


class TestTheInterface:
    """Design tests 1 and 2."""

    async def test_it_satisfies_the_abc_and_declares_what_it_is(self, tmp_path):
        target = _target(tmp_path)
        assert isinstance(target, Target)
        assert target.runs_own_retry_loop is True
        assert target.injects_out_of_process is True
        assert target.reports_token_usage is False
        # Declared, never inferred: False until --verify-tool is given.
        assert target.has_effect_oracle is False
        assert _target(tmp_path, verify_tool="effects").has_effect_oracle is True

    def test_the_abstract_method_set_is_unchanged(self):
        """The guard against this design quietly breaking the frozen interface.

        Adding an abstract member to `Target` breaks every third-party subclass
        and needs a major version. `AgentTarget` adds none -- what it adds is
        class attributes with defaults, which is additive.
        """
        assert Target.__abstractmethods__ == frozenset(
            {"setup", "invoke", "teardown", "describe"}
        )

    def test_the_new_class_attribute_defaults_false_on_every_other_target(self):
        from ratemyagent.targets import LLMTarget, MCPTarget, MockTarget
        from ratemyagent.targets.fault_proxy import FaultProxy

        for kind in (MCPTarget, LLMTarget, MockTarget, FaultProxy):
            assert kind.injects_out_of_process is False

    def test_the_target_kind_is_registered(self, tmp_path):
        target = build_target(
            "agent",
            agent_command=_agent("careful_agent.py"),
            tasks_path=str(TASKS),
            upstream=_upstream(tmp_path / "state.jsonl"),
        )
        assert isinstance(target, AgentTarget)

    @pytest.mark.parametrize("missing", ["agent_command", "tasks_path", "upstream"])
    def test_the_factory_names_what_is_missing(self, tmp_path, missing):
        kwargs = {
            "agent_command": _agent("careful_agent.py"),
            "tasks_path": str(TASKS),
            "upstream": _upstream(tmp_path / "state.jsonl"),
        }
        kwargs[missing] = None
        with pytest.raises(TargetError) as exc:
            build_target("agent", **kwargs)
        assert {"agent_command": "--agent", "tasks_path": "--tasks",
                "upstream": "--upstream"}[missing] in str(exc.value)


class TestDescribe:
    """Design test 4."""

    async def test_it_carries_the_command_the_upstream_and_the_task_file(self, tmp_path):
        target = _target(tmp_path)
        await target.setup()
        info = target.describe()
        assert info.kind == "agent"
        assert "careful_agent.py" in info.metadata["agent_command"]
        assert str(TASKS) == info.metadata["tasks_path"]
        assert info.metadata["task_ids"] == ["t1", "t2"]
        assert info.metadata["expected_effects"] == {"t1": 1, "t2": 1}
        assert info.to_dict()["metadata"]["tasks"] == 2

    async def test_a_credential_in_the_upstream_uri_is_redacted(self, tmp_path):
        """Same rule as `MCPTarget`: this reaches the report, the JSON and the
        AGENTS.md state block."""
        target = AgentTarget(
            agent_command=_agent("careful_agent.py"),
            tasks_path=TASKS,
            upstream="https://scanner:s3cret@host/mcp",
            work_dir=tmp_path / "work",
        )
        await target.setup()
        blob = json.dumps(target.describe().to_dict())
        assert "s3cret" not in blob
        assert "<redacted>" in blob


class TestTheTaskFile:
    """Refused rather than filled in: every denominator starts here."""

    def _write(self, tmp_path: Path, tasks: object) -> Path:
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"tasks": tasks}))
        return path

    async def test_a_missing_expected_effects_is_refused_not_defaulted(self, tmp_path):
        """A default of 1 is also a legal value, so it erases the distinction
        between a task that declared it and one that forgot."""
        path = self._write(tmp_path, [
            {"id": "t1", "prompt": "p", "tool": "event", "arguments": {}}
        ])
        target = AgentTarget(agent_command="x", tasks_path=path, upstream="stdio://x.py",
                             work_dir=tmp_path / "w")
        with pytest.raises(TargetError, match="expected_effects"):
            await target.setup()

    async def test_a_non_integer_expected_effects_is_refused(self, tmp_path):
        path = self._write(tmp_path, [
            {"id": "t1", "prompt": "p", "expected_effects": "one",
             "tool": "event", "arguments": {}}
        ])
        target = AgentTarget(agent_command="x", tasks_path=path, upstream="stdio://x.py",
                             work_dir=tmp_path / "w")
        with pytest.raises(TargetError, match="must be an integer"):
            await target.setup()

    async def test_a_duplicate_id_is_refused(self, tmp_path):
        path = self._write(tmp_path, [
            {"id": "t1", "prompt": "p", "expected_effects": 1, "tool": "event",
             "arguments": {}},
            {"id": "t1", "prompt": "q", "expected_effects": 1, "tool": "event",
             "arguments": {}},
        ])
        target = AgentTarget(agent_command="x", tasks_path=path, upstream="stdio://x.py",
                             work_dir=tmp_path / "w")
        with pytest.raises(TargetError, match="appears twice"):
            await target.setup()

    async def test_requests_does_not_multiply_tasks(self, tmp_path):
        """The task is the unit. `expected_effects` is declared per task and the
        oracle brackets each one, so running t1 twenty times would make every
        per-task number mean something else."""
        target = _target(tmp_path)
        await target.setup()
        assert [r.op for r in target.probe_requests(20)] == ["t1", "t2"]


class TestTheConfigIsTheChannel:
    """Amendment A1.

    The MCP SDK copies six named variables into a stdio child and drops the
    rest, so a record path exported by the scan reaches the proxy through
    nothing. The `env` block in the per-task config is the only channel there
    is, which is why the test is that the record exists and knows its task.
    """

    async def test_the_config_carries_an_explicit_env_block(self, tmp_path):
        target = _target(tmp_path)
        await target.setup()
        path = target._write_config("t1")
        entry = json.loads(path.read_text())["mcpServers"]["ratemyagent"]
        assert entry["env"]["RMA_PROXY_RECORD"] == str(target.record_path("t1"))
        assert entry["env"]["RMA_PROXY_SCHEDULE"] == str(target.schedule_path)
        assert entry["env"]["RMA_TASK_ID"] == "t1"
        assert "--upstream" in entry["args"]

    async def test_the_record_is_non_empty_and_carries_the_task_id(self, tmp_path):
        """The A1 test, end to end: agent -> proxy -> upstream, and back."""
        target = _target(tmp_path)
        await target.setup()
        try:
            response = await target.invoke(target.sample_request(0))
            assert response.ok, response.error

            rows = invocation_rows(read_record(target.record_path("t1")))
            assert rows, "the proxy recorded nothing, so the env block never arrived"
            assert {row["task_id"] for row in rows} == {"t1"}
            assert {row["op"] for row in rows} == {"event"}
        finally:
            await target.teardown()

    async def test_the_agent_can_find_its_config_from_the_environment_alone(self, tmp_path):
        """A1 gives two channels; this is the one argv does not cover.

        A real agent reads one or the other, and which one is not ours to pick.
        Tested by running the fixture with no `--mcp-config` on its argv, so a
        build that quietly stopped setting `RMA_MCP_CONFIG` would be caught here
        rather than by whoever wires up the first agent that only reads env.
        """
        import os
        import subprocess

        target = _target(tmp_path)
        await target.setup()
        try:
            config = target._write_config("t1")
            proc = subprocess.run(
                [sys.executable, str(AGENTS / "careful_agent.py"),
                 "--tasks", str(TASKS), "--task", "t1"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "RMA_MCP_CONFIG": str(config)},
            )
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True
            assert invocation_rows(read_record(target.record_path("t1")))
        finally:
            await target.teardown()

    async def test_the_record_replays_into_trajectories(self, tmp_path):
        """Design test 3, end to end rather than at the format's level."""
        target = _target(tmp_path)
        await target.setup()
        try:
            await target.invoke(target.sample_request(0))
            invocations, trajectories = replay(read_record(target.record_path("t1")))
            assert len(invocations) == 1
            assert len(trajectories) == 1
            assert trajectories[0].final_status == "success"
        finally:
            await target.teardown()


class TestAgentBaseline:
    async def test_it_reports_the_clean_path_call_count_per_task(self, tmp_path):
        target = _target(tmp_path)
        await target.setup()
        context = ScanContext()
        try:
            result = await AgentBaseline().execute(target, ProbeConfig(), context)
        finally:
            await target.teardown()

        assert result.metrics["tasks_completed"] == 2
        assert result.metrics["clean_calls_per_task"] == {"t1": 1, "t2": 1}
        assert result.metrics["clean_calls_by_task"]["t1"] == {"event": 1}
        # Phase 2 sizes the forced schedule from this.
        assert context.artifacts["agent_clean_calls"]["t1"] == {"event": 1}

    async def test_it_is_not_applicable_to_a_service_target(self):
        from ratemyagent.targets import MockTarget

        result = await AgentBaseline().execute(MockTarget.healthy(), ProbeConfig())
        assert result.applicable is False

    async def test_it_refuses_when_a_task_fails_with_no_faults(self, tmp_path):
        """A broken fixture is not a finding about the agent under fault.

        `_refuse_unusable_baseline` one level up, and the same argument: a scan
        measuring its own misconfiguration should refuse rather than publish.
        """
        tasks = tmp_path / "tasks.json"
        tasks.write_text(json.dumps({"tasks": [{
            # The twin rejects a non-string payload, so the agent cannot
            # complete this task however well it behaves.
            "id": "t1", "prompt": "p", "expected_effects": 1,
            "tool": "event", "arguments": {"id": "a", "payload": 7},
        }]}))
        target = AgentTarget(
            agent_command=_agent("careful_agent.py"), tasks_path=tasks,
            upstream=_upstream(tmp_path / "state.jsonl"),
            work_dir=tmp_path / "work", allow_mutating=True,
        )
        await target.setup()
        try:
            with pytest.raises(ProbeRefusal, match="did not complete with no faults"):
                await AgentBaseline().execute(target, ProbeConfig())
        finally:
            await target.teardown()

    async def test_an_empty_record_is_refused_as_absence(self, tmp_path):
        """Amendment A2, produced the way it actually happens: A1 undone.

        Zero calls is a legal-looking value -- it is what an agent that did
        nothing produces, and also what a proxy that never received its record
        path produces. Nothing downstream can tell them apart, so the
        distinction is made here.

        The record is emptied by dropping the config's `env` block rather than
        by patching the read, because that is the failure this guards: the
        agent still runs, the proxy still serves it, the upstream is still
        written to, and the only thing missing is the scan's evidence. Patching
        the read would test a condition that cannot arise.
        """
        target = _EnvlessAgentTarget(
            agent_command=_agent("careful_agent.py"), tasks_path=TASKS,
            upstream=_upstream(tmp_path / "state.jsonl"),
            work_dir=tmp_path / "work", allow_mutating=True,
        )
        await target.setup()
        try:
            with pytest.raises(ProbeRefusal, match="no calls recorded"):
                await AgentBaseline().execute(target, ProbeConfig())
        finally:
            await target.teardown()

    async def test_the_refusal_names_the_env_block(self, tmp_path):
        """The remedy, not just the symptom: a scan that says "no calls" and
        not where to look sends someone to read the agent instead."""
        target = _EnvlessAgentTarget(
            agent_command=_agent("careful_agent.py"), tasks_path=TASKS,
            upstream=_upstream(tmp_path / "state.jsonl"),
            work_dir=tmp_path / "work", allow_mutating=True,
        )
        await target.setup()
        try:
            with pytest.raises(ProbeRefusal) as exc:
                await AgentBaseline().execute(target, ProbeConfig())
        finally:
            await target.teardown()
        assert "env" in str(exc.value) and "RMA_PROXY_RECORD" in str(exc.value)


class TestTheOutOfProcessBranch:
    async def test_the_fault_probe_reads_the_record_rather_than_a_proxy(self, tmp_path):
        target = _target(tmp_path)
        await target.setup()
        context = ScanContext()
        try:
            await AgentBaseline().execute(target, ProbeConfig(), context)
            result = await FaultInjector(
                FaultConfig(rates={FaultKind.RESPONSE_LOST: 1.0})
            ).execute(target, ProbeConfig(), context)
        finally:
            await target.teardown()

        assert result.metrics["interposed"] is True
        assert result.metrics["scheduled_faults"] > 0
        assert result.metrics["injected"] > 0
        # The trajectories reached phase 3's channel, in the shape it reads.
        assert context.artifacts["trajectories"]
        assert context.artifacts["max_retries"] == 2

    async def test_the_schedule_is_identical_for_two_different_agents(self, tmp_path):
        """The reason the table is keyed on position rather than on a key.

        Careful and blind make different numbers of calls with different
        arguments, so a seeded draw keyed on `trajectory_key` would give them
        different faults and any comparison would measure the draw.
        """
        schedules = []
        for agent in ("careful_agent.py", "blind_agent.py"):
            work = tmp_path / agent
            target = _target(work, agent=agent)
            await target.setup()
            context = ScanContext()
            try:
                await AgentBaseline().execute(target, ProbeConfig(), context)
                probe = FaultInjector()
                schedules.append(probe._schedule_for(
                    target, ProbeConfig(seed=7), context,
                    FaultConfig.uniform(0.3, seed=7),
                ))
            finally:
                await target.teardown()
        assert schedules[0] == schedules[1]
        assert schedules[0], "an empty schedule would make this vacuous"

    async def test_an_empty_record_under_fault_is_refused(self, tmp_path):
        """A2 again, on the branch that would otherwise score the run.

        This is the expensive one. The baseline refusal only costs a clean run;
        here the scan has faults injected, an agent that reports success, and no
        evidence -- which is exactly the shape that printed 100/100 PASS over a
        duplicated mutation in 1.4.0 (PROGRESS 8b entry 28).
        """
        target = _EnvlessAgentTarget(
            agent_command=_agent("careful_agent.py"), tasks_path=TASKS,
            upstream=_upstream(tmp_path / "state.jsonl"),
            work_dir=tmp_path / "work", allow_mutating=True,
        )
        await target.setup()
        try:
            with pytest.raises(ProbeRefusal, match="no calls recorded"):
                await FaultInjector().execute(target, ProbeConfig())
        finally:
            await target.teardown()


class TestTheHang:
    """The agent that never sets a read timeout.

    The MCP Python SDK exposes a per-request read timeout on `ClientSession`;
    whether a given host sets one is unverified, so an agent that sets none is a
    shape a real scan will meet. A dropped reply leaves it waiting forever,
    which is a real production failure mode and not a harness artifact -- so the
    output is `abandoned` plus a refusal, and not a fix.

    Driven with a `RESPONSE_LOST`-only config rather than through `--fault-rate`,
    so the hang does not depend on which kind a seed happened to draw. Pinning a
    seed to get the fault you want is how a feature ships green and broken on
    default flags (PROGRESS 8b entry 27).
    """

    async def test_a_timeout_less_agent_is_abandoned_and_refuses(self, tmp_path):
        target = _target(tmp_path, agent="no_timeout_agent.py", timeout_s=3.0)
        await target.setup()
        context = ScanContext()
        try:
            with pytest.raises(ProbeRefusal) as exc:
                await FaultInjector(
                    FaultConfig(rates={FaultKind.RESPONSE_LOST: 1.0})
                ).execute(target, ProbeConfig(), context)
        finally:
            await target.teardown()

        assert "abandoned" in str(exc.value)
        assert target.outcomes["t1"] == "abandoned"
        # The record still holds what the proxy saw before the agent stalled:
        # the call landed, the reply was dropped.
        rows = invocation_rows(read_record(target.record_path("t1")))
        assert rows and rows[0]["injected"] == "response_lost"

    def test_a_refusal_exits_two_not_one(self, tmp_path, monkeypatch):
        """Exit 1 means the target failed a policy. Nothing failed a policy
        when no measurement was taken -- the 1.4.1 distinction, and the reason
        `ProbeRefusal` is a `TargetError`."""
        from ratemyagent.probes.latency import LatencyProfiler

        async def refuse(self, target, config, context=None):
            raise ProbeRefusal("nothing was measured")

        monkeypatch.setattr(LatencyProfiler, "run", refuse)
        result = CliRunner().invoke(
            cli, ["scan", "--target", "mock", "--probes", "latency", "--requests", "3"]
        )
        assert result.exit_code == 2
        assert "nothing was measured" in result.output


class TestTheCliSurface:
    def test_agent_flags_are_refused_on_other_targets(self):
        """The `--header`-on-stdio rule: ignored is worse than refused."""
        result = CliRunner().invoke(
            cli, ["scan", "--target", "mock", "--agent", "python x.py"]
        )
        assert result.exit_code != 0
        assert "--target agent only" in result.output

    @pytest.mark.parametrize("probe", ["latency", "cost", "concurrency", "contract"])
    def test_a_service_probe_is_refused_on_an_agent_target(self, probe, tmp_path):
        """Amendment A7."""
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "python x.py",
            "--tasks", str(TASKS), "--upstream", "stdio://x.py",
            "--probes", probe,
        ])
        assert result.exit_code == 2
        assert "does not apply to --target agent" in result.output

    def test_a_verify_tool_that_writes_is_refused_at_setup(self, tmp_path):
        """C2 accepts --verify-tool on an agent target, with the server scan's
        refusal: an oracle that writes changes the number it defines."""
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", _agent("careful_agent.py"),
            "--tasks", str(TASKS), "--upstream", _upstream(tmp_path / "s.jsonl"),
            "--allow-mutating", "--verify-tool", "event",
        ])
        assert result.exit_code == 2, result.output
        assert "event" in result.output
        assert not (tmp_path / "s.jsonl").exists(), "no task ran"

    def test_a_verify_tool_needs_allow_mutating(self, tmp_path):
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", _agent("careful_agent.py"),
            "--tasks", str(TASKS), "--upstream", _upstream(tmp_path / "s.jsonl"),
            "--verify-tool", "effects", "--verify-count", "entries",
        ])
        assert result.exit_code == 2, result.output
        assert "--allow-mutating" in result.output

    def test_the_default_probe_set_stays_six_for_a_service_target(self):
        """`agent_baseline` is registered and is deliberately not in "all".

        Adding it would put an unmeasurable row on every scan this tool has ever
        produced, and move the `Probes: n/6` line CI output is read off.
        """
        from ratemyagent.probes import DEFAULT_PROBES, PROBES, resolve_probes

        assert "agent_baseline" in PROBES
        assert "agent_baseline" not in DEFAULT_PROBES
        assert len(resolve_probes(None)) == 6

    def test_the_proxy_command_exists_and_needs_an_upstream(self):
        result = CliRunner().invoke(cli, ["proxy"])
        assert result.exit_code != 0
        assert "--upstream" in result.output


class TestEndToEndOnDefaultFlags:
    """The standing rule: every new feature gets one arm on default flags.

    No `--seed`, no `--warmup`, no `--concurrency`, no `--requests`. A default
    is the configuration nobody chose, which is exactly why no fixture author
    reaches for it -- 1.4.0 shipped a false `stale` on every default-seed scan
    behind a green 7-mutation matrix and a twin campaign, because every test
    pinned a seed (PROGRESS 8b entry 27).
    """

    @pytest.mark.parametrize("agent", ["careful_agent.py", "blind_agent.py"])
    def test_a_full_agent_scan_completes_on_defaults(self, tmp_path, agent):
        state = tmp_path / "state.jsonl"
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent",
            "--agent", _agent(agent),
            "--tasks", str(TASKS),
            "--upstream", _upstream(state),
            "--allow-mutating",
            "--fault-rate", "0.3",
        ])
        assert result.exit_code == 0, result.output
        assert "agent_baseline" in result.output
        # The twin applied something: the agent really did reach the upstream.
        assert state.exists() and state.read_text().strip()


@pytest.fixture(autouse=True)
def _keep_temp_dirs_out_of_the_tree(tmp_path):
    yield
    shutil.rmtree(tmp_path / "work", ignore_errors=True)


def test_no_fixture_agent_imports_ratemyagent():
    """A6, asserted rather than asserted-to.

    An agent that imported the scanner would be testing the scanner's idea of
    itself, and the one thing Phase C has to establish is that the MCP boundary
    is all there is between them.

    Matched on import statements rather than on the word, because the word
    appears in these files' own docstrings explaining that they do not import
    it -- a gate that reads its own explanation as a violation is a gate nobody
    keeps.
    """
    importing = re.compile(r"^\s*(?:import|from)\s+ratemyagent\b", re.M)
    for path in sorted(AGENTS.glob("*.py")):
        source = path.read_text()
        assert not importing.search(source), path.name
        # And each really is a module that runs standalone.
        assert "__main__" in source or path.name.startswith("_"), path.name


def test_every_fixture_agent_but_one_sets_a_read_timeout():
    """A6: three agents with a timeout, one deliberate variant without."""
    timeout_less = []
    for path in sorted(AGENTS.glob("*_agent.py")):
        if "read_timeout_s=None" in path.read_text():
            timeout_less.append(path.name)
    assert timeout_less == ["no_timeout_agent.py"]
