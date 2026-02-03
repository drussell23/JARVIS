#!/bin/bash
#
# JARVIS "Invincible Node" Deployment Script v1.0
# ================================================
#
# Deploys a persistent GCP Spot VM with STOP termination action.
# This creates an "Invincible Node" that survives preemption in STOPPED state
# and can be quickly restarted (~30s) instead of recreated (~3-5 min).
#
# ARCHITECTURE:
# - Single Static IP (Regional) - Never changes
# - Persistent Spot VM with --instance-termination-action=STOP
# - Idempotent: Safe to run multiple times
#
# STATE MACHINE:
# - RUNNING/STOPPED: Skip creation, node exists
# - NOT_FOUND: Create new VM with static IP
#
# USAGE:
#   ./deploy_spot_node.sh                    # Deploy with defaults
#   ./deploy_spot_node.sh --force-recreate   # Delete and recreate
#   ./deploy_spot_node.sh --status           # Check current status
#
# ENVIRONMENT VARIABLES (all optional, with sensible defaults):
#   GCP_PROJECT_ID         - GCP project (auto-detected if not set)
#   GCP_REGION             - Region for static IP (default: us-central1)
#   GCP_ZONE               - Zone for VM (default: us-central1-a)
#   GCP_VM_MACHINE_TYPE    - Machine type (default: e2-highmem-4 = 32GB RAM)
#   GCP_VM_INSTANCE_NAME   - Instance name (default: jarvis-prime-node)
#   GCP_VM_STATIC_IP_NAME  - Static IP name (default: jarvis-prime-static)
#   JARVIS_PRIME_PORT      - Port for health endpoint (default: 8000)
#
# REQUIREMENTS:
# - gcloud CLI authenticated with appropriate permissions
# - compute.instances.*, compute.addresses.* IAM permissions
#

set -euo pipefail

# Suppress gcloud Python deprecation warnings
export CLOUDSDK_PYTHON_SITEPACKAGES=1

# ============================================================================
# CONFIGURATION (All from environment with sensible defaults)
# ============================================================================

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# GCP Configuration
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
REGION="${GCP_REGION:-us-central1}"
ZONE="${GCP_ZONE:-us-central1-a}"

# VM Configuration
MACHINE_TYPE="${GCP_VM_MACHINE_TYPE:-e2-highmem-4}"  # 4 vCPU, 32GB RAM
INSTANCE_NAME="${GCP_VM_INSTANCE_NAME:-jarvis-prime-node}"
STATIC_IP_NAME="${GCP_VM_STATIC_IP_NAME:-jarvis-prime-static}"
PRIME_PORT="${JARVIS_PRIME_PORT:-8000}"

# Image Configuration
IMAGE_PROJECT="${GCP_IMAGE_PROJECT:-ubuntu-os-cloud}"
IMAGE_FAMILY="${GCP_IMAGE_FAMILY:-ubuntu-2204-lts}"
BOOT_DISK_SIZE="${GCP_BOOT_DISK_SIZE_GB:-50}"
BOOT_DISK_TYPE="${GCP_BOOT_DISK_TYPE:-pd-ssd}"

# Startup script path (relative to this script's directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STARTUP_SCRIPT="${GCP_STARTUP_SCRIPT_PATH:-${SCRIPT_DIR}/backend/core/gcp_vm_startup.sh}"

# Network tags for firewall rules
NETWORK_TAGS="jarvis-node,http-server,https-server"

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════════════${NC}"
}

# Check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Get instance status (RUNNING, STOPPED, TERMINATED, or NOT_FOUND)
get_instance_status() {
    local status
    status=$(gcloud compute instances describe "$INSTANCE_NAME" \
        --project="$PROJECT_ID" \
        --zone="$ZONE" \
        --format="value(status)" 2>/dev/null || echo "NOT_FOUND")
    echo "$status"
}

# Get static IP address
get_static_ip_address() {
    local result
    result=$(gcloud compute addresses describe "$STATIC_IP_NAME" \
        --project="$PROJECT_ID" \
        --region="$REGION" \
        --format="value(address)" 2>/dev/null) || true
    # Clean up any whitespace/newlines
    echo "$result" | tr -d '[:space:]' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || echo ""
}

# Check if static IP exists
static_ip_exists() {
    gcloud compute addresses describe "$STATIC_IP_NAME" \
        --project="$PROJECT_ID" \
        --region="$REGION" \
        --format="value(name)" >/dev/null 2>&1
}

# ============================================================================
# PRE-FLIGHT CHECKS
# ============================================================================

preflight_checks() {
    log_header "Pre-flight Checks"

    # Check gcloud CLI
    if ! command_exists gcloud; then
        log_error "gcloud CLI not found. Install from: https://cloud.google.com/sdk/docs/install"
        exit 1
    fi
    log_success "gcloud CLI found"

    # Check project ID
    if [ -z "$PROJECT_ID" ]; then
        log_error "GCP_PROJECT_ID not set and could not auto-detect."
        log_error "Set it with: export GCP_PROJECT_ID=your-project-id"
        exit 1
    fi
    log_success "Project ID: $PROJECT_ID"

    # Check authentication
    if ! gcloud auth list --filter="status:ACTIVE" --format="value(account)" | head -1 >/dev/null 2>&1; then
        log_error "Not authenticated to GCP. Run: gcloud auth login"
        exit 1
    fi
    log_success "GCP authentication valid"

    # Check startup script exists
    if [ ! -f "$STARTUP_SCRIPT" ]; then
        log_warn "Startup script not found at: $STARTUP_SCRIPT"
        log_warn "VM will start without custom initialization"
    else
        log_success "Startup script found: $STARTUP_SCRIPT"
    fi

    # Display configuration
    log_info "Configuration:"
    log_info "  Region: $REGION"
    log_info "  Zone: $ZONE"
    log_info "  Machine Type: $MACHINE_TYPE"
    log_info "  Instance Name: $INSTANCE_NAME"
    log_info "  Static IP Name: $STATIC_IP_NAME"
    log_info "  Port: $PRIME_PORT"
}

# ============================================================================
# STATIC IP MANAGEMENT
# ============================================================================

ensure_static_ip() {
    log_header "Static IP Management" >&2

    if static_ip_exists; then
        local ip_address
        ip_address=$(get_static_ip_address)
        log_success "Static IP exists: $STATIC_IP_NAME ($ip_address)" >&2
        echo "$ip_address"
        return 0
    fi

    log_info "Reserving regional static IP: $STATIC_IP_NAME" >&2

    if ! gcloud compute addresses create "$STATIC_IP_NAME" \
        --project="$PROJECT_ID" \
        --region="$REGION" \
        --description="JARVIS Prime Invincible Node Static IP"; then
        log_error "Failed to reserve static IP" >&2
        exit 1
    fi

    local ip_address
    ip_address=$(get_static_ip_address)
    log_success "Static IP reserved: $ip_address" >&2
    echo "$ip_address"
}

# ============================================================================
# FIREWALL RULE MANAGEMENT
# ============================================================================

ensure_firewall_rule() {
    log_header "Firewall Configuration"

    local rule_name="allow-jarvis-prime-${PRIME_PORT}"

    # Check if rule exists
    if gcloud compute firewall-rules describe "$rule_name" \
        --project="$PROJECT_ID" \
        --format="value(name)" >/dev/null 2>&1; then
        log_success "Firewall rule exists: $rule_name"
        return 0
    fi

    log_info "Creating firewall rule: $rule_name"

    if ! gcloud compute firewall-rules create "$rule_name" \
        --project="$PROJECT_ID" \
        --allow="tcp:${PRIME_PORT}" \
        --target-tags="jarvis-node" \
        --source-ranges="0.0.0.0/0" \
        --description="Allow JARVIS Prime health checks and inference on port ${PRIME_PORT}"; then
        log_error "Failed to create firewall rule"
        exit 1
    fi

    log_success "Firewall rule created"
}

# ============================================================================
# VM CREATION
# ============================================================================

create_vm() {
    local static_ip="$1"

    log_header "Creating Invincible Node"

    log_info "Instance: $INSTANCE_NAME"
    log_info "Machine Type: $MACHINE_TYPE (32GB RAM)"
    log_info "Static IP: $static_ip"
    log_info "Termination Action: STOP (Invincible)"

    # Build the gcloud command
    local cmd=(
        gcloud compute instances create "$INSTANCE_NAME"
        --project="$PROJECT_ID"
        --zone="$ZONE"
        --machine-type="$MACHINE_TYPE"
        --image-project="$IMAGE_PROJECT"
        --image-family="$IMAGE_FAMILY"
        --boot-disk-size="${BOOT_DISK_SIZE}GB"
        --boot-disk-type="$BOOT_DISK_TYPE"
        --network-interface="address=${static_ip},network-tier=PREMIUM"
        --tags="$NETWORK_TAGS"
        --scopes="cloud-platform"
        --provisioning-model="SPOT"
        --instance-termination-action="STOP"
        --maintenance-policy="TERMINATE"
        --no-restart-on-failure
        --labels="created-by=jarvis,type=prime-node,vm-class=invincible"
        --metadata="jarvis-port=${PRIME_PORT}"
    )

    # Add startup script if it exists
    if [ -f "$STARTUP_SCRIPT" ]; then
        cmd+=(--metadata-from-file="startup-script=${STARTUP_SCRIPT}")
    fi

    log_info "Executing VM creation..."

    if ! "${cmd[@]}"; then
        log_error "Failed to create VM"
        exit 1
    fi

    log_success "VM created successfully!"
    log_info ""
    log_info "  Instance: $INSTANCE_NAME"
    log_info "  Static IP: $static_ip"
    log_info "  Health endpoint: http://${static_ip}:${PRIME_PORT}/health"
    log_info ""
    log_info "  The VM will be available for health checks within 30-60 seconds."
    log_info "  Full inference capabilities after ~2-3 minutes (model loading)."
}

# ============================================================================
# STATUS COMMAND
# ============================================================================

show_status() {
    log_header "Invincible Node Status"

    local status
    status=$(get_instance_status)

    log_info "Instance: $INSTANCE_NAME"
    log_info "Status: $status"

    if [ "$status" != "NOT_FOUND" ]; then
        # Get more details
        local details
        details=$(gcloud compute instances describe "$INSTANCE_NAME" \
            --project="$PROJECT_ID" \
            --zone="$ZONE" \
            --format="yaml(name,status,machineType,scheduling.provisioningModel,scheduling.instanceTerminationAction,networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null || echo "")

        if [ -n "$details" ]; then
            echo ""
            echo "$details"
        fi
    fi

    # Static IP status
    echo ""
    if static_ip_exists; then
        local ip_address
        ip_address=$(get_static_ip_address)
        log_success "Static IP: $ip_address ($STATIC_IP_NAME)"

        # Try health check if RUNNING
        if [ "$status" = "RUNNING" ]; then
            echo ""
            log_info "Checking health endpoint..."
            if curl -s --connect-timeout 5 "http://${ip_address}:${PRIME_PORT}/health" 2>/dev/null | head -c 500; then
                echo ""
            else
                log_warn "Health endpoint not responding (VM may still be starting)"
            fi
        fi
    else
        log_warn "No static IP reserved"
    fi
}

# ============================================================================
# MAIN LOGIC
# ============================================================================

main() {
    # Parse arguments
    local force_recreate=false
    local show_status_only=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --force-recreate)
                force_recreate=true
                shift
                ;;
            --status)
                show_status_only=true
                shift
                ;;
            --help|-h)
                echo "JARVIS Invincible Node Deployment Script"
                echo ""
                echo "Usage: $0 [options]"
                echo ""
                echo "Options:"
                echo "  --status           Show current node status"
                echo "  --force-recreate   Delete and recreate the node"
                echo "  --help, -h         Show this help message"
                echo ""
                echo "Environment Variables:"
                echo "  GCP_PROJECT_ID         GCP project ID"
                echo "  GCP_REGION             Region (default: us-central1)"
                echo "  GCP_ZONE               Zone (default: us-central1-a)"
                echo "  GCP_VM_MACHINE_TYPE    Machine type (default: e2-highmem-4)"
                echo "  GCP_VM_INSTANCE_NAME   Instance name (default: jarvis-prime-node)"
                echo "  GCP_VM_STATIC_IP_NAME  Static IP name (default: jarvis-prime-static)"
                echo "  JARVIS_PRIME_PORT      Port (default: 8000)"
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done

    # Status only mode
    if [ "$show_status_only" = true ]; then
        preflight_checks
        show_status
        exit 0
    fi

    # Normal deployment flow
    preflight_checks

    # Get or create static IP
    local static_ip
    static_ip=$(ensure_static_ip)

    if [ -z "$static_ip" ]; then
        log_error "Failed to get or create static IP"
        exit 1
    fi

    # Ensure firewall rule exists
    ensure_firewall_rule

    # Check current instance status
    log_header "Instance Status Check"
    local status
    status=$(get_instance_status)
    log_info "Current status: $status"

    case "$status" in
        RUNNING)
            if [ "$force_recreate" = true ]; then
                log_warn "Force recreate requested - deleting existing VM"
                gcloud compute instances delete "$INSTANCE_NAME" \
                    --project="$PROJECT_ID" \
                    --zone="$ZONE" \
                    --quiet
                create_vm "$static_ip"
            else
                log_success "Node already RUNNING - no action needed"
                log_info "Health endpoint: http://${static_ip}:${PRIME_PORT}/health"
                log_info ""
                log_info "To force recreate, use: $0 --force-recreate"
            fi
            ;;
        STOPPED|TERMINATED|SUSPENDED)
            log_success "Node exists in $status state - no creation needed"
            log_info "The node will be started on-demand by the supervisor."
            log_info ""
            log_info "To start manually: gcloud compute instances start $INSTANCE_NAME --zone=$ZONE"
            ;;
        STAGING|PROVISIONING)
            log_info "Node is being created/started - wait for completion"
            ;;
        NOT_FOUND)
            create_vm "$static_ip"
            ;;
        *)
            log_warn "Unknown status: $status"
            log_info "Attempting to create VM..."
            create_vm "$static_ip"
            ;;
    esac

    log_header "Deployment Complete"
    log_success "Invincible Node is ready!"
    log_info ""
    log_info "Static IP: $static_ip"
    log_info "Health: http://${static_ip}:${PRIME_PORT}/health"
    log_info ""
    log_info "Next Steps:"
    log_info "  1. The unified_supervisor.py will auto-wake this node on startup"
    log_info "  2. Node survives preemption in STOPPED state (~30s restart)"
    log_info "  3. Monitor with: $0 --status"
}

# Run main
main "$@"
