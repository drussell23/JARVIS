#!/bin/bash
# =============================================================================
# ECAPA Cloud Service - GCP Cloud Run Deployment Script
# =============================================================================
#
# This script builds and deploys the ECAPA cloud service to GCP Cloud Run.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker installed (for local builds)
#   - GCP project with Cloud Run, Artifact Registry, and Cloud Build enabled
#
# Usage:
#   ./deploy_cloud_run.sh                    # Deploy with defaults
#   ./deploy_cloud_run.sh --region us-east1  # Deploy to specific region
#   ./deploy_cloud_run.sh --local-build      # Build locally, push to GCR
#   ./deploy_cloud_run.sh --dry-run          # Show commands without executing
#
# v20.5.0 - Enterprise-Grade Cloud Run Deployment
#   - min-instances=1 eliminates cold starts (the root cause of ECAPA probe timeouts)
#   - Startup CPU Boost doubles vCPU during model loading
#   - Gen2 execution environment for full Linux compat with native C extensions
#   - Instance-based billing (--no-cpu-throttling) keeps model warm between requests
#   - Session affinity routes sequential requests to warm instances
#   - Startup probe prevents traffic before ECAPA model is loaded
#   - Liveness probe restarts deadlocked containers
# =============================================================================

set -e

# =============================================================================
# CONFIGURATION (Override with environment variables)
# =============================================================================

GCP_PROJECT="${GCP_PROJECT_ID:-jarvis-473803}"
GCP_REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${ECAPA_SERVICE_NAME:-jarvis-ml}"
IMAGE_NAME="${ECAPA_IMAGE_NAME:-ecapa-cloud-service}"

# Cloud Run configuration
MEMORY="${CLOUD_RUN_MEMORY:-4Gi}"
CPU="${CLOUD_RUN_CPU:-2}"
# v20.5.0: min-instances=1 eliminates cold starts for latency-sensitive ML inference.
# Cloud Run cold start for ECAPA model = 10-30s. Client probe timeout = 8-12s.
# With min-instances=0, probe ALWAYS times out when container is cold.
# Cost: ~$145/month for 1 always-warm instance (2 vCPU, 4Gi).
# Override with CLOUD_RUN_MIN_INSTANCES=0 for dev/staging to save costs.
MIN_INSTANCES="${CLOUD_RUN_MIN_INSTANCES:-1}"
MAX_INSTANCES="${CLOUD_RUN_MAX_INSTANCES:-3}"
# v20.5.0: Concurrency reduced from 10 to 6 for CPU-bound ECAPA inference.
# 2 vCPU can realistically handle 2 concurrent inference + 4 headroom for health/IO.
CONCURRENCY="${CLOUD_RUN_CONCURRENCY:-6}"
TIMEOUT="${CLOUD_RUN_TIMEOUT:-300s}"
# v20.5.0: Instance-based billing keeps CPU always allocated. Prevents latency
# spike on first request after idle (CPU re-allocation delay). Required for ML
# inference where model must stay warm in memory.
CPU_THROTTLING="${CLOUD_RUN_CPU_THROTTLING:-false}"  # false = always-on CPU
# v20.5.0: Startup CPU Boost temporarily doubles vCPU during container startup.
# 2 vCPU → 4 vCPU for model loading. Cuts cold start by 30-50%.
CPU_BOOST="${CLOUD_RUN_CPU_BOOST:-true}"
# v20.5.0: Gen2 execution environment uses microVM instead of gVisor sandbox.
# Full Linux compat for PyTorch/SpeechBrain native C extensions (torch, torchaudio,
# soundfile, numpy BLAS). No system call emulation overhead = faster inference.
EXECUTION_ENV="${CLOUD_RUN_EXECUTION_ENV:-gen2}"
# v20.5.0: Session affinity routes sequential requests from same client to same
# instance. Warm caches (ECAPA embeddings, audio features) stay hot.
SESSION_AFFINITY="${CLOUD_RUN_SESSION_AFFINITY:-true}"

# Security / cost control
# If unauthenticated access is allowed, anyone who finds the URL can generate billable requests.
# Default to authenticated-only; opt-in to public access via env.
ALLOW_UNAUTHENTICATED="${CLOUD_RUN_ALLOW_UNAUTHENTICATED:-false}"

# Artifact Registry
AR_REPO="${AR_REPO:-jarvis-ml}"
AR_LOCATION="${AR_LOCATION:-us-central1}"

# Build mode
LOCAL_BUILD=false
DRY_RUN=false
SKIP_BUILD=false

# =============================================================================
# ARGUMENT PARSING
# =============================================================================

while [[ $# -gt 0 ]]; do
    case $1 in
        --project)
            GCP_PROJECT="$2"
            shift 2
            ;;
        --region)
            GCP_REGION="$2"
            shift 2
            ;;
        --service-name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --local-build)
            LOCAL_BUILD=true
            shift
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --project PROJECT      GCP project ID (default: $GCP_PROJECT)"
            echo "  --region REGION        GCP region (default: $GCP_REGION)"
            echo "  --service-name NAME    Cloud Run service name (default: $SERVICE_NAME)"
            echo "  --local-build          Build image locally instead of Cloud Build"
            echo "  --skip-build           Skip build, deploy existing image"
            echo "  --dry-run              Show commands without executing"
            echo "  --help                 Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

run_cmd() {
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] $*"
    else
        log "Running: $*"
        "$@"
    fi
}

check_prerequisites() {
    log "Checking prerequisites..."

    # Check gcloud
    if ! command -v gcloud &> /dev/null; then
        echo "ERROR: gcloud CLI not found. Install from https://cloud.google.com/sdk/docs/install"
        exit 1
    fi

    # Check authentication
    if ! gcloud auth list 2>/dev/null | grep -q "ACTIVE"; then
        echo "ERROR: Not authenticated with gcloud. Run: gcloud auth login"
        exit 1
    fi

    # Check project
    gcloud projects describe "$GCP_PROJECT" &> /dev/null || {
        echo "ERROR: Cannot access project $GCP_PROJECT"
        exit 1
    }

    log "✅ Prerequisites OK"
}

# =============================================================================
# MAIN DEPLOYMENT
# =============================================================================

cd "$(dirname "$0")"

log "============================================================"
log "ECAPA Cloud Service Deployment - v20.5.0"
log "============================================================"
log "Project:      $GCP_PROJECT"
log "Region:       $GCP_REGION"
log "Service:      $SERVICE_NAME"
log "Memory:       $MEMORY"
log "CPU:          $CPU"
log "Instances:    $MIN_INSTANCES-$MAX_INSTANCES"
log "Concurrency:  $CONCURRENCY"
log "CPU Boost:    $CPU_BOOST"
log "CPU Throttle: $CPU_THROTTLING"
log "Exec Env:     $EXECUTION_ENV"
log "Session Aff:  $SESSION_AFFINITY"
log "============================================================"

check_prerequisites

# Set project
run_cmd gcloud config set project "$GCP_PROJECT"

# Enable required APIs
log "Enabling required APIs..."
run_cmd gcloud services enable \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    containerregistry.googleapis.com

# Create Artifact Registry repo if it doesn't exist
log "Checking Artifact Registry repository..."
if ! gcloud artifacts repositories describe "$AR_REPO" --location="$AR_LOCATION" &> /dev/null; then
    log "Creating Artifact Registry repository: $AR_REPO"
    run_cmd gcloud artifacts repositories create "$AR_REPO" \
        --repository-format=docker \
        --location="$AR_LOCATION" \
        --description="JARVIS ML containers"
fi

# Build image
IMAGE_URI="${AR_LOCATION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${IMAGE_NAME}:latest"
IMAGE_URI_TAGGED="${AR_LOCATION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${IMAGE_NAME}:v20.5.0"

if [ "$SKIP_BUILD" = false ]; then
    if [ "$LOCAL_BUILD" = true ]; then
        log "Building image locally..."

        # Configure Docker for Artifact Registry
        run_cmd gcloud auth configure-docker "${AR_LOCATION}-docker.pkg.dev" --quiet

        # Build
        run_cmd docker build -t "$IMAGE_URI" -t "$IMAGE_URI_TAGGED" .

        # Push
        log "Pushing image to Artifact Registry..."
        run_cmd docker push "$IMAGE_URI"
        run_cmd docker push "$IMAGE_URI_TAGGED"
    else
        log "Building image with Cloud Build (JIT Optimization included)..."
        # v20.5.0: gcloud builds submit --tag only accepts a single URI.
        # Build with versioned tag, then add :latest alias.
        run_cmd gcloud builds submit \
            --tag "$IMAGE_URI_TAGGED" \
            --timeout=3600s \
            --machine-type=E2_HIGHCPU_8 \
            .
        # Tag as :latest for the deploy step
        log "Adding :latest tag..."
        run_cmd gcloud container images add-tag "$IMAGE_URI_TAGGED" "$IMAGE_URI" --quiet
    fi
else
    log "Skipping build (--skip-build specified)"
fi

# Deploy to Cloud Run
log "Deploying to Cloud Run..."
AUTH_FLAG=""
if [ "$ALLOW_UNAUTHENTICATED" = "true" ]; then
    AUTH_FLAG="--allow-unauthenticated"
fi
# v20.5.0: Build deploy flags dynamically based on configuration.
# This avoids hardcoded flag assumptions and respects env var overrides.
DEPLOY_FLAGS=(
    --image "$IMAGE_URI"
    --region "$GCP_REGION"
    --platform managed
    --memory "$MEMORY"
    --cpu "$CPU"
    --min-instances "$MIN_INSTANCES"
    --max-instances "$MAX_INSTANCES"
    --concurrency "$CONCURRENCY"
    --timeout "$TIMEOUT"
    --execution-environment "$EXECUTION_ENV"
    --port 8010
    --set-env-vars "ECAPA_DEVICE=cpu,ECAPA_WARMUP_ON_START=true,ECAPA_CACHE_TTL=3600,ECAPA_USE_OPTIMIZED=true"
)

# CPU throttling: false = instance-based billing (always-on CPU)
if [ "$CPU_THROTTLING" = "false" ]; then
    DEPLOY_FLAGS+=(--no-cpu-throttling)
else
    DEPLOY_FLAGS+=(--cpu-throttling)
fi

# Startup CPU Boost: doubles vCPU during container startup
if [ "$CPU_BOOST" = "true" ]; then
    DEPLOY_FLAGS+=(--cpu-boost)
else
    DEPLOY_FLAGS+=(--no-cpu-boost)
fi

# Session affinity: routes sequential requests to same warm instance
if [ "$SESSION_AFFINITY" = "true" ]; then
    DEPLOY_FLAGS+=(--session-affinity)
fi

# v20.5.0: Startup probe — prevents Cloud Run from sending traffic to instances
# that haven't finished loading the ECAPA model. Without this, Cloud Run considers
# the container ready as soon as the HTTP port opens (before model loads).
# Budget: initialDelay(5s) + failureThreshold(20) × period(3s) = 65s
DEPLOY_FLAGS+=(
    --startup-probe "httpGet.path=/health,httpGet.port=8010,initialDelaySeconds=5,periodSeconds=3,failureThreshold=20,timeoutSeconds=3"
)

# v20.5.0: Liveness probe — detects deadlocked containers (corrupted model state,
# PyTorch hang, etc.) and restarts them. 3 consecutive failures = restart.
DEPLOY_FLAGS+=(
    --liveness-probe "httpGet.path=/health,httpGet.port=8010,initialDelaySeconds=0,periodSeconds=30,failureThreshold=3,timeoutSeconds=5"
)

# Authentication flag
if [ -n "$AUTH_FLAG" ]; then
    DEPLOY_FLAGS+=($AUTH_FLAG)
fi

run_cmd gcloud run deploy "$SERVICE_NAME" "${DEPLOY_FLAGS[@]}"

# Get service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --region "$GCP_REGION" \
    --format 'value(status.url)')

log "============================================================"
log "✅ DEPLOYMENT COMPLETE"
log "============================================================"
log "Service URL: $SERVICE_URL"
log "Health:      $SERVICE_URL/health"
log "Status:      $SERVICE_URL/status"
log ""
log "Test with:"
log "  curl $SERVICE_URL/health"
log ""
log "Update .env.gcp with:"
log "  JARVIS_CLOUD_ML_ENDPOINT=${SERVICE_URL}/api/ml"
log "============================================================"

# Verify deployment
log "Verifying deployment..."
sleep 5

if curl -sf "$SERVICE_URL/health" > /dev/null; then
    log "✅ Health check passed!"
else
    log "⚠️  Health check pending (service may still be starting)"
    log "   Check logs: gcloud run logs read $SERVICE_NAME --region $GCP_REGION"
fi
