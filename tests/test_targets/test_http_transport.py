"""Streamable HTTP transport, credentials, and the SDK differences behind both.

`--uri` accepted stdio:// and sse:// only. SSE was deprecated by the 2025-06-18
spec and replaced by Streamable HTTP, so the one network transport pointed at the
dead protocol and every hosted server was unscannable -- which is why every
latency figure this project had was sub-millisecond local stdio.
"""

from __future__ import annotations

import contextlib

import pytest

from ratemyagent.targets import MCPTarget, TargetError
from ratemyagent.targets.base import REDACTED, redact_headers, redact_uri
from ratemyagent.targets.mcp import _parse_uri


class TestTransportSelection:
    @pytest.mark.parametrize("uri", [
        "https://example.com/mcp",
        "http://localhost:3001/mcp",
        "https://example.com/sse",
    ])
    def test_bare_http_selects_streamable_http(self, uri):
        """Behavioural change in 0.1.7. Path shape does not override it: a
        server on /sse that speaks Streamable HTTP is the common case now."""
        assert _parse_uri(uri) == ("http", [uri])

    @pytest.mark.parametrize("uri,expected", [
        ("sse://h:8080/sse", "http://h:8080/sse"),
        ("sse+https://h/sse", "https://h/sse"),
    ])
    def test_sse_remains_reachable_explicitly(self, uri, expected):
        assert _parse_uri(uri) == ("sse", [expected])

    def test_an_unusable_scheme_names_all_three(self):
        with pytest.raises(TargetError, match="stdio://.*https://.*sse://"):
            _parse_uri("ftp://host/mcp")

    def test_a_network_target_is_named_by_its_url(self):
        target = MCPTarget("https://example.com/mcp")
        assert target._default_name() == "https://example.com/mcp"


class TestOptionsMatchTheTransport:
    """Raise, not warn. A silently ignored option looks like configuration."""

    def test_env_is_refused_on_http(self):
        with pytest.raises(TargetError, match="env= is a stdio option"):
            MCPTarget("https://example.com/mcp", env={"TOKEN": "x"})

    def test_env_is_refused_on_sse(self):
        with pytest.raises(TargetError, match="env= is a stdio option"):
            MCPTarget("sse+https://example.com/sse", env={"TOKEN": "x"})

    def test_env_is_fine_on_stdio(self):
        assert MCPTarget("stdio://./s.py", env={"TOKEN": "x"}).env == {"TOKEN": "x"}

    def test_headers_are_refused_on_stdio(self):
        with pytest.raises(TargetError, match="headers= is an http/sse option"):
            MCPTarget("stdio://./s.py", headers={"Authorization": "Bearer x"})

    def test_headers_are_fine_on_http(self):
        target = MCPTarget("https://x/mcp", headers={"Authorization": "Bearer x"})
        assert target.headers == {"Authorization": "Bearer x"}


class TestCredentialsNeverReachAnArtifact:
    """The one real leak risk: a scan artifact gets committed and pasted around."""

    def test_header_values_are_replaced_by_name_only(self):
        redacted = redact_headers({"Authorization": "Bearer sk-secret", "X-Api-Key": "k"})

        assert redacted == {"Authorization": REDACTED, "X-Api-Key": REDACTED}
        assert "sk-secret" not in str(redacted)

    def test_no_header_is_treated_as_safe_to_print(self):
        """No allowlist. `Cookie` does not announce itself as a credential."""
        for name in ("Accept", "User-Agent", "Cookie", "X-Trace-Id"):
            assert redact_headers({name: "value"})[name] == REDACTED

    @pytest.mark.parametrize("uri,expected", [
        ("https://user:tok@h/mcp", f"https://user:{REDACTED}@h/mcp"),
        ("https://tok@h/mcp", f"https://tok:{REDACTED}@h/mcp"),
        ("https://h/mcp", "https://h/mcp"),
        ("stdio://./s.py", "stdio://./s.py"),
        (None, None),
    ])
    def test_uri_userinfo_is_stripped(self, uri, expected):
        assert redact_uri(uri) == expected

    def test_an_at_sign_in_a_path_is_not_credentials(self):
        assert redact_uri("https://h/mcp/@scope/pkg") == "https://h/mcp/@scope/pkg"

    def test_describe_carries_names_without_values(self):
        target = MCPTarget(
            "https://user:tok@example.com/mcp",
            headers={"Authorization": "Bearer sk-secret"},
        )
        info = target.describe()
        rendered = str(info.to_dict())

        assert "sk-secret" not in rendered
        assert "tok" not in info.uri
        assert info.metadata["headers"] == {"Authorization": REDACTED}
        # Provenance survives: a reader can still tell the scan was authenticated.
        assert "Authorization" in rendered


class TestHeadersAreActuallySent:
    """Recording a header name proves nothing about whether it went on the wire."""

    async def test_they_are_handed_to_the_http_client(self, monkeypatch):
        import contextlib

        captured: dict = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        @contextlib.asynccontextmanager
        async def fake_transport(url, http_client=None):
            captured["url"] = url
            captured["client"] = http_client
            yield ("read", "write", "session_id")  # 1.x arity

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", fake_transport,
            raising=False,
        )

        target = MCPTarget(
            "https://example.com/mcp",
            headers={"Authorization": "Bearer sk-secret"},
            timeout_s=12.0,
        )
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            read, write = await target._open_streamable_http(stack)

        assert read == "read" and write == "write", "3-tuple must index, not unpack"
        assert captured["headers"] == {"Authorization": "Bearer sk-secret"}
        assert captured["timeout"] == 12.0
        assert captured["url"] == "https://example.com/mcp"

    async def test_a_two_tuple_transport_also_works(self, monkeypatch):
        """SDK 2.x yields two items where 1.x yields three."""
        import contextlib
        from contextlib import AsyncExitStack

        @contextlib.asynccontextmanager
        async def fake_transport(url, http_client=None):
            yield ("read", "write")

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", fake_transport,
            raising=False,
        )

        target = MCPTarget("https://example.com/mcp")
        async with AsyncExitStack() as stack:
            assert await target._open_streamable_http(stack) == ("read", "write")


class TestTeardownStaysInItsOwnTask:
    """`asyncio.wait_for` runs its awaitable in a new task on Python 3.10 and
    3.11, which is enough to break every stdio scan on those versions.

    `stack.aclose()` exits the MCP SDK's anyio cancel scopes, and anyio requires
    the exit to happen in the task that entered them. `wait_for` moved it,
    producing `RuntimeError: Attempted to exit cancel scope in a different task`
    and a bare `CancelledError` out of the scan. 3.12 reimplemented `wait_for`
    on `asyncio.timeout()`, which stays in the current task -- so the bug was
    invisible to every scan this project ran by hand, all of them on 3.13.

    This asserts the property rather than the symptom: the close must not be
    bounded by anything that spawns a task. A version-specific integration
    failure is caught by the stdio scan step in CI; this catches the reintroduc-
    tion of the pattern on any version.
    """

    async def test_the_close_runs_in_the_calling_task(self):
        import asyncio
        from contextlib import AsyncExitStack

        from ratemyagent.targets import MCPTarget

        entered = asyncio.current_task()
        closed_in: list = []

        class RecordingStack(AsyncExitStack):
            async def aclose(self):
                closed_in.append(asyncio.current_task())

        target = MCPTarget("stdio://./server.py")
        await target._close(RecordingStack())

        assert closed_in == [entered], (
            "teardown ran in a different task than the caller; on Python 3.10 "
            "and 3.11 that breaks every stdio scan"
        )

    async def test_a_hanging_close_is_still_bounded(self):
        """The 0.1.8 property the bound exists for, which must survive the fix.

        Built the way `setup()` builds it: our cancel scope entered *first*, so
        it is outermost and the deadline set on it bounds everything inside.
        The earlier version of this test called `_close` on a bare stack, which
        no longer has a scope to bound -- and that gap is the point, because a
        bound that only works when the structure is right is the only kind that
        does not corrupt the structure.
        """
        import time
        from contextlib import AsyncExitStack

        import anyio

        from ratemyagent.targets import MCPTarget

        stack = AsyncExitStack()
        scope = anyio.CancelScope()
        stack.enter_context(scope)

        @contextlib.asynccontextmanager
        async def never_closes():
            try:
                yield
            finally:
                await anyio.sleep(30)

        await stack.enter_async_context(never_closes())

        target = MCPTarget("stdio://./server.py")
        target.CLOSE_TIMEOUT_S = 0.05

        started = time.perf_counter()
        await target._close(stack, scope)

        assert time.perf_counter() - started < 5, "the close was not bounded"
        assert scope.cancelled_caught


class TestTheSSETransportIsActuallyExercised:
    """`sse://` shipped before 0.1.7 and no test or scan had ever opened one.

    URI parsing was covered; the branch that connects was not. It worked the
    first time it was pointed at a real server (`server-everything sse`,
    2026-09-09, `Target: mcp-servers/everything (mcp via sse)`), and that is
    luck rather than evidence -- the same shape as two `MCPTarget` setups in one
    event loop, which was also "obviously fine" and was broken from 0.1.16 to
    0.1.20 because nothing ran it.

    It is public API: `--uri sse://...` is documented, and SSE is the transport
    every pre-2025-06-18 server still speaks. A path with users and no test is
    the standing rule's subject, not an exception to it.
    """

    async def test_an_sse_uri_opens_the_sse_client_with_the_resolved_url(
        self, monkeypatch
    ):
        """`sse://host/sse` resolves to `http://` on the wire, not `sse://`."""
        captured: dict = {}

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            yield ("read", "write")

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=False)

        target = MCPTarget(
            "sse://localhost:8080/sse",
            headers={"Authorization": "Bearer sk-secret"},
        )
        with contextlib.suppress(Exception):
            await target.setup()
        await target.teardown()

        assert captured["url"] == "http://localhost:8080/sse", (
            "the scheme must be rewritten for the wire; sse:// is our spelling"
        )
        assert captured["headers"] == {"Authorization": "Bearer sk-secret"}, (
            "headers are documented for sse:// and must reach the client"
        )

    async def test_sse_https_keeps_tls(self, monkeypatch):
        captured: dict = {}

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None):
            captured["url"] = url
            yield ("read", "write")

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=False)

        target = MCPTarget("sse+https://example.com/sse")
        with contextlib.suppress(Exception):
            await target.setup()
        await target.teardown()

        assert captured["url"] == "https://example.com/sse"

    async def test_an_sse_uri_does_not_take_the_streamable_http_path(
        self, monkeypatch
    ):
        """The two network transports are not interchangeable.

        Falling through to Streamable HTTP against an SSE server is the exact
        failure 0.1.7's hint text exists to explain, and it would look like a
        server fault rather than a client one.
        """
        called: list[str] = []

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None):
            called.append("sse")
            yield ("read", "write")

        async def fake_streamable(self, stack):
            called.append("http")
            return ("read", "write")

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=False)
        monkeypatch.setattr(MCPTarget, "_open_streamable_http", fake_streamable)

        target = MCPTarget("sse://localhost:8080/sse")
        with contextlib.suppress(Exception):
            await target.setup()
        await target.teardown()

        assert called == ["sse"], f"sse:// took the wrong branch: {called}"

    def test_the_sdk_exposes_sse_client_with_the_signature_we_call(self):
        """The standing rule from section 8b, applied to the transport we forgot.

        "Before calling into the SDK, inspect the symbol under both majors and
        confirm the name, the signature and the return shape." `streamablehttp_client`
        was renamed between 1.x and 2.x and read as a server fault for a release;
        `sse_client` is called with a positional url and a `headers` keyword, and
        nothing checked that either survives. The CI matrix installs both majors,
        so this runs against both.
        """
        import inspect

        from mcp.client.sse import sse_client

        params = inspect.signature(sse_client).parameters
        assert "headers" in params, (
            f"this SDK's sse_client has no `headers` parameter: {list(params)}. "
            "MCPTarget passes headers= for sse://, so auth would be dropped "
            "silently rather than raising."
        )
        first = next(iter(params))
        assert first in ("url", "endpoint"), (
            f"sse_client's first parameter is {first!r}; MCPTarget passes the "
            "URL positionally"
        )
