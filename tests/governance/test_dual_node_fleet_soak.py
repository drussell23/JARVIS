"""DUAL-NODE FLEET SOAK -- the full elastic-mesh choreography, proven locally.

Exercises the REAL GpuEscalationLane + FleetRegistry + crypto naming with a
simulated GCP substrate, proving structurally (in seconds, no cloud cost) that:

  1. the CPU (7B) survival node boots + registers,
  2. a heavy op dynamically scales the GPU (32B) node IN PARALLEL,
  3. the two nodes carry crypto-distinct names + firewall rules (ZERO collision),
  4. both endpoints live concurrently in the registry; the router picks per-op,
  5. draining the heavy op REAPS the GPU node while the CPU stays alive.

This is the local proof; a single real-GCP confirm is the follow-up.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.failover_gpu_lane import GpuEscalationLane
from backend.core.ouroboros.governance.failover_naming import firewall_name, node_name
from backend.core.ouroboros.governance.fleet_registry import (
    get_fleet_registry,
    reset_fleet_registry,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    reset_fleet_registry()
    yield
    reset_fleet_registry()


async def test_dual_node_full_choreography(capsys):
    reg = get_fleet_registry()
    log = []

    # ---- Phase 1: CPU survival node boots + registers (crypto-namespaced) ----
    cpu_name, cpu_fw = node_name("cpu"), firewall_name("cpu")
    reg.register("cpu", "http://cpu-ext-ip:11434")
    log.append(f"[1] CPU node UP   vm={cpu_name} fw={cpu_fw} ep={reg.endpoint_for('cpu')}")
    assert reg.classes() == ("cpu",)

    # ---- Build the GPU lane over a SIMULATED GCP substrate that uses the REAL
    #      naming + registry, asserting collision-freedom at provision time. ----
    seen = {}

    async def provision_gpu():
        gpu_name, gpu_fw = node_name("gpu"), firewall_name("gpu")
        # STRUCTURAL collision guarantee: GPU assets differ from the live CPU's.
        assert gpu_name != cpu_name, "VM name collision!"
        assert gpu_fw != cpu_fw, "firewall name collision!"
        seen["gpu_name"], seen["gpu_fw"] = gpu_name, gpu_fw
        ep = "http://gpu-ext-ip:11434"
        reg.register("gpu", ep)
        log.append(f"[3] GPU node UP   vm={gpu_name} fw={gpu_fw} ep={ep}")
        return ep

    async def reap_gpu():
        reg.unregister("gpu")
        log.append(f"[5] GPU node REAPED vm={seen.get('gpu_name')} -- CPU still UP")

    lane = GpuEscalationLane(
        provision_fn=provision_gpu, reap_fn=reap_gpu,
        outage_confirmed_fn=lambda: True,
    )

    # ---- Phase 2-3: a heavy COMPLEX op arrives -> dynamic GPU scale-out --------
    log.append("[2] heavy COMPLEX op arrives -> requesting escalation")
    gpu_ep = await lane.request("heavy-op", urgency="immediate", complexity="complex")
    assert gpu_ep == "http://gpu-ext-ip:11434"

    # ---- Phase 4: both nodes live concurrently; router picks per-op -----------
    assert set(reg.classes()) == {"cpu", "gpu"}
    assert reg.endpoint_for("cpu") != reg.endpoint_for("gpu")  # distinct endpoints
    assert lane.endpoint == reg.endpoint_for("gpu")
    log.append(f"[4] FLEET LIVE    cpu={reg.endpoint_for('cpu')} gpu={reg.endpoint_for('gpu')}")
    # A concurrent BACKGROUND op must NOT touch the GPU -> router sends it to CPU.
    bg = await lane.request("bg-op", urgency="background", complexity="simple")
    assert bg is None
    assert lane.gpu_inflight_count() == 1  # bg op did not land on the GPU

    # ---- Phase 5: drain the heavy op -> GPU reaped, CPU survives --------------
    await lane.complete("heavy-op")
    assert reg.endpoint_for("gpu") is None, "GPU should be reaped on drain"
    assert reg.endpoint_for("cpu") == "http://cpu-ext-ip:11434", "CPU must survive"
    assert lane.is_gpu_active() is False
    assert reg.classes() == ("cpu",)

    print("\n".join(log))
    with capsys.disabled():
        print("\n=== DUAL-NODE FLEET SOAK CHOREOGRAPHY ===")
        for line in log:
            print(line)
        print("=== CPU survived; GPU elastically scaled + reaped; zero collision ===")
