#!/bin/bash
# =============================================================================
# jarvis_cost_janitor.sh — external cleanup for crash/kill-9 scenarios
# =============================================================================
#
# Run: crontab: */5 * * * * /path/to/jarvis_cost_janitor.sh
#
# Checks if supervisor is running. If not, ensures:
# 1. No GCP VMs with created-by=jarvis label (except allowlisted)
# 2. No Cloud SQL proxy process
# 3. No launchd plist for proxy
# 4. Static IPs with created-by=jarvis label released (except allowlisted)
#
# Safety: Uses labels.created-by=jarvis filter ONLY (never broad name patterns).
# =============================================================================

set -euo pipefail

PROJECT="${JARVIS_GCP_PROJECT:-jarvis-473803}"
PLIST="$HOME/Library/LaunchAgents/com.jarvis.cloudsql-proxy.plist"

# --- Approved always-on set (empty by default in solo mode) ---
# Comma-separated VM names that should NOT be deleted even when supervisor is down.
ALWAYS_ON_VMS="${JARVIS_ALWAYS_ON_VMS:-}"
ALWAYS_ON_IPS="${JARVIS_ALWAYS_ON_IPS:-}"

# Is supervisor running?
if pgrep -f "unified_supervisor" > /dev/null 2>&1; then
    exit 0  # Supervisor is alive, nothing to clean
fi

echo "[janitor] Supervisor not running — enforcing zero-cost posture ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

# Kill orphan Cloud SQL proxy
if pgrep -f "cloud-sql-proxy" > /dev/null 2>&1; then
    echo "[janitor] Killing orphan Cloud SQL proxy"
    pkill -f "cloud-sql-proxy" 2>/dev/null || true
fi

# Remove launchd plist
if [ -f "$PLIST" ]; then
    echo "[janitor] Removing proxy launchd plist"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
fi

# Delete orphan GCP VMs — ONLY those with labels.created-by=jarvis
gcloud compute instances list --project="$PROJECT" \
    --filter="labels.created-by=jarvis AND status=RUNNING" \
    --format="value(name,zone)" 2>/dev/null | \
while read -r name zone; do
    [ -z "$name" ] && continue
    # Check allowlist
    if echo ",$ALWAYS_ON_VMS," | grep -q ",$name,"; then
        echo "[janitor] SKIP (allowlisted): $name"
        continue
    fi
    echo "[janitor] DELETE VM: $name ($zone)"
    gcloud compute instances delete "$name" --project="$PROJECT" \
        --zone="$zone" --quiet &
done
wait

# Release static IPs — ONLY those with labels.created-by=jarvis
gcloud compute addresses list --project="$PROJECT" \
    --filter="labels.created-by=jarvis AND status=RESERVED" \
    --format="value(name,region)" 2>/dev/null | \
while read -r name region; do
    [ -z "$name" ] && continue
    if echo ",$ALWAYS_ON_IPS," | grep -q ",$name,"; then
        echo "[janitor] SKIP (allowlisted): $name"
        continue
    fi
    echo "[janitor] RELEASE IP: $name ($region)"
    gcloud compute addresses delete "$name" --project="$PROJECT" \
        --region="$region" --quiet &
done
wait

echo "[janitor] Zero-cost enforcement complete ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
