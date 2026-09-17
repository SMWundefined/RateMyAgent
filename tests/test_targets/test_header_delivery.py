"""Do `--header` credentials reach the wire? Asserted against bytes, not kwargs.

Redaction at `describe()` has been tested since 0.1.7. **Delivery never was.**
The two are different claims, and only one of them is what a user needs: a
scan that redacts a token it never sent is perfectly safe and perfectly
useless. That gap is how `--header` shipped in 0.1.7 and went thirteen releases
without an authenticated run against a real server.

The existing unit test asserts the headers reach the `httpx.AsyncClient`
*constructor*. That is one layer short: it passes if the client is built with
the headers and then discarded, or if the SDK is handed a different client. So:

- `TestTheClientCarriesThem` uses a real `httpx.AsyncClient` and asserts the
  instance the SDK receives has the headers on it.
- `TestTheyReachTheWire` runs a recording TCP proxy in front of a real MCP
  server and greps the client's actual bytes, over **both** network
  transports. Nothing between the flag and the socket is mocked.

The wire tests need a server on :3001 and are skipped without one, the way the
`assets/` checks are -- stated rather than hidden. CI starts one.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from ratemyagent.targets import MCPTarget
from ratemyagent.targets.mcp import outgoing_headers

HEADERS = {"Authorization": "Bearer sk-wire-test", "X-Probe": "ratemyagent"}

#: The two network transports need the reference server in different modes, so
#: they get a port each rather than one upstream that can only be one of them.
#: Skipped independently: whichever is running is tested, and a missing pair
#: says which command starts it rather than failing as if the code were broken.
UPSTREAMS = {
    "http://": (("127.0.0.1", 3001), "/mcp", "streamableHttp"),
    "sse://": (("127.0.0.1", 3005), "/sse", "sse"),
}


def _is_up(address: tuple[str, int]) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection(address, timeout=0.5):
            return True
    return False


def _require(scheme: str) -> tuple[tuple[str, int], str]:
    address, path, mode = UPSTREAMS[scheme]
    if not _is_up(address):
        port = address[1]
        pytest.skip(
            f"no MCP server on :{port}; CI starts one, locally run "
            f"`PORT={port} npx -y @modelcontextprotocol/server-everything {mode}`"
        )
    return address, path


class TestTheClientCarriesThem:
    """One layer below the constructor: the object the SDK is handed."""

    async def test_the_sdk_receives_a_client_with_the_headers_set(self, monkeypatch):
        from contextlib import AsyncExitStack

        received: dict = {}

        @contextlib.asynccontextmanager
        async def fake_transport(url, http_client=None):
            # Read them off the live client, not off constructor kwargs.
            received["headers"] = dict(http_client.headers)
            received["timeout"] = http_client.timeout
            yield ("read", "write")

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", fake_transport,
            raising=False,
        )

        target = MCPTarget("https://example.com/mcp", headers=dict(HEADERS), timeout_s=12.0)
        async with AsyncExitStack() as stack:
            await target._open_streamable_http(stack)

        for name, value in HEADERS.items():
            assert received["headers"].get(name.lower()) == value, (
                f"{name} is not set on the client the SDK was handed"
            )

    async def test_no_headers_is_not_an_empty_dict(self, monkeypatch):
        """`headers=None` must not become `{}` and mask a dropped credential."""
        from contextlib import AsyncExitStack

        @contextlib.asynccontextmanager
        async def fake_transport(url, http_client=None):
            yield ("read", "write")

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", fake_transport,
            raising=False,
        )
        target = MCPTarget("https://example.com/mcp")
        assert target.headers is None
        async with AsyncExitStack() as stack:
            await target._open_streamable_http(stack)


class TestTheUserAgent:
    """A scan says what it is, unless the user said otherwise (1.4.2).

    Scanning is load: fault injection, retries, and a concurrency ramp against
    somebody's server. Anonymous traffic that behaves like that is what gets an
    IP blocked, and an operator reading their own access log should be able to
    tell a reliability scan from a crawler without having to ask.

    The user's own `User-Agent` wins. It is a deliberate choice -- some gateways
    route on it -- and this default is not.
    """

    def test_it_names_the_tool_the_version_and_where_to_look(self):
        import ratemyagent
        from ratemyagent.targets.mcp import user_agent

        assert user_agent() == (
            f"ratemyagent/{ratemyagent.__version__} "
            "(+https://github.com/SMWundefined/RateMyAgent)"
        )

    def test_it_is_added_when_the_user_sent_none(self):
        sent = outgoing_headers(None)
        assert sent["User-Agent"].startswith("ratemyagent/")

    def test_it_does_not_displace_the_credentials(self):
        sent = outgoing_headers(dict(HEADERS))
        assert sent["Authorization"] == HEADERS["Authorization"]
        assert sent["User-Agent"].startswith("ratemyagent/")

    @pytest.mark.parametrize("name", ["User-Agent", "user-agent", "USER-AGENT"])
    def test_the_users_own_wins_whatever_its_case(self, name):
        sent = outgoing_headers({name: "acme-scanner/2"})
        assert sent[name] == "acme-scanner/2"
        assert not any(
            value.startswith("ratemyagent/") for value in sent.values()
        ), f"ours overrode a {name} the user chose deliberately"

    def test_the_caller_dict_is_not_mutated(self):
        supplied = {"X-Probe": "ratemyagent"}
        outgoing_headers(supplied)
        assert supplied == {"X-Probe": "ratemyagent"}

    async def test_the_streamable_http_client_carries_it(self, monkeypatch):
        """One layer below the helper: the client the SDK is actually handed."""
        from contextlib import AsyncExitStack

        received: dict = {}

        @contextlib.asynccontextmanager
        async def fake_transport(url, http_client=None):
            received["headers"] = dict(http_client.headers)
            yield ("read", "write")

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", fake_transport,
            raising=False,
        )
        target = MCPTarget("https://example.com/mcp", timeout_s=12.0)
        async with AsyncExitStack() as stack:
            await target._open_streamable_http(stack)

        assert received["headers"]["user-agent"].startswith("ratemyagent/")

    async def test_the_sse_client_is_given_it(self, monkeypatch):
        received: dict = {}

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None, **kwargs):
            received["headers"] = dict(headers or {})
            yield ("read", "write")
            raise AssertionError("unreachable: setup() continues past this")

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=False)
        target = MCPTarget("sse://example.com/sse", timeout_s=5.0)
        with contextlib.suppress(Exception):
            await target.setup()
        await target.teardown()

        assert received.get("headers", {}).get("User-Agent", "").startswith(
            "ratemyagent/"
        ), received


class _RecordingProxy:
    """Forwards to the upstream MCP server, keeping the client's first bytes."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        self.seen: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> "_RecordingProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    @staticmethod
    async def _pipe(reader, writer) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    async def _handle(self, client_reader, client_writer) -> None:
        first = await client_reader.read(65536)
        self.seen.append(first.decode("latin-1", "replace"))
        server_reader, server_writer = await asyncio.open_connection(*self.upstream)
        server_writer.write(first)
        await server_writer.drain()
        await asyncio.gather(
            self._pipe(client_reader, server_writer),
            self._pipe(server_reader, client_writer),
        )

    @property
    def wire(self) -> str:
        return "\n".join(self.seen)


class TestTheyReachTheWire:
    """The claim that matters, checked against bytes."""

    @pytest.mark.parametrize("scheme", list(UPSTREAMS))
    async def test_both_network_transports_send_them(self, scheme):
        upstream, path = _require(scheme)
        async with _RecordingProxy(upstream) as proxy:
            target = MCPTarget(
                f"{scheme}localhost:{proxy.port}{path}",
                headers=dict(HEADERS),
                tool="echo",
                tool_args={"message": "hi"},
                timeout_s=20,
            )
            try:
                await target.setup()
            finally:
                await target.teardown()

        assert proxy.seen, "the proxy saw no traffic at all"
        for name, value in HEADERS.items():
            assert f"{name}: {value}".lower() in proxy.wire.lower(), (
                f"{name} never reached the wire over {scheme}. The scan would "
                "authenticate with nothing while redacting a token it never sent."
            )

    @pytest.mark.parametrize("scheme", list(UPSTREAMS))
    async def test_both_network_transports_send_the_user_agent(self, scheme):
        """1.4.2, on the wire: what an operator's access log will show."""
        upstream, path = _require(scheme)
        async with _RecordingProxy(upstream) as proxy:
            target = MCPTarget(
                f"{scheme}localhost:{proxy.port}{path}",
                tool="echo", tool_args={"message": "hi"}, timeout_s=20,
            )
            try:
                await target.setup()
            finally:
                await target.teardown()

        assert proxy.seen, "the proxy saw no traffic at all"
        assert "user-agent: ratemyagent/" in proxy.wire.lower(), proxy.wire[:400]

    @pytest.mark.parametrize("scheme", list(UPSTREAMS))
    async def test_a_user_supplied_agent_is_what_reaches_the_wire(self, scheme):
        upstream, path = _require(scheme)
        async with _RecordingProxy(upstream) as proxy:
            target = MCPTarget(
                f"{scheme}localhost:{proxy.port}{path}",
                headers={"User-Agent": "acme-scanner/2"},
                tool="echo", tool_args={"message": "hi"}, timeout_s=20,
            )
            try:
                await target.setup()
            finally:
                await target.teardown()

        wire = proxy.wire.lower()
        assert "user-agent: acme-scanner/2" in wire, proxy.wire[:400]
        assert "ratemyagent/" not in wire, (
            "ours went out beside the one the user chose"
        )

    async def test_the_proxy_would_notice_their_absence(self):
        """The deliberate failing case: no headers, nothing on the wire.

        Without this the test above passes on any wire containing the string,
        including one where the server echoed it back.
        """
        upstream, _ = _require("http://")
        async with _RecordingProxy(upstream) as proxy:
            target = MCPTarget(
                f"http://localhost:{proxy.port}/mcp",
                tool="echo",
                tool_args={"message": "hi"},
                timeout_s=20,
            )
            try:
                await target.setup()
            finally:
                await target.teardown()

        assert proxy.seen, "the proxy saw no traffic at all"
        assert "sk-wire-test" not in proxy.wire
        assert "authorization:" not in proxy.wire.lower()
