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

SENTINEL=0
for arg in "$@"; do
  case "$arg" in
    --sentinel) SENTINEL=1 ;;
    --help|-h)
      echo "usage: cockpit_interactive.sh [--sentinel]"
      echo "  --sentinel   the organism discovers, sanctions and applies its"
      echo "               own work; you observe. Red tier still escalates."
      exit 0 ;;
  esac
done
if [ "$SENTINEL" = "1" ]; then
  export JARVIS_SENTINEL_MODE_ENABLED=true
  export JARVIS_GOAL_DISCOVERY_ENABLED=true
  echo "sentinel: ARMED — the organism will select and apply its own work"
  echo "          auto-approve ceiling: ${JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER:-APPROVAL_REQUIRED} (red always escalates)"
else
  echo "sentinel: off (pass --sentinel to let the organism drive)"
fi

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

# --- 3. the execution envelope --------------------------------------------
# NOTHING is transcribed here. This launcher used to carry a hand-copied list
# of presentation flags and NONE of the ~40 execution budgets soak26.sh set,
# so a /goal sanction typed into the cockpit ran the same production pipeline
# on DEFAULT budgets -- which is where the huge-file goals kept dying.
#
# The values live in ONE place, governance/production_envelope.py, and are
# DERIVED from the wall clock (pipeline = wall * f, generation = pipeline * f)
# rather than written down three times and left to drift apart.
#
# ouroboros_battle_test.py hydrates the same envelope in-process at boot, so
# this eval is belt-and-braces: it makes the values visible to anything the
# launcher runs BEFORE python starts, and to an operator reading `env`. Both
# paths use setdefault semantics -- ${VAR:-value} here, `if name not in
# environ` there -- so an operator override always wins, identically.
if ! ENVELOPE="$("$PY" -m backend.core.ouroboros.governance.production_envelope \
      --profile cockpit --wall-seconds "$WALL_S" --shell 2>/dev/null)"; then
  die "could not build the execution envelope. The cockpit will NOT be started
  on default budgets -- that is the failure this indirection exists to prevent.
  Check: $PY -m backend.core.ouroboros.governance.production_envelope --profile cockpit"
fi
eval "$ENVELOPE"
echo "envelope: $(echo "$ENVELOPE" | grep -c '^export') vars from production_envelope (profile=cockpit)"

# --- 4. outward-facing acts stay inside this machine -----------------------
# The envelope already air-gaps pushes; re-stated here so an operator reading
# the launcher sees it without opening a Python module. The review lane may
# still BUILD local branches -- the air-gap is what stops them reaching origin
# (2026-09-07: 22 ouroboros/review/* branches escaped from isolated soaks).
export JARVIS_REMOTE_PUSH_AIRGAP=true
export JARVIS_ORANGE_PR_ENABLED=false

# --- 5. Autonomous Sentinel Mode (OPT-IN, never implicit) ------------------
# Unattended application of code the organism authored ITSELF is the most
# consequential capability in this system, so it is not reachable by starting
# the cockpit -- it takes a deliberate flag, every time, and the flag arms BOTH
# switches together because either alone is a different (weaker) thing:
#   discovery without sentinel -> it files goals a human still approves
#   sentinel without discovery -> it approves goals a human still writes
#
#   bash scripts/soaks/cockpit_interactive.sh --sentinel
#
# Red-tier work, the governance substrate, Order-2 self-modification and any
# recursion-bound breach still stop for a human -- no flag lifts that floor.

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
