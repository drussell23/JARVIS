"""Sovereign Reasoning-Capability Profiler tests (2026-06-21).

Adaptive, immortal profile that learns which DW models reject reasoning_effort=none
(can't disable reasoning) from live error feedback — Meryem @ DW (gpt-oss-120b),
Seb @ DW (deepseek-v4-pro)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_reasoning_profile import (
    ReasoningProfile,
    error_indicates_reasoning_rejection,
    maybe_learn_from_error,
    get_reasoning_profile,
    reasoning_profile_enabled,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_PROFILE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DW_REASONING_REJECTION_PATTERNS", raising=False)
    monkeypatch.delenv("JARVIS_DW_REASONING_LEARNED_FLOOR", raising=False)


def _prof():
    return ReasoningProfile(path=Path(tempfile.mktemp()))


def test_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_PROFILE_ENABLED", raising=False)
    assert reasoning_profile_enabled() is True


def test_error_recognition_reasoning_vs_other():
    assert error_indicates_reasoning_rejection(
        "400: model does not support reasoning_effort=none"
    ) is True
    assert error_indicates_reasoning_rejection(
        "reasoning cannot be disabled for this model"
    ) is True
    # transport / entitlement / timeout must NOT match (precision)
    assert error_indicates_reasoning_rejection("403 entitlement_blocked") is False
    assert error_indicates_reasoning_rejection("502 bad gateway") is False
    assert error_indicates_reasoning_rejection("") is False


def test_record_then_learned_min_effort():
    p = _prof()
    assert p.learned_min_effort("vendor/m") is None
    p.record_reasoning_floor("vendor/m")  # default floor "low"
    assert p.learned_min_effort("vendor/m") == "low"


def test_record_monotonic_never_lowers():
    p = _prof()
    p.record_reasoning_floor("vendor/m", "medium")
    p.record_reasoning_floor("vendor/m", "low")  # must not lower
    assert p.learned_min_effort("vendor/m") == "medium"


def test_persists_and_rehydrates_across_fork():
    path = Path(tempfile.mktemp())
    ReasoningProfile(path=path).record_reasoning_floor("vendor/m")
    payload = json.loads(path.read_text())
    assert payload["min_effort"]["vendor/m"] == "low"
    assert payload["schema_version"] == "reasoning_profile.1"
    # fresh instance = forked subprocess rehydrating
    assert ReasoningProfile(path=path).learned_min_effort("vendor/m") == "low"


def test_disabled_no_learning(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_PROFILE_ENABLED", "0")
    p = _prof()
    p.record_reasoning_floor("vendor/m")
    assert p.learned_min_effort("vendor/m") is None


def test_clear():
    p = _prof()
    p.record_reasoning_floor("vendor/m")
    p.clear("vendor/m")
    assert p.learned_min_effort("vendor/m") is None


def test_record_never_raises():
    p = _prof()
    p.record_reasoning_floor("")
    p.record_reasoning_floor(None)  # type: ignore
    assert p.learned_min_effort("") is None


def test_maybe_learn_only_on_reasoning_error(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_PROFILE_STATE_PATH", tempfile.mktemp())
    # reset singleton so it reads the temp path
    import backend.core.ouroboros.governance.dw_reasoning_profile as M
    M._DEFAULT_PROFILE = None
    assert maybe_learn_from_error(
        "vendor/r", "none",
        "model does not support reasoning_effort=none",
    ) is True
    assert get_reasoning_profile().learned_min_effort("vendor/r") == "low"
    # a non-reasoning error must NOT learn
    assert maybe_learn_from_error("vendor/q", "none", "403 entitlement_blocked") is False
    assert get_reasoning_profile().learned_min_effort("vendor/q") is None


def test_provider_resolver_consults_learned_tier(monkeypatch):
    """End-to-end: _dw_model_min_effort returns the learned floor for a model NOT in
    the static seed map."""
    monkeypatch.setenv("JARVIS_DW_REASONING_PROFILE_STATE_PATH", tempfile.mktemp())
    import backend.core.ouroboros.governance.dw_reasoning_profile as M
    M._DEFAULT_PROFILE = None
    M.get_reasoning_profile().record_reasoning_floor("vendor/future-reasoner", "low")
    from backend.core.ouroboros.governance.doubleword_provider import _dw_model_min_effort
    assert _dw_model_min_effort("vendor/future-reasoner") == "low"


def test_gpt_oss_floored_by_static_seed():
    """Meryem's case: gpt-oss-120b floored immediately via the static seed (no error
    needed) so reasoning_effort=none is never sent."""
    from backend.core.ouroboros.governance.doubleword_provider import _reasoning_effort_for
    assert _reasoning_effort_for("trivial", "openai/gpt-oss-120b") == "low"
    assert _reasoning_effort_for("trivial", "openai/gpt-oss-20b") == "low"
