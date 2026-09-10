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

import os
import pathlib
import re

import pytest

from ratemyagent.models import ProbeResult, ScanResult, TargetInfo
from ratemyagent.outputs import render_scorecard
from ratemyagent.policy import Policy, evaluate
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.contract import ContractTester
from ratemyagent.targets.mock import MockTarget
from tests.support import contains_phrase, find_phrases, is_mention, mention_spans

#: Phrases that collapse "nine scans" and "seven servers" into one wrong noun.
BANNED_PHRASES = ("nine servers", "eight servers", "eight published servers")


def unquoted_claims(text: str, phrases: tuple[str, ...] = BANNED_PHRASES) -> list[str]:
    """Every banned phrase in `text` used as a claim, not as a quoted mention.

    Backticked and fenced occurrences are skipped, anchored the way the
    `SUPERSEDED-PENDING-RESCAN` banner gate was: a live marker and a description
    of one are different strings, and the writeup has to be able to name what it
    warns about. See `tests.support.mention_spans` for what counts.

    A module-level function, not a method, so the gates below can run it against
    a hand-built document. A gate that can only be pointed at the real file
    cannot be shown a deliberate failure.
    """
    hits = []
    for phrase in phrases:
        spans = mention_spans(text)
        for match in find_phrases(text, phrase, re.I):
            if is_mention(text, match.start(), match.end(), spans):
                continue
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"line {line}: ...{text[max(0, match.start() - 70) : match.end()]}")
    return hits


def status_block(text: str) -> str:
    """The whole `**Status:**` paragraph, not just its first line.

    Taking a single line was the other half of the same defect: a status claim
    the author wrapped is split across two lines, and a gate reading `lines[i]`
    can only ever see the first of them.
    """
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("**Status:**"))
    block = []
    for line in lines[start:]:
        if block and not line.strip():
            break
        block.append(line)
    return "\n".join(block)

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

    #: Prose that ships. Tracked, so the banned-phrase gate below runs in the
    #: `test` job on every push rather than skipping the way the `assets/`
    #: checks must. The phrase is most expensive exactly here -- `README.md`
    #: is the page on PyPI -- and until 2026-09-09 it was the one surface
    #: nothing checked.
    TRACKED_PROSE = (
        ROOT / "README.md",
        ROOT / "docs" / "PROBES.md",
        ROOT / "docs" / "POLICY.md",
    )

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
        status = status_block(self.NEXTSTEPS.read_text())
        phrase = f"{scans} scans across {servers} distinct servers"
        assert contains_phrase(status, phrase), status

    @pytest.mark.skipif(not NEXTSTEPS.exists(), reason="assets/ is gitignored")
    def test_the_status_line_matches_the_shipped_version_and_suite(self):
        import ratemyagent

        status = status_block(self.NEXTSTEPS.read_text())
        phrase = f"v{ratemyagent.__version__} on PyPI"
        assert contains_phrase(status, phrase), status

    def test_the_tracked_prose_surfaces_are_all_present(self):
        """A haystack that quietly shrinks to nothing is a gate that cannot fail.

        `TRACKED_PROSE` is a hardcoded list of paths. Rename or move one and the
        loop below would skip it in silence, leaving a green check over an
        unchecked file -- the shape in every entry of section 8b. So the list is
        asserted before it is used.
        """
        assert self.TRACKED_PROSE, (
            "the tracked-prose list is empty, so the banned-phrase gate reads "
            "nothing in CI and cannot fail. An empty list satisfies every "
            "'all present' check written as a loop -- state the floor."
        )
        missing = [p.name for p in self.TRACKED_PROSE if not p.exists()]
        assert not missing, (
            f"the banned-phrase gate names files that no longer exist: {missing}. "
            "Fix the list rather than letting it check fewer surfaces."
        )

    def test_no_surface_says_nine_servers(self):
        """The phrase collapses nine scans and seven servers into one wrong noun.

        **Not skipped.** It reads the three tracked prose files unconditionally,
        so it runs in the `test` job on every push and every Python in the
        matrix. The two `assets/` files are added when present -- they are
        gitignored working material, so locally this covers five surfaces and in
        CI it covers three. It used to cover the two that CI can never see.

        Backticked and fenced occurrences are exempt, because the writeup has to
        be able to name the phrase it is warning about. The first version of
        this check failed on PROGRESS section 8b explaining why the phrase is
        wrong -- a checker that could not distinguish its subject from a mention
        of its subject, which is the rule three entries above it in that same
        section. The second version got that right only for a phrase whose
        *immediately* preceding character was a quote, which is not the form any
        real mention in that section takes.

        Matching is word-by-word via `tests.support`. Escaping the phrase as one
        literal is what let a wrapped occurrence sit in section 8 from week 6 to
        2026-09-09: the sixth instance in that same list.
        """
        surfaces = [*self.TRACKED_PROSE, self.PROGRESS, self.NEXTSTEPS]
        checked = 0
        for path in surfaces:
            if not path.exists():
                continue
            checked += 1
            hits = unquoted_claims(path.read_text())
            assert not hits, (
                f"{path.name} uses a banned phrase as a claim, not a backticked "
                f"mention:\n" + "\n".join(f"  {h}" for h in hits)
            )
        assert checked >= len(self.TRACKED_PROSE), (
            f"only {checked} surface(s) were read; the tracked ones are not optional"
        )


class TestTheBannedPhraseGateReadsTrackedProse:
    """The gate's two 2026-09-09 changes, each with a case that fails without it.

    Until this session the gate read `assets/PROGRESS.md` and
    `assets/NextSteps.MD` and nothing else. Both are gitignored, so the standing
    rule -- anything that exists to verify something else runs in CI -- was
    violated by the checker written to enforce a different half of it. It ran
    only when someone ran it locally, which is the condition under which the
    week-6 claim survived twenty releases.

    It now reads `README.md`, `docs/PROBES.md` and `docs/POLICY.md` as well, all
    tracked, so it executes in the `test` job on 3.10 through 3.13. Verified by
    running it against a `git archive` checkout: **PASSED, not skipped**, with
    only the five `assets/`-dependent checks in the module skipping.

    The second change is the anchor. Extending the haystack made the gate match
    the section 8b entry describing the gate -- a checker unable to tell its
    subject from a mention of its subject, for the fourth time in this project.
    Anchored the way `SUPERSEDED-PENDING-RESCAN` was: that gate matched the HTML
    comment form, so prose could name the token in backticks. A phrase has no
    comment form, so the code span is the anchor.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def _tracked(self, monkeypatch, tmp_path, body: str, name: str = "README.md"):
        """Point the gate at one document we control, and only that one.

        The `assets/` paths are redirected to nowhere as well, which is not
        incidental tidiness: without it these cases read the real, gitignored
        `PROGRESS.md` alongside the fixture, so a case asserting the gate
        *passes* would pass locally and could flip in CI where those files do
        not exist -- and did, silently, until a mutation on the quote rule was
        killed by seven tests that had no quote in their fixture. A test whose
        verdict depends on a file CI never sees is the same defect as a gate
        that only runs locally, one level up.
        """
        path = tmp_path / name
        path.write_text(body)
        monkeypatch.setattr(TestStatusCounts, "TRACKED_PROSE", (path,))
        monkeypatch.setattr(TestStatusCounts, "PROGRESS", tmp_path / "no-PROGRESS.md")
        monkeypatch.setattr(TestStatusCounts, "NEXTSTEPS", tmp_path / "no-NextSteps.MD")
        return path

    # -- deliberate failing case 1: an unbackticked violation ----------------

    def test_an_unbackticked_claim_in_a_tracked_file_fails_the_gate(
        self, monkeypatch, tmp_path
    ):
        """The case the old gate could not have had: the file was never read."""
        self._tracked(monkeypatch, tmp_path,
                      "# RateMyAgent\n\nWe validated this against nine servers.\n")

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    def test_an_unbackticked_claim_wrapped_across_lines_fails_the_gate(
        self, monkeypatch, tmp_path
    ):
        """Both fixes at once: a tracked surface *and* a line wrap."""
        body = "# RateMyAgent\n\nWe validated this against nine\nservers in total.\n"
        assert re.search(re.escape("nine servers"), body, re.I) is None, (
            "the fixture is not actually wrapped"
        )
        self._tracked(monkeypatch, tmp_path, body)

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    @pytest.mark.parametrize("name", ["README.md", "PROBES.md", "POLICY.md"])
    def test_each_tracked_surface_is_actually_read(self, monkeypatch, tmp_path, name):
        """Naming a file in the list is not the same as reading it."""
        self._tracked(monkeypatch, tmp_path,
                      "a claim about eight published servers\n", name=name)

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    # -- deliberate failing case 2: a backticked mention must not trip -------

    def test_a_backticked_mention_in_a_tracked_file_passes(self, monkeypatch, tmp_path):
        self._tracked(monkeypatch, tmp_path,
                      "Never write `nine servers`; the count is nine scans.\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_a_backticked_mention_wrapped_across_lines_passes(
        self, monkeypatch, tmp_path
    ):
        """The form the section 8b demonstration actually takes."""
        self._tracked(monkeypatch, tmp_path,
                      "the wrapped form reads `eight\nservers` and is a mention\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_a_fenced_block_is_a_mention(self, monkeypatch, tmp_path):
        self._tracked(monkeypatch, tmp_path,
                      "text\n\n```\ngrep -n 'nine servers' README.md\n```\n\nmore\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_a_double_quoted_mention_passes(self, monkeypatch, tmp_path):
        """The form section 8b's correction table uses."""
        self._tracked(monkeypatch, tmp_path,
                      'the surface said "eight published servers" and was wrong\n')

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_a_stray_quote_earlier_in_the_file_does_not_silence_the_gate(
        self, monkeypatch, tmp_path
    ):
        """Why double quotes are an adjacency test and not a paired span.

        Pairing quotes across a document made `... servers", never "nine
        servers".` read as a claim: the stray closing quote paired with the
        mention's opening quote and left the phrase bare. Asking only whether
        the phrase itself is wrapped cannot be thrown off that way -- and a
        genuine claim after a stray quote still fails.
        """
        self._tracked(monkeypatch, tmp_path,
                      'a quote opens here" and later we claim nine servers outright\n')

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    def test_an_apostrophe_does_not_make_a_mention(self, monkeypatch, tmp_path):
        """`'` is not a delimiter: one contraction must not silence the rest."""
        self._tracked(monkeypatch, tmp_path,
                      "we don't guess, and it isn't nine servers either\n")

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    # -- the anchor must be a span test, not a character test ----------------

    def test_a_claim_inside_a_longer_backtick_span_is_a_mention(
        self, monkeypatch, tmp_path
    ):
        """The case that separates a span anchor from the old character rule.

        The previous rule asked whether the character *immediately* before the
        match was a quote. That is right only when the phrase begins the span.
        `` `the phrase nine servers is banned` `` is a mention whose phrase sits
        mid-span, and the old rule called it a claim -- so the fix for one false
        negative would have shipped a false positive, and a survivor in the
        mutation run said so before this test existed.
        """
        self._tracked(monkeypatch, tmp_path,
                      "write it as `the phrase nine servers is banned` instead\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_a_claim_mid_fenced_block_is_a_mention(self, monkeypatch, tmp_path):
        """Same discriminator, fenced form: no quote precedes the phrase."""
        self._tracked(monkeypatch, tmp_path,
                      "text\n\n```\n# we used to claim nine servers here\n```\n\nmore\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    # -- the gate must not be re-disabled ------------------------------------

    def test_the_gate_carries_no_skip_marker(self):
        """Re-adding the `assets/` skipif would silently return it to local-only.

        Calling the method directly cannot catch that -- a `skipif` mark only
        acts during collection -- so the mark itself is what gets asserted. This
        is the regression the whole change exists to prevent: the gate ran for
        twenty releases in a form CI could never execute.
        """
        marks = getattr(
            TestStatusCounts.test_no_surface_says_nine_servers, "pytestmark", []
        )
        names = {mark.name for mark in marks}
        assert not names & {"skip", "skipif"}, (
            f"the banned-phrase gate is marked {sorted(names)}, so it can skip. "
            "It reads tracked files and must run in the `test` job unconditionally; "
            "guard the optional assets/ surfaces inside the loop instead."
        )

    # -- the haystack must not quietly shrink --------------------------------

    def test_a_renamed_tracked_file_is_caught_rather_than_skipped(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(TestStatusCounts, "TRACKED_PROSE",
                            (tmp_path / "gone.md",))

        with pytest.raises(AssertionError, match="no longer exist"):
            TestStatusCounts().test_the_tracked_prose_surfaces_are_all_present()

    def test_the_gate_itself_refuses_to_pass_over_a_missing_tracked_file(
        self, monkeypatch, tmp_path
    ):
        """Two independent checks, because one of them can be deleted.

        The surfaces-present test above covers the same ground, and the gate's
        own `checked >= len(TRACKED_PROSE)` floor looks redundant beside it --
        redundant enough that a mutation removing the floor survived the first
        run. It is not redundant: the gate `continue`s past a missing file, so
        without the floor it reads fewer surfaces and still reports success.
        """
        present = tmp_path / "README.md"
        present.write_text("nine scans across seven distinct servers\n")
        monkeypatch.setattr(TestStatusCounts, "TRACKED_PROSE",
                            (present, tmp_path / "vanished.md"))
        monkeypatch.setattr(TestStatusCounts, "PROGRESS", tmp_path / "absent-a.md")
        monkeypatch.setattr(TestStatusCounts, "NEXTSTEPS", tmp_path / "absent-b.md")

        with pytest.raises(AssertionError, match="not optional"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    def test_the_gate_still_runs_when_assets_is_absent(self, monkeypatch, tmp_path):
        """The CI condition: gitignored working material is simply not there.

        The gate must read the tracked surfaces and pass, not skip. Its own
        `checked >= len(TRACKED_PROSE)` assertion is what makes an empty run
        fail rather than look green.
        """
        clean = tmp_path / "clean"
        clean.mkdir()
        surfaces = []
        for name in ("README.md", "PROBES.md", "POLICY.md"):
            path = clean / name
            path.write_text("nine scans across seven distinct servers\n")
            surfaces.append(path)
        monkeypatch.setattr(TestStatusCounts, "TRACKED_PROSE", tuple(surfaces))
        monkeypatch.setattr(TestStatusCounts, "PROGRESS", tmp_path / "absent-PROGRESS.md")
        monkeypatch.setattr(TestStatusCounts, "NEXTSTEPS", tmp_path / "absent-NextSteps.MD")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_an_emptied_surface_list_fails_rather_than_reading_nothing(
        self, monkeypatch, tmp_path
    ):
        """A gate that reads nothing must not report success.

        Written first as a test asserting the empty list *passed*, which is a
        test documenting a hole rather than closing one. An empty list satisfies
        `checked >= len(TRACKED_PROSE)` and every `all(...)` written as a loop,
        so the floor has to be stated outright.
        """
        monkeypatch.setattr(TestStatusCounts, "TRACKED_PROSE", ())
        monkeypatch.setattr(TestStatusCounts, "PROGRESS", tmp_path / "absent-a.md")
        monkeypatch.setattr(TestStatusCounts, "NEXTSTEPS", tmp_path / "absent-b.md")

        with pytest.raises(AssertionError, match="empty"):
            TestStatusCounts().test_the_tracked_prose_surfaces_are_all_present()


class TestEveryPhraseGateSurvivesAWrap:
    """One deliberate wrapped case per gate, not one for the helper.

    A helper test proves `tests.support` works. It does not prove any given gate
    is *calling* it -- and a gate that quietly kept `re.escape(phrase)` would sit
    green beside a passing helper test forever. That is the shape this project
    keeps producing: the guard is added, the guard is not itself guarded.

    So these do not test the helper. **Each one points the real gate at a
    deliberately wrapped document and runs the real gate method.** Redirecting
    the gate's own path attribute is the only way the assertion covers the gate
    body rather than a re-implementation of it: rewrite any gate below to use
    `in` again and its case here fails, because the body is what runs.

    Polarity differs by gate and so does the expected verdict:

    - gate 1 is negative -- the wrapped fixture *violates*, so the gate must raise
    - gates 2-6 are positive -- the wrapped fixture is *valid*, so it must not

    Both directions are covered per gate: a widened matcher that passes on
    anything would satisfy the first half of each pair and fail the second.
    """

    #: A section 9 table the counts parser can read: 2 rows, 2 distinct servers.
    FAKE_TABLE = (
        "## 9. Real MCP servers scanned\n\n"
        "| Server | Arguments | 0.1.19 | 0.1.20 |\n"
        "|---|---|---:|---:|\n"
        "| `alpha` | synthesized | 100 | 100 |\n"
        "| `beta` | synthesized | 100 | 100 |\n"
        "\n"
    )

    @staticmethod
    def _redirect(monkeypatch, cls, attr, tmp_path, name, text):
        """Point a gate's path attribute at a document we control."""
        path = tmp_path / name
        path.write_text(text)
        monkeypatch.setattr(cls, attr, path)
        return path

    # -- gate 1: the banned-phrase gate --------------------------------------

    def test_gate_1_catches_a_wrapped_claim(self, monkeypatch, tmp_path):
        """The 2026-09-09 regression, in the shape it actually had."""
        wrapped = (
            self.FAKE_TABLE
            + "the scanner sends a placeholder, which three of the eight\n"
            + "servers rejected outright -- a claim, not a mention.\n"
        )
        assert re.search(re.escape("eight servers"), wrapped, re.I) is None, (
            "the fixture is not actually wrapped, so it would prove nothing"
        )
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", wrapped)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", "**Status:** nothing to see.\n")

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    def test_gate_1_still_exempts_a_wrapped_mention(self, monkeypatch, tmp_path):
        """Widening must not cost the gate its subject/mention distinction."""
        text = self.FAKE_TABLE + 'avoid the phrase `eight\nservers` in prose.\n'
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", text)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", "**Status:** nothing to see.\n")

        TestStatusCounts().test_no_surface_says_nine_servers()

    def test_gate_1_catches_a_claim_wrapped_inside_a_blockquote(
        self, monkeypatch, tmp_path
    ):
        """`>` is not whitespace, and both notes files are full of blockquotes.

        The obvious fix -- joining on `\\s+` -- would ship a gate still blind to
        the format its own subject is written in. Nearly the seventh instance.
        """
        wrapped = self.FAKE_TABLE + "> a claim about nine\n> servers, in a quote\n"
        assert re.search(r"nine\s+servers", wrapped) is None, (
            "this case is only interesting if a plain \\s+ separator misses it"
        )
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", wrapped)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", "**Status:** nothing to see.\n")

        with pytest.raises(AssertionError, match="banned phrase"):
            TestStatusCounts().test_no_surface_says_nine_servers()

    # -- gate 2: the status line against the table ---------------------------

    def test_gate_2_reads_a_wrapped_status_paragraph(self, monkeypatch, tmp_path):
        status = "**Status:** v9.9.9 on PyPI. 2 scans across 2 distinct\nservers.\n"
        assert re.search(re.escape("2 scans across 2 distinct servers"), status) is None, (
            "the fixture is not actually wrapped"
        )
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", self.FAKE_TABLE)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", status)

        TestStatusCounts().test_the_status_line_matches_the_table()

    def test_gate_2_still_fails_on_a_wrong_wrapped_count(self, monkeypatch, tmp_path):
        """A widened matcher that passes on anything would sail through this."""
        status = "**Status:** v9.9.9 on PyPI. 7 scans across 4 distinct\nservers.\n"
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", self.FAKE_TABLE)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", status)

        with pytest.raises(AssertionError):
            TestStatusCounts().test_the_status_line_matches_the_table()

    def test_gate_2_reads_the_paragraph_and_not_the_rest_of_the_file(
        self, monkeypatch, tmp_path
    ):
        """Widen to the paragraph, not to the whole document.

        The correct phrase sits after a blank line, so it belongs to a different
        paragraph and must not satisfy the gate.
        """
        status = (
            "**Status:** v9.9.9 on PyPI. counts omitted.\n"
            "\n"
            "Elsewhere: 2 scans across 2 distinct servers.\n"
        )
        self._redirect(monkeypatch, TestStatusCounts, "PROGRESS", tmp_path,
                       "PROGRESS.md", self.FAKE_TABLE)
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", status)

        with pytest.raises(AssertionError):
            TestStatusCounts().test_the_status_line_matches_the_table()

    # -- gate 3: the status line against the shipped version -----------------

    def test_gate_3_reads_a_wrapped_version_claim(self, monkeypatch, tmp_path):
        import ratemyagent

        version = ratemyagent.__version__
        status = f"**Status:** long enough that it wraps before v{version}\non PyPI.\n"
        assert re.search(re.escape(f"v{version} on PyPI"), status) is None, (
            "the fixture is not actually wrapped"
        )
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", status)

        TestStatusCounts().test_the_status_line_matches_the_shipped_version_and_suite()

    def test_gate_3_still_fails_on_the_wrong_version(self, monkeypatch, tmp_path):
        status = "**Status:** v0.0.1-not-the-shipped-one\non PyPI.\n"
        self._redirect(monkeypatch, TestStatusCounts, "NEXTSTEPS", tmp_path,
                       "NextSteps.MD", status)

        with pytest.raises(AssertionError):
            TestStatusCounts().test_the_status_line_matches_the_shipped_version_and_suite()

    # -- gate 4: the README documented command -------------------------------

    def _readme(self, flags: str) -> str:
        """A README shaped the way the gate walks it: fence, then pasted block."""
        return (
            "# RateMyAgent\n\n"
            "```bash\n"
            f"{flags}\n"
            "```\n\n"
            "```\nRateMyAgent Scan Results\npasted output\n```\n"
        )

    def test_gate_4_reads_a_wrapped_flag(self, monkeypatch, tmp_path):
        wrapped = self._readme(
            "ratemyagent scan --target mock --profile\n"
            "  degraded --requests 40 --concurrency 16 --fault-rate 0.3"
        )
        assert re.search(re.escape("--profile degraded"), wrapped) is None, (
            "the fixture is not actually wrapped"
        )
        self._redirect(monkeypatch, TestPastedOutputIsReal, "README", tmp_path,
                       "README.md", wrapped)

        TestPastedOutputIsReal().test_the_documented_command_is_the_one_that_was_run()

    def test_gate_4_still_fails_on_a_missing_flag(self, monkeypatch, tmp_path):
        missing = self._readme("ratemyagent scan --target mock --requests 40")
        self._redirect(monkeypatch, TestPastedOutputIsReal, "README", tmp_path,
                       "README.md", missing)

        with pytest.raises(AssertionError, match="without"):
            TestPastedOutputIsReal().test_the_documented_command_is_the_one_that_was_run()

    # -- gate 5: the examples/ documented command ----------------------------

    def _examples_root(self, tmp_path, monkeypatch, body: str):
        (tmp_path / "examples").mkdir(exist_ok=True)
        (tmp_path / "examples" / "README.md").write_text(body)
        monkeypatch.setattr(TestCommittedExamplesAreReal, "ROOT", tmp_path)

    def test_gate_5_reads_a_wrapped_flag(self, monkeypatch, tmp_path):
        wrapped = (
            "ratemyagent scan --target mock --profile failing --requests 40\n"
            "  --concurrency 16 --fault-rate 0.3 --seed\n  42\n"
        )
        assert re.search(re.escape("--seed 42"), wrapped) is None, (
            "the fixture is not actually wrapped"
        )
        self._examples_root(tmp_path, monkeypatch, wrapped)

        TestCommittedExamplesAreReal().test_the_documented_command_is_the_one_that_was_run()

    def test_gate_5_still_fails_on_a_missing_flag(self, monkeypatch, tmp_path):
        self._examples_root(tmp_path, monkeypatch, "ratemyagent scan --profile failing\n")

        with pytest.raises(AssertionError, match="no longer documents"):
            TestCommittedExamplesAreReal().test_the_documented_command_is_the_one_that_was_run()

    # -- gate 6: the generated-asset banner ----------------------------------

    def test_gate_6_reads_a_wrapped_banner(self, monkeypatch, tmp_path):
        wrapped = (
            "# Schema strictness\n\nGenerated by\n"
            "`tools/schema_strictness.py`. Do not\nedit by hand.\n"
        )
        assert re.search(re.escape("Do not edit by hand"), wrapped) is None, (
            "the fixture is not actually wrapped"
        )
        self._redirect(monkeypatch, TestStrictnessDocIsGenerated, "ASSET", tmp_path,
                       "schema_strictness.md", wrapped)

        TestStrictnessDocIsGenerated().test_it_says_it_is_generated()

    def test_gate_6_still_fails_on_a_missing_banner(self, monkeypatch, tmp_path):
        self._redirect(monkeypatch, TestStrictnessDocIsGenerated, "ASSET", tmp_path,
                       "schema_strictness.md", "# Schema strictness\n\njust a table\n")

        with pytest.raises(AssertionError):
            TestStrictnessDocIsGenerated().test_it_says_it_is_generated()


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
            # Phrase-matched: a flag and its value are two words, and a fence
            # this long is exactly where an editor wraps.
            assert contains_phrase(documented, flag), (
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
            assert contains_phrase(documented, flag), (
                f"examples/README.md no longer documents {flag}"
            )


class TestBeforeStatesSurvive:
    """Evidence for a finding is not a scratch file. Name it, and check it.

    `assets/round3/firecrawl.txt` held the before-state for the largest finding
    since the retracted crash claim -- 49 crashes that the fix turned into 0 --
    and a bulk `cp` of the re-scan overwrote it. It survived only because a job
    temp directory still had a copy. Nothing gated it.

    Same shape as the root `REPORT.md` being destroyed by the next scan, which
    is why `assets/round3/` exists at all: the lesson was learned for scans
    written by the tool and not for evidence copied by hand.

    Two guards, because they fail differently. The files are `chmod 444`, which
    stops the accident at the point it happens; this test stops a *deletion*,
    which read-only permissions do not (removing a read-only file needs only a
    writable directory). Neither is sufficient and neither subsumes the other.

    Skips when `assets/` is absent -- gitignored working material, so local-only
    by construction, the same stated weakness the section 9 checks carry.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    ROUND3 = ROOT / "assets" / "round3"

    #: Before-states that are load-bearing for a written-up finding. Adding a
    #: pair to the notes means adding its name here; that coupling is the point.
    REQUIRED = {
        "firecrawl-before-0.1.20.txt": (
            "49 crashes and 8 clean rejections, before a JSON-RPC error was "
            "treated as delivered. The 'after' is meaningless alone."
        ),
    }

    @pytest.mark.skipif(not ROUND3.exists(), reason="assets/ is gitignored")
    def test_every_named_before_state_is_still_there(self):
        missing = {
            name: why for name, why in self.REQUIRED.items()
            if not (self.ROUND3 / name).exists()
        }
        assert not missing, (
            "a before-state named as evidence is gone:\n"
            + "\n".join(f"  {n}: {w}" for n, w in missing.items())
            + "\nIt is the half of a comparison that cannot be regenerated -- "
            "re-running the scan produces the after, never the before."
        )

    @pytest.mark.skipif(not ROUND3.exists(), reason="assets/ is gitignored")
    def test_they_are_not_writable(self):
        """`chmod 444` is the guard against the accident that happened."""
        writable = [
            name for name in self.REQUIRED
            if (self.ROUND3 / name).exists()
            and os.access(self.ROUND3 / name, os.W_OK)
        ]
        assert not writable, (
            f"before-states are writable and a bulk copy will overwrite them: "
            f"{writable}. `chmod 444 assets/round3/*-before-*`"
        )

    @pytest.mark.skipif(not ROUND3.exists(), reason="assets/ is gitignored")
    def test_a_before_state_is_not_empty(self):
        """A truncating overwrite leaves the name and destroys the evidence."""
        for name in self.REQUIRED:
            path = self.ROUND3 / name
            if not path.exists():
                continue
            assert path.stat().st_size > 200, (
                f"{name} is {path.stat().st_size} bytes -- present but emptied, "
                "which passes an existence check and fails the reader"
            )

    @pytest.mark.skipif(not ROUND3.exists(), reason="assets/ is gitignored")
    def test_every_before_file_on_disk_is_named_here(self):
        """The inverse direction: an unlisted pair is an ungated one.

        Without this the list is a subset nobody maintains, and the next
        before-state gets the protection of being mentioned in prose.
        """
        on_disk = {p.name for p in self.ROUND3.glob("*-before-*")}
        unlisted = on_disk - set(self.REQUIRED)
        assert not unlisted, (
            f"before-states exist that no test names: {sorted(unlisted)}. Add "
            "them to REQUIRED with what they are evidence for, or they are one "
            "bulk copy from gone."
        )


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


class TestStrictnessDocIsGenerated:
    """The asset needed three corrections in one week and was about to need a
    fourth. It is generated now, and this is the gate that says so.

    Network-dependent -- these are live hosted servers -- so like the section 9
    checks it cannot run in CI. It runs locally when `assets/` is present, which
    is the same weakness those checks have and is stated rather than hidden.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    ASSET = ROOT / "assets" / "schema_strictness.md"

    @pytest.mark.skipif(not ASSET.exists(), reason="assets/ is gitignored")
    def test_it_says_it_is_generated(self):
        head = self.ASSET.read_text()[:400]
        # Phrase-matched: the generator is free to reflow its own banner, and a
        # banner that wrapped would otherwise read as a missing banner.
        assert contains_phrase(head, "Generated by `tools/schema_strictness.py`")
        assert contains_phrase(head, "Do not edit by hand")

    @pytest.mark.skipif(not ASSET.exists(), reason="assets/ is gitignored")
    def test_the_generator_exists_and_offers_a_check_mode(self):
        source = (self.ROOT / "tools" / "schema_strictness.py").read_text()
        assert "--check" in source and "--write" in source


class TestTheFrozenSurfaceIsWhatTheDocumentSays:
    """Rule 3 of the promotion procedure, applied to the procedure itself.

    `docs/API-STABILITY.md` freezes eleven metric names, and says they are the
    ones a `ThresholdSpec` reads. If a spec is added, renamed or dropped without
    the document moving, the freeze silently stops describing the code -- which
    is the failure this project has had three times, most recently a docstring
    that claimed `Response.delivered` was a fact it was not.

    Tracked, so unlike the `assets/` checks this one runs in CI.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    DOC = ROOT / "docs" / "API-STABILITY.md"

    def test_the_documented_names_are_exactly_the_scored_ones(self):
        from ratemyagent.policy import THRESHOLD_SPECS

        expected = {s.metric for s in THRESHOLD_SPECS}
        expected |= {s.derived_from for s in THRESHOLD_SPECS if s.derived_from}

        block = self.DOC.read_text().split("```")[1]
        documented = set(block.split())

        assert documented == expected, (
            "docs/API-STABILITY.md lists the frozen metric names and the code "
            f"disagrees.\n  only in the doc:  {sorted(documented - expected)}\n"
            f"  only in the code: {sorted(expected - documented)}"
        )

    def test_the_count_in_the_prose_matches_the_list(self):
        """The doc says 'eleven', and says it was 'twelve' in the first draft."""
        from ratemyagent.policy import THRESHOLD_SPECS

        count = len({s.metric for s in THRESHOLD_SPECS}) + len(
            {s.derived_from for s in THRESHOLD_SPECS if s.derived_from}
        )
        words = {11: "eleven", 12: "twelve", 13: "thirteen", 10: "ten"}
        assert words[count] in self.DOC.read_text().lower(), (
            f"the frozen list has {count} names and the prose does not say so"
        )

    def test_every_frozen_result_shape_still_exports(self):
        """A frozen field that vanishes from to_dict() thaws silently."""
        from ratemyagent.models import Caveat, CheckResult

        check = CheckResult(
            name="n", probe="p", metric="m", direction="min", threshold=1.0,
            observed=1.0, score=100.0, passed=True, reason="r",
        )
        assert "threshold_source" in check.to_dict()

        caveat = Caveat(probe="p", metrics=("m",), effect="suppress", reason="r")
        exported = caveat.to_dict()
        for field in ("probe", "metrics", "effect", "reason", "scope"):
            assert field in exported, f"Caveat.{field} is frozen and is not exported"
