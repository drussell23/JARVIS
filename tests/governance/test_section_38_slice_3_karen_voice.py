"""Section 38 Slice 3 (PRD v2.59 to v2.60, 2026-05-07) -
Karen voice + barge-in regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_3(monkeypatch):
    monkeypatch.delenv("JARVIS_KAREN_VOICE_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_COOLDOWN_S", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_MIN_TIER", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_PERSONA", raising=False,
    )
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    kva.reset_announcer_for_tests()
    yield
    kva.reset_announcer_for_tests()


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", value)
    assert master_enabled() is True


# Closed taxonomy


def test_tier_taxonomy_4_values():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        VoiceEventTier,
    )
    assert {m.name for m in VoiceEventTier} == {
        "CRITICAL", "IMPORTANT", "NORMAL", "SILENT",
    }


# tier_for_event mapping


@pytest.mark.parametrize(
    "event_type,expected_tier",
    [
        ("governor_emergency_brake", "critical"),
        ("posture_changed", "important"),
        ("behavioral_drift_detected", "important"),
        ("cost_band_crossed", "important"),
        ("memory_pressure_changed", "important"),
        ("task_completed", "normal"),
        ("plan_generated", "normal"),
        ("heartbeat", "silent"),
        ("stream_lag", "silent"),
        ("task_created", "silent"),
        ("unknown_event_xyz", "silent"),
        ("", "silent"),
    ],
)
def test_tier_for_event_mapping(event_type, expected_tier):
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        tier_for_event,
    )
    assert tier_for_event(event_type).value == expected_tier


def test_tier_for_event_defensive_on_non_string():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        tier_for_event,
    )
    for bad in (None, 42, [], {}):
        assert tier_for_event(bad).value == "silent"


# redact_sensitive


def test_redact_secret_shape_sk_prefix():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    out = redact_sensitive(
        "API key is sk-proj-abcd1234efgh5678",
    )
    assert "[redacted]" in out
    assert "sk-proj" not in out


def test_redact_bearer_token():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    out = redact_sensitive(
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    )
    assert "[redacted]" in out


def test_redact_aws_key_shape():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    out = redact_sensitive("Set token to AKIAIOSFODNN7EXAMPLE")
    assert "[redacted]" in out


def test_redact_password_substring():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    out = redact_sensitive("Your password expired")
    assert "[redacted]" in out


def test_redact_clean_text_unchanged():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    out = redact_sensitive("Cost approaching budget")
    assert out == "Cost approaching budget"


def test_redact_defensive_on_bad_input():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        redact_sensitive,
    )
    for bad in (None, 42, [], {}):
        result = redact_sensitive(bad)
        assert result is not None or result == bad


# auto_mute


def test_auto_mute_in_ci(monkeypatch):
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        auto_mute_active,
    )
    monkeypatch.setenv("CI", "true")
    assert auto_mute_active() is True


def test_auto_mute_when_not_tty():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        auto_mute_active,
    )
    assert auto_mute_active() is True


# record_event flow


def test_record_event_master_off_returns_none(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        get_default_announcer,
    )
    a = get_default_announcer()
    result = a.record_event(
        event_type="posture_changed",
        payload={"posture": "HARDEN"},
        op_id="op-x",
    )
    assert result is None


def test_record_event_auto_mute_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        get_default_announcer,
    )
    a = get_default_announcer()
    result = a.record_event(
        event_type="posture_changed",
        payload={"posture": "HARDEN"},
        op_id="op-x",
    )
    assert result is None


def test_record_event_with_overrides(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    a = kva.get_default_announcer()
    with patch.object(
        kva, "auto_mute_active", return_value=False,
    ), patch.object(
        kva.KarenVoiceAnnouncer, "_schedule_tts", return_value=None,
    ):
        result = a.record_event(
            event_type="posture_changed",
            payload={"posture": "HARDEN"},
            op_id="op-test",
        )
    assert result is not None
    assert "HARDEN" in result.text
    assert result.tier.value == "important"
    assert result.op_id == "op-test"


def test_record_event_silent_tier_dropped(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    a = kva.get_default_announcer()
    with patch.object(
        kva, "auto_mute_active", return_value=False,
    ), patch.object(
        kva.KarenVoiceAnnouncer, "_schedule_tts",
    ):
        result = a.record_event(
            event_type="heartbeat",
            payload={},
        )
    assert result is None


def test_record_event_below_min_tier_dropped(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_KAREN_VOICE_MIN_TIER", "critical",
    )
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    a = kva.get_default_announcer()
    with patch.object(
        kva, "auto_mute_active", return_value=False,
    ), patch.object(
        kva.KarenVoiceAnnouncer, "_schedule_tts",
    ):
        result = a.record_event(
            event_type="posture_changed",
            payload={"posture": "HARDEN"},
        )
    assert result is None


def test_record_event_cooldown_suppresses_same_op(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    a = kva.get_default_announcer()
    with patch.object(
        kva, "auto_mute_active", return_value=False,
    ), patch.object(
        kva.KarenVoiceAnnouncer, "_schedule_tts",
    ):
        first = a.record_event(
            event_type="posture_changed",
            payload={"posture": "HARDEN"},
            op_id="op-cd",
        )
        second = a.record_event(
            event_type="posture_changed",
            payload={"posture": "EXPLORE"},
            op_id="op-cd",
        )
    assert first is not None
    assert second is None


def test_record_event_persisted_to_recent(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance import (
        karen_voice_announcer as kva,
    )
    a = kva.get_default_announcer()
    with patch.object(
        kva, "auto_mute_active", return_value=False,
    ), patch.object(
        kva.KarenVoiceAnnouncer, "_schedule_tts",
    ):
        a.record_event(
            event_type="governor_emergency_brake",
            payload={},
            op_id="op-recent",
        )
    items = a.recent(limit=5)
    assert len(items) == 1
    assert items[0].source_event == "governor_emergency_brake"


# Mute toggles


def test_set_mute_manual(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        get_default_announcer,
    )
    a = get_default_announcer()
    a.set_mute(on=True)
    assert a.is_muted() is True
    a.set_mute(on=False)
    assert a.status()["mute_manual"] is False


# /voice REPL


def test_voice_repl_unmatched_returns_matched_false():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/something_else")
    assert r.matched is False


def test_voice_repl_help_master_off():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice help")
    assert r.ok is True
    assert "Karen" in r.text


def test_voice_repl_status_renders():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_voice_repl_off_sets_mute(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        get_default_announcer,
    )
    a = get_default_announcer()
    r = dispatch_voice_command("/voice off")
    assert r.ok is True
    assert a.status()["mute_manual"] is True


def test_voice_repl_on_clears_mute(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        get_default_announcer,
    )
    a = get_default_announcer()
    a.set_mute(on=True)
    r = dispatch_voice_command("/voice on")
    assert r.ok is True
    assert a.status()["mute_manual"] is False


def test_voice_repl_tier_critical(monkeypatch):
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_MIN_TIER", raising=False,
    )
    r = dispatch_voice_command("/voice tier critical")
    assert r.ok is True
    import os
    assert (
        os.environ.get("JARVIS_KAREN_VOICE_MIN_TIER")
        == "critical"
    )


def test_voice_repl_tier_invalid_rejected():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice tier invalid")
    assert r.ok is False
    assert "invalid" in r.text.lower()


def test_voice_repl_cooldown_valid(monkeypatch):
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_COOLDOWN_S", raising=False,
    )
    r = dispatch_voice_command("/voice cooldown 60")
    assert r.ok is True
    import os
    assert (
        os.environ.get("JARVIS_KAREN_VOICE_COOLDOWN_S")
        == "60.0"
    )


def test_voice_repl_cooldown_invalid_rejected():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice cooldown abc")
    assert r.ok is False


def test_voice_repl_persona_valid(monkeypatch):
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    monkeypatch.delenv(
        "JARVIS_KAREN_VOICE_PERSONA", raising=False,
    )
    r = dispatch_voice_command("/voice persona friday")
    assert r.ok is True
    import os
    assert (
        os.environ.get("JARVIS_KAREN_VOICE_PERSONA") == "friday"
    )


def test_voice_repl_recent_empty():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice recent")
    assert r.ok is True
    assert "no announcements" in r.text.lower()


def test_voice_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.voice_repl import (
        dispatch_voice_command,
    )
    r = dispatch_voice_command("/voice gibberish")
    assert r.ok is False
    assert "unknown" in r.text.lower()


# AST pins


def _karen_pins():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _karen_source():
    return Path(
        "backend/core/ouroboros/governance/"
        "karen_voice_announcer.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _karen_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _karen_pins()
    src = _karen_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _karen_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _karen_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_sensitive_tokens_fires_on_missing_compose():
    pins = _karen_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_sensitive_tokens" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry seed


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.karen_voice_announcer import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    assert n == 6
    names = {c["name"] for c in reg.calls}
    assert "JARVIS_KAREN_VOICE_ENABLED" in names


# Composition assertions


def test_canonical_voice_pipeline_importable():
    from backend.voice.barge_in_detector import (
        safe_say_with_barge_in,
        get_barge_in_detector,
    )
    assert callable(safe_say_with_barge_in)
    assert get_barge_in_detector() is not None


def test_canonical_sensitive_tokens_importable():
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (
        _SENSITIVE_NAME_TOKENS,
    )
    assert isinstance(_SENSITIVE_NAME_TOKENS, frozenset)
    assert "password" in _SENSITIVE_NAME_TOKENS
    assert "token" in _SENSITIVE_NAME_TOKENS
