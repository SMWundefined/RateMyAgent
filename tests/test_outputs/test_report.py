"""Markdown report and terminal scorecard.

Both render the same blocks from outputs/common.py, so several tests here check
that the two agree -- a report that contradicts the CI line that sent someone to
read it is worse than no report.
"""

from __future__ import annotations

import pytest

from ratemyagent import Policy, scan
from ratemyagent.outputs import render_report, render_scorecard
from ratemyagent.outputs.common import format_value, target_rows, verdict_lines
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget


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
        lines = verdict_lines(await scan_mock())
        assert lines[0].startswith("PASS: score")

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
        tail = card.strip().splitlines()[-2:]

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
