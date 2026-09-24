#!/usr/bin/env python3
"""Gate S runner: LangGraph.

Satisfies the five obligations in DESIGN-GATE-S section 1.1. Imports nothing
from `ratemyagent`.

    --wire-check   stage A. Calls the tool directly, NO MODEL, NO SPEND.
    (otherwise)    stage B onward. One task under a hard recursion limit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

INNER_DEADLINE_S = float(os.environ.get("GATE_S_INNER_DEADLINE", "120"))
RECURSION_LIMIT = int(os.environ.get("GATE_S_MAX_TURNS", "8")) * 2
MODEL = os.environ.get("GATE_S_MODEL", "claude-haiku-4-5")
TOOL = "event"


def load_connection(config_path: str) -> dict:
    cfg = json.load(open(config_path))
    srv = cfg["mcpServers"]["ratemyagent"]
    _note_pass(srv.get("env", {}))
    return {
        "transport": "stdio",
        "command": srv["command"],
        "args": list(srv.get("args", [])),
        # The load-bearing line. Merged over os.environ, never replacing it.
        "env": {**os.environ, **srv.get("env", {})},
    }


#: Which pass this process is serving, read off RMA_PROXY_RECORD in the config's
#: env block (`record-<pass>-<task>.jsonl`). One element so the loader can set it.
PASS_NAME = [""]
#: Tokens and the returned model string, accumulated per LLM call. The final
#: graph state carries both, but a run that RAISES has no final state -- and a
#: raise is what a dropped reply produces here. Phase 1 measured $0.0000 for a
#: run that really spent.
TOKENS = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
          "models_returned_by_api": []}
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


def _versions() -> dict:
    import importlib.metadata as md
    out = {}
    for pkg in ("langgraph", "langchain-anthropic", "langchain-mcp-adapters",
                "langchain-core", "mcp", "anthropic"):
        try:
            out[pkg] = md.version(pkg)
        except Exception:
            out[pkg] = "absent"
    return out


async def wire_check(conn: dict, task: dict) -> int:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    started = time.time()
    client = MultiServerMCPClient({"ratemyagent": conn})
    loaded = await client.get_tools()
    # **The restriction here is the runner's, not the stack's.** This adapter
    # has no tool filter: get_tools() returns everything the server exposes and
    # the narrowing is this list comprehension. Asserted rather than trusted,
    # because a second callable tool acquires its own fault schedule.
    tools = [t for t in loaded if t.name == TOOL]
    names = sorted(t.name for t in tools)
    if names != [TOOL]:
        emit(False, f"tool restriction did not take: server offered "
                    f"{sorted(t.name for t in loaded)}, kept {names}")
        return 1
    tool = tools[0]
    result = await tool.ainvoke(dict(task["arguments"]))
    sidecar({
        "stack": "langgraph",
        "mode": "wire-check",
        "tools_offered_by_server": sorted(t.name for t in loaded),
        "tools_visible": names,
        "elapsed_s": round(time.time() - started, 3),
        "versions": _versions(),
    })
    emit(True, tools_visible=names, result=str(result)[:200])
    return 0


def _probe():
    """A callback that records usage as it happens, not at the end."""
    from langchain_core.callbacks import BaseCallbackHandler

    class _P(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            TOKENS["requests"] += 1
            for gens in getattr(response, "generations", []) or []:
                for gen in gens:
                    msg = getattr(gen, "message", None)
                    meta = getattr(msg, "usage_metadata", None) or {}
                    TOKENS["input_tokens"] += int(meta.get("input_tokens", 0) or 0)
                    TOKENS["output_tokens"] += int(meta.get("output_tokens", 0) or 0)
                    rm = getattr(msg, "response_metadata", {}) or {}
                    for key in ("model", "model_name"):
                        if rm.get(key) and str(rm[key]) not in TOKENS["models_returned_by_api"]:
                            TOKENS["models_returned_by_api"].append(str(rm[key]))
    return _P()


async def model_run(conn: dict, prompt: str) -> int:
    from langchain_anthropic import ChatAnthropic
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.errors import GraphRecursionError
    from langgraph.prebuilt import create_react_agent

    started = time.time()
    usage: dict = {}
    try:
        client = MultiServerMCPClient({"ratemyagent": conn})
        offered = await client.get_tools()
        tools = [t for t in offered if t.name == TOOL]
        if sorted(t.name for t in tools) != [TOOL]:
            emit(False, f"tool restriction did not take: server offered "
                        f"{sorted(t.name for t in offered)}")
            return 1
        llm = ChatAnthropic(model=MODEL, max_tokens=2048)
        agent = create_react_agent(llm, tools)
        # The runner owns the clock. Without this a stack that never returns is
        # killed by the scan and recorded `abandoned`, which is a refusal and
        # not a finding -- DESIGN-GATE-S section 6.2.
        state = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": RECURSION_LIMIT,
                        "callbacks": [_probe()]},
            ),
            timeout=INNER_DEADLINE_S,
        )
        usage = _usage(state)
        final = state["messages"][-1]
        emit(True, result=str(getattr(final, "content", final))[:400])
        code = 0
    except asyncio.TimeoutError:
        # THE HANG, recorded as a positive observation rather than a missing
        # one. A lower bound: "no action within INNER_DEADLINE_S", never
        # "hangs forever".
        emit(False, f"no action within {INNER_DEADLINE_S:.0f}s of invocation",
             hang=True, deadline_s=INNER_DEADLINE_S)
        code = 0
    except GraphRecursionError as exc:
        emit(False, f"recursion limit {RECURSION_LIMIT} reached: {exc}")
        code = 0
    except Exception as exc:  # noqa: BLE001 -- the claim is the report
        emit(False, f"{type(exc).__name__}: {exc}")
        code = 0
    sidecar({
        "stack": "langgraph",
        "mode": "model",
        "model_requested": MODEL,
        "recursion_limit": RECURSION_LIMIT,
        # Deliberately NOT set. ClientSession(read_timeout_seconds=None) is the
        # default and the adapter's stdio path sets none, so nothing bounds a
        # read but the close. Deterministic, therefore a difference under test
        # rather than a nondeterminism to repair -- the configuration principle.
        "mcp_read_timeout_s": "stack default (None)",
        "mcp_read_timeout_is_override": False,
        "inner_deadline_s": INNER_DEADLINE_S,
        "elapsed_s": round(time.time() - started, 3),
        "usage": usage or dict(TOKENS),
        "from_callback": dict(TOKENS),
        "versions": _versions(),
    })
    return code


def _usage(state) -> dict:
    """Summed from `usage_metadata` on every AIMessage (section 9.7)."""
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "messages_with_usage": 0, "models_returned_by_api": []}
    seen = set()
    try:
        for msg in state.get("messages", []):
            meta = getattr(msg, "usage_metadata", None)
            if not meta:
                continue
            total["messages_with_usage"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                total[key] += int(meta.get(key, 0) or 0)
            # The pin asserted from the RESPONSE, not from the config: a
            # `model=` we passed proves only what we asked for. langchain
            # surfaces the returned string under one of two keys depending on
            # version, so both are read and neither is assumed.
            meta_r = getattr(msg, "response_metadata", {}) or {}
            for key in ("model", "model_name"):
                if meta_r.get(key):
                    seen.add(str(meta_r[key]))
        total["models_returned_by_api"] = sorted(seen)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"usage unreadable: {type(exc).__name__}: {exc}"}
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--wire-check", action="store_true")
    ap.add_argument("--tasks")
    ap.add_argument("--task")
    args = ap.parse_args()

    conn = load_connection(args.config)
    if args.wire_check:
        tasks = json.load(open(args.tasks))["tasks"]
        task = next(t for t in tasks if str(t["id"]) == str(args.task))
        return asyncio.run(wire_check(conn, task))
    return asyncio.run(model_run(conn, args.prompt))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit(False, f"runner failed before the task: {type(exc).__name__}: {exc}")
        sys.exit(1)
