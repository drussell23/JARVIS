# 🏗️ JARVIS Terraform Infrastructure

> **Cost-Optimized GCP Infrastructure for Solo Developers**

This Terraform configuration provides secure, cost-optimized infrastructure for JARVIS with a focus on **$0 fixed monthly costs** during development.

---

## 📊 Cost Summary

| Resource | Status | Monthly Cost |
|----------|--------|--------------|
| VPC/Network | ✅ Enabled | **$0** (free) |
| Secret Manager | ✅ Enabled | **$0** (free tier) |
| Monitoring Dashboard | ✅ Enabled | **$0** (free) |
| Budget Alerts | ✅ Enabled | **$0** (free) |
| VM Alert Policy | ✅ Enabled | **$0** (free) |
| Spot VM Template | ✅ Enabled | **$0** (template is free) |
| Spot VMs (when running) | Dynamic | ~$0.01-0.03/hr |
| Redis (Memorystore) | ❌ Disabled | **$0** (would be ~$15/mo) |
| **TOTAL FIXED COST** | | **$0/month** |

> 💡 With these defaults, you only pay for Spot VM time when VMs are actually running!

---

## 🛡️ Cost Protection Features

### 1. GCP Budget Alerts (Integrated!)
The Terraform creates **native GCP billing budget alerts** that:
- Alert at **25%, 50%, 75%, 90%, 100%** of your monthly budget
- Send **forecasted spend alerts** (warns BEFORE you exceed)
- Email notifications (if configured)
- Default budget: **$10/month**

```hcl
# These alerts are created directly in GCP Billing
module "budget" {
  monthly_budget_usd = 10  # Alerts at $2.50, $5, $7.50, $9, $10
}
```

### 2. VM Running Too Long Alert
Monitoring alert that triggers if any VM runs > 3 hours:
- Catches orphaned VMs the Triple-Lock missed
- Appears in GCP Console Monitoring

### 3. Triple-Lock Safety System
VMs automatically terminate through three mechanisms:
1. **Platform-Level**: GCP `max_run_duration` = 3 hours
2. **VM-Side**: Startup script self-destructs if JARVIS dies
3. **Local Cleanup**: `shutdown_hook.py` cleans up on exit

### 4. Hard Budget Enforcement (Python)
The `cost_tracker.py` blocks VM creation when daily budget exceeded.

---

## 🚀 Quick Start

### Prerequisites
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.0.0
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated
- GCP project with billing enabled

### Step 1: Configure Your Settings

```bash
cd terraform

# Copy the example configuration
cp terraform.tfvars.example terraform.tfvars

# Edit with your settings
nano terraform.tfvars  # or code terraform.tfvars
```

**Important settings to configure:**

```hcl
# Your GCP project
project_id = "jarvis-473803"

# REQUIRED for budget alerts - find with: gcloud billing accounts list
billing_account_id = "01ABCD-EFGH23-IJKL45"

# Optional: Email for budget notifications
alert_emails = ["your@email.com"]

# Monthly budget (default: $10)
monthly_budget_usd = 10
```

### Step 2: Initialize Terraform

```bash
terraform init
```

### Step 3: Review the Plan

```bash
terraform plan
```

This shows exactly what will be created. Review to ensure:
- `enable_redis = false` (saves ~$15/month)
- Budget alerts are configured
- No unexpected resources

### Step 4: Deploy

```bash
terraform apply
```

Type `yes` to confirm. Deployment takes ~2-5 minutes.

### Step 5: Verify

```bash
# Check outputs
terraform output

# Verify budget in GCP Console
# Go to: Billing → Budgets & alerts
```

---

## 📁 Module Structure

```
terraform/
├── main.tf                    # Main configuration
├── variables.tf               # Input variables
├── outputs.tf                 # Output values
├── terraform.tfvars.example   # Example configuration
└── modules/
    ├── budget/                # 💰 GCP Billing Budget Alerts
    │   └── main.tf
    ├── compute/               # 🖥️ Spot VM Instance Template
    │   └── spot_template.tf
    ├── monitoring/            # 📊 Dashboards & Alert Policies
    │   └── main.tf
    ├── network/               # 🌐 VPC, Subnets, Firewall
    │   └── main.tf
    ├── security/              # 🔐 Secret Manager Secrets
    │   └── main.tf
    └── storage/               # 📦 Redis/Memorystore (optional)
        └── main.tf
```

---

## ⚙️ Configuration Options

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `project_id` | `jarvis-473803` | Your GCP project ID |
| `region` | `us-central1` | GCP region |
| `zone` | `us-central1-a` | GCP zone |
| `developer_mode` | `true` | Enable cost-optimized settings |

### Budget & Alerts

| Variable | Default | Description |
|----------|---------|-------------|
| `billing_account_id` | `""` | **Required for budget alerts!** |
| `monthly_budget_usd` | `10` | Monthly budget for alerts |
| `alert_emails` | `[]` | Emails for notifications |

### Spot VMs

| Variable | Default | Description |
|----------|---------|-------------|
| `enable_spot_vm_template` | `true` | Create VM template (free) |
| `spot_vm_machine_type` | `e2-medium` | VM size (~$0.03/hr) |
| `spot_vm_disk_size_gb` | `20` | Boot disk size |
| `spot_vm_max_runtime_hours` | `3` | Triple-Lock max runtime |

### Redis (Disabled by Default)

| Variable | Default | Description |
|----------|---------|-------------|
| `enable_redis` | `false` | Enable Cloud Memorystore |
| `redis_memory_size_gb` | `1` | Redis memory size |
| `redis_tier` | `BASIC` | Redis tier |

> ⚠️ **Redis costs ~$15/month minimum!** Use local Redis for development:
> ```bash
> docker run -d -p 6379:6379 --name jarvis-redis redis:alpine
> ```

---

## 🔧 Common Commands

### Terraform Operations

```bash
# Initialize (first time or after adding modules)
terraform init

# Preview changes
terraform plan

# Apply changes
terraform apply

# Destroy all resources (careful!)
terraform destroy

# Show current state
terraform show

# Show outputs
terraform output
```

### Enable/Disable Resources

```bash
# Enable Redis (adds ~$15/month)
terraform apply -var="enable_redis=true"

# Disable Redis
terraform apply -var="enable_redis=false"

# Change VM size
terraform apply -var="spot_vm_machine_type=e2-highmem-4"
```

### Find Your Billing Account ID

```bash
gcloud billing accounts list
```

Output example:
```
ACCOUNT_ID            NAME                OPEN  MASTER_ACCOUNT_ID
01ABCD-EFGH23-IJKL45  My Billing Account  True
```

### Check Current GCP Costs

```bash
# List all JARVIS VMs
gcloud compute instances list --filter="labels.app=jarvis"

# Delete all JARVIS VMs (emergency cleanup)
gcloud compute instances delete \
  $(gcloud compute instances list --filter="labels.app=jarvis" --format="value(name)") \
  --zone=us-central1-a --quiet
```

---

## 🔄 Integration with JARVIS

### Automatic Integration

The Terraform outputs are used by JARVIS automatically:

1. **Spot VM Template**: `gcp_vm_manager.py` uses the template for creating VMs
2. **VPC/Subnet**: VMs are launched in the configured network
3. **Secret Manager**: API keys are stored securely

### Environment Variables

Set these in your environment to use Terraform-created resources:

```bash
# If Redis is enabled
export REDIS_HOST=$(terraform output -raw redis_host)
export REDIS_PORT=$(terraform output -raw redis_port)

# Spot VM template
export GCP_VM_TEMPLATE=$(terraform output -raw spot_vm_template_link)
```

### Cost Tracker Integration

The Python `cost_tracker.py` provides additional protection:
- Hard budget enforcement (blocks VM creation when over budget)
- Cost forecasting (warns before exceeding)
- Solo developer mode (stricter limits)

---

## ❓ FAQ

### Should I deploy this Terraform?

**Yes, if you want:**
- ✅ Budget alerts before you overspend (FREE)
- ✅ Monitoring dashboard for visibility (FREE)
- ✅ Spot VM template for memory offloading (FREE)
- ✅ Proper VPC/networking for security (FREE)

**All of the above are free!** The only paid resource (Redis) is disabled by default.

### Will Terraform cost me money?

**No!** With the default configuration:
- All enabled resources are **$0/month**
- Redis is disabled (would be ~$15/month)
- You only pay for Spot VM time when VMs run

### Is the budget integrated with GCP?

**Yes!** The `budget` module creates a `google_billing_budget` resource that:
- Integrates directly with GCP Billing
- Shows up in GCP Console → Billing → Budgets & alerts
- Sends native GCP alert notifications

### What if I don't have a billing account ID?

You can still deploy, but budget alerts won't be created:
```bash
terraform apply  # Works without billing_account_id, just no budget alerts
```

Budget alerts are highly recommended though - they're free and prevent surprise bills!

### What's the difference between GCP Redis and local Redis?

| Feature | Local Redis | GCP Cloud Memorystore |
|---------|-------------|----------------------|
| Cost | **Free** | ~$15/month |
| Setup | `docker run -p 6379:6379 redis:alpine` | Terraform creates it |
| Persistence | Lost on restart | Persists |
| Use case | Development | Production / Multi-VM |

**For solo development, use local Redis!** It's free and works exactly the same.

### How does Redis help with WebSockets?

**Without Redis (Polling):**
```
Client: "Any updates?" → Server: "No"
Client: "Any updates?" → Server: "No"
Client: "Any updates?" → Server: "Yes!" (finally!)
```
*Wasteful, delayed, high latency*

**With Redis Pub/Sub (Push):**
```
Event happens → Server publishes to Redis → ALL clients get instant update
```
*Efficient, instant, real-time*

### How is Redis integrated with cost_tracker.py?

The `cost_tracker.py` v3.0 includes full Redis integration:

```python
from backend.core.cost_tracker import get_cost_tracker

tracker = get_cost_tracker()

# Register WebSocket for real-time updates
async def ws_handler(message):
    await websocket.send_json(message)

tracker.register_websocket_subscriber(ws_handler)

# Events are automatically published to Redis:
# - jarvis:cost:updates    → Cost changes
# - jarvis:cost:vm_events  → VM created/deleted
# - jarvis:cost:alerts     → Budget alerts
# - jarvis:cost:budget     → Budget status changes
```

### How do I enable Redis later?

```bash
terraform apply -var="enable_redis=true"
```

This will:
- Create a Cloud Memorystore Redis instance
- Cost ~$15/month (1GB BASIC tier)
- Take 10-15 minutes to provision

---

## 🛠️ Troubleshooting

### "Error: Billing account not found"

Your billing account ID is incorrect. Find it with:
```bash
gcloud billing accounts list
```

### "Error: Required 'compute.networks.create' permission"

Your GCP credentials don't have permission. Ensure you have:
```bash
gcloud auth application-default login
```

### "Error: Backend configuration changed"

The GCS bucket for state doesn't exist:
```bash
# Create the bucket
gsutil mb -p jarvis-473803 gs://jarvis-473803-terraform-state
```

### VMs keep running after shutdown

Check the Triple-Lock system:
1. Verify `max_run_duration` in VM template
2. Check `gcp_vm_startup.sh` for self-destruct logic
3. Ensure `shutdown_hook.py` is registered

---

## 📚 Related Documentation

- [GCP VM Auto-Creation Flow](../GCP_VM_AUTO_CREATE_AND_SHUTDOWN_FLOW.md)
- [Cost Optimization Guide](../GCP_COST_OPTIMIZATION_IMPROVEMENTS.md)
- [Infrastructure Gap Analysis](../GCP_INFRASTRUCTURE_GAP_ANALYSIS.md)

---

## 🏷️ Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.0.0 | 2024-12 | Added budget module, cost protection, developer mode |
| 1.0.0 | 2024-12 | Initial Terraform configuration |

