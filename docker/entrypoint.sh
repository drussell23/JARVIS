#!/bin/bash
# =============================================================================
# JARVIS Backend Entrypoint Script
# =============================================================================
#
# Production entrypoint for JARVIS backend container that:
# - Initializes the SQLite training database
# - Verifies required directories exist
# - Checks model availability
# - Starts the JARVIS supervisor or main API
#
# Usage:
#   ./entrypoint.sh [command]
#
# Commands:
#   supervisor  - Start the full JARVIS supervisor (default)
#   api         - Start only the FastAPI backend
#   training    - Start the training engine
#   shell       - Start a bash shell for debugging
#
# Version: 9.4.0
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

JARVIS_HOME="${JARVIS_HOME:-/app}"
JARVIS_DATA_DIR="${JARVIS_DATA_DIR:-/app/data}"
JARVIS_MODELS_DIR="${JARVIS_MODELS_DIR:-/app/models}"
JARVIS_LOGS_DIR="${JARVIS_LOGS_DIR:-/app/data/logs}"
JARVIS_TRAINING_DB="${JARVIS_TRAINING_DB:-/app/data/training_db/jarvis_training.db}"

# Colors for logging (disabled if not TTY)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m' # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

# -----------------------------------------------------------------------------
# Logging Functions
# -----------------------------------------------------------------------------

log_info() {
    echo -e "${BLUE}[JARVIS]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[JARVIS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[JARVIS]${NC} WARNING: $1"
}

log_error() {
    echo -e "${RED}[JARVIS]${NC} ERROR: $1"
}

# -----------------------------------------------------------------------------
# Initialization Functions
# -----------------------------------------------------------------------------

init_directories() {
    log_info "Initializing directories..."

    # Create required directories
    mkdir -p "$JARVIS_DATA_DIR/training_db"
    mkdir -p "$JARVIS_DATA_DIR/voiceprints"
    mkdir -p "$JARVIS_DATA_DIR/experiences"
    mkdir -p "$JARVIS_DATA_DIR/knowledge"
    mkdir -p "$JARVIS_LOGS_DIR"
    mkdir -p "$JARVIS_MODELS_DIR/current"
    mkdir -p "$JARVIS_MODELS_DIR/archive"

    log_success "Directories initialized"
}

init_database() {
    log_info "Initializing SQLite training database..."

    # Check if schema file exists
    SCHEMA_FILE="$JARVIS_HOME/docker/training_db_schema.sql"
    if [ -f "$SCHEMA_FILE" ]; then
        # Initialize database with schema
        sqlite3 "$JARVIS_TRAINING_DB" < "$SCHEMA_FILE" 2>/dev/null || true
        log_success "Training database initialized: $JARVIS_TRAINING_DB"
    else
        log_warning "Schema file not found, skipping database init"
    fi

    # Verify database
    if [ -f "$JARVIS_TRAINING_DB" ]; then
        COUNT=$(sqlite3 "$JARVIS_TRAINING_DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo "0")
        log_info "Training database has $COUNT tables"
    fi
}

check_models() {
    log_info "Checking model availability..."

    # Check for base model
    if [ -f "$JARVIS_MODELS_DIR/current/jarvis-prime-latest.gguf" ]; then
        log_success "JARVIS Prime model found"
    else
        log_warning "JARVIS Prime model not found - will download on first use"
    fi

    # Check for ECAPA-TDNN (voice biometrics)
    if [ -d "$JARVIS_MODELS_DIR/ecapa_tdnn" ]; then
        log_success "ECAPA-TDNN voice model found"
    else
        log_info "ECAPA-TDNN will be downloaded on first voice enrollment"
    fi
}

check_environment() {
    log_info "Checking environment..."

    # Required environment variables
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        log_warning "ANTHROPIC_API_KEY not set - Claude features will be limited"
    else
        log_success "ANTHROPIC_API_KEY configured"
    fi

    # Optional environment variables
    if [ -n "$GCS_BUCKET" ]; then
        log_info "GCS bucket configured: $GCS_BUCKET"
    fi

    if [ -n "$JARVIS_PRIME_CLOUD_RUN_URL" ]; then
        log_info "Cloud Run URL configured for JARVIS Prime"
    fi

    # Log Python version
    PYTHON_VERSION=$(python3 --version 2>&1)
    log_info "Python: $PYTHON_VERSION"
}

run_health_check() {
    log_info "Running pre-start health check..."

    # Check Python imports
    python3 -c "
import sys
try:
    import fastapi
    import uvicorn
    import torch
    import anthropic
    print('Core dependencies OK')
except ImportError as e:
    print(f'Missing dependency: {e}')
    sys.exit(1)
" || {
        log_error "Dependency check failed"
        exit 1
    }

    log_success "Health check passed"
}

# -----------------------------------------------------------------------------
# Start Commands
# -----------------------------------------------------------------------------

start_supervisor() {
    log_info "Starting JARVIS Supervisor..."

    cd "$JARVIS_HOME"

    # Set Python path
    export PYTHONPATH="$JARVIS_HOME:$JARVIS_HOME/backend:$PYTHONPATH"

    # Start supervisor with all components
    exec python3 run_supervisor.py \
        --host 0.0.0.0 \
        --port 8010 \
        --loading-port 8011 \
        --no-browser \
        "$@"
}

start_api() {
    log_info "Starting JARVIS API only..."

    cd "$JARVIS_HOME"
    export PYTHONPATH="$JARVIS_HOME:$JARVIS_HOME/backend:$PYTHONPATH"

    exec python3 -m uvicorn backend.main:app \
        --host 0.0.0.0 \
        --port 8010 \
        "$@"
}

start_training() {
    log_info "Starting JARVIS Training Engine..."

    cd "$JARVIS_HOME"
    export PYTHONPATH="$JARVIS_HOME:$JARVIS_HOME/backend:$PYTHONPATH"

    exec python3 docker/training_entrypoint.py \
        --mode continuous \
        "$@"
}

start_shell() {
    log_info "Starting interactive shell..."
    exec /bin/bash
}

# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------

main() {
    log_info "=========================================="
    log_info "  JARVIS AI Agent v9.4.0"
    log_info "  Production Container Entrypoint"
    log_info "=========================================="

    # Initialize
    init_directories
    init_database
    check_models
    check_environment
    run_health_check

    # Parse command
    COMMAND="${1:-supervisor}"
    shift || true

    log_info "Starting command: $COMMAND"

    case "$COMMAND" in
        supervisor)
            start_supervisor "$@"
            ;;
        api)
            start_api "$@"
            ;;
        training)
            start_training "$@"
            ;;
        shell)
            start_shell
            ;;
        python)
            exec python3 "$@"
            ;;
        *)
            # Pass through any other command
            exec "$@"
            ;;
    esac
}

# Run main
main "$@"
