"""Slice 24 — posture cadence flipped to the yielding chunked path by default.

Two soaks (bt-2026-07-15-10:22 = 3,182 ms; ...-154242 = 3,494 ms) caught
``posture_observer.run_one_cycle`` holding the GIL on the ~5-min cadence: the
wholesale path runs the ENTIRE cycle in ONE offload thread, and a thread
running pure-Python holds the GIL. Slice 24 defaults to the chunked path, which
dispatches each of the 12 collectors separately with an ``asyncio.sleep(0)``
between them (heartbeats process between chunks), and offloads the
``_process_bundle`` inference tail too — so no on-loop GIL span remains.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import posture_observer as PO


def test_wholesale_default_is_chunked(monkeypatch):
    """The default (no env) is now the chunked yielding path."""
    monkeypatch.delenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", raising=False)
    assert PO.wholesale_offload_enabled() is False


def test_wholesale_env_still_available_for_rollback(monkeypatch):
    monkeypatch.setenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", "true")
    assert PO.wholesale_offload_enabled() is True
    monkeypatch.setenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", "false")
    assert PO.wholesale_offload_enabled() is False


def test_chunked_path_offloads_process_bundle_tail():
    """The chunked path offloads the DirectionInferrer tail (no on-loop GIL
    span) and keeps the loop-affine on_change on the loop."""
    src = inspect.getsource(PO.PostureObserver._run_one_cycle_impl)
    assert "offload" in src and "cpu_bound=False" in src
    assert "_fire_on_change" in src
    # on_change fires AFTER the offloaded tail resolves (loop thread), not inside.
    assert src.index("offload") < src.index("_fire_on_change")


def test_collect_with_timeout_is_fail_soft():
    """A collector that raises from build_bundle_async must not propagate now
    that the chunked path is default (parity with the wholesale offload trap)."""
    src = inspect.getsource(PO.PostureObserver._collect_with_timeout)
    assert "except asyncio.TimeoutError" in src
    assert "except Exception" in src  # general fail-soft (Slice 24)


def test_batch_entitlement_fallback_already_wired():
    """Mandate 1's batch 35B→397B fallback ALREADY exists in _upload_file
    (Task 4/F1) — verified live in the soak. Pin it so it can't regress; do
    NOT build a duplicate resolver (mandate 3)."""
    import backend.core.ouroboros.governance.doubleword_provider as DW
    src = inspect.getsource(DW.DoublewordProvider._upload_file)
    assert "is_entitlement_blocked" in src
    assert "_entitlement_fallback_model" in src
    assert "_entitlement_retry" in src


def test_aegis_registered_in_live_process():
    """Mandate 4 — the battle-test registers the aegis daemon PID in the LIVE
    (post-re-exec) process so the child_reaper cascade fast-path targets it."""
    import pathlib
    src = pathlib.Path("scripts/ouroboros_battle_test.py").read_text()
    assert "register_child" in src
    assert "subprocess_pid" in src
    assert 'role="aegis_daemon"' in src
