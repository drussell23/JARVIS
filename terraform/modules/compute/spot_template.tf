# =============================================================================
# JARVIS Spot VM Instance Template - Cost-Optimized for Solo Developer
# =============================================================================
# 
# This creates an INSTANCE TEMPLATE (not actual VMs) with the Triple-Lock safety
# system built in. VMs are created dynamically by gcp_vm_manager.py.
#
# Cost savings:
# - Uses Spot VMs (60-91% cheaper than regular VMs)
# - Auto-terminates after max_runtime_seconds (prevents orphaned VMs)
# - Configurable machine type (use smaller during development)
# - Configurable disk size (smaller = cheaper)
#
# Triple-Lock Safety System:
# 1. Platform-Level: max_run_duration auto-terminates VMs
# 2. VM-Side: startup script self-destruct if backend dies
# 3. Local: shutdown_hook.py cleans up on JARVIS exit

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

variable "zone" {
  description = "GCP Zone"
  type        = string
}

variable "network_id" {
  description = "VPC Network ID"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID"
  type        = string
}

variable "machine_type" {
  description = "VM machine type (e2-micro, e2-medium, e2-highmem-4, etc.)"
  type        = string
  default     = "e2-medium"
}

variable "disk_size_gb" {
  description = "Boot disk size in GB"
  type        = number
  default     = 20
}

variable "max_runtime_seconds" {
  description = "Maximum runtime before auto-termination (Triple-Lock)"
  type        = number
  default     = 10800  # 3 hours
}

# =============================================================================
# INSTANCE TEMPLATE
# =============================================================================

resource "google_compute_instance_template" "spot_template" {
  name_prefix  = "jarvis-spot-"
  project      = var.project_id
  machine_type = var.machine_type
  region       = var.region

  # Spot provisioning with Triple-Lock safety
  scheduling {
    preemptible                 = true
    automatic_restart           = false
    provisioning_model          = "SPOT"
    instance_termination_action = "DELETE"
    
    # TRIPLE-LOCK #1: Platform-level auto-termination
    # GCP automatically deletes the VM after this duration
    max_run_duration {
      seconds = var.max_runtime_seconds
    }
  }

  disk {
    source_image = "ubuntu-os-cloud/ubuntu-2204-lts"
    auto_delete  = true
    boot         = true
    disk_size_gb = var.disk_size_gb
    disk_type    = "pd-standard"  # Cheapest disk type
  }

  network_interface {
    network    = var.network_id
    subnetwork = var.subnet_id
    access_config {
      # Ephemeral public IP for external access
      network_tier = "STANDARD"  # Cheaper than PREMIUM
    }
  }

  # TRIPLE-LOCK #2: VM-side self-destruct script
  # The startup script monitors the backend process and shuts down if it dies
  metadata = {
    # Try to load startup script, fall back to inline if file not found
    startup-script = <<-EOF
      #!/bin/bash
      # JARVIS Spot VM Startup Script with Self-Destruct
      
      echo "🚀 JARVIS Spot VM starting..."
      echo "   Machine Type: ${var.machine_type}"
      echo "   Max Runtime: ${var.max_runtime_seconds}s"
      
      # Log startup
      mkdir -p /var/log/jarvis
      echo "$(date): VM started" >> /var/log/jarvis/lifecycle.log
      
      # TRIPLE-LOCK #2: Self-destruct monitor
      # If no JARVIS process runs within 5 minutes, shut down
      (
        sleep 300  # Wait 5 mins for startup
        while true; do
          if ! pgrep -f "python.*main.py|jarvis" > /dev/null; then
            echo "$(date): No JARVIS process detected, initiating self-destruct" >> /var/log/jarvis/lifecycle.log
            sudo shutdown -h now
            exit 0
          fi
          sleep 60
        done
      ) &
      
      echo "✅ Self-destruct monitor activated"
    EOF
    
    shutdown-script = <<-EOF
      #!/bin/bash
      echo "$(date): VM shutting down" >> /var/log/jarvis/lifecycle.log
      echo "🛑 JARVIS Spot VM terminated"
    EOF
  }

  service_account {
    email  = "default"
    scopes = ["cloud-platform"]  # Full access for flexibility
  }

  labels = {
    app         = "jarvis"
    environment = "development"
    managed-by  = "terraform"
    type        = "spot-vm"
  }

  tags = ["jarvis-node", "spot-vm"]

  lifecycle {
    create_before_destroy = true
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "template_id" {
  description = "Instance template ID"
  value       = google_compute_instance_template.spot_template.id
}

output "template_self_link" {
  description = "Instance template self-link for gcp_vm_manager"
  value       = google_compute_instance_template.spot_template.self_link
}

output "machine_type" {
  description = "Configured machine type"
  value       = var.machine_type
}

output "max_runtime_hours" {
  description = "Maximum runtime in hours"
  value       = var.max_runtime_seconds / 3600
}

