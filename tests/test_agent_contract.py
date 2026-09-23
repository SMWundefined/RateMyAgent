"""The launch contract: `--agent-command`, `--claim-path`, `--work-dir`.

1.5.1 built one fixed argv, `CMD --mcp-config P --tasks F --task ID`, and read
the claim off the last JSON line. The Phase D spike found that the first half of
that cannot launch any hosted agent CLI -- Claude Code rejects unknown flags
before the run starts, so `--tasks` kills the launch -- and had to put a shim in
front of it. These three flags are what remove the shim.

The default of each is 1.5.1's behaviour exactly, and there is an arm for that,
because a default is the configuration nobody chose and therefore the one no
fixture author reaches for (CLAUDE.md, and PROGRESS section 8b entry 27).
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.targets.agent import (
    DEFAULT_AGENT_ARGV,
    REQUIRED_TEMPLATE_FIELDS,
    TEMPLATE_FIELDS,
    AgentTarget,
    _check_template,
    _claim_at,
    _parse_claim,
    _render_argv,
)
from ratemyagent.targets.base import TargetError

FIXTURES = Path(__file__).parent / "fixtures"
AGENTS = FIXTURES / "agents"
TWIN = FIXTURES / "event_twin_mcp_server.py"


def _upstream(tmp_path: Path) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append", "--role", "{role}",
        "--state", str(tmp_path / "state.jsonl"),
        "--calls", str(tmp_path / "calls.jsonl"),
    ])


# -- the template ------------------------------------------------------------


class TestTheDefaultIsWhat1_5_1_Built:
    def test_the_default_template_renders_the_shipped_argv(self):
        """The default-flags arm. Byte for byte, the 1.5.1 command line."""
        assert _render_argv(
            DEFAULT_AGENT_ARGV,
            config="/w/mcp-chaos-t1.json", prompt="ignored",
            task_id="t1", tasks="/w/tasks.json",
        ) == ["--mcp-config", "/w/mcp-chaos-t1.json", "--tasks", "/w/tasks.json",
              "--task", "t1"]

    def test_a_target_with_no_template_keeps_the_default(self, tmp_path):
        target = AgentTarget(
            agent_command="python agent.py",
            tasks_path=AGENTS / "tasks.json",
            upstream=_upstream(tmp_path),
            work_dir=tmp_path / "w",
        )
        assert target.agent_argv is None
        assert target.claim_path is None

    def test_the_default_is_exempt_from_the_placeholder_rule(self):
        """It carries `{tasks}` rather than `{prompt}`, and that is correct.

        The three fixtures read the prompt out of the task file themselves,
        which is the arrangement the default exists to preserve. The rule is
        about templates a user wrote, so `None` is checked and passes.
        """
        assert "{prompt}" not in DEFAULT_AGENT_ARGV
        _check_template(None)  # does not raise


class TestASuppliedTemplateIsChecked:
    @pytest.mark.parametrize("template,missing", [
        ("--mcp-config {config}", "{prompt}"),
        ("-p {prompt}", "{config}"),
        ("--model haiku", "{config}"),
    ])
    def test_a_missing_placeholder_is_named(self, template, missing):
        with pytest.raises(TargetError) as exc:
            _check_template(template)
        assert missing in str(exc.value)
        assert "--agent-command" in str(exc.value)

    def test_both_missing_are_both_named(self):
        with pytest.raises(TargetError) as exc:
            _check_template("--model haiku")
        assert "{config}" in str(exc.value) and "{prompt}" in str(exc.value)

    def test_an_unfillable_placeholder_is_refused(self):
        """A typo is a refusal, not a literal brace on the command line."""
        with pytest.raises(TargetError) as exc:
            _check_template("-p {prompt} --mcp-config {config} --seed {seed}")
        assert "{seed}" in str(exc.value)
        assert all(field in str(exc.value) for field in ("{config}", "{prompt}"))

    def test_the_required_set_is_the_documented_one(self):
        assert REQUIRED_TEMPLATE_FIELDS == ("config", "prompt")
        assert set(REQUIRED_TEMPLATE_FIELDS) <= set(TEMPLATE_FIELDS)

    @pytest.mark.asyncio
    async def test_setup_refuses_before_anything_runs(self, tmp_path):
        """At setup, so a bad template costs no agent run and no upstream."""
        target = AgentTarget(
            agent_command="claude",
            tasks_path=AGENTS / "tasks.json",
            upstream=_upstream(tmp_path),
            work_dir=tmp_path / "w",
            agent_argv="-p {prompt} --output-format json",
        )
        with pytest.raises(TargetError, match=r"\{config\}"):
            await target.setup()


class TestRendering:
    def test_a_prompt_with_spaces_stays_one_argument(self):
        """Split first, substitute second. The whole reason for the order.

        Substituting into the string and splitting afterwards would turn a
        sentence into six arguments, and the agent would be launched with a
        prompt it never sees.
        """
        argv = _render_argv(
            "-p {prompt} --mcp-config {config}",
            config="/w/c.json", prompt="Record the event 'alpha' exactly once.",
            task_id="t1", tasks="/w/t.json",
        )
        assert argv == ["-p", "Record the event 'alpha' exactly once.",
                        "--mcp-config", "/w/c.json"]

    def test_a_placeholder_inside_a_larger_token_is_substituted(self):
        argv = _render_argv(
            "--record={task_id}.jsonl",
            config="c", prompt="p", task_id="t7", tasks="t",
        )
        assert argv == ["--record=t7.jsonl"]

    def test_the_spike_template_renders(self):
        """The command the spike needed a shim for, now expressible directly."""
        argv = _render_argv(
            "-p {prompt} --model claude-haiku-4-5 --mcp-config {config} "
            "--strict-mcp-config --output-format json",
            config="/w/mcp-chaos-t1.json",
            prompt="Record an event with id 'alpha'.",
            task_id="t1", tasks="/w/tasks.json",
        )
        assert argv[:2] == ["-p", "Record an event with id 'alpha'."]
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--mcp-config") + 1] == "/w/mcp-chaos-t1.json"
        assert "--tasks" not in argv, "the flag that killed the launch is gone"


# -- the claim ---------------------------------------------------------------


class TestTheClaimPath:
    def test_the_default_is_still_the_last_json_line(self):
        """1.5.1's rule, unchanged: the *last* object carrying `ok`."""
        stdout = (
            'starting\n{"ok": false, "note": "first attempt"}\n'
            'progress\n{"ok": true, "result": "done"}\n'
        )
        assert _parse_claim(stdout) == {"ok": True, "result": "done"}

    def test_a_pointer_reads_a_single_document(self):
        """What `claude -p --output-format json --json-schema` prints."""
        stdout = json.dumps({
            "type": "result", "is_error": False, "session_id": "abc",
            "total_cost_usd": 0.04,
            "structured_output": {"ok": True, "detail": "recorded alpha once"},
        })
        assert _claim_at(stdout, "structured_output") == {
            "ok": True, "detail": "recorded alpha once"
        }

    def test_the_pointer_nests(self):
        stdout = json.dumps({"a": {"b": {"ok": False, "error": "nope"}}})
        assert _claim_at(stdout, "a.b")["error"] == "nope"

    @pytest.mark.parametrize("stdout,needle", [
        ("", "printed nothing"),
        ("not json at all", "one JSON document"),
        ('{"result": "text"}', "no 'structured_output'"),
        ('{"structured_output": "a string"}', "str"),
        ('{"structured_output": {"detail": "no ok key"}}', 'no "ok" key'),
    ])
    def test_a_malformed_claim_refuses(self, stdout, needle):
        """**Refuses, never reports a failed task.**

        Returning `ok=False` here would record a misconfiguration as an agent
        that tried and failed -- a measurement invented out of a broken flag,
        which is the shape section 8b keeps cataloguing.
        """
        with pytest.raises(TargetError) as exc:
            _claim_at(stdout, "structured_output")
        assert needle in str(exc.value)

    def test_the_refusal_names_what_was_there(self):
        """So the fix is visible without re-running the agent."""
        with pytest.raises(TargetError) as exc:
            _claim_at(json.dumps({"result": "t", "session_id": "s"}), "structured_output")
        assert "result" in str(exc.value) and "session_id" in str(exc.value)

    def test_a_pointer_does_not_fall_back_to_the_last_line(self):
        """Two rules at once would make a typo silently succeed."""
        stdout = '{"ok": true, "result": "top level"}'
        with pytest.raises(TargetError):
            _claim_at(stdout, "structured_output")


# -- the work directory ------------------------------------------------------


class TestTheWorkDirectory:
    @pytest.mark.asyncio
    async def test_a_named_directory_is_used_and_created(self, tmp_path):
        chosen = tmp_path / "keep" / "here"
        target = AgentTarget(
            agent_command="python agent.py",
            tasks_path=AGENTS / "tasks.json",
            upstream=_upstream(tmp_path),
            work_dir=chosen,
        )
        await target.setup()
        assert target.work_dir == chosen
        assert chosen.is_dir()
        # The schedule the baseline pass writes lands there, not in temp.
        assert (chosen / "schedule-run.json").exists()

    @pytest.mark.asyncio
    async def test_the_default_is_unchanged(self, tmp_path):
        """The default-flags arm: still a fresh directory under the system temp.

        Unchanged on purpose. It is also why the flag exists -- that directory
        is wiped on restart, and the records are the evidence -- but changing
        the default would move every existing scan's artifacts.
        """
        import tempfile

        target = AgentTarget(
            agent_command="python agent.py",
            tasks_path=AGENTS / "tasks.json",
            upstream=_upstream(tmp_path),
        )
        await target.setup()
        assert target.work_dir.is_dir()
        assert str(target.work_dir).startswith(tempfile.gettempdir())
        assert "ratemyagent-agent-" in target.work_dir.name

    @pytest.mark.asyncio
    async def test_where_it_went_is_reported(self, tmp_path):
        """The report has to say where, or the records are not findable."""
        chosen = tmp_path / "named"
        target = AgentTarget(
            agent_command="python agent.py",
            tasks_path=AGENTS / "tasks.json",
            upstream=_upstream(tmp_path),
            work_dir=chosen,
        )
        await target.setup()
        assert target.describe().metadata["work_dir"] == str(chosen)


# -- the CLI -----------------------------------------------------------------


class TestTheFlagsAreAgentOnly:
    """Refused on any other target, the way `--agent` and `--tasks` already are.

    Silently dropping a flag is the `--header`-on-stdio failure: the user
    believes they configured the scan and nothing says otherwise.
    """

    @pytest.mark.parametrize("flag,value", [
        ("--agent-command", "-p {prompt} --mcp-config {config}"),
        ("--claim-path", "structured_output"),
        ("--work-dir", "/tmp/somewhere"),
        ("--lost-reply-close-after", "3"),
    ])
    @pytest.mark.parametrize("command", ["scan", "ci"])
    def test_refused_on_a_mock_target(self, flag, value, command):
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        result = CliRunner().invoke(
            cli, [command, "--target", "mock", flag, value]
        )
        assert result.exit_code != 0
        assert flag in result.output
        assert "--target agent only" in result.output

    def test_the_bare_flag_uses_the_default_hold(self):
        """`--lost-reply-close-after` with no value is still the flag."""
        from ratemyagent.targets.fault_proxy import DEFAULT_CLOSE_AFTER_S

        assert DEFAULT_CLOSE_AFTER_S == 5.0
