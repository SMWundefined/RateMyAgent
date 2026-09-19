"""The interpreter path an agent scan writes down.

`AgentTarget.proxy_command` defaults to `sys.executable`, and an agent scan is
run from a virtualenv, so before 1.6.1 every `--json-out` from this path carried
an absolute path through the user's home directory. That file is the one people
paste into issues.

The property under test is not "the string changed". It is the pair: **no home
directory reaches the export, and everything that says what ran is still
legible.** A redaction that took the whole command out would pass the first half
and destroy the reason the field exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent.models import FaultKind
from ratemyagent.policy import Policy
from ratemyagent.probes.agent_baseline import AgentBaseline
from ratemyagent.probes.behavior import BehaviorAnalyzer
from ratemyagent.probes.fault import FaultInjector
from ratemyagent.scanner import scan
from ratemyagent.targets import AgentTarget
from ratemyagent.targets.base import REDACTED, redact_command, redact_env, redact_headers

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "tests" / "fixtures" / "agents"
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TASKS = AGENTS / "tasks.json"


def _agent(name: str) -> str:
    return shlex.join([sys.executable, str(AGENTS / name)])


def _upstream(tmp_path: Path) -> str:
    return "stdio://" + shlex.join([
        sys.executable, str(TWIN), "--mode", "append",
        "--state", str(tmp_path / "state.jsonl"),
    ])


class TestAScansExport:
    """The end-to-end arm: a real scan, and the document `--json-out` writes.

    **Everything the user names is named relatively here, deliberately.** A
    `--agent` command, a `--tasks` path and a `stdio://` upstream are written
    down exactly as typed -- that is the 1.5.1 rule, and `TestWhatIsStillWritten
    AsGiven` below asserts it rather than leaving it to prose. So a run whose
    own arguments carry no home directory is the only run in which "the export
    carries no home directory" tests *our* path and not the caller's.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def export(cls, tmp_path_factory):
        work = tmp_path_factory.mktemp("export")
        relative = Path("tests") / "fixtures"

        async def run():
            target = AgentTarget(
                agent_command=shlex.join(
                    ["python3", str(relative / "agents" / "careful_agent.py")]
                ),
                tasks_path=relative / "agents" / "tasks.json",
                upstream="stdio://" + shlex.join([
                    "python3", str(relative / "event_twin_mcp_server.py"),
                    "--mode", "append", "--state", str(work / "state.jsonl"),
                ]),
                work_dir=work / "work",
                allow_mutating=True,
                verify_tool="effects",
                verify_count="entries",
            )
            result = await scan(
                target,
                probes=[
                    AgentBaseline(),
                    FaultInjector(schedule={("t1", "event", 1): FaultKind.RESPONSE_LOST}),
                    BehaviorAnalyzer(),
                ],
                policy=Policy.default(),
            )
            # Exactly what `_write_json` writes for `--json-out`.
            return json.dumps(result.to_dict(), indent=2), result

        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            return asyncio.run(run())
        finally:
            os.chdir(previous)

    def test_it_carries_no_home_directory(self, export):
        """The leak, stated as the thing a user would find in their issue."""
        document, _ = export
        home = str(Path.home())
        assert home not in document, (
            f"{home} reached the export; grep it for the surrounding key"
        )

    def test_the_interpreter_is_still_identified_by_name(self, export):
        """Not a blanket removal: the export still says which Python ran."""
        _, result = export
        written = result.target.metadata["proxy_command"]
        assert written.startswith(f"{REDACTED}/")
        assert Path(sys.executable).name in written

    def test_the_module_invocation_stays_legible(self, export):
        """What identifies the proxy is the point of the field, and survives."""
        _, result = export
        assert result.target.metadata["proxy_command"].endswith(
            "-m ratemyagent.cli proxy"
        )


class TestTheRule:
    """Which executables are rewritten, and which are written down as given."""

    def test_a_private_absolute_path_loses_its_directory_only(self):
        written = redact_command(
            ["/Users/someone/src/app/.venv/bin/python", "-m", "ratemyagent.cli", "proxy"]
        )
        assert written == f"{REDACTED}/python -m ratemyagent.cli proxy"

    @pytest.mark.parametrize("executable", [
        "/usr/bin/python3",
        "/usr/local/bin/python3.11",
        "/opt/homebrew/bin/python3.12",
        "/bin/sh",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    ])
    def test_a_system_wide_interpreter_is_unchanged(self, executable):
        """No home directory sits under these, and the full path is a fact
        about the machine's software that a reader may want."""
        assert redact_command([executable, "-m", "x"]) == f"{executable} -m x"

    @pytest.mark.parametrize("executable", [
        "python3",
        "./venv/bin/python",
        "../tools/python3.12",
        "venv/bin/python",
    ])
    def test_a_relative_or_bare_interpreter_is_unchanged(self, executable):
        assert redact_command([executable, "-m", "x"]) == f"{executable} -m x"

    def test_a_windows_user_path_is_redacted_too(self):
        written = redact_command([r"C:\Users\someone\venv\Scripts\python.exe", "-m", "x"])
        assert written == f"{REDACTED}\\python.exe -m x"

    def test_only_the_executable_is_touched(self):
        """A flag that happens to be a private path is an argument the caller
        chose, and arguments are written down as given."""
        written = redact_command(["/usr/bin/python3", "--state", "/Users/someone/db"])
        assert written == "/usr/bin/python3 --state /Users/someone/db"

    def test_an_empty_command_is_an_empty_string(self):
        assert redact_command([]) == ""
        assert redact_command(None) == ""


class TestWhatIsStillWrittenAsGiven:
    """The residual, asserted rather than described.

    **This is the 1.5.1 rule, not an oversight.** A `stdio://` URI is exempt
    from `redact_uri` because what follows the scheme is a command line, and
    rewriting it invented a secret and made a pinned version unreadable in the
    artifacts that exist to say what was scanned. `--agent`, `--tasks` and
    `--work-dir` are the same kind of string: the user typed them, and the
    scorecard prints the work directory so they can find their records.

    So an agent scan launched with absolute paths still writes those paths down,
    and a user who wants an export with no home directory in it names their
    arguments relatively. What 1.6.1 fixes is the path *this code inserted* on
    its own, which no argument of theirs could have kept out.
    """

    async def test_the_agent_command_is_written_down_as_typed(self, tmp_path):
        target = AgentTarget(
            agent_command=_agent("careful_agent.py"),
            tasks_path=TASKS,
            upstream=_upstream(tmp_path),
            work_dir=tmp_path / "work",
            allow_mutating=True,
        )
        await target.setup()
        info = target.describe()
        assert info.metadata["agent_command"] == _agent("careful_agent.py")
        assert str(TASKS) == info.metadata["tasks_path"]
        # And the one we inserted is the one that is not.
        assert info.metadata["proxy_command"].startswith(f"{REDACTED}/")


class TestTheOtherRedactionsAreUntouched:
    """`--header` and `--env` are a different rule for a different reason:
    names in, values out. Nothing here changes either."""

    def test_header_values_are_still_replaced_and_names_kept(self):
        assert redact_headers({"Authorization": "Bearer sk-live-1", "X-Api-Key": "k"}) == {
            "Authorization": REDACTED,
            "X-Api-Key": REDACTED,
        }

    def test_env_values_are_still_replaced_and_names_kept(self):
        assert redact_env({"FIRECRAWL_API_KEY": "fc-1", "HOME": "/Users/someone"}) == {
            "FIRECRAWL_API_KEY": REDACTED,
            "HOME": REDACTED,
        }
