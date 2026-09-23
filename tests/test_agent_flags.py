"""`--repeats` and `--agent-kind`, from the command line.

Both were built in 1.6.1 with their machinery reachable only from Python. This
is the wiring: that each flag is declared on **both** `scan` and `ci`, refused
on any other target, and — the part §8b entry 36 is about — actually *in effect*
rather than merely accepted.

**Each has a default-flags arm.** A default is the configuration nobody chose,
so it is the one no fixture author reaches for, and 1.4.0's `--verify-tool`
passed its whole suite and its mutation matrix while reporting a false `stale`
on every default-seed scan. The arms below pass no `--seed`, no `--repeats` and
no `--agent-kind`, and assert what a scan does when nobody asked for anything.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _upstream(tmp_path: Path) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--state", str(tmp_path / "state.jsonl"),
    ])


def _run(command: str, tmp_path: Path, *extra: str, agent: str = "careful_agent.py"):
    """One scan or ci invocation with `--json-out`, and the document it wrote."""
    out = tmp_path / "out.json"
    result = CliRunner().invoke(cli, [
        command, "--target", "agent",
        "--agent", _agent(agent),
        "--tasks", str(TASKS),
        "--upstream", _upstream(tmp_path),
        "--work-dir", str(tmp_path / "work"),
        "--allow-mutating",
        "--verify-tool", "effects", "--verify-count", "entries",
        "--json-out", str(out),
        *extra,
    ])
    document = json.loads(out.read_text()) if out.exists() else None
    return result, document


def _behavior(document: dict) -> dict:
    return next(
        probe["metrics"] for probe in document["probes"] if probe["probe"] == "behavior"
    )


class TestTheDefaults:
    """No flags at all: what a scan does when nobody chose anything.

    Deliberately passing no `--seed`, no `--repeats` and no `--agent-kind`.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def plain(cls, tmp_path_factory):
        return _run("scan", tmp_path_factory.mktemp("plain"))

    def test_the_scan_completes(self, plain):
        result, document = plain
        assert result.exit_code in (0, 1), result.output
        assert document is not None

    def test_the_task_set_runs_once(self, plain):
        """`--repeats` defaults to 1, and a default of 1 has to look like a
        scan with no repeats rather than like a repeat run of one."""
        _, document = plain
        metrics = _behavior(document)
        assert metrics.get("repeat_groups") is None
        assert metrics.get("repeat_by_group") is None
        fault = next(
            p["metrics"] for p in document["probes"] if p["probe"] == "fault"
        )
        assert fault["repeats"] == 1
        assert len(fault["runs"]) == 1

    def test_the_agent_is_taken_to_be_scripted(self, plain):
        _, document = plain
        assert document["target"]["metadata"]["agent_kind"] == "scripted"

    def test_and_therefore_nothing_is_withheld(self, plain):
        """The default must not quietly unscore a scripted agent, which is what
        every scan before 1.6.1 was."""
        metrics = _behavior(plain[1])
        assert metrics["agent_kind"] == "scripted"
        assert metrics["retry_amplification"] is not None


class TestAgentKindReachesTheTarget:
    """Declared on both commands, and in effect from both."""

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_llm_reaches_the_target(self, command, tmp_path):
        _, document = _run(command, tmp_path, "--agent-kind", "llm")
        assert document["target"]["metadata"]["agent_kind"] == "llm"

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_and_the_withholding_actually_happens(self, command, tmp_path):
        """§8b entry 36: a flag that is accepted and configures nothing. The
        metadata alone would not catch that — this reads the consequence."""
        _, document = _run(command, tmp_path, "--agent-kind", "llm")
        metrics = _behavior(document)
        assert metrics["retry_amplification"] is None
        assert metrics["backoff_shape"] is None
        assert metrics["unscored_retry_amplification"] is not None

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_scripted_is_still_scored_from_both(self, command, tmp_path):
        _, document = _run(command, tmp_path, "--agent-kind", "scripted")
        assert _behavior(document)["retry_amplification"] is not None

    def test_an_unknown_kind_is_refused_by_click(self):
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "x", "--tasks", str(TASKS),
            "--upstream", "stdio://x", "--agent-kind", "model",
        ])
        assert result.exit_code != 0
        assert "model" in result.output


class TestRepeatsReachesTheRun:
    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_the_task_set_runs_n_times(self, command, tmp_path):
        _, document = _run(command, tmp_path, "--repeats", "3")
        fault = next(
            p["metrics"] for p in document["probes"] if p["probe"] == "fault"
        )
        assert fault["repeats"] == 3
        assert len(fault["runs"]) == 3

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_and_the_report_carries_a_range(self, command, tmp_path):
        """The consequence, not just the count: a flag that ran three times and
        reported one number would be accepted and useless."""
        _, document = _run(command, tmp_path, "--repeats", "3")
        metrics = _behavior(document)
        assert metrics["repeats"] == 3
        assert metrics["repeat_by_group"]
        rendered = " ".join(
            value
            for group in metrics["repeat_by_group"]
            for value in group["metrics"].values()
        )
        assert "n=3" in rendered
        assert "mean" not in rendered

    def test_each_repeat_writes_its_own_record(self, tmp_path):
        """Shared record files would carry the first run's ordinals into the
        second, so the schedule would start wherever the last run stopped."""
        _run("scan", tmp_path, "--repeats", "3")
        work = tmp_path / "work"
        assert (work / "record-chaos-t1.jsonl").exists()
        assert (work / "record-chaos2-t1.jsonl").exists()
        assert (work / "record-chaos3-t1.jsonl").exists()

    def test_the_first_run_keeps_the_original_pass_name(self, tmp_path):
        """So an R=1 scan writes exactly the files it wrote before repeats."""
        _run("scan", tmp_path, "--repeats", "1")
        work = tmp_path / "work"
        assert (work / "record-chaos-t1.jsonl").exists()
        assert not (work / "record-chaos1-t1.jsonl").exists()

    def test_scoring_says_it_took_the_worst_run(self, tmp_path):
        _, document = _run("scan", tmp_path, "--repeats", "3")
        assert "worst of 3 runs" in _behavior(document)["repeat_scoring"]

    def test_zero_repeats_is_refused(self):
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "x", "--tasks", str(TASKS),
            "--upstream", "stdio://x", "--repeats", "0",
        ])
        assert result.exit_code != 0
        assert "at least 1" in result.output


class TestRepeatsAgainstTheBudget:
    """Refused before the first agent starts, not discovered two-thirds in."""

    def test_a_repeat_count_that_cannot_fit_the_scan_timeout_is_refused(self):
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "x", "--tasks", str(TASKS),
            "--upstream", "stdio://x", "--repeats", "10",
            "--timeout", "60", "--scan-timeout", "120",
        ])
        assert result.exit_code != 0
        assert "--repeats 10 does not fit in --scan-timeout 120s" in result.output
        # The arithmetic is shown, because "too big" without the numbers is not
        # something a user can act on.
        assert "2 task(s) at --timeout 60s" in result.output
        assert "Raise --scan-timeout above" in result.output

    def test_a_repeat_count_that_fits_is_not_refused(self):
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "x", "--tasks", str(TASKS),
            "--upstream", "stdio://x", "--repeats", "2",
            "--timeout", "10", "--scan-timeout", "3600",
        ])
        assert "does not fit" not in result.output

    def test_an_unset_scan_timeout_is_not_graded(self):
        """The default budget is derived from `--timeout` and is deliberately
        generous. Refusing against a number nobody chose would be grading our
        own head-room."""
        result = CliRunner().invoke(cli, [
            "scan", "--target", "agent", "--agent", "x", "--tasks", str(TASKS),
            "--upstream", "stdio://x", "--repeats", "50", "--timeout", "600",
        ])
        assert "does not fit" not in result.output


class TestBothFlagsAreRefusedElsewhere:
    @pytest.mark.parametrize("command", ["scan", "ci"])
    @pytest.mark.parametrize("flag,value", [
        ("--repeats", "3"),
        ("--agent-kind", "llm"),
    ])
    def test_refused_on_a_non_agent_target(self, command, flag, value):
        """Same rule as every other agent flag: ignored is worse than refused."""
        result = CliRunner().invoke(cli, [command, "--target", "mock", flag, value])
        assert result.exit_code != 0
        assert f"{flag} is --target agent only" in result.output
