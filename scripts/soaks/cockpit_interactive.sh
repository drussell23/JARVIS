#!/usr/bin/env bash
# The O+V cockpit, interactive, on a real terminal.
#
# WHY A SEPARATE LAUNCHER. The cockpit's rich surfaces -- the GENERATE token
# stream (stream_renderer, 16 ms batched Rich Live), the NOTIFY_APPLY diff
# overlay (diff_preview), the bottom status line and the collapsible op
# blocks -- all gate on a REAL interactive TTY. Under a background or piped
# run the harness auto-detects non-TTY, sets headless, and every one of
# those falls through to plain spinner-and-sleep output. A cockpit session
# launched the way the soaks are launched therefore shows none of the
# things it exists to show, and looks broken rather than headless.
#
# So this script must be run BY HAND from a terminal:
#
#     wsl -d Ubuntu -u jarvis_svc
#     cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis
#     bash scripts/soaks/cockpit_interactive.sh
#
# It refuses if stdin/stdout are not a TTY rather than starting a session
# that cannot render -- the refusal is the whole point of the check.
#
# WHAT YOU WILL SEE, and where it comes from:
#   * a minimal boot panel, then an idle breadcrumb (presentation_restraint)
#   * `/preflight` and `/organism` for the detail the panel omits
#   * live GENERATE token streaming as candidates are written
#   * `Update(path)` blocks with numbered diffs, 3-hunk cap
#   * a Yellow-tier diff overlay before anything auto-applies
#   * 89 auto-discovered slash verbs (Tab completes; `/help verbs` lists)
#   * `/expand t-N | d-N | o-N | n-N | s-N` to open any bounded ref
#   * `/btw <question>` to ask WITHOUT taking the floor from running work
#
# Everything this session exercises is what the last few days changed: the
# swarm client resolver, the symbol-scoped L2 repair, the fortified VALIDATE.
set -uo pipefail

JARVIS_DIR="${OV_MAIN_TREE:-/mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis}"
PY="${OV_PYTHON:-$HOME/.venvs/ov/bin/python}"
MODEL="${JARVIS_LOCAL_MODEL_NAME:-}"          # empty => the .env pin
OLLAMA_API="${JARVIS_LOCAL_MODEL_BASE_URL:-http://127.0.0.1:11434}"
FREE_MIB="${OV_GPU_FREE_MIB:-20480}"          # the cockpit needs ~18.6 GB free
COST_CAP="${OV_COST_CAP:-0.50}"
IDLE_S="${OV_IDLE_TIMEOUT_S:-900}"
WALL_S="${OV_MAX_WALL_S:-3600}"

die() { echo "REFUSING: $*" >&2; exit 2; }

# --- 1. a real terminal, or nothing ---------------------------------------
[ -t 0 ] && [ -t 1 ] || die "not a TTY. The cockpit's stream, diff overlay and
  status line all gate on an interactive terminal and would silently render
  as plain text. Open a terminal and run this by hand:
    wsl -d Ubuntu -u jarvis_svc
    cd $JARVIS_DIR && bash scripts/soaks/cockpit_interactive.sh"

cd "$JARVIS_DIR" || die "no tree at $JARVIS_DIR"
[ -x "$PY" ] || die "no interpreter at $PY (set OV_PYTHON)"

# --- 2. the local lane must be able to answer ------------------------------
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | head -1)
TOTAL=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | head -1)
if [ -n "$USED" ] && [ -n "$TOTAL" ]; then
  FREE=$((TOTAL - USED))
  echo "gpu: ${USED}/${TOTAL} MiB used, ${FREE} MiB free"
  [ "$FREE" -ge "$FREE_MIB" ] || die "only ${FREE} MiB free; the model needs ~${FREE_MIB}.
  A training run or another soak still holds the card -- wait for it, or
  lower OV_GPU_FREE_MIB if you know what else is resident."
fi

TAGS=$(curl -s -m 5 "$OLLAMA_API/api/tags" 2>/dev/null) || true
[ -n "$TAGS" ] || die "ollama is not answering at $OLLAMA_API"
if [ -n "$MODEL" ]; then
  echo "$TAGS" | grep -q "\"name\":\"$MODEL\"" \
    || die "JARVIS_LOCAL_MODEL_NAME=$MODEL is not served by ollama"
  echo "model: $MODEL (exported; overrides the .env pin)"
else
  echo "model: from .env pin ($(grep -m1 '^JARVIS_LOCAL_MODEL_NAME=' .env 2>/dev/null | cut -d= -f2-))"
fi

# --- 3. the flags the cockpit's surfaces need ------------------------------
# All default-TRUE in code; exported so a session is reproducible from the
# script rather than from whatever the shell happened to carry.
export JARVIS_PRESENTATION_RESTRAINT_ENABLED=true
export JARVIS_REPL_COMPLETION_ENABLED=true
export JARVIS_REPL_INPUT_POLISH_ENABLED=true
export JARVIS_LIVE_STATUS_LINE_ENABLED=true
export JARVIS_OP_COLLAPSE_ENABLED=true
export JARVIS_TOOL_RENDER_REGISTRY_ENABLED=true
export JARVIS_NARRATIVE_INTENT_ENABLED=true
export JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED=true
export JARVIS_BTW_ENABLED=true
export JARVIS_REVIEW_BRANCH_ENABLED=true

# What the recent work added, so this session actually exercises it.
export JARVIS_SWARM_ROUTING_ENABLED=true
export JARVIS_L2_SYMBOL_SCOPED_ENABLED=true
export JARVIS_TEST_TIMEOUT_S="${JARVIS_TEST_TIMEOUT_S:-600}"

# Yellow-tier work pauses for a human here -- that pause IS the surface
# under test, so the review timeout is generous and auto-apply is off.
export JARVIS_NOTIFY_APPLY_DELAY_S="${JARVIS_NOTIFY_APPLY_DELAY_S:-8}"
export JARVIS_REVIEW_TIMEOUT_S="${JARVIS_REVIEW_TIMEOUT_S:-900}"

echo
echo "cockpit starting — interactive, cost cap \$${COST_CAP}, wall ${WALL_S}s"
echo "  /help verbs     every slash command"
echo "  /organism       what booted"
echo "  /posture        the inferred strategic posture"
echo "  /btw <q>        ask without taking the floor"
echo "  Ctrl-C          stop (a partial summary is still written)"
echo

# --no-headless: force the REPL even if the TTY probe is fooled by a
# multiplexer. This is the one launcher that always wants it.
exec "$PY" scripts/ouroboros_battle_test.py \
  --no-headless \
  --cost-cap "$COST_CAP" \
  --idle-timeout "$IDLE_S" \
  --max-wall-seconds "$WALL_S" \
  -v
