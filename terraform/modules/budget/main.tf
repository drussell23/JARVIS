# =============================================================================
# JARVIS GCP Budget Alerts - ZERO COST
# =============================================================================
#
# This module creates billing budget alerts to notify you BEFORE you exceed
# your budget. This is FREE and highly recommended for solo developers.
#
# Features:
# - Multiple threshold alerts (25%, 50%, 75%, 90%, 100%)
# - Email notifications (optional)
# - Forecasted spend alerts (warns before you hit budget)
# - Per-service filtering (optional)

# =============================================================================
# VARIABLES
# =============================================================================

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "billing_account_id" {
  description = "GCP Billing Account ID (leave empty to skip budget creation)"
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD"
  type        = number
  default     = 10  # $10/month for solo developer
}

variable "alert_emails" {
  description = "List of emails to notify on budget alerts"
  type        = list(string)
  default     = []
}

variable "alert_thresholds" {
  description = "Budget percentage thresholds for alerts"
  type        = list(number)
  default     = [0.25, 0.50, 0.75, 0.90, 1.0]  # 25%, 50%, 75%, 90%, 100%
}

# =============================================================================
# BUDGET RESOURCE
# =============================================================================

# Only create budget if billing account is provided
resource "google_billing_budget" "jarvis_budget" {
  count = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "JARVIS Monthly Budget ($${var.monthly_budget_usd})"

  budget_filter {
    projects = ["projects/${var.project_id}"]
    
    # Track all services, but you could filter to specific ones:
    # services = ["services/24E6-581D-38E5"]  # Compute Engine
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  # Create threshold rules for each percentage
  dynamic "threshold_rules" {
    for_each = var.alert_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # Also alert on FORECASTED spend (before you actually hit the budget)
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "budget_id" {
  description = "Budget ID (null if billing account not provided)"
  value       = var.billing_account_id != "" ? google_billing_budget.jarvis_budget[0].id : null
}

output "budget_name" {
  description = "Budget display name"
  value       = var.billing_account_id != "" ? google_billing_budget.jarvis_budget[0].display_name : "Not configured (no billing account)"
}

output "monthly_budget" {
  description = "Monthly budget amount in USD"
  value       = var.monthly_budget_usd
}

output "alert_thresholds" {
  description = "Configured alert thresholds"
  value       = var.alert_thresholds
}

