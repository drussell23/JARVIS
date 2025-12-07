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
# CONFIGURATION - v18.4.0
# =============================================================================
SOURCE_CACHE="${ECAPA_SOURCE_CACHE:-/opt/ecapa_cache}"
RUNTIME_CACHE="${ECAPA_CACHE_DIR:-/tmp/ecapa_cache}"
FALLBACK_CACHE="${HOME}/.cache/ecapa"
LOG_PREFIX="[ENTRYPOINT]"

# Required files for ECAPA model
REQUIRED_FILES="hyperparams.yaml embedding_model.ckpt"

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
    log_info "ECAPA Cloud Service Startup - v18.5.0"
    log_info "=============================================="
    log_info "User: $(whoami) (UID: $(id -u))"
    log_info "Working directory: $(pwd)"
    log_info "Pre-baked cache: $SOURCE_CACHE"
    log_info "HF_HOME: ${HF_HOME:-not set}"
    log_info "HF_HUB_OFFLINE: ${HF_HUB_OFFLINE:-not set}"
    log_info "=============================================="

    # Step 1: Verify pre-baked cache exists
    log_info "Step 1: Verifying pre-baked cache..."

    # Check HuggingFace cache exists (this is where the model actually lives)
    HF_CACHE_PATH="${HF_HOME:-/opt/ecapa_cache/huggingface}"
    if [ -d "$HF_CACHE_PATH" ]; then
        log_info "✅ HuggingFace cache found: $HF_CACHE_PATH"
        # List contents for debugging
        HF_CACHE_CONTENTS=$(find "$HF_CACHE_PATH" -type f -name "*.yaml" -o -name "*.ckpt" 2>/dev/null | head -5)
        if [ -n "$HF_CACHE_CONTENTS" ]; then
            log_info "✅ Model files found in HuggingFace cache"
            log_debug "Files: $HF_CACHE_CONTENTS"
        else
            log_warn "⚠️ No model files found in HuggingFace cache"
        fi
    else
        log_warn "⚠️ HuggingFace cache not found: $HF_CACHE_PATH"
    fi

    # Also check savedir
    if [ -d "$SOURCE_CACHE" ]; then
        log_info "✅ Source cache exists: $SOURCE_CACHE"
        ls -la "$SOURCE_CACHE" 2>/dev/null | head -10 || true
    else
        log_warn "⚠️ Source cache not found: $SOURCE_CACHE"
    fi

    # Step 2: Create writable temp directories
    log_info "Step 2: Creating temp directories..."

    mkdir -p /tmp/torch_cache /tmp/xdg_cache /tmp/speechbrain_cache 2>/dev/null || true

    log_info "Environment configured:"
    log_info "  ECAPA_CACHE_DIR=${ECAPA_CACHE_DIR:-not set}"
    log_info "  HF_HOME=${HF_HOME:-not set}"
    log_info "  HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-not set}"

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

    log_info "=============================================="
    log_info "Starting ECAPA Cloud Service..."
    log_info "=============================================="

    # Start the service
    exec python ecapa_cloud_service.py
}

# =============================================================================
# RUN
# =============================================================================
main "$@"
