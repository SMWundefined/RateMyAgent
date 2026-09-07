"""The scan-level deadline: turning a hang into an attributable failure.

`mcp-server-fetch` hung three times -- twice in this project's own test harness,
where it was worked around with a wall-clock cap in a throwaway script, and once
in a user's session, where it had to be killed by hand. The workaround existed
for three appearances before the product did.

`--timeout` cannot bound this. It bounds one request, and the stalls are outside
any request: entering `stdio_client()` waits for a subprocess handshake before a
request exists, and closing the exit stack waits for that subprocess to go away.
"""

from __future__ import annotations

import asyncio

import pytest

from ratemyagent import scan
from ratemyagent.probes import ProbeConfig
from ratemyagent.scanner import ScanTimeout
from ratemyagent.targets import MockTarget


class TestBudget:
    def test_it_is_derived_from_the_request_budget_not_the_request_timeout(self):
        config = ProbeConfig(requests=20, timeout_s=30.0)
        assert config.scan_budget() == 30.0 * 20 * 4

    def test_an_explicit_budget_wins(self):
        assert ProbeConfig(scan_timeout_s=12.0).scan_budget() == 12.0

    def test_a_tiny_scan_still_gets_a_floor(self):
        """A one-request scan must not get a budget so small it self-trips."""
        assert ProbeConfig(requests=1, timeout_s=1.0).scan_budget() == 60.0

    def test_the_budget_is_recorded_in_the_config_export(self):
        assert ProbeConfig(scan_timeout_s=99.0).to_dict()["scan_timeout_s"] == 99.0


class TestExpiry:
    async def test_a_stalled_scan_raises_rather_than_hanging(self):
        class Stalls(MockTarget):
            async def setup(self):
                await super().setup()
                await asyncio.sleep(30)

        with pytest.raises(ScanTimeout) as caught:
            await scan(Stalls(), config=ProbeConfig(scan_timeout_s=0.3))

        assert "0s budget" in str(caught.value) or "budget" in str(caught.value)

    async def test_it_names_the_phase_it_died_in(self):
        """A bare kill says nothing. The message has to point somewhere."""
        class StallsInProbe(MockTarget):
            async def invoke(self, request):
                await asyncio.sleep(30)
                return await super().invoke(request)

        with pytest.raises(ScanTimeout, match="phase baseline"):
            await scan(
                StallsInProbe(), probes="latency",
                config=ProbeConfig(scan_timeout_s=0.4, requests=2, warmup=0),
            )

    async def test_it_names_the_probe_too(self):
        class StallsInProbe(MockTarget):
            async def invoke(self, request):
                await asyncio.sleep(30)
                return await super().invoke(request)

        with pytest.raises(ScanTimeout, match="probe latency"):
            await scan(
                StallsInProbe(), probes="latency",
                config=ProbeConfig(scan_timeout_s=0.4, requests=2, warmup=0),
            )

    async def test_a_healthy_scan_is_unaffected(self):
        result = await scan(MockTarget.healthy(), config=ProbeConfig(requests=6, warmup=0))
        assert result.score is not None


class TestItIsAScannerFailureNotATargetFailure:
    def test_scan_timeout_is_a_target_error_subclass(self):
        """Both CLI paths already map TargetError to exit 2, which is the code
        for 'the scan did not happen' -- distinct from exit 1, 'the target
        failed its policy'. Conflating them makes CI unable to tell a broken
        scanner from a broken dependency."""
        from ratemyagent.targets import TargetError

        assert issubclass(ScanTimeout, TargetError)


class TestEscapedCancellation:
    """`wait_for` does not always convert its own expiry.

    When the cancellation lands inside a nested cancel scope -- which the MCP
    SDK's anyio task groups create -- it escapes as `CancelledError` instead of
    `TimeoutError`, and a timed-out handshake against a hosted server printed a
    raw `Cancelled via cancel scope` traceback. That is the bare kill the
    deadline exists to prevent.
    """

    async def test_an_escaped_cancellation_becomes_a_clean_timeout(self):
        class SwallowsIntoCancelled(MockTarget):
            async def setup(self):
                await super().setup()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise asyncio.CancelledError("Cancelled via cancel scope") from None

        with pytest.raises(ScanTimeout, match="budget"):
            await scan(SwallowsIntoCancelled(), config=ProbeConfig(scan_timeout_s=0.3))

    async def test_a_cancellation_that_is_not_ours_still_propagates(self):
        """Ctrl-C and enclosing task groups must not be relabelled as timeouts."""
        class CancelsImmediately(MockTarget):
            async def setup(self):
                await super().setup()
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await scan(CancelsImmediately(), config=ProbeConfig(scan_timeout_s=300))
