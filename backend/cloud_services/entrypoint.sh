#!/bin/bash
# =============================================================================
# ECAPA Cloud Service Entrypoint Script
# =============================================================================
# Robust startup script that handles:
# - Cache directory creation and permissions
# - Fallback mechanisms for model loading
# - Comprehensive error handling and logging
# - Health pre-checks before service start
#
# v18.3.0
# =============================================================================

set -e  # Exit on error

# =============================================================================
# CONFIGURATION
# =============================================================================
SOURCE_CACHE="${ECAPA_SOURCE_CACHE:-/opt/ecapa_cache}"
RUNTIME_CACHE="${ECAPA_CACHE_DIR:-/tmp/ecapa_cache}"
FALLBACK_CACHE="${HOME}/.cache/ecapa"
LOG_PREFIX="[ENTRYPOINT]"

# =============================================================================
# LOGGING FUNCTIONS
# =============================================================================
log_info() {
    echo "${LOG_PREFIX} [INFO] $(date '+%Y-%m-%d %H:%M:%S') $1"
}

log_warn() {
    echo "${LOG_PREFIX} [WARN] $(date '+%Y-%m-%d %H:%M:%S') $1" >&2
}

log_error() {
    echo "${LOG_PREFIX} [ERROR] $(date '+%Y-%m-%d %H:%M:%S') $1" >&2
}

log_debug() {
    if [ "${DEBUG:-false}" = "true" ]; then
        echo "${LOG_PREFIX} [DEBUG] $(date '+%Y-%m-%d %H:%M:%S') $1"
    fi
}

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

# Check if directory is writable
is_writable() {
    local dir="$1"
    if [ -d "$dir" ]; then
        touch "$dir/.write_test" 2>/dev/null && rm -f "$dir/.write_test" 2>/dev/null
        return $?
    fi
    return 1
}

# Create directory with proper permissions
create_writable_dir() {
    local dir="$1"
    log_info "Creating directory: $dir"

    # Remove if exists (start fresh)
    rm -rf "$dir" 2>/dev/null || true

    # Create new directory
    mkdir -p "$dir"

    # Set permissive permissions
    chmod 777 "$dir" 2>/dev/null || chmod 755 "$dir" 2>/dev/null || true

    if is_writable "$dir"; then
        log_info "Directory $dir is writable"
        return 0
    else
        log_warn "Directory $dir may not be writable"
        return 1
    fi
}

# Copy cache with proper permissions
copy_cache() {
    local src="$1"
    local dst="$2"

    log_info "Copying cache from $src to $dst"

    # Create destination directory fresh
    create_writable_dir "$dst"

    # Check if source exists
    if [ ! -d "$src" ]; then
        log_warn "Source cache not found: $src"
        return 1
    fi

    # Copy contents (not the directory itself)
    if [ -d "$src" ] && [ "$(ls -A $src 2>/dev/null)" ]; then
        # Use cp with dereference to handle symlinks
        cp -rL "$src"/* "$dst"/ 2>/dev/null || cp -r "$src"/* "$dst"/ 2>/dev/null

        # Set permissions on all files and directories
        chmod -R 777 "$dst" 2>/dev/null || chmod -R 755 "$dst" 2>/dev/null || true

        # Specifically ensure yaml and pickle files are readable/writable
        find "$dst" -type f \( -name "*.yaml" -o -name "*.pkl" -o -name "*.ckpt" -o -name "*.pt" \) \
            -exec chmod 666 {} \; 2>/dev/null || true

        log_info "Cache copy complete. Contents:"
        ls -la "$dst" 2>/dev/null || true

        return 0
    else
        log_warn "Source cache is empty: $src"
        return 1
    fi
}

# Verify cache integrity
verify_cache() {
    local cache_dir="$1"
    local required_files=("hyperparams.yaml" "embedding_model.ckpt")
    local missing=0

    log_info "Verifying cache integrity in $cache_dir"

    for file in "${required_files[@]}"; do
        if [ -f "$cache_dir/$file" ]; then
            log_debug "Found: $file"
            # Check if readable
            if [ -r "$cache_dir/$file" ]; then
                log_debug "Readable: $file"
            else
                log_warn "Not readable: $file"
                chmod 644 "$cache_dir/$file" 2>/dev/null || true
            fi
        else
            log_warn "Missing required file: $file"
            missing=$((missing + 1))
        fi
    done

    if [ $missing -gt 0 ]; then
        log_warn "Cache verification failed: $missing files missing"
        return 1
    fi

    log_info "Cache verification passed"
    return 0
}

# =============================================================================
# MAIN SETUP LOGIC
# =============================================================================

main() {
    log_info "=============================================="
    log_info "ECAPA Cloud Service Startup - v18.3.0"
    log_info "=============================================="
    log_info "User: $(whoami) (UID: $(id -u))"
    log_info "Working directory: $(pwd)"
    log_info "Source cache: $SOURCE_CACHE"
    log_info "Runtime cache: $RUNTIME_CACHE"
    log_info "=============================================="

    # Step 1: Setup runtime cache directory
    log_info "Step 1: Setting up runtime cache..."

    CACHE_READY=false
    FINAL_CACHE_DIR=""

    # Try primary location: /tmp/ecapa_cache
    if copy_cache "$SOURCE_CACHE" "$RUNTIME_CACHE"; then
        if verify_cache "$RUNTIME_CACHE"; then
            FINAL_CACHE_DIR="$RUNTIME_CACHE"
            CACHE_READY=true
            log_info "Primary cache setup successful: $RUNTIME_CACHE"
        fi
    fi

    # Try fallback location if primary failed
    if [ "$CACHE_READY" = "false" ]; then
        log_warn "Primary cache setup failed, trying fallback..."

        if copy_cache "$SOURCE_CACHE" "$FALLBACK_CACHE"; then
            if verify_cache "$FALLBACK_CACHE"; then
                FINAL_CACHE_DIR="$FALLBACK_CACHE"
                CACHE_READY=true
                export ECAPA_CACHE_DIR="$FALLBACK_CACHE"
                log_info "Fallback cache setup successful: $FALLBACK_CACHE"
            fi
        fi
    fi

    # If no pre-downloaded cache works, let SpeechBrain download fresh
    if [ "$CACHE_READY" = "false" ]; then
        log_warn "Pre-downloaded cache not available, will download fresh"

        # Create empty writable cache directory
        create_writable_dir "$RUNTIME_CACHE"
        FINAL_CACHE_DIR="$RUNTIME_CACHE"

        # Set environment to download fresh
        export ECAPA_CACHE_DIR="$RUNTIME_CACHE"
        export SPEECHBRAIN_CACHE="$RUNTIME_CACHE"

        log_info "Fresh download will be attempted at startup"
    fi

    # Step 2: Set additional environment variables
    log_info "Step 2: Setting environment variables..."

    # Ensure various cache directories point to writable location
    export HF_HOME="${FINAL_CACHE_DIR}/huggingface"
    export TRANSFORMERS_CACHE="${FINAL_CACHE_DIR}/transformers"
    export TORCH_HOME="${FINAL_CACHE_DIR}/torch"
    export XDG_CACHE_HOME="${FINAL_CACHE_DIR}"

    # Create subdirectories
    mkdir -p "$HF_HOME" "$TRANSFORMERS_CACHE" "$TORCH_HOME" 2>/dev/null || true
    chmod -R 777 "$FINAL_CACHE_DIR" 2>/dev/null || true

    log_info "Cache environment configured:"
    log_info "  ECAPA_CACHE_DIR=$ECAPA_CACHE_DIR"
    log_info "  HF_HOME=$HF_HOME"
    log_info "  TORCH_HOME=$TORCH_HOME"

    # Step 3: Pre-flight checks
    log_info "Step 3: Running pre-flight checks..."

    # Check Python
    if command -v python &> /dev/null; then
        PYTHON_VERSION=$(python --version 2>&1)
        log_info "Python: $PYTHON_VERSION"
    else
        log_error "Python not found!"
        exit 1
    fi

    # Check if main script exists
    if [ ! -f "ecapa_cloud_service.py" ]; then
        log_error "ecapa_cloud_service.py not found in $(pwd)"
        exit 1
    fi

    # Step 4: Final cache state
    log_info "Step 4: Final cache state:"
    log_info "Cache directory: $FINAL_CACHE_DIR"
    ls -la "$FINAL_CACHE_DIR" 2>/dev/null || log_warn "Could not list cache directory"

    # Show disk space
    df -h "$FINAL_CACHE_DIR" 2>/dev/null | tail -1 || true

    log_info "=============================================="
    log_info "Starting ECAPA Cloud Service..."
    log_info "=============================================="

    # Step 5: Start the service
    exec python ecapa_cloud_service.py
}

# =============================================================================
# RUN
# =============================================================================
main "$@"
