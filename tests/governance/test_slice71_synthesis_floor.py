"""Slice 71 — Dynamic Temporal Inheritance & Adaptive Synthesis Floor Gating.

Root cause (verify-first, bt-2026-06-02-232715): the Claude fallback's Venom
tool loop derives its per-round / stream budget from the GENERATE-phase
``deadline`` (providers.py:7905 ``_generate_raw`` + 8732 ``deadline_mono``).
A *continuation* round (``is_tool_round = round_index > 0``) runs after earlier
rounds have CONSUMED that phase window, so ``_remaining_utc_budget_s(deadline)``
collapses to its 1.0s floor → Claude is handed ``budget=1.0s`` → ``first_token=
NEVER`` → ``fallback_failed`` → no candidate → no score.

The op's TRUE wall envelope (``OperationContext.pipeline_deadline``, stamped
once at submit) still had runway (313s in the soak). The Envelope Inheritance
Invariant: tool-loop synthesis inherits the MORE generous of the phase deadline
and the wall envelope, so a consumed phase window can't starve the fallback.
The outer ``_call_fallback`` ``_race_or_wait_for`` + wall watchdog remain the
absolute ceilings — inheritance only RAISES the inner budget, never beyond the
op's wall envelope.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.core.ouroboros.governance.providers import (
    _synthesis_envelope_deadline,
    _synthesis_envelope_enabled,
)


class _Ctx:
    """Minimal OperationContext stand-in carrying a pipeline_deadline."""

    def __init__(self, pipeline_deadline):
        self.pipeline_deadline = pipeline_deadline


def _utc(seconds_from_now: float) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_from_now)


def test_inherits_wall_envelope_when_more_generous(monkeypatch):
    """Consumed phase deadline (≈now) + healthy wall envelope (≈now+300s):
    the synthesis deadline inherits the wall envelope."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    phase = _utc(0.5)          # consumed phase window
    wall = _utc(300.0)         # healthy pipeline_deadline
    eff = _synthesis_envelope_deadline(_Ctx(wall), phase)
    assert eff == wall


def test_keeps_phase_when_phase_more_generous(monkeypatch):
    """If the phase deadline is somehow later than the wall envelope, keep it
    (max semantics — never SHRINK the budget)."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    phase = _utc(400.0)
    wall = _utc(120.0)
    eff = _synthesis_envelope_deadline(_Ctx(wall), phase)
    assert eff == phase


def test_disabled_flag_is_byte_identical_passthrough(monkeypatch):
    """Flag off → returns the phase deadline unchanged (legacy behavior)."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "false")
    phase = _utc(1.0)
    wall = _utc(300.0)
    assert _synthesis_envelope_deadline(_Ctx(wall), phase) == phase


def test_absent_pipeline_deadline_passthrough(monkeypatch):
    """No wall envelope on the context → phase deadline unchanged."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    phase = _utc(10.0)
    assert _synthesis_envelope_deadline(_Ctx(None), phase) == phase


def test_none_phase_with_wall_returns_wall(monkeypatch):
    """Phase deadline None but wall envelope present → inherit the wall."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    wall = _utc(200.0)
    assert _synthesis_envelope_deadline(_Ctx(wall), None) == wall


def test_never_raises_on_bad_context(monkeypatch):
    """A context without pipeline_deadline attr → safe passthrough, no raise."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    phase = _utc(5.0)
    assert _synthesis_envelope_deadline(object(), phase) == phase


def test_naive_aware_mismatch_falls_back_to_phase(monkeypatch):
    """A tz-naive wall vs tz-aware phase would raise on compare → keep phase
    (fail-safe; never crash the generation path)."""
    monkeypatch.setenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", "true")
    phase = _utc(5.0)
    naive_wall = datetime.now() + timedelta(seconds=300)  # naive on purpose
    assert _synthesis_envelope_deadline(_Ctx(naive_wall), phase) == phase


def test_default_enabled(monkeypatch):
    """Slice 71 graduates default-on (operator wants it for the scored soak)."""
    monkeypatch.delenv("JARVIS_FALLBACK_SYNTHESIS_ENVELOPE_ENABLED", raising=False)
    assert _synthesis_envelope_enabled() is True
