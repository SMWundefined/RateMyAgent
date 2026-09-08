"""Markdown report and terminal scorecard.

Both render the same blocks from outputs/common.py, so several tests here check
that the two agree -- a report that contradicts the CI line that sent someone to
read it is worse than no report.
"""

from __future__ import annotations

import re

import pytest

from ratemyagent import Policy, scan
from ratemyagent.outputs import render_report, render_scorecard
from ratemyagent.outputs.common import format_value, target_rows, verdict_lines
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget
from tests.conftest import verdict_tail


def config(**kwargs) -> ProbeConfig:
    defaults = {"requests": 20, "warmup": 0, "timeout_s": 5.0, "concurrency": 8,
                "extra": {"fault_rate": 0.3}}
    return ProbeConfig(**{**defaults, **kwargs})


async def scan_mock(target=None, **kwargs):
    return await scan(target or MockTarget.healthy(), config=config(**kwargs))


class TestFormatting:
    @pytest.mark.parametrize(
        "value,units,expected",
        [
            (5000, "ms", "5.00s"),
            (442.3, "ms", "0.44s"),
            (0.05, "rate", "5.0%"),
            (0.1234, "$", "$0.1234"),
            (1.5, "x", "1.50x"),
            (16, "", "16"),
            (None, "ms", "-"),
        ],
    )
    def test_values_render_in_readable_units(self, value, units, expected):
        assert format_value(value, units) == expected


class TestActualVsTarget:
    async def test_failures_are_listed_first(self):
        rows = target_rows(await scan_mock(MockTarget.failing()))
        statuses = [row.status for row in rows]

        assert statuses[0] == "FAIL"
        assert statuses == sorted(statuses, key=lambda s: {"FAIL": 0, "pass": 1, "n/a": 2}[s])

    async def test_every_check_gets_a_row(self):
        result = await scan_mock()
        assert len(target_rows(result)) == len(result.checks)

    async def test_skipped_checks_show_as_na(self):
        rows = {row.label: row for row in target_rows(await scan_mock())}
        assert rows["cost per request"].status == "n/a"
        assert rows["cost per request"].actual == "-"

    async def test_rows_carry_both_sides_of_the_comparison(self):
        rows = {row.label: row for row in target_rows(await scan_mock())}
        latency = rows["p95 latency"]

        assert latency.actual.endswith("s")
        assert latency.target == "5.00s"


class TestVerdictLines:
    async def test_a_pass_says_pass(self):
        """A clean scan: over the threshold and nothing failed."""
        result = await scan_mock()
        for check in result.checks:
            check.passed = True
        result.passed = True

        assert verdict_lines(result)[0].startswith("PASS: score")

    async def test_a_high_score_with_a_failed_check_says_fail_and_why(self):
        """The contradiction this replaces: 99/100 printed PASS above a table
        with FAIL in it, because one failed check averaged away to 0.6 points."""
        result = await scan_mock()
        result.score = 99.0
        measured = [c for c in result.checks if not c.skipped]
        for check in measured:
            check.passed = True
        measured[0].passed = False
        result.passed = False

        headline = verdict_lines(result)[0]

        assert headline.startswith("FAIL: score 99")
        assert "meets pass threshold" in headline, "must not claim it is below 75"
        assert "1 check failed" in headline

    async def test_a_failure_names_the_biggest_gaps(self):
        lines = verdict_lines(await scan_mock(MockTarget.failing()))

        assert lines[0].startswith("FAIL: score")
        assert lines[1].startswith("Biggest gaps:")
        assert "/" in lines[1]

    async def test_gaps_are_limited(self):
        lines = verdict_lines(await scan_mock(MockTarget.failing()), limit=1)
        assert lines[1].count("(") == 1


class TestScorecard:
    async def test_shows_actual_against_target(self):
        card = render_scorecard(await scan_mock())

        assert "actual" in card and "target" in card and "status" in card
        assert "p95 latency" in card

    async def test_shows_the_score_breakdown_with_points(self):
        card = render_scorecard(await scan_mock())

        assert "Score breakdown:" in card
        assert "/20" in card and "/35" in card

    async def test_ends_with_the_verdict(self):
        """An engineer reads the last two lines off a CI log."""
        card = render_scorecard(await scan_mock(MockTarget.failing()))
        tail = verdict_tail(card)[-2:]

        assert tail[0].startswith("FAIL: score")
        assert tail[1].startswith("Biggest gaps:")

    async def test_groups_by_phase(self):
        card = render_scorecard(await scan_mock())

        assert "Phase 1  baseline" in card
        assert "Phase 2  chaos" in card
        assert "Phase 3  behavior" in card

    async def test_checks_can_be_suppressed(self):
        card = render_scorecard(await scan_mock(), show_checks=False)
        assert "Score breakdown:" not in card


class TestReport:
    async def test_has_a_phase_section_for_each_phase(self):
        report = render_report(await scan_mock())

        assert "## Phase 1 — Baseline" in report
        assert "## Phase 2 — Fault injection" in report
        assert "## Phase 3 — Behavior analysis" in report

    async def test_phases_appear_in_pipeline_order(self):
        report = render_report(await scan_mock())
        assert report.index("Phase 1") < report.index("Phase 2") < report.index("Phase 3")

    async def test_includes_the_actual_vs_target_table(self):
        report = render_report(await scan_mock())

        assert "## Actual vs target" in report
        assert "| measurement | actual | target | status |" in report

    async def test_includes_the_score_breakdown_with_a_total(self):
        result = await scan_mock()
        report = render_report(result)

        assert "## Score breakdown" in report
        assert f"**{result.score:.0f}/100**" in report

    async def test_includes_every_finding(self):
        result = await scan_mock(MockTarget.failing())
        report = render_report(result)

        for probe in result.probes:
            for finding in probe.findings:
                assert finding in report

    async def test_concurrency_levels_are_tabulated(self):
        report = render_report(await scan_mock(MockTarget.saturating(), concurrency=16))
        assert "| concurrency | error rate | p95 | goodput |" in report

    async def test_contract_outcomes_are_tabulated(self):
        report = render_report(await scan_mock())
        assert "| edge case | worst outcome |" in report

    async def test_records_how_the_scan_was_run(self):
        report = render_report(await scan_mock())

        assert "## How this scan was run" in report
        assert "| seed |" in report

    async def test_an_inapplicable_probe_says_why(self):
        report = render_report(await scan_mock())
        assert "Not applicable to this target" in report

    async def test_verdict_matches_the_scorecard(self):
        """Two renderings of one scan must not disagree."""
        result = await scan_mock(MockTarget.failing())
        report, card = render_report(result), render_scorecard(result)

        for line in verdict_lines(result):
            assert line in report
            assert line in card

    async def test_a_failing_scan_marks_failures_in_bold(self):
        report = render_report(await scan_mock(MockTarget.failing()))
        assert "**FAIL**" in report

    async def test_report_is_valid_markdown_structure(self):
        report = render_report(await scan_mock())

        assert report.startswith("# RateMyAgent report")
        # Every table header is followed by a separator row.
        lines = report.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
                assert lines[i + 1].startswith("|")

    async def test_a_custom_policy_name_is_reported(self):
        result = await scan(
            MockTarget.healthy(),
            config=config(),
            policy=Policy(name="my-service", thresholds={"p95_latency_ms": 5000}),
        )
        assert "my-service" in render_report(result)


class TestProbeProvenance:
    """A saved artifact must say what it measured, not just what it scored.

    Establishing that a server-memory 100/100 came from calling `create_entities`
    with `{"entities": []}` -- creating zero entities -- required re-running the
    scan, because the report recorded the number and not the call behind it.
    """

    async def test_the_header_names_the_tool_and_arguments(self):
        result = await scan_mock()
        result.target.metadata = {
            "probe_tool": "create_entities",
            "probe_args": {"entities": []},
        }
        rendered = render_report(result)

        assert "**Probe tool:** `create_entities`" in rendered
        assert '"entities": []' in rendered

    async def test_a_target_without_a_probe_tool_adds_no_rows(self):
        result = await scan_mock()
        result.target.metadata = {}

        assert "Probe tool" not in render_report(result)

    async def test_long_arguments_are_truncated_rather_than_wrapped(self):
        result = await scan_mock()
        result.target.metadata = {
            "probe_tool": "t",
            "probe_args": {"blob": "x" * 500},
        }
        line = next(
            ln for ln in render_report(result).splitlines() if "Probe arguments" in ln
        )
        assert len(line) < 220 and line.rstrip("`").endswith("...")


class TestSubSecondLatency:
    """`{:.2f}s` reported a real 0.73ms p95 as "0.00s".

    Unreadable, and worse, indistinguishable from a missing measurement -- which
    is how a scan of a local stdio server looked like it had measured nothing.
    """

    @pytest.mark.parametrize("seconds,expected", [
        (7.9884, "7.99s"),      # unchanged: the common case
        (0.4423, "0.44s"),      # unchanged: still comparable to a 5.00s target
        (0.01, "0.01s"),        # the cutoff itself stays in seconds
        (0.0073, "7.3ms"),
        (0.00073, "0.73ms"),    # the measurement that used to read 0.00s
        (0.0, "0ms"),
        (None, "-"),
    ])
    def test_it_scales_to_something_readable(self, seconds, expected):
        from ratemyagent.formatting import format_seconds

        assert format_seconds(seconds) == expected

    def test_no_real_measurement_renders_as_zero_seconds(self):
        """The property that matters, not just the examples above."""
        from ratemyagent.formatting import format_seconds

        for exponent in range(0, 7):
            value = 1 / (10 ** exponent)
            assert format_seconds(value) != "0.00s", value

    def test_the_threshold_column_uses_the_same_scale(self):
        """Actual and target must be comparable without unit conversion."""
        assert format_value(5000.0, "ms") == "5.00s"
        assert format_value(0.7314, "ms") == "0.73ms"


class TestNoRendererCollapsesToZero:
    """One formatter, every duration. Five call sites were missed on the first
    pass and only surfaced by reading real output -- 'p95 rose to 0.00s from
    0.00s' from the concurrency probe, in the same scan whose latency line had
    already been fixed."""

    async def test_no_output_surface_prints_a_zero_second_duration(self):
        from ratemyagent.outputs import render_agents_md

        result = await scan_mock(MockTarget.healthy())
        # Force every duration metric sub-millisecond, the case that used to
        # render as 0.00s everywhere.
        for probe in result.probes:
            for key, value in list(probe.metrics.items()):
                if key.endswith("_s") and isinstance(value, (int, float)):
                    probe.metrics[key] = 0.0007

        # Anchored: a plain `"0.00s" in rendered` also matches the "0.00s" inside
        # "10.00s", which is a threshold doing nothing wrong.
        collapsed = re.compile(r"(?<![\d.])0\.00s")

        for name, rendered in (
            ("scorecard", render_scorecard(result)),
            ("report", render_report(result)),
            ("agents_md", render_agents_md(result)),
        ):
            hit = collapsed.search(rendered)
            assert hit is None, (
                f"{name} collapses a real value to 0.00s: "
                f"{rendered[max(0, hit.start() - 60):hit.end() + 10]!r}"
            )


class TestContractCoverageReachesTheReader:
    """The full tool count existed in the metrics dict and was rendered nowhere.

    `metrics["tools"]` has always held the real number; the report table showed
    only `tools_probed`, so "tools probed 3" against a twelve-tool server told a
    reader nothing was missing. That is how a document whose entire purpose was
    stating denominators came to present three tools as a server's five.
    """

    async def test_the_report_states_what_was_left_out(self):
        async with MockTarget.healthy(
            tools=("get_a", "get_b", "get_c", "get_d", "delete_e")
        ) as target:
            result = await scan(target, config=config(), policy=Policy.default())

        report = render_report(result)

        assert "tools exposed" in report, "the full count is still unrendered"
        assert "skipped as unsafe" in report
        assert "past the cap" in report

    async def test_the_scorecard_summary_carries_the_denominator(self):
        async with MockTarget.healthy(
            tools=("get_a", "get_b", "get_c", "delete_d")
        ) as target:
            result = await scan(target, config=config(), policy=Policy.default())

        rendered = render_scorecard(result)

        assert "of 4 tools" in rendered
        assert "skipped as mutating" in rendered

    async def test_complete_coverage_says_so_without_a_caveat(self):
        """A denominator note on a scan that probed everything is noise."""
        async with MockTarget.healthy(tools=("get_a",)) as target:
            result = await scan(target, config=config(), policy=Policy.default())

        rendered = render_scorecard(result)

        assert "1 tool:" in rendered
        assert " of " not in rendered.split("Contract")[1].split("\n")[0]
