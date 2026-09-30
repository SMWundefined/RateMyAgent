"""1.9.0: `--verify-command`, `--key-path`/`retry_keys`, `suggested_seed`,
`PASS, UNRECONCILED`, and the agent's stderr on every exit without a result.

DESIGN-TIER-1 with the 1.9.0 brief's changes. Registered predictions:
`assets/moat/1.9.0/PREDICTIONS-1.9.0.md`. Every new check here has a case built
to fail it, and each feature has one run on default flags (CLAUDE.md check 5).

The upstream is `tests/fixtures/orders_mcp_server.py`, the README
walkthrough's server: `create_order` writes a row into `orders` in a SQLite
file. The verify command reads that file through the stdlib `sqlite3` module
via `python -c` -- the `sqlite3` CLI is not guaranteed on CI runners -- and the
interpreter's own path is quoted, which in this repository's checkout contains
a space.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli
from ratemyagent.models import FaultKind, ProbeResult, ScanResult, TargetInfo
from ratemyagent.outputs.common import UNRECONCILED, verdict_lines
from ratemyagent.policy import Policy, evaluate, unreconciled_readings
from ratemyagent.probes.agent_metrics import retry_key_metrics, sum_retry_keys
from ratemyagent.probes.fault import (
    FaultInjector,
    draw_fault,
    schedule_kinds,
    suggest_seed,
)
from ratemyagent.proxy import (
    IDEMPOTENCY_ARG,
    key_at,
    operation_fingerprint,
    read_key_path,
    write_schedule,
)
from ratemyagent.targets import AgentTarget, TargetError
from ratemyagent.targets.agent import (
    DEFAULT_KEY_PATH,
    STDERR_TAIL_CHARS,
    parse_verify_output,
    run_verify_command,
    verify_command_digest,
)
from ratemyagent.targets.fault_proxy import ALL_FAULTS, OPT_IN_FAULTS, FaultConfig

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
ORDERS = ROOT / "tests" / "fixtures" / "orders_mcp_server.py"

#: The walkthrough's task, with the arguments a scripted agent sends.
ORDER = {"customer_id": 42, "item": "rma-probe-1", "quantity": 1}
#: The first seed from the default 1337 whose table drops the reply to
#: `t1`'s first `create_order` call, by the stdlib re-implementation in
#: `assets/moat/1.9.0/predict_inputs.py` (registered before the build).
WALKTHROUGH_SEED = 1431


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _agent(name: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name), *extra])


def _upstream(db: Path, *extra: str) -> str:
    return "stdio://" + shlex.join([sys.executable, str(ORDERS), "--db", str(db), *extra])


def _count_command(db: Path) -> str:
    """`select count(*) from orders`, through the stdlib, as a shell string."""
    script = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "print(c.execute('select count(*) from orders').fetchone()[0])"
    )
    return f"{_q(sys.executable)} -c {_q(script)} {_q(db)}"


def _entries_command(db: Path) -> str:
    """The rows as a JSON list; `json_group_array` prints `[]` when empty."""
    script = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("
        "\"select json_group_array(json_object('id',id,'item',item)) from orders\""
        ").fetchone()[0])"
    )
    return f"{_q(sys.executable)} -c {_q(script)} {_q(db)}"


def _tasks(path: Path, arguments: dict | None = None, **task) -> Path:
    body = {
        "id": "t1",
        "prompt": "Create an order for customer 42, item 'rma-probe-1', quantity 1.",
        "expected_effects": 1, "tool": "create_order",
        "arguments": dict(ORDER if arguments is None else arguments),
        **task,
    }
    path.write_text(json.dumps({"tasks": [body]}))
    return path


def _make_db(db: Path) -> Path:
    sqlite3.connect(db).execute(
        "create table if not exists orders (id integer primary key autoincrement,"
        " customer_id integer not null, item text not null,"
        " quantity integer not null, idempotency_key text unique)"
    ).connection.close()
    return db


def _run(command: str, *args: str):
    return CliRunner().invoke(cli, [command, *args])


def _agent_args(tmp: Path, db: Path, agent: str, *extra: str) -> list[str]:
    return [
        "--target", "agent", "--agent", agent,
        "--tasks", str(_tasks(tmp / "tasks.json")),
        "--upstream", _upstream(db),
        "--work-dir", str(tmp / "work"),
        "--allow-mutating", *extra,
    ]


def _metrics(path: Path, probe: str) -> dict:
    data = json.loads(path.read_text())
    return next(p for p in data["probes"] if p["probe"] == probe)["metrics"]


# -- 1. the read: parse and failure table (PREDICTIONS §1, P1-P18) ------------


class TestTheParse:
    @pytest.mark.parametrize("stdout, path, expected", [
        ("3\n", None, 3),                                   # P1
        ("0", None, 0),                                     # P2
        ("[]", None, []),                                   # P3
        ('[{"id":1,"item":"a"}]', None, [{"id": 1, "item": "a"}]),  # P4
        ('{"items":[1,2]}', "items", [1, 2]),               # P13
        ('{"n":4}', "n", 4),                                # P15
    ])
    def test_a_count_or_a_list_is_a_reading(self, stdout, path, expected):
        assert parse_verify_output(stdout, path) == expected

    @pytest.mark.parametrize("stdout, path", [
        ("", None),                  # P5: empty stdout is not []
        ("   \n\t", None),           # P6
        ("3.5", None),               # P7
        ("3.0", None),               # P8: a float, even an integral one
        ("-1", None),                # P9
        ("true", None),              # P10: a bool is not a count
        ('"3"', None),               # P11
        ("3 rows", None),            # P12: not JSON
        ('{"items":[1,2]}', None),   # P14: an object with no path
        ('{"a":1}', "items"),        # P16: a path that misses
        ('{"items":"x"}', "items"),  # a path to a string
    ])
    def test_anything_else_is_no_reading_and_never_zero(self, stdout, path):
        assert parse_verify_output(stdout, path) is None


class TestTheRun:
    async def test_a_non_zero_exit_is_not_read(self):
        """P17: a psql that printed 0 and exited 2 did not measure."""
        read = await run_verify_command("echo 0; exit 2", timeout_s=10)
        assert read.value is None
        assert read.exit_code == 2
        assert read.first_line == "0"

    async def test_a_timeout_kills_the_whole_process_group(self, tmp_path):
        """P18: the shell and a backgrounded grandchild both go."""
        pid_file = tmp_path / "pid"
        read = await run_verify_command(
            f"sleep 30 & echo $! > {_q(pid_file)}; wait", timeout_s=1,
        )
        assert read.value is None and read.timed_out
        grandchild = int(pid_file.read_text())
        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild, 0)

    async def test_stdin_is_closed_so_a_prompt_fails_instead_of_hanging(self):
        started = time.monotonic()
        read = await run_verify_command("read answer; echo ${answer:-none}", timeout_s=10)
        assert time.monotonic() - started < 5
        assert read.value is None and read.first_line == "none"

    async def test_the_scans_environment_is_inherited(self, monkeypatch):
        """A secret stays out of the text: it expands in the shell."""
        monkeypatch.setenv("RMA_TEST_COUNT", "7")
        read = await run_verify_command('echo "$RMA_TEST_COUNT"', timeout_s=10)
        assert read.value == 7


# -- 1. setup refusals (S1, S2, S6) -----------------------------------------


def _target(tmp: Path, command: str, **kwargs) -> AgentTarget:
    kwargs.setdefault("allow_mutating", True)
    return AgentTarget(
        agent_command=_agent("blind_agent.py"),
        tasks_path=_tasks(tmp / "tasks.json"),
        upstream=_upstream(tmp / "app.db"),
        work_dir=tmp / "work",
        verify_command=command,
        **kwargs,
    )


class TestSetup:
    async def test_a_command_that_does_not_measure_is_refused_with_its_evidence(
        self, tmp_path,
    ):
        """S1: exit code, the stderr tail and the first stdout line, all printed."""
        target = _target(tmp_path, "echo 'no such table: orders' >&2; echo oops; exit 3")
        with pytest.raises(TargetError) as caught:
            await target.setup()
        text = str(caught.value)
        assert "did not measure at setup" in text
        assert "Exit code: 3" in text
        assert "no such table: orders" in text
        assert "'oops'" in text

    async def test_two_readings_that_differ_are_refused(self, tmp_path):
        """S2: a reading that moves with nothing running cannot bracket a task."""
        counter = tmp_path / "n"
        command = (
            f"n=$(cat {_q(counter)} 2>/dev/null || echo 0); "
            f"echo $((n+1)) > {_q(counter)}; echo $n"
        )
        with pytest.raises(TargetError) as caught:
            await _target(tmp_path, command).setup()
        assert "read 0 and then 1 at setup" in str(caught.value)

    async def test_two_equal_readings_pass(self, tmp_path):
        """The control for S2: the same store, read twice, agrees."""
        db = _make_db(tmp_path / "app.db")
        target = _target(tmp_path, _count_command(db))
        await target.setup()
        assert await target.read_effect_entries() == 0

    async def test_without_allow_mutating_it_is_refused(self, tmp_path):
        """S6: the same rule as --verify-tool."""
        db = _make_db(tmp_path / "app.db")
        with pytest.raises(TargetError, match="needs --allow-mutating"):
            await _target(tmp_path, _count_command(db), allow_mutating=False).setup()

    def test_both_oracles_is_refused_by_the_constructor_too(self, tmp_path):
        with pytest.raises(TargetError, match="mutually exclusive"):
            _target(tmp_path, "echo 0", verify_tool="list_orders")


class TestTheFlags:
    """S3, S4, S5: usage errors, exit 2, before anything starts."""

    def _args(self, tmp_path: Path, *extra: str) -> list[str]:
        return [
            "--target", "agent", "--agent", _agent("blind_agent.py"),
            "--tasks", str(_tasks(tmp_path / "tasks.json")),
            "--upstream", _upstream(tmp_path / "app.db"), "--allow-mutating", *extra,
        ]

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_both_oracles_is_a_usage_error(self, tmp_path, command):
        result = _run(command, *self._args(
            tmp_path, "--verify-command", "echo 0", "--verify-tool", "list_orders",
        ))
        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in result.output
        assert not (tmp_path / "app.db").exists()

    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_verify_args_with_a_command_is_a_usage_error(self, tmp_path, command):
        result = _run(command, *self._args(
            tmp_path, "--verify-command", "echo 0", "--verify-args", "{}",
        ))
        assert result.exit_code == 2, result.output
        assert "--verify-args is for an MCP --verify-tool" in result.output

    @pytest.mark.parametrize("flag, value", [
        ("--verify-command", "echo 0"), ("--key-path", "options.key"),
    ])
    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_both_flags_are_agent_only(self, tmp_path, command, flag, value):
        result = _run(command, "--target", "mcp", "--uri", _upstream(tmp_path / "a.db"),
                      flag, value)
        assert result.exit_code == 2, result.output
        assert f"{flag} is --target agent only" in result.output

    def test_a_key_path_with_an_empty_segment_is_refused(self, tmp_path):
        result = _run("scan", *self._args(tmp_path, "--key-path", "options..key"))
        assert result.exit_code == 2, result.output
        assert "is not a dotted path" in result.output


class TestTheDigest:
    def test_first_word_and_a_hash_never_the_text(self):
        digest = verify_command_digest("PGPASSWORD=hunter2 /Users/me/bin/psql -c 'select 1'")
        assert digest.startswith("psql sha256:")
        assert "hunter2" not in digest and "/Users/me" not in digest

    def test_absent_is_none(self):
        assert verify_command_digest(None) is None


# -- 1 + 3, end to end: default flags, a path with a space, the seed ----------


@pytest.fixture(scope="module")
def spaced(tmp_path_factory) -> Path:
    """A working directory whose path contains a space."""
    root = tmp_path_factory.mktemp("verify") / "with space"
    root.mkdir()
    return root


class TestOnDefaultFlags:
    """Nothing beyond the required flags: no --seed, --fault-rate or --repeats.

    One task, one write, the walkthrough's shape. At the default seed the
    table drops no reply, so the honest result is NO VERDICT -- and 1.9.0's
    reason names a seed that does (DESIGN-TIER-1 1.6).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, spaced):
        db = _make_db(spaced / "app.db")
        out = spaced / "default.scan.json"
        # A made-up credential in the command's environment prefix: it must
        # not travel into the export.
        command = "RMA_FAKE_TOKEN=s3cr3t-token " + _count_command(db)
        result = _run("ci", *_agent_args(
            spaced, db, _agent("blind_agent.py"),
            "--verify-command", command, "--json-out", str(out),
        ))
        return result, out, db

    def test_it_completes_with_no_verdict_naming_a_seed(self, run):
        result, _, _ = run
        assert result.exit_code == 2, result.output
        assert (
            "NO VERDICT  no task had a call whose outcome was unknown; raise "
            f"--fault-rate, or --seed {WALKTHROUGH_SEED} drops the reply to t1's "
            "first create_order call." in result.output
        )

    def test_every_read_measured(self, run):
        _, out, db = run
        behavior = _metrics(out, "behavior")
        assert behavior["task_oracle_status"] == {"t1": "ok"}
        assert behavior["effects_by_task"] == {"t1": 1}
        assert behavior["duplicate_mutations"] == 0
        # The clean pass and the chaos pass each wrote one row.
        assert sqlite3.connect(db).execute("select count(*) from orders").fetchone()[0] == 2

    def test_the_seed_is_a_fault_probe_metric(self, run):
        _, out, _ = run
        assert _metrics(out, "fault")["suggested_seed"] == {
            "seed": WALKTHROUGH_SEED, "task_id": "t1", "tool": "create_order",
        }

    def test_the_command_is_written_down_as_its_digest_only(self, run):
        _, out, _ = run
        text = out.read_text()
        metadata = json.loads(text)["target"]["metadata"]
        assert metadata["verify_command_digest"].startswith(
            Path(sys.executable).name + " sha256:"
        )
        assert metadata["verify_tool"] is None
        assert "s3cr3t-token" not in text
        assert "select count(*)" not in text

    def test_retry_keys_is_printed_on_default_flags(self, run):
        result, out, _ = run
        # blind_agent sends no key, and --key-path was not given.
        assert _metrics(out, "behavior")["retry_keys"] is None
        assert (
            "not read: no call carried idempotency_key; pass --key-path if your "
            "tool takes its key elsewhere" in result.output
        )

    def test_the_suggested_seed_does_drop_the_reply(self, spaced, run):
        """The deliberate check on the suggestion: re-run at it."""
        root = spaced / "reseeded"
        root.mkdir()
        db = _make_db(root / "app.db")
        out = root / "reseeded.scan.json"
        _run("ci", *_agent_args(
            root, db, _agent("blind_agent.py"),
            "--verify-command", _count_command(db), "--json-out", str(out),
            "--seed", str(WALKTHROUGH_SEED),
        ))
        behavior = _metrics(out, "behavior")
        assert behavior["uncertain_tasks"] == 1
        assert behavior["realized_placement"].startswith(
            "t1:create_order#1=response_lost"
        )
        assert _metrics(out, "fault")["suggested_seed"] is None


class TestFailures:
    def test_a_mid_scan_failed_read_is_no_verdict_naming_the_command(self, tmp_path):
        """S7: reads 1-4 answer (setup x2, clean window); the chaos window does not."""
        db = _make_db(tmp_path / "app.db")
        counter = tmp_path / "reads"
        command = (
            f"n=$(cat {_q(counter)} 2>/dev/null || echo 0); "
            f"echo $((n+1)) > {_q(counter)}; "
            f"if [ $n -ge 4 ]; then echo 'database is locked' >&2; exit 5; fi; "
            + _count_command(db)
        )
        out = tmp_path / "out.scan.json"
        result = _run("ci", *_agent_args(
            tmp_path, db, _agent("blind_agent.py"),
            "--verify-command", command, "--json-out", str(out),
            "--seed", str(WALKTHROUGH_SEED),
        ))
        assert result.exit_code == 2, result.output
        assert "NO VERDICT  the verify command did not read the upstream around task t1" in (
            result.output
        )
        assert _metrics(out, "behavior")["task_oracle_status"] == {"t1": "failed"}

    def test_a_server_that_keeps_state_in_memory_is_refused(self, tmp_path):
        """S8: the clean-pass persistence refusal, naming the command."""
        db = _make_db(tmp_path / "app.db")
        args = _agent_args(tmp_path, db, _agent("blind_agent.py"),
                           "--verify-command", _count_command(db))
        args[args.index("--upstream") + 1] = "stdio://" + shlex.join(
            [sys.executable, str(ORDERS), "--db", ":memory:"]
        )
        result = _run("scan", *args)
        assert result.exit_code == 2, result.output
        assert "the verify command does not see the effects" in result.output
        assert "reads whatever store it names" in result.output

    def test_an_unquoted_agent_path_with_a_space_is_refused(self, spaced):
        # Refused at setup, before the agent runs, so its content is moot.
        agent_copy = spaced / "blind agent.py"
        agent_copy.write_text("raise SystemExit('never started')\n")
        db = _make_db(spaced / "refused.db")
        result = _run("scan", *_agent_args(
            spaced, db, f"{_q(sys.executable)} {agent_copy}",
            "--verify-command", _count_command(db),
        ))
        assert result.exit_code == 2, result.output
        assert "--agent splits on whitespace" in result.output
        assert f"\"{agent_copy}\"" in result.output


class TestAUsedWorkDir:
    """Step 6 of the walkthrough, re-run into step 5's `--work-dir` (1.9.0).

    The proxy continues a record, so a second scan into the same directory
    started its schedule where the first stopped: at the suggested seed the
    re-run realized no fault and named the same seed again.
    """

    def test_a_second_scan_into_the_same_work_dir_is_refused(self, tmp_path):
        db = _make_db(tmp_path / "app.db")
        args = _agent_args(tmp_path, db, _agent("blind_agent.py"),
                           "--verify-command", _count_command(db))
        first = _run("scan", *args)
        assert first.exit_code == 0, first.output
        rows = (tmp_path / "work" / "record-chaos-t1.jsonl").read_text()

        second = _run("scan", *args, "--seed", str(WALKTHROUGH_SEED))
        assert second.exit_code == 2, second.output
        assert "already holds" in second.output
        assert "record-baseline-t1.jsonl" in second.output
        # Refused, never repaired: the earlier scan's evidence is untouched.
        assert (tmp_path / "work" / "record-chaos-t1.jsonl").read_text() == rows

    def test_a_new_work_dir_at_the_suggested_seed_drops_the_reply(self, tmp_path):
        """The control: the walkthrough's step 6 as the README now prints it."""
        db = _make_db(tmp_path / "app.db")
        args = _agent_args(tmp_path, db, _agent("blind_agent.py"),
                           "--verify-command", _count_command(db))
        assert _run("scan", *args).exit_code == 0
        args[args.index("--work-dir") + 1] = str(tmp_path / "work-2")
        out = tmp_path / "second.scan.json"
        _run("scan", *args, "--seed", str(WALKTHROUGH_SEED), "--json-out", str(out))
        assert _metrics(out, "behavior")["uncertain_tasks"] == 1


class TestEntriesFeedExpectedEntries:
    """A list from the command reads per entry exactly as the verify tool's does."""

    @pytest.mark.parametrize("oracle", ["command", "tool"])
    def test_the_same_per_entry_reading(self, tmp_path, oracle):
        db = _make_db(tmp_path / "app.db")
        extra = (
            ["--verify-command", _entries_command(db)] if oracle == "command"
            else ["--verify-tool", "list_orders"]
        )
        out = tmp_path / "out.scan.json"
        args = _agent_args(tmp_path, db, _agent("blind_agent.py"), *extra,
                           "--json-out", str(out))
        _tasks(tmp_path / "tasks.json", expected_entries=["rma-probe-1"])
        _run("scan", *args)
        task = _metrics(out, "fault")["task_results"]["t1"]
        assert task["effects_by_entry"] == {"rma-probe-1": 1}
        assert task["unmatched_effects"] == 0


# -- 2. --key-path and retry_keys -------------------------------------------


def _row(seq: int, key, *, operation: str | None = "op", fingerprint: str | None = None):
    return {
        "kind": "invocation", "sequence": seq, "op": "create_order",
        "idempotency_key": key, "operation_fingerprint": operation,
        "fingerprint": fingerprint or f"fp-{key}",
    }


class TestRetryKeys:
    def test_kept_changed_and_no_key(self):
        rows = [
            _row(1, "k1"), _row(2, "k1"), _row(3, "k2"), _row(4, None),
        ]
        assert retry_key_metrics({"t1": rows})["retry_keys"] == {
            "kept": 1, "changed": 1, "no_key": 1,
        }

    def test_a_key_where_the_first_had_none_is_changed(self):
        rows = [_row(1, None), _row(2, "k1")]
        assert retry_key_metrics({"t1": rows})["retry_keys"] == {
            "kept": 0, "changed": 1, "no_key": 0,
        }

    def test_rows_need_not_be_adjacent(self):
        """RUN-LIVE §3: the re-send came after two other writes."""
        rows = [
            _row(1, "a", operation="A"), _row(3, "b", operation="B"),
            _row(4, "c", operation="C"), _row(5, "a", operation="A"),
        ]
        assert retry_key_metrics({"t1": rows})["retry_keys"] == {
            "kept": 1, "changed": 0, "no_key": 0,
        }

    def test_two_tasks_do_not_share_an_operation(self):
        assert retry_key_metrics({"t1": [_row(1, "k")], "t2": [_row(2, "k")]})[
            "retry_keys"
        ] == {"kept": 0, "changed": 0, "no_key": 0}

    def test_a_pre_1_9_0_row_groups_on_its_full_fingerprint(self):
        """No operation_fingerprint: a changed key is a new operation there."""
        rows = [
            _row(1, "k1", operation=None, fingerprint="F"),
            _row(2, "k1", operation=None, fingerprint="F"),
            _row(3, "k2", operation=None, fingerprint="G"),
        ]
        assert retry_key_metrics({"t1": rows})["retry_keys"] == {
            "kept": 1, "changed": 0, "no_key": 0,
        }

    def test_nothing_carried_is_not_read_rather_than_no_key(self):
        rows = [_row(1, None), _row(2, None)]
        assert retry_key_metrics({"t1": rows})["retry_keys"] is None

    def test_a_key_on_the_clean_pass_makes_a_keyless_retry_no_key(self):
        """RUN-LIVE r2: keyed every clean write, keyed nothing in chaos.

        Judged per run, that read "not read", and the five replicates summed to
        `kept 1, no key 0` instead of the design's `kept 1, no key 4`.
        """
        rows = [_row(1, None), _row(2, None)]
        assert retry_key_metrics({"t1": rows}, seen_elsewhere=True)["retry_keys"] == {
            "kept": 0, "changed": 0, "no_key": 1,
        }

    def test_repeats_are_summed_never_averaged(self):
        runs = [{"kept": 1, "changed": 0, "no_key": 0}, None,
                {"kept": 0, "changed": 0, "no_key": 1}]
        assert sum_retry_keys(runs) == {"kept": 1, "changed": 0, "no_key": 1}
        assert sum_retry_keys([None, None]) is None


class TestTheProxyReadsTheKeyPath:
    def test_the_default_is_the_1_8_0_rule(self):
        assert DEFAULT_KEY_PATH == IDEMPOTENCY_ARG
        assert key_at({"idempotency_key": "k"}, IDEMPOTENCY_ARG) == "k"
        assert key_at({"idempotency_key": 3}, IDEMPOTENCY_ARG) is None
        assert key_at({"options": {"key": "k"}}, IDEMPOTENCY_ARG) is None

    def test_a_dotted_path_reads_a_nested_string_and_a_miss_is_none(self):
        assert key_at({"options": {"key": "k"}}, "options.key") == "k"
        assert key_at({"options": "k"}, "options.key") is None
        assert key_at({}, "options.key") is None

    def test_the_operation_fingerprint_ignores_only_the_key(self):
        first = operation_fingerprint("op", {"a": 1, "options": {"key": "x"}}, "options.key")
        second = operation_fingerprint("op", {"a": 1, "options": {"key": "y"}}, "options.key")
        other = operation_fingerprint("op", {"a": 2, "options": {"key": "x"}}, "options.key")
        assert first == second != other

    def test_without_a_key_it_is_the_fingerprint(self):
        from ratemyagent.models import Request

        request = Request(op="op", payload={"a": 1})
        assert operation_fingerprint("op", {"a": 1}, IDEMPOTENCY_ARG) == request.fingerprint

    def test_the_schedule_file_is_1_8_0s_without_the_flag(self, tmp_path):
        write_schedule(tmp_path / "s.json", {})
        assert json.loads((tmp_path / "s.json").read_text()) == {"entries": []}
        assert read_key_path(tmp_path / "s.json") == IDEMPOTENCY_ARG
        write_schedule(tmp_path / "k.json", {}, key_path="options.key")
        assert read_key_path(tmp_path / "k.json") == "options.key"


class TestKeyPathEndToEnd:
    """The key lives at `options.key`; a blind retry keeps it."""

    @pytest.fixture(scope="class")
    @classmethod
    def runs(cls, tmp_path_factory):
        out = {}
        for flag in (None, "options.key"):
            root = tmp_path_factory.mktemp("keypath")
            db = _make_db(root / "app.db")
            arguments = {**ORDER, "options": {"key": "order-42-1"}}
            args = _agent_args(root, db, _agent("blind_agent.py"),
                               "--verify-command", _count_command(db),
                               "--seed", str(WALKTHROUGH_SEED),
                               "--json-out", str(root / "out.scan.json"))
            _tasks(root / "tasks.json", arguments)
            args[args.index("--upstream") + 1] = _upstream(db, "--key-path", "options.key")
            if flag:
                args += ["--key-path", flag]
            result = _run("scan", *args)
            out[flag] = (result, _metrics(root / "out.scan.json", "behavior"), root)
        return out

    def test_with_the_flag_the_kept_key_is_counted(self, runs):
        result, metrics, root = runs["options.key"]
        assert metrics["retry_keys"]["kept"] >= 1, metrics["retry_keys"]
        assert metrics["retry_keys"]["changed"] == 0
        assert "(key at options.key)" in result.output
        rows = [json.loads(line) for line in
                (root / "work" / "record-chaos-t1.jsonl").read_text().splitlines()]
        keyed = [r for r in rows if r.get("kind") == "invocation"]
        assert {r["idempotency_key"] for r in keyed} == {"order-42-1"}

    def test_without_it_the_key_is_not_read_and_says_so(self, runs):
        result, metrics, _ = runs[None]
        assert metrics["retry_keys"] is None
        assert "pass --key-path if your tool takes its key elsewhere" in result.output

    def test_the_key_is_report_only(self, runs):
        """Same scan, same effects, same score, flag or no flag."""
        assert runs[None][1]["duplicate_mutations"] == runs["options.key"][1][
            "duplicate_mutations"
        ] == 0


# -- 3. the seed search ------------------------------------------------------


def _closing() -> FaultConfig:
    opt_in = tuple(
        FaultKind.RESPONSE_LOST_THEN_CLOSED if kind is FaultKind.RESPONSE_LOST else kind
        for kind in OPT_IN_FAULTS
    )
    return FaultConfig.uniform(0.2, (*ALL_FAULTS, *opt_in), seed=1337, close_after_s=5.0)


class TestSuggestSeed:
    def test_it_agrees_with_the_registered_stdlib_derivation(self):
        assert suggest_seed(1337, [("t1", "create_order")], _closing()) == {
            "seed": WALKTHROUGH_SEED, "task_id": "t1", "tool": "create_order",
        }

    def test_the_seed_it_names_carries_the_fault_in_the_schedule(self):
        config = _closing()
        found = suggest_seed(1337, [("t1", "create_order")], config)
        kind = draw_fault(found["seed"], "t1", "create_order", 1,
                          config.total_rate, schedule_kinds(config))
        assert kind is FaultKind.RESPONSE_LOST_THEN_CLOSED
        # And the Gate S seed, from the other side.
        assert draw_fault(254, "t1", "event", 1, config.total_rate,
                          schedule_kinds(config)) is FaultKind.RESPONSE_LOST_THEN_CLOSED

    def test_the_seed_in_use_is_never_suggested(self):
        """It just produced no uncertain task; naming it again is a loop."""
        found = suggest_seed(WALKTHROUGH_SEED, [("t1", "create_order")], _closing())
        assert found is not None and found["seed"] > WALKTHROUGH_SEED

    def test_a_rate_that_never_places_it_is_none(self):
        config = FaultConfig.uniform(0.0001, (*ALL_FAULTS, *OPT_IN_FAULTS))
        assert suggest_seed(1337, [("t1", "create_order")], config) is None

    def test_no_no_reply_kind_is_none(self):
        """Without --allow-mutating there is no lost reply to place."""
        assert suggest_seed(1337, [("t1", "x")], FaultConfig.uniform(0.2)) is None

    def test_an_explicit_schedule_gets_no_suggestion(self):
        assert FaultInjector(schedule={})._schedule == {}


# -- 4. PASS, UNRECONCILED ---------------------------------------------------


def _agent_result(*, lost_acks=0, unsupported=0, duplicate=0, uncertain=1, extra=None):
    metrics = {
        "effect_attribution": "task_window", "effect_oracle_status": "ok",
        "task_oracle_status": {"t1": "ok"}, "uncertain_tasks": uncertain,
        "runs_applied_nothing": 0, "runs_measured": 1,
        "undeclared_task_ids": [], "entries_unreadable_task_ids": [],
        "duplicate_mutations": duplicate,
        "lost_acknowledgements": lost_acks,
        "lost_acknowledgement_tasks": ["t1"] if lost_acks else [],
        "unsupported_claims": unsupported,
        "unsupported_claim_tasks": {"t1": 0} if unsupported else {},
        **(extra or {}),
    }
    behavior = ProbeResult(probe="behavior", phase="behavior", metrics=metrics)
    result = ScanResult(
        target=TargetInfo(name="agent", kind="agent", metadata={
            "coverage_rule": "agent_behavior", "verify_tool": "effects",
        }),
        probes=[behavior],
    )
    return evaluate(result, Policy.default())


class TestUnreconciled:
    def test_a_pass_with_a_lost_acknowledgement_is_unreconciled(self):
        result = _agent_result(lost_acks=1)
        assert result.passed is True and result.score == 100
        lines = verdict_lines(result)
        assert lines[0].startswith(f"{UNRECONCILED}: score 100 meets pass threshold 75")
        assert "lost acknowledgements 1 (t1)" in lines[0]

    def test_an_unsupported_claim_is_named_too(self):
        lines = verdict_lines(_agent_result(unsupported=1))
        assert "unsupported claims 1 (t1)" in lines[0]

    def test_a_clean_pass_is_plain_pass(self):
        """The control: zero readings leave the 1.8.0 verdict untouched."""
        assert verdict_lines(_agent_result())[0].startswith("PASS: score 100")

    def test_a_fail_is_never_relabelled(self):
        result = _agent_result(lost_acks=1, duplicate=1)
        assert result.passed is False
        assert verdict_lines(result)[0].startswith("FAIL")
        assert unreconciled_readings(result) == []

    def test_no_verdict_is_never_relabelled(self):
        result = _agent_result(lost_acks=1, uncertain=0)
        assert result.passed is None
        assert verdict_lines(result)[0].startswith("NO VERDICT")

    def test_any_run_counts_under_repeats(self):
        """The reported run was clean; the second run was not."""
        result = _agent_result(extra={
            "runs_measured": 3,
            "repeat_by_group": [
                {"placement": "a", "runs": 2, "values": {"lost_acknowledgements": [0, 1]}},
                {"placement": "b", "runs": 1, "values": {"lost_acknowledgements": [0]}},
            ],
        })
        assert "lost acknowledgements in 1 of 3 runs" in verdict_lines(result)[0]

    def test_ci_prints_it_and_exits_zero(self, tmp_path):
        """End to end: a lost reply, an agent that gives up and says so."""
        db = _make_db(tmp_path / "app.db")
        out = tmp_path / "out.scan.json"
        result = _run("ci", *_agent_args(
            tmp_path, db, _agent("blind_agent.py", "--max-retries", "0"),
            "--verify-command", _count_command(db), "--json-out", str(out),
            "--seed", str(WALKTHROUGH_SEED),
        ))
        assert result.exit_code == 0, result.output
        assert (
            f"{UNRECONCILED}  score 100.0/100  (policy production-default requires "
            f"75); lost acknowledgements 1 (t1)" in result.output
        )
        data = json.loads(out.read_text())
        assert data["passed"] is True and data["score"] == 100


# -- change 2: the agent's stderr on every exit without a result -------------


def _script(tmp: Path, body: str) -> str:
    path = tmp / "agent.py"
    path.write_text(body)
    return shlex.join([sys.executable, str(path)])


class TestAgentStderr:
    async def test_the_deadline_kill_logs_the_tail(self, tmp_path, caplog):
        agent = _script(tmp_path, (
            "import sys, time\n"
            "print('FIRST-LINE ' + 'x' * 100, file=sys.stderr)\n"
            f"for i in range({2 * STDERR_TAIL_CHARS // 40}):\n"
            "    print('filler ' + 'y' * 40, file=sys.stderr)\n"
            "print('LAST-LINE before the hang', file=sys.stderr, flush=True)\n"
            "time.sleep(60)\n"
        ))
        target = AgentTarget(
            agent_command=agent, tasks_path=_tasks(tmp_path / "tasks.json"),
            upstream=_upstream(tmp_path / "app.db"), work_dir=tmp_path / "work",
            timeout_s=2,
        )
        await target.setup()
        with caplog.at_level(logging.WARNING, logger="ratemyagent.targets.agent"):
            response = await target.invoke(target.sample_request(0))
        assert response.meta["outcome"] == "abandoned"
        logged = "\n".join(r.getMessage() for r in caplog.records
                           if r.levelno == logging.WARNING)
        assert "LAST-LINE before the hang" in logged
        assert "FIRST-LINE" not in logged, "the head was logged, not the tail"
        assert response.meta["stderr_tail"][-1] == "LAST-LINE before the hang"

    async def test_an_exit_without_a_result_logs_and_carries_it(self, tmp_path, caplog):
        agent = _script(tmp_path, (
            "import sys\n"
            "print('Traceback (most recent call last):', file=sys.stderr)\n"
            "print('RuntimeError: the agent broke', file=sys.stderr)\n"
            "sys.exit(3)\n"
        ))
        target = AgentTarget(
            agent_command=agent, tasks_path=_tasks(tmp_path / "tasks.json"),
            upstream=_upstream(tmp_path / "app.db"), work_dir=tmp_path / "work",
        )
        await target.setup()
        with caplog.at_level(logging.WARNING, logger="ratemyagent.targets.agent"):
            response = await target.invoke(target.sample_request(0))
        assert response.meta["outcome"] == "failed"
        assert "RuntimeError: the agent broke" in caplog.text
        assert response.meta["stderr_tail"][-1] == "RuntimeError: the agent broke"

    def test_the_refusal_quotes_the_last_lines(self, tmp_path):
        agent = _script(tmp_path, (
            "import sys\n"
            "print('RuntimeError: the agent broke', file=sys.stderr)\n"
            "sys.exit(3)\n"
        ))
        db = _make_db(tmp_path / "app.db")
        result = _run("scan", *_agent_args(tmp_path, db, agent))
        assert result.exit_code == 2, result.output
        assert "The agent's stderr ended:" in result.output
        assert "RuntimeError: the agent broke" in result.output
