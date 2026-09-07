"""Target ABC: the single interface every probe runs against."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from ..models import ErrorKind, Request, Response, TargetInfo, ToolInfo

#: Failure kinds that mean the transport itself died -- nothing came back.
#:
#: Used by the simulating targets (MockTarget, FaultProxy) to decide
#: `Response.delivered`, because for them "did anything arrive" is part of what
#: they are pretending to be rather than something they can observe. Real
#: adapters never consult this: they know directly whether they got an answer.
TRANSPORT_KINDS = frozenset({ErrorKind.CONNECTION, ErrorKind.TIMEOUT})


class TargetError(RuntimeError):
    """Raised for setup and configuration failures, not per-request failures.

    A request that fails comes back as a Response with ok=False; only a target
    that cannot be used at all raises.
    """


class Target(ABC):
    """Something a scan can be pointed at.

    #: Does this target retry on its own behalf?
    #:
    #: False for a service -- an MCP server, a chat completion endpoint, a mock.
    #: When a scan faults one of those, the retrying is done by *this scanner*,
    #: so retry amplification, backoff shape and recovery latency describe the
    #: harness rather than the target. Those metrics are withheld rather than
    #: reported against something that cannot have produced them.
    #:
    #: True for a caller -- an agent or a client wrapping a service, where the
    #: trajectory really is the target's. Nothing sets it True yet; `AgentTarget`
    #: is what turns caller-strategy scoring back on.

    Probes never construct requests out of thin air; they ask the target for
    representative traffic via probe_requests(), so a probe works against any
    adapter without knowing what an MCP tool or a chat completion looks like.
    """

    runs_own_retry_loop: bool = False

    @abstractmethod
    async def setup(self) -> None:
        """Connect and discover capabilities. Must be called before invoke()."""

    @abstractmethod
    async def invoke(self, request: Request) -> Response:
        """Send one request. Never raises for target-side failures."""

    @abstractmethod
    async def teardown(self) -> None:
        """Release connections and subprocesses. Safe to call twice."""

    @abstractmethod
    def describe(self) -> TargetInfo:
        """Metadata about the target. Only valid after setup()."""

    def list_tools(self) -> list[ToolInfo]:
        """Capabilities discovered during setup().

        Meaningful for MCP targets; targets without a tool surface return an
        empty list rather than raising, so callers never have to special-case.
        """
        return []

    def sample_request(self, index: int = 0) -> Request:
        """Build one representative request.

        Adapters override this. `index` lets the target vary payloads across a
        run while keeping every request individually reproducible.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not know how to build probe traffic; "
            "override sample_request() or pass requests explicitly"
        )

    def probe_requests(self, count: int, *, offset: int = 0) -> list[Request]:
        """Build `count` requests for a probe to send.

        `offset` keeps labels unique across phases, so warmup traffic cannot
        collide with the measured run.
        """
        return [self.sample_request(offset + i) for i in range(count)]

    async def __aenter__(self) -> "Target":
        await self.setup()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.teardown()


def classify_exception(exc: BaseException) -> ErrorKind:
    """Map a raised exception onto the failure taxonomy.

    Kept transport-agnostic on purpose: adapters that know more (an HTTP 429,
    say) should tag the Response themselves and never reach this.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TIMEOUT
    if isinstance(exc, asyncio.CancelledError):
        return ErrorKind.CANCELLED
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return ErrorKind.CONNECTION

    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return ErrorKind.TIMEOUT
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return ErrorKind.RATE_LIMIT
    if "connect" in text or "refused" in text or "unreachable" in text:
        return ErrorKind.CONNECTION
    if any(code in text for code in ("500", "502", "503", "504")):
        return ErrorKind.SERVER_ERROR
    if "json" in text or "decode" in text or "parse" in text or "validation" in text:
        return ErrorKind.PROTOCOL
    return ErrorKind.UNKNOWN


REDACTED = "<redacted>"


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Header names, never their values.

    Which headers were sent is useful provenance -- it answers "was this scan
    authenticated" when a saved report is read back months later. The values are
    credentials, and a scan artifact gets committed, pasted into issues and
    attached to releases. Names in, values out, no allowlist: deciding which
    header is "safe enough" to print is a judgement that only has to be wrong
    once, and `Cookie` and `X-Api-Key` do not announce themselves.
    """
    return {name: REDACTED for name in sorted(headers or {})}


def redact_uri(uri: str | None) -> str | None:
    """Strip credentials from a URI's userinfo.

    `https://user:token@host/mcp` puts a secret somewhere nobody thinks to look
    for one, and it reaches the report header, the JSON export and the AGENTS.md
    state block as the target's identity.
    """
    if not uri or "@" not in uri:
        return uri

    scheme, separator, rest = uri.partition("://")
    if not separator or "@" not in rest:
        return uri

    userinfo, _, host = rest.rpartition("@")
    if "/" in userinfo:  # an @ in the path, not credentials
        return uri
    name = userinfo.split(":", 1)[0]
    return f"{scheme}://{name}:{REDACTED}@{host}" if name else f"{scheme}://{host}"

def error_response(exc: BaseException, latency_s: float) -> Response:
    """Standard failed Response for an exception raised during invoke().

    `delivered=False`: this is the one place a Response is built from a raised
    exception rather than from something the target sent back, which makes it
    the single source of the crash signal. Every other construction site got an
    answer of some kind and leaves `delivered` at its default.
    """
    return Response(
        ok=False,
        latency_s=latency_s,
        error=f"{type(exc).__name__}: {exc}",
        error_kind=classify_exception(exc),
        delivered=False,
    )
