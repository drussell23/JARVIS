# =============================================================================
# JARVIS Backend Cloud Run Module v9.4
# =============================================================================
# Deploys the full JARVIS-AI-Agent backend to Google Cloud Run
#
# Features:
# - Serverless auto-scaling (0-10 instances)
# - Neural Mesh with 60+ agents
# - Data Flywheel integration
# - Intelligent Continuous Scraping
# - Multi-repo connectivity (JARVIS-Prime, Reactor-Core)
# - VPC access for Redis/ChromaDB
# - Artifact Registry for Docker images
#
# Cost Estimate:
# - Cloud Run: ~$0 when idle, ~$0.05-0.15/hour when running
# - Artifact Registry: $0.10/GB/month
# - Cloud SQL (optional): ~$10-30/month
#
# Usage:
#   module "jarvis_backend" {
#     source = "./modules/jarvis_backend"
#     ...
#   }
# =============================================================================

# =============================================================================
# Variables
# =============================================================================

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for Cloud Run"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "network_id" {
  description = "VPC Network ID for VPC connector"
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet ID for VPC connector"
  type        = string
  default     = null
}

variable "image_tag" {
  description = "Docker image tag to deploy"
  type        = string
  default     = "latest"
}

variable "min_instances" {
  description = "Minimum Cloud Run instances. Set to 1+ for latency-sensitive ML inference to eliminate cold starts."
  type        = number
  default     = 1
}

variable "max_instances" {
  description = "Maximum Cloud Run instances"
  type        = number
  default     = 5
}

variable "memory" {
  description = "Memory allocation (e.g., '4Gi', '8Gi')"
  type        = string
  default     = "4Gi"
}

variable "cpu" {
  description = "CPU allocation (e.g., '2', '4')"
  type        = string
  default     = "2"
}

variable "timeout" {
  description = "Request timeout in seconds"
  type        = number
  default     = 300
}

variable "concurrency" {
  description = "Maximum concurrent requests per instance"
  type        = number
  default     = 80
}

# Integration variables
variable "jarvis_prime_url" {
  description = "JARVIS-Prime Cloud Run URL for inference"
  type        = string
  default     = ""
}

variable "redis_host" {
  description = "Redis host for caching (optional)"
  type        = string
  default     = ""
}

variable "redis_port" {
  description = "Redis port"
  type        = number
  default     = 6379
}

variable "chromadb_host" {
  description = "ChromaDB host for embeddings (optional)"
  type        = string
  default     = ""
}

variable "gcs_bucket" {
  description = "GCS bucket for models and data"
  type        = string
  default     = ""
}

# Neural Mesh configuration
variable "neural_mesh_enabled" {
  description = "Enable Neural Mesh with 60+ agents"
  type        = bool
  default     = true
}

variable "neural_mesh_max_agents" {
  description = "Maximum number of Neural Mesh agents"
  type        = number
  default     = 60
}

# API Keys (via Secret Manager)
variable "anthropic_api_key_secret" {
  description = "Secret Manager secret ID for Anthropic API key"
  type        = string
  default     = "anthropic-api-key"
}

# =============================================================================
# Artifact Registry
# =============================================================================

resource "google_artifact_registry_repository" "jarvis_backend" {
  location      = var.region
  repository_id = "jarvis-backend"
  description   = "JARVIS Backend Docker images"
  format        = "DOCKER"

  labels = {
    app         = "jarvis-backend"
    environment = var.environment
    managed_by  = "terraform"
    created-by  = "jarvis" # Required for orphan detection by GCPReconciler
  }
}

# =============================================================================
# Service Account
# =============================================================================

resource "google_service_account" "jarvis_backend" {
  account_id   = "jarvis-backend-${var.environment}"
  display_name = "JARVIS Backend Service Account"
  description  = "Service account for JARVIS Backend Cloud Run"
}

# Artifact Registry access
resource "google_project_iam_member" "artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.jarvis_backend.email}"
}

# GCS access
resource "google_project_iam_member" "gcs_access" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.jarvis_backend.email}"
}

# Secret Manager access
resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.jarvis_backend.email}"
}

# Cloud Run invoker (for inter-service calls)
resource "google_project_iam_member" "run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.jarvis_backend.email}"
}

# =============================================================================
# Cloud Run Service
# =============================================================================

resource "google_cloud_run_v2_service" "jarvis_backend" {
  name     = "jarvis-backend-${var.environment}"
  location = var.region

  template {
    # Scaling
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    # Execution environment
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    max_instance_request_concurrency = var.concurrency

    # Container
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/jarvis-backend/jarvis-backend:${var.image_tag}"

      # Resources
      # cpu_idle=false = instance-based billing (always-on CPU).
      # Required for ML inference: model stays warm in memory between requests,
      # no CPU re-allocation delay on request arrival.
      # startup_cpu_boost=true = doubles vCPU during container startup,
      # cutting model load time by 30-50%.
      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      # Ports
      ports {
        container_port = 8010
      }

      # ===========================================
      # Environment Variables
      # ===========================================

      # Core
      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
      env {
        name  = "PYTHONPATH"
        value = "/app:/app/backend"
      }
      env {
        name  = "JARVIS_DOCKER"
        value = "true"
      }
      env {
        name  = "JARVIS_CLOUD_RUN"
        value = "true"
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      # Paths
      env {
        name  = "JARVIS_DATA_DIR"
        value = "/app/data"
      }
      env {
        name  = "JARVIS_MODELS_DIR"
        value = "/app/models"
      }

      # Neural Mesh
      env {
        name  = "NEURAL_MESH_ENABLED"
        value = tostring(var.neural_mesh_enabled)
      }
      env {
        name  = "NEURAL_MESH_PRODUCTION"
        value = "true"
      }
      env {
        name  = "NEURAL_MESH_MAX_AGENTS"
        value = tostring(var.neural_mesh_max_agents)
      }

      # Data Flywheel
      env {
        name  = "DATA_FLYWHEEL_ENABLED"
        value = "true"
      }
      env {
        name  = "CONTINUOUS_SCRAPING_ENABLED"
        value = "true"
      }

      # JARVIS-Prime integration
      dynamic "env" {
        for_each = var.jarvis_prime_url != "" ? [1] : []
        content {
          name  = "JARVIS_PRIME_CLOUD_RUN_URL"
          value = var.jarvis_prime_url
        }
      }

      # Redis
      dynamic "env" {
        for_each = var.redis_host != "" ? [1] : []
        content {
          name  = "REDIS_HOST"
          value = var.redis_host
        }
      }
      dynamic "env" {
        for_each = var.redis_host != "" ? [1] : []
        content {
          name  = "REDIS_PORT"
          value = tostring(var.redis_port)
        }
      }

      # ChromaDB
      dynamic "env" {
        for_each = var.chromadb_host != "" ? [1] : []
        content {
          name  = "CHROMADB_HOST"
          value = var.chromadb_host
        }
      }

      # GCS
      dynamic "env" {
        for_each = var.gcs_bucket != "" ? [1] : []
        content {
          name  = "GCS_BUCKET"
          value = var.gcs_bucket
        }
      }

      # API Keys from Secret Manager
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.anthropic_api_key_secret
            version = "latest"
          }
        }
      }

      # Startup probe — prevents traffic before ML model is loaded.
      # Budget: 5s delay + 20 failures × 3s period = 65s total startup budget.
      # Aligned with deploy_cloud_run.sh v20.5.0.
      startup_probe {
        http_get {
          path = "/health"
          port = 8010
        }
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 20
      }

      # Liveness probe — restarts deadlocked containers (corrupted model state,
      # PyTorch hang). 3 consecutive failures × 30s period = 90s to declare dead.
      liveness_probe {
        http_get {
          path = "/health"
          port = 8010
        }
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 30
        failure_threshold     = 3
      }
    }

    # Request timeout
    timeout = "${var.timeout}s"

    # VPC connector for Redis/ChromaDB access
    dynamic "vpc_access" {
      for_each = var.network_id != null ? [1] : []
      content {
        connector = google_vpc_access_connector.jarvis_backend[0].id
        egress    = "PRIVATE_RANGES_ONLY"
      }
    }

    # Service account
    service_account = google_service_account.jarvis_backend.email

    # Labels
    labels = {
      app         = "jarvis-backend"
      environment = var.environment
      managed_by  = "terraform"
      neural_mesh = var.neural_mesh_enabled ? "enabled" : "disabled"
      created-by  = "jarvis" # Required for orphan detection by GCPReconciler
    }
  }

  # Traffic
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  labels = {
    app         = "jarvis-backend"
    environment = var.environment
    managed_by  = "terraform"
    created-by  = "jarvis" # Required for orphan detection by GCPReconciler
  }

  depends_on = [
    google_artifact_registry_repository.jarvis_backend,
    google_project_iam_member.artifact_reader,
    google_project_iam_member.secret_accessor,
  ]
}

# =============================================================================
# VPC Access Connector
# =============================================================================

resource "google_vpc_access_connector" "jarvis_backend" {
  count = var.network_id != null ? 1 : 0

  name          = "jarvis-backend-vpc-${var.environment}"
  region        = var.region
  network       = var.network_id
  ip_cidr_range = "10.9.0.0/28"

  min_throughput = 200
  max_throughput = 400
}

# =============================================================================
# IAM - Public Access (for API)
# =============================================================================

resource "google_cloud_run_v2_service_iam_member" "public" {
  location = google_cloud_run_v2_service.jarvis_backend.location
  name     = google_cloud_run_v2_service.jarvis_backend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# =============================================================================
# Outputs
# =============================================================================

output "service_url" {
  description = "JARVIS Backend Cloud Run service URL"
  value       = google_cloud_run_v2_service.jarvis_backend.uri
}

output "service_name" {
  description = "Cloud Run service name"
  value       = google_cloud_run_v2_service.jarvis_backend.name
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository URL"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/jarvis-backend"
}

output "service_account_email" {
  description = "Service account email"
  value       = google_service_account.jarvis_backend.email
}

output "docker_push_command" {
  description = "Command to push Docker image"
  value       = "docker push ${var.region}-docker.pkg.dev/${var.project_id}/jarvis-backend/jarvis-backend:${var.image_tag}"
}

output "docker_build_command" {
  description = "Command to build Docker image"
  value       = "docker build -f docker/Dockerfile.backend -t ${var.region}-docker.pkg.dev/${var.project_id}/jarvis-backend/jarvis-backend:${var.image_tag} ."
}
