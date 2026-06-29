"""Dynamic IaC Accelerator Injection -- GPU guestAccelerators in the REST payload.

When the tier router selects the GPU/32B quality tier, the native Compute REST
provisioning bridge must inject ``guestAccelerators`` (e.g. an L4) AND the GPU-
mandatory ``scheduling.onHostMaintenance=TERMINATE`` into the instances.insert
payload. No accelerator on the survival tier -> byte-identical CPU payload.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gcr


def _client():
    return gcr.GCPComputeRest()


def test_no_accelerator_by_default():
    p = _client()._build_insert_payload(
        name="n", zone="us-central1-b", project="p",
        machine_type="e2-highmem-2", image_family="img", startup_script="x", spot=True,
    )
    assert "guestAccelerators" not in p
    assert "onHostMaintenance" not in p.get("scheduling", {})


def test_injects_guest_accelerator():
    p = _client()._build_insert_payload(
        name="n", zone="us-central1-b", project="p",
        machine_type="g2-standard-8", image_family="img32b", startup_script="x", spot=True,
        accelerator_type="nvidia-l4", accelerator_count=2,
    )
    accs = p.get("guestAccelerators")
    assert accs and accs[0]["acceleratorCount"] == 2
    # Zonal acceleratorType URL -- dynamic, never a hardcoded full path.
    assert accs[0]["acceleratorType"].endswith(
        "zones/us-central1-b/acceleratorTypes/nvidia-l4"
    )


def test_gpu_forces_onhostmaintenance_terminate():
    """GPUs cannot live-migrate -> onHostMaintenance MUST be TERMINATE."""
    p = _client()._build_insert_payload(
        name="n", zone="us-central1-b", project="p",
        machine_type="g2-standard-8", image_family="i", startup_script="x", spot=False,
        accelerator_type="nvidia-l4", accelerator_count=1,
    )
    assert p["scheduling"]["onHostMaintenance"] == "TERMINATE"


def test_zero_count_is_not_gpu():
    p = _client()._build_insert_payload(
        name="n", zone="z", project="p", machine_type="e2", image_family="i",
        startup_script="x", spot=True, accelerator_type="nvidia-l4", accelerator_count=0,
    )
    assert "guestAccelerators" not in p  # count 0 -> CPU payload (no accidental GPU)


async def test_create_instance_threads_accelerator(monkeypatch):
    """create_instance forwards the accelerator into the payload it POSTs."""
    seen = {}

    async def fake_http(url, *, method, headers=None, body=None, timeout_s=30.0):
        import json
        seen["payload"] = json.loads(body.decode()) if body else None
        return (200, '{"status":"PENDING"}')

    async def fake_token(self):
        return "tok"
    monkeypatch.setattr(gcr, "_http_request", fake_http)
    monkeypatch.setattr(gcr.GCPComputeRest, "access_token", fake_token)
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCP_ZONE", "us-central1-b")

    c = _client()
    ok, _ = await c.create_instance(
        startup_script="x", machine_type="g2-standard-8",
        accelerator_type="nvidia-l4", accelerator_count=1,
    )
    assert ok is True
    assert seen["payload"]["guestAccelerators"][0]["acceleratorCount"] == 1
