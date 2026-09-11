"""Backoff after a rate limit, and the fixture that makes it testable at all.

**The fixture is the deliverable, not the backoff.** `FaultProxy._choose_fault`
seeds on `(trajectory, attempt)`, so a retry draws the same fault whether it
happens immediately or ten seconds later. Nothing in the regression set rewards
waiting, so a scanner could implement backoff perfectly and every existing test
would pass identically with it deleted.

`tests/fixtures/rate_limited_mcp_server.py` is the missing case: it refuses with
a 429-shaped tool error until N seconds have elapsed, then answers. A caller
that waits recovers; a caller that hammers does not.

Measured against it, `--requests 12` on a server that clears after 6s:

    backoff off   0/12 disrupted recovered,  3.00x amplification, 0.45s
    backoff on    1/1  disrupted recovered,  1.17x amplification, 10.3s

which is the 585-calls-for-200-operations defect, on a fixture.
"""

from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from ratemyagent.models import ErrorKind, Response
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes.fault import _BackoffBudget
from ratemyagent.targets.base import parse_retry_after


def _rate_limited(**meta) -> Response:
    return Response(ok=False, latency_s=0.0, error="429 rate limit",
                    error_kind=ErrorKind.RATE_LIMIT, meta=meta)


class TestTheTriggerIsTheKindNotTheHint:
    """The decision that shaped the design.

    The case backoff exists for -- a stdio server relaying an upstream 429 as a
    tool result -- has **no `Retry-After` anywhere** and is classified by
    matching the message text. Injected faults always carry a hint. Keying on
    the hint would have waited politely for our own fictions and hammered the
    only real rate limiter in the corpus.
    """

    async def test_a_rate_limit_with_no_hint_still_waits(self):
        budget = _BackoffBudget(0.01, 1.0)
        await budget.wait_for(_rate_limited())

        assert budget.waits == 1, (
            "no header, so no wait -- which is exactly backwards: the header is "
            "absent precisely in the case that motivated this"
        )

    @pytest.mark.parametrize("kind", [
        ErrorKind.TIMEOUT, ErrorKind.SERVER_ERROR, ErrorKind.CONNECTION,
        ErrorKind.INVALID_RESPONSE,
    ])
    async def test_other_failures_do_not_wait(self, kind):
        budget = _BackoffBudget(5.0, 30.0)
        await budget.wait_for(
            Response(ok=False, latency_s=0.0, error="x", error_kind=kind)
        )
        assert budget.waits == 0, "'overloaded' and 'glitched' are different"

    async def test_a_success_does_not_wait(self):
        budget = _BackoffBudget(5.0, 30.0)
        await budget.wait_for(Response(ok=True, latency_s=0.1))
        assert budget.waits == 0


class TestTheHintRefinesTheWait:
    async def test_a_shorter_hint_is_honoured(self):
        budget = _BackoffBudget(5.0, 30.0)
        await budget.wait_for(_rate_limited(retry_after_s=0.01, injected=True))
        assert budget.simulated_s == pytest.approx(0.01)

    async def test_a_longer_hint_is_capped(self):
        """`Retry-After: 120` unbounded is a denial of service on your own scan."""
        budget = _BackoffBudget(5.0, 30.0)
        await budget.wait_for(_rate_limited(retry_after_s=120, injected=True))
        assert budget.simulated_s == pytest.approx(5.0)

    async def test_no_hint_falls_back_to_the_ceiling(self):
        budget = _BackoffBudget(5.0, 30.0)
        await budget.wait_for(_rate_limited(injected=True))
        assert budget.simulated_s == pytest.approx(5.0)

    @pytest.mark.parametrize("headers,expected", [
        ({"retry-after": "3"}, 3.0),
        ({"Retry-After": "0.5"}, 0.5),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"retry-after": "-1"}, None),
        ({}, None),
        (None, None),
    ])
    def test_the_header_parser(self, headers, expected):
        assert parse_retry_after(headers) == expected


class TestTheBudgetIsBounded:
    async def test_it_stops_waiting_when_exhausted(self):
        budget = _BackoffBudget(1.0, 2.0)
        for _ in range(5):
            await budget.wait_for(_rate_limited(injected=True))

        assert budget.waits == 2, "waited past the budget"
        assert budget.unwaited == 3

    async def test_exhaustion_continues_rather_than_stopping(self):
        """Reverting silently to hammering is the failure this release fixes."""
        budget = _BackoffBudget(1.0, 1.0)
        for _ in range(3):
            await budget.wait_for(_rate_limited(injected=True))

        metrics = budget.metrics()
        assert metrics["backoff_unwaited"] == 2
        assert metrics["backoff_budget_exhausted"] is True

    async def test_a_zero_ceiling_disables_waiting_entirely(self):
        budget = _BackoffBudget(0.0, 30.0)
        await budget.wait_for(_rate_limited())
        assert budget.waits == 0 and budget.unwaited == 1

    async def test_the_last_wait_is_trimmed_to_what_is_left(self):
        """The budget is a total, not a count of full-ceiling waits.

        Added because a mutant that computed `min(hint, ceiling)` and dropped
        the remaining-budget term survived the whole file. Every existing case
        used a budget that was a whole multiple of the ceiling, so the final
        wait never needed trimming and overshoot was unobservable. 7.0 over a
        5.0 ceiling makes the second wait a partial one.
        """
        budget = _BackoffBudget(5.0, 7.0)
        await budget.wait_for(_rate_limited(injected=True))
        await budget.wait_for(_rate_limited(injected=True))

        assert budget.waits == 2
        assert budget.simulated_s == pytest.approx(7.0), "overshot the budget"


class TestTheConfigRefusesNegativeBackoff:
    """`ProbeConfig` validates the two new fields, and nothing tested that.

    Zero is meaningful for both -- a zero ceiling disables waiting, which is
    how the no-backoff arm of the fixture comparison is run -- so the guard has
    to reject below zero without rejecting zero.
    """

    @pytest.mark.parametrize("field", ["backoff_max_s", "backoff_budget_s"])
    def test_a_negative_value_is_refused(self, field):
        with pytest.raises(ValueError, match="cannot be negative"):
            ProbeConfig(**{field: -1.0})

    @pytest.mark.parametrize("field", ["backoff_max_s", "backoff_budget_s"])
    def test_zero_is_allowed(self, field):
        assert getattr(ProbeConfig(**{field: 0.0}), field) == 0.0


class TestAnInjectedFaultGetsASimulatedWait:
    """The same rule the mock target already follows.

    An injected 429 is our own fiction and is seeded per attempt, so a retry
    draws the same fault however long we wait. Sleeping for one costs wall clock
    and cannot change the outcome -- it measures the harness. The arithmetic is
    still recorded, so the numbers and caveats stay true.

    Without this the test suite went from 6 seconds to over two minutes, which
    is how it was found.
    """

    async def test_an_injected_wait_does_not_touch_the_clock(self):
        budget = _BackoffBudget(5.0, 30.0)
        started = asyncio.get_running_loop().time()
        await budget.wait_for(_rate_limited(injected="rate_limit"))
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.5, f"slept {elapsed:.1f}s for a fault we invented"
        assert budget.simulated_s == pytest.approx(5.0)
        assert budget.waited_s == 0.0

    async def test_a_real_rate_limit_does_touch_the_clock(self):
        budget = _BackoffBudget(0.05, 30.0)
        started = asyncio.get_running_loop().time()
        await budget.wait_for(_rate_limited())
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.04, "a real rate limit is the case waiting exists for"
        assert budget.waited_s == pytest.approx(0.05)
        assert budget.simulated_s == 0.0

    async def test_the_two_are_reported_separately(self):
        """A fast scan must not be mistaken for one that did not back off."""
        budget = _BackoffBudget(0.01, 30.0)
        await budget.wait_for(_rate_limited(injected=True))
        await budget.wait_for(_rate_limited())

        metrics = budget.metrics()
        assert metrics["backoff_simulated_s"] > 0
        assert metrics["backoff_waited_s"] > 0


class TestAgainstTheTimeDependentServer:
    """End to end, on the fixture that makes waiting matter."""

    @staticmethod
    def _uri(*extra: str) -> str:
        return "stdio://" + shlex.join(
            [sys.executable, "tests/fixtures/rate_limited_mcp_server.py", *extra]
        )

    async def _behaviour(self, budget_s: float):
        from ratemyagent.probes.base import ScanContext
        from ratemyagent.probes.behavior import BehaviorAnalyzer
        from ratemyagent.probes.fault import FaultInjector
        from ratemyagent.targets.mcp import MCPTarget

        target = MCPTarget(
            self._uri("--recover-after", "0.6"), tool="get_thing",
            tool_args={"query": "x"}, timeout_s=20,
        )
        await target.setup()
        try:
            config = ProbeConfig(
                # Two retries at 0.5s must outlast the 0.6s recovery window,
                # or waiting cannot help and the test proves nothing. The
                # assertion below says so rather than passing vacuously.
                requests=6, warmup=0, backoff_max_s=0.5,
                backoff_budget_s=budget_s, extra={"fault_rate": 0.0},
            )
            context = ScanContext()
            await FaultInjector().execute(target, config, context)
            return await BehaviorAnalyzer().execute(target, config, context)
        finally:
            await target.teardown()

    async def test_waiting_recovers_where_hammering_does_not(self):
        hammered = await self._behaviour(0.0)
        waited = await self._behaviour(30.0)

        assert hammered.metrics["recovered"] == 0, (
            "the fixture is meant to refuse every immediate retry"
        )
        assert waited.metrics["recovered"] > 0, (
            "waiting did not help, so the fixture is not time-dependent and "
            "this test proves nothing"
        )

    async def test_waiting_lowers_amplification(self):
        """The 585-calls-for-200-operations defect, measured."""
        hammered = await self._behaviour(0.0)
        waited = await self._behaviour(30.0)

        assert hammered.metrics["attempts"] > waited.metrics["attempts"], (
            f"hammering sent {hammered.metrics['attempts']} calls and waiting "
            f"sent {waited.metrics['attempts']}; backoff is supposed to reduce "
            "load on a dependency that is already refusing"
        )
