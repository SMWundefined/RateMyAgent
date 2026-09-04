"""CLI surface. Every case here runs against the built-in mock target."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ratemyagent import __version__
from ratemyagent.cli import cli
from tests.conftest import verdict_tail


@pytest.fixture
def run():
    runner = CliRunner()
    return lambda *args: runner.invoke(cli, list(args))


class TestHelp:
    def test_group_help(self, run):
        result = run("--help")
        assert result.exit_code == 0
        assert "scan" in result.output

    def test_scan_help_lists_the_documented_options(self, run):
        result = run("scan", "--help")
        assert result.exit_code == 0
        for option in ("--target", "--uri", "--probes", "--output", "--requests",
                       "--concurrency", "--seed", "--phases", "--fault-rate"):
            assert option in result.output

    def test_version(self, run):
        result = run("--version")
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_probes_command_lists_every_probe(self, run):
        result = run("probes")
        assert result.exit_code == 0
        for name in ("latency", "cost", "concurrency", "contract", "fault", "behavior"):
            assert name in result.output


class TestScan:
    def test_mock_scan_prints_a_scorecard(self, run):
        result = run("scan", "--target", "mock", "--requests", "10")

        assert result.exit_code == 0
        assert "RateMyAgent Scan Results" in result.output
        assert "Latency" in result.output
        assert "Score:" in result.output
        assert "Score breakdown:" in result.output

    def test_a_fast_target_scores_full_marks_on_latency(self, run):
        result = run("scan", "--target", "mock", "--profile", "healthy",
                     "--requests", "20", "--probes", "latency")
        assert "Score: 100/100" in result.output
        assert "PASS: score" in result.output

    def test_cost_shows_na_without_a_price(self, run):
        """A target with no published price is not graded on cost."""
        result = run("scan", "--target", "mock", "--requests", "10", "--probes", "cost")
        assert "n/a" in result.output

    def test_cost_is_graded_once_prices_are_given(self, run):
        result = run("scan", "--target", "mock", "--profile", "bloated",
                     "--requests", "10", "--probes", "cost",
                     "--price-in", "5", "--price-out", "25")
        assert "/req" in result.output

    def test_saturating_profile_reports_a_saturation_point(self, run):
        result = run("scan", "--target", "mock", "--profile", "saturating",
                     "--requests", "16", "--concurrency", "32", "--probes", "concurrency")
        assert "Saturation point is" in result.output

    def test_a_broken_target_fails_the_policy(self, run):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20")
        assert "FAIL: score" in result.output

    def test_scorecard_shows_every_phase(self, run):
        result = run("scan", "--target", "mock", "--requests", "5")
        assert "Behavior" in result.output

    def test_scorecard_groups_results_by_phase(self, run):
        result = run("scan", "--target", "mock", "--requests", "10")

        assert "Phase 1  baseline" in result.output
        assert "Phase 2  chaos" in result.output
        assert "Phase 3  behavior" in result.output
        assert result.output.index("Phase 1") < result.output.index("Phase 2")
        assert result.output.index("Phase 2") < result.output.index("Phase 3")

    def test_phases_can_be_limited_to_baseline(self, run):
        result = run("scan", "--target", "mock", "--requests", "10", "--phases", "baseline")

        assert result.exit_code == 0
        assert "Phase 1  baseline" in result.output
        assert "Phase 2  chaos" not in result.output

    def test_fault_rate_zero_injects_nothing(self, run):
        result = run("scan", "--target", "mock", "--requests", "10", "--fault-rate", "0")

        assert result.exit_code == 0
        assert "No faults were injected" in result.output

    def test_seed_makes_runs_reproducible(self, run):
        """Everything except wall clock: the Duration line is not seeded."""
        def measured(output: str) -> list[str]:
            return [line for line in output.splitlines() if "Duration:" not in line]

        first = run("scan", "--target", "mock", "--profile", "degraded", "--seed", "7")
        second = run("scan", "--target", "mock", "--profile", "degraded", "--seed", "7")

        assert measured(first.output) == measured(second.output)

    def test_json_out_writes_the_full_result(self, run, tmp_path):
        path = tmp_path / "nested" / "scan.json"
        result = run("scan", "--target", "mock", "--requests", "5", "--json-out", str(path))

        assert result.exit_code == 0
        payload = json.loads(path.read_text())
        assert payload["target"]["kind"] == "mock"
        assert payload["probes"][0]["metrics"]["requests"] == 5


class TestValidation:
    def test_target_is_required(self, run):
        assert run("scan").exit_code != 0

    def test_unknown_target_is_rejected(self, run):
        assert run("scan", "--target", "banana").exit_code != 0

    def test_llm_target_needs_a_provider(self, run):
        result = run("scan", "--target", "llm")
        assert result.exit_code != 0
        assert "--provider" in result.output

    def test_mcp_without_uri_is_rejected(self, run):
        result = run("scan", "--target", "mcp")
        assert result.exit_code != 0
        assert "--uri" in result.output

    def test_the_behavior_probe_runs_now_that_it_exists(self, run):
        result = run("scan", "--target", "mock", "--probes", "behavior")
        assert result.exit_code == 0

    def test_unknown_probe_is_rejected(self, run):
        result = run("scan", "--target", "mock", "--probes", "nonsense")
        assert result.exit_code != 0
        assert "unknown probe" in result.output

    @pytest.mark.parametrize("args", [("--requests", "0"), ("--warmup", "-1")])
    def test_nonsense_counts_are_rejected(self, run, args):
        assert run("scan", "--target", "mock", *args).exit_code != 0

    @pytest.mark.parametrize("rate", ["-0.1", "1.5"])
    def test_fault_rate_out_of_range_is_rejected(self, run, rate):
        result = run("scan", "--target", "mock", "--fault-rate", rate)
        assert result.exit_code != 0
        assert "--fault-rate" in result.output

    def test_unknown_phase_is_rejected(self, run):
        result = run("scan", "--target", "mock", "--phases", "nonsense")
        assert result.exit_code != 0
        assert "unknown phase" in result.output

    def test_malformed_tool_args_are_rejected(self, run):
        result = run("scan", "--target", "mock", "--tool-args", "{not json}")
        assert result.exit_code != 0
        assert "valid JSON" in result.output

    def test_non_object_tool_args_are_rejected(self, run):
        result = run("scan", "--target", "mock", "--tool-args", "[1, 2]")
        assert result.exit_code != 0
        assert "JSON object" in result.output


class TestCiCommand:
    """The retention mechanism: a gate that a pipeline can act on."""

    def test_exits_zero_when_the_policy_passes(self, run, tmp_path):
        policy = tmp_path / "easy.yaml"
        policy.write_text("name: easy\nthresholds:\n  p95_latency_ms: 60000\npass_score: 10\n")

        result = run("ci", "--target", "mock", "--profile", "healthy",
                     "--requests", "10", "--policy", str(policy))

        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_exits_one_when_the_policy_fails(self, run, tmp_path):
        policy = tmp_path / "strict.yaml"
        policy.write_text("name: strict\nthresholds:\n  p95_latency_ms: 1\npass_score: 99\n")

        result = run("ci", "--target", "mock", "--profile", "degraded",
                     "--requests", "10", "--policy", str(policy))

        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_failing_checks_are_named(self, run, tmp_path):
        policy = tmp_path / "strict.yaml"
        policy.write_text("name: strict\nthresholds:\n  p95_latency_ms: 1\npass_score: 99\n")

        result = run("ci", "--target", "mock", "--profile", "degraded",
                     "--requests", "10", "--policy", str(policy))

        assert "p95_latency_ms" in result.output

    def test_exits_two_when_the_scan_could_not_run(self, run, tmp_path):
        """A broken scanner must not look like a failing target."""
        missing = tmp_path / "nope.yaml"
        result = run("ci", "--target", "mock", "--policy", str(missing))

        assert result.exit_code == 2

    def test_quiet_prints_only_the_verdict(self, run):
        result = run("ci", "--target", "mock", "--requests", "5", "--quiet")

        assert "RateMyAgent Scan Results" not in result.output
        assert "score" in result.output

    def test_uses_the_shipped_policy_by_default(self, run):
        result = run("ci", "--target", "mock", "--requests", "5", "--quiet")
        assert "production-default" in result.output

    def test_json_out_is_written(self, run, tmp_path):
        path = tmp_path / "ci.scan.json"
        run("ci", "--target", "mock", "--requests", "5", "--quiet", "--json-out", str(path))

        import json
        payload = json.loads(path.read_text())
        assert payload["policy"] == "production-default"
        assert 0 <= payload["score"] <= 100


class TestPolicyCommand:
    def test_shows_the_default_policy(self, run):
        result = run("policy")

        assert result.exit_code == 0
        assert "production-default" in result.output
        assert "p95_latency_ms" in result.output

    def test_shows_which_probe_metric_each_threshold_reads(self, run):
        result = run("policy")
        assert "latency.p95_s" in result.output

    def test_reports_a_bad_policy_file(self, run, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: x\nthresholds:\n  nonsense_key: 1\n")

        result = run("policy", "--policy", str(bad))
        assert result.exit_code != 0
        assert "unknown threshold" in result.output


class TestOutputFormats:
    def test_report_is_written(self, run, tmp_path):
        path = tmp_path / "REPORT.md"
        result = run("scan", "--target", "mock", "--requests", "10",
                     "--output", "report", "--report-out", str(path))

        assert result.exit_code == 0
        assert path.exists()
        assert path.read_text().startswith("# RateMyAgent report")
        assert f"Report written to {path}" in result.output

    def test_agents_md_is_written(self, run, tmp_path):
        path = tmp_path / "AGENTS.md"
        result = run("scan", "--target", "mock", "--requests", "10",
                     "--output", "agents-md", "--agents-md-out", str(path))

        assert result.exit_code == 0
        assert path.read_text().startswith("# AGENTS.md")

    def test_report_output_does_not_print_the_scorecard(self, run, tmp_path):
        result = run("scan", "--target", "mock", "--requests", "10", "--output", "report",
                     "--report-out", str(tmp_path / "R.md"))
        assert "RateMyAgent Scan Results" not in result.output

    def test_output_all_writes_everything(self, run, tmp_path):
        report, agents = tmp_path / "R.md", tmp_path / "A.md"
        result = run("scan", "--target", "mock", "--requests", "10", "--output", "all",
                     "--report-out", str(report), "--agents-md-out", str(agents))

        assert result.exit_code == 0
        assert "RateMyAgent Scan Results" in result.output
        assert report.exists() and agents.exists()

    def test_rescanning_produces_deltas(self, run, tmp_path):
        path = tmp_path / "AGENTS.md"
        args = ("scan", "--target", "mock", "--requests", "10",
                "--output", "agents-md", "--agents-md-out", str(path))

        run(*args, "--profile", "degraded")
        run(*args, "--profile", "healthy")

        assert "## Since the last scan" in path.read_text()

    def test_parent_directories_are_created(self, run, tmp_path):
        path = tmp_path / "deep" / "nested" / "REPORT.md"
        run("scan", "--target", "mock", "--requests", "5", "--output", "report",
            "--report-out", str(path))
        assert path.exists()


class TestScorecardFormat:
    def test_shows_actual_against_target(self, run):
        result = run("scan", "--target", "mock", "--requests", "10")

        assert "actual" in result.output and "target" in result.output
        assert "Score breakdown:" in result.output

    def test_ends_with_the_verdict(self, run):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "10")
        tail = verdict_tail(result.output)[-2:]

        assert tail[0].startswith("FAIL: score")
        assert tail[1].startswith("Biggest gaps:")


class TestAgentsMdHint:
    """The scorecard should point at the fix guide, without ever asking."""

    def test_hint_appears_when_there_is_something_to_fix(self, run):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20")

        assert "Run with --output agents-md to generate a fix guide." in result.output
        assert "findings across" in result.output

    def test_hint_sits_above_the_verdict(self):
        """CLAUDE.md: the last two lines are the verdict. The hint must not
        displace what a CI log gets grepped for."""
        from click.testing import CliRunner

        from ratemyagent.cli import cli

        result = CliRunner().invoke(
            cli, ["scan", "--target", "mock", "--profile", "failing", "--requests", "20"]
        )
        lines = verdict_tail(result.output)

        assert lines[-2].startswith("FAIL: score")
        assert lines[-1].startswith("Biggest gaps:")
        assert any("fix guide" in line for line in lines[:-2])

    def test_hint_counts_findings_and_probes(self, run):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20")
        hint = [line for line in result.output.splitlines() if "fix guide" in line][0]

        import re
        match = re.match(r"(\d+) findings across (\d+) probes\.", hint)
        assert match, hint
        assert int(match.group(1)) > 0 and int(match.group(2)) > 0

    def test_no_hint_when_agents_md_was_already_written(self, run, tmp_path):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20",
                     "--output", "agents-md", "--agents-md-out", str(tmp_path / "A.md"))

        assert "Run with --output agents-md" not in result.output

    def test_no_hint_when_the_guide_would_be_empty(self, run):
        """Do not send someone to a file that says "nothing to fix"."""
        result = run("scan", "--target", "mock", "--requests", "10", "--probes", "latency")
        assert "fix guide" not in result.output

    def test_writing_reports_the_path_and_counts(self, run, tmp_path):
        path = tmp_path / "AGENTS.md"
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20",
                     "--output", "agents-md", "--agents-md-out", str(path))

        assert f"AGENTS.md written to {path}" in result.output
        assert "recommendation" in result.output
        assert "critical" in result.output

    def test_recommendation_count_matches_the_file(self, run, tmp_path):
        path = tmp_path / "AGENTS.md"
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20",
                     "--output", "agents-md", "--agents-md-out", str(path))

        import re
        count = int(re.search(r"\((\d+) recommendation", result.output).group(1))
        assert path.read_text().count("\n### ") == count

    def test_report_output_reports_its_path(self, run, tmp_path):
        path = tmp_path / "REPORT.md"
        run_result = run("scan", "--target", "mock", "--requests", "10",
                         "--output", "report", "--report-out", str(path))

        assert f"Report written to {path}" in run_result.output


class TestCiHasNoSideEffects:
    """CI is a gate: pass/fail, nothing written unless explicitly asked."""

    def test_ci_never_writes_agents_md(self, run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run("ci", "--target", "mock", "--profile", "failing", "--requests", "10")

        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "REPORT.md").exists()

    def test_ci_has_no_output_flag(self, run):
        result = run("ci", "--target", "mock", "--output", "agents-md")
        assert result.exit_code != 0

    def test_ci_does_not_suggest_the_fix_guide(self, run):
        """A CI log is machine-read; a suggestion there is noise."""
        result = run("ci", "--target", "mock", "--profile", "failing",
                     "--requests", "10", "--quiet")
        assert "fix guide" not in result.output

    def test_ci_writes_json_only_when_asked(self, run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run("ci", "--target", "mock", "--requests", "10", "--quiet")

        assert list(tmp_path.iterdir()) == []


class TestNonInteractive:
    def test_no_command_prompts_for_input(self, run):
        """SRE tools have to stay pipeable: nothing may block on stdin."""
        for args in (
            ("scan", "--target", "mock", "--requests", "5"),
            ("ci", "--target", "mock", "--requests", "5", "--quiet"),
            ("policy",),
            ("probes",),
        ):
            result = run(*args)
            # CliRunner supplies empty stdin; a prompt would raise or hang.
            assert result.exit_code in (0, 1), f"{args} -> {result.output}"

    def test_scorecard_output_has_no_trailing_blank_line(self, run):
        result = run("scan", "--target", "mock", "--requests", "5", "--probes", "latency")
        assert not result.output.endswith("\n\n")
