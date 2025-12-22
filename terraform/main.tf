terraform {
  required_version = ">= 1.0.0"
  
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  
  backend "gcs" {
    bucket = "jarvis-473803-terraform-state"
    prefix = "prod"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# =============================================================================
# 💰 COST PROTECTION - Budget Alerts (FREE)
# =============================================================================
# ALWAYS enabled - protects you from unexpected charges.
# Set your billing_account_id to enable budget alerts.

module "budget" {
  source = "./modules/budget"
  
  project_id         = var.project_id
  billing_account_id = var.billing_account_id
  monthly_budget_usd = var.monthly_budget_usd
  alert_emails       = var.alert_emails
  alert_thresholds   = [0.25, 0.50, 0.75, 0.90, 1.0]  # Alert at 25%, 50%, 75%, 90%, 100%
}

# =============================================================================
# 🌐 CORE INFRASTRUCTURE (Always enabled - $0 cost)
# =============================================================================
# These resources are completely FREE in GCP.

module "network" {
  source     = "./modules/network"
  project_id = var.project_id
  region     = var.region
}

module "security" {
  source     = "./modules/security"
  project_id = var.project_id
}

module "monitoring" {
  source             = "./modules/monitoring"
  project_id         = var.project_id
  monthly_budget_usd = var.monthly_budget_usd
}

# =============================================================================
# 🖥️ SPOT VM COMPUTE (Enabled by default - $0 until VMs run)
# =============================================================================
# Creates an instance TEMPLATE only (FREE). Actual VMs are created dynamically
# by gcp_vm_manager.py and cost ~$0.01-0.03/hour when running.
#
# Triple-Lock Safety ensures VMs auto-terminate after max_runtime.

module "compute" {
  count = var.enable_spot_vm_template ? 1 : 0
  
  source       = "./modules/compute"
  project_id   = var.project_id
  region       = var.region
  zone         = var.zone
  network_id   = module.network.vpc_id
  subnet_id    = module.network.subnet_id
  
  # Cost-optimized settings for solo developer
  machine_type        = var.spot_vm_machine_type
  disk_size_gb        = var.spot_vm_disk_size_gb
  max_runtime_seconds = var.spot_vm_max_runtime_hours * 3600
  
  depends_on = [module.network]
}

# =============================================================================
# 📦 REDIS / CLOUD MEMORYSTORE (DISABLED by default - saves ~$15/month)
# =============================================================================
# ⚠️ COST WARNING: Minimum ~$15/month even when idle!
#
# Only enable when you need:
# - Shared caching across multiple VMs
# - Persistent cache that survives VM restarts
# - Production deployment
#
# For development, use:
# - Local Redis: docker run -p 6379:6379 redis:alpine
# - In-memory caching (built into JARVIS)

module "storage" {
  count = var.enable_redis ? 1 : 0
  
  source         = "./modules/storage"
  project_id     = var.project_id
  region         = var.region
  network_id     = module.network.vpc_id
  memory_size_gb = var.redis_memory_size_gb
  tier           = var.redis_tier
  
  depends_on = [module.network]
}

# =============================================================================
# 📊 COST ANALYSIS
# =============================================================================

locals {
  # Detailed cost breakdown
  cost_breakdown = {
    # Always-on resources
    network = {
      resource = "VPC + Firewall"
      cost     = "$0/month"
      notes    = "Completely free"
    }
    security = {
      resource = "Secret Manager"
      cost     = "$0/month"
      notes    = "Free tier: 10,000 operations/month"
    }
    monitoring = {
      resource = "Dashboards + Alerts"
      cost     = "$0/month"
      notes    = "Free tier: 100 alert policies"
    }
    budget = {
      resource = "Budget Alerts"
      cost     = "$0/month"
      notes    = "Always free"
    }
    
    # Optional resources
    spot_vm_template = {
      resource = "Instance Template"
      cost     = var.enable_spot_vm_template ? "$0/month (VMs: ~$0.01-0.03/hr when running)" : "Disabled"
      notes    = "Template is free, only pay for VM runtime"
    }
    redis = {
      resource = "Cloud Memorystore"
      cost     = var.enable_redis ? "~$${var.redis_memory_size_gb * 15}/month" : "Disabled ($0)"
      notes    = var.enable_redis ? "⚠️ This is the main cost driver" : "Using local/in-memory cache"
    }
  }
  
  # Total estimated monthly cost
  total_monthly_estimate = var.enable_redis ? var.redis_memory_size_gb * 15 : 0
  
  # Cost warnings
  cost_warnings = compact([
    var.enable_redis ? "⚠️ Redis enabled: ~$${var.redis_memory_size_gb * 15}/month" : "",
    var.spot_vm_max_runtime_hours > 3 ? "⚠️ VM max runtime > 3 hours increases orphan risk" : "",
    var.monthly_budget_usd > 20 ? "ℹ️ Budget set above $20/month" : "",
  ])
}

