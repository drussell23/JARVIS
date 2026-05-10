"""§39 Tier-5 (PRD v2.74 to v2.75, 2026-05-09) -
embodied surfaces regression spine: architecture_viz +
confidence_aura + attention_mirror + procedural_portrait.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tier5(monkeypatch):
    for var in (
        "JARVIS_ARCHITECTURE_VIZ_ENABLED",
        "JARVIS_CONFIDENCE_AURA_ENABLED",
        "JARVIS_ATTENTION_MIRROR_ENABLED",
        "JARVIS_ATTENTION_MIRROR_WINDOW_S",
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ============================================ #5 architecture viz


def test_arch_master_default_false():
    from backend.core.ouroboros.governance.architecture_viz import (
        master_enabled,
    )
    assert master_enabled() is False


def test_arch_zone_taxonomy_8():
    from backend.core.ouroboros.governance.architecture_viz import (
        OrganismZone,
    )
    assert {m.name for m in OrganismZone} == {
        "Z0_BOOT", "Z1_EVENT_STREAM", "Z2_REPL",
        "Z3_SENSORS", "Z4_INTAKE", "Z5_GOVERNANCE",
        "Z6_OUROBOROS", "Z7_CONSCIOUSNESS",
    }


def test_arch_aggregate_master_off():
    from backend.core.ouroboros.governance.architecture_viz import (
        aggregate_architecture_snapshot,
    )
    snap = aggregate_architecture_snapshot()
    assert snap.cells == ()


def test_arch_aggregate_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ARCHITECTURE_VIZ_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.architecture_viz import (
        OrganismZone, aggregate_architecture_snapshot,
    )
    snap = aggregate_architecture_snapshot()
    # 8 cells, one per zone
    assert len(snap.cells) == 8
    seen = {c.zone for c in snap.cells}
    assert seen == set(OrganismZone)


def test_arch_format_master_off():
    from backend.core.ouroboros.governance.architecture_viz import (
        format_architecture_viz,
    )
    assert format_architecture_viz() == ""


def test_arch_format_master_on_renders(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ARCHITECTURE_VIZ_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.architecture_viz import (
        format_architecture_viz,
    )
    out = format_architecture_viz()
    assert "Organism architecture" in out
    assert "Z0 Boot" in out
    assert "Z7 Consciousness" in out


def test_arch_pins():
    from backend.core.ouroboros.governance.architecture_viz import (
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    assert len(pins) == 4
    src = Path(
        "backend/core/ouroboros/governance/"
        "architecture_viz.py"
    ).read_text()
    tree = ast.parse(src)
    for pin in pins:
        violations = pin.validate(tree, src)
        assert not violations, (
            f"{pin.invariant_name}: {violations}"
        )


def test_arch_pin_zone_taxonomy_fires():
    from backend.core.ouroboros.governance.architecture_viz import (
        register_shipped_invariants,
    )
    pin = next(
        p for p in register_shipped_invariants()
        if "zone_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class OrganismZone(str, enum.Enum):\n"
        "    Z0_BOOT = 'z0_boot'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_arch_register_flags_count():
    from backend.core.ouroboros.governance.architecture_viz import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 1


# ============================================ #15 confidence aura


def test_aura_master_default_false():
    from backend.core.ouroboros.governance.confidence_aura import (
        master_enabled,
    )
    assert master_enabled() is False


def test_aura_tier_taxonomy_4():
    from backend.core.ouroboros.governance.confidence_aura import (
        ConfidenceTier,
    )
    assert {m.name for m in ConfidenceTier} == {
        "CERTAIN", "CONFIDENT", "UNCERTAIN", "SCATTERED",
    }


@pytest.mark.parametrize(
    "margin,expected", [
        (4.5, "CERTAIN"),
        (4.0, "CERTAIN"),
        (3.99, "CONFIDENT"),
        (2.0, "CONFIDENT"),
        (1.99, "UNCERTAIN"),
        (0.5, "UNCERTAIN"),
        (0.49, "SCATTERED"),
        (-1.0, "SCATTERED"),
        (None, "SCATTERED"),
    ],
)
def test_tier_for_margin(margin, expected):
    from backend.core.ouroboros.governance.confidence_aura import (
        ConfidenceTier, _tier_for_margin,
    )
    assert _tier_for_margin(margin) is getattr(
        ConfidenceTier, expected,
    )


def test_tier_for_nan_is_scattered():
    """NaN comparisons fail the >= test → fall through
    to SCATTERED (the post-loop default)."""
    from backend.core.ouroboros.governance.confidence_aura import (
        ConfidenceTier, _tier_for_margin,
    )
    assert _tier_for_margin(float("nan")) is ConfidenceTier.SCATTERED


def test_aura_aggregate_master_off():
    from backend.core.ouroboros.governance.confidence_aura import (
        aggregate_aura,
    )
    snap = aggregate_aura(None)
    assert snap.tokens == ()


def test_aura_aggregate_real_trace(monkeypatch):
    """Compose canonical ConfidenceTrace with synthetic
    tokens; verify per-token tier assignment."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_AURA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.confidence_capture import (  # noqa: E501
        ConfidenceToken, ConfidenceTrace,
    )
    from backend.core.ouroboros.governance.confidence_aura import (
        ConfidenceTier, aggregate_aura,
    )
    trace = ConfidenceTrace(
        tokens=(
            # margin 4.9 → CERTAIN
            ConfidenceToken(
                token="x", logprob=-0.1,
                top_logprobs=(
                    ("x", -0.1), ("y", -5.0),
                ),
            ),
            # margin 1.0 → UNCERTAIN
            ConfidenceToken(
                token="y", logprob=-1.0,
                top_logprobs=(
                    ("y", -1.0), ("z", -2.0),
                ),
            ),
            # margin 0.2 → SCATTERED
            ConfidenceToken(
                token="z", logprob=-3.0,
                top_logprobs=(
                    ("z", -3.0), ("w", -3.2),
                ),
            ),
        ),
        provider="test",
    )
    snap = aggregate_aura(trace)
    assert len(snap.tokens) == 3
    tiers = [t.tier for t in snap.tokens]
    assert tiers == [
        ConfidenceTier.CERTAIN,
        ConfidenceTier.UNCERTAIN,
        ConfidenceTier.SCATTERED,
    ]
    assert snap.provider == "test"


def test_aura_format_master_off():
    from backend.core.ouroboros.governance.confidence_aura import (
        format_aura_summary,
    )
    assert format_aura_summary(None) == ""


def test_aura_format_renders_tints(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_AURA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.confidence_aura import (
        AuraSnapshot, AuraToken, ConfidenceTier,
        format_aura_summary,
    )
    snap = AuraSnapshot(
        provider="test",
        tokens=(
            AuraToken(
                text="hi", tier=ConfidenceTier.CERTAIN,
                margin=5.0,
            ),
        ),
        by_tier={"certain": 1},
    )
    out = format_aura_summary(snap)
    assert "Confidence aura" in out
    assert "█" in out  # CERTAIN glyph
    assert "on green" in out  # CERTAIN tint


def test_aura_pins():
    from backend.core.ouroboros.governance.confidence_aura import (
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    assert len(pins) == 4
    src = Path(
        "backend/core/ouroboros/governance/"
        "confidence_aura.py"
    ).read_text()
    tree = ast.parse(src)
    for pin in pins:
        violations = pin.validate(tree, src)
        assert not violations, (
            f"{pin.invariant_name}: {violations}"
        )


def test_aura_pin_thresholds_fires():
    from backend.core.ouroboros.governance.confidence_aura import (
        register_shipped_invariants,
    )
    pin = next(
        p for p in register_shipped_invariants()
        if "thresholds_canonical" in p.invariant_name
    )
    bad = "x = 1\n"  # no _MARGIN_THRESHOLDS
    assert pin.validate(ast.parse(bad), bad)


def test_aura_register_flags_count():
    from backend.core.ouroboros.governance.confidence_aura import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 1


# ============================================ #16 attention mirror


def test_attention_master_default_false():
    from backend.core.ouroboros.governance.attention_mirror import (
        master_enabled,
    )
    assert master_enabled() is False


def test_attention_focus_taxonomy_4():
    from backend.core.ouroboros.governance.attention_mirror import (
        AttentionFocus,
    )
    assert {m.name for m in AttentionFocus} == {
        "READING", "SEARCHING", "THINKING", "IDLE",
    }


def test_attention_aggregate_master_off():
    from backend.core.ouroboros.governance.attention_mirror import (
        AttentionFocus, aggregate_attention,
    )
    snap = aggregate_attention()
    assert snap.primary_focus is AttentionFocus.IDLE
    assert snap.items == ()


def test_attention_aggregate_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.attention_mirror import (
        aggregate_attention,
    )
    # No active broker events → IDLE primary focus.
    snap = aggregate_attention()
    assert snap.window_s == 30


def test_attention_format_master_off():
    from backend.core.ouroboros.governance.attention_mirror import (
        format_attention_mirror,
    )
    assert format_attention_mirror() == ""


def test_attention_format_idle(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.attention_mirror import (
        format_attention_mirror,
    )
    out = format_attention_mirror()
    # Idle case renders informational stub
    assert "Attention mirror" in out
    assert "idle" in out.lower()


def test_attention_format_with_signals(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.attention_mirror import (
        AttentionFocus, AttentionItem, AttentionSnapshot,
        format_attention_mirror,
    )
    snap = AttentionSnapshot(
        primary_focus=AttentionFocus.READING,
        items=(
            AttentionItem(
                focus=AttentionFocus.READING,
                summary="read_file orchestrator.py",
                op_id="op-1",
                observed_at_unix=1000.0,
            ),
        ),
        aggregated_at_unix=1000.0,
        window_s=30,
    )
    out = format_attention_mirror(snapshot=snap)
    assert "📖" in out
    assert "orchestrator.py" in out


def test_attention_window_clamped(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_WINDOW_S", "9999",
    )
    from backend.core.ouroboros.governance.attention_mirror import (
        aggregate_attention,
    )
    snap = aggregate_attention()
    assert snap.window_s == 300  # MAX clamp


def test_attention_pins():
    from backend.core.ouroboros.governance.attention_mirror import (
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    assert len(pins) == 4
    src = Path(
        "backend/core/ouroboros/governance/"
        "attention_mirror.py"
    ).read_text()
    tree = ast.parse(src)
    for pin in pins:
        violations = pin.validate(tree, src)
        assert not violations, (
            f"{pin.invariant_name}: {violations}"
        )


def test_attention_register_flags_count():
    from backend.core.ouroboros.governance.attention_mirror import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 2


# ============================================ #17 procedural portrait


def test_portrait_master_default_false():
    from backend.core.ouroboros.governance.procedural_portrait import (
        master_enabled,
    )
    assert master_enabled() is False


def test_portrait_mode_taxonomy_3():
    from backend.core.ouroboros.governance.procedural_portrait import (
        PortraitMode,
    )
    assert {m.name for m in PortraitMode} == {
        "AT_REST", "WORKING", "ALERT",
    }


@pytest.mark.parametrize(
    "mood,posture,expected", [
        ("emergency", "consolidate", "ALERT"),
        ("struggling", "explore", "ALERT"),
        ("neutral", "harden", "ALERT"),
        ("neutral", "maintain", "AT_REST"),
        ("", "", "AT_REST"),
        ("confident", "consolidate", "WORKING"),
        ("confident", "explore", "WORKING"),
    ],
)
def test_mode_for_inputs(mood, posture, expected):
    from backend.core.ouroboros.governance.procedural_portrait import (
        PortraitMode, _mode_for_inputs,
    )
    assert _mode_for_inputs(
        mood_label=mood, posture_label=posture,
    ) is getattr(PortraitMode, expected)


def test_portrait_aggregate_master_off():
    from backend.core.ouroboros.governance.procedural_portrait import (
        PortraitMode, aggregate_portrait,
    )
    state = aggregate_portrait()
    assert state.mode is PortraitMode.AT_REST
    assert state.face == ()


def test_portrait_aggregate_master_on_deterministic(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.procedural_portrait import (
        aggregate_portrait,
    )
    # Same inputs → same seed → same face glyphs.
    s1 = aggregate_portrait(
        mood_label="emergency", posture_label="harden",
        heartbeat_glyph="♥",
    )
    s2 = aggregate_portrait(
        mood_label="emergency", posture_label="harden",
        heartbeat_glyph="♥",
    )
    assert s1.face == s2.face
    assert s1.seed == s2.seed
    # Different inputs → different face.
    s3 = aggregate_portrait(
        mood_label="confident", posture_label="consolidate",
        heartbeat_glyph="♥",
    )
    assert s3.face != s1.face


def test_portrait_alert_mode(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.procedural_portrait import (
        PortraitMode, aggregate_portrait,
    )
    state = aggregate_portrait(
        mood_label="emergency", posture_label="harden",
        heartbeat_glyph="♥",
    )
    assert state.mode is PortraitMode.ALERT
    # Face has 4 lines + box decoration
    assert len(state.face) == 4


def test_portrait_format_master_off():
    from backend.core.ouroboros.governance.procedural_portrait import (
        format_portrait,
    )
    assert format_portrait() == ""


def test_portrait_format_renders(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.procedural_portrait import (
        aggregate_portrait, format_portrait,
    )
    state = aggregate_portrait(
        mood_label="confident", posture_label="consolidate",
        heartbeat_glyph="♥",
    )
    out = format_portrait(state)
    assert "Procedural portrait" in out
    assert "┌─────┐" in out
    assert "└─────┘" in out
    assert "♥" in out


def test_portrait_pins():
    from backend.core.ouroboros.governance.procedural_portrait import (
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    assert len(pins) == 4
    src = Path(
        "backend/core/ouroboros/governance/"
        "procedural_portrait.py"
    ).read_text()
    tree = ast.parse(src)
    for pin in pins:
        violations = pin.validate(tree, src)
        assert not violations, (
            f"{pin.invariant_name}: {violations}"
        )


def test_portrait_register_flags_count():
    from backend.core.ouroboros.governance.procedural_portrait import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 1


# ============================================ /embodied REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/something")
    assert r.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied help")
    assert r.ok is True
    assert "arch" in r.text
    assert "portrait" in r.text


def test_repl_status():
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied status")
    assert r.ok is True


def test_repl_arch_master_off():
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied arch")
    assert r.ok is False


def test_repl_arch_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ARCHITECTURE_VIZ_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied arch")
    assert r.ok is True


def test_repl_attention_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ATTENTION_MIRROR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied attention")
    assert r.ok is True


def test_repl_portrait_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied portrait")
    assert r.ok is True


def test_repl_aura_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_AURA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied aura")
    # Aura without active trace renders an info stub
    assert r.ok is True


def test_repl_all(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ARCHITECTURE_VIZ_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PROCEDURAL_PORTRAIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied all")
    assert r.ok is True


def test_repl_unknown():
    from backend.core.ouroboros.governance.embodied_repl import (
        dispatch_embodied_command,
    )
    r = dispatch_embodied_command("/embodied bogus")
    assert r.ok is False


# ============================================ Canonical SSE smokes


def test_canonical_event_architecture_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_ARCHITECTURE_SNAPSHOT, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_ARCHITECTURE_SNAPSHOT in _VALID_EVENT_TYPES


def test_canonical_event_aura_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CONFIDENCE_AURA_RENDERED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_CONFIDENCE_AURA_RENDERED in _VALID_EVENT_TYPES


def test_canonical_event_attention_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_ATTENTION_MIRROR_UPDATED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_ATTENTION_MIRROR_UPDATED in _VALID_EVENT_TYPES


def test_canonical_event_portrait_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PORTRAIT_RENDERED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_PORTRAIT_RENDERED in _VALID_EVENT_TYPES


def test_canonical_confidence_token_margin_callable():
    """Lockstep — aura depends on margin_top1_top2."""
    from backend.core.ouroboros.governance.verification.confidence_capture import (
        ConfidenceToken,
    )
    tok = ConfidenceToken(
        token="x", logprob=-1.0,
        top_logprobs=(("x", -1.0), ("y", -3.0)),
    )
    margin = tok.margin_top1_top2()
    assert margin == 2.0
