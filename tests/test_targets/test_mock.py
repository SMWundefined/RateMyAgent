"""MockTarget behavior. Everything here runs with no network and no API keys."""

from __future__ import annotations

import pytest

from ratemyagent.models import ErrorKind, Request
from ratemyagent.targets import MockTarget, TargetError


async def test_setup_teardown_gates_invoke():
    target = MockTarget.healthy()
    with pytest.raises(TargetError):
        await target.invoke(target.sample_request(0))

    await target.setup()
    response = await target.invoke(target.sample_request(0))
    assert response.ok

    await target.teardown()
    with pytest.raises(TargetError):
        await target.invoke(target.sample_request(0))


async def test_async_context_manager_sets_up_and_tears_down():
    async with MockTarget.healthy() as target:
        assert (await target.invoke(target.sample_request(0))).ok
    assert not target._connected


async def test_same_label_gives_same_result():
    async with MockTarget.degraded() as target:
        first = await target.invoke(Request(op="echo", label="echo#7"))
        second = await target.invoke(Request(op="echo", label="echo#7"))
    assert first.latency_s == second.latency_s
    assert first.ok == second.ok


async def test_behavior_is_order_independent():
    """Labels, not a shared cursor, drive the RNG -- so concurrency cannot skew a run."""
    requests = MockTarget.degraded().probe_requests(10)

    async with MockTarget.degraded() as forward:
        in_order = [(await forward.invoke(r)).latency_s for r in requests]
    async with MockTarget.degraded() as backward:
        reversed_run = [(await backward.invoke(r)).latency_s for r in reversed(requests)]

    assert in_order == list(reversed(reversed_run))


async def test_different_seeds_diverge():
    async with MockTarget.healthy(seed=1) as one, MockTarget.healthy(seed=2) as two:
        request = Request(op="echo", label="echo#0")
        assert (await one.invoke(request)).latency_s != (await two.invoke(request)).latency_s


async def test_healthy_profile_never_fails():
    async with MockTarget.healthy() as target:
        responses = [await target.invoke(r) for r in target.probe_requests(50)]
    assert all(r.ok for r in responses)
    assert all(0 < r.latency_s < 1.0 for r in responses)


async def test_failing_profile_produces_tagged_errors():
    async with MockTarget.failing() as target:
        responses = [await target.invoke(r) for r in target.probe_requests(100)]

    failures = [r for r in responses if not r.ok]
    assert 0.2 < len(failures) / len(responses) < 0.5
    assert all(isinstance(r.error_kind, ErrorKind) for r in failures)
    assert all(r.error for r in failures)


async def test_successful_responses_carry_probe_signals():
    async with MockTarget.healthy() as target:
        response = await target.invoke(target.sample_request(0))

    assert response.ttft_s is not None and response.ttft_s < response.latency_s
    assert response.server_time_s is not None and response.server_time_s < response.latency_s
    assert response.tokens_in and response.tokens_out


async def test_probe_requests_are_uniquely_labeled():
    target = MockTarget.healthy()
    labels = [r.label for r in target.probe_requests(10)]
    assert len(set(labels)) == 10


async def test_probe_requests_offset_avoids_collisions():
    target = MockTarget.healthy()
    warmup = {r.label for r in target.probe_requests(3)}
    measured = {r.label for r in target.probe_requests(5, offset=3)}
    assert not warmup & measured


async def test_sleep_scale_zero_reports_latency_without_sleeping():
    """Simulated latency keeps the suite fast while the probe math stays real."""
    async with MockTarget(latency_s=10.0, jitter_s=0.0, tail_probability=0.0) as target:
        response = await target.invoke(target.sample_request(0))
    assert response.latency_s == pytest.approx(10.0)


def test_describe_reports_capabilities():
    info = MockTarget.healthy().describe()
    assert info.kind == "mock"
    assert info.capabilities
    assert info.metadata["simulated"] is True


async def test_calls_are_recorded_in_order():
    async with MockTarget.healthy() as target:
        assert target.calls == []
        for request in target.probe_requests(3):
            await target.invoke(request)

    assert [call.label for call in target.calls] == ["echo#0", "search#1", "summarize#2"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tools": ()},
        {"error_rate": 1.5},
        {"error_rate": -0.1},
        {"error_rate": 0.5, "error_kinds": ()},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        MockTarget(**kwargs)
