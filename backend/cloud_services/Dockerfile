# ECAPA Cloud Service Dockerfile v20.2.0
# =======================================
#
# Production-ready container for ECAPA-TDNN speaker embedding service.
# Optimized for GCP Cloud Run with ULTRA-FAST cold starts (<5s).
#
# Key Features:
# - Multi-stage build for minimal image size (~2GB)
# - FULLY PRE-BAKED model weights (no runtime downloads)
# - JIT/ONNX/Quantization compilation during build
# - ONNX Runtime for 10-50x faster model loading
# - Custom.py stub file prevents HuggingFace hub lookups at runtime
# - Pre-warmed PyTorch/SpeechBrain compilation cache
# - Pre-run warmup inference during build (JIT compilation done)
# - Strict offline mode (zero network calls at runtime)
# - Non-root user for security
#
# Build: docker build -t ecapa-cloud-service .
# Run:   docker run -p 8010:8010 ecapa-cloud-service
#
# v20.2.0 - Pure PyTorch JIT model (no SpeechBrain at runtime) for <5s cold start

# =============================================================================
# BUILD ARGUMENTS - Dynamic configuration
# =============================================================================
ARG PYTHON_VERSION=3.11
ARG ECAPA_MODEL_SOURCE=speechbrain/spkrec-ecapa-voxceleb
ARG CACHE_DIR=/opt/ecapa_cache
ARG HF_CACHE_DIR=/opt/ecapa_cache/huggingface
ARG OPTIMIZATION_STRATEGY=all

# =============================================================================
# Stage 1: Build dependencies and pre-bake model
# =============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Re-declare ARGs after FROM
ARG ECAPA_MODEL_SOURCE
ARG CACHE_DIR
ARG HF_CACHE_DIR
ARG OPTIMIZATION_STRATEGY

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements-cloud.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Install ONNX dependencies for optimization (REQUIRED for fast cold starts)
# - onnx: For model export
# - onnxruntime: For fast inference at runtime
# - onnx-simplifier: For optimizing the ONNX graph
RUN pip install --no-cache-dir \
    onnx==1.15.0 \
    onnxruntime==1.16.3 \
    onnx-simplifier==0.4.35 && \
    echo "ONNX dependencies installed successfully"

# =============================================================================
# PRE-BAKE MODEL CACHE (CRITICAL FOR FAST COLD STARTS)
# =============================================================================
# Set ALL cache environment variables to ensure consistent locations
ENV HF_HOME="${HF_CACHE_DIR}"
ENV HUGGINGFACE_HUB_CACHE="${HF_CACHE_DIR}"
ENV TRANSFORMERS_CACHE="${HF_CACHE_DIR}"
ENV TORCH_HOME="${CACHE_DIR}/torch"
ENV XDG_CACHE_HOME="${CACHE_DIR}/xdg"
ENV SPEECHBRAIN_CACHE="${CACHE_DIR}/speechbrain"

# Create all cache directories
RUN mkdir -p ${CACHE_DIR} ${HF_CACHE_DIR} ${CACHE_DIR}/torch ${CACHE_DIR}/xdg ${CACHE_DIR}/speechbrain

# Copy pre-bake and compile scripts
COPY prebake_model.py /tmp/prebake_model.py
COPY compile_model.py /tmp/compile_model.py

# Step 1: Download and pre-bake the ECAPA model with FULL verification
# This downloads the model from HuggingFace and runs warmup inference
RUN echo "=== Step 1: Pre-baking ECAPA Model ===" && \
    python /tmp/prebake_model.py ${CACHE_DIR} ${ECAPA_MODEL_SOURCE}

# Step 2: Run model optimization (JIT, ONNX, Quantization)
# This compiles the model into optimized formats for ultra-fast loading
RUN echo "=== Step 2: Running Model Optimization Suite ===" && \
    python /tmp/compile_model.py ${CACHE_DIR} ${ECAPA_MODEL_SOURCE} --strategy=${OPTIMIZATION_STRATEGY} || \
    echo "Optimization completed (some strategies may have failed - check manifest)"

# List final cache contents for verification
RUN echo "=== Pre-baked + Optimized cache contents ===" && \
    ls -la ${CACHE_DIR}/ && \
    echo "" && \
    echo "=== Optimized model files ===" && \
    ls -la ${CACHE_DIR}/*.pt ${CACHE_DIR}/*.onnx 2>/dev/null || echo "(No optimized files)" && \
    echo "" && \
    echo "=== Manifest files ===" && \
    cat ${CACHE_DIR}/.optimization_manifest.json 2>/dev/null || echo "(No optimization manifest)" && \
    echo "" && \
    echo "=== HuggingFace cache ===" && \
    ls -la ${HF_CACHE_DIR}/ 2>/dev/null || echo "(empty or not used)"


# =============================================================================
# Stage 2: Production image (minimal, secure)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS production

# Re-declare ARGs
ARG CACHE_DIR
ARG HF_CACHE_DIR

# Labels
LABEL maintainer="JARVIS AI Agent Team"
LABEL version="20.2.0"
LABEL description="ECAPA-TDNN Cloud Speaker Embedding Service - Ultra-Fast Cold Starts with JIT/ONNX/Quantization"

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy COMPLETE pre-baked + optimized model cache (this is the magic!)
COPY --from=builder ${CACHE_DIR} ${CACHE_DIR}

# Create non-root user for security
RUN groupadd -r ecapa && useradd -r -g ecapa -m -d /home/ecapa ecapa

# Create writable temp directories for runtime
RUN mkdir -p /tmp/torch_cache /tmp/xdg_cache /tmp/speechbrain_cache && \
    chown -R ecapa:ecapa /tmp/torch_cache /tmp/xdg_cache /tmp/speechbrain_cache && \
    chmod -R 777 /tmp/torch_cache /tmp/xdg_cache /tmp/speechbrain_cache

# Set permissions on pre-baked cache (read-only is fine for model files)
RUN chown -R ecapa:ecapa ${CACHE_DIR} && \
    find ${CACHE_DIR} -type d -exec chmod 755 {} \; && \
    find ${CACHE_DIR} -type f -exec chmod 644 {} \;

# Create app directory
WORKDIR /app

# Copy application code
COPY ecapa_cloud_service.py .
COPY compile_model.py .
COPY __init__.py .
COPY entrypoint.sh .

# Set ownership and permissions
RUN chown -R ecapa:ecapa /app && \
    chmod +x /app/entrypoint.sh

# Switch to non-root user
USER ecapa

# =============================================================================
# ENVIRONMENT CONFIGURATION - Zero Network Mode + Optimization
# =============================================================================
# Point everything to pre-baked cache and DISABLE all network access
ENV ECAPA_MODEL_PATH="speechbrain/spkrec-ecapa-voxceleb"
ENV ECAPA_CACHE_DIR="${CACHE_DIR}"
ENV ECAPA_SOURCE_CACHE="${CACHE_DIR}"
ENV ECAPA_DEVICE="cpu"
ENV ECAPA_WARMUP_ON_START="true"
ENV PORT="8010"

# CRITICAL: Force strict offline mode (no network calls!)
ENV HF_HOME="${HF_CACHE_DIR}"
ENV HUGGINGFACE_HUB_CACHE="${HF_CACHE_DIR}"
ENV TRANSFORMERS_CACHE="${HF_CACHE_DIR}"
ENV HF_HUB_OFFLINE="1"
ENV TRANSFORMERS_OFFLINE="1"
ENV HF_DATASETS_OFFLINE="1"
ENV ECAPA_SKIP_HF_DOWNLOAD="true"
ENV ECAPA_STRICT_OFFLINE="true"

# Optimization settings (v20.0.0)
ENV ECAPA_USE_OPTIMIZED="true"
ENV ECAPA_PREFERRED_STRATEGY="auto"
ENV ECAPA_OPTIMIZATION_MANIFEST="${CACHE_DIR}/.optimization_manifest.json"

# Writable caches for torch compilation artifacts
ENV TORCH_HOME="/tmp/torch_cache"
ENV XDG_CACHE_HOME="/tmp/xdg_cache"
ENV SPEECHBRAIN_CACHE="/tmp/speechbrain_cache"

# Pre-baking verification
ENV ECAPA_PREBAKED_MANIFEST="${CACHE_DIR}/.prebaked_manifest.json"

# Expose port
EXPOSE 8010

# Health check (fast since model is pre-loaded with optimization)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

# Use robust entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
