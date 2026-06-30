"""Task HW2 -- Autonomous Diagnostic Reaper.

When a heavy cold-start node never goes green (AWAKENING times out / L7 never
serves), blindly deleting it throws away the ONE artifact that explains WHY it
failed: the boot serial console. These tests prove the FSM autonomously reads
``get_serial_port_output`` BEFORE issuing the delete, classifies the failure
(NVIDIA driver fault / kernel panic / OOM / disk-full), and seals that
classification into the Cryo-DLQ + a flare -- so the next ignition is informed,
not blind. The reaper is fail-soft (never blocks the teardown) and gated by a
master switch.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import intake_dlq
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
    FailoverState,
)
from backend.core.ouroboros.governance.failover_tier import FailoverTier


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_DIAGNOSTIC_REAPER_ENABLED", "true")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


@pytest.fixture
def dlq_capture(monkeypatch):
    """Capture every Cryo-DLQ write, bypassing the JARVIS_INTAKE_DLQ_ENABLED
    gate, with auto-restore (monkeypatch) so it never pollutes sibling files."""
    sink: list = []

    def _append_dlq(envelope, *, reason, path=None):
        sink.append((envelope, reason))

    monkeypatch.setattr(intake_dlq, "append_dlq", _append_dlq)
    return sink


def _make_ctrl(clock, *, serial=None, serial_raises=False, calls=None, **kw):
    flares = kw.pop("flares", [])

    def _serial_port_fn():
        if calls is not None:
            calls.append("serial")
        if serial_raises:
            raise RuntimeError("metadata unreachable")
        return serial

    def _delete():
        if calls is not None:
            calls.append("delete")
        return True

    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=_delete,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: False,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: flares.append(payload),
        serial_port_fn=_serial_port_fn,
    )
    defaults.update(kw)
    ctrl = FailoverLifecycleController(**defaults)
    ctrl._captured_flares = flares  # type: ignore[attr-defined]
    return ctrl


def _heavy_tier() -> FailoverTier:
    return FailoverTier(
        name="quality",
        machine_type="g2-standard-4",
        image_family="x",
        model_label="qwen2.5-coder:32b",
        accelerator_type="nvidia-l4",
        accelerator_count=1,
    )


# Representative serial console fragments (trimmed real-world boot logs).
_NVIDIA = (
    "[   42.118] NVRM: GPU 0000:00:04.0: RmInitAdapter failed! (0x26:0xffff:1456)\n"
    "[   42.119] NVRM: rm_init_adapter failed for device bearing minor number 0\n"
    "nvidia-smi: NVIDIA-SMI has failed because it couldn't communicate with the "
    "NVIDIA driver.\n"
)
_PANIC = (
    "[   13.882] Kernel panic - not syncing: VFS: Unable to mount root fs on "
    "unknown-block(0,0)\n[   13.883] CPU: 0 PID: 1 Comm: swapper/0 Not tainted\n"
)
_OOM = (
    "[  301.55] Out of memory: Killed process 1442 (ollama) total-vm:24190000kB\n"
    "[  301.56] oom-killer: gfp_mask=0x..., order=0\n"
)
_DISK = "[   88.1] write error: No space left on device\n"
_BENIGN = "[    0.00] Linux version 6.1.0\n[    2.31] systemd[1]: Reached target.\n"


# ---------------------------------------------------------------------------
# _classify_serial_output -- pure classifier, priority-ordered
# ---------------------------------------------------------------------------

def test_classify_nvidia_driver_fault():
    assert fl._classify_serial_output(_NVIDIA) == "nvidia_driver_fault"


def test_classify_kernel_panic():
    assert fl._classify_serial_output(_PANIC) == "kernel_panic"


def test_classify_oom():
    assert fl._classify_serial_output(_OOM) == "oom"


def test_classify_disk_full():
    assert fl._classify_serial_output(_DISK) == "disk_full"


def test_classify_empty():
    assert fl._classify_serial_output(None) == "empty"
    assert fl._classify_serial_output("") == "empty"
    assert fl._classify_serial_output("   \n  ") == "empty"


def test_classify_unknown():
    assert fl._classify_serial_output(_BENIGN) == "unknown"


def test_classify_priority_nvidia_beats_oom():
    # On a GPU cold-start, an NVIDIA fault is the actionable root cause even when
    # an OOM also appears downstream -- the GPU classification must win.
    both = _OOM + _NVIDIA
    assert fl._classify_serial_output(both) == "nvidia_driver_fault"


# ---------------------------------------------------------------------------
# _diagnose_before_reap -- reads serial, classifies, seals to Cryo-DLQ + flare
# ---------------------------------------------------------------------------

async def test_diagnose_seals_classification_to_cryo_dlq(dlq_capture):
    dlq = dlq_capture
    clock = FakeClock()
    ctrl = _make_ctrl(clock, serial=_NVIDIA)
    ctrl._awakened_tier = _heavy_tier()

    classification = await ctrl._diagnose_before_reap()

    assert classification == "nvidia_driver_fault"
    assert len(dlq) == 1
    envelope, reason = dlq[0]
    assert reason == "node_coldstart_failure:nvidia_driver_fault"
    assert envelope["classification"] == "nvidia_driver_fault"
    # The serial excerpt must be carried so the diagnosis is auditable.
    assert "NVRM" in envelope["serial_excerpt"]


async def test_diagnose_emits_flare(dlq_capture):
    clock = FakeClock()
    ctrl = _make_ctrl(clock, serial=_PANIC)

    await ctrl._diagnose_before_reap()

    flares = ctrl._captured_flares  # type: ignore[attr-defined]
    assert any(
        f.get("classification") == "kernel_panic" for f in flares
    ), flares


async def test_diagnose_fail_soft_when_serial_unavailable(dlq_capture):
    # Serial read raises (metadata unreachable) -> classified 'unavailable',
    # NEVER raises, and still seals a record so the teardown is auditable.
    dlq = dlq_capture
    clock = FakeClock()
    ctrl = _make_ctrl(clock, serial_raises=True)

    classification = await ctrl._diagnose_before_reap()

    assert classification == "unavailable"
    assert dlq and dlq[0][1] == "node_coldstart_failure:unavailable"


async def test_diagnose_disabled_skips_serial_read(dlq_capture, monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_DIAGNOSTIC_REAPER_ENABLED", "false")
    clock = FakeClock()
    calls: list = []
    ctrl = _make_ctrl(clock, serial=_NVIDIA, calls=calls)

    classification = await ctrl._diagnose_before_reap()

    assert classification == "disabled"
    assert "serial" not in calls  # no serial read when master-off
    assert dlq_capture == []


# ---------------------------------------------------------------------------
# Integration: AWAKENING timeout invokes the reaper BEFORE the delete
# ---------------------------------------------------------------------------

async def test_awakening_timeout_diagnoses_before_delete(dlq_capture):
    dlq = dlq_capture
    clock = FakeClock()
    calls: list = []
    ctrl = _make_ctrl(clock, serial=_NVIDIA, calls=calls)
    ctrl._awakened_tier = _heavy_tier()
    ctrl._state = FailoverState.AWAKENING
    ctrl._awakening_started_at = clock.t
    # Push past the (adaptive, heavy-scaled) self-heal deadline.
    clock.t += ctrl._adaptive_timeout(fl._awaken_timeout_s()) + 10.0

    await ctrl._tick_awakening(now=clock.t)

    # The serial read MUST precede the delete -- diagnose, then reap.
    assert calls.index("serial") < calls.index("delete"), calls
    assert ctrl.state == FailoverState.DORMANT
    assert dlq and dlq[0][1] == "node_coldstart_failure:nvidia_driver_fault"


async def test_awakening_timeout_still_reaps_when_diagnosis_raises(dlq_capture):
    # Even if diagnosis blows up, the node is STILL deleted (cost-leak guard).
    clock = FakeClock()
    calls: list = []
    ctrl = _make_ctrl(clock, serial_raises=True, calls=calls)
    ctrl._state = FailoverState.AWAKENING
    ctrl._awakening_started_at = clock.t
    clock.t += ctrl._adaptive_timeout(fl._awaken_timeout_s()) + 10.0

    await ctrl._tick_awakening(now=clock.t)

    assert "delete" in calls
    assert ctrl.state == FailoverState.DORMANT
