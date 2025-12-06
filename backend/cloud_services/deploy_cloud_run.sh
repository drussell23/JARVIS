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
# v18.2.0
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
MIN_INSTANCES="${CLOUD_RUN_MIN_INSTANCES:-0}"  # Scale to zero
MAX_INSTANCES="${CLOUD_RUN_MAX_INSTANCES:-3}"
CONCURRENCY="${CLOUD_RUN_CONCURRENCY:-10}"
TIMEOUT="${CLOUD_RUN_TIMEOUT:-300s}"

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
log "ECAPA Cloud Service Deployment - v18.2.0"
log "============================================================"
log "Project:    $GCP_PROJECT"
log "Region:     $GCP_REGION"
log "Service:    $SERVICE_NAME"
log "Memory:     $MEMORY"
log "CPU:        $CPU"
log "Instances:  $MIN_INSTANCES-$MAX_INSTANCES"
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
IMAGE_URI_TAGGED="${AR_LOCATION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${IMAGE_NAME}:$(date +%Y%m%d-%H%M%S)"

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
        log "Building image with Cloud Build..."
        run_cmd gcloud builds submit \
            --tag "$IMAGE_URI" \
            --timeout=1800s \
            .
    fi
else
    log "Skipping build (--skip-build specified)"
fi

# Deploy to Cloud Run
log "Deploying to Cloud Run..."
run_cmd gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE_URI" \
    --region "$GCP_REGION" \
    --platform managed \
    --memory "$MEMORY" \
    --cpu "$CPU" \
    --min-instances "$MIN_INSTANCES" \
    --max-instances "$MAX_INSTANCES" \
    --concurrency "$CONCURRENCY" \
    --timeout "$TIMEOUT" \
    --allow-unauthenticated \
    --set-env-vars "ECAPA_DEVICE=cpu,ECAPA_WARMUP_ON_START=true,ECAPA_CACHE_TTL=3600" \
    --port 8010

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
