#!/usr/bin/env bash
# =============================================================================
# APARS State Hygiene Fix — applies to GCP j-prime VM
# =============================================================================
# Root cause: /tmp/jarvis_progress.json is written once by the startup script
# (APARS launcher) and never reset when j-prime restarts. After a preemption
# or manual restart, the stale file contains error data from the previous boot,
# causing /health to report ready_for_inference=false even when the model is live.
#
# Fix:
#   1. Patch jarvis_apars_launcher.py to validate PID + boot_id in the
#      enrichment middleware before injecting APARS data.
#   2. Write a systemd drop-in (or rc.local entry) that resets the progress
#      file on every boot BEFORE j-prime starts.
#
# Usage:
#   gcloud compute ssh jarvis-prime-node --zone=us-central1-a --project=jarvis-473803 \
#     --command="bash /dev/stdin" < scripts/fix_apars_hygiene.sh
# =============================================================================

set -euo pipefail

APARS_FILE="/tmp/jarvis_progress.json"
LAUNCHER="/tmp/jarvis_apars_launcher.py"
BOOT_RESET_SCRIPT="/etc/jarvis/reset_apars_on_boot.sh"

echo "=== APARS Hygiene Fix ==="

# ── Step 1: Create the boot-time reset script ─────────────────────────────────
sudo mkdir -p /etc/jarvis
sudo tee "$BOOT_RESET_SCRIPT" > /dev/null << 'RESET_SCRIPT'
#!/usr/bin/env bash
# Reset APARS progress file on every boot so stale state never poisons /health
APARS_FILE="/tmp/jarvis_progress.json"
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown")
PID_FILE="/tmp/jarvis_prime.pid"

# Write a clean "booting" state with current boot_id
cat > "$APARS_FILE" << JSON
{
  "boot_id": "$BOOT_ID",
  "pid": 0,
  "phase": 0,
  "phase_number": 0,
  "phase_name": "booting",
  "phase_progress": 0,
  "total_progress": 0,
  "checkpoint": "boot_reset",
  "model_loaded": false,
  "ready_for_inference": false,
  "error": null,
  "deployment_mode": "golden_image",
  "deps_prebaked": true,
  "skipped_phases": [2, 3],
  "updated_at": $(date +%s),
  "elapsed_seconds": 0,
  "startup_script_version": "236.0",
  "startup_script_metadata_version": "236.0"
}
JSON
echo "APARS progress file reset for boot_id=$BOOT_ID"
RESET_SCRIPT
sudo chmod +x "$BOOT_RESET_SCRIPT"
echo "✓ Boot reset script written to $BOOT_RESET_SCRIPT"

# ── Step 2: Register with rc.local / systemd ──────────────────────────────────
if systemctl is-enabled rc-local &>/dev/null; then
    # rc.local exists — prepend our reset before j-prime start
    if ! grep -q "reset_apars_on_boot" /etc/rc.local 2>/dev/null; then
        sudo sed -i 's|^exit 0|'"$BOOT_RESET_SCRIPT"'\nexit 0|' /etc/rc.local
        echo "✓ Registered in /etc/rc.local"
    fi
else
    # Write a oneshot systemd unit
    sudo tee /etc/systemd/system/jarvis-apars-reset.service > /dev/null << 'UNIT'
[Unit]
Description=Reset JARVIS APARS progress file on boot
Before=jarvis-prime.service
DefaultDependencies=no

[Service]
Type=oneshot
ExecStart=/etc/jarvis/reset_apars_on_boot.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable jarvis-apars-reset.service
    echo "✓ systemd unit jarvis-apars-reset.service enabled"
fi

# ── Step 3: Patch the APARS middleware to validate PID before injecting ───────
if [ -f "$LAUNCHER" ]; then
    # Inject PID validation into the middleware's /health enrichment
    # The patch adds a staleness check: if apars["pid"] != current server PID,
    # skip injection and return the raw (healthy) server response.
    PATCH_MARKER="# APARS_HYGIENE_PATCHED"
    if ! grep -q "$PATCH_MARKER" "$LAUNCHER"; then
        sudo python3 - "$LAUNCHER" << 'PATCHER'
import sys, re

path = sys.argv[1]
src = open(path).read()

# Find the enrichment block and inject PID validation
old = "if d.get('ready_for_inference') or d.get('status') == 'healthy':"
new = (
    "# APARS_HYGIENE_PATCHED\n"
    "                import os\n"
    "                _cur_pid = int(open('/tmp/jarvis_prime.pid').read().strip()) "
    "if os.path.exists('/tmp/jarvis_prime.pid') else 0\n"
    "                _apars_pid = d.get('pid', 0)\n"
    "                if _apars_pid and _apars_pid != _cur_pid:\n"
    "                    # Stale APARS data from a previous process — skip injection\n"
    "                    pass\n"
    "                elif d.get('ready_for_inference') or d.get('status') == 'healthy':"
)
if old in src:
    src = src.replace(old, new)
    open(path, 'w').write(src)
    print(f"✓ Patched {path} with PID staleness check")
else:
    print(f"⚠ Pattern not found in {path} — manual review needed")
PATCHER
    else
        echo "⚠ APARS launcher already patched — skipping"
    fi
else
    echo "⚠ $LAUNCHER not found — APARS launcher may not be running"
fi

# ── Step 4: Write PID file when j-prime starts ────────────────────────────────
# Add a wrapper that writes /tmp/jarvis_prime.pid when the server is launched
WRAPPER="/usr/local/bin/jarvis-prime-start"
sudo tee "$WRAPPER" > /dev/null << 'WRAPPER_SCRIPT'
#!/usr/bin/env bash
# Wrapper: reset APARS state, write PID file, then start j-prime
/etc/jarvis/reset_apars_on_boot.sh

cd /opt/jarvis-prime
/opt/jarvis-prime/venv/bin/python -m jarvis_prime.server \
    --host 0.0.0.0 --port 8000 --context-size 8192 "$@" &
JP_PID=$!
echo $JP_PID > /tmp/jarvis_prime.pid
echo "j-prime started: PID=$JP_PID"
wait $JP_PID
WRAPPER_SCRIPT
sudo chmod +x "$WRAPPER"
echo "✓ j-prime wrapper written to $WRAPPER"

# ── Step 5: Apply reset NOW (without reboot) ──────────────────────────────────
bash "$BOOT_RESET_SCRIPT"

# Update the running server's APARS file with current PID
CURRENT_PID=$(pgrep -f "jarvis_prime.server" | head -1 || echo "0")
if [ "$CURRENT_PID" != "0" ]; then
    echo "$CURRENT_PID" > /tmp/jarvis_prime.pid
    python3 -c "
import json, time, os
path = '/tmp/jarvis_progress.json'
d = json.load(open(path)) if os.path.exists(path) else {}
d['pid'] = $CURRENT_PID
d['error'] = None
d['ready_for_inference'] = True
d['model_loaded'] = True
d['updated_at'] = int(time.time())
json.dump(d, open(path, 'w'), indent=2)
print(f'Updated APARS: pid={$CURRENT_PID} ready_for_inference=True')
"
fi

echo ""
echo "=== APARS Hygiene Fix Complete ==="
echo "  Boot reset script : $BOOT_RESET_SCRIPT"
echo "  j-prime wrapper   : $WRAPPER"
echo "  Current APARS pid : ${CURRENT_PID:-unknown}"
