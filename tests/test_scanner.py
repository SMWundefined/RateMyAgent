"""Scan orchestration."""

from __future__ import annotations

import pytest

from ratemyagent import scan
from ratemyagent.models import Grade
from ratemyagent.probes import FaultInjector, LatencyProfiler, ProbeConfig
from ratemyagent.targets import MockTarget
from tests.conftest import ScriptedTarget


async def test_scan_returns_a_result_per_probe(healthy_target, config):
    result = await scan(healthy_target, config=config)

    assert [p.probe for p in result.probes] == [
        "latency", "cost", "concurrency", "contract", "fault"
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
    assert result.probes[0].grade is Grade.F


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


async def test_unimplemented_probe_is_named_in_the_error(healthy_target, config):
    with pytest.raises(KeyError, match="week 4"):
        await scan(healthy_target, probes="behavior", config=config)


async def test_unknown_probe_is_rejected(healthy_target, config):
    with pytest.raises(KeyError, match="unknown probe"):
        await scan(healthy_target, probes="nonsense", config=config)


async def test_parallel_mode_runs_the_same_probes(healthy_target, config):
    result = await scan(healthy_target, config=config, parallel=True)

    assert {p.probe for p in result.probes} == {
        "latency", "cost", "concurrency", "contract", "fault"
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


async def test_overall_grade_reflects_target_health(config):
    good = await scan(MockTarget.healthy(), config=config)
    bad = await scan(MockTarget.failing(), config=config)

    assert good.overall_grade.points > bad.overall_grade.points
    assert bad.overall_grade is Grade.F


async def test_latency_alone_still_grades_a_for_a_fast_target(config):
    result = await scan(MockTarget.healthy(), probes="latency", config=config)

    assert result.overall_grade is Grade.A


async def test_thin_evidence_holds_the_overall_grade_down(config):
    """Several probes cap themselves when the evidence is thin, and it compounds.

    The alternative -- dropping inconclusive probes from the average -- would
    report an A for a target whose failure handling and concurrency limit were
    never really tested.
    """
    result = await scan(MockTarget.healthy(), config=config)

    fault = next(p for p in result.probes if p.probe == "fault")
    concurrency = next(p for p in result.probes if p.probe == "concurrency")

    assert fault.grade is Grade.C
    assert any("capped at C" in f for f in fault.findings)
    # Ceiling of 5 in the fixture: we never asked it for more, so we cannot
    # claim it handles more.
    assert concurrency.grade is Grade.C
    assert any("floor set by the test" in f for f in concurrency.findings)
    assert result.overall_grade is Grade.C


async def test_a_validating_target_outscores_a_permissive_one(config):
    """The contract probe is the only difference between these two."""
    from tests.conftest import ValidatingTarget

    permissive = await scan(MockTarget.healthy(), config=config)
    validating = await scan(ValidatingTarget(), config=config)

    assert permissive.probe("contract").grade is Grade.C
    assert validating.probe("contract").grade is Grade.A
    assert validating.overall_grade.points > permissive.overall_grade.points


async def test_an_unpriceable_probe_is_excluded_from_the_overall(config):
    """Cost against a target with no price is not a C, it is not applicable."""
    result = await scan(MockTarget.healthy(), config=config)

    cost = result.probe("cost")
    assert cost.applicable is False

    graded = [p.grade for p in result.probes if p.applicable]
    assert result.overall_grade is Grade.average(graded)


async def test_result_serializes_to_json(healthy_target, config):
    import json

    payload = json.loads(json.dumps((await scan(healthy_target, config=config)).to_dict()))

    assert payload["target"]["kind"] == "mock"
    assert payload["probes"][0]["probe"] == "latency"
    assert payload["overall_grade"] in {"A", "B", "C", "D", "F"}


class TestPhasePipeline:
    async def test_default_scan_runs_baseline_then_chaos(self, healthy_target, config):
        result = await scan(healthy_target, config=config)

        assert [p.phase for p in result.probes] == [
            "baseline", "baseline", "baseline", "baseline", "chaos"
        ]
        assert [p.probe for p in result.probes][0] == "latency"
        assert [p.probe for p in result.probes][-1] == "fault"

    async def test_phase_order_is_fixed_regardless_of_request_order(
        self, healthy_target, config
    ):
        """Phase 2 without phase 1 first has nothing to compare against."""
        result = await scan(healthy_target, phases=["chaos", "baseline"], config=config)

        assert result.probes[0].phase == "baseline"
        assert result.probes[-1].phase == "chaos"

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

    async def test_selecting_an_empty_phase_yields_no_results(self, healthy_target, config):
        result = await scan(healthy_target, phases="behavior", config=config)

        assert result.probes == []

    async def test_unknown_phase_is_rejected(self, healthy_target, config):
        with pytest.raises(KeyError, match="unknown phase"):
            await scan(healthy_target, phases="nonsense", config=config)

    async def test_phases_are_recorded_on_the_result(self, healthy_target, config):
        result = await scan(healthy_target, phases="baseline", config=config)

        assert result.config["phases"] == ["baseline"]

    async def test_both_phases_serialize(self, healthy_target, config):
        import json

        payload = json.loads(json.dumps((await scan(healthy_target, config=config)).to_dict()))

        assert {p["phase"] for p in payload["probes"]} == {"baseline", "chaos"}

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
