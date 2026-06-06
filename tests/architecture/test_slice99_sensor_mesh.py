"""Slice 99 — Asynchronous Ambiguity Sensor Mesh tests (live chaos matrix).

The load-bearing properties under test:

  * Producer hooks are tiny, best-effort, and NEVER break the hot path.
  * The async sampler is a PULL observer — it reads accumulators and
    fires ``record_ambiguity`` on a DYNAMIC threshold breach.
  * The convergence engine's per-signal decay preserves the
    pure-function-of-window recovery (no latch).
  * Two simultaneous live sources (LLM parse-loop + forced partition)
    both fire → the REAL ``risk_tier_floor.recommended_floor()`` reaches
    ``approval_required`` (paranoia) with zero missed signals.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from backend.core.ouroboros.governance import ambiguity_sensor_mesh as mesh
from backend.core.ouroboros.governance import dynamic_risk_convergence as drc
from backend.core.ouroboros.governance import risk_tier_floor


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


# ---------------------------------------------------------------------------
# 1. Per-signal decay in the convergence engine
# ---------------------------------------------------------------------------


def test_per_signal_decay_counts_then_drops(monkeypatch):
    _enable_convergence(monkeypatch)
    # Global window is 60s; this signal has its OWN 10s decay.
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "60.0")
    t0 = 1000.0
    drc.record_ambiguity(
        drc.AmbiguitySignal.CONTRADICTORY_OUTPUT,
        now_unix=t0,
        weight=1.0,
        decay_s=10.0,
    )
    # At t+5 (< 10s decay) it counts.
    assert drc.convergence_score(now_unix=t0 + 5) == pytest.approx(1.0)
    # At t+15 (> its OWN 10s decay, even though global window is 60s) it
    # drops — per-signal age vs per-signal decay, no latch.
    assert drc.convergence_score(now_unix=t0 + 15) == pytest.approx(0.0)


def test_decay_none_falls_back_to_global_window(monkeypatch):
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "60.0")
    t0 = 2000.0
    # No decay_s → legacy global-window behavior, byte-identical.
    drc.record_ambiguity(
        drc.AmbiguitySignal.CONTRADICTORY_OUTPUT, now_unix=t0,
    )
    assert drc.convergence_score(now_unix=t0 + 15) == pytest.approx(1.0)
    assert drc.convergence_score(now_unix=t0 + 61) == pytest.approx(0.0)


def test_decay_nonpositive_falls_back(monkeypatch):
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "60.0")
    t0 = 2500.0
    drc.record_ambiguity(
        drc.AmbiguitySignal.MALFORMED_INTENT, now_unix=t0, decay_s=0.0,
    )
    drc.record_ambiguity(
        drc.AmbiguitySignal.MALFORMED_INTENT, now_unix=t0, decay_s=-5.0,
    )
    # decay_s<=0 → treated as None → global window.
    assert drc.convergence_score(now_unix=t0 + 30) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 2. Producer hook → accumulator
# ---------------------------------------------------------------------------


def test_hook_appends_to_accumulator(monkeypatch):
    _enable_mesh(monkeypatch)
    now = 3000.0
    for _ in range(5):
        mesh.note_cross_repo_emit_failure("partition", now_unix=now)
    count = mesh._window_count(  # noqa: SLF001 — intentional probe
        mesh.SignalClass.CROSS_REPO_HANDSHAKE, now,
    )
    assert count == 5


def test_hook_inert_when_master_off():
    # Master NOT set (default-FALSE).
    now = 3100.0
    for _ in range(5):
        mesh.note_llm_contradiction("x", now_unix=now)
    count = mesh._window_count(  # noqa: SLF001
        mesh.SignalClass.CONTRADICTORY_OUTPUT, now,
    )
    assert count == 0


# ---------------------------------------------------------------------------
# 3. Sampler fires record_ambiguity on threshold breach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sampler_fires_on_threshold_breach(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_THRESHOLD", "3")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_WEIGHT", "6.0")
    now = 4000.0
    for _ in range(4):  # 4 >= 3 threshold
        mesh.note_llm_contradiction("retry-loop", now_unix=now)

    report = await mesh.run_sensor_mesh_once(now_unix=now)
    assert report.fired["contradictory_output"] is True
    # The convergence engine actually received the signal → floor rises.
    assert drc.convergence_score(now_unix=now) >= 6.0
    assert (
        drc.recommended_convergence_floor(now_unix=now)
        == "approval_required"
    )


@pytest.mark.asyncio
async def test_sampler_does_not_fire_below_threshold(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_THRESHOLD", "5")
    now = 4200.0
    for _ in range(2):  # 2 < 5 threshold
        mesh.note_llm_contradiction("x", now_unix=now)
    report = await mesh.run_sensor_mesh_once(now_unix=now)
    assert report.fired["contradictory_output"] is False
    assert drc.convergence_score(now_unix=now) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4. THE CHAOS TEST — two live sources fire simultaneously → paranoia
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chaos_dual_source_reaches_paranoia(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    # Each class fires with weight 3.0 → two sources = 6.0 = paranoia.
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_THRESHOLD", "3")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_HANDSHAKE_THRESHOLD", "3")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_WEIGHT", "3.0")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_HANDSHAKE_WEIGHT", "3.0")
    now = 5000.0

    # Simulate a LIVE LLM parsing loop (contradiction burst) AND a forced
    # partition (emit-failure burst) SIMULTANEOUSLY.
    for _ in range(5):
        mesh.note_llm_contradiction("parse-loop", now_unix=now)
    for _ in range(5):
        mesh.note_cross_repo_emit_failure("partition", now_unix=now)

    report = await mesh.run_sensor_mesh_once(now_unix=now)

    # BOTH sources fired — zero missed signals.
    assert report.fired["contradictory_output"] is True
    assert report.fired["cross_repo_handshake"] is True
    assert report.window_counts["contradictory_output"] == 5
    assert report.window_counts["cross_repo_handshake"] == 5

    # The REAL risk_tier_floor reaches approval_required (paranoia).
    monkeypatch.setattr(drc.time, "time", lambda: now)
    assert risk_tier_floor.recommended_floor() == "approval_required"


# ---------------------------------------------------------------------------
# 5. Recovery — after bursts stop + decay elapses, floor relaxes to None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_is_automatic_after_decay(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_THRESHOLD", "3")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_HANDSHAKE_THRESHOLD", "3")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_WEIGHT", "3.0")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_HANDSHAKE_WEIGHT", "3.0")
    # Per-class convergence decay 10s.
    monkeypatch.setenv("JARVIS_SENSOR_MESH_CONTRADICTORY_DECAY_S", "10.0")
    monkeypatch.setenv("JARVIS_SENSOR_MESH_HANDSHAKE_DECAY_S", "10.0")
    # Mesh sliding window short so accumulators self-clear too.
    monkeypatch.setenv("JARVIS_SENSOR_MESH_WINDOW_S", "10.0")
    now = 6000.0

    for _ in range(5):
        mesh.note_llm_contradiction("x", now_unix=now)
    for _ in range(5):
        mesh.note_cross_repo_emit_failure("x", now_unix=now)
    await mesh.run_sensor_mesh_once(now_unix=now)
    monkeypatch.setattr(drc.time, "time", lambda: now)
    assert risk_tier_floor.recommended_floor() == "approval_required"

    # Bursts STOP. Advance past both the mesh window AND the per-signal
    # convergence decay (10s each). NO reset call anywhere.
    later = now + 20.0
    report = await mesh.run_sensor_mesh_once(now_unix=later)
    # Accumulators aged out → nothing new fires.
    assert report.fired["contradictory_output"] is False
    assert report.fired["cross_repo_handshake"] is False
    # Convergence floor relaxes to None automatically (per-signal decay).
    assert drc.recommended_convergence_floor(now_unix=later) is None
    monkeypatch.setattr(drc.time, "time", lambda: later)
    assert risk_tier_floor.recommended_floor() is None


# ---------------------------------------------------------------------------
# 6. Hot-path safety — hooks swallow everything; emit unaffected
# ---------------------------------------------------------------------------


def test_hook_swallows_accumulator_raise(monkeypatch):
    _enable_mesh(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("accumulator exploded")

    # Force a layer INSIDE _append (the never-raises boundary) to blow
    # up; the public hooks must still swallow it and NEVER propagate.
    monkeypatch.setattr(mesh, "_ensure_capacity", _boom)
    mesh.note_cross_repo_emit_failure("x")
    mesh.note_llm_contradiction("x")
    mesh.note_malformed_intent("x")  # none of these may raise


@pytest.mark.asyncio
async def test_sampler_swallows_record_ambiguity_raise(monkeypatch):
    _enable_mesh(monkeypatch)
    _enable_convergence(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_MALFORMED_THRESHOLD", "1")
    now = 7000.0

    def _boom(*_a, **_k):
        raise RuntimeError("convergence exploded")

    monkeypatch.setattr(drc, "record_ambiguity", _boom)
    mesh.note_malformed_intent("x", now_unix=now)
    # The sampler fire path must swallow the convergence raise.
    report = await mesh.run_sensor_mesh_once(now_unix=now)
    # Count breached but the fire failed silently → fired False, no raise.
    assert report.fired["malformed_intent"] is False


def test_emit_ripple_unaffected_by_sensor_raise(monkeypatch):
    """emit_ripple must return its normal result even if the sensor hook
    raises — best-effort hook, hot-path safe."""
    import backend.core.ouroboros.cross_repo_mesh.ripple_emitter as re_mod
    from backend.core.ouroboros.governance import ambiguity_sensor_mesh

    def _boom(*_a, **_k):
        raise RuntimeError("sensor exploded")

    # Patch the LAZY-IMPORTED hook target so the emitter's
    # _note_emit_failure try/except is the boundary under test (the real
    # hot-path-safety guarantee). emit_ripple must still return normally.
    monkeypatch.setattr(
        ambiguity_sensor_mesh, "note_cross_repo_emit_failure", _boom,
    )
    monkeypatch.setenv("JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", "test-psk")

    def _bad_sign(*_a, **_k):
        raise ValueError("sign boom")

    monkeypatch.setattr(re_mod, "sign_ripple", _bad_sign)
    payload = re_mod.build_ripple(
        "test", "intent", {"a": 1}, now_unix=1.0,
    )
    result = asyncio.run(re_mod.emit_ripple(payload))
    assert result.verdict == re_mod.VerifyVerdict.DISABLED
    assert "sign_error" in (result.detail or "")


# ---------------------------------------------------------------------------
# 7. Loop bound + master-off no-op + garbage tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_terminates_on_iterations(monkeypatch):
    _enable_mesh(monkeypatch)
    monkeypatch.setenv("JARVIS_SENSOR_MESH_INTERVAL_S", "0")
    passes = await asyncio.wait_for(
        mesh.run_sensor_mesh_loop(iterations=3, interval_s=0.0),
        timeout=5.0,
    )
    assert passes == 3


@pytest.mark.asyncio
async def test_loop_master_off_is_noop(monkeypatch):
    # Master off → passes run but nothing fires.
    monkeypatch.setenv("JARVIS_SENSOR_MESH_INTERVAL_S", "0")
    now = 8000.0
    for _ in range(10):
        mesh.note_llm_contradiction("x", now_unix=now)
    passes = await mesh.run_sensor_mesh_loop(iterations=2, interval_s=0.0)
    assert passes == 2
    report = await mesh.run_sensor_mesh_once(now_unix=now)
    assert report.any_fired is False


@pytest.mark.asyncio
async def test_run_once_never_raises_on_garbage(monkeypatch):
    _enable_mesh(monkeypatch)
    # Garbage now_unix → tolerated.
    report = await mesh.run_sensor_mesh_once(now_unix="not-a-number")  # type: ignore[arg-type]
    assert isinstance(report, mesh.SensorMeshReport)


def test_report_to_dict_roundtrip(monkeypatch):
    _enable_mesh(monkeypatch)
    now = 8500.0
    report = asyncio.run(mesh.run_sensor_mesh_once(now_unix=now))
    d = report.to_dict()
    assert d["schema_version"] == mesh.SENSOR_MESH_SCHEMA_VERSION
    assert set(d["window_counts"]) == {c.value for c in mesh.SignalClass}
    assert "fired" in d and "weights" in d and "decays" in d
    assert "thresholds" in d and "evaluated_at_unix" in d


# ---------------------------------------------------------------------------
# 8. AST pins + FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_ast_pins_canonical_pass():
    pins = mesh.register_shipped_invariants()
    assert len(pins) >= 4
    with open(mesh.__file__, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    for pin in pins:
        result = pin.validate(tree, source)
        assert result == (), f"{pin.invariant_name} failed: {result}"


def test_ast_pin_class_taxonomy_regression():
    pins = {p.invariant_name: p for p in mesh.register_shipped_invariants()}
    pin = pins["sensor_mesh_class_taxonomy_closed"]
    bad = (
        "import enum\n"
        "class SignalClass(str, enum.Enum):\n"
        "    CROSS_REPO_HANDSHAKE = 'cross_repo_handshake'\n"
        "    CONTRADICTORY_OUTPUT = 'contradictory_output'\n"  # malformed removed
    )
    tree = ast.parse(bad)
    assert pin.validate(tree, bad) != ()


def test_ast_pin_authority_regression():
    pins = {p.invariant_name: p for p in mesh.register_shipped_invariants()}
    name = next(n for n in pins if "authority" in n)
    pin = pins[name]
    bad = (
        "from backend.core.ouroboros.governance.orchestrator import foo\n"
    )
    tree = ast.parse(bad)
    assert pin.validate(tree, bad) != ()


def test_ast_pin_master_default_false_regression():
    pins = {p.invariant_name: p for p in mesh.register_shipped_invariants()}
    name = next(n for n in pins if "master" in n)
    pin = pins[name]
    bad = (
        "def master_enabled():\n"
        "    return _flag('X', default=True)\n"
    )
    tree = ast.parse(bad)
    assert pin.validate(tree, bad) != ()


def test_ast_pin_pull_model_regression():
    """If a producer hook calls record_ambiguity directly (push from the
    hot path) the PULL-model pin must trip."""
    pins = {p.invariant_name: p for p in mesh.register_shipped_invariants()}
    pin = pins["sensor_mesh_hooks_pull_model"]
    bad = (
        "def note_llm_contradiction(detail=''):\n"
        "    drc.record_ambiguity(SIG)\n"
    )
    tree = ast.parse(bad)
    assert pin.validate(tree, bad) != ()


def test_flag_registry_seed_count():
    class _Reg:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = _Reg()
    count = mesh.register_flags(reg)
    assert count == len(reg.registered)
    assert count >= 1
    names = {s.name for s in reg.registered}
    assert "JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED" in names


# ---------------------------------------------------------------------------
# 9. Live emit-side wiring — emit failure feeds the accumulator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_failure_feeds_handshake_accumulator(monkeypatch):
    """End-to-end: a real emit_ripple sign failure increments the
    handshake accumulator via the wired hook."""
    _enable_mesh(monkeypatch)
    import backend.core.ouroboros.cross_repo_mesh.ripple_emitter as re_mod

    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", "test-psk")
    now = 9000.0

    def _bad_sign(*_a, **_k):
        raise ValueError("sign boom")

    monkeypatch.setattr(re_mod, "sign_ripple", _bad_sign)
    payload = re_mod.build_ripple("test", "intent", {"a": 1}, now_unix=now)

    before = mesh._window_count(  # noqa: SLF001
        mesh.SignalClass.CROSS_REPO_HANDSHAKE, now,
    )
    result = await re_mod.emit_ripple(payload)
    assert result.verdict == re_mod.VerifyVerdict.DISABLED
    after = mesh._window_count(  # noqa: SLF001
        mesh.SignalClass.CROSS_REPO_HANDSHAKE, now,
    )
    assert after == before + 1
