"""Target ABC: the single interface every probe runs against."""

from __future__ import annotations

import asyncio
import contextlib
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


#: Environment variables the MCP SDK copies into a stdio child. Everything else
#: in the parent environment is dropped, by design -- `get_default_environment()`
#: calls these "deemed safe to inherit". Named here so the CLI help can say what
#: does survive rather than only what does not.
INHERITED_ENV_VARS = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")


def redact_env(env: dict[str, str] | None) -> dict[str, str]:
    """Variable names, never their values. Same rule as `redact_headers`.

    A stdio server's credential arrives this way and nowhere else -- the SDK
    copies six variables into the child and drops the rest, so `FIRECRAWL_API_KEY`
    in the parent reaches nothing. `--env` is the only path, which makes it
    exactly as sensitive as `--header` and it gets the same treatment.
    """
    return {name: REDACTED for name in sorted(env or {})}


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

#: JSON-RPC codes an SDK raises for a session that died rather than answered.
#: `CONNECTION_CLOSED` is the transport going away, which is a death however it
#: is spelled.
_TRANSPORT_DEATH_CODES = frozenset({-32000})


def jsonrpc_error_code(exc: BaseException) -> int | None:
    """The JSON-RPC error code this exception carries, if it is a *reply*.

    `McpError` is raised when an error **arrives over the connection** -- the
    SDK's own words. The server received the request, decided against it, and
    answered `{"error": {"code": -32602, ...}}`. That is delivered traffic
    surfaced as a raised exception, and reading "raised" as "nothing arrived" is
    what made a schema-validating server look like it was crashing.

    Duck-typed rather than importing `mcp`, which is an optional extra and must
    not become a hard dependency of the base module.

    **The sign separates a reply from a death.** JSON-RPC application errors are
    negative (-32602 invalid params, -32603 internal error). The SDK reuses
    *positive* HTTP status codes for its own transport failures -- a read
    timeout raises `McpError(code=408)` -- and those did not arrive from the
    server at all. Returning None for them keeps them on the crash path where
    they belong.
    """
    code = getattr(getattr(exc, "error", None), "code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        return None
    if code >= 0 or code in _TRANSPORT_DEATH_CODES:
        return None
    return code


def outer_cancellation_requested() -> bool:
    """Did something *outside* this coroutine ask for cancellation?

    Every cancel-scope handler in this project asks the same question -- is this
    cancellation ours to convert, or the caller's to honour -- and each one has
    answered it locally and differently. This is the shared half.

    **On 3.11+ this is a fact.** `asyncio.Task.cancelling()` counts outstanding
    `cancel()` calls against the running task, so a non-zero count means an
    outer scope requested cancellation and the coroutine must not swallow it.

    **On 3.10 there is no such counter, and this returns False -- a correlate,
    not the fact.** The consequence is that on 3.10 a caller's cancellation
    during a converted site may be reported as a target-side failure rather than
    propagating. That is the wrong answer, it is stated here rather than
    discovered later, and callers that must not get it wrong should check for
    their own evidence too rather than relying on this alone.

    Written this way deliberately. A docstring in this project claimed
    `Response.delivered` was "a fact about whether anything arrived" when it was
    a well-correlated stand-in, and that sentence shipped for thirteen releases
    and inverted a scored dimension. A proxy described as a proxy is a smaller
    problem than a proxy described as a fact.
    """
    task = None
    with contextlib.suppress(RuntimeError):
        task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    if cancelling is None:  # Python 3.10, or no running task.
        return False
    return cancelling() > 0


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
