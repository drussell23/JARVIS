# =============================================================================
# JARVIS Cloud Memorystore (Redis) - Optional for Solo Developer
# =============================================================================
#
# ⚠️ COST WARNING: Cloud Memorystore costs ~$0.02/hour minimum (~$15/month)
#
# For development, consider using:
# - Local Redis via Docker: docker run -p 6379:6379 redis:alpine
# - In-memory caching in Python (already built into JARVIS)
#
# Only enable this when you need:
# - Shared caching across multiple VMs
# - Persistent cache that survives VM restarts
# - Production deployment

# =============================================================================
# VARIABLES
# =============================================================================

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region"
  type        = string
}

variable "network_id" {
  description = "VPC Network ID for private connection"
  type        = string
}

variable "memory_size_gb" {
  description = "Redis memory size in GB (minimum 1GB)"
  type        = number
  default     = 1
}

variable "tier" {
  description = "Redis tier: BASIC (cheapest) or STANDARD_HA (high availability)"
  type        = string
  default     = "BASIC"
}

# =============================================================================
# REDIS INSTANCE
# =============================================================================

resource "google_redis_instance" "cache" {
  name               = "jarvis-redis"
  memory_size_gb     = var.memory_size_gb
  tier               = var.tier
  region             = var.region
  project            = var.project_id
  
  authorized_network = var.network_id
  connect_mode       = "DIRECT_PEERING"

  redis_version      = "REDIS_7_0"
  display_name       = "JARVIS Intelligence Cache"

  # Maintenance window - early morning on Sunday to minimize disruption
  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 3
        minutes = 0
      }
    }
  }

  labels = {
    app         = "jarvis"
    environment = "development"
    managed-by  = "terraform"
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "redis_host" {
  description = "Redis instance IP address"
  value       = google_redis_instance.cache.host
}

output "redis_port" {
  description = "Redis port (usually 6379)"
  value       = google_redis_instance.cache.port
}

output "redis_connection_string" {
  description = "Redis connection string for JARVIS"
  value       = "redis://${google_redis_instance.cache.host}:${google_redis_instance.cache.port}"
}

output "monthly_cost_estimate" {
  description = "Estimated monthly cost"
  value       = "$${var.memory_size_gb * 15}/month (BASIC tier)"
}

