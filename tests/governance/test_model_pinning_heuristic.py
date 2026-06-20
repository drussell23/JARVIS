"""Sovereign Context-Routing Override Matrix — model-pin soft-lock tests.

Covers: gated-off byte-identity, Rank-1 promotion (present + absent + dedup),
the soft-lock trip/clear/cooldown-expiry cycle, the passive-outcome wiring
(only the pinned model is tracked), env-tunable threshold/cooldown, and
fail-soft posture.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import model_pinning_heuristic as mph


@pytest.fixture(autouse=True)
def _clean_ledger_and_env(monkeypatch):
    """Each test starts with a clean ledger + no pin declared."""
    monkeypatch.delenv("JARVIS_DW_PRIMARY_OVERRIDE", raising=False)
    monkeypatch.delenv("JARVIS_DW_PIN_FAIL_THRESHOLD", raising=False)
    monkeypatch.delenv("JARVIS_DW_PIN_COOLDOWN_S", raising=False)
    mph.get_pin_ledger().reset()
    yield
    mph.get_pin_ledger().reset()


_FLEET = ("Qwen/Qwen3.5-397B-A17B-FP8", "openai/gpt-oss-120b", "deepseek/V4")


# ── gated OFF → byte-identical legacy ──────────────────────────────────────

def test_no_pin_is_byte_identical():
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET


def test_empty_pin_is_byte_identical(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "   ")
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET


# ── pin promotes to Rank 1 across routes ───────────────────────────────────

def test_pin_present_promoted_to_rank1(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    out = mph.apply_model_pin("standard", _FLEET)
    assert out[0] == "openai/gpt-oss-120b"
    # rest preserved in order, pin deduped (no duplicate)
    assert out == ("openai/gpt-oss-120b", "Qwen/Qwen3.5-397B-A17B-FP8", "deepseek/V4")
    assert out.count("openai/gpt-oss-120b") == 1


def test_pin_absent_is_prepended(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "newco/fast-7b")
    out = mph.apply_model_pin("complex", _FLEET)
    assert out[0] == "newco/fast-7b"
    assert out == ("newco/fast-7b",) + _FLEET


def test_pin_applies_to_every_route(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    for route in ("immediate", "standard", "complex", "background", "speculative"):
        assert mph.apply_model_pin(route, _FLEET)[0] == "openai/gpt-oss-120b", route


def test_pin_on_empty_ranked(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    assert mph.apply_model_pin("standard", ()) == ("openai/gpt-oss-120b",)


# ── soft lock: trip → defer to EWMA → recover ──────────────────────────────

def test_soft_lock_trips_after_threshold_and_defers_to_ewma(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", "3")
    # below threshold → still pinned
    for _ in range(2):
        mph.note_pin_outcome("openai/gpt-oss-120b", success=False)
    assert mph.apply_model_pin("standard", _FLEET)[0] == "openai/gpt-oss-120b"
    # hit threshold → soft-locked → yields to the EWMA ranking unchanged
    mph.note_pin_outcome("openai/gpt-oss-120b", success=False)
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET


def test_single_success_clears_the_streak(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", "3")
    for _ in range(3):
        mph.note_pin_outcome("openai/gpt-oss-120b", success=False)
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET  # locked
    mph.note_pin_outcome("openai/gpt-oss-120b", success=True)  # clears
    assert mph.apply_model_pin("standard", _FLEET)[0] == "openai/gpt-oss-120b"


def test_cooldown_expiry_self_heals(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", "1")
    monkeypatch.setenv("JARVIS_DW_PIN_COOLDOWN_S", "0.05")
    mph.note_pin_outcome("openai/gpt-oss-120b", success=False)  # trips at 1
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET    # locked
    import time
    time.sleep(0.07)
    assert mph.apply_model_pin("standard", _FLEET)[0] == "openai/gpt-oss-120b"


# ── the passive wiring tracks ONLY the pinned model ────────────────────────

def test_note_outcome_ignores_non_pinned_models(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", "1")
    # failures for a DIFFERENT model must not soft-lock the pin
    mph.note_pin_outcome("Qwen/Qwen3.5-397B-A17B-FP8", success=False)
    mph.note_pin_outcome("deepseek/V4", success=False)
    assert mph.apply_model_pin("standard", _FLEET)[0] == "openai/gpt-oss-120b"


def test_threshold_and_cooldown_are_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", "5")
    for _ in range(4):
        mph.note_pin_outcome("openai/gpt-oss-120b", success=False)
    assert mph.apply_model_pin("standard", _FLEET)[0] == "openai/gpt-oss-120b"  # 4<5
    mph.note_pin_outcome("openai/gpt-oss-120b", success=False)                  # 5
    assert mph.apply_model_pin("standard", _FLEET) == _FLEET


def test_invalid_threshold_falls_back_to_default(monkeypatch):
    for bad in ("bad", "0", "-2", ""):
        monkeypatch.setenv("JARVIS_DW_PIN_FAIL_THRESHOLD", bad)
        assert mph._fail_threshold() == mph._DEFAULT_FAIL_THRESHOLD, bad


def test_model_pin_override_reads_env(monkeypatch):
    assert mph.model_pin_override() == ""
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "  openai/gpt-oss-120b  ")
    assert mph.model_pin_override() == "openai/gpt-oss-120b"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
