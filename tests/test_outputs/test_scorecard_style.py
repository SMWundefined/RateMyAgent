"""Terminal colour on the scorecard.

Colour is decoration, so every test here is really asking one of two things:
does it stay out of the way when nobody asked for it, and does it leave the
numbers alone when it is on. An escape code in a CI log or a committed report
is the failure mode worth guarding.
"""

from __future__ import annotations

from click.testing import CliRunner

from ratemyagent import __version__, scan
from ratemyagent.cli import cli
from ratemyagent.outputs import render_report, render_scorecard
from ratemyagent.outputs.scorecard import CRITICAL_CHECKS
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget
from tests.conftest import strip_ansi

ESC = "\x1b["


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 20, "warmup": 0, "timeout_s": 5.0, "concurrency": 8,
                "extra": {"fault_rate": 0.3}}
    return ProbeConfig(**{**defaults, **kwargs})


async def scan_mock(target=None, **kwargs):
    return await scan(target or MockTarget.healthy(), config=config(**kwargs))


class TestOptOut:
    """Nothing gets colour unless it asks."""

    async def test_default_render_has_no_escapes(self):
        card = render_scorecard(await scan_mock())

        assert ESC not in card

    async def test_failing_default_render_has_no_escapes(self):
        card = render_scorecard(await scan_mock(MockTarget.failing()))

        assert ESC not in card

    async def test_colour_is_opt_in(self):
        result = await scan_mock(MockTarget.failing())

        assert ESC in render_scorecard(result, color=True)

    def test_cli_output_is_clean_when_piped(self):
        """CliRunner is not a TTY, so click.echo must strip what we styled.

        This is the guarantee that matters: `ratemyagent scan > out.txt` and
        every CI runner take this path.
        """
        result = CliRunner().invoke(
            cli, ["scan", "--target", "mock", "--profile", "failing", "--requests", "10"]
        )

        assert result.exit_code == 0
        assert ESC not in result.output

    def test_ci_command_output_is_clean_when_piped(self):
        result = CliRunner().invoke(
            cli, ["ci", "--target", "mock", "--profile", "failing", "--requests", "10"]
        )

        assert ESC not in result.output

    async def test_markdown_report_never_carries_escapes(self):
        """common.py is shared with the report, so colour must not leak there."""
        assert ESC not in render_report(await scan_mock(MockTarget.failing()))


class TestColourLeavesTheTextAlone:
    """Styling is applied after layout, never before."""

    async def test_colour_only_adds_escapes(self):
        result = await scan_mock(MockTarget.failing())

        assert strip_ansi(render_scorecard(result, color=True)) == render_scorecard(result)

    async def test_table_alignment_survives_colour(self):
        """The status column is styled; the columns before it must not move."""
        result = await scan_mock(MockTarget.failing())

        plain = render_scorecard(result).splitlines()
        painted = [strip_ansi(line) for line in render_scorecard(result, color=True).splitlines()]

        assert plain == painted


class TestMarkers:
    async def test_pass_and_fail_use_green_and_red(self):
        passing = render_scorecard(await scan_mock(), color=True)
        failing = render_scorecard(await scan_mock(MockTarget.failing()), color=True)

        assert "\x1b[32m" in passing  # green somewhere
        assert "\x1b[31m" in failing  # red somewhere

    async def test_verdict_is_bold(self):
        card = render_scorecard(await scan_mock(MockTarget.failing()), color=True)
        verdict = [line for line in card.splitlines() if "FAIL: score" in line][0]

        assert "\x1b[1m" in verdict or ";1m" in verdict

    async def test_hint_is_cyan(self):
        card = render_scorecard(
            await scan_mock(MockTarget.failing()), hint="Run with --output agents-md.", color=True
        )
        hint = [line for line in card.splitlines() if "agents-md" in line][0]

        assert "\x1b[36m" in hint

    async def test_critical_prefix_is_a_text_marker(self):
        """No emojis anywhere -- the marker is a word."""
        result = await scan_mock(MockTarget.failing())
        card = strip_ansi(render_scorecard(result, color=True))

        failed_critical = {
            check.name for check in result.checks
            if check.name in CRITICAL_CHECKS and not check.passed and not check.skipped
        }
        if failed_critical:
            assert "CRITICAL" in card
        assert card.isascii()

    async def test_no_critical_marker_when_nothing_critical_failed(self):
        result = await scan_mock()
        critical_failed = any(
            check.name in CRITICAL_CHECKS and not check.passed and not check.skipped
            for check in result.checks
        )

        if not critical_failed:
            assert "CRITICAL" not in render_scorecard(result)


class TestByline:
    async def test_byline_is_last_and_names_the_version(self):
        card = render_scorecard(await scan_mock())
        last = card.strip().splitlines()[-1]

        assert last == (
            f"ratemyagent v{__version__} - pip install ratemyagent - "
            "github.com/SMWundefined/RateMyAgent"
        )

    async def test_byline_is_dim_when_coloured(self):
        card = render_scorecard(await scan_mock(), color=True)
        last = card.strip().splitlines()[-1]

        assert "\x1b[2m" in last

    async def test_verdict_still_precedes_the_byline(self):
        """The byline may sit below the verdict, but must not bury it."""
        card = render_scorecard(await scan_mock(MockTarget.failing()))
        lines = [line for line in card.strip().splitlines() if line.strip()]

        assert lines[-1].startswith("ratemyagent v")
        assert lines[-2].startswith("Biggest gaps:")
        assert lines[-3].startswith("FAIL: score")
