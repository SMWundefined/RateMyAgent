#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "langgraph==1.2.12",
#     "langgraph-prebuilt==1.1.0",
#     "langchain-mcp-adapters==0.3.1",
#     "langchain-core==1.6.4",
#     "mcp==1.30.0",
# ]
# ///
"""RateMyAgent runner: a LangGraph agent, one task per process.

A LangGraph agent has no command line, so a scan cannot launch it directly.
This file is the part a scan needs: it reads the MCP config the scan wrote,
starts the server exactly as that config says (which starts `ratemyagent
proxy`), runs one task, and prints one claim.

**Edit `build_agent()` and nothing else.** It receives the scan's MCP tools
and returns your compiled graph -- your model, your prompt, your other tools.

    ratemyagent scan --target agent --agent-kind llm \\
        --agent "python run_langgraph.py" \\
        --agent-command '--config {config} --prompt {prompt} --tasks {tasks} --task {task_id}' \\
        ...

`--scripted` runs with no model at all: a scripted chat model calls the task's
`tool` with its `arguments` and then answers. It costs nothing and needs no
API key, which makes it the dry run of the plumbing and what CI runs. It never
builds a real model.

The claim is the last JSON line on stdout: {"ok": true|false, ...}.

Imports nothing from `ratemyagent`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

#: The runner's own deadline for one task, in seconds. On expiry it PRINTS A
#: CLAIM rather than being killed: the adapter's stdio path sets no read
#: timeout, so after a dropped reply only this, or the scan's
#: --lost-reply-close-after, ends the wait. Keep it below the scan's --timeout.
INNER_DEADLINE_S = 120.0

#: A hard cap on graph steps, so a looping agent ends as a claim.
RECURSION_LIMIT = 16

#: Tool names the agent may see, or None for every tool the server offers.
#: This adapter has no tool filter of its own, so the narrowing is here.
ALLOWED_TOOLS: list[str] | None = None


def build_agent(tools: list):
    """EDIT THIS: return your compiled graph, given the scan's MCP tools.

    `tools` are the server's tools as LangChain tools; add your own beside
    them if the agent has others.
    """
    from langchain_anthropic import ChatAnthropic  # your model's package
    from langgraph.prebuilt import create_react_agent

    return create_react_agent(ChatAnthropic(model="...", max_tokens=2048), tools)


def load_connection(config_path: str) -> dict:
    """The MCP config the scan wrote, as a langchain-mcp-adapters connection.

    The `env` block is the load-bearing part: `RMA_PROXY_RECORD` is how the
    record comes to exist. Merged over `os.environ`, never replacing it.
    """
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    server = config["mcpServers"]["ratemyagent"]
    return {
        "transport": "stdio",
        "command": server["command"],
        "args": list(server.get("args", [])),
        "env": {**os.environ, **server.get("env", {})},
    }


def emit(ok: bool, error: str | None = None, **extra) -> None:
    """The claim, on stdout, as the last JSON line."""
    body = {"ok": bool(ok)}
    if error:
        body["error"] = error
    body.update(extra)
    print(json.dumps(body), flush=True)


def load_task(tasks_path: str | None, task_id: str | None) -> dict:
    if not tasks_path or not task_id:
        raise SystemExit(
            "--scripted needs the task's tool and arguments: add "
            "--tasks {tasks} --task {task_id} to --agent-command"
        )
    with open(tasks_path, encoding="utf-8") as handle:
        body = json.load(handle)
    tasks = body["tasks"] if isinstance(body, dict) else body
    return next(task for task in tasks if str(task["id"]) == str(task_id))


def scripted_agent(task: dict, tools: list):
    """A graph whose model is scripted: call the task's tool once, then answer.

    `GenericFakeChatModel.bind_tools` raises `NotImplementedError` at these
    pins, and `create_react_agent` calls it, so the subclass returns itself:
    the script already says which tool to call. Built here and never through
    `build_agent()`, so --scripted cannot reach a real model.
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import create_react_agent

    class ScriptedChatModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

    model = ScriptedChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": str(task["tool"]), "args": dict(task["arguments"]),
            "id": "rma-scripted-1",
        }]),
        AIMessage(content="done"),
    ]))
    return create_react_agent(model, tools)


async def run(connection: dict, prompt: str, task: dict | None) -> int:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.errors import GraphRecursionError

    # The `try` sits outside the stack, so whatever the stack raises becomes a
    # claim rather than a missing one.
    try:
        client = MultiServerMCPClient({"ratemyagent": connection})
        tools = await client.get_tools()
        if ALLOWED_TOOLS is not None:
            tools = [tool for tool in tools if tool.name in ALLOWED_TOOLS]
        agent = scripted_agent(task, tools) if task is not None else build_agent(tools)
        state = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": RECURSION_LIMIT},
            ),
            timeout=INNER_DEADLINE_S,
        )
        final = state["messages"][-1]
        emit(True, result=str(getattr(final, "content", final))[:400])
    except asyncio.TimeoutError:
        emit(False, f"no action within {INNER_DEADLINE_S:.0f}s", hang=True)
    except GraphRecursionError as exc:
        emit(False, f"recursion limit {RECURSION_LIMIT} reached: {exc}")
    except Exception as exc:  # noqa: BLE001 -- the claim is the report
        emit(False, f"{type(exc).__name__}: {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the MCP config the scan wrote")
    parser.add_argument("--prompt", required=True, help="the task's prompt")
    parser.add_argument("--scripted", action="store_true",
                        help="no model: call the task's tool, then answer")
    parser.add_argument("--tasks", help="the scan's task file (needed by --scripted)")
    parser.add_argument("--task", help="the task id (needed by --scripted)")
    args = parser.parse_args()

    connection = load_connection(args.config)
    task = load_task(args.tasks, args.task) if args.scripted else None
    return asyncio.run(run(connection, args.prompt, task))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        emit(False, f"runner failed before the task: {type(exc).__name__}: {exc}")
        sys.exit(1)
