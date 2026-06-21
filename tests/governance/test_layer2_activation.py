"""YM-T10 — Layer-2 production-activation wiring tests (Sovereign Daemon
Injection Protocol).

The cross-cutting review found Layer 2 correct but DORMANT. YM-T10 activates
the four production seams:

  * SEAM 1 — GovernedLoopService spawns the OperatorPresenceWatcher daemon +
    awaits operator_yield_bridge.attach() during boot, both fail-soft.
  * SEAM 2 — the default SensorGovernor singleton is constructed with
    operator_active_fn=operator_present (the hard-zero DI link).
  * SEAM 3 — the SerpentREPL input boundary calls note_human_input().

These tests assert the WIRING is present + safe (structural + behavioral),
without re-testing the underlying Layer-2 unit behaviour (covered by
test_operator_presence.py / test_operator_yield_bridge.py / etc.).

Everything is byte-identical when JARVIS_OPERATOR_YIELD_ENABLED is off — these
tests therefore assert presence of the DI'd function / spawn / hook, plus
fail-soft behaviour, rather than feature behaviour with the flag on.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import List

import pytest


# ===========================================================================
# SEAM 2 — Governor DI (the hard-zero link). Highest value + easiest.
# ===========================================================================


def test_default_governor_has_operator_active_fn_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_default_governor()/ensure_seeded() produce a governor whose
    _operator_active_fn is wired (not None) — the DI link is live."""
    import backend.core.ouroboros.governance.sensor_governor as sg

    # Reset the singleton so we observe a fresh construction.
    sg.reset_default_governor()
    try:
        gov = sg.get_default_governor()
        assert gov._operator_active_fn is not None, (
            "SEAM 2 regression: governor singleton built without "
            "operator_active_fn — the hard-zero DI link is dormant"
        )
        # The DI'd fn must be the deterministic presence probe.
        from backend.core.ouroboros.governance.operator_presence import (
            operator_present,
        )
        assert gov._operator_active_fn is operator_present

        # ensure_seeded() returns the SAME singleton (still DI'd).
        seeded = sg.ensure_seeded()
        assert seeded is gov
        assert seeded._operator_active_fn is operator_present
    finally:
        sg.reset_default_governor()


def test_governor_di_is_byte_identical_when_yield_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the master flag off, the DI'd fn reports the operator absent
    (idle by default) so the governor behaves exactly as pre-YM-T10."""
    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
    import backend.core.ouroboros.governance.operator_presence as op

    # No recent human input + flag off → operator_present() must be False
    # regardless of the timestamp (the watcher/governor gating handles the
    # flag; the bare probe just reports presence).
    op._last_input = 0.0
    # operator_present() is the probe the governor calls; under the
    # default-idle module state it should report absent.
    assert op.operator_present() is False


def test_governor_di_fail_soft_when_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the lazy operator_present import raises, the governor must STILL
    be constructable (fail-soft → operator_active_fn=None), never crashing
    the singleton accessor."""
    import builtins

    import backend.core.ouroboros.governance.sensor_governor as sg

    sg.reset_default_governor()
    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name.endswith("operator_presence") or "operator_presence" in name:
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    try:
        gov = sg.get_default_governor()  # must NOT raise
        assert gov is not None
        # fail-soft path → fn is None, governor still works.
        assert gov._operator_active_fn is None
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        sg.reset_default_governor()


# ===========================================================================
# SEAM 3 — zero-latency REPL/intake human-input hook.
# ===========================================================================


def test_note_human_input_updates_presence_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """note_human_input() (the SEAM-3 hook) makes operator_present() True
    within the idle window."""
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    import backend.core.ouroboros.governance.operator_presence as op

    op._last_input = 0.0
    assert op.operator_present() is False  # stale → absent
    op.note_human_input()  # the SEAM-3 call
    assert op.operator_present() is True  # fresh → present


def test_repl_input_loop_calls_note_human_input() -> None:
    """The SerpentREPL input boundary is wired to note_human_input().

    Structural assertion against the REPL source: the highest-signal human-
    input boundary (_loop) must reference note_human_input so every human
    submission stamps presence. Lazy-import + fail-soft so a missing module
    never perturbs REPL dispatch.
    """
    from backend.core.ouroboros.battle_test import serpent_flow

    src = inspect.getsource(serpent_flow)
    assert "note_human_input" in src, (
        "SEAM 3 regression: REPL input boundary no longer calls "
        "note_human_input() — operator presence will never be stamped"
    )
    # The hook lives in the interactive input loop (_loop).
    loop_src = inspect.getsource(serpent_flow.SerpentREPL._loop)
    assert "note_human_input" in loop_src, (
        "SEAM 3 regression: note_human_input() not in SerpentREPL._loop"
    )


# ===========================================================================
# SEAM 1 — non-blocking daemon boot + attach (fail-soft).
# ===========================================================================


def test_attach_callable_and_subscribes_when_on_noop_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake-bus/fake-pool attach() subscribes both topics when yield on,
    and subscribes nothing when off (the GLS SEAM-1 call passes bus=None +
    pool=self._bg_pool; here we drive attach directly with fakes)."""
    from backend.core.ouroboros.governance import operator_yield_bridge as oyb
    from backend.core.ouroboros.governance.operator_presence import (
        EVENT_OPERATOR_ACTIVE,
        EVENT_OPERATOR_IDLE,
    )

    class _FakeBus:
        def __init__(self) -> None:
            self.subscribed: List[str] = []

        async def subscribe(self, pattern, handler, **kw):
            self.subscribed.append(pattern)
            return f"sub-{len(self.subscribed)}"

    class _FakePool:
        def list_all(self):
            return []

    # OFF → no-op (nothing subscribed)
    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
    bus_off = _FakeBus()
    asyncio.run(oyb.attach(bus=bus_off, pool=_FakePool()))
    assert bus_off.subscribed == []

    # ON → both topics subscribed
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    bus_on = _FakeBus()
    asyncio.run(oyb.attach(bus=bus_on, pool=_FakePool()))
    assert EVENT_OPERATOR_ACTIVE in bus_on.subscribed
    assert EVENT_OPERATOR_IDLE in bus_on.subscribed


def test_gls_has_operator_yield_layer_helper_and_task_ref() -> None:
    """GLS exposes the SEAM-1 spawn helper + the strong task-ref attribute,
    and stop() cancels the task. Structural — full GLS boot is too heavy to
    instantiate in a unit test (see report)."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopService,
    )

    assert hasattr(GovernedLoopService, "_start_operator_yield_layer"), (
        "SEAM 1 regression: GLS missing _start_operator_yield_layer helper"
    )
    assert asyncio.iscoroutinefunction(
        GovernedLoopService._start_operator_yield_layer
    )

    # start() must invoke the helper; stop() must cancel the task ref.
    start_src = inspect.getsource(GovernedLoopService.start)
    assert "_start_operator_yield_layer" in start_src, (
        "SEAM 1 regression: start() no longer invokes the yield-layer boot"
    )
    stop_src = inspect.getsource(GovernedLoopService.stop)
    assert "_operator_presence_task" in stop_src, (
        "SEAM 1 regression: stop() no longer cancels the watcher daemon"
    )


def test_start_operator_yield_layer_fail_soft_on_watcher_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SEAM-1 boot helper is fail-soft in ISOLATION: if the watcher
    spawn raises (e.g. OperatorPresenceWatcher constructor blows up), the
    helper must NOT propagate — boot proceeds. We drive the unbound coroutine
    against a minimal stand-in self to avoid a full GLS boot.
    """
    import backend.core.ouroboros.governance.governed_loop_service as gls_mod
    import backend.core.ouroboros.governance.operator_presence as op_mod

    # Make the watcher constructor explode.
    class _ExplodingWatcher:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated watcher boom")

    monkeypatch.setattr(op_mod, "OperatorPresenceWatcher", _ExplodingWatcher)

    class _FakeSelf:
        _bg_pool = None
        _operator_presence_task = None

    fake = _FakeSelf()
    # Must NOT raise — fail-soft swallows the watcher error AND the
    # attach (bus=None resolves to no real bus → graceful skip).
    asyncio.run(
        gls_mod.GovernedLoopService._start_operator_yield_layer(fake)
    )
    # Watcher spawn failed → task ref stays None (degraded, not crashed).
    assert fake._operator_presence_task is None


def test_start_operator_yield_layer_spawns_task_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the happy path the helper spawns a strong-ref daemon task. With the
    flag off the watcher.run() returns immediately (no-op), so the task
    completes cleanly — proving the create_task wiring + ref storage."""
    import backend.core.ouroboros.governance.governed_loop_service as gls_mod

    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)

    class _FakeSelf:
        _bg_pool = None
        _operator_presence_task = None

    fake = _FakeSelf()

    async def _drive() -> None:
        await gls_mod.GovernedLoopService._start_operator_yield_layer(fake)
        # A task ref must have been stored.
        assert fake._operator_presence_task is not None
        # Flag off → watcher.run() returns immediately; let it settle.
        await asyncio.sleep(0)
        await asyncio.wait_for(fake._operator_presence_task, timeout=1.0)

    asyncio.run(_drive())
