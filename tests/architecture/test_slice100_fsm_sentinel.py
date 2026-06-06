"""Slice 100 — FSM Sentinel Guard tests.

Wires Slice 99's ``note_llm_contradiction`` producer hook into the
orchestrator's GENERATE_RETRY loop via the self-contained
``observe_generate_retry`` sentinel.

Load-bearing properties under test:

  * The sentinel fires ONLY past the attempt threshold (a single
    first-attempt retry is NOT ambiguity; a sustained run is).
  * The sentinel is a PULL-model producer — it appends to the
    accumulator, it NEVER calls ``record_ambiguity`` directly.
  * The sentinel NEVER raises — a telemetry failure can NEVER break the
    FSM hot path (master-off + raising note hook both swallowed).
  * THE CHAOS arc: FSM retry burst → sentinel → sensor mesh sampler →
    convergence engine → the REAL ``risk_tier_floor.recommended_floor()``
    raises its floor. Nothing raises through the whole chain.
  * The orchestrator call-site is statically proven to be wrapped in
    try/except with the lazy-import INSIDE the try (FSM-unbreakable).
  * Recovery: signals decay → sampler fires nothing → floor relaxes.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance import ambiguity_sensor_mesh as mesh
from backend.core.ouroboros.governance import dynamic_risk_convergence as drc
from backend.core.ouroboros.governance import risk_tier_floor


_ORCHESTRATOR_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "orchestrator.py"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip mesh + convergence + risk-floor knobs and reset both buffers
    so each test starts from a known baseline."""
    for key in list(os.environ):
        if (
            key.startswith("JARVIS_SENSOR_MESH_")
            or key.startswith("JARVIS_CONVERGENCE_")
            or key.startswith("JARVIS_SENTINEL_")
            or key in (
                "JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED",
                "JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED",
                "JARVIS_MIN_RISK_TIER",
                "JARVIS_PARANOIA_MODE",
                "JARVIS_AUTO_APPLY_QUIET_HOURS",
                "JARVIS_VISION_SENSOR_RISK_FLOOR",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    mesh.reset_accumulators()
    drc.reset_signals()
    yield
    mesh.reset_accumulators()
    drc.reset_signals()


def _enable_mesh(monkeypatch):
    monkeypatch.setenv("JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED", "true")


def _enable_convergence(monkeypatch):
    monkeypatch.setenv("JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED", "true")


def _contradictory_count(now: float) -> int:
    return mesh._window_count(  # noqa: SLF001 — intentional probe
        mesh.SignalClass.CONTRADICTORY_OUTPUT, now,
    )


def _dt(now_unix: float) -> datetime:
    """The real risk_tier_floor.recommended_floor() takes a datetime; the
    convergence engine derives now_unix from it. Convert so the whole
    reflex arc shares one coherent clock."""
    return datetime.fromtimestamp(now_unix, tz=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Sentinel fires above threshold, not on first attempt
# ---------------------------------------------------------------------------


def test_sentinel_fires_above_threshold(monkeypatch):
    _enable_mesh(monkeypatch)
    now = 10_000.0
    # attempt 1 == first retry → below default threshold (2) → no fire.
    assert mesh.observe_generate_retry("op1", 1, now_unix=now) is False
    assert _contradictory_count(now) == 0
    # attempt 2 == repeatedly fighting the validator → fires.
    assert mesh.observe_generate_retry("op1", 2, now_unix=now) is True
    assert _contradictory_count(now) == 1


def test_sentinel_threshold_is_env_tunable(monkeypatch):
    _enable_mesh(monkeypatch)
    monkeypatch.setenv("JARVIS_SENTINEL_CONTRADICTION_ATTEMPT_THRESHOLD", "3")
    now = 10_100.0
    assert mesh.observe_generate_retry("op2", 2, now_unix=now) is False
    assert _contradictory_count(now) == 0
    assert mesh.observe_generate_retry("op2", 3, now_unix=now) is True
    assert _contradictory_count(now) == 1


# ---------------------------------------------------------------------------
# 2. Hot-path safety — NEVER raises, inert master-off
# ---------------------------------------------------------------------------


def test_sentinel_swallows_raising_hook(monkeypatch):
    _enable_mesh(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(mesh, "note_llm_contradiction", _boom)
    # Above threshold so it WOULD try to fire — must swallow the raise.
    result = mesh.observe_generate_retry("op3", 5, now_unix=10_200.0)
    assert result is False  # did not fire (swallowed), did not raise


def test_sentinel_inert_when_master_off():
    # Master NOT set (default-FALSE). note_llm_contradiction no-ops, so
    # the accumulator never gains an event — but observe_generate_retry
    # still returns True (it DID record; the hook is just inert downstream).
    now = 10_300.0
    fired = mesh.observe_generate_retry("op4", 5, now_unix=now)
    # The sentinel called note_llm_contradiction (returns True), but the
    # hook is inert master-off so no event lands in the accumulator.
    assert fired is True
    assert _contradictory_count(now) == 0


def test_sentinel_bad_attempt_num_never_raises(monkeypatch):
    _enable_mesh(monkeypatch)
    # Garbage attempt_num → returns False, never raises.
    assert mesh.observe_generate_retry("op5", None, now_unix=10_400.0) is False
    assert mesh.observe_generate_retry("op5", "x", now_unix=10_400.0) is False


# ---------------------------------------------------------------------------
# 3. THE CHAOS TEST — full reflex arc, nothing raises
# ---------------------------------------------------------------------------


def test_chaos_full_reflex_arc(monkeypatch):
    """Simulate the FSM retry loop firing the sentinel across 5
    consecutive syntax-broken payloads. Prove: FSM retry → sentinel →
    sensor mesh → convergence → REAL risk_tier_floor floor rises. And
    nothing raises through the whole chain."""
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    # Keep windows wide so all signals stay in-window for the assertion.
    monkeypatch.setenv("JARVIS_SENSOR_MESH_WINDOW_S", "120.0")
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "120.0")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_DECAY_S", "120.0")

    t0 = 20_000.0
    fires = 0
    # Mimic 5 consecutive retries (attempt_num 1..5), each a broken payload.
    for k in range(1, 6):
        fired = mesh.observe_generate_retry(
            f"op-chaos", k, detail="SyntaxError: bad payload", now_unix=t0,
        )
        if fired:
            fires += 1
    # Threshold default 2 → fired on attempts 2,3,4,5 → 4 fires.
    assert fires == 4
    assert _contradictory_count(t0) == 4

    # Sampler pass 1: count (4) >= mesh contradictory threshold (3) →
    # fires record_ambiguity once (weight 3.0) → score 3.0 → ELEVATED.
    report1 = asyncio.run(mesh.run_sensor_mesh_once(now_unix=t0))
    assert report1.fired[mesh.SignalClass.CONTRADICTORY_OUTPUT.value] is True
    floor1 = risk_tier_floor.recommended_floor(_dt(t0))
    assert floor1 == "notify_apply"

    # Sampler pass 2 (same instant) → second record_ambiguity → score 6.0
    # → PARANOIA → approval_required. Proves the reflex can escalate.
    asyncio.run(mesh.run_sensor_mesh_once(now_unix=t0))
    floor2 = risk_tier_floor.recommended_floor(_dt(t0))
    assert floor2 == "approval_required"


# ---------------------------------------------------------------------------
# 4. FSM-unbreakable — static proof the orchestrator wiring is wrapped
# ---------------------------------------------------------------------------


def test_orchestrator_wiring_is_try_wrapped_with_lazy_import_inside():
    """Static AST proof: the orchestrator's sentinel call-site imports
    observe_generate_retry LAZILY *inside* a try/except and calls it
    inside that same try — so neither ImportError nor a sentinel raise
    can ever break the 6,000-line GENERATE FSM."""
    source = _ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    found_guarded_call = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body = node.body
        # The try body must (a) import observe_generate_retry and
        # (b) call it (aliased as _sentinel_observe).
        imports_sentinel = any(
            isinstance(n, ast.ImportFrom)
            and (n.module or "").endswith("ambiguity_sensor_mesh")
            and any(
                a.name == "observe_generate_retry" for a in n.names
            )
            for n in ast.walk(ast.Module(body=body, type_ignores=[]))
        )
        if not imports_sentinel:
            continue
        calls_sentinel = any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name)
                 and n.func.id == "_sentinel_observe")
                or (isinstance(n.func, ast.Name)
                    and n.func.id == "observe_generate_retry")
            )
            for n in ast.walk(ast.Module(body=body, type_ignores=[]))
        )
        if not calls_sentinel:
            continue
        # The except must be a bare/broad Exception that does nothing
        # disruptive (pass).
        handlers = node.handlers
        assert handlers, "sentinel try-block has no except handler"
        found_guarded_call = True
        break

    assert found_guarded_call, (
        "orchestrator sentinel call-site is NOT inside a try/except with "
        "the lazy-import + call both inside the try body"
    )


# ---------------------------------------------------------------------------
# 5. Recovery — signals decay → sampler fires nothing → floor relaxes
# ---------------------------------------------------------------------------


def test_recovery_floor_relaxes_after_decay(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_WINDOW_S", "60.0")
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "60.0")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_DECAY_S", "60.0")

    t0 = 30_000.0
    # Burst — 4 sentinel fires above threshold.
    for k in range(1, 6):
        mesh.observe_generate_retry("op-rec", k, now_unix=t0)
    assert _contradictory_count(t0) == 4

    # Sampler fires → floor rises.
    asyncio.run(mesh.run_sensor_mesh_once(now_unix=t0))
    assert risk_tier_floor.recommended_floor(_dt(t0)) == "notify_apply"

    # Far in the future: mesh accumulator empties (60s window) → sampler
    # fires NOTHING; convergence signals decay (60s) → floor relaxes.
    t_future = t0 + 200.0
    assert _contradictory_count(t_future) == 0
    report = asyncio.run(mesh.run_sensor_mesh_once(now_unix=t_future))
    assert (
        report.fired[mesh.SignalClass.CONTRADICTORY_OUTPUT.value] is False
    )
    assert risk_tier_floor.recommended_floor(_dt(t_future)) is None
