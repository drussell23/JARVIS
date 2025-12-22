variable "project_id" {
  description = "The GCP Project ID"
  type        = string
  default     = "jarvis-473803"
}

variable "region" {
  description = "Default GCP Region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Default GCP Zone"
  type        = string
  default     = "us-central1-a"
}

# =============================================================================
# DEVELOPER MODE - Cost Optimization for Solo Developers
# =============================================================================
# When true, disables expensive resources like Redis (Cloud Memorystore)
# that aren't needed during development/testing. This can save ~$15-20/month.
#
# Resources affected by developer_mode:
# - Redis (Cloud Memorystore): DISABLED - use local Redis or in-memory caching
# - Spot VM templates: ENABLED - still needed for memory offloading
# - VPC/Network: ENABLED - required for Spot VMs
# - Secret Manager: ENABLED - free tier covers development usage
# - Monitoring: ENABLED - dashboards are free

variable "developer_mode" {
  description = "Enable developer mode for cost savings (disables Redis, uses minimal resources)"
  type        = bool
  default     = true  # Default to true for solo developer
}

variable "enable_redis" {
  description = "Enable Cloud Memorystore Redis (costs ~$0.02/hour = $15/month minimum)"
  type        = bool
  default     = false  # Disabled by default, use local Redis during development
}

variable "enable_spot_vm_template" {
  description = "Create Spot VM instance template for dynamic scaling"
  type        = bool
  default     = true  # Enabled - needed for memory offloading
}

# Spot VM Configuration
variable "spot_vm_machine_type" {
  description = "Machine type for Spot VMs (e2-micro is cheapest, e2-highmem-4 for ML)"
  type        = string
  default     = "e2-medium"  # Good balance of cost/performance for development
}

variable "spot_vm_disk_size_gb" {
  description = "Boot disk size for Spot VMs in GB"
  type        = number
  default     = 20  # Reduced from 50GB to save on disk costs
}

variable "spot_vm_max_runtime_hours" {
  description = "Maximum runtime for Spot VMs before auto-termination (Triple-Lock safety)"
  type        = number
  default     = 3  # 3 hours max - aligned with cost_tracker
}

# Redis Configuration (if enabled)
variable "redis_memory_size_gb" {
  description = "Redis memory size in GB (minimum 1GB for Cloud Memorystore)"
  type        = number
  default     = 1  # Minimum size
}

variable "redis_tier" {
  description = "Redis tier (BASIC is cheapest, STANDARD_HA for production)"
  type        = string
  default     = "BASIC"  # Cheapest option
}

# =============================================================================
# BUDGET & COST PROTECTION
# =============================================================================
# These settings help prevent unexpected charges.

variable "billing_account_id" {
  description = "GCP Billing Account ID for budget alerts (find via: gcloud billing accounts list)"
  type        = string
  default     = ""  # Leave empty to skip budget creation (you can still use monitoring alerts)
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD - alerts trigger at 25%, 50%, 75%, 90%, 100%"
  type        = number
  default     = 10  # $10/month is plenty for solo development with Spot VMs
}

variable "alert_emails" {
  description = "Email addresses to notify on budget alerts"
  type        = list(string)
  default     = []  # Add your email: ["your@email.com"]
}

# =============================================================================
# COST SAFETY LIMITS
# =============================================================================

variable "max_concurrent_vms" {
  description = "Maximum number of Spot VMs that can run simultaneously"
  type        = number
  default     = 2  # Limit to 2 VMs max for cost safety
}

variable "max_daily_vm_hours" {
  description = "Maximum total VM hours per day (for cost calculations)"
  type        = number
  default     = 6  # 6 hours * $0.03/hr = $0.18/day max
}

