"""Slice 225 Phase 2 — Sovereign DW Autarky: fallback-aware primary budget.

ROOT CAUSE (live soak, GOAL-001::file-00): the primary (DW) is severed at the
30s ``_PRIMARY_MAX_TIMEOUT_S`` cap to hand off to the Claude fallback for the
Manifesto §5 cascade — but when Claude is OUT OF CREDITS its breaker trips
(``terminal_quota``), so the sever just accelerates exhaustion into a dead lane.
file-00's heavy generation never gets enough DW runway to produce a patch.

FIX: when the fallback (Claude) lane is unreliable (breaker OPEN/HALF_OPEN),
``_compute_primary_budget`` gives the DW primary the FULL remaining budget (up
to a sovereign-autarky ceiling, default 180s) instead of the 30s/75s reflex cap
— there is no live fallback to reserve runway for. Mirrors the existing
``force_batch`` precedent ("Claude disabled → no fallback to reserve → full
runway"). ``fallback_dead=False`` (default) is byte-identical to legacy.
"""
from __future__ import annotations

import importlib

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    _FALLBACK_MIN_RESERVE_S,
    _PRIMARY_MAX_TIMEOUT_S,
)


# ── default (fallback alive) is byte-identical to legacy ───────────────────

def test_fallback_alive_is_legacy_30s_cap():
    """fallback_dead=False (default) → the 30s Tier-3 cap still binds."""
    assert CandidateGenerator._compute_primary_budget(220.0) == _PRIMARY_MAX_TIMEOUT_S
    assert CandidateGenerator._compute_primary_budget(
        220.0, fallback_dead=False) == _PRIMARY_MAX_TIMEOUT_S


# ── fallback dead → DW gets the full budget (no 30s sever, no reserve) ──────

def test_fallback_dead_lifts_the_30s_cap():
    """Claude breaker OPEN → DW gets the full remaining budget, NOT 30s."""
    budget = CandidateGenerator._compute_primary_budget(180.0, fallback_dead=True)
    assert budget > _PRIMARY_MAX_TIMEOUT_S, (
        f"expected full budget, got 30s-capped {budget}")
    # Full remaining (180s) since it's at/under the autarky ceiling.
    assert budget == pytest.approx(180.0, abs=0.5)


def test_fallback_dead_does_not_reserve_for_dead_lane():
    """No fb_reserve carved out for a fallback that can't run."""
    budget = CandidateGenerator._compute_primary_budget(100.0, fallback_dead=True)
    # Legacy would cap at 30s; autarky gives the full 100s (no 30s reserve hole).
    assert budget == pytest.approx(100.0, abs=0.5)
    assert budget > 100.0 - _FALLBACK_MIN_RESERVE_S


def test_fallback_dead_respects_autarky_ceiling():
    """Even with huge remaining, cost-safety ceiling (default 180s) caps it."""
    budget = CandidateGenerator._compute_primary_budget(600.0, fallback_dead=True)
    assert budget == pytest.approx(180.0, abs=0.5), (
        f"expected 180s autarky ceiling, got {budget}")


def test_fallback_dead_ceiling_is_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_AUTARKY_MAX_BUDGET_S", "240")
    budget = CandidateGenerator._compute_primary_budget(600.0, fallback_dead=True)
    assert budget == pytest.approx(240.0, abs=0.5)


def test_fallback_dead_zero_remaining_is_zero():
    assert CandidateGenerator._compute_primary_budget(0.0, fallback_dead=True) == 0.0


# ── config-disabled Claude is a DEAD fallback → autarky must engage ─────────
# Gap: ``_claude_breaker_open()`` reads only the breaker STATE, so a
# CONFIG-disabled Claude (``JARVIS_PROVIDER_CLAUDE_DISABLED=true``) — which is
# never even constructed, the deadest possible fallback — leaves the breaker
# CLOSED and the autarky full-runway grant never engaged, holding the sole-lane
# DW to the reflex cap until it TIMED OUT. ``_claude_config_disabled()`` closes it.

def test_claude_config_disabled_true_when_env_on(monkeypatch):
    from backend.core.ouroboros.governance.candidate_generator import (
        _claude_config_disabled,
    )
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", val)
        assert _claude_config_disabled() is True, f"{val!r} should disable Claude"


def test_claude_config_disabled_false_when_unset(monkeypatch):
    from backend.core.ouroboros.governance.candidate_generator import (
        _claude_config_disabled,
    )
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    assert _claude_config_disabled() is False


def test_claude_config_disabled_false_when_explicit_false(monkeypatch):
    from backend.core.ouroboros.governance.candidate_generator import (
        _claude_config_disabled,
    )
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "false")
    assert _claude_config_disabled() is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
