"""The checkers get checked.

Standing rule, adopted in 0.1.5: anything that exists to verify something else
runs in CI. Five bugs in this project were a correction or a check that was
itself unchecked -- a substring table with an unknown bucket, a getattr with a
default, a grep gate that counted markers instead of artifacts, correction prose
that outlived its condition, and a verification script broken under the SDK its
own instructions install. A guard nobody runs is worse than no guard, because it
converts "unknown" into "verified".

`examples/mcp_server_git_repro.py` is the one users are pointed at to check a
crash report, including a crash report from us. It is exercised here against a
stdlib stub so the run needs no network, no uvx and no real server.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "mcp_server_git_repro.py"
STUB = ROOT / "tests" / "fixtures" / "stub_mcp_server.py"


def run_repro(*extra: str) -> subprocess.CompletedProcess[str]:
    # --server is one string that the script shlex-splits, so every component
    # needs quoting: this repo lives under "Silicon Valley" and so does its
    # interpreter, and an unquoted space silently becomes two argv entries.
    server = shlex.join([sys.executable, str(STUB)])
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--server", server, *extra],
        capture_output=True, text=True, timeout=180,
    )


class TestCrashRepro:
    def test_it_reports_no_crash_against_a_server_that_does_not_crash(self):
        """Exit 0 is the claim: the stub never crashes, so neither may the report."""
        proc = run_repro()

        assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        assert "18/18 malformed calls answered" in proc.stdout
        assert "did not crash" in proc.stdout

    def test_it_reads_the_rejections_rather_than_skipping_them(self):
        """A silent pass would also print no crashes. Check it saw the traffic."""
        proc = run_repro()

        assert proc.stdout.count("rejected") >= 18, proc.stdout
        # Both rejection vocabularies must appear: the one an error-text
        # classifier recognises, and the one it does not.
        assert "Input validation error" in proc.stdout
        assert "outside the allowed repository" in proc.stdout

    def test_a_missing_sdk_attribute_raises_rather_than_passing(self):
        """The near-miss worth pinning.

        `sdk_attr` must not take a default. With one, an unknown SDK shape makes
        every result read as not-an-error, every malformed input reads as
        accepted, and the script exits 0 -- a clean pass from the tool whose job
        is catching clean passes that should not be.
        """
        sys.path.insert(0, str(ROOT / "examples"))
        try:
            import mcp_server_git_repro as repro
        finally:
            sys.path.pop(0)

        with pytest.raises(AttributeError, match="unsupported mcp SDK"):
            repro.sdk_attr(object(), "is_error", "isError")

    def test_the_stub_answers_every_edge_case_without_dying(self):
        """If the fixture itself starts crashing, the test above passes for the
        wrong reason -- it would be measuring a dead stub, not a live one."""
        proc = run_repro()

        assert "session still usable at the end" in proc.stdout
        assert "CRASH" not in proc.stdout
        assert "DEGRADED" not in proc.stdout


class TestMultiFieldCoverageEndToEnd:
    """The deterministic gate for 0.1.15, against a real stdio server.

    `git_show` on the stub declares `repo_path` and `revision` required and
    validates only the first. That is the shape no server in the regression set
    could produce -- every tool in every contract window on all eight surveyed
    endpoints has zero or one required field -- so this is the only place the fix
    can be shown to land without a live third-party target.

    The unit tests prove the payloads are generated. This proves the probe sends
    them, the classifier reads them, and an unvalidated second field surfaces as
    an accepted violation rather than as nothing at all.
    """

    async def _scan(self):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.contract import ContractTester
        from ratemyagent.targets import MCPTarget

        server = shlex.join([sys.executable, str(STUB), "--repository", "/tmp"])
        # Real arguments, because without them this proves nothing: the stub
        # validates `repo_path`, so a synthesized placeholder is rejected on the
        # first field and the unchecked second field never gets a chance to be
        # accepted. 0.1.14 had to land before 0.1.15 could be demonstrated at
        # all, which is the clearest argument the ordering was right.
        target = MCPTarget(
            f"stdio://{server}",
            tool="git_show",
            tool_args={"repo_path": "/tmp", "revision": "HEAD"},
            timeout_s=30,
        )
        await target.setup()
        try:
            return await ContractTester().execute(target, ProbeConfig(requests=1))
        finally:
            await target.teardown()

    @pytest.mark.asyncio
    async def test_the_unvalidated_second_field_is_found(self):
        result = await self._scan()
        wrong = [c for c in result.metrics["cases"] if c["wrongly_accepted"]]

        assert wrong, "a declared-and-unchecked required field was not detected"
        assert {c["field"] for c in wrong} == {"revision"}, (
            "the accepted violations should localise to the unchecked field"
        )
        assert result.metrics["accepted_invalid"] > 0

    @pytest.mark.asyncio
    async def test_the_validated_field_is_not_blamed(self):
        """`repo_path` is checked, so nothing on it may be reported as accepted.
        A probe that corrupted both fields at once could not tell them apart."""
        result = await self._scan()
        blamed = {
            c["field"] for c in result.metrics["cases"]
            if c["wrongly_accepted"] and c["field"] == "repo_path"
        }

        assert not blamed

    @pytest.mark.asyncio
    async def test_the_finding_names_the_argument(self):
        result = await self._scan()
        joined = " ".join(result.findings)

        assert "revision" in joined, "the finding does not say which argument"
