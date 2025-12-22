# =============================================================================
# JARVIS Monitoring & Cost Alerts - Zero Cost Resources
# =============================================================================
# All resources in this module are FREE:
# - Cloud Monitoring dashboards: FREE
# - Budget alerts: FREE
# - Alert policies: FREE (first 100 policies)

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD for alerts"
  type        = number
  default     = 10  # $10/month for solo developer
}

variable "alert_email" {
  description = "Email for budget alerts (optional)"
  type        = string
  default     = ""
}

# =============================================================================
# SYSTEM HEALTH DASHBOARD (FREE)
# =============================================================================

resource "google_monitoring_dashboard" "dashboard" {
  project        = var.project_id
  dashboard_json = <<EOF
{
  "displayName": "JARVIS System Health & Cost",
  "gridLayout": {
    "columns": "2",
    "widgets": [
      {
        "title": "💰 Estimated Compute Costs (Today)",
        "scorecard": {
          "timeSeriesQuery": {
            "timeSeriesFilter": {
              "filter": "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/uptime\"",
              "aggregation": {
                "perSeriesAligner": "ALIGN_SUM",
                "crossSeriesReducer": "REDUCE_SUM",
                "alignmentPeriod": "86400s"
              }
            }
          }
        }
      },
      {
        "title": "🖥️ Active VM Count",
        "scorecard": {
          "timeSeriesQuery": {
            "timeSeriesFilter": {
              "filter": "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/uptime\"",
              "aggregation": {
                "perSeriesAligner": "ALIGN_RATE",
                "crossSeriesReducer": "REDUCE_COUNT",
                "alignmentPeriod": "60s"
              }
            }
          },
          "thresholds": [
            {
              "value": 1,
              "color": "YELLOW",
              "direction": "ABOVE"
            },
            {
              "value": 3,
              "color": "RED",
              "direction": "ABOVE"
            }
          ]
        }
      },
      {
        "title": "⏱️ VM Uptime (hours today)",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/uptime\"",
                  "aggregation": {
                    "perSeriesAligner": "ALIGN_SUM",
                    "crossSeriesReducer": "REDUCE_SUM",
                    "alignmentPeriod": "3600s"
                  }
                }
              },
              "plotType": "STACKED_BAR"
            }
          ],
          "yAxis": {
            "label": "Uptime (seconds)",
            "scale": "LINEAR"
          }
        }
      },
      {
        "title": "🧠 VM CPU Utilization",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/cpu/utilization\"",
                  "aggregation": {
                    "perSeriesAligner": "ALIGN_MEAN",
                    "crossSeriesReducer": "REDUCE_MEAN",
                    "alignmentPeriod": "60s"
                  }
                }
              },
              "plotType": "LINE"
            }
          ]
        }
      },
      {
        "title": "📊 Redis Memory Usage",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "resource.type=\"redis_instance\" AND metric.type=\"redis.googleapis.com/stats/memory/usage\"",
                  "aggregation": {
                    "perSeriesAligner": "ALIGN_MEAN",
                    "crossSeriesReducer": "REDUCE_MEAN",
                    "alignmentPeriod": "60s"
                  }
                }
              },
              "plotType": "LINE"
            }
          ],
          "timeshiftDuration": "0s",
          "yAxis": {
            "label": "Memory (bytes)",
            "scale": "LINEAR"
          }
        }
      },
      {
        "title": "🌐 Network Egress",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/network/sent_bytes_count\"",
                  "aggregation": {
                    "perSeriesAligner": "ALIGN_RATE",
                    "crossSeriesReducer": "REDUCE_SUM",
                    "alignmentPeriod": "60s"
                  }
                }
              },
              "plotType": "LINE"
            }
          ]
        }
      }
    ]
  }
}
EOF
}

# =============================================================================
# COST ALERT POLICY (FREE) - Alerts when VMs run too long
# =============================================================================

resource "google_monitoring_alert_policy" "vm_running_too_long" {
  project      = var.project_id
  display_name = "JARVIS: VM Running > 3 Hours (Cost Alert)"
  combiner     = "OR"
  
  conditions {
    display_name = "VM uptime exceeds 3 hours"
    
    condition_threshold {
      filter          = "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/uptime\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 10800  # 3 hours in seconds
      
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  
  notification_channels = []  # Add notification channel ID if you want alerts
  
  documentation {
    content   = "A JARVIS Spot VM has been running for more than 3 hours. This may indicate the Triple-Lock safety system failed. Check and terminate orphaned VMs to save costs."
    mime_type = "text/markdown"
  }
  
  alert_strategy {
    auto_close = "1800s"  # Auto-close after 30 minutes
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "dashboard_id" {
  description = "Monitoring dashboard ID"
  value       = google_monitoring_dashboard.dashboard.id
}

output "cost_alert_policy_id" {
  description = "Cost alert policy ID"
  value       = google_monitoring_alert_policy.vm_running_too_long.id
}

