#!/bin/bash
# =============================================================================
# jarvis_verify_zero_cost.sh — deterministic pass/fail for zero-cost posture
# =============================================================================
#
# Exit 0 = all clear. Exit 1 = cost leak detected.
# Can be used in CI or post-shutdown validation.
#
# Safety: All filters use labels.created-by=jarvis (never broad name patterns).
# =============================================================================

set -uo pipefail

PROJECT="${JARVIS_GCP_PROJECT:-jarvis-473803}"
REGION="${JARVIS_GCP_REGION:-us-central1}"
FAILURES=0

# --- Allowlists (same as janitor) ---
ALWAYS_ON_VMS="${JARVIS_ALWAYS_ON_VMS:-}"
ALWAYS_ON_IPS="${JARVIS_ALWAYS_ON_IPS:-}"

# --- Cloud Run services to check (env-driven, not hardcoded) ---
CLOUD_RUN_SERVICES="${JARVIS_CLOUD_RUN_SERVICES:-jarvis-prime-ecapa}"

# Check 1: No running VMs with jarvis label (except allowlisted)
VM_LIST=$(gcloud compute instances list --project="$PROJECT" \
    --filter="labels.created-by=jarvis AND status=RUNNING" \
    --format="value(name)" 2>/dev/null || true)
for vm in $VM_LIST; do
    [ -z "$vm" ] && continue
    if ! echo ",$ALWAYS_ON_VMS," | grep -q ",$vm,"; then
        echo "FAIL: Running VM '$vm' not in allowlist"
        FAILURES=$((FAILURES + 1))
    fi
done

# Check 2: No Cloud SQL proxy process
if pgrep -f "cloud-sql-proxy" > /dev/null 2>&1; then
    echo "FAIL: Cloud SQL proxy still running (pid: $(pgrep -f cloud-sql-proxy))"
    FAILURES=$((FAILURES + 1))
fi

# Check 3: No launchd plist for proxy
PLIST="$HOME/Library/LaunchAgents/com.jarvis.cloudsql-proxy.plist"
if [ -f "$PLIST" ]; then
    echo "FAIL: Proxy launchd plist still exists at $PLIST"
    FAILURES=$((FAILURES + 1))
fi

# Check 4: No static IPs with jarvis label (except allowlisted)
IP_LIST=$(gcloud compute addresses list --project="$PROJECT" \
    --filter="labels.created-by=jarvis AND status=RESERVED" \
    --format="value(name)" 2>/dev/null || true)
for ip in $IP_LIST; do
    [ -z "$ip" ] && continue
    if ! echo ",$ALWAYS_ON_IPS," | grep -q ",$ip,"; then
        echo "FAIL: Reserved static IP '$ip' not in allowlist"
        FAILURES=$((FAILURES + 1))
    fi
done

# Check 5: Cloud Run min-instances=0 for all tracked services (if solo mode)
if [ "${JARVIS_SOLO_DEVELOPER_MODE:-true}" = "true" ]; then
    IFS=',' read -ra SERVICES <<< "$CLOUD_RUN_SERVICES"
    for svc in "${SERVICES[@]}"; do
        [ -z "$svc" ] && continue
        MIN_INST=$(gcloud run services describe "$svc" \
            --project="$PROJECT" --region="$REGION" \
            --format="value(spec.template.metadata.annotations['autoscaling.knative.dev/minScale'])" 2>/dev/null || true)
        # Missing annotation = default 0 (Cloud Run semantics) = PASS
        # Empty string or "0" = PASS. Anything else = FAIL.
        if [ -n "$MIN_INST" ] && [ "$MIN_INST" != "0" ]; then
            echo "FAIL: Cloud Run '$svc' min-instances=$MIN_INST (expected 0 or unset in solo mode)"
            FAILURES=$((FAILURES + 1))
        fi
    done
fi

if [ "$FAILURES" -eq 0 ]; then
    echo "PASS: Zero-cost posture verified ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
    exit 0
else
    echo "FAIL: $FAILURES cost leak(s) detected ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
    exit 1
fi
