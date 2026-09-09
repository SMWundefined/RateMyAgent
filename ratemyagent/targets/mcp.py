"""MCP server adapter.

Connects over stdio or SSE, discovers the server's tools, and invokes one of
them as probe traffic. The `mcp` SDK is an optional dependency and is imported
lazily so that `import ratemyagent` works without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from ..models import ErrorKind, Request, Response, TargetInfo, ToolInfo
from .base import Target, TargetError, error_response, redact_headers, redact_uri
from .mutability import Mutability, classify, describe_refusal

logger = logging.getLogger(__name__)

_INSTALL_HINT = "MCP support needs the mcp SDK: pip install 'ratemyagent[mcp]'"

#: Keys that mean "this call failed" when they appear in an otherwise successful
#: tool result. The MCP spec has `isError` for this, but FastMCP-based servers
#: commonly return a normal result with an error object in the body instead.
ERROR_PAYLOAD_KEYS: tuple[str, ...] = (
    "error", "error_type", "error_message", "error_code",
)

#: Probe calls to see before warning that synthesized arguments look invalid.
#: Enough that one unlucky rejection cannot trigger it.
ERROR_PAYLOAD_WARN_AFTER = 5


class MCPTarget(Target):
    """An MCP server reached over stdio or SSE.

    By default the adapter profiles the first discovered tool, with arguments
    synthesized from that tool's input schema. Probing invokes a real tool for
    real, so pass `tool`/`tool_args` explicitly when the first tool is not
    something you want called dozens of times.
    """

    def __init__(
        self,
        uri: str,
        *,
        tool: str | None = None,
        tool_args: dict[str, Any] | None = None,
        timeout_s: float = 30.0,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_mutating: bool = False,
    ) -> None:
        self.uri = uri
        self.timeout_s = timeout_s
        self.env = env
        #: Sent on every request over http/sse. Redacted everywhere a scan is
        #: written down -- a bearer token in a saved report is the one thing
        #: here that leaks something the user cannot rotate by re-running.
        self.headers = dict(headers) if headers else None
        self._requested_tool = tool
        self._requested_args = tool_args
        #: Confirms that calling a state-changing tool once per request, and
        #: again under fault injection, is intended.
        self.allow_mutating = allow_mutating

        self._transport, self._spec = _parse_uri(uri)

        # Raise rather than warn. `env` sets subprocess environment variables,
        # which no network transport has; accepting it silently would let a
        # caller believe they had configured something.
        if env and self._transport != "stdio":
            raise TargetError(
                f"env= is a stdio option and {self._transport}:// has no subprocess "
                "to set it on. Use headers= (--header) to send credentials."
            )
        if headers and self._transport == "stdio":
            raise TargetError(
                "headers= is an http/sse option; stdio:// has no request headers. "
                "Use env= to configure a subprocess."
            )
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._tools: list[Any] = []
        self._probe_tool: str | None = None
        self._probe_args: dict[str, Any] = {}
        self._server_name: str | None = None
        self._server_version: str | None = None

        # Only counts calls made with the arguments this adapter synthesized.
        self._probe_calls = 0
        self._probe_error_payloads = 0
        self._warned_error_payloads = False

        #: False when the server refused the `initialize` handshake. Not a
        #: failure -- stateless servers serve tools without one.
        self._handshake = True

    # -- Target interface ----------------------------------------------------


    async def _open_streamable_http(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Connect over Streamable HTTP, the transport that replaced SSE.

        Two SDK differences, both measured rather than assumed:

        1. `streamablehttp_client` (no underscore) exists only in 1.x. The
           underscored `streamable_http_client` exists in both, so that is the
           one to call -- the documented spelling is the 2.x regression.
        2. It yields three items in 1.29.1 (read, write, get_session_id) and two
           in 2.1.1. `read, write = ...` therefore raises on 1.x and works on
           2.x, which is invisible to whoever writes it on whichever major they
           happen to have installed. Index instead of unpacking.
        """
        try:
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise TargetError(
                "this mcp SDK has no streamable_http_client, so https:// cannot be "
                f"scanned with it ({exc}). Upgrade the SDK, or use sse+https:// for "
                "the deprecated transport."
            ) from exc

        import httpx

        client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=self.timeout_s, headers=self.headers or None)
        )
        streams = await stack.enter_async_context(
            streamable_http_client(self._spec[0], http_client=client)
        )
        return streams[0], streams[1]

    async def setup(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise TargetError(_INSTALL_HINT) from exc

        stack = AsyncExitStack()
        try:
            if self._transport == "stdio":
                from mcp.client.stdio import stdio_client

                command, *args = self._spec
                params = StdioServerParameters(command=command, args=args, env=self.env)
                read, write = await stack.enter_async_context(stdio_client(params))
            elif self._transport == "http":
                read, write = await self._open_streamable_http(stack)
            else:
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(self._spec[0], headers=self.headers)
                )

            session = await stack.enter_async_context(ClientSession(read, write))

            # The handshake is best-effort. MCP's stateless core (spec
            # 2026-07-28) rejects `initialize` outright while serving tools
            # normally, so treating it as mandatory refuses to scan servers
            # that work fine. list_tools() is the real gate: if that answers,
            # the server is usable.
            init = None
            try:
                init = await asyncio.wait_for(
                    session.initialize(), timeout=self.timeout_s
                )
            except Exception as exc:
                self._handshake = False
                logger.info(
                    "initialize() unavailable on %s (%s); continuing without it",
                    self.uri,
                    exc,
                )

            listing = await asyncio.wait_for(session.list_tools(), timeout=self.timeout_s)
        except TargetError:
            await self._close(stack)
            raise
        except Exception as exc:
            await self._close(stack)
            hint = ""
            if self._transport == "http" and "sse" in self._spec[0].rsplit("/", 1)[-1].lower():
                # Bare http(s):// meant SSE before 0.1.7. A path ending in /sse
                # that will not speak Streamable HTTP is most likely a server
                # still on the old transport.
                hint = (
                    f"\n\nThat path looks like an SSE endpoint. If the server still "
                    f"speaks the deprecated transport, try:\n  sse+{self._spec[0]}"
                )
            raise TargetError(
                f"could not connect to MCP server at {self.uri}: {exc}{hint}"
            ) from exc

        self._stack = stack
        self._session = session
        self._tools = list(getattr(listing, "tools", []) or [])

        info = _sdk_attr(init, "server_info", "serverInfo")
        server_info = getattr(info, "name", None)
        self._server_name = server_info or self._default_name()
        self._server_version = getattr(info, "version", None)

        try:
            self._select_probe_tool()
        except TargetError:
            # Tear down here, in the task that opened the stack. Letting the
            # refusal escape with the session still open means the stack is
            # closed later from a different task, and anyio raises "Attempted to
            # exit cancel scope in a different task" over the top of the message
            # the user actually needs to read.
            await self.teardown()
            raise

    async def invoke(self, request: Request) -> Response:
        if self._session is None:
            raise TargetError("MCPTarget.invoke() called before setup()")

        timeout = request.timeout_s or self.timeout_s
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(request.op, request.payload),
                timeout=timeout,
            )
        except Exception as exc:
            return error_response(exc, time.perf_counter() - started)

        latency = time.perf_counter() - started
        text = _result_text(result)

        if _sdk_attr(result, "is_error", "isError", default=False):
            kind, unclassified = _delivered_error_kind(text)
            return Response(
                ok=False,
                latency_s=latency,
                error=text or "tool reported an error",
                error_kind=kind,
                output=text,
                meta={"reason_unclassified": True} if unclassified else {},
            )

        # A tool can report failure without setting isError: FastMCP-based
        # servers routinely return a normal result whose body is an error
        # object. Counting those as successes makes a run where every call was
        # rejected report a 0% error rate, with latency measuring the rejection
        # path rather than the work.
        payload = _error_payload(text)
        if payload is not None:
            self._record_call(request, error_payload=True)
            kind, unclassified = _delivered_error_kind(text)
            meta: dict[str, Any] = {"error_payload": True}
            if unclassified:
                meta["reason_unclassified"] = True
            return Response(
                ok=False,
                latency_s=latency,
                error=_payload_message(payload),
                error_kind=kind,
                output=text,
                meta=meta,
            )

        self._record_call(request, error_payload=False)
        return Response(ok=True, latency_s=latency, output=text)

    def _record_call(self, request: Request, *, error_payload: bool) -> None:
        """Track whether the synthesized probe arguments are landing.

        Only calls carrying the arguments *we* invented are counted. The
        contract probe deliberately sends malformed input and would otherwise
        make every scan look like a synthesis failure.
        """
        if self._requested_args is not None or request.payload != self._probe_args:
            return

        self._probe_calls += 1
        if error_payload:
            self._probe_error_payloads += 1

        if (
            not self._warned_error_payloads
            and self._probe_calls >= ERROR_PAYLOAD_WARN_AFTER
            and self._probe_error_payloads == self._probe_calls
        ):
            self._warned_error_payloads = True
            logger.warning(
                "All responses appear to contain error payloads despite reporting "
                "success. Your synthesized arguments are likely invalid -- pass "
                "--tool and --tool-args with real values."
            )

    #: Seconds to wait for a connection to close before abandoning it. Cleanup
    #: must be bounded or it defeats the scan deadline: cancelling a scan runs
    #: the teardown, and `stack.aclose()` waits on a subprocess that has stopped
    #: answering, so the cancellation never lands and the process hangs anyway.
    CLOSE_TIMEOUT_S = 10.0

    async def _close(self, stack: AsyncExitStack) -> None:
        """Close a connection, giving up rather than waiting forever.

        Bounded with anyio rather than `asyncio.wait_for`, and the difference is
        not stylistic. On Python 3.10 and 3.11 `wait_for` wraps its awaitable in
        `ensure_future()`, so `stack.aclose()` runs in a **new task** and exits
        the MCP SDK's anyio cancel scopes from a task that never entered them:

            RuntimeError: Attempted to exit cancel scope in a different task
            than it was entered in

        which leaves the scan as a bare `CancelledError` traceback. Python 3.12
        reimplemented `wait_for` on top of `asyncio.timeout()`, which runs the
        awaitable in the *current* task, so 3.12 and 3.13 were unaffected and
        nothing run locally ever saw it. Every stdio scan on the two older
        versions failed this way from 0.1.8 -- when this bound was added to stop
        a hang -- until 0.1.16.

        `setup()` states the rule one function above the line that broke it:
        tear down in the task that opened the stack. `anyio.move_on_after` keeps
        that promise. Its cancel scope belongs to the current task and nests
        inside the SDK's own scopes instead of fighting them.

        anyio is imported here rather than at module scope for the reason the
        `mcp` SDK is: both are optional-extra dependencies, and `import
        ratemyagent` has to work without them.
        """
        import anyio

        try:
            with anyio.move_on_after(self.CLOSE_TIMEOUT_S) as scope:
                await stack.aclose()
            if scope.cancelled_caught:
                logger.warning(
                    "MCP connection to %s did not close within %.0fs; abandoning "
                    "it. The server process may outlive this scan.",
                    self.uri, self.CLOSE_TIMEOUT_S,
                )
        except Exception as exc:  # pragma: no cover - server-dependent shutdown noise
            logger.debug("MCP teardown raised during shutdown: %s", exc)

    async def teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await self._close(stack)

    def describe(self) -> TargetInfo:
        return TargetInfo(
            name=self._server_name or self._default_name(),
            kind="mcp",
            # Everything below is written to reports, JSON and the AGENTS.md
            # state block, so credentials are stripped here rather than at each
            # renderer -- one place to get right instead of four.
            uri=redact_uri(self.uri),
            capabilities=[getattr(tool, "name", "?") for tool in self._tools],
            metadata={
                "transport": self._transport,
                "headers": redact_headers(self.headers),
                "server_version": self._server_version,
                "handshake": self._handshake,
                "probe_tool": self._probe_tool,
                "probe_args": dict(self._probe_args),
                # Whether `probe_args` is what the user passed or what this
                # adapter invented. They render identically and mean opposite
                # things, and the contract probe needs the difference: it may
                # only build edge cases from a payload a human vouched for.
                "probe_args_source": (
                    "user" if self._requested_args is not None else "synthesized"
                ),
                "probe_tool_mutability": self._probe_tool_mutability(),
                "tool_count": len(self._tools),
            },
        )

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=getattr(tool, "name", "?"),
                description=getattr(tool, "description", None),
                input_schema=_sdk_attr(tool, "input_schema", "inputSchema") or {},
                # `annotations` kept its name across the 1.x/2.x rename, but the
                # hints inside are optional, so absent stays None rather than
                # becoming False.
                read_only=_hint(tool, "readOnlyHint", "read_only_hint"),
                destructive=_hint(tool, "destructiveHint", "destructive_hint"),
            )
            for tool in self._tools
        ]

    def sample_request(self, index: int = 0) -> Request:
        if self._probe_tool is None:
            raise TargetError("MCPTarget.sample_request() called before setup()")
        return Request(
            op=self._probe_tool,
            payload=dict(self._probe_args),
            timeout_s=self.timeout_s,
            label=f"{self._probe_tool}#{index}",
        )

    # -- internals -----------------------------------------------------------

    def _default_name(self) -> str:
        if self._transport in ("sse", "http"):
            return redact_uri(self._spec[-1]) or self._spec[-1]
        return " ".join(self._spec)

    def _probe_tool_mutability(self) -> str | None:
        """What the probed tool was classified as, for the record."""
        if self._probe_tool is None:
            return None
        tool = next((t for t in self.list_tools() if t.name == self._probe_tool), None)
        return classify(tool).value if tool else None

    def _auto_select(self, tools: list[ToolInfo]) -> ToolInfo:
        """Pick a tool to probe, or refuse.

        Auto-selection requires a positive READ_ONLY. MUTATING and UNKNOWN both
        refuse, and the second half of that is the point: nobody chose this tool,
        so "we could not tell" has to mean "not without you saying so". The old
        behaviour took whatever was first in the list -- which on
        `server-memory` is `create_entities` -- and warned.
        """
        for tool in tools:
            if classify(tool) is Mutability.READ_ONLY:
                if tool.name != tools[0].name:
                    logger.info(
                        "skipped %r when auto-selecting: it is not known to be read-only",
                        tools[0].name,
                    )
                logger.warning(
                    "no --tool given; profiling %r, the first read-only tool, and "
                    "invoking it for real. Pass --tool to choose a different one.",
                    tool.name,
                )
                return tool

        raise TargetError(describe_refusal(tools, tools[0]))

    def _check_explicit_choice(self, tool: ToolInfo) -> None:
        """An explicitly named mutating tool needs a second key.

        Deliberately narrower than auto-selection: naming a tool is a choice a
        person made, so UNKNOWN passes here with a warning. Only a tool known to
        mutate requires --allow-mutating, because that is the case where the
        caller may not realise the scan writes once per request.
        """
        verdict = classify(tool)
        if verdict is Mutability.MUTATING and not self.allow_mutating:
            declared = (
                "declares readOnlyHint=false" if tool.read_only is False
                else "has a name that suggests it modifies state"
            )
            raise TargetError(
                f"{tool.name!r} {declared}, and probing calls it once per request "
                f"and again under fault injection.\n\n"
                f"Re-run with --allow-mutating to confirm that is intended, and "
                f"point the scan at something disposable."
            )
        if verdict is Mutability.UNKNOWN:
            logger.warning(
                "%r is not known to be read-only: the server publishes no "
                "readOnlyHint and the name is inconclusive. Probing will call it "
                "for real, once per request.",
                tool.name,
            )

    def _select_probe_tool(self) -> None:
        tools = self.list_tools()
        names = [tool.name for tool in tools]

        if self._requested_tool is not None:
            if self._requested_tool not in names:
                available = ", ".join(name for name in names if name) or "none"
                raise TargetError(
                    f"tool {self._requested_tool!r} not found on {self.uri}; available: {available}"
                )
            chosen = next(tool for tool in tools if tool.name == self._requested_tool)
            self._check_explicit_choice(chosen)
            self._probe_tool = chosen.name
        elif tools:
            self._probe_tool = self._auto_select(tools).name
        else:
            raise TargetError(f"MCP server at {self.uri} exposes no tools to probe")

        if self._requested_args is not None:
            self._probe_args = dict(self._requested_args)
            return

        schema = next(
            (
                _sdk_attr(tool, "input_schema", "inputSchema")
                for tool in self._tools
                if getattr(tool, "name", None) == self._probe_tool
            ),
            None,
        )
        self._probe_args = synthesize_args(schema or {})

        vacuous = vacuous_required_fields(schema or {})
        if vacuous:
            raise TargetError(_describe_vacuous(self._probe_tool, vacuous, self._probe_args))

        if self._probe_args:
            logger.info("synthesized arguments for %s: %s", self._probe_tool, self._probe_args)


def _hint(tool: Any, *names: str) -> bool | None:
    """One optional boolean from a tool's annotations, or None if unstated."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    for name in names:
        value = getattr(annotations, name, None)
        if isinstance(value, bool):
            return value
    return None

def _sdk_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first attribute that exists, across MCP SDK major versions.

    The SDK renamed its model fields from camelCase to snake_case in 2.0
    (`isError` -> `is_error`, `inputSchema` -> `input_schema`, `serverInfo` ->
    `server_info`); the camelCase spellings survive only as wire aliases, not as
    Python attributes.

    Every one of those was read here with `getattr(obj, "camelCase", default)`,
    which does not raise when the attribute vanishes -- it quietly returns the
    default. With `mcp>=2` installed, `isError` read False for every failed call,
    so every tool error became a success: `mcp-server-git` scored 100/100 with
    "18 accepted" where 1.x scores 91 with "9 rejected". A scanner that reports a
    server as flawless because it cannot see the errors is worse than one that
    invents them.

    Take no default here unless one is genuinely correct: raising on an unknown
    shape is better than scoring one.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def synthesize_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal argument set satisfying a JSON Schema's required fields.

    Only required properties are filled: the goal is the cheapest call the
    server will accept, not exhaustive coverage.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: _default_for(properties.get(name) or {}) for name in required}


#: Values that satisfy a schema's `required` while telling the server nothing.
#: `{}` is deliberately absent: a required object whose sub-schema requires
#: nothing is legitimately called with `{}`, and refusing that would break tools
#: like `read_graph` that simply take no arguments.
VACUOUS_DEFAULTS: tuple[Any, ...] = ([], "")


def vacuous_required_fields(schema: dict[str, Any]) -> list[str]:
    """Required fields synthesis can only fill with "do nothing".

    Per required field, never "is the payload empty". Those look identical and
    mean opposite things: `{"entities": []}` is a required field we could not
    fill, while `{}` from a tool with no required fields is a complete and
    correct payload.
    """
    required = schema.get("required") or []
    args = synthesize_args(schema)
    return [
        name for name in required
        if any(args.get(name) == empty and type(args.get(name)) is type(empty)
               for empty in VACUOUS_DEFAULTS)
    ]

def _default_for(spec: dict[str, Any]) -> Any:
    if "default" in spec:
        return spec["default"]
    if spec.get("enum"):
        return spec["enum"][0]

    declared = spec.get("type")
    if isinstance(declared, list):
        declared = next((item for item in declared if item != "null"), "string")

    if declared == "integer":
        return int(spec.get("minimum", 1))
    if declared == "number":
        return float(spec.get("minimum", 1))
    if declared == "boolean":
        return False
    if declared == "array":
        return []
    if declared == "object":
        return synthesize_args(spec)
    return "ratemyagent probe"


def _describe_vacuous(tool: str, fields: list[str], args: dict[str, Any]) -> str:
    """The refusal. Names the field, not just the tool: the field is what the
    caller has to fill."""
    if len(fields) == 1:
        which = f"the required field {fields[0]!r} with {args[fields[0]]!r}"
    else:
        listed = " and ".join(repr(f) for f in fields)
        which = f"the required fields {listed} with empty values"

    skeleton = json.dumps(
        {name: ["..."] if args.get(name) == [] else "..." for name in fields}
    )
    return (
        f"refusing to probe {tool!r}: argument synthesis filled {which}, "
        f"which asks the server to do nothing.\n\n"
        f"Every timed call would be an empty round trip. The scan would report low "
        f"latency, a 0% error rate and full marks, and none of it would be about this "
        f"tool -- server-memory scored 100/100 that way on 20 successful no-ops.\n\n"
        f"Supply real arguments:\n"
        f"  ratemyagent scan ... --tool {tool} --tool-args '{skeleton}'"
    )

def _parse_uri(uri: str) -> tuple[str, list[str]]:
    """Split a target URI into (transport, spec).

    stdio://./server.py        -> ("stdio", [sys.executable, "./server.py"])
    stdio://node build/mcp.js  -> ("stdio", ["node", "build/mcp.js"])
    https://host/mcp           -> ("http",  ["https://host/mcp"])
    http://localhost:3001/mcp  -> ("http",  ["http://localhost:3001/mcp"])
    sse://localhost:8080/sse   -> ("sse",   ["http://localhost:8080/sse"])
    sse+https://host/sse       -> ("sse",   ["https://host/sse"])

    Bare `http(s)://` means Streamable HTTP as of 0.1.7. It used to mean SSE,
    which the 2025-06-18 spec deprecated and replaced -- so the old mapping
    pointed the only network transport at the dead protocol, and every hosted
    server failed with a TaskGroup error. SSE is still reachable, explicitly,
    via `sse+https://`.
    """
    if uri.startswith("stdio://"):
        remainder = uri[len("stdio://") :].strip()
        if not remainder:
            raise TargetError("stdio:// URI needs a command, e.g. stdio://./server.py")
        parts = shlex.split(remainder)
        if parts[0].endswith(".py"):
            parts = [sys.executable, *parts]
        return "stdio", parts

    for prefix, scheme in (("sse+https://", "https://"), ("sse+http://", "http://"), ("sse://", "http://")):
        if uri.startswith(prefix):
            return "sse", [scheme + uri[len(prefix) :]]

    if uri.startswith(("http://", "https://")):
        return "http", [uri]

    raise TargetError(
        f"unsupported MCP URI {uri!r}; expected stdio://<command>, "
        "https://<host>/<path>, or sse://<host>/<path> for the deprecated transport"
    )


def _result_text(result: Any) -> str:
    """Flatten an MCP CallToolResult's content blocks into text."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"<{getattr(block, 'type', 'content')}>")
    return "\n".join(parts)


def _error_payload(text: str) -> dict[str, Any] | None:
    """Find an error object inside a tool result that reported success.

    Returns the offending mapping, or None when the body is not JSON, is not a
    mapping, or carries no error key with a meaningful value. `{"error": null}`
    and `{"error": false}` are explicitly *not* errors -- plenty of tools
    include the key unconditionally.
    """
    if not text:
        return None

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None

    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None

    candidates = parsed if isinstance(parsed, list) else [parsed]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ERROR_PAYLOAD_KEYS:
            if key not in item:
                continue
            value = item[key]
            if value is None or value is False or value == "" or value == [] or value == {}:
                continue
            return item
    return None


def _payload_message(payload: dict[str, Any]) -> str:
    """A one-line description of an error payload, for the Response."""
    for key in ERROR_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"tool returned an error payload: {value.strip()}"
    for key in ERROR_PAYLOAD_KEYS:
        if key in payload:
            return f"tool returned an error payload: {key}={payload[key]!r}"
    return "tool returned an error payload"


def _classify_delivered_error(text: str) -> ErrorKind:
    """Classify an error the server *delivered*, defaulting to INVALID_RESPONSE.

    Deliberately never UNKNOWN: the contract probe treats UNKNOWN as a crash,
    and a tool that returns an error is *rejecting* input, which is correct
    behaviour rather than a transport failure. Whatever the wording, a message
    that arrived proves the transport carried it.

    Named `_classify_payload_error` until 0.1.4, which scoped it to the
    error-payload branch by its name alone. The `isError` branch twenty lines
    above needed the same rule and did not get it, so any rejection whose
    wording `_classify_tool_error` did not recognise was graded a crash. That
    produced a 33-50% "contract crash rate" against two published MCP servers
    that crash nothing, reported upstream in error and retracted. The invariant
    was written and tested; the name hid it from the site that needed it.
    """
    return _delivered_error_kind(text)[0]


def _delivered_error_kind(text: str) -> tuple[ErrorKind, bool]:
    """(kind, whether the substring table could not attribute a cause).

    The UNKNOWN -> INVALID_RESPONSE mapping is what stops a delivered error
    being graded a crash, but it also erases the fact that we did not recognise
    the message. That fact is worth reporting: it measures this scanner's
    coverage, not the target's behaviour, and hiding it is how the coverage gap
    silently became a crash count in the first place. The second element rides
    along in Response.meta so the contract probe can show it.
    """
    raw = _classify_tool_error(text)
    if raw is ErrorKind.UNKNOWN:
        return ErrorKind.INVALID_RESPONSE, True
    return raw, False


def _classify_tool_error(text: str) -> ErrorKind:
    lowered = (text or "").lower()
    if "rate limit" in lowered or "429" in lowered:
        return ErrorKind.RATE_LIMIT
    if "timeout" in lowered or "timed out" in lowered:
        return ErrorKind.TIMEOUT
    if any(code in lowered for code in ("500", "502", "503", "504")):
        return ErrorKind.SERVER_ERROR
    if "invalid" in lowered or "validation" in lowered or "schema" in lowered:
        return ErrorKind.INVALID_RESPONSE
    return ErrorKind.UNKNOWN
