# runners/ — to RE-RUN Gate S, not to re-check it

**The checkers do not need anything in this directory.** `../verify_gate_s.py` and
`../fisher_contrasts.py` re-derive every published number from `../evidence/`, stdlib
only.

These are the scripts that drove the three stacks, shipped as they ran, because Gate S's
claim is about third-party code: a reader who cannot see the runner cannot tell whether a
zero is the stack's or ours. Each runner reads the per-task MCP config the scan writes,
starts the server exactly as written (its `env` block included, merged over the
environment), runs one task, and prints one claim. What to look for:

- **The `try` sits outside the stack.** `run_openai_agents.py` wraps `Runner.run(...)`
  and `run_langgraph.py` wraps `agent.ainvoke(...)`; each catches only what has already
  left every handler the stack provides, and reports it as a claim rather than
  re-raising (an unhandled exception prints no claim, and no claim refuses the scan).
- **No stack handler is disabled.** `create_react_agent(llm, tools)` gets the default
  `ToolNode`; the OpenAI agent keeps the default `failure_error_function`.
- **The one override:** `MCPServerStdio(client_session_timeout_seconds=120)` — the SDK's
  default 5 s races the proxy's 5 s close. `cache_tools_list=False` is written out and
  equals the SDK default.
- **Tool restriction is asserted, not assumed:** both SDK runners refuse with a claim
  unless the model-visible tool set is exactly `["event"]`. `run_claude_code.py` restricts
  by flags; since 1.7.3 the twin's agent copy advertises `event` only, so the restriction
  holds at the source for every client (`tests/test_twin_tool_surface.py`).
- **Usage is read from provider callbacks**, so a run that raises still reports what it
  spent.

| file | what it is |
|---|---|
| `run_openai_agents.py` | OpenAI Agents SDK 0.22.3 via `LitellmModel("anthropic/claude-haiku-4-5")` |
| `run_langgraph.py` | LangGraph 1.2.12 `create_react_agent` + `ChatAnthropic("claude-haiku-4-5")` |
| `run_claude_code.py` | a thin wrapper: runs `claude --bare -p …` with an inner deadline, strips the parent session's `CLAUDE_CODE_*` variables, and always prints a `structured_output` |
| `run-gate-s.sh` | the replicate driver: one scan per replicate on a fresh state, a spend check before every scan |

## Re-running costs money and needs things this package does not install

- The three stacks and their dependencies (`openai-agents[litellm]`, `langgraph`,
  `langchain-anthropic`, `langchain-mcp-adapters`, `mcp<2` for the adapters at 0.3.1,
  and the `claude` CLI). None is a dependency of `ratemyagent`.
- An Anthropic API key. **The runners read it only from `ANTHROPIC_API_KEY` in the
  environment.** `run-gate-s.sh` will source a key file if the variable is unset —
  `$GATE_S_KEYFILE`, default `~/.gate-s-venv/key.env`, i.e. outside any checkout — and
  never echoes it. Keep the key out of the tree: nothing here writes it anywhere, and no
  file in `../evidence/` contains it.
- `run-gate-s.sh` expects the layout it ran in (the runners, the twin and
  `tasks-gate-s.json` beside it, `spend.py` for the spend check). This copy is the one
  the Claude Code re-run used: it adds optional `GATE_S_OUT`, `GATE_S_CEILING` and
  `GATE_S_ARMS` overrides whose defaults reproduce the phase 2 invocation exactly.

Measured cost per replicate (baseline + chaos): Claude Code about $0.018, each SDK arm
about $0.0036, at `claude-haiku-4-5` list prices.
