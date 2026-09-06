"""Standing check: wherever the tool prints component counts beside a total, they sum.

Three near-misses in one release cycle argued for one check instead of three
catches:

- the contract summary read "9 rejected cleanly (9 unclassified), 0 accepted,
  0 crashed" for eighteen edge cases, so the parts summed to nine
- the scorecard breakdown column sums to the un-renormalised total, which is
  correct but reads as broken arithmetic to anyone adding it up
- the banner grep gate counted the token inside its own documentation, so it
  could never reach zero

A reader who adds up the parts and does not get the total stops trusting the
whole report, and is right to. These tests do the adding.

Rounding: the contract counts are integers and must reconcile exactly. The
scorecard breakdown prints `{points:.0f}` per row, so a row can be off by up to
half a point in either direction and the check is tolerance-aware. Getting that
backwards would either miss real breaks or fail on every run.
"""

from __future__ import annotations

import re

import pytest

from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.outputs import render_scorecard
from ratemyagent.policy import Policy, evaluate
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import ContractTester
from ratemyagent.targets.mock import MockTarget

SUMMARY = re.compile(
    r"(?P<total>\d+) edge cases across \d+ tools: "
    r"(?P<rejected>\d+) rejected(?: \((?P<clean>\d+) cleanly, (?P<unc>\d+) unclassified\))?"
    r"(?: cleanly)?, (?P<accepted>\d+) accepted, (?P<crashed>\d+) crashed"
)
BREAKDOWN_ROW = re.compile(r"^\s{4}(\w+)\s+(-|\d+)/(\d+)")
TOTAL = re.compile(r"^\s+Score: (\d+)/100")


def parse_summary(text: str) -> dict[str, int]:
    match = SUMMARY.search(text)
    assert match, f"summary line did not parse, so it cannot be checked: {text!r}"
    return {k: int(v) for k, v in match.groupdict().items() if v is not None}


class TestContractCounts:
    """Exact: these are whole edge cases, not rounded points."""

    @pytest.mark.parametrize("profile", ["healthy", "degraded", "failing"])
    async def test_metric_components_sum_to_cases_run(self, profile):
        async with getattr(MockTarget, profile)() as target:
            result = await ContractTester().execute(target, ProbeConfig(requests=8))

        m = result.metrics
        assert m["rejected"] + m["accepted"] + m["crashes"] == m["cases_run"]

    @pytest.mark.parametrize("profile", ["healthy", "degraded", "failing"])
    async def test_the_printed_summary_sums_to_its_own_total(self, profile):
        async with getattr(MockTarget, profile)() as target:
            result = await ContractTester().execute(target, ProbeConfig(requests=8))

        parts = parse_summary(result.summary)
        assert parts["rejected"] + parts["accepted"] + parts["crashed"] == parts["total"]

    async def test_the_unclassified_split_sums_to_its_own_rejected_count(self):
        """The regression: the split must partition rejections, not replace them."""
        from tests.test_targets.test_mcp_error_payloads import (
            FakeResult,
            FakeTool,
            mcp_target,
        )

        target = mcp_target(
            lambda n, a: FakeResult("wording we do not recognise", is_error=True)
        )
        target._tools = [FakeTool("t")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        parts = parse_summary(result.summary)
        assert "unc" in parts, "unclassified rejections were not broken out"
        assert parts["clean"] + parts["unc"] == parts["rejected"]
        assert parts["rejected"] + parts["accepted"] + parts["crashed"] == parts["total"]
        assert (
            result.metrics["rejected_unclassified"] <= result.metrics["rejected"]
        ), "a subset count exceeded the set it is drawn from"


class TestScorecardBreakdown:
    """Tolerance-aware: each row prints `{points:.0f}`."""

    def _scored(self) -> ScanResult:
        probes = [
            ProbeResult(probe="latency", metrics={"p95_s": 1.0, "error_rate": 0.02}),
            ProbeResult(probe="contract", metrics={"crash_rate": 0.1, "accepted_invalid": 2}),
            ProbeResult(probe="concurrency", metrics={"max_sustained_concurrency": 8}),
            ProbeResult(probe="cost", metrics={}, applicable=False),
        ]
        result = ScanResult(target=TargetInfo(name="t", kind="mock"), probes=probes)
        return evaluate(result, Policy.default())

    def test_the_printed_rows_reconcile_with_the_printed_total(self):
        rendered = render_scorecard(self._scored())

        rows = [BREAKDOWN_ROW.match(line) for line in rendered.splitlines()]
        measured = [(int(m.group(2)), int(m.group(3))) for m in rows if m and m.group(2) != "-"]
        assert measured, "no measured rows parsed, so nothing was actually checked"

        total_match = next(
            (TOTAL.match(line) for line in rendered.splitlines() if TOTAL.match(line)), None
        )
        assert total_match, "no total line parsed"

        earned = sum(points for points, _ in measured)
        available = sum(weight for _, weight in measured)
        expected = earned / available * 100

        # Each row may round by up to half a point; propagate that to the total.
        slack = (len(measured) * 0.5) / available * 100 + 0.5
        assert abs(int(total_match.group(1)) - expected) <= slack, (
            f"breakdown sums to {earned}/{available} = {expected:.1f}, "
            f"total says {total_match.group(1)}"
        )

    def test_unmeasured_rows_are_excluded_from_both_sides(self):
        """An n/a row must print, and must not be summed into either figure."""
        rendered = render_scorecard(self._scored())
        assert "cost" in rendered
        assert "-/15" in rendered
