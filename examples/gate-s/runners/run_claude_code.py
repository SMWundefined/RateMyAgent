#!/usr/bin/env python3
"""Gate S runner: Claude Code.

A thin wrapper, not an adapter. Claude Code satisfies the launch contract on
its own (Gate BD, Gate D2); the wrapper exists for three reasons the other two
stacks get for free:

  1. **The tee.** `_run_task` parses the claim and keeps nothing else, so
     without this the usage block Claude Code prints -- the only per-run token
     accounting there is -- is discarded by the scan (section 9.7).
  2. **An inner deadline.** Claude Code has no `--max-turns` and no
     inner-deadline flag in 2.1.281. Left bare it would be the one stack whose
     hang is only ever `abandoned`, which is a refusal and not a finding.
  3. **A clean environment.** A `claude` launched from inside another Claude
     Code session inherits CLAUDE_CODE_* and attaches to the parent's session.

It prints ONE JSON document, always carrying `structured_output`, so
`--claim-path structured_output` resolves on the deadline path too.

Imports nothing from `ratemyagent`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

MODEL = os.environ.get("GATE_S_MODEL", "claude-haiku-4-5")
#: Longer than Claude Code's own API-error ladder, measured at 185 s over 11
#: attempts in GATE-S-PHASE0 item 3. An inner deadline below that would record
#: an authentication or 5xx failure as a hang.
INNER_DEADLINE_S = float(os.environ.get("GATE_S_INNER_DEADLINE", "300"))
MAX_BUDGET_USD = os.environ.get("GATE_S_MAX_BUDGET_USD", "0.50")
CLAIM_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "error": {"type": "string"}},
    "required": ["ok"],
})


def child_env() -> dict:
    """os.environ with this session's own Claude Code variables removed.

    Measured in phase 0: a nested `claude` inherits CLAUDE_CODE_MESSAGING_SOCKET
    and friends. The gate must launch a fresh session, not a child of whatever
    launched the scan.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE_") and k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")}
    return env


#: Which pass this process serves. Read out of the CONFIG's env block, not out
#: of our own environment: RMA_PROXY_RECORD is addressed to the proxy, and the
#: wrapper only passes it on. Reading os.environ gave every run the same
#: sidecar name and the chaos pass overwrote the baseline (phase 0, item 4).
PASS_NAME = [""]


def note_pass(config_path: str) -> None:
    try:
        env = json.load(open(config_path))["mcpServers"]["ratemyagent"]["env"]
    except Exception:
        return
    parts = os.path.basename(env.get("RMA_PROXY_RECORD", "")).split("-")
    PASS_NAME[0] = parts[1] if len(parts) > 2 else ""


def emit(document: dict) -> None:
    print(json.dumps(document), flush=True)
    base = os.environ.get("GATE_S_SIDECAR")
    if base:
        root, ext = os.path.splitext(base)
        try:
            with open(f"{root}-{PASS_NAME[0] or 'unknown'}{ext}", "w") as handle:
                json.dump(document, handle, indent=2)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--prompt", required=True)
    args = ap.parse_args()
    note_pass(args.config)

    argv = [
        os.environ.get("GATE_S_CLAUDE", "claude"),
        # --bare: Anthropic auth is strictly ANTHROPIC_API_KEY (the CLI's own
        # help), and no hooks, no auto-memory, no CLAUDE.md auto-discovery.
        "--bare",
        "-p", args.prompt,
        "--model", MODEL,
        "--mcp-config", args.config,
        "--strict-mcp-config",
        # Restriction by OMISSION, as the other two stacks do it: the built-in
        # set is emptied rather than denied at use time (section 3).
        "--tools", "",
        "--allowedTools", "mcp__ratemyagent__event",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--output-format", "json",
        "--json-schema", CLAIM_SCHEMA,
        "--max-budget-usd", MAX_BUDGET_USD,
    ]

    started = time.time()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=child_env(),
            stdin=subprocess.DEVNULL, timeout=INNER_DEADLINE_S,
        )
    except subprocess.TimeoutExpired:
        # The hang, as a positive observation. A LOWER BOUND, never "forever".
        emit({"structured_output": {
                  "ok": False,
                  "error": f"no result within {INNER_DEADLINE_S:.0f}s"},
              "gate_s": {"hang": True, "deadline_s": INNER_DEADLINE_S,
                         "elapsed_s": round(time.time() - started, 3)}})
        return 0
    except FileNotFoundError as exc:
        emit({"structured_output": {"ok": False, "error": str(exc)}})
        return 0

    text = proc.stdout.strip()
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        emit({"structured_output": {
                  "ok": False,
                  "error": f"claude printed no JSON document (exit "
                           f"{proc.returncode}): {text[:200]!r}"},
              "gate_s": {"stderr": proc.stderr[-400:]}})
        return 0

    if "structured_output" not in document:
        # An API error prints a result document with no structured_output at
        # all (measured, phase 0 item 3). Without this the scan would REFUSE on
        # --claim-path rather than record the run, so the shape is supplied and
        # what happened is carried beside it.
        document["structured_output"] = {
            "ok": False,
            "error": str(document.get("result") or "no structured_output"),
        }
    document["gate_s"] = {
        "elapsed_s": round(time.time() - started, 3),
        "exit_code": proc.returncode,
        "argv_model": MODEL,
        "max_budget_usd": MAX_BUDGET_USD,
    }
    emit(document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
