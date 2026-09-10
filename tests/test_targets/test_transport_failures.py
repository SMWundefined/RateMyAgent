"""A transport that refuses must name itself, not raise a traceback.

`asyncio.CancelledError` inherits from `BaseException`, so
`MCPTarget.setup()`'s `except Exception` never saw it. Against an endpoint that
rejects the connection at the HTTP layer -- a 401, or anything that is not MCP
-- the SDK's anyio scope cancels the pending request, that cancellation walked
past the conversion to `TargetError`, and the user got a stack trace ending in
`anyio/streams/memory.py` naming neither their URL nor the status code.

The CLI then exited **1**. Under this project's contract that means "the target
failed its policy", for a scan that never connected. CI cannot tell those apart,
which is the whole reason the 0/1/2 split exists.

Second escape of this shape. 0.1.20 fixed a cancel scope unwinding in the wrong
order, swallowed by a broad `except Exception` in `_close`. Cancellation is not
an error subclass and does not behave like one.

The half that needs guarding hardest is the *other* direction: a genuine
cancellation -- Ctrl-C, an outer timeout, the scan deadline -- must stay a
cancellation. Converting those would make the scanner report the user's own
interrupt as a target fault, which is the same class of error as the retracted
crash finding.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from ratemyagent.targets import MCPTarget, TargetError


class _RefusingServer:
    """Answers every request with one status and closes."""

    def __init__(self, status: int = 401, body: bytes = b'{"error":"unauthorized"}'):
        self.status, self.body = status, body
        self.requests = 0

    async def __aenter__(self) -> "_RefusingServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        with contextlib.suppress(Exception):
            await reader.read(65536)
            self.requests += 1
            writer.write(
                b"HTTP/1.1 %d NO\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n%s"
                % (self.status, len(self.body), self.body)
            )
            await writer.drain()
            writer.close()


class TestARefusedTransportIsNamed:
    @pytest.mark.parametrize("status", [401, 403, 500, 404])
    async def test_it_raises_target_error_carrying_the_status(self, status):
        async with _RefusingServer(status) as server:
            target = MCPTarget(
                f"http://localhost:{server.port}/mcp",
                headers={"Authorization": "Bearer bad"},
                timeout_s=5,
            )
            with pytest.raises(TargetError) as caught:
                await target.setup()
            await target.teardown()

        message = str(caught.value)
        assert str(status) in message, (
            f"the error does not name the status: {message!r}. A user debugging "
            "credentials cannot act on a cancellation."
        )
        assert f"localhost:{server.port}" in message, "the error does not name the URL"

    @pytest.mark.parametrize("status,expected", [(401, True), (403, True), (500, False)])
    async def test_only_auth_statuses_mention_credentials(self, status, expected):
        """A 500 is not a credentials problem and must not suggest it is."""
        async with _RefusingServer(status) as server:
            target = MCPTarget(f"http://localhost:{server.port}/mcp", timeout_s=5)
            with pytest.raises(TargetError) as caught:
                await target.setup()
            await target.teardown()

        assert ("--header" in str(caught.value)) is expected

    async def test_it_is_not_a_bare_cancellation(self):
        """The regression, stated as its own case.

        `CancelledError` is `BaseException`, so this passes trivially if the
        handler is written as `except Exception`. `pytest.raises(TargetError)`
        above would also fail in that case, but it would fail with a confusing
        cancellation rather than saying what went wrong -- so name it.
        """
        async with _RefusingServer(401) as server:
            target = MCPTarget(f"http://localhost:{server.port}/mcp", timeout_s=5)
            try:
                await target.setup()
            except TargetError:
                pass
            except asyncio.CancelledError:  # pragma: no cover - the bug
                pytest.fail(
                    "setup() let a CancelledError escape. It inherits from "
                    "BaseException, so `except Exception` does not catch it, and "
                    "the CLI reports a scan that never connected as a policy "
                    "failure (exit 1) with a raw traceback."
                )
            finally:
                await target.teardown()


class TestGenuineCancellationIsPreserved:
    """The guard on the guard: do not convert the user's own interrupt."""

    async def test_a_cancelled_setup_with_no_http_failure_stays_cancelled(
        self, monkeypatch
    ):
        """No recorded status means no evidence the transport refused.

        Ctrl-C, an outer timeout and the scan deadline all arrive as
        `CancelledError` with nothing wrong at the HTTP layer. Converting those
        to `TargetError` would report the user's own interrupt as a target
        fault -- the same class of error as the retracted crash finding, which
        published our failure as somebody else's.
        """
        async def never_returns(self, stack):
            raise asyncio.CancelledError()

        monkeypatch.setattr(MCPTarget, "_open_streamable_http", never_returns)

        target = MCPTarget("http://example.com/mcp", timeout_s=5)
        with pytest.raises(asyncio.CancelledError):
            await target.setup()
        await target.teardown()

    async def test_a_recorded_status_is_what_licenses_the_conversion(self):
        """The two tests above differ only in whether a status was seen."""
        target = MCPTarget("http://example.com/mcp")
        assert target._last_http_status is None, (
            "nothing has failed yet, so there is no evidence to convert on"
        )
