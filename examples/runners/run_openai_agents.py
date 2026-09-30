#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "openai-agents==0.22.3",
#     "mcp==1.30.0",
# ]
# ///
"""RateMyAgent runner: an OpenAI Agents SDK agent, one task per process.

The OpenAI Agents SDK has no command line, so a scan cannot launch it
directly. This file is the part a scan needs: it reads the MCP config the scan
wrote, starts the server exactly as that config says (which starts
`ratemyagent proxy`), runs one task, and prints one claim.

**Edit `build_agent()` and nothing else.** Return your `Agent(...)` as you
would in your own code -- model, instructions, tools of your own -- and leave
`mcp_servers` out: the runner attaches the scan's MCP server itself.

    ratemyagent scan --target agent --agent-kind llm \\
        --agent "python run_openai_agents.py" \\
        --agent-command '--config {config} --prompt {prompt} --tasks {tasks} --task {task_id}' \\
        ...

`--scripted` runs with no model at all: a scripted model calls the task's
`tool` with its `arguments` and then answers. It costs nothing and needs no
API key, which makes it the dry run of the plumbing and what CI runs. It never
builds a real model.

The claim is the last JSON line on stdout: {"ok": true|false, ...}.

Imports nothing from `ratemyagent`: the MCP config and the claim line are the
whole interface, and a runner that imported the scanner would be testing the
scanner's idea of itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

#: The runner's own deadline for one task, in seconds. On expiry it PRINTS A
#: CLAIM rather than being killed: a stack that waits forever on a dropped
#: reply is then a finding ("no action within 120s"), not a task the scan had
#: to abandon. Keep it below the scan's --timeout.
INNER_DEADLINE_S = 120.0

#: The SDK's own MCP read deadline. `MCPServerStdio` defaults it to 5 s, the
#: same value as the proxy's `--lost-reply-close-after` default: two clocks at
#: one value, and whichever fires first decides whether the model sees a
#: timeout or a closed session -- two faults the scan counts separately. That
#: is a race, not a difference between agents, so it is set well clear of the
#: close. Remove it to measure the SDK's default as your users would meet it.
MCP_READ_TIMEOUT_S = 120.0

#: A hard cap on model turns, so a looping agent ends as a claim.
MAX_TURNS = 8

#: Tool names the agent may see, or None for every tool the server offers. A
#: second tool the agent can call gets its own fault schedule, so narrowing
#: this makes runs of one task comparable.
ALLOWED_TOOLS: list[str] | None = None


def build_agent():
    """EDIT THIS: return your agent, without `mcp_servers`.

    The runner attaches the scan's MCP server with `Agent.clone`. Everything
    else -- model, instructions, other tools, guardrails -- stays yours. With
    no `model`, the SDK uses its default and reads OPENAI_API_KEY.
    """
    from agents import Agent

    return Agent(
        name="my-agent",
        instructions=(
            "Use the available tools to do what the user asks. When it is done, "
            "say what you did."
        ),
        # model="...",  # your model here
    )


def load_server_params(config_path: str) -> dict:
    """The MCP config the scan wrote, read as data.

    The `env` block is the load-bearing part: `RMA_PROXY_RECORD` is how the
    record -- the only account of what this agent actually did -- comes to
    exist. Merged over `os.environ` rather than replacing it, because the
    proxy is a Python process that needs PATH and friends.
    """
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    server = config["mcpServers"]["ratemyagent"]
    return {
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


def scripted_agent(task: dict):
    """An agent whose model is scripted: call the task's tool once, then answer.

    Built here and never through `build_agent()`, so --scripted cannot reach a
    real model whatever that function does.
    """
    from agents import Agent
    from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

    model = ScriptedModel([
        ModelStep(output=[function_call(
            str(task["tool"]), dict(task["arguments"]), call_id="rma-scripted-1",
        )]),
        ModelStep(output=[assistant_message("done")]),
    ])
    return Agent(name="scripted", instructions="", model=model)


async def run(params: dict, prompt: str, task: dict | None) -> int:
    from agents import Runner
    from agents.exceptions import MaxTurnsExceeded
    from agents.mcp import MCPServerStdio, create_static_tool_filter

    kwargs = dict(params=params, cache_tools_list=False)
    if ALLOWED_TOOLS is not None:
        kwargs["tool_filter"] = create_static_tool_filter(allowed_tool_names=ALLOWED_TOOLS)
    if MCP_READ_TIMEOUT_S is not None:
        kwargs["client_session_timeout_seconds"] = MCP_READ_TIMEOUT_S

    # The `try` sits outside the stack, so whatever the stack raises becomes a
    # claim. An unhandled exception prints no claim, and a task with no claim
    # is refused by the scan rather than recorded as the failure it was.
    try:
        async with MCPServerStdio(**kwargs) as server:
            if task is not None:
                agent = scripted_agent(task).clone(mcp_servers=[server])
                from agents.testing import ScriptedModel

                if not isinstance(agent.model, ScriptedModel):
                    emit(False, "--scripted refused: the agent's model is not scripted")
                    return 1
            else:
                agent = build_agent().clone(mcp_servers=[server])
            result = await asyncio.wait_for(
                Runner.run(agent, prompt, max_turns=MAX_TURNS),
                timeout=INNER_DEADLINE_S,
            )
            emit(True, result=str(result.final_output)[:400])
    except asyncio.TimeoutError:
        emit(False, f"no action within {INNER_DEADLINE_S:.0f}s", hang=True)
    except MaxTurnsExceeded as exc:
        emit(False, f"turn cap {MAX_TURNS} reached: {exc}")
    except Exception as exc:  # noqa: BLE001 -- the claim is the report
        # Reported, never swallowed and never re-raised: this is the agent's
        # own account of how the task ended.
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

    params = load_server_params(args.config)
    task = load_task(args.tasks, args.task) if args.scripted else None
    return asyncio.run(run(params, args.prompt, task))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        emit(False, f"runner failed before the task: {type(exc).__name__}: {exc}")
        sys.exit(1)
