#!/bin/bash
# =============================================================================
# Complete Voice Unlock Deployment Script
# =============================================================================
#
# This script provides a unified deployment interface for:
# - Local Docker development
# - GCP Cloud Run deployment
# - GCP VM Spot instance setup
# - Voice unlock system integration
#
# Usage:
#   ./deploy_voice_unlock_complete.sh --mode local
#   ./deploy_voice_unlock_complete.sh --mode cloud-run
#   ./deploy_voice_unlock_complete.sh --mode vm
#   ./deploy_voice_unlock_complete.sh --mode all
#
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
MODE=""
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"
CLOUD_SERVICES_DIR="${BACKEND_DIR}/cloud_services"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode)
            MODE="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --mode MODE    Deployment mode: local, cloud-run, vm, or all"
            echo "  --help         Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Helper functions
log() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

success() {
    echo -e "${GREEN}✅${NC} $1"
}

warning() {
    echo -e "${YELLOW}⚠️${NC} $1"
}

error() {
    echo -e "${RED}❌${NC} $1"
}

check_prerequisites() {
    log "Checking prerequisites..."
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        error "Python 3 not found"
        exit 1
    fi
    
    # Check Docker (for local/cloud-run)
    if [[ "$MODE" == "local" || "$MODE" == "cloud-run" || "$MODE" == "all" ]]; then
        if ! command -v docker &> /dev/null; then
            error "Docker not found (required for local/cloud-run mode)"
            exit 1
        fi
    fi
    
    # Check gcloud (for cloud-run/vm)
    if [[ "$MODE" == "cloud-run" || "$MODE" == "vm" || "$MODE" == "all" ]]; then
        if ! command -v gcloud &> /dev/null; then
            error "gcloud CLI not found (required for cloud-run/vm mode)"
            exit 1
        fi
        
        if ! gcloud auth list 2>/dev/null | grep -q "ACTIVE"; then
            error "Not authenticated with gcloud. Run: gcloud auth login"
            exit 1
        fi
    fi
    
    success "Prerequisites OK"
}

deploy_local() {
    log "Deploying local Docker environment..."
    
    cd "${CLOUD_SERVICES_DIR}"
    
    # Build and start
    log "Building Docker image..."
    docker compose build
    
    log "Starting services..."
    docker compose up -d
    
    # Wait for health check
    log "Waiting for service to be ready..."
    MAX_RETRIES=30
    RETRY=0
    
    while [ $RETRY -lt $MAX_RETRIES ]; do
        if curl -sf http://localhost:8010/health > /dev/null 2>&1; then
            success "Local service is ready!"
            echo ""
            echo "Service URL: http://localhost:8010"
            echo "Health: http://localhost:8010/health"
            echo "Status: http://localhost:8010/status"
            echo ""
            echo "To view logs: docker compose logs -f"
            echo "To stop: docker compose down"
            return 0
        fi
        sleep 2
        RETRY=$((RETRY + 1))
    done
    
    error "Service failed to start within timeout"
    echo "Check logs: docker compose logs"
    return 1
}

deploy_cloud_run() {
    log "Deploying to GCP Cloud Run..."
    
    cd "${CLOUD_SERVICES_DIR}"
    
    # Run deployment script
    if [ -f "deploy_cloud_run.sh" ]; then
        chmod +x deploy_cloud_run.sh
        ./deploy_cloud_run.sh
    else
        error "deploy_cloud_run.sh not found"
        return 1
    fi
    
    # Get service URL
    SERVICE_URL=$(gcloud run services describe jarvis-ml \
        --region us-central1 \
        --format 'value(status.url)' 2>/dev/null || echo "")
    
    if [ -n "$SERVICE_URL" ]; then
        success "Cloud Run deployment complete!"
        echo ""
        echo "Service URL: ${SERVICE_URL}"
        echo "Health: ${SERVICE_URL}/health"
        echo ""
        echo "Update .env with:"
        echo "  JARVIS_CLOUD_ML_ENDPOINT=${SERVICE_URL}/api/ml"
        return 0
    else
        warning "Could not retrieve service URL"
        return 1
    fi
}

deploy_vm() {
    log "Setting up GCP VM Spot instance configuration..."
    
    # Check if VM manager exists
    if [ ! -f "${BACKEND_DIR}/core/gcp_vm_manager.py" ]; then
        warning "VM manager not found. Skipping VM setup."
        return 0
    fi
    
    # Check configuration
    log "Checking VM configuration..."
    
    if [ -z "$GCP_PROJECT_ID" ]; then
        warning "GCP_PROJECT_ID not set. Using default: jarvis-473803"
        export GCP_PROJECT_ID=jarvis-473803
    fi
    
    # Verify VM manager can be imported
    python3 -c "
import sys
sys.path.insert(0, '${BACKEND_DIR}')
try:
    from core.gcp_vm_manager import get_gcp_vm_manager
    print('✅ VM manager available')
except ImportError as e:
    print(f'⚠️ VM manager not available: {e}')
    sys.exit(1)
" || {
        warning "VM manager not available. Install dependencies:"
        echo "  pip install google-cloud-compute"
        return 0
    }
    
    success "VM configuration ready"
    echo ""
    echo "VM auto-creation is enabled when:"
    echo "  - Memory pressure >85%"
    echo "  - GCP_VM_ENABLED=true"
    echo ""
    echo "Check VM status:"
    echo "  python3 ${BACKEND_DIR}/core/gcp_vm_status.py"
    return 0
}

setup_voice_unlock() {
    log "Setting up voice unlock system..."
    
    cd "${PROJECT_ROOT}"
    
    # Check dependencies
    log "Checking Python dependencies..."
    python3 -c "
import sys
missing = []
try:
    import numpy
except ImportError:
    missing.append('numpy')
try:
    import torch
except ImportError:
    missing.append('torch')
try:
    import speechbrain
except ImportError:
    missing.append('speechbrain')
try:
    import scipy
except ImportError:
    missing.append('scipy')
try:
    import librosa
except ImportError:
    missing.append('librosa')

if missing:
    print(f'Missing: {', '.join(missing)}')
    print('Install with: pip install ' + ' '.join(missing))
    sys.exit(1)
else:
    print('✅ All dependencies installed')
" || {
        warning "Some dependencies are missing"
        echo "Install with: pip install numpy torch speechbrain scipy librosa"
    }
    
    # Check voice profiles
    log "Checking voice profiles..."
    python3 -c "
import sys
import os
sys.path.insert(0, '${BACKEND_DIR}')
try:
    from intelligence.hybrid_database_sync import HybridDatabaseSync
    import asyncio
    
    async def check():
        db = HybridDatabaseSync()
        await db.initialize()
        profile = await db.find_owner_profile()
        if profile:
            samples = profile.get('total_samples', 0)
            print(f'✅ Voice profile found: {profile.get(\"name\")} ({samples} samples)')
        else:
            print('⚠️ No voice profile found')
            print('Enroll with: python backend/voice/enroll_voice.py')
    
    asyncio.run(check())
except Exception as e:
    print(f'⚠️ Could not check profiles: {e}')
" || warning "Could not check voice profiles"
    
    # Run diagnostic
    log "Running diagnostic check..."
    if [ -f "${BACKEND_DIR}/voice_unlock/intelligent_diagnostic_system.py" ]; then
        python3 "${BACKEND_DIR}/voice_unlock/intelligent_diagnostic_system.py" --json > /tmp/voice_unlock_diagnostics.json 2>&1 || true
        if [ -f /tmp/voice_unlock_diagnostics.json ]; then
            STATUS=$(python3 -c "import json; d=json.load(open('/tmp/voice_unlock_diagnostics.json')); print(d.get('overall_status', 'unknown'))" 2>/dev/null || echo "unknown")
            if [ "$STATUS" == "healthy" ]; then
                success "Voice unlock system is healthy"
            else
                warning "Voice unlock system has issues. Check diagnostics:"
                echo "  python3 ${BACKEND_DIR}/voice_unlock/intelligent_diagnostic_system.py"
            fi
        fi
    fi
    
    success "Voice unlock setup complete"
}

# Main execution
main() {
    echo ""
    echo "============================================================"
    echo "  Complete Voice Unlock Deployment"
    echo "============================================================"
    echo ""
    
    if [ -z "$MODE" ]; then
        error "Mode not specified. Use --mode local, cloud-run, vm, or all"
        echo ""
        echo "Usage: $0 --mode <mode>"
        exit 1
    fi
    
    check_prerequisites
    
    case "$MODE" in
        local)
            deploy_local
            setup_voice_unlock
            ;;
        cloud-run)
            deploy_cloud_run
            setup_voice_unlock
            ;;
        vm)
            deploy_vm
            setup_voice_unlock
            ;;
        all)
            log "Deploying all components..."
            deploy_local && success "Local deployment complete" || warning "Local deployment had issues"
            deploy_cloud_run && success "Cloud Run deployment complete" || warning "Cloud Run deployment had issues"
            deploy_vm && success "VM setup complete" || warning "VM setup had issues"
            setup_voice_unlock
            ;;
        *)
            error "Invalid mode: $MODE"
            echo "Valid modes: local, cloud-run, vm, all"
            exit 1
            ;;
    esac
    
    echo ""
    echo "============================================================"
    success "Deployment complete!"
    echo "============================================================"
    echo ""
    echo "Next steps:"
    echo "  1. Test voice unlock: Say 'Hey JARVIS, unlock my screen'"
    echo "  2. Check diagnostics: python backend/voice_unlock/intelligent_diagnostic_system.py"
    echo "  3. Monitor logs: tail -f backend.log"
    echo ""
}

# Run main
main "$@"
