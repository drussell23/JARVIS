# ECAPA Cloud Service Dockerfile
# ===============================
#
# Production-ready container for ECAPA-TDNN speaker embedding service.
# Optimized for GCP Cloud Run with:
# - Multi-stage build for minimal image size (~2GB)
# - Pre-downloaded model weights
# - Health check integration
# - Non-root user for security
# - Compatible with CloudECAPAClient v18.2.0
#
# Build: docker build -t ecapa-cloud-service .
# Run:   docker run -p 8010:8010 ecapa-cloud-service
#
# Local Development:
#   docker-compose up -d
#
# GCP Deployment:
#   ./deploy_cloud_run.sh
#
# v18.3.0 - Robust entrypoint script with fallback cache handling

# =============================================================================
# Stage 1: Build dependencies
# =============================================================================
FROM python:3.11-slim-bookworm AS builder

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

# Pre-download ECAPA model to cache (speeds up container startup)
RUN python -c "from speechbrain.inference.speaker import EncoderClassifier; \
    EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', \
    savedir='/opt/ecapa_cache')"


# =============================================================================
# Stage 2: Production image
# =============================================================================
FROM python:3.11-slim-bookworm AS production

# Labels
LABEL maintainer="JARVIS AI Agent Team"
LABEL version="18.0.0"
LABEL description="ECAPA-TDNN Cloud Speaker Embedding Service"

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

# Copy pre-downloaded model cache
COPY --from=builder /opt/ecapa_cache /opt/ecapa_cache

# Create non-root user for security
RUN groupadd -r ecapa && useradd -r -g ecapa ecapa

# Create app directory
WORKDIR /app

# Copy application code
COPY ecapa_cloud_service.py .
COPY __init__.py .
COPY entrypoint.sh .

# Set ownership and permissions
# Note: 775 ensures ecapa user/group can write; speechbrain needs write access for lock files
RUN chown -R ecapa:ecapa /app /opt/ecapa_cache && \
    chmod -R 775 /opt/ecapa_cache && \
    chmod +x /app/entrypoint.sh

# Switch to non-root user
USER ecapa

# Environment variables
# Use /tmp for runtime cache (writable), pre-downloaded cache is at /opt/ecapa_cache
ENV ECAPA_MODEL_PATH="speechbrain/spkrec-ecapa-voxceleb"
ENV ECAPA_CACHE_DIR="/tmp/ecapa_cache"
ENV ECAPA_SOURCE_CACHE="/opt/ecapa_cache"
ENV ECAPA_DEVICE="cpu"
ENV ECAPA_WARMUP_ON_START="true"
ENV PORT="8010"

# Additional cache environment variables for various libraries
ENV HF_HOME="/tmp/ecapa_cache/huggingface"
ENV TRANSFORMERS_CACHE="/tmp/ecapa_cache/transformers"
ENV TORCH_HOME="/tmp/ecapa_cache/torch"
ENV XDG_CACHE_HOME="/tmp/ecapa_cache"
ENV SPEECHBRAIN_CACHE="/tmp/ecapa_cache"

# Expose port
EXPOSE 8010

# Health check (increased start period for model loading)
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

# Use robust entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
