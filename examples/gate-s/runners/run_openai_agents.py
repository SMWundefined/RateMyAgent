#!/usr/bin/env python3
"""Gate S runner: the OpenAI Agents SDK.

Satisfies the five obligations in DESIGN-GATE-S section 1.1. Imports nothing
from `ratemyagent`: it reads a JSON file and starts a subprocess, and that is
the whole of its contact with the tool.

    --wire-check   stage A. Calls the tool directly, NO MODEL, NO SPEND.
    (otherwise)    stage B onward. One task under a hard turn cap.

The claim is the last JSON line on stdout, per `_parse_claim`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

#: The runner's own deadline, measured from the faulted invocation rather than
#: from process start -- see the phase 0 correction in GATE-S-PHASE0. On expiry
#: the runner PRINTS A CLAIM rather than being killed, which is what turns a
#: hang into a positive observation instead of a refusal.
INNER_DEADLINE_S = float(os.environ.get("GATE_S_INNER_DEADLINE", "120"))
#: The SDK's own MCP read deadline, and the one override this arm carries.
#:
#: `MCPServerStdio` defaults it to 5 s, which is the same value as the proxy's
#: `--lost-reply-close-after 5`: two clocks at one value, and whichever fires
#: first decides whether the model sees a timeout error or a closed session --
#: two faults the tool counts separately and never sums. That is
#: nondeterminism, not difference, so the configuration principle overrides it
#: for the gate replicates.
#:
#: "default" leaves it unset, which is what the --hold-reply probe runs at:
#: that probe's whole job is to measure the stack's own deadline as a user
#: would meet it (design addendum 2, amendment 3).
_mcp = os.environ.get("GATE_S_MCP_TIMEOUT", "120")
MCP_READ_TIMEOUT: float | None | str = "default" if _mcp == "default" else float(_mcp)
MAX_TURNS = int(os.environ.get("GATE_S_MAX_TURNS", "8"))
MODEL = os.environ.get("GATE_S_MODEL", "claude-haiku-4-5")
TOOL = "event"


def load_server_spec(config_path: str) -> dict:
    """The MCP config the scan wrote, read as data.

    The `env` block is the load-bearing part: `RMA_PROXY_RECORD` is how the
    record file -- the only account of what this stack actually did -- comes to
    exist. Merged over `os.environ` rather than replacing it, because the child
    is a Python process that needs PATH and friends.
    """
    cfg = json.load(open(config_path))
    srv = cfg["mcpServers"]["ratemyagent"]
    _note_pass(srv.get("env", {}))
    return {
        "command": srv["command"],
        "args": list(srv.get("args", [])),
        "env": {**os.environ, **srv.get("env", {})},
    }


#: Which pass this process is serving, read off RMA_PROXY_RECORD in the config's
#: env block (`record-<pass>-<task>.jsonl`). One element so the loader can set it.
PASS_NAME = [""]
#: Process start, so a claim can say how long the stack waited before making it.
STARTED = [time.time()]


def _note_pass(env: dict) -> None:
    name = os.path.basename(env.get("RMA_PROXY_RECORD", ""))
    parts = name.split("-")
    PASS_NAME[0] = parts[1] if len(parts) > 2 else ""

def emit(ok: bool, error: str | None = None, **extra) -> None:
    """The claim, on stdout as the last JSON line -- and a copy on disk.

    The copy is not decoration. `_run_task` parses the claim and keeps nothing
    else, so without it the only account of what this runner said is discarded
    by the scan (DESIGN-GATE-S section 9.7).
    """
    body = {"ok": bool(ok)}
    if error:
        body["error"] = error
    body.update(extra)
    print(json.dumps(body), flush=True)
    base = os.environ.get("GATE_S_SIDECAR")
    if base:
        root, _ = os.path.splitext(base)
        record = dict(body)
        record["elapsed_since_start_s"] = round(time.time() - STARTED[0], 3)
        try:
            with open(f"{root}-{PASS_NAME[0] or 'unknown'}.claim.json", "w") as h:
                json.dump(record, h, indent=2)
        except OSError:
            pass


def sidecar(payload: dict) -> None:
    """Per-run stack identity and usage (sections 9.7 and 10.4).

    One file PER PASS: the scan runs this process twice against one config
    directory (baseline, then chaos) and a single path would leave only the
    last. The pass name is read off the record path the scan itself chose.
    """
    base = os.environ.get("GATE_S_SIDECAR")
    if not base:
        return
    which = PASS_NAME[0] or "unknown"
    root, ext = os.path.splitext(base)
    with open(f"{root}-{which}{ext}", "w") as handle:
        json.dump(payload, handle, indent=2)


async def wire_check(spec: dict, task: dict) -> int:
    """Stage A: prove the contract without a model.

    Connects through the config, lists tools, calls the one tool once with the
    task's own arguments, and prints a claim. Everything the model would do
    except the deciding.
    """
    from agents.mcp import MCPServerStdio, create_static_tool_filter

    started = time.time()
    server = _server(MCPServerStdio, create_static_tool_filter, spec)
    async with server:
        tools = await server.list_tools()
        names = sorted(t.name for t in tools)
        if names != [TOOL]:
            # The fault schedule is keyed (task_id, tool, ordinal); a second
            # callable tool acquires its own schedule and the run is not a
            # replicate of the others. Refuse rather than measure it.
            emit(False, f"tool restriction did not take: {names}")
            return 1
        result = await server.call_tool(TOOL, dict(task["arguments"]))
        sidecar({
            "stack": "openai-agents",
            "mode": "wire-check",
            "tools_visible": names,
            "elapsed_s": round(time.time() - started, 3),
            "versions": _versions(),
        })
        emit(True, tools_visible=names,
             result=str(getattr(result, "content", result))[:200])
    return 0


#: Every model string the API ITSELF returned, captured off LiteLLM's success
#: hook. The pin must be asserted from the response, never from the config: a
#: `model=` we passed proves only what we asked for. `ModelResponse` in this SDK
#: carries no model field, so the hook is the only place the returned string is
#: visible without a second API call.
MODELS_RETURNED: list[str] = []
#: Tokens, accumulated off the same hook. `RunResult` carries the usage, but a
#: run that RAISES has no result -- and a raise is this arm's normal outcome
#: under a dropped reply. Phase 1 measured $0.0000 for two runs that really
#: spent, which is an undercount, not a saving.
TOKENS = {"requests": 0, "input_tokens": 0, "output_tokens": 0}


def _install_model_probe() -> None:
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        class _Probe(CustomLogger):
            def _note(self, response_obj):
                name = getattr(response_obj, "model", None)
                if name and name not in MODELS_RETURNED:
                    MODELS_RETURNED.append(str(name))
                u = getattr(response_obj, "usage", None)
                if u is not None:
                    TOKENS["requests"] += 1
                    for src, dst in (("prompt_tokens", "input_tokens"),
                                     ("completion_tokens", "output_tokens")):
                        TOKENS[dst] += int(getattr(u, src, 0) or 0)

            async def async_log_success_event(self, kwargs, response_obj,
                                              start_time, end_time):
                self._note(response_obj)

            def log_success_event(self, kwargs, response_obj,
                                  start_time, end_time):
                self._note(response_obj)

        litellm.callbacks = [*getattr(litellm, "callbacks", []), _Probe()]
    except Exception as exc:  # noqa: BLE001
        MODELS_RETURNED.append(f"probe unavailable: {type(exc).__name__}")


def _server(MCPServerStdio, create_static_tool_filter, spec):
    """The MCP server object, with the read deadline set or deliberately not.

    Passing nothing leaves the SDK's own 5 s default in place, which is what
    the --hold-reply probe needs and what the gate replicates must not have.
    """
    kwargs = dict(
        params=spec,
        cache_tools_list=False,
        tool_filter=create_static_tool_filter(allowed_tool_names=[TOOL]),
    )
    if MCP_READ_TIMEOUT != "default":
        kwargs["client_session_timeout_seconds"] = MCP_READ_TIMEOUT
    return MCPServerStdio(**kwargs)


def _versions() -> dict:
    import importlib.metadata as md
    out = {}
    for pkg in ("openai-agents", "litellm", "mcp", "openai"):
        try:
            out[pkg] = md.version(pkg)
        except Exception:
            out[pkg] = "absent"
    return out


async def model_run(spec: dict, prompt: str) -> int:
    """Stage B onward: the model chooses its own calls, under a hard cap."""
    from agents import Agent, Runner
    from agents.exceptions import MaxTurnsExceeded
    from agents.extensions.models.litellm_model import LitellmModel
    from agents.mcp import MCPServerStdio, create_static_tool_filter

    _install_model_probe()
    started = time.time()
    server = _server(MCPServerStdio, create_static_tool_filter, spec)
    usage = {}
    try:
        async with server:
            agent = Agent(
                name="gate-s",
                instructions="",
                model=LitellmModel(
                    model=f"anthropic/{MODEL}",
                    api_key=os.environ.get("ANTHROPIC_API_KEY"),
                ),
                mcp_servers=[server],
            )
            names = sorted(t.name for t in await server.list_tools())
            if names != [TOOL]:
                emit(False, f"tool restriction did not take: {names}")
                return 1
            # The runner owns the outer clock too: without it a stack that
            # never returns is killed by the scan and recorded `abandoned`,
            # which is a refusal and not a finding (design section 6.2).
            result = await asyncio.wait_for(
                Runner.run(agent, prompt, max_turns=MAX_TURNS),
                timeout=INNER_DEADLINE_S,
            )
            usage = _usage(result)
            emit(True, result=str(result.final_output)[:400])
            code = 0
    except asyncio.TimeoutError:
        emit(False, f"no action within {INNER_DEADLINE_S:.0f}s of invocation",
             hang=True, deadline_s=INNER_DEADLINE_S)
        code = 0
    except MaxTurnsExceeded as exc:
        emit(False, f"turn cap {MAX_TURNS} reached: {exc}")
        code = 0
    except Exception as exc:  # noqa: BLE001 -- the claim is the report
        # A raise IS the observation for this stack. It is reported as a claim,
        # never swallowed and never re-raised: an unhandled exception prints no
        # claim, and no claim refuses the scan instead of recording a failure.
        emit(False, f"{type(exc).__name__}: {exc}")
        code = 0
    sidecar({
        "stack": "openai-agents",
        "mode": "model",
        "model_requested": f"anthropic/{MODEL}",
        "max_turns": MAX_TURNS,
        "mcp_read_timeout_s": MCP_READ_TIMEOUT,
        "mcp_read_timeout_is_override": MCP_READ_TIMEOUT != "default",
        "inner_deadline_s": INNER_DEADLINE_S,
        "elapsed_s": round(time.time() - started, 3),
        # `usage or TOKENS`: on the raise path there is no RunResult to read,
        # so the callback's running total is the only account of what was spent.
        "usage": usage or dict(TOKENS),
        "models_returned_by_api": list(MODELS_RETURNED),
        "versions": _versions(),
    })
    return code


def _usage(result) -> dict:
    """Token usage, read back from the SDK's own accounting (section 9.7)."""
    try:
        u = result.context_wrapper.usage
        out = {
            "requests": u.requests,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "total_tokens": u.total_tokens,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"usage unreadable: {type(exc).__name__}: {exc}"}
    out["models_returned_by_api"] = list(MODELS_RETURNED)
    out["from_callback"] = dict(TOKENS)
    out["responses"] = len(getattr(result, "raw_responses", []) or [])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--wire-check", action="store_true")
    ap.add_argument("--tasks")
    ap.add_argument("--task")
    args = ap.parse_args()

    spec = load_server_spec(args.config)
    if args.wire_check:
        tasks = json.load(open(args.tasks))["tasks"]
        task = next(t for t in tasks if str(t["id"]) == str(args.task))
        return asyncio.run(wire_check(spec, task))
    return asyncio.run(model_run(spec, args.prompt))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit(False, f"runner failed before the task: {type(exc).__name__}: {exc}")
        sys.exit(1)
