#!/bin/sh
# Gate S PHASE 2 -- the replicates. Authorised to $0.50.
#
#   run-gate-s.sh                  all 15: replicates 1..5, arms interleaved
#   run-gate-s.sh <arm> <r>        one replicate (a re-run is reported as one)
#
# arms: claude-code | openai | langgraph   (the names verify_gate_s.py reads;
#        `openai` and `langgraph` are in its MODEL_COUNTED set, EXACTLY)
#
# Everything is DESIGN-GATE-S's registered configuration as amended by phase 0
# and phase 1: seed 254, --fault-rate 0.2, --lost-reply-close-after 5, one scan
# per replicate against a fresh state (--repeats 1), task file byte-identical.
#
# THE CONFIGURATION PRINCIPLE: fix what creates nondeterminism, keep everything
# else at the stack's default. The ONE stack override, named beside every
# number it affects:
#   - openai: MCPServerStdio client_session_timeout_seconds = 120 (default 5,
#     which races the proxy's 5 s close). cache_tools_list is at its SDK
#     default (False); the runner spells it out, it does not change it.
# Harness guards (ours, not the stacks'):
#   - turn cap N=8 (design section 6.3): openai max_turns=8, langgraph
#     recursion_limit=16; Claude Code has no turn flag;
#   - Claude Code --max-budget-usd 0.05 per run (phase 1's most expensive run
#     was $0.0124; 0.20 per run would let two runs eat most of $0.50);
#   - T_inner 300 s (Claude Code) / 120 s (SDK arms), T_outer 420 s,
#     --scan-timeout 1800 s (phase 0 addendum).
#
# SPEND: before every scan the running total (spend.py, from the sidecars) plus
# that scan's WORST case must fit under the ceiling, or the script stops. Worst
# case per scan: Claude Code 2 x $0.05 budget cap; SDK arms $0.05 (measured
# $0.0037 per replicate in phase 1; 8 turns x 2 runs is the bound).
set -u
CEILING="${GATE_S_CEILING:-0.50}"   # re-runs set their own ceiling
cd "$(dirname "$0")" || exit 2
HERE=$(pwd)
OUT="${GATE_S_OUT:-$HERE/phase2}"   # re-runs write to their own root, never over phase2/
PY="$HOME/.gate-s-venv/.venv/bin/python"
TOOL="uvx --from ratemyagent==1.7.2 ratemyagent"
mkdir -p "$OUT"
LOG="$OUT/run-log.txt"

KEYFILE="${GATE_S_KEYFILE:-$HOME/.gate-s-venv/key.env}"
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f "$KEYFILE" ]; then
  . "$KEYFILE"
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ANTHROPIC_API_KEY is not set and $KEYFILE does not exist." >&2
  exit 2
fi
export ANTHROPIC_API_KEY

# The verifier reads the task file from the root it is pointed at. A COPY, and
# checked byte-identical, so the replicates and the check read the same task.
cp "$HERE/tasks-gate-s.json" "$OUT/tasks-gate-s.json"
cmp -s "$HERE/tasks-gate-s.json" "$OUT/tasks-gate-s.json" || { echo "task copy differs" >&2; exit 2; }

worst() {
  case "$1" in claude-code) echo 0.10 ;; *) echo 0.05 ;; esac
}

one() {
  ARM="$1"; R="$2"; TAG="$ARM-$R"
  SPENT=$(python3 "$HERE/spend.py" --root "$OUT" --total-only)
  if ! python3 -c "import sys; sys.exit(0 if $SPENT + $(worst "$ARM") <= $CEILING else 1)"; then
    echo "STOP before $TAG: spent \$$SPENT + worst case \$$(worst "$ARM") would exceed \$$CEILING" | tee -a "$LOG"
    return 3
  fi

  case "$ARM" in
    claude-code)
      AGENT="python3 \"$HERE/run_claude_code.py\""
      CLAIM="--claim-path structured_output"
      INNER=300; MCP_T=""; BUDGET=0.05 ;;
    openai)
      AGENT="\"$PY\" \"$HERE/run_openai_agents.py\""
      CLAIM=""; INNER=120; MCP_T=120; BUDGET="" ;;
    langgraph)
      AGENT="\"$PY\" \"$HERE/run_langgraph.py\""
      CLAIM=""; INNER=120; MCP_T=""; BUDGET="" ;;
    *) echo "unknown arm $ARM" >&2; return 2 ;;
  esac

  rm -f "$OUT"/state-$TAG.jsonl "$OUT"/calls-$TAG.jsonl "$OUT"/state-$TAG.jsonl.gen
  rm -f "$OUT"/sidecar-$TAG-*.json
  rm -rf "$OUT/work-$TAG"
  mkdir -p "$OUT/work-$TAG"
  : > "$OUT/state-$TAG.jsonl"
  wc -l < "$OUT/state-$TAG.jsonl" | tr -d ' ' > "$OUT/pre-state-$TAG.txt"

  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # shellcheck disable=SC2086 -- $CLAIM is deliberately word-split (0 or 2 args)
  env GATE_S_SIDECAR="$OUT/sidecar-$TAG.json" \
      GATE_S_MODEL=claude-haiku-4-5 GATE_S_MAX_TURNS=8 \
      GATE_S_INNER_DEADLINE=$INNER \
      ${MCP_T:+GATE_S_MCP_TIMEOUT=$MCP_T} \
      ${BUDGET:+GATE_S_MAX_BUDGET_USD=$BUDGET} \
  $TOOL scan --target agent \
    --agent "$AGENT" \
    --agent-command '--config {config} --prompt {prompt}' \
    $CLAIM \
    --agent-kind llm \
    --tasks "$HERE/tasks-gate-s.json" \
    --upstream "stdio://python3 \"$HERE/event_twin_mcp_server.py\" --mode append --key-scope operation --role {role} --state \"$OUT/state-$TAG.jsonl\" --calls \"$OUT/calls-$TAG.jsonl\"" \
    --verify-tool effects --verify-count entries \
    --work-dir "$OUT/work-$TAG" \
    --allow-mutating --timeout 420 --scan-timeout 1800 \
    --fault-rate 0.2 --seed 254 --lost-reply-close-after 5 \
    --repeats 1 \
    --json-out "$OUT/out-$TAG.json" \
    > "$OUT/stdout-$TAG.txt" 2> "$OUT/stderr-$TAG.txt"
  CODE=$?
  SPENT=$(python3 "$HERE/spend.py" --root "$OUT" --total-only)
  echo "$START  $TAG  scan exit $CODE  spend so far \$$SPENT of \$$CEILING" | tee -a "$LOG"
  return 0
}

if [ $# -eq 2 ]; then
  one "$1" "$2"; exit $?
fi
for R in 1 2 3 4 5; do
  for ARM in ${GATE_S_ARMS:-claude-code openai langgraph}; do
    one "$ARM" "$R" || exit $?
  done
done
python3 "$HERE/spend.py" --root "$OUT" --ceiling "$CEILING" | tee -a "$LOG"
