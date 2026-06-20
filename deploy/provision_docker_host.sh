#!/usr/bin/env bash
# ============================================================================
# Sovereign Cloud Orchestrator — raw-Linux-host Docker provisioning (2026-06-19)
# ============================================================================
# Idempotent: installs Docker Engine + the compose plugin on a bare Ubuntu/
# Debian (apt) or RHEL/Fedora (dnf) host and enables dockerd on boot (so the
# `restart: always` prod container survives reboots). Safe to re-run.
#
#   curl -fsSL <raw>/deploy/provision_docker_host.sh | sudo bash
# or, after clone:
#   sudo bash deploy/provision_docker_host.sh
#
# Does NOT touch secrets, the repo, or the container — pure host prep.
# ============================================================================
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: this provisioner targets a Linux host (got $(uname -s))." >&2
  exit 2
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "ERROR: run as root (sudo) — installs system packages + enables docker." >&2
  exit 2
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "✅ docker + compose plugin already present — nothing to do."
  docker --version; docker compose version
  systemctl enable --now docker 2>/dev/null || true
  exit 0
fi

if command -v apt-get >/dev/null 2>&1; then
  echo "── apt path (Debian/Ubuntu) ──"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME:-stable} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
elif command -v dnf >/dev/null 2>&1; then
  echo "── dnf path (RHEL/Fedora) ──"
  dnf -y install dnf-plugins-core
  dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null \
    || dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
  dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
else
  echo "ERROR: no apt-get or dnf — install Docker manually for this distro." >&2
  exit 1
fi

systemctl enable --now docker
echo "✅ provisioned:"; docker --version; docker compose version
echo "   dockerd enabled on boot → restart:always container survives reboot."
