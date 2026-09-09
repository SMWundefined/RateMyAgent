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
        wrong = [
            c for c in result.metrics["cases"]
            if c["wrongly_accepted"] and c["tool"] == "git_show"
        ]

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
            and c["tool"] == "git_show"
        }

        assert not blamed

    @pytest.mark.asyncio
    async def test_the_finding_names_the_argument(self):
        result = await self._scan()
        joined = " ".join(result.findings)

        assert "revision" in joined, "the finding does not say which argument"


class TestBaselineRejectionDoesNotSwallowRealFindings:
    """The finding the per-case rule must not discard.

    `git_log` on the stub rejects the synthesized `repo_path` placeholder -- it
    is not a real path -- and *accepts* a null in that same field. A per-tool
    rule would drop every case from this tool because its baseline was rejected,
    taking a genuine unvalidated null with it. The per-case rule keeps it,
    because `null_required[repo_path]` replaces the placeholder rather than
    carrying it.

    That asymmetry is only expressible because 0.1.19 generates cases per field.
    Before it, every case carried the whole baseline and per-tool was the only
    available rule.
    """

    async def _scan(self):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.contract import ContractTester
        from ratemyagent.targets import MCPTarget

        server = shlex.join([sys.executable, str(STUB), "--repository", "/tmp"])
        target = MCPTarget(f"stdio://{server}", timeout_s=30)
        await target.setup()
        try:
            return await ContractTester().execute(
                target, ProbeConfig(requests=1, extra={"contract_tool_limit": 4})
            )
        finally:
            await target.teardown()

    @pytest.mark.asyncio
    async def test_the_baseline_rejection_is_recorded(self):
        result = await self._scan()

        assert "git_log" in result.metrics["tools_rejecting_baseline"]

    @pytest.mark.asyncio
    async def test_the_accepted_null_is_still_counted(self):
        """The half that matters. A rule that suppresses this is worse than no
        rule: it hides an unvalidated field behind a bad placeholder."""
        result = await self._scan()
        kept = [
            c for c in result.metrics["cases"]
            if c["tool"] == "git_log" and c["wrongly_accepted"]
        ]

        assert kept, "the accepted null was discarded with the baseline"
        assert result.metrics["accepted_invalid"] > 0
        assert not kept[0]["carries_baseline"]

    @pytest.mark.asyncio
    async def test_only_contaminated_cases_are_dropped(self):
        """`git_log` declares one required field and no optional ones, so every
        scoreable case replaces the placeholder and nothing is excluded. The
        drops come from `git_show`, whose second required field keeps the first
        one's rejected value. Precision, not blanket suppression."""
        result = await self._scan()

        assert result.metrics["cases_unattributable"] > 0
        dropped_from_git_log = [
            c for c in result.metrics["cases"]
            if c["tool"] == "git_log" and c["carries_baseline"] and c["should_reject"]
        ]
        assert not dropped_from_git_log, (
            "a single-required-field tool should lose nothing to this rule"
        )

    @pytest.mark.asyncio
    async def test_the_caveat_names_the_tool(self):
        result = await self._scan()
        reasons = " ".join(c.reason for c in result.caveats)

        assert "git_log" in reasons
        assert "rejected its own well-formed baseline" in reasons


class TestTwoTargetsInOneEventLoop:
    """The gate that would have caught 0.1.16 being wrong.

    `scan()` and `MCPTarget` are public API, so scanning two servers from one
    process is a supported thing to do -- and it was broken from 0.1.16 until
    0.1.20 on Python 3.12 and 3.13. Nothing caught it because every test and the
    entire CLI open exactly one target per process.

    Two fixes traded one violation of the same invariant for another. 0.1.8
    bounded the teardown with `asyncio.wait_for`, which ran `aclose()` in a new
    *task* on 3.10 and 3.11. 0.1.16 replaced it with `anyio.move_on_after`,
    which fixed the task and broke the *nesting* -- wrapping an unwind in a
    fresh scope means exiting an outer scope from inside a newer inner one. The
    invariant both violated is the same one: unwind a scope in the structure
    that created it.

    A broad `except Exception` in `_close` swallowed the resulting RuntimeError,
    so the first teardown reported success and the corrupted scope state
    surfaced later as an unrelated `CancelledError` in the *next* setup.
    """

    async def _session(self, index: int) -> int:
        from ratemyagent.targets import MCPTarget

        server = shlex.join([sys.executable, str(STUB), "--repository", "/tmp"])
        target = MCPTarget(
            f"stdio://{server}",
            tool="git_status",
            tool_args={"repo_path": "/tmp"},
            timeout_s=30,
        )
        await target.setup()
        try:
            return len(target.list_tools())
        finally:
            await target.teardown()

    @pytest.mark.asyncio
    async def test_three_sequential_targets_in_one_loop(self):
        counts = [await self._session(n) for n in range(3)]

        assert counts == [4, 4, 4], (
            "a later setup was cancelled by scope state the previous teardown "
            "left behind"
        )

    @pytest.mark.asyncio
    async def test_a_target_survives_a_previous_one_being_torn_down(self):
        """Narrower than the loop above: it is specifically the *second* setup
        that failed, and it failed inside `stdio_client`, nowhere near the
        teardown that caused it."""
        assert await self._session(1)
        assert await self._session(2)

    @pytest.mark.asyncio
    async def test_cancel_scope_misuse_is_not_swallowed(self):
        """`_close` catches server shutdown noise and must keep doing so, but a
        RuntimeError from cancel-scope misuse is a defect in our own structure.
        Swallowing it is what made the 0.1.16 regression invisible."""
        from contextlib import AsyncExitStack

        from ratemyagent.targets import MCPTarget

        class MisusedStack(AsyncExitStack):
            async def aclose(self):
                raise RuntimeError(
                    "Attempted to exit a cancel scope that isn't the current "
                    "task's current cancel scope"
                )

        class NoisyStack(AsyncExitStack):
            async def aclose(self):
                raise OSError("server closed the pipe untidily")

        target = MCPTarget("stdio://./server.py")

        with pytest.raises(RuntimeError):
            await target._close(MisusedStack())

        await target._close(NoisyStack())  # genuine noise, still swallowed
