"""start_soak_vm — Sovereign Cloud soak-mode entry on GCPVMManager.

Verifies it reuses the instance-build recipe, appends soak metadata (DW key +
pin), and bypasses the offload gating — failing cleanly on bad config.
"""
from __future__ import annotations

import pytest

import google.cloud.compute_v1 as compute_v1
from backend.core.gcp_vm_manager import GCPVMManager, VMManagerConfig


def _mgr(tmp_path, *, enabled=True, script=True):
    cfg = VMManagerConfig()
    cfg.enabled = enabled
    cfg.project_id = "jarvis-473803"
    cfg.zone = "us-central1-a"
    cfg.machine_type = "e2-custom-8-16384"
    if script:
        p = tmp_path / "startup.sh"
        p.write_text("#!/bin/bash\necho soak\n")
        cfg.startup_script_path = str(p)
    else:
        cfg.startup_script_path = None
    return GCPVMManager(cfg)


class _FakeClient:
    def __init__(self):
        self.inserts = []

    def insert(self, *, project, zone, instance_resource):
        self.inserts.append((project, zone, instance_resource))
        return object()


@pytest.mark.asyncio
async def test_missing_startup_script_fails_clean(tmp_path):
    m = _mgr(tmp_path, script=False)
    ok, msg = await m.start_soak_vm()
    assert ok is False and "SOAK_STARTUP_SCRIPT_MISSING" in msg


@pytest.mark.asyncio
async def test_gcp_disabled_fails_clean(tmp_path):
    m = _mgr(tmp_path, enabled=False)
    ok, msg = await m.start_soak_vm()
    assert ok is False and "GCP_DISABLED" in msg


@pytest.mark.asyncio
async def test_success_inserts_and_reuses_recipe(tmp_path, monkeypatch):
    m = _mgr(tmp_path)
    fake = _FakeClient()
    m.instances_client = fake  # _get_instances_client returns the cached client
    built = {"called": None}

    def _fake_build(vm_name, components, trigger, metadata):
        built["called"] = (vm_name, tuple(components), trigger)
        return compute_v1.Instance(
            name=vm_name, metadata=compute_v1.Metadata(items=[]),
        )
    monkeypatch.setattr(m, "_build_instance_config", _fake_build)

    ok, name = await m.start_soak_vm(
        extra_metadata={
            "jarvis-dw-api-key": "SECRET",
            "jarvis-dw-primary-override": "openai/gpt-oss-120b",
        },
    )
    assert ok is True
    assert name.startswith("jarvis-ouroboros-soak-")
    assert built["called"][1] == ("ouroboros_soak",)
    assert built["called"][2] == "ouroboros_soak"
    assert len(fake.inserts) == 1
    proj, zone, inst = fake.inserts[0]
    assert proj == "jarvis-473803" and zone == "us-central1-a"
    keys = {it.key: it.value for it in inst.metadata.items}
    assert keys["jarvis-dw-api-key"] == "SECRET"
    assert keys["jarvis-dw-primary-override"] == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_create_failure_is_failsoft(tmp_path, monkeypatch):
    m = _mgr(tmp_path)

    def _boom(vm_name, components, trigger, metadata):
        raise RuntimeError("compute exploded")
    m.instances_client = _FakeClient()
    monkeypatch.setattr(m, "_build_instance_config", _boom)
    ok, msg = await m.start_soak_vm()
    assert ok is False and "SOAK_CREATE_FAILED" in msg and "RuntimeError" in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
