# ECAPA Cloud Service Dockerfile
# ===============================
#
# Production-ready container for ECAPA-TDNN speaker embedding service.
# Optimized for GCP Cloud Run with:
# - Multi-stage build for minimal image size
# - Pre-downloaded model weights
# - Health check integration
# - Non-root user for security
#
# Build: docker build -t ecapa-cloud-service .
# Run:   docker run -p 8010:8010 ecapa-cloud-service
#
# v18.0.0

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

# Set ownership
RUN chown -R ecapa:ecapa /app /opt/ecapa_cache

# Switch to non-root user
USER ecapa

# Environment variables
ENV ECAPA_MODEL_PATH="speechbrain/spkrec-ecapa-voxceleb"
ENV ECAPA_CACHE_DIR="/opt/ecapa_cache"
ENV ECAPA_DEVICE="cpu"
ENV ECAPA_WARMUP_ON_START="true"
ENV PORT="8010"

# Expose port
EXPOSE 8010

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

# Run the service
CMD ["python", "ecapa_cloud_service.py"]
