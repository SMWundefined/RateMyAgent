"""What the "no calls recorded" refusal tells the user -- read off the disk.

**The refusal is right; its advice encoded a prior.** A task with no call on its
record is refused (absence is not zero, PROGRESS 8b entry 28). Until 1.7.4 the
message said the record "is empty or missing" and told the user to check the
config's `env` block, whatever the record held. Gate BD's replication on
2026-09-24 refused on a chaos record holding a `notifications/initialized` row.
The proxy wrote that row, so the record path had arrived -- and the refusal
still said the file was empty and blamed the env block.

`explain_unrecorded` classifies the record before choosing its advice. Four
branches, each pinned here by the text the user reads:

1. **missing** -- `there is no record at`; the env block is advised, unless
2. another pass's record for the task holds rows -- then it is not;
3. **rows, none a call** -- `holds no tool calls`, the rows counted, the
   agent's own exit code and claim, and **no env-block advice**;
4. **present, no readable row** -- zero bytes keeps the env block as one
   candidate among several; bytes without a readable row, or another pass's
   rows, rule it out.

Every branch: "empty or missing" never appears. The suppression in branch 3 is
also pinned end to end through the CLI, reproducing replicate 2 with
`quitter_agent`. And the deliberate failing case: the predicate these tests use
to detect env-block advice fires on the 1.7.3 message, verbatim.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli
from ratemyagent.models import Response
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.base import ProbeConfig, ProbeRefusal
from ratemyagent.proxy import explain_unrecorded
from ratemyagent.targets.agent import AgentTarget

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"

#: The 1.7.3 refusal from the replication's replicate 2, verbatim (paths cut).
OLD_MESSAGE = (
    "no calls recorded for task 't1' under fault: the record at work-2/"
    "record-chaos-t1.jsonl is empty or missing, so nothing about this task was "
    "measured. An empty record is not zero calls -- it is no evidence.\n\n"
    "Check the `env` block in work-2/mcp-chaos-t1.json reaches the proxy: the MCP "
    "SDK copies six variables into a stdio child and drops the rest, so "
    "RMA_PROXY_RECORD travels in that block or not at all."
)
INITIALIZED = {"kind": "notification", "sequence": 0, "task_id": "t1",
               "method": "notifications/initialized", "params": None,
               "received_at": 1.0}
CALL = {"kind": "invocation", "sequence": 1, "op": "event", "ok": True, "task_id": "t1"}


def flat(text: str) -> str:
    """What a reader reads: the CLI wraps its output, so compare words."""
    return " ".join(text.split())


def advises_env_block(text: str) -> bool:
    """Does this message send the user to the config's `env` block?"""
    return "check the `env` block" in flat(text).lower()


def _write(path: Path, rows: list[dict] | None = None, raw: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows or []))


def _failed(exit_code: int = 1, error: str = "connected, made no tool call") -> Response:
    return Response(ok=False, latency_s=0.1, error=error,
                    meta={"outcome": "failed", "task_id": "t1", "exit_code": exit_code})


def _explain(work: Path, *, response=None) -> str:
    return explain_unrecorded("t1", work / "record-chaos-t1.jsonl",
                              work / "mcp-chaos-t1.json",
                              response=response, under_fault=True)


# -- the deliberate failing case ------------------------------------------------


def test_the_predicate_catches_the_1_7_3_message():
    """The tests below assert `advises_env_block` is False where it must be. A
    predicate that could not fire would make every one of them pass vacuously,
    so it is shown to fire on the message this release replaces."""
    assert advises_env_block(OLD_MESSAGE)
    assert "empty or missing" in OLD_MESSAGE


# -- branch 1: missing ----------------------------------------------------------


def test_missing_with_no_other_evidence_keeps_the_env_advice(tmp_path):
    msg = _explain(tmp_path)
    assert flat(msg).startswith(
        "no calls recorded for task 't1' under fault: there is no record at")
    assert advises_env_block(msg)
    assert "RMA_PROXY_RECORD" in msg
    assert "empty or missing" not in msg


def test_missing_with_another_pass_on_disk_rules_the_env_block_out(tmp_path):
    _write(tmp_path / "record-baseline-t1.jsonl", [INITIALIZED, CALL])
    msg = _explain(tmp_path, response=_failed())
    assert "there is no record at" in flat(msg)
    assert ("The baseline record for this task holds 2 row(s), 1 of them tool call(s)"
            in flat(msg))
    assert "this is not the `env` block" in flat(msg)
    assert not advises_env_block(msg)
    assert "empty or missing" not in msg


# -- branch 2 (the replication's case): rows, none a call ------------------------


def test_a_row_on_the_record_suppresses_the_env_advice(tmp_path):
    _write(tmp_path / "record-chaos-t1.jsonl", [INITIALIZED])
    _write(tmp_path / "record-baseline-t1.jsonl", [INITIALIZED, CALL])
    msg = _explain(tmp_path, response=_failed(exit_code=1))
    text = flat(msg)
    assert "holds no tool calls -- 1 row(s): notifications/initialized --" in text
    assert "Those rows were written by the proxy, so the record path reached it" in text
    assert "The agent connected and made no tool call." in text
    assert "The agent exited 1 and reported failure: connected, made no tool call" in text
    assert not advises_env_block(msg)
    assert "RMA_PROXY_RECORD" not in msg
    assert "empty or missing" not in msg


def test_rows_are_counted_by_what_they_are(tmp_path):
    cancelled = dict(INITIALIZED, method="notifications/cancelled", sequence=1)
    _write(tmp_path / "record-chaos-t1.jsonl", [INITIALIZED, cancelled, cancelled])
    text = flat(_explain(tmp_path))
    assert "3 row(s): notifications/initialized, notifications/cancelled x2" in text


# -- branch 3/4: present, no readable row ---------------------------------------


def test_zero_bytes_and_no_other_evidence_leaves_the_env_block_one_candidate(tmp_path):
    _write(tmp_path / "record-chaos-t1.jsonl", raw=b"")
    msg = _explain(tmp_path)
    text = flat(msg)
    assert "holds no tool calls -- no readable row in 0 byte(s)" in text
    assert "Several causes fit and nothing here tells them apart" in text
    assert advises_env_block(msg)          # one candidate among several, and said so
    assert "empty or missing" not in msg


def test_zero_bytes_with_another_pass_on_disk_rules_the_env_block_out(tmp_path):
    _write(tmp_path / "record-chaos-t1.jsonl", raw=b"")
    _write(tmp_path / "record-baseline-t1.jsonl", [INITIALIZED, CALL])
    msg = _explain(tmp_path)
    assert "no readable row in 0 byte(s)" in flat(msg)
    assert "this is not the `env` block" in flat(msg)
    assert not advises_env_block(msg)


def test_bytes_without_a_readable_row_mean_the_proxy_had_the_path(tmp_path):
    _write(tmp_path / "record-chaos-t1.jsonl", raw=b'{"kind": "notifica')
    msg = _explain(tmp_path)
    assert "no readable row in 18 byte(s)" in flat(msg)
    assert "started writing here and was cut off" in flat(msg)
    assert not advises_env_block(msg)


def test_another_task_s_record_is_not_this_task_s_evidence(tmp_path):
    """Task `1` must not claim task `b-1`'s record by a loose glob."""
    _write(tmp_path / "record-baseline-b-1.jsonl", [CALL])
    msg = explain_unrecorded("1", tmp_path / "record-chaos-1.jsonl",
                             tmp_path / "mcp-chaos-1.json")
    assert advises_env_block(msg)          # no sibling evidence for task 1


def test_an_abandoned_agent_is_described_as_killed(tmp_path):
    _write(tmp_path / "record-chaos-t1.jsonl", [INITIALIZED])
    killed = Response(ok=False, latency_s=240.0, error="killed",
                      meta={"outcome": "abandoned", "task_id": "t1"})
    assert "did not finish within the scan's deadline and was killed" in flat(
        _explain(tmp_path, response=killed))


# -- end to end: what the user reads ---------------------------------------------


def _agent(*argv: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / "quitter_agent.py"), *argv])


def _one_task(tmp_path: Path) -> Path:
    tasks = json.loads((AGENTS / "tasks.json").read_text())
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": [tasks["tasks"][0]]}))
    return path


def test_the_replication_s_refusal_end_to_end_through_the_cli(tmp_path):
    """Replicate 2, reproduced: a clean pass with its call, then a chaos pass in
    which the agent connects and quits. The user reads the CLI's output."""
    work = tmp_path / "work"
    result = CliRunner().invoke(cli, [
        "scan", "--target", "agent", "--agent", _agent(),
        "--tasks", str(_one_task(tmp_path)),
        "--upstream", "stdio://" + shlex.join(
            [sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
             "--state", str(tmp_path / "state.jsonl")]),
        "--allow-mutating", "--verify-tool", "effects", "--verify-count", "entries",
        "--fault-rate", "0.2", "--seed", "3", "--work-dir", str(work),
    ])
    text = flat(result.output)
    assert result.exit_code == 2, result.output
    assert "no calls recorded for task 't1' under fault" in text
    assert "holds no tool calls -- 1 row(s): notifications/initialized --" in text
    assert "this is not the `env` block" in text
    assert "The agent exited 1 and reported failure: connected, made no tool call" in text
    assert "The baseline record for this task holds" in text
    assert not advises_env_block(result.output)
    assert "RMA_PROXY_RECORD" not in result.output
    assert "empty or missing" not in result.output
    # And the record really is what the message says it is.
    rows = [json.loads(line) for line in (work / "record-chaos-t1.jsonl").read_text().splitlines()]
    assert [r.get("method") for r in rows] == ["notifications/initialized"]


async def test_the_baseline_refusal_reads_the_disk_too(tmp_path):
    """The clean-pass site, through the probe: connect, quit, refuse."""
    tasks = _one_task(tmp_path)
    target = AgentTarget(
        agent_command=_agent("--quit-in", "baseline"), tasks_path=tasks,
        upstream="stdio://" + shlex.join(
            [sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
             "--state", str(tmp_path / "state.jsonl")]),
        work_dir=tmp_path / "work", allow_mutating=True,
    )
    await target.setup()
    try:
        with pytest.raises(ProbeRefusal) as exc:
            await AgentBaseline().execute(target, ProbeConfig())
    finally:
        await target.teardown()
    msg = str(exc.value)
    assert flat(msg).startswith("no calls recorded for task 't1': the record at")
    assert "holds no tool calls -- 1 row(s): notifications/initialized --" in flat(msg)
    assert "The agent exited 1 and reported failure" in flat(msg)
    assert not advises_env_block(msg)
    assert "empty or missing" not in msg
