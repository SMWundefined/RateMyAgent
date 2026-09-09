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

import pathlib
import re

import pytest

from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.outputs import render_scorecard
from ratemyagent.policy import Policy, evaluate
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import ContractTester
from ratemyagent.targets.mock import MockTarget

SUMMARY = re.compile(
    # "across 3 tools" when coverage is complete, "across 3 of 12 tools (6
    # skipped as mutating)" when it is not. The bare form was the entire bug:
    # it was read as the server's tool count for the life of the probe.
    r"(?P<total>\d+) edge cases across \d+(?: of \d+)? tools?(?: \([^)]*\))?: "
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
        # Named for a verb the mutability classifier knows: an unclassified
        # *tool* is now skipped, and this test is about unclassified *rejection
        # wording*, which is a different axis entirely.
        target._tools = [FakeTool("get_thing")]
        result = await ContractTester().execute(target, ProbeConfig(requests=5))

        parts = parse_summary(result.summary)
        assert "unc" in parts, "unclassified rejections were not broken out"
        assert parts["clean"] + parts["unc"] == parts["rejected"]
        assert parts["rejected"] + parts["accepted"] + parts["crashed"] == parts["total"]
        assert (
            result.metrics["rejected_unclassified"] <= result.metrics["rejected"]
        ), "a subset count exceeded the set it is drawn from"


class TestVersionSources:
    """Two files hardcode the version; nothing asserted they agree until 0.1.4.

    `pyproject.toml` decides what PyPI publishes and `__init__.py` decides what
    `--version` and the scorecard byline print. Drift ships a wheel whose
    self-reported version contradicts the index it came from -- a plausible
    wrong number, which is the failure mode this module exists for.
    """

    def test_pyproject_and_dunder_version_agree(self):
        import ratemyagent

        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / "pyproject.toml").read_text()
        match = re.search(r'^version = "([^"]+)"', text, re.M)
        assert match, "no version found in pyproject.toml"
        assert match.group(1) == ratemyagent.__version__, (
            f"pyproject says {match.group(1)}, __init__ says {ratemyagent.__version__}"
        )


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
        printed = int(total_match.group(1))

        # The column sums to the *pre-cap* mean. When a cap bites, the printed
        # total is lower on purpose -- and must say so, or a reader adding the
        # column up finds a number that does not match and cannot tell why.
        result = self._scored()
        if result.cap_reason:
            assert printed < expected
            assert result.cap_reason in rendered, "the cap is applied but not explained"
        else:
            assert abs(printed - expected) <= slack, (
                f"breakdown sums to {earned}/{available} = {expected:.1f}, "
                f"total says {printed}"
            )

    def test_unmeasured_rows_are_excluded_from_both_sides(self):
        """An n/a row must print, and must not be summed into either figure."""
        rendered = render_scorecard(self._scored())
        assert "cost" in rendered
        assert "-/15" in rendered


class TestStatusCounts:
    """The status line is derived from the section 9 table, not written beside it.

    This count inflated across three surfaces without anyone writing a false
    sentence: PROGRESS said "eight published servers", the roadmap status line
    said nine, and a later plan said twelve. None was derived from the table,
    which has nine rows over seven distinct servers. A number that can drift
    from its source will.

    Skips when `assets/` is absent -- it is gitignored working material, so this
    check is local-only by construction and cannot run in CI. That is a real
    weakness of this particular check, not a property of the rule.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    PROGRESS = ROOT / "assets" / "PROGRESS.md"
    NEXTSTEPS = ROOT / "assets" / "NextSteps.MD"

    def _table_counts(self) -> tuple[int, int]:
        """(scans, distinct servers) read from the section 9 table itself."""
        text = self.PROGRESS.read_text()
        # Anchored: section 8b quotes this header while explaining why the
        # parser keys off it, and an unanchored search finds the prose first.
        match = re.search(r"^\| Server \| Arguments \|", text, re.M)
        assert match, "section 9 table not found"
        start = match.start()
        block = text[start : text.index("\n\n", start)]
        rows = [ln for ln in block.splitlines() if ln.startswith("| `")]
        servers = {ln.split("|")[1].strip() for ln in rows}
        return len(rows), len(servers)

    @pytest.mark.skipif(not PROGRESS.exists(), reason="assets/ is gitignored")
    def test_the_table_still_has_the_shape_the_prose_claims(self):
        scans, servers = self._table_counts()
        assert (scans, servers) == (9, 7), (
            f"table now has {scans} scans over {servers} servers; every count "
            "in PROGRESS and NextSteps needs updating with it"
        )

    @pytest.mark.skipif(not NEXTSTEPS.exists(), reason="assets/ is gitignored")
    def test_the_status_line_matches_the_table(self):
        scans, servers = self._table_counts()
        status = next(
            ln for ln in self.NEXTSTEPS.read_text().splitlines()
            if ln.startswith("**Status:**")
        )
        assert f"{scans} scans across {servers} distinct servers" in status, status

    @pytest.mark.skipif(not NEXTSTEPS.exists(), reason="assets/ is gitignored")
    def test_the_status_line_matches_the_shipped_version_and_suite(self):
        import ratemyagent

        status = next(
            ln for ln in self.NEXTSTEPS.read_text().splitlines()
            if ln.startswith("**Status:**")
        )
        assert f"v{ratemyagent.__version__} on PyPI" in status, status

    @pytest.mark.skipif(not PROGRESS.exists(), reason="assets/ is gitignored")
    def test_no_surface_says_nine_servers(self):
        """'Nine servers' is the ambiguity that produced the drift: nine scans,
        seven servers, and the phrase collapses them.

        Quoted and backticked occurrences are exempt, because the writeup has to
        be able to name the phrase it is warning about. The first version of
        this check failed on PROGRESS section 8b explaining why "nine servers"
        is wrong -- a checker that cannot distinguish its subject from a mention
        of its subject, which is the rule three entries above it in that same
        section. Third time that shape has appeared.
        """
        mention = re.compile(r'["`\']')
        for path in (self.PROGRESS, self.NEXTSTEPS):
            if not path.exists():
                continue
            for phrase in ("nine servers", "eight servers", "eight published servers"):
                for match in re.finditer(re.escape(phrase), path.read_text(), re.I):
                    before = path.read_text()[max(0, match.start() - 1) : match.start()]
                    assert mention.match(before), (
                        f"{path.name} uses {phrase!r} as a claim, not a quoted mention: "
                        f"...{path.read_text()[max(0, match.start() - 70):match.end()]}"
                    )


class TestCapIsExportedNotJustPrinted:
    """A JSON consumer must be able to reconcile the breakdown too."""

    def test_the_export_carries_both_scores_and_the_reason(self):
        probes = [
            ProbeResult(probe="latency", metrics={"p95_s": 1.0}),
            ProbeResult(probe="behavior", metrics={"recovery_rate": 0.857}),
        ]
        result = evaluate(
            ScanResult(target=TargetInfo(name="t", kind="mock"), probes=probes),
            Policy(thresholds={"p95_latency_ms": 5000, "recovery_rate_min": 0.90}),
        )
        payload = result.to_dict()

        assert payload["score"] == 89
        assert payload["uncapped_score"] > 89
        assert "recovery_rate_min" in payload["cap_reason"]

    def test_an_uncapped_scan_exports_them_equal_with_no_reason(self):
        result = evaluate(
            ScanResult(
                target=TargetInfo(name="t", kind="mock"),
                probes=[ProbeResult(probe="latency", metrics={"p95_s": 1.0})],
            ),
            Policy(thresholds={"p95_latency_ms": 5000}),
        )
        payload = result.to_dict()

        assert payload["score"] == payload["uncapped_score"]
        assert payload["cap_reason"] is None


class TestPastedOutputIsReal:
    """The scan output in the README must be output the tool actually produces.

    It went stale for four releases without anyone noticing. The block was
    captured under 0.1.6 and still showed `concurrency 15/15`, a scored
    `retry amplification` row the 0.1.9 split had made `n/a`, and a composite of
    86 that 0.1.10 moved to 84. Every number in it was true once, which is
    exactly why nobody reread it.

    A pasted transcript is a claim about behaviour, and the standing rule is
    that anything verifying something else runs in CI. The README ships in the
    sdist and on PyPI, so unlike the section 9 checks above this one has its
    source available in a clean checkout and runs everywhere.

    The mock target is deterministic, so this is an equality check rather than a
    fuzzy one -- with one masked line. Wall-clock duration is the only thing in
    the block that is not a property of the target, and it straddles the 10ms
    boundary where `format_seconds` switches units, so it prints "0.01s" on one
    run and "9.6ms" on the next. Masking that line keeps every number that
    describes the target under exact comparison.

    If it fails, do not edit the README by hand: rerun the command in the fence
    above the block and paste what comes out.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    README = ROOT / "README.md"
    COMMAND = [
        "scan", "--target", "mock", "--profile", "degraded",
        "--requests", "40", "--concurrency", "16", "--fault-rate", "0.3",
    ]

    DURATION = re.compile(r"Duration: \S+")

    @classmethod
    def _mask(cls, text: str) -> str:
        return cls.DURATION.sub("Duration: -", text.strip())

    def test_the_readme_block_matches_a_real_run(self):
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        text = self.README.read_text()
        match = re.search(r"```\nRateMyAgent Scan Results\n(.*?)\n```", text, re.S)
        assert match, "README no longer contains a pasted scan block"
        pasted = "RateMyAgent Scan Results\n" + match.group(1)

        result = CliRunner().invoke(cli, self.COMMAND)
        assert result.exit_code == 0, result.output

        assert self._mask(pasted) == self._mask(result.output), (
            "the README's pasted scan output is not what the tool prints. "
            "Rerun the documented command and paste the result; do not edit the "
            "numbers by hand."
        )

    def test_the_documented_command_is_the_one_that_was_run(self):
        """Guards the other half: the fence above the block must invoke this."""
        text = self.README.read_text()
        block = text.index("```\nRateMyAgent Scan Results")
        fence = text.rindex("```bash", 0, block)
        documented = text[fence:block]

        for flag in ("--profile degraded", "--requests 40",
                     "--concurrency 16", "--fault-rate 0.3"):
            assert flag in documented, (
                f"README documents a command without {flag!r}, so the block "
                "below it is output from something else"
            )


class TestCommittedExamplesAreReal:
    """The examples/ fixtures are published artifacts and went stale silently.

    `mock-failing.AGENTS.md` was regenerated for 0.1.17 and had been stale since
    **0.1.9**: it still scored `concurrency 0/15`, a dimension retired in
    0.1.10, and showed `retry amplification` as a scored row after the 0.1.9
    behaviour split withheld it. Three shipped changes passed over it, and the
    README transcript gate covered only the README.

    Deterministic by construction -- the mock is seeded and fault injection is
    seeded per operation and attempt -- so this is an equality check on
    everything except the timestamp and the wall-clock duration.

    The `mcp-server-git` pair is not gated here: it needs `uvx`, a real server
    and a real repository, so like the section 9 checks it cannot run in CI.
    That is a real weakness of this particular check, stated rather than papered
    over.

    If it fails, do not edit the fixture: delete it and rerun the command in
    `examples/README.md`. Deleting first matters -- an existing file is diffed
    against, so regenerating in place adds a "Since the last scan" section that
    does not belong in a standalone example.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    COMMAND = [
        "scan", "--target", "mock", "--profile", "failing", "--requests", "40",
        "--concurrency", "16", "--fault-rate", "0.3", "--seed", "42",
    ]
    #: Everything that changes between two runs of the same seeded scan: the
    #: timestamps in the header and the state block, and wall-clock duration.
    #: Every number that describes the target stays under exact comparison.
    VOLATILE = re.compile(
        r'^.*(Scanned:|Duration:|"generated_at"|"scanned_at"|\| duration).*$',
        re.M,
    )

    def _stable(self, text: str) -> str:
        return self.VOLATILE.sub("", text).strip()

    def _regenerate(self, tmp_path, output: str, flag: str) -> str:
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        out = tmp_path / "generated.md"
        result = CliRunner().invoke(
            cli, [*self.COMMAND, "--output", output, flag, str(out)]
        )
        assert result.exit_code == 0, result.output
        return out.read_text()

    def test_the_agents_md_example_matches_a_real_run(self, tmp_path):
        committed = self.ROOT / "examples" / "mock-failing.AGENTS.md"
        fresh = self._regenerate(tmp_path, "agents-md", "--agents-md-out")

        assert self._stable(committed.read_text()) == self._stable(fresh), (
            "examples/mock-failing.AGENTS.md is not what the tool generates. "
            "Delete it and rerun the command in examples/README.md."
        )

    def test_the_report_example_matches_a_real_run(self, tmp_path):
        committed = self.ROOT / "examples" / "mock-failing.report.md"
        fresh = self._regenerate(tmp_path, "report", "--report-out")

        assert self._stable(committed.read_text()) == self._stable(fresh), (
            "examples/mock-failing.report.md is not what the tool generates. "
            "Delete it and rerun the command in examples/README.md."
        )

    def test_the_documented_command_is_the_one_that_was_run(self):
        """Guards the other half, the way the README transcript gate does."""
        documented = (self.ROOT / "examples" / "README.md").read_text()
        for flag in ("--profile failing", "--requests 40", "--concurrency 16",
                     "--fault-rate 0.3", "--seed 42"):
            assert flag in documented, f"examples/README.md no longer documents {flag}"


class TestCaveatsReachEveryConsumer:
    """The JSON export is the fourth surface, and it was the one I forgot.

    Caveats landed in the scorecard, the report and AGENTS.md in 0.1.17, and
    `--json-out` kept exporting `findings` alone -- so the one consumer that
    cannot read a dim grey line and infer anything was the one told nothing.
    Caught by reading a re-scan artifact, not by a test, which is why there is
    now a test.
    """

    async def test_the_json_export_carries_them(self):
        from ratemyagent import Policy, scan
        from ratemyagent.probes import ProbeConfig
        from ratemyagent.targets import MockTarget

        async with MockTarget.healthy() as target:
            result = await scan(
                target, config=ProbeConfig(requests=10, warmup=0),
                policy=Policy.default(),
            )

        exported = result.to_dict()
        latency = next(p for p in exported["probes"] if p["probe"] == "latency")

        assert "caveats" in latency, "caveats are not exported at all"
        suppressed = [c for c in latency["caveats"] if c["metrics"] == ["p99_s"]]
        assert suppressed, "the p99 suppression did not reach the export"
        assert suppressed[0]["effect"] == "suppress"
        assert suppressed[0]["scope"] == "metric"
