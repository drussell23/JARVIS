# =============================================================================
# JARVIS Security Module - Secret Manager (Data Sources for Existing Secrets)
# =============================================================================
# These secrets already exist in your GCP project.
# We use data sources to reference them, not create them.

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

# Reference existing secrets (don't create new ones)
data "google_secret_manager_secret" "anthropic_api_key" {
  project   = var.project_id
  secret_id = "anthropic-api-key"
}

data "google_secret_manager_secret" "jarvis_db_password" {
  project   = var.project_id
  secret_id = "jarvis-db-password"
}

data "google_secret_manager_secret" "picovoice_access_key" {
  project   = var.project_id
  secret_id = "picovoice-access-key"
}

data "google_secret_manager_secret" "openai_api_key" {
  project   = var.project_id
  secret_id = "openai-api-key"
}

data "google_secret_manager_secret" "elevenlabs_api_key" {
  project   = var.project_id
  secret_id = "elevenlabs-api-key"
}

# Output the secret references
output "secret_ids" {
  description = "Map of secret names to their full resource names"
  value = {
    "anthropic-api-key"   = data.google_secret_manager_secret.anthropic_api_key.name
    "jarvis-db-password"  = data.google_secret_manager_secret.jarvis_db_password.name
    "picovoice-access-key"= data.google_secret_manager_secret.picovoice_access_key.name
    "openai-api-key"      = data.google_secret_manager_secret.openai_api_key.name
    "elevenlabs-api-key"  = data.google_secret_manager_secret.elevenlabs_api_key.name
  }
}

