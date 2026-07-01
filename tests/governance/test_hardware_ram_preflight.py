"""Intelligent Hardware-Model RAM Assertion (Pre-Flight Gate).

The g2-standard-4 (16GB RAM) OOM-killed llama-server loading the 19.85GB
qwen2.5-coder:32b GGUF -- the kernel global_oom, BEFORE the L4 VRAM was ever
used. This gate refuses to provision a host whose system RAM cannot physically
hold the model + OS overhead: it derives the machine's RAM + the model's GGUF
size and mathematically asserts RAM > gguf + overhead BEFORE the instances.insert,
raising HardwareProvisioningMismatchError (never attempting an impossible load).
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_tier as ft


_GIB = 1024 ** 3


# --- machine RAM resolution -------------------------------------------------

def test_machine_ram_known_g2_types():
    assert ft.machine_type_ram_bytes("g2-standard-4") == 16 * _GIB
    assert ft.machine_type_ram_bytes("g2-standard-8") == 32 * _GIB
    assert ft.machine_type_ram_bytes("g2-standard-16") == 64 * _GIB


def test_machine_ram_survival_and_unknown():
    assert ft.machine_type_ram_bytes("e2-highmem-2") == 16 * _GIB
    assert ft.machine_type_ram_bytes("totally-made-up") == 0
    assert ft.machine_type_ram_bytes("") == 0


def test_machine_ram_g2_pattern_derivation():
    # g2-standard-N derives N*4GiB even if not in the static map.
    assert ft.machine_type_ram_bytes("g2-standard-48") == 192 * _GIB


def test_machine_ram_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_MACHINE_RAM_MB_G2_STANDARD_4", "65536")
    assert ft.machine_type_ram_bytes("g2-standard-4") == 64 * _GIB


# --- GGUF size estimate -----------------------------------------------------

def test_gguf_estimate_matches_observed_32b():
    # 32B * ~0.62 bytes/param ~= 19.8GB (the observed /api/tags size was 19.85GB).
    n = ft.estimate_gguf_bytes("qwen2.5-coder:32b")
    assert 18 * _GIB < n < 21 * _GIB


def test_gguf_estimate_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MODEL_BYTES", str(5 * _GIB))
    assert ft.estimate_gguf_bytes("qwen2.5-coder:32b") == 5 * _GIB


def test_gguf_estimate_unknown_label_zero():
    assert ft.estimate_gguf_bytes("no-param-count-here") == 0


# --- the assertion ----------------------------------------------------------

def test_assert_raises_on_g2_standard_4_with_32b():
    # 16GB RAM < 19.85GB model + 4GB overhead -> IMPOSSIBLE load -> raise.
    with pytest.raises(ft.HardwareProvisioningMismatchError):
        ft.assert_host_ram_fits_model("g2-standard-4", "qwen2.5-coder:32b")


def test_assert_passes_on_g2_standard_8_with_32b():
    # 32GB RAM > 19.85GB + 4GB = 23.85GB -> fits -> no raise.
    ft.assert_host_ram_fits_model("g2-standard-8", "qwen2.5-coder:32b")


def test_assert_failopen_on_unknown_ram():
    # Can't determine RAM -> do NOT block (only block when CERTAIN it won't fit).
    ft.assert_host_ram_fits_model("mystery-machine", "qwen2.5-coder:32b")


def test_assert_failopen_on_unknown_model():
    ft.assert_host_ram_fits_model("g2-standard-4", "opaque-model")


def test_assert_overhead_is_tunable(monkeypatch):
    # Tighten overhead so g2-standard-4 + a 7B model still fits (7B*0.62~=4.3GB).
    ft.assert_host_ram_fits_model("g2-standard-4", "qwen2.5-coder:7b")
    # ...but a huge overhead makes even that fail.
    monkeypatch.setenv("JARVIS_HOST_RAM_OVERHEAD_BYTES", str(20 * _GIB))
    with pytest.raises(ft.HardwareProvisioningMismatchError):
        ft.assert_host_ram_fits_model("g2-standard-4", "qwen2.5-coder:7b")


# --- WIRING: _do_awaken halts (never inserts) on a RAM mismatch --------------

import backend.core.ouroboros.governance.failover_lifecycle as fl  # noqa: E402
from backend.core.ouroboros.governance import provider_quarantine as pq  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def _fake_forecast(conf="HIGH"):
    class _F:
        confidence = conf
        p50_s = 300.0
        p90_s = 600.0
        velocity_hint = 1.0
        samples = 5
    return _F()


@pytest.fixture
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    for k, v in {
        "JARVIS_FAILOVER_LIFECYCLE_ENABLED": "true",
        "JARVIS_FAILOVER_ROUTE": "dw",
        "JARVIS_QUARANTINE_WINDOW": "5",
        "JARVIS_JPRIME_COLDSTART_S": "100",
        "JARVIS_CRYO_AWAKEN_MARGIN": "1.5",
        "JARVIS_OUTAGE_CONFIRM_S": "120",
        # QUALITY tier: g2-standard-4 (16GB) + 32B (19.85GB) -> IMPOSSIBLE.
        "JARVIS_FAILOVER_QUALITY_TIER_ENABLED": "true",
        "JARVIS_FAILOVER_AWAKEN_URGENCY": "immediate",
        "JARVIS_FAILOVER_QUALITY_MACHINE": "g2-standard-4",
        "JARVIS_FAILOVER_QUALITY_MODEL": "qwen2.5-coder:32b",
    }.items():
        monkeypatch.setenv(k, v)
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


async def test_do_awaken_halts_on_ram_mismatch(_fresh):
    """A g2-standard-4 + 32B awaken must be REFUSED at the pre-flight gate: the
    vm_awaken_fn (which would fire instances.insert) is NEVER called, and the FSM
    reverts DORMANT (no impossible node)."""
    clock = _Clock()
    awaken_calls = []
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
    )
    ctrl._get_forecast = lambda: _fake_forecast("HIGH")
    grad = pq.get_provider_health_gradient()
    for _ in range(5):
        grad.record_sweep("dw", success=False)

    await ctrl.tick()  # DORMANT -> AWAKENING -> _do_awaken -> gate FAILS -> DORMANT

    assert awaken_calls == []                       # instances.insert NEVER attempted
    assert ctrl.state == fl.FailoverState.DORMANT   # halted (no impossible node)


async def test_do_awaken_proceeds_on_ram_fit(_fresh, monkeypatch):
    """Bump to g2-standard-8 (32GB) -> the gate PASSES and the awaken proceeds
    (vm_awaken_fn IS called)."""
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MACHINE", "g2-standard-8")
    clock = _Clock()
    awaken_calls = []
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
    )
    ctrl._get_forecast = lambda: _fake_forecast("HIGH")
    grad = pq.get_provider_health_gradient()
    for _ in range(5):
        grad.record_sweep("dw", success=False)

    await ctrl.tick()  # -> AWAKENING -> gate PASSES -> vm_awaken_fn called

    assert awaken_calls == [1]                       # provisioning proceeded
    assert ctrl.state == fl.FailoverState.AWAKENING
