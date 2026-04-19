#!/usr/bin/env bash
# ferrari.sh — hardened Ferrari (frame_server) launcher.
#
# Wraps backend/vision/frame_server.py against every failure mode we
# observed during the 2026-04-18→19 VisionSensor graduation arc:
#
#   * macOS App Nap    — caffeinate -is prevents idle/system sleep
#   * Display sleep    — caffeinate -d keeps the backlight on
#   * Parent TTY death — nohup shields against SIGHUP, shell exit does
#                        not propagate to the capture loop
#   * Crash recovery   — supervisor loop restarts frame_server if it
#                        exits non-zero (TCC hiccup, resize exception,
#                        Quartz transient), with exponential backoff
#                        capped at 30s
#   * Stale processes  — start/stop/status commands with PID file,
#                        no more orphan pgrep hunts
#
# Manifesto §3 (Disciplined Concurrency): this is the OS-level discipline
# that lets the async event loop upstream assume Ferrari is always-on.
# Manifesto §8 (Absolute Observability): every launch/restart/exit is
# logged with timestamp + exit code to .jarvis/vision_ferrari.log.
#
# Usage:
#   scripts/ferrari.sh start    # launch Ferrari, daemonized
#   scripts/ferrari.sh stop     # kill Ferrari + supervisor + caffeinate
#   scripts/ferrari.sh status   # report alive/dead + frame age + uptime
#   scripts/ferrari.sh tail     # tail -f the Ferrari log
#   scripts/ferrari.sh restart  # stop then start
#
# Env tunables:
#   JARVIS_FERRARI_FPS           capture rate (default: 15)
#   JARVIS_FERRARI_RESTART_CAP_S max backoff between restarts (default: 30)
#   JARVIS_FERRARI_LOG           override log path (default: .jarvis/vision_ferrari.log)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO_ROOT/.jarvis"
mkdir -p "$LOG_DIR"

LOG_FILE="${JARVIS_FERRARI_LOG:-$LOG_DIR/vision_ferrari.log}"
PID_FILE="$LOG_DIR/vision_ferrari.pid"
SUP_PID_FILE="$LOG_DIR/vision_ferrari_supervisor.pid"
FPS="${JARVIS_FERRARI_FPS:-15}"
RESTART_CAP_S="${JARVIS_FERRARI_RESTART_CAP_S:-30}"
FRAME_JPG="/tmp/claude/latest_frame.jpg"

log() { echo "[ferrari.sh $(date '+%Y-%m-%dT%H:%M:%S')] $*" >> "$LOG_FILE"; }

is_alive() {
  local pid_file="$1"
  [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null
}

cmd_start() {
  if is_alive "$SUP_PID_FILE"; then
    echo "Ferrari supervisor already running: PID $(cat $SUP_PID_FILE)" >&2
    exit 0
  fi

  log "start fps=$FPS log=$LOG_FILE"

  # Supervisor loop: auto-restart frame_server if it exits.
  # Exponential backoff capped at RESTART_CAP_S.
  #
  # caffeinate -dis flags:
  #   -d  disable display sleep (keeps backlight on for capture)
  #   -i  disable idle sleep (keeps system awake)
  #   -s  disable system sleep on AC power
  #
  # nohup + trailing &: detach from calling shell, survive SIGHUP
  nohup caffeinate -dis bash -c '
    LOG_FILE="'"$LOG_FILE"'"
    PID_FILE="'"$PID_FILE"'"
    FPS="'"$FPS"'"
    RESTART_CAP_S="'"$RESTART_CAP_S"'"
    REPO_ROOT="'"$REPO_ROOT"'"
    BACKOFF=1

    echo "[supervisor $(date "+%Y-%m-%dT%H:%M:%S")] started fps=$FPS pid=$$" >> "$LOG_FILE"

    trap "rm -f \"$PID_FILE\"; echo \"[supervisor $(date +%H:%M:%S)] SIGTERM received, exiting\" >> \"$LOG_FILE\"; exit 0" TERM INT

    while true; do
      python3 "$REPO_ROOT/backend/vision/frame_server.py" --fps "$FPS" \
        >> "$LOG_FILE" 2>&1 &
      CHILD=$!
      echo "$CHILD" > "$PID_FILE"
      echo "[supervisor $(date "+%Y-%m-%dT%H:%M:%S")] frame_server started pid=$CHILD backoff=${BACKOFF}s" >> "$LOG_FILE"

      wait "$CHILD"
      EXIT=$?
      rm -f "$PID_FILE"
      echo "[supervisor $(date "+%Y-%m-%dT%H:%M:%S")] frame_server exit=$EXIT; sleeping ${BACKOFF}s before restart" >> "$LOG_FILE"

      sleep "$BACKOFF"
      # Exponential backoff, capped
      BACKOFF=$((BACKOFF * 2))
      [ "$BACKOFF" -gt "$RESTART_CAP_S" ] && BACKOFF="$RESTART_CAP_S"
    done
  ' >> "$LOG_FILE" 2>&1 &
  SUP_PID=$!
  echo "$SUP_PID" > "$SUP_PID_FILE"
  # Detach from job table so calling shell exit doesn't send SIGHUP
  disown "$SUP_PID" 2>/dev/null || true

  # Wait up to 5s for frame_server to publish its first frame
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    if [ -f "$FRAME_JPG" ]; then
      AGE=$(python3 -c "import os,time;print(f'{time.time()-os.stat(\"$FRAME_JPG\").st_mtime:.2f}')" 2>/dev/null || echo NA)
      if [ "$AGE" != "NA" ]; then
        INT_AGE=$(python3 -c "print(int(float('$AGE')))")
        if [ "$INT_AGE" -lt 3 ]; then
          echo "Ferrari started: supervisor_pid=$SUP_PID frame_age=${AGE}s log=$LOG_FILE"
          log "ready frame_age=${AGE}s supervisor_pid=$SUP_PID"
          exit 0
        fi
      fi
    fi
  done
  echo "Ferrari supervisor started (pid=$SUP_PID) but frame not publishing yet — check $LOG_FILE"
}

cmd_stop() {
  log "stop requested"
  if is_alive "$SUP_PID_FILE"; then
    SUP_PID=$(cat "$SUP_PID_FILE")
    kill -TERM "$SUP_PID" 2>/dev/null || true
    # Give supervisor 2s to clean up, then hard-kill
    sleep 2
    kill -9 "$SUP_PID" 2>/dev/null || true
  fi
  if is_alive "$PID_FILE"; then
    kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
  fi
  # Sweep any stray frame_server / caffeinate wrappers for THIS repo
  pgrep -f "$REPO_ROOT/backend/vision/frame_server.py" 2>/dev/null \
    | while read -r pid; do kill -9 "$pid" 2>/dev/null || true; done
  pgrep -f "caffeinate -dis.*frame_server" 2>/dev/null \
    | while read -r pid; do kill -9 "$pid" 2>/dev/null || true; done
  rm -f "$PID_FILE" "$SUP_PID_FILE"
  log "stopped"
  echo "Ferrari stopped"
}

cmd_status() {
  SUP_OK=no
  FS_OK=no
  SUP_PID="-"
  FS_PID="-"
  FRAME_AGE="NA"
  if is_alive "$SUP_PID_FILE"; then
    SUP_OK=yes
    SUP_PID=$(cat "$SUP_PID_FILE")
  fi
  if is_alive "$PID_FILE"; then
    FS_OK=yes
    FS_PID=$(cat "$PID_FILE")
  fi
  if [ -f "$FRAME_JPG" ]; then
    FRAME_AGE=$(python3 -c "import os,time;print(f'{time.time()-os.stat(\"$FRAME_JPG\").st_mtime:.2f}')" 2>/dev/null || echo NA)
  fi
  echo "supervisor:   $SUP_OK  pid=$SUP_PID"
  echo "frame_server: $FS_OK  pid=$FS_PID"
  echo "frame_age:    ${FRAME_AGE}s"
  echo "log:          $LOG_FILE"
  if [ "$SUP_OK" = "yes" ] && [ "$FS_OK" = "yes" ]; then
    if [ "$FRAME_AGE" != "NA" ]; then
      INT_AGE=$(python3 -c "print(int(float('$FRAME_AGE')))" 2>/dev/null || echo 999)
      if [ "$INT_AGE" -lt 3 ]; then
        echo "health:       LIVE"
        exit 0
      else
        echo "health:       STALE (frame age ${FRAME_AGE}s — Ferrari may be in a restart loop; check log)"
        exit 1
      fi
    fi
  fi
  echo "health:       DEAD"
  exit 1
}

cmd_tail() {
  exec tail -F "$LOG_FILE"
}

cmd_restart() {
  cmd_stop || true
  sleep 1
  cmd_start
}

case "${1:-start}" in
  start|--start)   cmd_start ;;
  stop|--stop)     cmd_stop ;;
  status|--status) cmd_status ;;
  tail|--tail)     cmd_tail ;;
  restart|--restart) cmd_restart ;;
  *)
    echo "Usage: $0 {start|stop|status|tail|restart}"
    exit 2
    ;;
esac
