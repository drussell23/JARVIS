# =============================================================================
# jprime_gpu_golden_image.pkr.hcl
#
# HashiCorp Packer template to BAKE the `jarvis-prime-coder-32b` GPU golden image
# for the Sovereign Failover Mesh's QUALITY tier (Adaptive Workload Provisioner).
#
# WHY PRE-BAKED (not cloud-init): the failover node is a TEMPORARY survival tier.
# Installing NVIDIA drivers + CUDA + pulling a 32B model at boot costs 5-10 min of
# cold-boot — unacceptable. This image bakes the driver, CUDA, Ollama, AND the
# pre-pulled 32B weights so the quality node boots READY. The runtime cloud-init
# only forces the Ollama bind (failover_deadman.build_inference_bind_block) and
# the TTFT armor (prime_client) absorbs the model-load latency.
#
# The companion survival image (`jarvis-prime-coder`, 7B/CPU) is a separate, much
# simpler bake (no driver/CUDA) and is NOT in scope here.
#
# Build:
#   packer init  jprime_gpu_golden_image.pkr.hcl
#   packer build -var "project_id=jarvis-473803" jprime_gpu_golden_image.pkr.hcl
#
# Notes
#   * Everything is a `variable` — NO hardcoded project / zone / model / GPU.
#   * The BUILD instance carries a GPU so `ollama pull` warms + verifies on-device.
#   * Image family `jarvis-prime-coder-32b` is what failover_tier.py's
#     JARVIS_FAILOVER_QUALITY_IMAGE default resolves to — the provisioner POSTs
#     sourceImage=.../family/jarvis-prime-coder-32b.
# =============================================================================

packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = ">= 1.1.0"
    }
  }
}

# ----------------------------------------------------------------------------
# Variables — override at build time; defaults match failover_tier.py.
# ----------------------------------------------------------------------------
variable "project_id" {
  type        = string
  description = "GCP project to build + publish the image in."
}

variable "zone" {
  type        = string
  default     = "us-central1-b" # an L4-available zone
  description = "Build zone — MUST have the chosen accelerator available."
}

variable "image_family" {
  type        = string
  default     = "jarvis-prime-coder-32b"
  description = "Published family — must equal JARVIS_FAILOVER_QUALITY_IMAGE."
}

variable "source_image_family" {
  type        = string
  default     = "ubuntu-2204-lts"
  description = "Base OS family (GPU-driver compatible)."
}

variable "build_machine_type" {
  type        = string
  default     = "g2-standard-8"
  description = "Build VM — a GPU box so `ollama pull` warms on-device."
}

variable "accelerator_type" {
  type        = string
  default     = "nvidia-l4"
  description = "GPU for the BUILD instance (match the runtime quality tier)."
}

variable "accelerator_count" {
  type    = number
  default = 1
}

variable "model_label" {
  type        = string
  default     = "qwen2.5-coder:32b"
  description = "Ollama model to PRE-PULL into the image. Must equal JARVIS_FAILOVER_QUALITY_MODEL."
}

variable "cuda_keyring_deb" {
  type        = string
  default     = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb"
  description = "NVIDIA CUDA keyring (pin/override as needed)."
}

variable "disk_size_gb" {
  type        = number
  default     = 80 # 32B Q4 weights ~20GB + CUDA toolkit headroom
}

# ----------------------------------------------------------------------------
# Source — the GPU build instance.
# ----------------------------------------------------------------------------
source "googlecompute" "jprime_gpu" {
  project_id              = var.project_id
  zone                    = var.zone
  source_image_family     = var.source_image_family
  image_name              = "${var.image_family}-{{timestamp}}"
  image_family            = var.image_family
  image_description       = "JARVIS J-Prime QUALITY tier: ${var.model_label} on ${var.accelerator_type}, pre-baked driver+CUDA+Ollama."
  machine_type            = var.build_machine_type
  disk_size               = var.disk_size_gb
  ssh_username            = "packer"
  on_host_maintenance     = "TERMINATE" # GPUs cannot live-migrate — mirrors gcp_compute_rest._build_insert_payload

  accelerator_type  = "projects/${var.project_id}/zones/${var.zone}/acceleratorTypes/${var.accelerator_type}"
  accelerator_count = var.accelerator_count

  image_labels = {
    role  = "jprime-failover-quality"
    model = replace(replace(var.model_label, ":", "_"), ".", "-")
    tier  = "gpu-32b"
  }
}

# ----------------------------------------------------------------------------
# Build — driver → CUDA → Ollama → pre-pull model → systemd → verify.
# ----------------------------------------------------------------------------
build {
  sources = ["source.googlecompute.jprime_gpu"]

  # 1) NVIDIA driver + CUDA toolkit (pre-baked — never installed at boot).
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo apt-get update -y",
      "sudo apt-get install -y build-essential dkms curl jq linux-headers-$(uname -r)",
      "curl -fsSL ${var.cuda_keyring_deb} -o /tmp/cuda-keyring.deb",
      "sudo dpkg -i /tmp/cuda-keyring.deb",
      "sudo apt-get update -y",
      # cuda-drivers pulls the matched kernel driver + libs; headless server build.
      "sudo apt-get install -y cuda-drivers cuda-toolkit",
      "echo 'export PATH=/usr/local/cuda/bin:$PATH' | sudo tee /etc/profile.d/cuda.sh",
    ]
  }

  # 2) Ollama (GPU-aware runtime).
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "curl -fsSL https://ollama.com/install.sh | sudo sh",
      "sudo systemctl enable ollama",
    ]
  }

  # 3) PRE-PULL the 32B weights INTO the image (the whole point — no boot download).
  #    Start the daemon transiently, pull, verify it loads on the GPU, stop.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo systemctl start ollama",
      "for i in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags && break || sleep 2; done",
      "sudo -u ollama OLLAMA_HOST=127.0.0.1:11434 ollama pull ${var.model_label}",
      # On-device smoke: a 1-token generation proves driver+CUDA+model coherence.
      "curl -sf http://127.0.0.1:11434/api/generate -d '{\"model\":\"${var.model_label}\",\"prompt\":\"ok\",\"stream\":false,\"options\":{\"num_predict\":1}}' | jq -e '.response' >/dev/null",
      "nvidia-smi || (echo 'DRIVER VERIFY FAILED' && exit 1)",
      "sudo systemctl stop ollama",
    ]
  }

  # 4) Bake the systemd drop-in so the runtime cloud-init only needs to (re)bind.
  #    Mirrors failover_deadman.build_inference_bind_block — bind 0.0.0.0 so the
  #    Reachability Racer's external natIP probe can reach it.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo mkdir -p /etc/systemd/system/ollama.service.d",
      "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0:11434\"\\nEnvironment=\"OLLAMA_KEEP_ALIVE=-1\"\\n' | sudo tee /etc/systemd/system/ollama.service.d/10-jarvis-bind.conf",
      "sudo systemctl daemon-reload",
      # Clean cloud-init state so the published image boots fresh.
      "sudo cloud-init clean --logs || true",
    ]
  }
}
