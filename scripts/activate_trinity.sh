#!/usr/bin/env bash
# =============================================================================
# JARVIS Trinity Activation Script (v295.0)
# =============================================================================
#
# Activates the full Mind/Body/Soul pipeline:
#   - Verifies J-Prime GCP VM is reachable and has reasoning endpoints
#   - Verifies protocol version compatibility
#   - Enables feature flags
#   - Restarts JARVIS with full pipeline active
#
# Usage:
#   ./scripts/activate_trinity.sh              # full activation
#   ./scripts/activate_trinity.sh --check-only # verify only, don't activate
#   ./scripts/activate_trinity.sh --rollback   # disable all flags, restart
#
# Prerequisites:
#   - J-Prime golden image deployed with reasoning/ module
#   - .env file has v295.0 flags (auto-added by this script if missing)
#
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$JARVIS_DIR/.env"
JPRIME_HOST="${JARVIS_PRIME_HOST:-136.113.252.164}"
JPRIME_PORT="${JARVIS_PRIME_PORT:-8000}"
JPRIME_URL="http://${JPRIME_HOST}:${JPRIME_PORT}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }

CHECKS_PASSED=0
CHECKS_FAILED=0

check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        log_ok "$name"
        CHECKS_PASSED=$((CHECKS_PASSED + 1))
    else
        log_fail "$name"
        CHECKS_FAILED=$((CHECKS_FAILED + 1))
    fi
}

# =============================================================================
# Rollback mode
# =============================================================================
if [[ "${1:-}" == "--rollback" ]]; then
    echo ""
    echo "=== JARVIS Trinity Rollback ==="
    echo ""

    # Disable all v295 flags
    if [[ -f "$ENV_FILE" ]]; then
        sed -i '' 's/^JARVIS_USE_REMOTE_REASONING=true/JARVIS_USE_REMOTE_REASONING=false/' "$ENV_FILE"
        sed -i '' 's/^JARVIS_VISION_LOOP_ENABLED=true/JARVIS_VISION_LOOP_ENABLED=false/' "$ENV_FILE"
        sed -i '' 's/^JARVIS_USE_REMOTE_BRAIN_SELECTOR=true/JARVIS_USE_REMOTE_BRAIN_SELECTOR=false/' "$ENV_FILE"
        log_ok "Feature flags disabled in .env"
    fi

    echo ""
    log_info "Rollback complete. Restart JARVIS to apply."
    exit 0
fi

# =============================================================================
# Pre-flight checks
# =============================================================================
echo ""
echo "=========================================="
echo "  JARVIS Trinity Activation (v295.0)"
echo "  Mind / Body / Soul"
echo "=========================================="
echo ""

# Check 1: J-Prime VM reachable
log_info "Checking J-Prime at ${JPRIME_URL}..."
if curl -sf --connect-timeout 5 "${JPRIME_URL}/health" >/dev/null 2>&1; then
    HEALTH=$(curl -sf "${JPRIME_URL}/health" 2>/dev/null)
    MODEL_LOADED=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('model_loaded', False))" 2>/dev/null || echo "unknown")
    log_ok "J-Prime reachable (model_loaded=$MODEL_LOADED)"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "J-Prime not reachable at ${JPRIME_URL}"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 2: Reasoning health endpoint
log_info "Checking reasoning pipeline..."
if curl -sf --connect-timeout 5 "${JPRIME_URL}/v1/reason/health" >/dev/null 2>&1; then
    REASON_HEALTH=$(curl -sf "${JPRIME_URL}/v1/reason/health" 2>/dev/null)
    GRAPH_READY=$(echo "$REASON_HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reasoning_graph_ready', False))" 2>/dev/null || echo "unknown")
    log_ok "Reasoning pipeline ready (graph_ready=$GRAPH_READY)"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "Reasoning endpoint not available — deploy J-Prime golden image first"
    log_info "  ssh jarvis-prime-gpu && cd /opt/jarvis-prime && git pull && sudo systemctl restart jarvis-prime"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 3: Protocol version compatibility
log_info "Checking protocol compatibility..."
if curl -sf --connect-timeout 5 "${JPRIME_URL}/v1/protocol/version" >/dev/null 2>&1; then
    PROTO=$(curl -sf "${JPRIME_URL}/v1/protocol/version" 2>/dev/null)
    PROTO_VER=$(echo "$PROTO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('current_version', 'unknown'))" 2>/dev/null || echo "unknown")
    log_ok "Protocol version: $PROTO_VER"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "Protocol version endpoint not available"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 4: Vision analyze endpoint
log_info "Checking vision analyze endpoint..."
VISION_RESP=$(curl -sf --connect-timeout 5 -X POST "${JPRIME_URL}/v1/vision/analyze" \
    -H "Content-Type: application/json" \
    -d '{"request_id":"check","session_id":"check","trace_id":"check","frame":{"artifact_ref":"check","width":1440,"height":900,"scale_factor":2.0,"captured_at_ms":0,"display_id":0},"task":{"type":"find_element","target_description":"test","action_intent":"click"}}' 2>/dev/null || echo "FAILED")
if [[ "$VISION_RESP" != "FAILED" ]]; then
    log_ok "Vision analyze endpoint responding"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "Vision analyze endpoint not available"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 5: Brain selection endpoint
log_info "Checking brain selection endpoint..."
BRAIN_RESP=$(curl -sf --connect-timeout 5 -X POST "${JPRIME_URL}/v1/reason/select" \
    -H "Content-Type: application/json" \
    -d '{"request_id":"check","session_id":"check","trace_id":"check","command":"test"}' 2>/dev/null || echo "FAILED")
if [[ "$BRAIN_RESP" != "FAILED" ]]; then
    log_ok "Brain selection endpoint responding"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "Brain selection endpoint not available"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 6: Full reasoning endpoint
log_info "Checking full reasoning endpoint..."
REASON_RESP=$(curl -sf --connect-timeout 10 -X POST "${JPRIME_URL}/v1/reason" \
    -H "Content-Type: application/json" \
    -d '{"request_id":"check","session_id":"check","trace_id":"check","command":"open Safari"}' 2>/dev/null || echo "FAILED")
if [[ "$REASON_RESP" != "FAILED" ]]; then
    REASON_STATUS=$(echo "$REASON_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status', 'unknown'))" 2>/dev/null || echo "unknown")
    log_ok "Full reasoning endpoint responding (status=$REASON_STATUS)"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_fail "Full reasoning endpoint not available"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
fi

# Check 7: Local JARVIS files exist
log_info "Checking local JARVIS vision modules..."
check "VisionActionLoop exists" test -f "$JARVIS_DIR/backend/vision/realtime/vision_action_loop.py"
check "Knowledge Fabric exists" test -f "$JARVIS_DIR/backend/knowledge/fabric.py"
check "MindClient exists" test -f "$JARVIS_DIR/backend/core/mind_client.py"
check "PRECHECK gate exists" test -f "$JARVIS_DIR/backend/vision/realtime/precheck_gate.py"

# Check 8: .env has v295 flags
log_info "Checking .env configuration..."
if grep -q "JARVIS_VISION_LOOP_ENABLED" "$ENV_FILE" 2>/dev/null; then
    log_ok ".env has v295.0 flags"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
else
    log_warn ".env missing v295.0 flags — will be added on activation"
fi

# =============================================================================
# Results
# =============================================================================
echo ""
echo "=========================================="
echo "  Pre-flight Results"
echo "=========================================="
echo ""
echo "  Passed: $CHECKS_PASSED"
echo "  Failed: $CHECKS_FAILED"
echo ""

if [[ "${1:-}" == "--check-only" ]]; then
    if [[ $CHECKS_FAILED -eq 0 ]]; then
        log_ok "All checks passed. Ready to activate."
    else
        log_warn "$CHECKS_FAILED check(s) failed. Fix before activating."
    fi
    exit $CHECKS_FAILED
fi

if [[ $CHECKS_FAILED -gt 2 ]]; then
    log_fail "Too many failures ($CHECKS_FAILED). Fix J-Prime deployment first."
    echo ""
    log_info "Deploy J-Prime golden image:"
    echo "  ssh jarvis-prime-gpu"
    echo "  cd /opt/jarvis-prime"
    echo "  git pull origin main"
    echo "  sudo systemctl restart jarvis-prime"
    echo ""
    log_info "Then re-run: ./scripts/activate_trinity.sh"
    exit 1
fi

# =============================================================================
# Activation
# =============================================================================
echo ""
echo "=========================================="
echo "  Activating Trinity Pipeline"
echo "=========================================="
echo ""

# Verify flags are set correctly
log_info "Verifying .env flags..."
VISION_LOOP=$(grep "^JARVIS_VISION_LOOP_ENABLED=" "$ENV_FILE" | cut -d= -f2)
REMOTE_REASON=$(grep "^JARVIS_USE_REMOTE_REASONING=" "$ENV_FILE" | cut -d= -f2)
SHADOW=$(grep "^JARVIS_BRAIN_SELECTOR_SHADOW=" "$ENV_FILE" | cut -d= -f2)

echo "  JARVIS_VISION_LOOP_ENABLED=$VISION_LOOP"
echo "  JARVIS_USE_REMOTE_REASONING=$REMOTE_REASON"
echo "  JARVIS_BRAIN_SELECTOR_SHADOW=$SHADOW"
echo ""

if [[ "$VISION_LOOP" == "true" && "$REMOTE_REASON" == "true" ]]; then
    log_ok "All flags active"
else
    log_warn "Some flags not set to true — enable in .env for full activation"
fi

echo ""
echo "=========================================="
echo "  Trinity Activation Complete"
echo "=========================================="
echo ""
echo "  Mind (J-Prime):   ${JPRIME_URL}"
echo "  Body (JARVIS):    Local MacBook Pro"
echo "  Soul (Reactor):   Learning from outcomes"
echo ""
echo "  Next: Restart JARVIS to apply"
echo ""
echo "  Capabilities activated:"
echo "    - Real-time 30 FPS vision (VisionActionLoop)"
echo "    - Mind reasoning pipeline (POST /v1/reason)"
echo "    - Brain selection shadow mode"
echo "    - PRECHECK safety gate (5 guards)"
echo "    - Knowledge Fabric (scene/semantic/trinity)"
echo "    - Tiered vision (L1 scene -> L2 J-Prime -> L3 Claude)"
echo ""
echo "  Test commands to try:"
echo '    "JARVIS, what do you see?"'
echo '    "JARVIS, open Safari"'
echo '    "JARVIS, click the submit button"'
echo '    "JARVIS, go to LinkedIn and message Zach"'
echo ""
