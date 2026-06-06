"""Slice 98 Phase 2 — Dynamic Risk-State Convergence Engine tests.

The load-bearing property under test is RECOVERY: the recommended
floor is a PURE FUNCTION of the current ambiguity window, not a
latched state machine. When signals age out, the floor drops
automatically with zero manual reset.
"""
from __future__ import annotations

import importlib
import os

import pytest

from backend.core.ouroboros.governance import dynamic_risk_convergence as drc
from backend.core.ouroboros.governance import risk_tier_floor


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all convergence + risk-floor env knobs so each test starts
    from a known baseline, and reset the module signal buffer."""
    for key in list(os.environ):
        if key.startswith("JARVIS_CONVERGENCE_") or key in (
            "JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED",
            "JARVIS_MIN_RISK_TIER",
            "JARVIS_PARANOIA_MODE",
            "JARVIS_AUTO_APPLY_QUIET_HOURS",
            "JARVIS_VISION_SENSOR_RISK_FLOOR",
        ):
            monkeypatch.delenv(key, raising=False)
    drc.reset_signals()
    yield
    drc.reset_signals()


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED", "true")


# ---------------------------------------------------------------------------
# 1. Throttle on ambiguity — score → floor mapping
# ---------------------------------------------------------------------------


def test_throttle_paranoia_band(monkeypatch):
    _enable(monkeypatch)
    now = 1000.0
    # 6 handshake failures (weight 1.0 each) == paranoia threshold.
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    assert drc.convergence_score(now_unix=now) == pytest.approx(6.0)
    assert drc.recommended_convergence_floor(now_unix=now) == "approval_required"
    verdict = drc.convergence_verdict(now_unix=now)
    assert verdict.band is drc.ConvergenceBand.PARANOIA
    assert verdict.recommended_floor == "approval_required"


def test_throttle_elevated_band(monkeypatch):
    _enable(monkeypatch)
    now = 1000.0
    # 4 signals → between elevated (3.0) and paranoia (6.0).
    for _ in range(4):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CONTRADICTORY_OUTPUT, now_unix=now,
        )
    assert drc.recommended_convergence_floor(now_unix=now) == "notify_apply"
    assert drc.convergence_verdict(now_unix=now).band is drc.ConvergenceBand.ELEVATED


def test_below_elevated_is_none(monkeypatch):
    _enable(monkeypatch)
    now = 1000.0
    drc.record_ambiguity(
        drc.AmbiguitySignal.MALFORMED_INTENT, now_unix=now,
    )
    drc.record_ambiguity(
        drc.AmbiguitySignal.MALFORMED_INTENT, now_unix=now,
    )
    # 2 < elevated threshold 3.0
    assert drc.recommended_convergence_floor(now_unix=now) is None
    assert drc.convergence_verdict(now_unix=now).band is drc.ConvergenceBand.NORMAL


# ---------------------------------------------------------------------------
# 2. Mutation-prevention while active (compose through real risk_tier_floor)
# ---------------------------------------------------------------------------


def test_mutation_prevention_via_real_floor(monkeypatch):
    _enable(monkeypatch)
    now = 2000.0
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    # Patch time.time so risk_tier_floor (which uses None → time.time())
    # sees the same window.
    monkeypatch.setattr(drc.time, "time", lambda: now)
    floor = risk_tier_floor.recommended_floor()
    assert floor == "approval_required"  # forbids SAFE_AUTO


# ---------------------------------------------------------------------------
# 3. THE RECOVERY TEST (load-bearing) — automatic, zero manual reset
# ---------------------------------------------------------------------------


def test_recovery_is_automatic_pure_function(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "60.0")
    t0 = 5000.0
    for _ in range(8):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=t0,
        )
    # Breaching paranoia at t0.
    assert drc.recommended_convergence_floor(now_unix=t0) == "approval_required"
    assert drc.convergence_verdict(now_unix=t0).band is drc.ConvergenceBand.PARANOIA

    # Now advance past the window — NO new signals, NO reset_signals() call.
    later = t0 + 60.0 + 1.0
    assert drc.recommended_convergence_floor(now_unix=later) is None
    assert drc.convergence_verdict(now_unix=later).band is drc.ConvergenceBand.NORMAL
    assert drc.convergence_score(now_unix=later) == pytest.approx(0.0)
    # The buffer was NEVER manually reset — prove the signals are still in
    # the deque but simply don't count (pure-function-of-window).
    assert len(drc._SIGNALS) == 8  # noqa: SLF001 — intentional invariant probe


# ---------------------------------------------------------------------------
# 4. Transient blip recovers purely from time advancing
# ---------------------------------------------------------------------------


def test_transient_blip_recovers(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERGENCE_WINDOW_S", "30.0")
    burst_t = 9000.0
    for _ in range(7):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CONTRADICTORY_OUTPUT, now_unix=burst_t,
        )
    assert drc.convergence_verdict(now_unix=burst_t).band is drc.ConvergenceBand.PARANOIA
    # Mid-window still elevated/paranoia.
    assert drc.recommended_convergence_floor(now_unix=burst_t + 15) is not None
    # After silence beyond the window → NORMAL, purely from time.
    assert drc.recommended_convergence_floor(now_unix=burst_t + 31) is None
    assert drc.convergence_verdict(now_unix=burst_t + 31).band is drc.ConvergenceBand.NORMAL


# ---------------------------------------------------------------------------
# 5. Master OFF → always None, byte-identical risk_tier_floor
# ---------------------------------------------------------------------------


def test_master_off_always_none(monkeypatch):
    # Master flag NOT set (default-FALSE).
    now = 1000.0
    for _ in range(20):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    assert drc.recommended_convergence_floor(now_unix=now) is None
    v = drc.convergence_verdict(now_unix=now)
    assert v.band is drc.ConvergenceBand.NORMAL
    assert v.recommended_floor is None


def test_master_off_risk_floor_byte_identical(monkeypatch):
    now = 3000.0
    for _ in range(20):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    monkeypatch.setattr(drc.time, "time", lambda: now)
    # With convergence master OFF (default), recommended_floor with no
    # other knobs returns None — exactly as if the engine didn't exist.
    assert risk_tier_floor.recommended_floor() is None


# ---------------------------------------------------------------------------
# 6. Composition strictest-wins
# ---------------------------------------------------------------------------


def test_composition_strictest_wins_env_stronger(monkeypatch):
    _enable(monkeypatch)
    now = 4000.0
    # Convergence at elevated (notify_apply).
    for _ in range(4):
        drc.record_ambiguity(
            drc.AmbiguitySignal.MALFORMED_INTENT, now_unix=now,
        )
    assert drc.recommended_convergence_floor(now_unix=now) == "notify_apply"
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    monkeypatch.setattr(drc.time, "time", lambda: now)
    # Env approval_required is stricter → wins.
    assert risk_tier_floor.recommended_floor() == "approval_required"


def test_composition_convergence_stronger(monkeypatch):
    _enable(monkeypatch)
    now = 4100.0
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    # Convergence at paranoia (approval_required) + weaker env.
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    monkeypatch.setattr(drc.time, "time", lambda: now)
    assert risk_tier_floor.recommended_floor() == "approval_required"


# ---------------------------------------------------------------------------
# 7. never-raises — convergence failure cannot break the floor
# ---------------------------------------------------------------------------


def test_garbage_signal_input_never_raises(monkeypatch):
    _enable(monkeypatch)
    # Garbage / wrong-typed signal should be tolerated.
    drc.record_ambiguity("not-an-enum", now_unix=1.0)  # type: ignore[arg-type]
    drc.record_ambiguity(None, now_unix=1.0)  # type: ignore[arg-type]
    # Must not raise on scoring / verdict.
    drc.convergence_score(now_unix=1.0)
    drc.convergence_verdict(now_unix=1.0)


def test_floor_robust_to_convergence_raise(monkeypatch):
    """If the convergence function raises, recommended_floor must still
    return the OTHER candidates — the floor stays robust."""
    _enable(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("convergence exploded")

    monkeypatch.setattr(
        drc, "recommended_convergence_floor", _boom,
    )
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    # The env floor must survive even though the convergence call raises.
    assert risk_tier_floor.recommended_floor() == "notify_apply"


def test_broken_broker_never_raises(monkeypatch):
    _enable(monkeypatch)
    # Force the lazy SSE publish path to blow up; band transition must
    # still not raise.
    import backend.core.ouroboros.governance.ide_observability_stream as stream

    def _boom_broker():
        raise RuntimeError("broker down")

    monkeypatch.setattr(stream, "get_default_broker", _boom_broker)
    now = 7000.0
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    # This triggers a NORMAL → PARANOIA transition → SSE publish attempt.
    drc.convergence_verdict(now_unix=now)  # must not raise


# ---------------------------------------------------------------------------
# 8. SSE fires only on band transition; AST pins; FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_sse_fires_only_on_band_transition(monkeypatch):
    _enable(monkeypatch)
    published: list = []

    class _FakeBroker:
        def publish(self, event_type, op_id, payload=None):
            published.append((event_type, payload))
            return "evt"

    import backend.core.ouroboros.governance.ide_observability_stream as stream
    monkeypatch.setattr(stream, "get_default_broker", lambda: _FakeBroker())
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)

    drc.reset_signals()  # also resets last-emitted band tracker
    now = 8000.0
    # First evaluation at NORMAL — no signals → no transition (NORMAL is
    # the initial band) → no publish.
    drc.convergence_verdict(now_unix=now)
    assert len(published) == 0

    # Breach into PARANOIA → one transition publish.
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    drc.convergence_verdict(now_unix=now)
    assert len(published) == 1
    assert published[0][0] == "schelling_convergence_changed"

    # Re-evaluate at same band → NO additional publish (transition-only).
    drc.convergence_verdict(now_unix=now)
    assert len(published) == 1

    # Recover to NORMAL (window aged out) → one more transition publish.
    drc.convergence_verdict(now_unix=now + 1000.0)
    assert len(published) == 2


def test_ast_pins_canonical_pass():
    pins = drc.register_shipped_invariants()
    assert len(pins) >= 4
    src_path = drc.__file__
    with open(src_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    import ast as _ast
    tree = _ast.parse(source)
    for pin in pins:
        result = pin.validate(tree, source)
        assert result == (), f"{pin.invariant_name} failed on canonical: {result}"


def test_ast_pin_taxonomy_regression():
    """Synthetic regression: removing a verdict member must trip the
    taxonomy pin."""
    pins = {p.invariant_name: p for p in drc.register_shipped_invariants()}
    pin = pins["convergence_band_taxonomy_closed"]
    import ast as _ast
    bad_source = (
        "import enum\n"
        "class ConvergenceBand(str, enum.Enum):\n"
        "    NORMAL = 'normal'\n"
        "    ELEVATED = 'elevated'\n"  # PARANOIA removed
        "class AmbiguitySignal(str, enum.Enum):\n"
        "    CROSS_REPO_HANDSHAKE_FAILURE = 'cross_repo_handshake_failure'\n"
        "    CONTRADICTORY_OUTPUT = 'contradictory_output'\n"
        "    MALFORMED_INTENT = 'malformed_intent'\n"
    )
    tree = _ast.parse(bad_source)
    result = pin.validate(tree, bad_source)
    assert result != ()


def test_ast_pin_authority_regression():
    pins = {p.invariant_name: p for p in drc.register_shipped_invariants()}
    name = next(n for n in pins if "authority" in n)
    pin = pins[name]
    import ast as _ast
    bad_source = (
        "from backend.core.ouroboros.governance.orchestrator import foo\n"
    )
    tree = _ast.parse(bad_source)
    result = pin.validate(tree, bad_source)
    assert result != ()


def test_ast_pin_master_default_false_regression():
    pins = {p.invariant_name: p for p in drc.register_shipped_invariants()}
    name = next(n for n in pins if "master" in n or "default" in n)
    pin = pins[name]
    import ast as _ast
    bad_source = (
        "def master_enabled():\n"
        "    return _flag('X', default=True)\n"  # should be False
    )
    tree = _ast.parse(bad_source)
    result = pin.validate(tree, bad_source)
    assert result != ()


def test_flag_registry_seed_count():
    from backend.core.ouroboros.governance import flag_registry

    class _Reg:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = _Reg()
    count = drc.register_flags(reg)
    assert count == len(reg.registered)
    assert count >= 1
    names = {s.name for s in reg.registered}
    assert "JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED" in names


def test_verdict_to_dict_roundtrip(monkeypatch):
    _enable(monkeypatch)
    now = 1234.0
    for _ in range(6):
        drc.record_ambiguity(
            drc.AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE, now_unix=now,
        )
    d = drc.convergence_verdict(now_unix=now).to_dict()
    assert d["band"] == "paranoia"
    assert d["recommended_floor"] == "approval_required"
    assert d["score"] == pytest.approx(6.0)
    assert d["signal_counts"]["cross_repo_handshake_failure"] == 6
    assert "schema_version" in d
    assert "evaluated_at_unix" in d
