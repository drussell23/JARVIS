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
# v18.5.0 - HuggingFace Hub cache pre-baked + offline mode for fast cold starts

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
# Set HF_HOME to match runtime location - this is where SpeechBrain downloads model files
ENV HF_HOME="/opt/ecapa_cache/huggingface"
ENV HUGGINGFACE_HUB_CACHE="/opt/ecapa_cache/huggingface"
ENV TRANSFORMERS_CACHE="/opt/ecapa_cache/huggingface"

# Download and cache the ECAPA model - this populates both HF cache and savedir
RUN mkdir -p /opt/ecapa_cache/huggingface && \
    python -c "from speechbrain.inference.speaker import EncoderClassifier; \
    encoder = EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', \
    savedir='/opt/ecapa_cache'); \
    print('Model downloaded successfully'); \
    print('Files in /opt/ecapa_cache:'); \
    import os; [print(f'  {f}') for f in os.listdir('/opt/ecapa_cache') if not f.startswith('.')]"


# =============================================================================
# Stage 2: Production image
# =============================================================================
FROM python:3.11-slim-bookworm AS production

# Labels
LABEL maintainer="JARVIS AI Agent Team"
LABEL version="18.5.0"
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

# Create non-root user for security with home directory
RUN groupadd -r ecapa && useradd -r -g ecapa -m -d /home/ecapa ecapa

# Create all required directories and set proper permissions BEFORE switching user
# This ensures ecapa user can read/write to all necessary locations
RUN mkdir -p /home/ecapa/.cache/ecapa && \
    mkdir -p /tmp/ecapa_cache && \
    chown -R ecapa:ecapa /home/ecapa && \
    chown -R ecapa:ecapa /tmp/ecapa_cache && \
    chmod -R 755 /home/ecapa && \
    chmod -R 777 /tmp/ecapa_cache

# Set permissions on pre-baked cache - MUST be readable by ecapa user
# Use 755 for directories and 644 for files to ensure readability
RUN chown -R ecapa:ecapa /opt/ecapa_cache && \
    find /opt/ecapa_cache -type d -exec chmod 755 {} \; && \
    find /opt/ecapa_cache -type f -exec chmod 644 {} \;

# Create app directory
WORKDIR /app

# Copy application code
COPY ecapa_cloud_service.py .
COPY __init__.py .
COPY entrypoint.sh .

# Set ownership and permissions for app
RUN chown -R ecapa:ecapa /app && \
    chmod +x /app/entrypoint.sh

# Switch to non-root user
USER ecapa

# Environment variables
# CRITICAL: Point HuggingFace to the PRE-BAKED cache location at /opt/ecapa_cache
ENV ECAPA_MODEL_PATH="speechbrain/spkrec-ecapa-voxceleb"
ENV ECAPA_CACHE_DIR="/opt/ecapa_cache"
ENV ECAPA_SOURCE_CACHE="/opt/ecapa_cache"
ENV ECAPA_DEVICE="cpu"
ENV ECAPA_WARMUP_ON_START="true"
ENV PORT="8010"

# Point HuggingFace to pre-baked cache (CRITICAL for offline mode)
ENV HF_HOME="/opt/ecapa_cache/huggingface"
ENV HUGGINGFACE_HUB_CACHE="/opt/ecapa_cache/huggingface"
ENV TRANSFORMERS_CACHE="/opt/ecapa_cache/huggingface"
ENV HF_HUB_OFFLINE="1"
ENV TRANSFORMERS_OFFLINE="1"

# Use /tmp for writable caches (torch, etc.)
ENV TORCH_HOME="/tmp/torch_cache"
ENV XDG_CACHE_HOME="/tmp/xdg_cache"
ENV SPEECHBRAIN_CACHE="/tmp/speechbrain_cache"

# Expose port
EXPOSE 8010

# Health check (increased start period for model loading)
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

# Use robust entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
