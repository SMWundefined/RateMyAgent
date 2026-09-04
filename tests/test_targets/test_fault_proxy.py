"""FaultProxy: injection, delegation, determinism, and recording.

The proxy is the only place faults come from, so its correctness is the
foundation for every phase 2 number. Everything here runs on mock targets.
"""

from __future__ import annotations

import pytest

from ratemyagent.models import ErrorKind, FaultKind, Request, Response
from ratemyagent.targets import FaultConfig, FaultProxy, MockTarget, wrap
from tests.conftest import ScriptedTarget

ALWAYS = 1.0


def only(kind: FaultKind, **kwargs) -> FaultConfig:
    """A config that injects exactly one kind of fault into every call."""
    return FaultConfig(rates={kind: ALWAYS}, **kwargs)


class TestFaultConfig:
    def test_off_injects_nothing(self):
        assert FaultConfig.off().active is False
        assert FaultConfig.off().total_rate == 0.0

    def test_uniform_spreads_the_total_across_kinds(self):
        config = FaultConfig.uniform(0.5)
        assert config.total_rate == pytest.approx(0.5)
        assert len(config.rates) == len(FaultKind)
        assert all(rate == pytest.approx(0.1) for rate in config.rates.values())

    def test_uniform_can_target_specific_kinds(self):
        config = FaultConfig.uniform(0.4, [FaultKind.TIMEOUT, FaultKind.RATE_LIMIT])
        assert set(config.rates) == {FaultKind.TIMEOUT, FaultKind.RATE_LIMIT}
        assert config.rates[FaultKind.TIMEOUT] == pytest.approx(0.2)

    @pytest.mark.parametrize("rate", [-0.1, 1.1])
    def test_out_of_range_rates_are_rejected(self, rate):
        with pytest.raises(ValueError):
            FaultConfig(rates={FaultKind.TIMEOUT: rate})

    def test_rates_summing_past_one_are_rejected(self):
        """At most one fault per call, so the rates are shares of a single draw."""
        with pytest.raises(ValueError, match="cannot exceed 1.0"):
            FaultConfig(rates={FaultKind.TIMEOUT: 0.7, FaultKind.SERVER_ERROR: 0.5})

    def test_non_fault_kind_keys_are_rejected(self):
        with pytest.raises(ValueError, match="FaultKind"):
            FaultConfig(rates={"timeout": 0.5})

    def test_uniform_rejects_empty_kinds(self):
        with pytest.raises(ValueError):
            FaultConfig.uniform(0.5, [])


class TestDelegation:
    async def test_lifecycle_forwards_to_inner(self):
        inner = ScriptedTarget.from_latencies([0.1])
        proxy = FaultProxy(inner, FaultConfig.off())

        await proxy.setup()
        await proxy.teardown()

        assert inner.setup_calls == 1
        assert inner.teardown_calls == 1

    async def test_probe_traffic_comes_from_the_inner_target(self):
        """Probes must generate the same requests they would unwrapped."""
        inner = MockTarget.healthy()
        proxy = FaultProxy(inner, FaultConfig.uniform(0.5))

        assert [r.label for r in proxy.probe_requests(3)] == [
            r.label for r in inner.probe_requests(3)
        ]
        assert proxy.sample_request(7).label == inner.sample_request(7).label

    def test_list_tools_forwards(self):
        inner = MockTarget.healthy()
        proxy = FaultProxy(inner, FaultConfig.off())
        assert [t.name for t in proxy.list_tools()] == [t.name for t in inner.list_tools()]

    def test_describe_marks_the_target_as_faulted(self):
        inner = MockTarget.healthy()
        info = FaultProxy(inner, FaultConfig.uniform(0.3)).describe()

        assert info.name == inner.describe().name
        assert info.metadata["fault_injected"] is True
        assert info.metadata["faults"]["total_rate"] == pytest.approx(0.3)

    def test_describe_does_not_mutate_the_inner_info(self):
        inner = MockTarget.healthy()
        FaultProxy(inner, FaultConfig.uniform(0.3)).describe()
        assert "fault_injected" not in inner.describe().metadata


class TestPassThrough:
    async def test_zero_rate_leaves_responses_untouched(self):
        inner = ScriptedTarget.from_latencies([0.25] * 5)
        proxy = FaultProxy(inner, FaultConfig.off())
        await proxy.setup()

        responses = [await proxy.invoke(r) for r in proxy.probe_requests(5)]

        assert all(r.ok for r in responses)
        assert all(r.latency_s == 0.25 for r in responses)
        assert proxy.injected_count == 0

    async def test_inner_failures_pass_through_untagged(self):
        """A real failure must not be relabelled as an injected one."""
        inner = ScriptedTarget.from_latencies([], failures=1)
        proxy = FaultProxy(inner, FaultConfig.off())
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert response.ok is False
        assert response.error_kind is ErrorKind.TIMEOUT
        assert proxy.invocations[0].injected is None


class TestInjectedFaults:
    @pytest.mark.parametrize(
        "kind,expected",
        [
            (FaultKind.TIMEOUT, ErrorKind.TIMEOUT),
            (FaultKind.RATE_LIMIT, ErrorKind.RATE_LIMIT),
            (FaultKind.SERVER_ERROR, ErrorKind.SERVER_ERROR),
            (FaultKind.CONNECTION_REFUSED, ErrorKind.CONNECTION),
            (FaultKind.MALFORMED, ErrorKind.INVALID_RESPONSE),
        ],
    )
    async def test_each_fault_maps_to_its_error_kind(self, kind, expected):
        proxy = FaultProxy(MockTarget.healthy(), only(kind))
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert response.ok is False
        assert response.error_kind is expected
        assert response.meta["injected"] == kind.value

    @pytest.mark.parametrize(
        "kind",
        [
            FaultKind.TIMEOUT,
            FaultKind.RATE_LIMIT,
            FaultKind.SERVER_ERROR,
            FaultKind.CONNECTION_REFUSED,
        ],
    )
    async def test_short_circuit_faults_never_reach_the_target(self, kind):
        inner = ScriptedTarget.from_latencies([0.1])
        proxy = FaultProxy(inner, only(kind))
        await proxy.setup()

        await proxy.invoke(proxy.sample_request(0))

        assert inner.calls == []

    async def test_timeout_reports_the_configured_wait(self):
        proxy = FaultProxy(MockTarget.healthy(), only(FaultKind.TIMEOUT, timeout_s=12.0))
        await proxy.setup()

        assert (await proxy.invoke(proxy.sample_request(0))).latency_s == 12.0

    async def test_rate_limit_carries_a_retry_after_hint(self):
        proxy = FaultProxy(MockTarget.healthy(), only(FaultKind.RATE_LIMIT, retry_after_s=2.5))
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert response.meta["status"] == 429
        assert response.meta["retry_after_s"] == 2.5

    async def test_malformed_still_reaches_the_target_and_pays_its_latency(self):
        """A truncated payload costs as much to produce as a valid one."""
        inner = ScriptedTarget.from_latencies([0.75])
        proxy = FaultProxy(inner, only(FaultKind.MALFORMED))
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert len(inner.calls) == 1
        assert response.latency_s == 0.75
        assert response.ok is False

    async def test_malformed_truncates_the_payload(self):
        inner = ScriptedTarget([Response(ok=True, latency_s=0.1, output="abcdefgh")])
        proxy = FaultProxy(inner, only(FaultKind.MALFORMED))
        await proxy.setup()

        assert (await proxy.invoke(proxy.sample_request(0))).output == "abcd"

    async def test_malformed_leaves_an_already_failed_response_alone(self):
        """Corrupting a real failure would overwrite an observation with a fake one."""
        inner = ScriptedTarget.from_latencies([], failures=1)
        proxy = FaultProxy(inner, only(FaultKind.MALFORMED))
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert response.error_kind is ErrorKind.TIMEOUT
        assert "injected" not in response.meta


class TestDeterminism:
    async def _injected(self, seed: int, n: int = 60) -> list[str | None]:
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.uniform(0.4, seed=seed))
        await proxy.setup()
        for request in proxy.probe_requests(n):
            await proxy.invoke(request)
        return [inv.injected.value if inv.injected else None for inv in proxy.invocations]

    async def test_same_seed_reproduces_the_run(self):
        assert await self._injected(42) == await self._injected(42)

    async def test_different_seeds_diverge(self):
        assert await self._injected(1) != await self._injected(2)

    async def test_injection_is_order_independent(self):
        """Seeded per trajectory, not from a shared cursor, so concurrency is safe."""
        requests = MockTarget.healthy().probe_requests(20)
        config = FaultConfig.uniform(0.5, seed=5)

        forward = FaultProxy(MockTarget.healthy(), config)
        await forward.setup()
        for request in requests:
            await forward.invoke(request)

        backward = FaultProxy(MockTarget.healthy(), config)
        await backward.setup()
        for request in reversed(requests):
            await backward.invoke(request)

        by_key = {inv.trajectory_id: inv.injected for inv in forward.invocations}
        assert all(by_key[inv.trajectory_id] == inv.injected for inv in backward.invocations)

    async def test_retries_draw_independently(self):
        """A request faulted once must be able to succeed on retry."""
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.uniform(0.5, seed=9))
        await proxy.setup()

        request = Request(op="echo", label="e#0", trajectory_id="t0")
        outcomes = [(await proxy.invoke(request)).ok for _ in range(8)]

        assert any(outcomes) and not all(outcomes)

    async def test_observed_rate_matches_configuration(self):
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.uniform(0.3, seed=3))
        await proxy.setup()
        n = 2000
        for request in proxy.probe_requests(n):
            await proxy.invoke(request)

        assert proxy.injected_count / n == pytest.approx(0.3, abs=0.03)


class TestRecording:
    async def test_every_call_is_recorded_in_order(self):
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.off())
        await proxy.setup()
        for request in proxy.probe_requests(5):
            await proxy.invoke(request)

        assert [inv.sequence for inv in proxy.invocations] == [0, 1, 2, 3, 4]

    async def test_repeat_calls_build_one_trajectory(self):
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.off())
        await proxy.setup()
        request = Request(op="echo", label="e#0", trajectory_id="t0")

        for _ in range(3):
            await proxy.invoke(request)

        assert len(proxy.trajectories) == 1
        trajectory = proxy.trajectories["t0"]
        assert trajectory.attempts == 3
        assert [inv.attempt for inv in trajectory.invocations] == [1, 2, 3]

    async def test_distinct_requests_get_distinct_trajectories(self):
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.off())
        await proxy.setup()
        for request in proxy.probe_requests(4):
            await proxy.invoke(request)

        assert len(proxy.trajectories) == 4

    async def test_injected_by_kind_counts_what_was_done(self):
        proxy = FaultProxy(MockTarget.healthy(), only(FaultKind.SERVER_ERROR))
        await proxy.setup()
        for request in proxy.probe_requests(6):
            await proxy.invoke(request)

        assert proxy.injected_by_kind() == {"server_error": 6}
        assert proxy.injected_count == 6

    async def test_reset_clears_history_but_keeps_config(self):
        proxy = FaultProxy(MockTarget.healthy(), FaultConfig.uniform(0.5, seed=1))
        await proxy.setup()
        for request in proxy.probe_requests(5):
            await proxy.invoke(request)

        proxy.reset()

        assert proxy.invocations == []
        assert proxy.trajectories == {}
        assert proxy.faults.total_rate == pytest.approx(0.5)


class TestWrapHelper:
    def test_wrap_defaults_to_all_kinds(self):
        proxy = wrap(MockTarget.healthy(), rate=0.25)
        assert proxy.faults.total_rate == pytest.approx(0.25)
        assert len(proxy.faults.rates) == len(FaultKind)

    def test_wrap_accepts_a_config(self):
        config = FaultConfig.uniform(0.1)
        assert wrap(MockTarget.healthy(), config).faults is config

    def test_wrap_accepts_specific_kinds(self):
        proxy = wrap(MockTarget.healthy(), [FaultKind.TIMEOUT], rate=0.5)
        assert set(proxy.faults.rates) == {FaultKind.TIMEOUT}


class TestNesting:
    async def test_a_proxy_can_wrap_a_proxy(self):
        """FaultProxy is a Target, so composition must hold."""
        inner = MockTarget.healthy()
        proxy = FaultProxy(FaultProxy(inner, FaultConfig.off()), only(FaultKind.SERVER_ERROR))
        await proxy.setup()

        response = await proxy.invoke(proxy.sample_request(0))

        assert response.error_kind is ErrorKind.SERVER_ERROR
