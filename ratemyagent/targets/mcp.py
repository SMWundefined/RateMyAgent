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
from .base import Target, TargetError, error_response

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
    ) -> None:
        self.uri = uri
        self.timeout_s = timeout_s
        self.env = env
        self._requested_tool = tool
        self._requested_args = tool_args

        self._transport, self._spec = _parse_uri(uri)
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
            else:
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(sse_client(self._spec[0]))

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
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise TargetError(f"could not connect to MCP server at {self.uri}: {exc}") from exc

        self._stack = stack
        self._session = session
        self._tools = list(getattr(listing, "tools", []) or [])

        server_info = getattr(getattr(init, "serverInfo", None), "name", None)
        self._server_name = server_info or self._default_name()
        self._server_version = getattr(getattr(init, "serverInfo", None), "version", None)

        self._select_probe_tool()

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

        if getattr(result, "isError", False):
            return Response(
                ok=False,
                latency_s=latency,
                error=text or "tool reported an error",
                error_kind=_classify_tool_error(text),
                output=text,
            )

        # A tool can report failure without setting isError: FastMCP-based
        # servers routinely return a normal result whose body is an error
        # object. Counting those as successes makes a run where every call was
        # rejected report a 0% error rate, with latency measuring the rejection
        # path rather than the work.
        payload = _error_payload(text)
        if payload is not None:
            self._record_call(request, error_payload=True)
            return Response(
                ok=False,
                latency_s=latency,
                error=_payload_message(payload),
                error_kind=_classify_payload_error(text),
                output=text,
                meta={"error_payload": True},
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

    async def teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:  # pragma: no cover - server-dependent shutdown noise
                logger.debug("MCP teardown raised during shutdown: %s", exc)

    def describe(self) -> TargetInfo:
        return TargetInfo(
            name=self._server_name or self._default_name(),
            kind="mcp",
            uri=self.uri,
            capabilities=[getattr(tool, "name", "?") for tool in self._tools],
            metadata={
                "transport": self._transport,
                "server_version": self._server_version,
                "handshake": self._handshake,
                "probe_tool": self._probe_tool,
                "probe_args": dict(self._probe_args),
                "tool_count": len(self._tools),
            },
        )

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=getattr(tool, "name", "?"),
                description=getattr(tool, "description", None),
                input_schema=getattr(tool, "inputSchema", None) or {},
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
        return self._spec[-1] if self._transport == "sse" else " ".join(self._spec)

    def _select_probe_tool(self) -> None:
        names = [getattr(tool, "name", None) for tool in self._tools]

        if self._requested_tool is not None:
            if self._requested_tool not in names:
                available = ", ".join(name for name in names if name) or "none"
                raise TargetError(
                    f"tool {self._requested_tool!r} not found on {self.uri}; available: {available}"
                )
            self._probe_tool = self._requested_tool
        elif names and names[0]:
            self._probe_tool = names[0]
            logger.warning(
                "no --tool given; profiling %r and invoking it for real. "
                "Pass --tool to choose a different one.",
                self._probe_tool,
            )
        else:
            raise TargetError(f"MCP server at {self.uri} exposes no tools to probe")

        if self._requested_args is not None:
            self._probe_args = dict(self._requested_args)
            return

        schema = next(
            (
                getattr(tool, "inputSchema", None)
                for tool in self._tools
                if getattr(tool, "name", None) == self._probe_tool
            ),
            None,
        )
        self._probe_args = synthesize_args(schema or {})
        if self._probe_args:
            logger.info("synthesized arguments for %s: %s", self._probe_tool, self._probe_args)


def synthesize_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal argument set satisfying a JSON Schema's required fields.

    Only required properties are filled: the goal is the cheapest call the
    server will accept, not exhaustive coverage.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: _default_for(properties.get(name) or {}) for name in required}


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


def _parse_uri(uri: str) -> tuple[str, list[str]]:
    """Split a target URI into (transport, spec).

    stdio://./server.py        -> ("stdio", [sys.executable, "./server.py"])
    stdio://node build/mcp.js  -> ("stdio", ["node", "build/mcp.js"])
    sse://localhost:8080/sse   -> ("sse",   ["http://localhost:8080/sse"])
    sse+https://host/sse       -> ("sse",   ["https://host/sse"])
    https://host/sse           -> ("sse",   ["https://host/sse"])
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
        return "sse", [uri]

    raise TargetError(
        f"unsupported MCP URI {uri!r}; expected stdio://<command> or sse://<host>/<path>"
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


def _classify_payload_error(text: str) -> ErrorKind:
    """Classify an error payload, defaulting to INVALID_RESPONSE.

    Deliberately never UNKNOWN: the contract probe treats UNKNOWN as a crash,
    and a tool that returns a structured error is *rejecting* input, which is
    correct behaviour rather than a transport failure.
    """
    kind = _classify_tool_error(text)
    return ErrorKind.INVALID_RESPONSE if kind is ErrorKind.UNKNOWN else kind


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
