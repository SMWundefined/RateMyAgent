"""The cancel-scope family, handled as a category, plus the stdio credential path.

Four escapes reached a user before this: 0.1.8 bounded `_close()` and broke task
identity; 0.1.16 fixed the task and broke scope nesting; `a9573f7` caught
`CancelledError` in `setup()`; and `invoke()` then did the same thing from the
fault phase. **Every fix went to the call site that was failing rather than to
the category** -- the same shape as an attribution guard written against one
string when a second was already present.

So: a top-level net first, then the two uncovered sites, and the sites get
*different* policies because the right answer genuinely differs. There is no
single wrapper.

| site | on cancellation |
|---|---|
| `setup()` | convert to `TargetError` -- a scan that never connected is exit 2 |
| `invoke()` | return a failed `Response`, unless the caller asked; then propagate |
| `_close()` | swallow and log, always -- a raise here replaces the real error |
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from ratemyagent.models import ErrorKind, Request
from ratemyagent.targets.base import outer_cancellation_requested
from ratemyagent.targets.mcp import MCPTarget
from tests.support import contains_phrase


class TestTheTopLevelNet:
    """Any escape is exit 2 with one line. The last guarantee of the contract."""

    @staticmethod
    def _run(raiser: str) -> subprocess.CompletedProcess:
        script = textwrap.dedent(f"""
            import asyncio, sys
            from ratemyagent import cli as c
            async def boom(*a, **k): {raiser}
            c.run_scan = boom
            sys.argv = ['ratemyagent', 'scan', '--target', 'mock', '--requests', '5']
            c.main()
        """)
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )

    @pytest.mark.parametrize("raiser", [
        "raise asyncio.CancelledError('scope died')",
        "raise RuntimeError('cancel scope in a different task')",
        "raise BaseException('something nobody enumerated')",
    ])
    def test_an_escape_exits_2_without_a_traceback(self, raiser):
        result = self._run(raiser)

        assert result.returncode == 2, (
            f"exit {result.returncode}. Under the 0/1/2 contract, 1 means the "
            "target failed its policy -- which a scan that never completed was "
            "never measured against."
        )
        assert "Traceback" not in result.stderr, result.stderr[:300]
        assert "the scan did not complete" in result.stderr

    def test_it_says_the_fault_is_ours(self):
        """A user should not go debugging their server over our bug."""
        assert "bug in ratemyagent" in self._run(
            "raise asyncio.CancelledError()"
        ).stderr

    def test_a_deliberate_exit_code_still_passes_through(self):
        """The net must not swallow the codes commands set on purpose."""
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; from ratemyagent import cli as c; "
             "sys.argv=['ratemyagent','scan','--target','mock','--requests','5',"
             "'--probes','nosuchprobe']; c.main()"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "Traceback" not in result.stderr


class TestThePredicateSaysWhatItIs:
    def test_no_outer_request_is_false(self):
        async def check():
            return outer_cancellation_requested()

        assert asyncio.run(check()) is False

    @pytest.mark.skipif(
        not hasattr(asyncio.Task, "cancelling"),
        reason="Task.cancelling() is 3.11+; on 3.10 this is a correlate that "
               "always returns False, which the docstring states",
    )
    def test_an_outer_request_is_seen(self):
        async def check():
            asyncio.current_task().cancel()
            seen = outer_cancellation_requested()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            return seen

        assert asyncio.run(check()) is True

    def test_it_does_not_raise_off_a_loop(self):
        assert outer_cancellation_requested() is False


class TestInvokeReturnsRatherThanRaising:
    """Site 7. `invoke()`'s contract is to return; a raise ends the phase."""

    class _CancellingSession:
        """A session whose `call_tool` is cancelled by a scope we do not own."""

        async def call_tool(self, name, arguments):
            raise asyncio.CancelledError()

    def _target(self) -> MCPTarget:
        target = MCPTarget("stdio://echo hi")
        target._session = self._CancellingSession()
        return target

    async def test_a_session_cancellation_becomes_a_failed_response(self):
        response = await self._target().invoke(Request(op="t", payload={}))

        assert response.ok is False
        assert response.error_kind is ErrorKind.TIMEOUT
        assert response.delivered is False

    async def test_the_callers_cancellation_still_propagates(self, monkeypatch):
        """Or the scan deadline and Ctrl-C stop working."""
        monkeypatch.setattr(
            "ratemyagent.targets.mcp.outer_cancellation_requested", lambda: True
        )
        with pytest.raises(asyncio.CancelledError):
            await self._target().invoke(Request(op="t", payload={}))


class TestCloseSwallowsCancellation:
    """Site 8, and the one nobody had looked at.

    `_close()` runs from `teardown()`, which runs from `finally` blocks. A raise
    here replaces whatever the caller was already handling, so a scan that
    failed for a real reason would report the cancellation instead.
    """

    async def test_it_returns_rather_than_propagating(self):
        class _CancellingStack:
            async def aclose(self):
                raise asyncio.CancelledError()

        target = MCPTarget("stdio://echo hi")
        await target._close(_CancellingStack())  # must not raise

    async def test_a_structural_error_is_still_never_swallowed(self):
        """`RuntimeError` stays loud: it is a defect in *our* nesting."""
        class _BrokenStack:
            async def aclose(self):
                raise RuntimeError("Attempted to exit a cancel scope ...")

        target = MCPTarget("stdio://echo hi")
        with pytest.raises(RuntimeError):
            await target._close(_BrokenStack())


class TestEnvIsTheOnlyPathToAStdioCredential:
    """`--env` did not exist, and the parent environment is not inherited.

    The SDK copies six variables into a stdio child and drops everything else,
    so a credential exported in the shell reaches nothing. Every stdio server
    needing a key had been scanned unauthenticated; nothing in the corpus needed
    one until firecrawl.
    """

    def test_the_flag_exists_and_parses(self):
        from ratemyagent.cli import _parse_env

        assert _parse_env(("A=1", "B=two=parts")) == {"A": "1", "B": "two=parts"}
        assert _parse_env(()) is None

    @pytest.mark.parametrize("bad", ["novalue", "=novalue"])
    def test_a_malformed_pair_is_refused(self, bad):
        import click

        from ratemyagent.cli import _parse_env

        with pytest.raises(click.BadParameter):
            _parse_env((bad,))

    def test_values_are_redacted_like_headers(self):
        target = MCPTarget("stdio://echo hi", env={"FIRECRAWL_API_KEY": "sk-secret"})
        rendered = str(target.describe().to_dict())

        assert "sk-secret" not in rendered
        assert "FIRECRAWL_API_KEY" in rendered, (
            "which variables were set is provenance worth keeping -- it answers "
            "'was this scan authenticated' when a report is read back later"
        )

    def test_the_help_names_what_is_inherited(self):
        """A user cannot guess a six-variable allowlist."""
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        help_text = CliRunner().invoke(cli, ["scan", "--help"]).output

        # Phrase-matched: click rewraps help to the terminal width, so the
        # phrase arrives split across lines. Exactly the wrap that made a
        # banned-phrase gate read green for twenty releases.
        assert contains_phrase(help_text, "THE PARENT ENVIRONMENT IS NOT INHERITED")
        for name in ("HOME", "PATH", "SHELL", "TERM", "USER"):
            assert name in help_text

    def test_there_is_no_env_from_flag(self):
        """Forwarding by name is one keystroke from forwarding by pattern."""
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        assert "--env-from" not in CliRunner().invoke(cli, ["scan", "--help"]).output


class TestADegradedServerIsCalledOut:
    """The server spoke on the way up and answered anyway.

    Third instance: `pypi-query-mcp-server` scored 98/100 on the wrong code
    path, `htag` reported strictness over tools it was not exercising, and
    `firecrawl-mcp` prints a keyless banner then serves 25 tools instead of 27.
    None of those is visible in any number the scan produces.
    """

    @staticmethod
    def _uri(*extra: str) -> str:
        import shlex

        return "stdio://" + shlex.join(
            [sys.executable, "tests/fixtures/strict_mcp_server.py", *extra]
        )

    async def _caveats(self, *extra: str):
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.probes.latency import LatencyProfiler

        target = MCPTarget(
            self._uri(*extra), tool="get_page",
            tool_args={"url": "https://example.com"}, timeout_s=20,
        )
        await target.setup()
        try:
            result = await LatencyProfiler().execute(
                target, ProbeConfig(requests=5, warmup=0)
            )
        finally:
            await target.teardown()
        return result.caveats

    async def test_stderr_plus_a_working_baseline_is_flagged(self):
        caveats = await self._caveats("--degrade")
        degraded = [c for c in caveats if "degraded path" in c.reason]

        assert degraded, "a server that announced a reduced mode went unmentioned"
        assert degraded[0].effect == "annotate", "a caveat, never a failure"
        assert degraded[0].scope == "probe"
        assert "keyless mode" in degraded[0].reason, (
            "quote the server's own words; paraphrasing loses the thing a user "
            "would recognise"
        )

    async def test_a_quiet_server_is_not_flagged(self):
        caveats = await self._caveats()
        assert not [c for c in caveats if "degraded path" in c.reason]

    async def test_a_dead_baseline_is_not_a_degraded_path(self, monkeypatch):
        """Both halves of the rule matter, and this is the second one.

        Output with a *failed* baseline is just the error, already reported by
        every other line in the scan. The case worth flagging is output with a
        *working* baseline -- where nothing else will ever mention it. Without
        this test the guard can be deleted and nothing notices.
        """
        from ratemyagent.probes.latency import _degraded_path_caveats

        class _Noisy:
            setup_stderr = "No API key set - running in keyless mode."

        assert _degraded_path_caveats(_Noisy(), {"error_rate": 0.0})
        assert not _degraded_path_caveats(_Noisy(), {"error_rate": 1.0}), (
            "every request failed, so the error is the finding; a second "
            "caveat about stderr is noise on top of it"
        )

    async def test_the_target_exposes_what_was_said(self):
        target = MCPTarget(
            self._uri("--degrade"), tool="get_page",
            tool_args={"url": "https://example.com"}, timeout_s=20,
        )
        await target.setup()
        try:
            assert "keyless mode" in target.setup_stderr
        finally:
            await target.teardown()

    def test_a_network_target_has_no_stderr_channel(self):
        assert MCPTarget("https://example.com/mcp").setup_stderr == ""
