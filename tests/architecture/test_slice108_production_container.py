"""Slice 108 — Production (layer-cached) container pipeline.

Mock matrix for the parameterized provisioner (governance vs full-ML image), plus
the marquee LIVE test (skipped when Docker is down): the REAL O+V governance test
suite runs inside the hardened, layer-cached production container against the live
read-only worktree mount — no rebuild per op (the Immutability Invariant).
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from backend.core.ouroboros.governance import image_provisioner as IP
from backend.core.ouroboros.governance import container_sandbox as CS
from backend.core.ouroboros.governance.runtime_sandbox import ContainmentPolicy


def _docker_responsive() -> bool:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


_skip_no_docker = pytest.mark.skipif(not _docker_responsive(), reason="Docker daemon not responding")
_GOV_IMAGE = "jarvis-governance-sandbox:latest"


class _FakeDocker:
    def __init__(self, *responses):
        self._responses = list(responses) or [(0, "", "")]
        self.calls = []

    async def __call__(self, argv, timeout_s):
        self.calls.append(list(argv))
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


# === Provisioner parameterization (governance vs default light image) =======

def test_default_targets_light_image(monkeypatch):
    for v in ("JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE", "JARVIS_SANDBOX_REQUIREMENTS_FILE",
              "JARVIS_SANDBOX_DOCKERFILE"):
        monkeypatch.delenv(v, raising=False)
    assert IP.verify_image() == "jarvis-verify-sandbox:latest"
    assert IP.requirements_file() == "requirements-sandbox.txt"
    assert IP.dockerfile_name() == "Dockerfile.verify-sandbox"


def test_production_env_retargets_image_and_inputs(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE", _GOV_IMAGE)
    monkeypatch.setenv("JARVIS_SANDBOX_REQUIREMENTS_FILE", "requirements-governance.txt")
    monkeypatch.setenv("JARVIS_SANDBOX_DOCKERFILE", "Dockerfile.production-sandbox")
    assert IP.verify_image() == _GOV_IMAGE
    assert IP.requirements_file() == "requirements-governance.txt"
    assert IP.dockerfile_name() == "Dockerfile.production-sandbox"


def test_hash_differs_between_light_and_production(monkeypatch):
    monkeypatch.delenv("JARVIS_SANDBOX_REQUIREMENTS_FILE", raising=False)
    monkeypatch.delenv("JARVIS_SANDBOX_DOCKERFILE", raising=False)
    light = IP.image_state_hash()
    monkeypatch.setenv("JARVIS_SANDBOX_REQUIREMENTS_FILE", "requirements-governance.txt")
    monkeypatch.setenv("JARVIS_SANDBOX_DOCKERFILE", "Dockerfile.production-sandbox")
    prod = IP.image_state_hash()
    assert light != prod and light != "unknown" and prod != "unknown"


def test_production_build_argv_uses_dockerfile_and_requirements(monkeypatch):
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE", _GOV_IMAGE)
    monkeypatch.setenv("JARVIS_SANDBOX_REQUIREMENTS_FILE", "requirements-governance.txt")
    monkeypatch.setenv("JARVIS_SANDBOX_DOCKERFILE", "Dockerfile.production-sandbox")
    fake = _FakeDocker((0, "STALE", ""), (0, "built", ""))   # inspect stale → build
    res = asyncio.run(IP.provision_image(docker_run=fake))
    assert res.rebuilt is True
    build = " ".join(fake.calls[-1])
    assert "Dockerfile.production-sandbox" in build
    assert "REQUIREMENTS=requirements-governance.txt" in build
    assert _GOV_IMAGE in build and "--label" in build


# === The marquee: the REAL O+V governance suite, contained, layer-cached =====

@_skip_no_docker
def test_live_governance_suite_runs_in_production_container(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE", _GOV_IMAGE)
    monkeypatch.setenv("JARVIS_SANDBOX_REQUIREMENTS_FILE", "requirements-governance.txt")
    monkeypatch.setenv("JARVIS_SANDBOX_DOCKERFILE", "Dockerfile.production-sandbox")
    # Ensure the layer-cached production image is present + current (pre-warm).
    prov = asyncio.run(IP.provision_image())
    assert prov.current is True
    # Run a REAL governance test suite against the live repo worktree (read-only mount).
    repo_root = os.getcwd()
    res = asyncio.run(CS.run_pytest_in_container(
        ["tests/governance/test_slice101_phase5_proof_gate.py"],
        worktree=repo_root, image=_GOV_IMAGE, policy=ContainmentPolicy(timeout_s=120),
    ))
    # The full governance import chain resolved inside the hardened container.
    assert res.total >= 12, f"expected the suite to run, got {res.diagnostic}"
    assert res.failed == 0, f"governance suite failed in container: {res.failed_names}"
    assert res.ok is True
