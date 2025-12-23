# =============================================================================
# JARVIS-Prime Cloud Run Module
# =============================================================================
# Deploys JARVIS-Prime Tier-0 Brain to Google Cloud Run
#
# Features:
# - Serverless (pay per request, scales to zero)
# - Auto-scaling 0-3 instances
# - GPU support via Cloud Run (optional)
# - Artifact Registry for Docker images
# - Connects to existing VPC for Redis access
#
# Cost Estimate:
# - Cloud Run: ~$0 when idle, ~$0.02-0.05/hour when running
# - Artifact Registry: $0.10/GB/month for image storage
# - GPU (optional): ~$1-2/hour when running
#
# Usage:
#   module "jarvis_prime" {
#     source = "./modules/jarvis_prime"
#     ...
#   }
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
  description = "Minimum Cloud Run instances (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum Cloud Run instances"
  type        = number
  default     = 3
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

variable "enable_gpu" {
  description = "Enable GPU (requires Cloud Run GPU preview)"
  type        = bool
  default     = false
}

variable "model_gcs_path" {
  description = "GCS path to GGUF model (e.g., gs://bucket/models/model.gguf)"
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

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

# =============================================================================
# Artifact Registry (Docker Image Storage)
# =============================================================================

resource "google_artifact_registry_repository" "jarvis_prime" {
  location      = var.region
  repository_id = "jarvis-prime"
  description   = "JARVIS-Prime Docker images"
  format        = "DOCKER"

  labels = {
    app         = "jarvis-prime"
    environment = var.environment
    managed_by  = "terraform"
  }
}

# =============================================================================
# Cloud Run Service
# =============================================================================

resource "google_cloud_run_v2_service" "jarvis_prime" {
  name     = "jarvis-prime-${var.environment}"
  location = var.region

  template {
    # Scaling configuration
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    # Container configuration
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/jarvis-prime/jarvis-prime:${var.image_tag}"

      # Resource allocation
      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle = true  # Allow CPU throttling when idle (cost savings)
      }

      # Ports
      ports {
        container_port = 8000
      }

      # Environment variables
      env {
        name  = "JARVIS_PRIME_HOST"
        value = "0.0.0.0"
      }
      env {
        name  = "JARVIS_PRIME_PORT"
        value = "8000"
      }
      env {
        name  = "MODEL_PATH"
        value = "/app/models/current.gguf"
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "LOG_LEVEL"
        value = var.environment == "prod" ? "INFO" : "DEBUG"
      }

      # Redis configuration (if provided)
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

      # Model from GCS (if provided)
      dynamic "env" {
        for_each = var.model_gcs_path != "" ? [1] : []
        content {
          name  = "MODEL_GCS_PATH"
          value = var.model_gcs_path
        }
      }

      # Startup probe
      startup_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 30
        timeout_seconds       = 10
        period_seconds        = 10
        failure_threshold     = 10
      }

      # Liveness probe
      liveness_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 60
        timeout_seconds       = 5
        period_seconds        = 30
      }
    }

    # Request timeout
    timeout = "${var.timeout}s"

    # VPC connector for Redis access (if network provided)
    dynamic "vpc_access" {
      for_each = var.network_id != null ? [1] : []
      content {
        connector = google_vpc_access_connector.jarvis_prime[0].id
        egress    = "PRIVATE_RANGES_ONLY"
      }
    }

    # Service account
    service_account = google_service_account.jarvis_prime.email

    # Labels
    labels = {
      app         = "jarvis-prime"
      environment = var.environment
      managed_by  = "terraform"
    }
  }

  # Traffic configuration
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  labels = {
    app         = "jarvis-prime"
    environment = var.environment
    managed_by  = "terraform"
  }

  depends_on = [
    google_artifact_registry_repository.jarvis_prime,
    google_project_iam_member.jarvis_prime_artifact_reader,
  ]
}

# =============================================================================
# VPC Access Connector (for Redis connectivity)
# =============================================================================

resource "google_vpc_access_connector" "jarvis_prime" {
  count = var.network_id != null ? 1 : 0

  name          = "jarvis-prime-vpc"
  region        = var.region
  network       = var.network_id
  ip_cidr_range = "10.8.0.0/28"

  min_throughput = 200
  max_throughput = 300
}

# =============================================================================
# Service Account
# =============================================================================

resource "google_service_account" "jarvis_prime" {
  account_id   = "jarvis-prime-${var.environment}"
  display_name = "JARVIS-Prime Service Account"
  description  = "Service account for JARVIS-Prime Cloud Run"
}

# Allow Cloud Run to pull images from Artifact Registry
resource "google_project_iam_member" "jarvis_prime_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.jarvis_prime.email}"
}

# Allow access to GCS for model storage
resource "google_project_iam_member" "jarvis_prime_gcs_reader" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.jarvis_prime.email}"
}

# Allow Cloud Run to invoke itself (for internal calls)
resource "google_project_iam_member" "jarvis_prime_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.jarvis_prime.email}"
}

# =============================================================================
# IAM - Allow public access (for API)
# =============================================================================

resource "google_cloud_run_v2_service_iam_member" "public" {
  location = google_cloud_run_v2_service.jarvis_prime.location
  name     = google_cloud_run_v2_service.jarvis_prime.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# =============================================================================
# Outputs
# =============================================================================

output "service_url" {
  description = "JARVIS-Prime Cloud Run service URL"
  value       = google_cloud_run_v2_service.jarvis_prime.uri
}

output "service_name" {
  description = "Cloud Run service name"
  value       = google_cloud_run_v2_service.jarvis_prime.name
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository URL"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/jarvis-prime"
}

output "service_account_email" {
  description = "Service account email"
  value       = google_service_account.jarvis_prime.email
}

output "docker_push_command" {
  description = "Command to push Docker image"
  value       = "docker push ${var.region}-docker.pkg.dev/${var.project_id}/jarvis-prime/jarvis-prime:${var.image_tag}"
}
