# =============================================================================
# JARVIS Terraform Outputs - Cost-Optimized for Solo Developer
# =============================================================================

# =============================================================================
# 💰 COST SUMMARY (Most Important!)
# =============================================================================

output "cost_summary" {
  description = "💰 Estimated monthly costs - REVIEW THIS!"
  value = {
    fixed_monthly_cost = var.enable_redis ? "~$${var.redis_memory_size_gb * 15}/month (Redis)" : "$0/month (all free tier!)"
    variable_costs     = "Spot VMs: ~$0.01-0.03/hour when running"
    budget_alerts      = var.billing_account_id != "" ? "Enabled at $${var.monthly_budget_usd}/month" : "Not configured (add billing_account_id)"
    
    breakdown = {
      network          = "$0 (free)"
      security         = "$0 (free tier)"
      monitoring       = "$0 (free)"
      budget_alerts    = "$0 (free)"
      spot_vm_template = "$0 (template free)"
      redis            = var.enable_redis ? "~$${var.redis_memory_size_gb * 15}/mo" : "$0 (disabled)"
    }
    
    warnings = compact([
      var.enable_redis ? "⚠️ Redis is your main cost - consider disabling for dev" : "",
      var.billing_account_id == "" ? "⚠️ Budget alerts not configured - add billing_account_id" : "",
    ])
  }
}

output "developer_mode" {
  description = "Whether developer mode is enabled (cost-optimized settings)"
  value       = var.developer_mode
}

# =============================================================================
# 🔐 BUDGET PROTECTION
# =============================================================================

output "budget_status" {
  description = "Budget alert configuration"
  value = {
    configured         = var.billing_account_id != ""
    monthly_budget     = "$${var.monthly_budget_usd}"
    alert_thresholds   = ["25%", "50%", "75%", "90%", "100%"]
    forecasted_alerts  = true
  }
}

# =============================================================================
# 🌐 INFRASTRUCTURE IDs
# =============================================================================

output "vpc_id" {
  description = "VPC Network ID"
  value       = module.network.vpc_id
}

output "subnet_id" {
  description = "Subnet ID"
  value       = module.network.subnet_id
}

output "secret_manager_ids" {
  description = "Secret Manager secret IDs"
  value       = module.security.secret_ids
}

# =============================================================================
# 🖥️ SPOT VM CONFIGURATION
# =============================================================================

output "spot_vm_template_id" {
  description = "Spot VM instance template ID"
  value       = var.enable_spot_vm_template ? module.compute[0].template_id : null
}

output "spot_vm_template_link" {
  description = "Spot VM template self-link for gcp_vm_manager"
  value       = var.enable_spot_vm_template ? module.compute[0].template_self_link : null
}

output "spot_vm_config" {
  description = "Spot VM configuration"
  value = {
    enabled           = var.enable_spot_vm_template
    machine_type      = var.spot_vm_machine_type
    disk_size_gb      = var.spot_vm_disk_size_gb
    max_runtime_hours = var.spot_vm_max_runtime_hours
    cost_per_hour     = "~$0.01-0.03 (Spot pricing)"
  }
}

# =============================================================================
# 📦 REDIS CONFIGURATION
# =============================================================================

output "redis_host" {
  description = "Redis IP address (null if disabled)"
  value       = var.enable_redis ? module.storage[0].redis_host : null
}

output "redis_port" {
  description = "Redis port (null if disabled)"
  value       = var.enable_redis ? module.storage[0].redis_port : null
}

output "redis_connection_string" {
  description = "Redis connection URL (null if disabled)"
  value       = var.enable_redis ? module.storage[0].redis_connection_string : null
  sensitive   = true
}

output "redis_status" {
  description = "Redis configuration status"
  value = {
    enabled        = var.enable_redis
    memory_size_gb = var.enable_redis ? var.redis_memory_size_gb : 0
    tier           = var.enable_redis ? var.redis_tier : "N/A"
    monthly_cost   = var.enable_redis ? "~$${var.redis_memory_size_gb * 15}" : "$0"
    alternative    = var.enable_redis ? "" : "Use: docker run -p 6379:6379 redis:alpine"
  }
}

# =============================================================================
# 🛡️ TRIPLE-LOCK SAFETY STATUS
# =============================================================================

output "triple_lock_status" {
  description = "Triple-Lock VM safety configuration"
  value = {
    platform_level = {
      description = "GCP auto-terminates VMs after max_run_duration"
      max_hours   = var.spot_vm_max_runtime_hours
    }
    vm_side = {
      description = "VM self-destructs if JARVIS process dies"
      enabled     = true
    }
    local_cleanup = {
      description = "shutdown_hook.py cleans up on exit"
      enabled     = true
    }
    cost_protection = {
      description = "cost_tracker blocks VM creation when over budget"
      enabled     = true
    }
  }
}

# =============================================================================
# 🚀 QUICK START COMMANDS
# =============================================================================

output "quick_start" {
  description = "Helpful commands for managing infrastructure"
  value = {
    check_costs      = "gcloud billing accounts list"
    list_vms         = "gcloud compute instances list --filter='labels.app=jarvis'"
    delete_all_vms   = "gcloud compute instances delete $(gcloud compute instances list --filter='labels.app=jarvis' --format='value(name)') --zone=${var.zone} --quiet"
    local_redis      = "docker run -d -p 6379:6379 --name jarvis-redis redis:alpine"
    terraform_plan   = "terraform plan"
    terraform_apply  = "terraform apply"
    enable_redis     = "terraform apply -var='enable_redis=true'"
  }
}

