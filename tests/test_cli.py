"""CLI surface. Every case here runs against the built-in mock target."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ratemyagent import __version__
from ratemyagent.cli import cli


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

    def test_probes_command_separates_available_from_planned(self, run):
        result = run("probes")
        assert result.exit_code == 0
        assert "latency" in result.output
        assert "fault" in result.output


class TestScan:
    def test_mock_scan_prints_a_scorecard(self, run):
        result = run("scan", "--target", "mock", "--requests", "10")

        assert result.exit_code == 0
        assert "RateMyAgent Scan Results" in result.output
        assert "Latency" in result.output
        assert "Overall:" in result.output

    def test_healthy_mock_grades_a_on_latency_alone(self, run):
        result = run("scan", "--target", "mock", "--profile", "healthy",
                     "--requests", "20", "--probes", "latency")
        assert "Overall: A" in result.output

    def test_cost_shows_na_without_a_price(self, run):
        """A target with no published price is not graded on cost."""
        result = run("scan", "--target", "mock", "--requests", "10", "--probes", "cost")
        assert "Cost .................. n/a" in result.output

    def test_cost_is_graded_once_prices_are_given(self, run):
        result = run("scan", "--target", "mock", "--profile", "bloated",
                     "--requests", "10", "--probes", "cost",
                     "--price-in", "5", "--price-out", "25")
        assert "n/a" not in result.output
        assert "/req" in result.output

    def test_saturating_profile_reports_a_saturation_point(self, run):
        result = run("scan", "--target", "mock", "--profile", "saturating",
                     "--requests", "16", "--concurrency", "32", "--probes", "concurrency")
        assert "Saturation point is" in result.output

    def test_failing_mock_grades_f(self, run):
        result = run("scan", "--target", "mock", "--profile", "failing", "--requests", "20")
        assert "Overall: F" in result.output

    def test_scorecard_lists_probes_that_did_not_run(self, run):
        result = run("scan", "--target", "mock", "--requests", "5")
        assert "Not run (" in result.output
        assert "behavior" in result.output

    def test_scorecard_groups_results_by_phase(self, run):
        result = run("scan", "--target", "mock", "--requests", "10")

        assert "Phase 1  baseline" in result.output
        assert "Phase 2  chaos" in result.output
        assert result.output.index("Phase 1") < result.output.index("Phase 2")

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
        first = run("scan", "--target", "mock", "--profile", "degraded", "--seed", "7")
        second = run("scan", "--target", "mock", "--profile", "degraded", "--seed", "7")
        assert first.output == second.output

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

    def test_unimplemented_probe_names_its_week(self, run):
        result = run("scan", "--target", "mock", "--probes", "behavior")
        assert result.exit_code != 0
        assert "week 4" in result.output

    def test_unknown_probe_is_rejected(self, run):
        result = run("scan", "--target", "mock", "--probes", "nonsense")
        assert result.exit_code != 0
        assert "unknown probe" in result.output

    def test_unimplemented_output_is_rejected(self, run):
        result = run("scan", "--target", "mock", "--output", "report")
        assert result.exit_code != 0
        assert "week 4" in result.output

    def test_agents_md_output_is_rejected(self, run):
        result = run("scan", "--target", "mock", "--output", "agents-md")
        assert result.exit_code != 0
        assert "week 5" in result.output

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
