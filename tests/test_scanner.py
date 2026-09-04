"""Scan orchestration."""

from __future__ import annotations

import pytest

from ratemyagent import Policy, scan
from ratemyagent.probes import FaultInjector, LatencyProfiler, ProbeConfig
from ratemyagent.targets import MockTarget
from tests.conftest import ScriptedTarget


async def test_scan_returns_a_result_per_probe(healthy_target, config):
    result = await scan(healthy_target, config=config)

    assert [p.probe for p in result.probes] == [
        "latency", "cost", "concurrency", "contract", "fault", "behavior"
    ]
    assert result.target.kind == "mock"
    assert result.duration_s > 0


async def test_scan_manages_the_target_lifecycle(config):
    target = ScriptedTarget.from_latencies([0.5])
    await scan(target, config=config)

    assert target.setup_calls == 1
    assert target.teardown_calls == 1


async def test_target_is_torn_down_even_when_a_probe_explodes(config, monkeypatch):
    target = ScriptedTarget.from_latencies([0.5])

    async def boom(self, target, config):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(LatencyProfiler, "run", boom)
    result = await scan(target, config=config)

    assert target.teardown_calls == 1
    assert result.probes[0].failed is True
    assert result.probes[0].applicable is False


async def test_setup_failure_propagates(config):
    class BadTarget(MockTarget):
        async def setup(self):
            raise RuntimeError("cannot connect")

    with pytest.raises(RuntimeError, match="cannot connect"):
        await scan(BadTarget(), config=config)


async def test_probes_can_be_selected_by_name(healthy_target, config):
    result = await scan(healthy_target, probes="latency", config=config)
    assert len(result.probes) == 1


async def test_probes_can_be_passed_as_instances(healthy_target, config):
    result = await scan(healthy_target, probes=[LatencyProfiler()], config=config)
    assert result.probes[0].probe == "latency"


async def test_unknown_probe_is_rejected(healthy_target, config):
    with pytest.raises(KeyError, match="unknown probe"):
        await scan(healthy_target, probes="nonsense", config=config)


async def test_parallel_mode_runs_the_same_probes(healthy_target, config):
    result = await scan(healthy_target, config=config, parallel=True)

    assert {p.probe for p in result.probes} == {
        "latency", "cost", "concurrency", "contract", "fault", "behavior"
    }
    assert result.config["parallel"] is True


async def test_config_is_recorded_on_the_result(healthy_target):
    config = ProbeConfig(requests=7, warmup=2, timeout_s=3.0, seed=42)
    result = await scan(healthy_target, config=config)

    assert result.config["requests"] == 7
    assert result.config["seed"] == 42
    assert result.probes[0].sample_count == 7


async def test_default_config_is_used_when_none_given(healthy_target):
    result = await scan(healthy_target)
    assert result.config["requests"] == 20


async def test_score_reflects_target_health(config):
    good = await scan(MockTarget.healthy(), config=config)
    bad = await scan(MockTarget.failing(), config=config)

    assert good.score > bad.score
    assert bad.passed is False


async def test_a_scan_is_scored_against_the_default_policy(config):
    result = await scan(MockTarget.healthy(), config=config)

    assert result.policy_name == "production-default"
    assert result.pass_score == 75
    assert 0 <= result.score <= 100
    assert result.passed is (result.score >= result.pass_score)


async def test_every_check_is_attached_to_the_probe_that_measured_it(config):
    result = await scan(MockTarget.healthy(), config=config)

    for check in result.checks:
        owner = result.probe(check.probe)
        assert owner is not None
        assert check in owner.checks


async def test_a_custom_policy_changes_the_verdict(config, tmp_path):
    """The point of a policy: the same scan, judged differently."""
    strict = tmp_path / "strict.yaml"
    strict.write_text(
        "name: strict\nthresholds:\n  p95_latency_ms: 1\npass_score: 99\n"
    )
    lenient = tmp_path / "lenient.yaml"
    lenient.write_text(
        "name: lenient\nthresholds:\n  p95_latency_ms: 60000\npass_score: 10\n"
    )

    tight = await scan(MockTarget.degraded(), config=config, policy=Policy.load(strict))
    loose = await scan(MockTarget.degraded(), config=config, policy=Policy.load(lenient))

    assert tight.passed is False
    assert loose.passed is True


async def test_a_probe_that_could_not_measure_is_skipped_not_failed(config):
    """Cost against a target with no price must not drag the score down."""
    result = await scan(MockTarget.healthy(), config=config)

    cost_checks = [c for c in result.checks if c.probe == "cost"]
    assert cost_checks and all(c.skipped for c in cost_checks)
    assert all(c.passed for c in cost_checks)


async def test_result_serializes_to_json(healthy_target, config):
    import json

    payload = json.loads(json.dumps((await scan(healthy_target, config=config)).to_dict()))

    assert payload["target"]["kind"] == "mock"
    assert payload["probes"][0]["probe"] == "latency"
    assert 0 <= payload["score"] <= 100
    assert payload["policy"] == "production-default"
    assert isinstance(payload["passed"], bool)


class TestPhasePipeline:
    async def test_default_scan_runs_baseline_then_chaos(self, healthy_target, config):
        result = await scan(healthy_target, config=config)

        assert [p.phase for p in result.probes] == [
            "baseline", "baseline", "baseline", "baseline", "chaos", "behavior"
        ]
        assert [p.probe for p in result.probes][0] == "latency"
        assert [p.probe for p in result.probes][-1] == "behavior"

    async def test_phase_order_is_fixed_regardless_of_request_order(
        self, healthy_target, config
    ):
        """Phase 2 without phase 1 first has nothing to compare against."""
        result = await scan(healthy_target, phases=["behavior", "baseline"], config=config)

        assert result.probes[0].phase == "baseline"
        assert result.probes[-1].phase == "behavior"

    async def test_phase_order_is_fixed_regardless_of_probe_order(
        self, healthy_target, config
    ):
        result = await scan(
            healthy_target, probes=[FaultInjector(), LatencyProfiler()], config=config
        )

        assert [p.phase for p in result.probes] == ["baseline", "chaos"]
        assert [p.probe for p in result.probes] == ["latency", "fault"]

    async def test_a_single_phase_can_be_selected(self, healthy_target, config):
        result = await scan(healthy_target, phases="baseline", config=config)

        assert [p.probe for p in result.probes] == [
            "latency", "cost", "concurrency", "contract"
        ]

    async def test_a_phase_with_no_probes_selected_yields_no_results(
        self, healthy_target, config
    ):
        result = await scan(
            healthy_target, probes="latency", phases="chaos", config=config
        )

        assert result.probes == []
        # The policy still reports what it could not measure.
        assert result.unmeasured_checks

    async def test_unknown_phase_is_rejected(self, healthy_target, config):
        with pytest.raises(KeyError, match="unknown phase"):
            await scan(healthy_target, phases="nonsense", config=config)

    async def test_phases_are_recorded_on_the_result(self, healthy_target, config):
        result = await scan(healthy_target, phases="baseline", config=config)

        assert result.config["phases"] == ["baseline"]

    async def test_both_phases_serialize(self, healthy_target, config):
        import json

        payload = json.loads(json.dumps((await scan(healthy_target, config=config)).to_dict()))

        assert {p["phase"] for p in payload["probes"]} == {
            "baseline", "chaos", "behavior"
        }

    async def test_target_is_torn_down_once_across_phases(self, config):
        target = ScriptedTarget.from_latencies([0.2])
        await scan(target, config=config)

        assert target.setup_calls == 1
        assert target.teardown_calls == 1

    async def test_chaos_phase_does_not_leak_faults_into_the_baseline(self, config):
        """Phase 1 must see the real target, or the comparison is meaningless."""
        result = await scan(MockTarget.healthy(), config=config)

        baseline = next(p for p in result.probes if p.phase == "baseline")
        assert baseline.error_rate == 0.0
