"""MCP server adapter.

Connects over stdio or SSE, discovers the server's tools, and invokes one of
them as probe traffic. The `mcp` SDK is an optional dependency and is imported
lazily so that `import ratemyagent` works without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from typing import Any

import anyio

from ..models import ErrorKind, Request, Response, TargetInfo, ToolInfo
from .base import (
    Target,
    TargetError,
    error_response,
    jsonrpc_error_code,
    outer_cancellation_requested,
    parse_retry_after,
    redact_env,
    redact_headers,
    redact_uri,
)
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


class _CapturedStderr:
    """A real file for the child's stderr, so it can be read back afterwards.

    Must be a genuine file: the SDK hands `errlog` to the subprocess and needs
    `fileno()`, so a Python object with a `write()` method is not enough -- the
    first attempt at this was exactly that and failed on every stdio scan.

    Not a silent swallow. `drain()` echoes whatever the server said to our own
    stderr once setup is over, so a user watching a scan still sees it; the
    delay is the cost of being able to report on it as well as print it.
    """

    def __init__(self, limit: int = 8000) -> None:
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        self._limit = limit
        self._text = ""

    def fileno(self) -> int:
        return self._file.fileno()

    def write(self, text: str) -> int:  # pragma: no cover - the fd is used
        return self._file.write(text)

    def flush(self) -> None:
        self._file.flush()

    def drain(self) -> str:
        """Read what the child has written so far, echo it, and remember it."""
        with contextlib.suppress(Exception):
            self._file.flush()
            self._file.seek(0)
            self._text = self._file.read(self._limit).strip()
            if self._text:
                sys.stderr.write(self._text + "\n")
                sys.stderr.flush()
        return self._text

    @property
    def text(self) -> str:
        return self._text

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._file.close()


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
        #: Did the server accept the probe payload when asked, once, at
        #: setup? None until the preflight runs, and None afterwards when
        #: nothing was delivered -- which is "no evidence", not "no".
        self._baseline_probe_ok: bool | None = None
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
        self._close_scope: anyio.CancelScope | None = None
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

        #: What the stdio child wrote to stderr while connecting. Empty for
        #: network transports, which have no such channel.
        self._stderr: "_CapturedStderr | None" = None

        #: Seconds a real `Retry-After` asked for, when the transport carried
        #: one. `None` for stdio, where an upstream 429 arrives relayed inside a
        #: tool result with no header anywhere -- the case backoff exists for.
        self._last_retry_after: float | None = None

        #: Last non-2xx HTTP status seen on the transport, when we own the
        #: client. The SDK cancels the pending request on a transport failure
        #: and the status does not survive into the exception, so it is caught
        #: on the way past instead -- see the CancelledError handling in
        #: `setup()`. `None` for stdio, and for sse:// where the SDK builds its
        #: own client.
        self._last_http_status: int | None = None

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

        async def _record_status(response: "httpx.Response") -> None:
            if response.status_code >= 400:
                self._last_http_status = response.status_code
                # Captured here because it is gone by the time the exception
                # reaches us: the SDK surfaces a transport failure without the
                # response. The hook already sees every response, so this is the
                # one place a real `Retry-After` is reachable on this path.
                self._last_retry_after = parse_retry_after(response.headers)

        client = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=self.timeout_s,
                headers=self.headers or None,
                event_hooks={"response": [_record_status]},
            )
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
        # Entered first, so it is the outermost scope on the stack and unwinds
        # last. The teardown bound is a deadline set on *this* scope rather than
        # a new scope wrapped around `aclose()` -- see `_close`.
        close_scope = anyio.CancelScope()
        stack.enter_context(close_scope)
        try:
            if self._transport == "stdio":
                from mcp.client.stdio import stdio_client

                command, *args = self._spec
                params = StdioServerParameters(command=command, args=args, env=self.env)
                # Tee the child's stderr rather than letting it reach ours. A
                # server that degrades instead of failing announces it here and
                # nowhere else -- firecrawl prints a keyless-mode banner and
                # then serves a smaller tool set perfectly happily, so the scan
                # succeeds against the wrong code path. Captured so a probe can
                # say so; still echoed, because a user watching a scan should
                # see what the server said.
                self._stderr = _CapturedStderr()
                read, write = await stack.enter_async_context(
                    stdio_client(params, errlog=self._stderr)
                )
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
        except asyncio.CancelledError:
            # `asyncio.CancelledError` inherits from `BaseException`, not
            # `Exception`, so the handler below never saw it. When the transport
            # fails -- a 401, or anything that is not MCP -- the SDK's anyio
            # scope cancels the pending request, and that cancellation walked
            # straight past the conversion to `TargetError`. The user got a
            # traceback ending in `anyio/streams/memory.py`, naming neither
            # their URL nor the status code, and the CLI exited **1**, which
            # under this project's contract means "the target failed its
            # policy" for a scan that never connected.
            #
            # Second escape of this shape: 0.1.20 fixed a cancel scope unwinding
            # in the wrong order, swallowed by a broad `except Exception` in
            # `_close`. Cancellation is not an error subclass and does not
            # behave like one; every handler on this path has to say so
            # explicitly.
            #
            # **Only converted when there is evidence the transport failed.**
            # A genuine cancellation from the caller -- Ctrl-C, an outer
            # timeout, the scan deadline -- must stay a cancellation, or the
            # scanner starts reporting the user's own interrupt as a target
            # fault. A recorded non-2xx status is that evidence; without one the
            # cancellation is re-raised untouched.
            await self._close(stack)
            status = self._last_http_status
            if status is None:
                raise
            hint = (
                " Check the credentials passed with --header."
                if status in (401, 403)
                else ""
            )
            raise TargetError(
                f"could not connect to MCP server at {self.uri}: the transport "
                f"returned HTTP {status} before the MCP session was "
                f"established.{hint}"
            ) from None
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
            # The recorded status belongs here too. A 404 surfaces as a real
            # exception rather than a cancellation, so it reaches this handler
            # instead -- and "Session terminated" tells a user nothing they can
            # act on, while "HTTP 404" tells them the path is wrong. Which
            # branch catches a transport failure is an SDK detail; the status is
            # the same fact either way.
            status = self._last_http_status
            if status is not None:
                hint = (
                    " Check the credentials passed with --header."
                    if status in (401, 403)
                    else hint
                )
                raise TargetError(
                    f"could not connect to MCP server at {self.uri}: the transport "
                    f"returned HTTP {status} ({exc}).{hint}"
                ) from exc
            raise TargetError(
                f"could not connect to MCP server at {self.uri}: {exc}{hint}"
            ) from exc

        if self._stderr is not None:
            self._stderr.drain()

        self._stack = stack
        self._close_scope = close_scope
        self._session = session
        self._tools = list(getattr(listing, "tools", []) or [])

        info = _sdk_attr(init, "server_info", "serverInfo")
        server_info = getattr(info, "name", None)
        self._server_name = server_info or self._default_name()
        self._server_version = getattr(info, "version", None)

        try:
            self._select_probe_tool()
            await self._preflight()
        except TargetError:
            # Tear down here, in the task that opened the stack. Letting the
            # refusal escape with the session still open means the stack is
            # closed later from a different task, and anyio raises "Attempted to
            # exit cancel scope in a different task" over the top of the message
            # the user actually needs to read.
            await self.teardown()
            raise

    async def _preflight(self) -> None:
        """Ask the server whether the probe payload is usable, before scoring it.

        **The dynamic twin of `vacuous_required_fields`.** That check asks the
        *schema* whether synthesis could fill the required fields, and it passes
        for `fetch`: `{"url": "ratemyagent probe"}` satisfies `type: string`
        and is not in `VACUOUS_DEFAULTS`. It is also not a URL, so every call in
        the scan is rejected -- and with no call to the server, nothing found
        out. A schema-shaped guard with no server-shaped twin.

        What that cost: a 20-request scan of a healthy server published
        `Every one of the 20 requests failed`, `something is broken at any
        load`, and **43/100**. Our defect, published as theirs, which is the
        retraction this project already made once.

        This is *not* the contract probe's control, and the two are not
        interchangeable:

        - Contract's control measures **delivery, not success** -- its docstring
          says so, and says it "works with synthesized arguments and needs no
          valid ones". It answers *is the session alive?*
        - This measures **acceptance**. It answers *is this payload usable?*

        Run 3 is the case where delivery is perfect and acceptance is zero, so
        contract's control returns `clean` and is right to.

        One call, once. Contract interleaves 1:1 because a target can die
        partway through a probe and pass a control taken before it died. The
        asymmetry here is deliberate and is the whole design: **a preflight that
        fails proves the fault is ours; a preflight that passes, followed by
        failure, proves it is the target's.** The second is the case worth
        scoring, and it survives.

        Note this does call the tool for real, once, in addition to the probes.
        For a tool cleared by `--allow-mutating` that is one more write, which is
        the same bargain `_select_probe_tool` already warns about.
        """
        response = await self.invoke(self.sample_request(0))

        if not response.delivered:
            # Nothing arrived, so this says nothing about the payload. Refusing
            # here would turn one flaky call into a failed scan, and the probes
            # have their own handling for a target that will not answer.
            # `None` means undetermined, and the probes treat it as "no
            # evidence" rather than as either verdict.
            self._baseline_probe_ok = None
            return

        self._baseline_probe_ok = response.ok
        if response.ok:
            return

        # Delivered and refused: a semantic answer from a server that is
        # demonstrably up. Deterministic, so it is safe to act on without a
        # retry -- which is why refusal is keyed on rejection and never on a
        # transport failure.
        if self._requested_args is not None:
            # A person vouched for these arguments. Their scan, their call: warn
            # loudly and let the probes withhold rather than overriding them.
            logger.warning(
                "%s rejected the --tool-args you passed: %s. Probes that depend "
                "on a working baseline will withhold rather than score it.",
                self._probe_tool, _first_line(response.error),
            )
            return

        # Recorded, not raised. The refusal lives in the scanner, because
        # whether this is fatal depends on which probes will run and `setup()`
        # cannot know that. The contract probe synthesizes its own baseline per
        # tool and attributes per case, so it produces real findings against a
        # tool that refuses its baseline -- that behaviour is deliberate, tested,
        # and would be unreachable if this raised here.
        logger.warning(
            "%s rejected the arguments this scan synthesized for it: %s",
            self._probe_tool, _first_line(response.error),
        )

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
        except asyncio.CancelledError:
            # Site 7 of the cancel-scope family, and the third escape.
            #
            # `invoke()` must **return** a Response, not raise: probes call it
            # in a loop and an exception ends the phase. But cancellation here
            # has two causes needing opposite treatment, and the policy differs
            # from `setup()`'s for that reason -- there is no single wrapper
            # that is right at both sites.
            #
            #   * the SDK's own scope gave up on this call -> a failed request,
            #     which the scan should record and continue past
            #   * the caller cancelled us -- Ctrl-C, the scan deadline -- which
            #     must propagate, or the scan cannot be stopped
            #
            # `outer_cancellation_requested()` separates them, and is a fact on
            # 3.11+ and a correlate on 3.10; see its docstring. When it says the
            # caller asked, re-raise untouched.
            if outer_cancellation_requested():
                raise
            elapsed = time.perf_counter() - started
            return Response(
                ok=False,
                latency_s=elapsed,
                error=(
                    "the MCP session cancelled this call before it returned "
                    f"(timeout {timeout:.0f}s)"
                ),
                error_kind=ErrorKind.TIMEOUT,
                delivered=False,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            code = jsonrpc_error_code(exc)
            if code is not None:
                return self._delivered_jsonrpc_error(exc, code, elapsed)
            return error_response(exc, elapsed)

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

    async def _close(
        self, stack: AsyncExitStack, scope: "anyio.CancelScope | None" = None
    ) -> None:
        """Close a connection, giving up rather than waiting forever.

        The bound is a **deadline on a scope this target already owns**, not a
        new scope wrapped around the unwind. That distinction is the whole bug
        history of this method.

        0.1.8 bounded it with `asyncio.wait_for`, which on Python 3.10 and 3.11
        runs its awaitable in a *new task* -- so `aclose()` exited the SDK's
        anyio scopes from a task that never entered them, and every stdio scan
        on those versions died. 0.1.16 replaced it with `anyio.move_on_after`,
        which fixed the task and broke the nesting: a cancel scope must be
        unwound innermost-first, and wrapping `aclose()` in a fresh scope means
        the close exits an *outer* scope while a newer inner one is still open:

            RuntimeError: Attempted to exit a cancel scope that isn't the
            current task's current cancel scope

        Two fixes, two violations of one invariant -- unwind a scope in the
        structure that created it -- each satisfying the part the previous one
        got wrong.

        `close_scope` is entered first in `setup()`, so it is outermost and
        `aclose()` unwinds it last. Setting its deadline bounds everything
        inside it without adding a level. Verified against the SDK directly:
        four sequential sessions in one event loop, one with a deliberately
        hanging teardown, all bounded and all recovering.
        """
        scope = scope or self._close_scope
        if scope is not None:
            scope.deadline = anyio.current_time() + self.CLOSE_TIMEOUT_S

        try:
            await stack.aclose()
        except asyncio.CancelledError:
            # Site 8, and the one nobody had looked at.
            #
            # The policy here is the **opposite** of `setup()`'s: swallow and
            # log, never propagate. `_close()` runs from `teardown()`, which
            # runs from `finally` blocks and `__aexit__`, so a raise here
            # replaces whatever the caller was already handling -- a scan that
            # failed for a real reason would report the cancellation instead.
            # That is the 0.1.16 shape ("surfaced as an unrelated
            # CancelledError in the next setup()") pointed forward.
            #
            # Swallowed even when the caller requested it. A cancellation that
            # arrives during shutdown has nothing left to interrupt: the work is
            # already over, and honouring it only destroys the diagnosis.
            logger.debug("MCP teardown cancelled; connection abandoned")
            return
        except RuntimeError:
            # Never swallowed. Cancel-scope misuse is a defect in *our*
            # structure, not shutdown noise from a server, and swallowing it is
            # why the 0.1.16 regression stayed invisible: the RuntimeError was
            # logged at debug, teardown reported success, and the corrupted
            # scope state surfaced as an unrelated CancelledError in the next
            # `setup()`. A broad `except` around a structural error converts a
            # loud failure into a silent one somewhere else.
            raise
        except Exception as exc:  # pragma: no cover - server-dependent shutdown
            logger.debug("MCP teardown raised during shutdown: %s", exc)
            return

        if scope is not None and scope.cancelled_caught:
            logger.warning(
                "MCP connection to %s did not close within %.0fs; abandoning "
                "it. The server process may outlive this scan.",
                self.uri, self.CLOSE_TIMEOUT_S,
            )

    def _delivered_jsonrpc_error(
        self, exc: BaseException, code: int, latency_s: float
    ) -> Response:
        """A JSON-RPC error the server sent back, not a transport that died.

        This is the fix for the largest defect found since the retracted crash
        finding, and it is that finding one layer down. 0.1.13 moved crash
        detection off error wording onto `Response.delivered` and recorded the
        change as reading "a fact about whether anything arrived". `delivered`
        was never that fact -- it was set from a single site that builds a
        Response out of a raised exception, so it meant "the SDK raised rather
        than returned". The two agree for every transport death and diverge for
        exactly one case: a server that validates a malformed call and answers
        with a JSON-RPC error.

        The consequence was an inverted metric. A server declaring
        `additionalProperties: false` and enforcing it -- the correct behaviour,
        and the only one available to a strict schema -- was recorded as
        crashing on every malformed input, and scored *worse* than a server that
        validates nothing.

        `-32602 invalid params` is a stronger signal than any substring table:
        the code says the server rejected the arguments, in a field defined by
        the spec rather than by the server's choice of words. Cases that reach
        here with that code no longer land in `rejected_unclassified`.
        """
        message = getattr(getattr(exc, "error", None), "message", None) or str(exc)
        # INVALID_PARAMS and INVALID_REQUEST are the spec's way of saying "your
        # input was wrong", which is a rejection whatever the prose says.
        if code in (-32602, -32600):
            kind, unclassified = ErrorKind.INVALID_RESPONSE, False
        else:
            kind, unclassified = _delivered_error_kind(message)
        meta: dict[str, Any] = {
            "jsonrpc_code": code, "reason_unclassified": unclassified,
        }
        if kind is ErrorKind.RATE_LIMIT and self._last_retry_after is not None:
            meta["retry_after_s"] = self._last_retry_after
        return Response(
            ok=False,
            latency_s=latency_s,
            error=f"jsonrpc {code}: {message}",
            error_kind=kind,
            delivered=True,
            meta=meta,
        )

    async def teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await self._close(stack)

    @property
    def setup_stderr(self) -> str:
        """Anything the server said on stderr while starting up."""
        return self._stderr.text if self._stderr is not None else ""

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
                "env": redact_env(self.env),
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
                # Whether the server accepted that payload when asked directly.
                # On `metadata` and deliberately not in `context.artifacts`:
                # that channel reached the contract probe only on the default
                # probe ordering and vanished silently on `--probes contract`,
                # leaving a crash rate that still printed and still capped the
                # score -- see `Control`. Metadata reaches every probe on every
                # ordering, including a probe run on its own.
                "baseline_probe_ok": self._baseline_probe_ok,
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

def _first_line(text: str | None, limit: int = 200) -> str:
    """One line of a server's error, for a log or a refusal.

    Server errors arrive as anything from a word to a stack trace. The refusal
    below quotes it because the server's own wording is the most useful thing in
    the message, and the log line quotes it for the same reason -- but neither
    is improved by four hundred characters of traceback.
    """
    if not text:
        return "(no message)"
    collapsed = " ".join(str(text).split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def _describe_rejected_baseline(
    tool: str | None, args: dict[str, Any], error: str | None
) -> str:
    """The refusal when the server rejects arguments this adapter invented.

    The dynamic counterpart of `_describe_vacuous`, and refusing rather than
    scoring is the same judgement: a measurement that cannot be attributed to
    the target is not a measurement of the target. `--allow-mutating` and the
    vacuous check both already refuse at setup, and the alternative here is the
    90 lines of false findings and a 43/100 that prompted this.

    Quotes what was sent and what came back, because the gap between them is
    usually self-evident -- `{"url": "ratemyagent probe"}` against a tool that
    wanted a URL needs no further explanation.
    """
    sent = json.dumps(args, sort_keys=True)
    return (
        f"refusing to probe {tool!r}: it rejected the arguments this scan "
        f"synthesized for it.\n\n"
        f"  sent:  {sent}\n"
        f"  said:  {_first_line(error)}\n\n"
        f"Argument synthesis fills required fields from the schema's types, "
        f"which satisfies the schema without satisfying the tool. Every probe "
        f"would measure that rejection rather than the target: the scan would "
        f"report a 100% error rate, 'something is broken at any load', and a "
        f"failing score, none of it about this server.\n\n"
        f"Supply arguments the tool accepts:\n"
        f"  ratemyagent scan ... --tool {tool} --tool-args '{{...}}'"
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
