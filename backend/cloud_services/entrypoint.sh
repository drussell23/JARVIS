#!/bin/bash
# =============================================================================
# ECAPA Cloud Service Entrypoint Script v20.3.0
# =============================================================================
# Robust startup script that handles:
# - Optimized model detection (JIT/ONNX/Quantized)
# - Cache directory creation and permissions
# - Manifest verification for instant startup
# - Fallback mechanisms for model loading
# - Comprehensive error handling and logging
# - Health pre-checks before service start
#
# v20.0.0 - Support for multi-strategy optimization (JIT/ONNX/Quantization)
# =============================================================================

set -e  # Exit on error

# =============================================================================
# CONFIGURATION
# =============================================================================
SOURCE_CACHE="${ECAPA_SOURCE_CACHE:-/opt/ecapa_cache}"
RUNTIME_CACHE="${ECAPA_CACHE_DIR:-/tmp/ecapa_cache}"
FALLBACK_CACHE="${HOME}/.cache/ecapa"
LOG_PREFIX="[ENTRYPOINT]"

# Required files for ECAPA model
REQUIRED_FILES="hyperparams.yaml embedding_model.ckpt"

# Optimized model files (v20.0.0)
JIT_MODEL="ecapa_jit_traced.pt"
ONNX_MODEL="ecapa_model.onnx"
QUANTIZED_MODEL="ecapa_quantized_dynamic.pt"
OPTIMIZATION_MANIFEST=".optimization_manifest.json"

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

log_success() {
    echo "${LOG_PREFIX} [OK] $(date '+%Y-%m-%d %H:%M:%S') $1"
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
    log_debug "Creating directory: $dir"

    mkdir -p "$dir" 2>/dev/null || true
    chmod 755 "$dir" 2>/dev/null || true

    if is_writable "$dir"; then
        log_debug "Directory $dir is writable"
        return 0
    else
        log_warn "Directory $dir may not be writable"
        return 1
    fi
}

# Check for optimized model files
check_optimized_models() {
    local cache_dir="$1"
    local found_models=""

    log_info "Checking for optimized model files..."

    # Check JIT model
    if [ -f "$cache_dir/$JIT_MODEL" ]; then
        local size=$(du -h "$cache_dir/$JIT_MODEL" 2>/dev/null | cut -f1)
        log_success "JIT model found: $JIT_MODEL ($size)"
        found_models="$found_models jit"
    fi

    # Check ONNX model
    if [ -f "$cache_dir/$ONNX_MODEL" ]; then
        local size=$(du -h "$cache_dir/$ONNX_MODEL" 2>/dev/null | cut -f1)
        log_success "ONNX model found: $ONNX_MODEL ($size)"
        found_models="$found_models onnx"
    fi

    # Check quantized model
    if [ -f "$cache_dir/$QUANTIZED_MODEL" ]; then
        local size=$(du -h "$cache_dir/$QUANTIZED_MODEL" 2>/dev/null | cut -f1)
        log_success "Quantized model found: $QUANTIZED_MODEL ($size)"
        found_models="$found_models quantized"
    fi

    # Check optimization manifest
    if [ -f "$cache_dir/$OPTIMIZATION_MANIFEST" ]; then
        log_success "Optimization manifest found"
        # Parse best strategy from manifest
        if command -v python &> /dev/null; then
            local best_strategy=$(python -c "
import json
try:
    with open('$cache_dir/$OPTIMIZATION_MANIFEST') as f:
        m = json.load(f)
        print(m.get('best_strategy', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)
            log_info "Best optimization strategy: $best_strategy"
        fi
    fi

    if [ -z "$found_models" ]; then
        log_warn "No optimized models found - will use standard SpeechBrain loading"
        return 1
    else
        log_info "Optimized models available:$found_models"
        return 0
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

    log_success "Cache verification passed"
    return 0
}

# =============================================================================
# MAIN SETUP LOGIC
# =============================================================================

main() {
    log_info "=============================================="
    log_info "ECAPA Cloud Service Startup v20.3.0"
    log_info "  Multi-Strategy Optimization Support"
    log_info "=============================================="
    log_info "User: $(whoami) (UID: $(id -u))"
    log_info "Working directory: $(pwd)"
    log_info "Pre-baked cache: $SOURCE_CACHE"
    log_info "HF_HOME: ${HF_HOME:-not set}"
    log_info "HF_HUB_OFFLINE: ${HF_HUB_OFFLINE:-not set}"
    log_info "ECAPA_USE_OPTIMIZED: ${ECAPA_USE_OPTIMIZED:-not set}"
    log_info "ECAPA_PREFERRED_STRATEGY: ${ECAPA_PREFERRED_STRATEGY:-auto}"
    log_info "=============================================="

    # ==========================================================================
    # Step 1: Verify pre-baked cache exists
    # ==========================================================================
    log_info "Step 1: Verifying pre-baked cache..."

    # Check HuggingFace cache exists
    HF_CACHE_PATH="${HF_HOME:-/opt/ecapa_cache/huggingface}"
    if [ -d "$HF_CACHE_PATH" ]; then
        log_success "HuggingFace cache found: $HF_CACHE_PATH"
    else
        log_warn "HuggingFace cache not found: $HF_CACHE_PATH"
    fi

    # Check source cache
    if [ -d "$SOURCE_CACHE" ]; then
        log_success "Source cache exists: $SOURCE_CACHE"
        ls -la "$SOURCE_CACHE" 2>/dev/null | head -10 || true
    else
        log_warn "Source cache not found: $SOURCE_CACHE"
    fi

    # Verify cache has required files
    if verify_cache "$SOURCE_CACHE"; then
        log_success "Base model files verified"
    else
        log_error "Base model files missing - service may fail to start"
    fi

    # ==========================================================================
    # Step 2: Check for optimized models (v20.0.0)
    # ==========================================================================
    log_info "Step 2: Checking for optimized models..."

    if check_optimized_models "$SOURCE_CACHE"; then
        log_success "Optimized models available for ultra-fast cold start"
        export ECAPA_USE_OPTIMIZED="true"
    else
        log_info "No optimized models - using standard loading"
        export ECAPA_USE_OPTIMIZED="false"
    fi

    # ==========================================================================
    # Step 3: Create writable temp directories
    # ==========================================================================
    log_info "Step 3: Creating temp directories..."

    create_writable_dir /tmp/torch_cache
    create_writable_dir /tmp/xdg_cache
    create_writable_dir /tmp/speechbrain_cache

    log_info "Environment configured:"
    log_info "  ECAPA_CACHE_DIR=${ECAPA_CACHE_DIR:-not set}"
    log_info "  HF_HOME=${HF_HOME:-not set}"
    log_info "  HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-not set}"
    log_info "  ECAPA_USE_OPTIMIZED=${ECAPA_USE_OPTIMIZED:-not set}"

    # ==========================================================================
    # Step 4: Pre-flight checks
    # ==========================================================================
    log_info "Step 4: Running pre-flight checks..."

    # Check Python
    if command -v python &> /dev/null; then
        PYTHON_VERSION=$(python --version 2>&1)
        log_success "Python: $PYTHON_VERSION"
    else
        log_error "Python not found!"
        exit 1
    fi

    # Check PyTorch
    TORCH_VERSION=$(python -c "import torch; print(f'{torch.__version__}')" 2>/dev/null || echo "NOT FOUND")
    log_info "  PyTorch: $TORCH_VERSION"

    # Check if main script exists
    if [ ! -f "ecapa_cloud_service.py" ]; then
        log_error "ecapa_cloud_service.py not found in $(pwd)"
        exit 1
    fi

    # ==========================================================================
    # Step 5: Start service
    # ==========================================================================
    log_info "=============================================="
    log_info "Starting ECAPA Cloud Service..."
    log_info "  Optimization: ${ECAPA_USE_OPTIMIZED:-auto}"
    log_info "  Strategy: ${ECAPA_PREFERRED_STRATEGY:-auto}"
    log_info "=============================================="

    # Start the service
    exec python ecapa_cloud_service.py
}

# =============================================================================
# RUN
# =============================================================================
main "$@"
