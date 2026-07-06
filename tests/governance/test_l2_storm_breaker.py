"""L2 VALIDATE_RETRY storm breaker — Slice 6 hardening.

Empirical context (run a1-brain-20260705-233225, GCP Brain node):
60 background-route doc_staleness ops each dispatched L2 twice, and
120/120 re-dispatches returned the IDENTICAL stop reason
``class_retries_exhausted:env`` — a monotonic exhaustion that cannot be
un-exhausted by a fresh dispatch. Each futile re-dispatch burned a fresh
120s timebox; the aggregate storm consumed the session wall clock and the
worker pool while the queue's priority math sat idle. 720 VALIDATE_RETRY
phase entries, zero repairs.

Three composable fixes, all structural (no sensor names, no global
disables):

1. TAXONOMY ROOT CAUSE — ``class_retries_exhausted`` joins the HARD stop
   prefixes in ``_l2_hook``. The taxonomy's own definition of HARD is
   "genuinely exhausted — re-dispatch would gain nothing"; a per-class
   retry exhaustion is literally that. Misclassified SOFT was the storm's
   ignition.
2. STICKY-REASON BREAKER — a re-dispatch that reproduces the identical
   stop reason has empirically falsified the SOFT premise ("transient —
   fresh dispatch could converge"). Generic: catches ANY deterministic
   failure, not an enumerated reason list.
3. ROUTE-AWARE DISPATCH BUDGET — BACKGROUND/SPECULATIVE ops default to a
   single L2 dispatch (no re-dispatch), composing with the existing
   route-gating cost philosophy (Venom tool loop is already skipped for
   those routes). Env-tunable: JARVIS_L2_DISPATCH_RETRIES_BACKGROUND.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.phase_runners import validate_runner as vr

_REPO = Path(__file__).resolve().parents[2]
_ORCH = _REPO / "backend" / "core" / "ouroboros" / "governance" / "orchestrator.py"
_ENGINE = _REPO / "backend" / "core" / "ouroboros" / "governance" / "repair_engine.py"
_VR = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)


# ---------------------------------------------------------------------------
# 1. resolve_l2_dispatch_budget — route-aware
# ---------------------------------------------------------------------------


class TestResolveL2DispatchBudget:
    def test_default_routes_get_two_dispatches(self, monkeypatch):
        monkeypatch.delenv("JARVIS_L2_DISPATCH_RETRIES", raising=False)
        monkeypatch.delenv("JARVIS_L2_DISPATCH_RETRIES_BACKGROUND", raising=False)
        for route in ("immediate", "standard", "complex", "", None):
            assert vr.resolve_l2_dispatch_budget(route) == 2

    def test_background_and_speculative_get_single_dispatch(self, monkeypatch):
        monkeypatch.delenv("JARVIS_L2_DISPATCH_RETRIES", raising=False)
        monkeypatch.delenv("JARVIS_L2_DISPATCH_RETRIES_BACKGROUND", raising=False)
        for route in ("background", "speculative", "BACKGROUND", " speculative "):
            assert vr.resolve_l2_dispatch_budget(route) == 1

    def test_background_env_knob_raises_bg_budget(self, monkeypatch):
        monkeypatch.delenv("JARVIS_L2_DISPATCH_RETRIES", raising=False)
        monkeypatch.setenv("JARVIS_L2_DISPATCH_RETRIES_BACKGROUND", "1")
        assert vr.resolve_l2_dispatch_budget("background") == 2

    def test_bg_budget_never_exceeds_global(self, monkeypatch):
        # Operator caps the global at 0 retries: BG cannot exceed it even
        # with a raised BG knob — strictest wins, same composing philosophy
        # as risk_tier_floor.
        monkeypatch.setenv("JARVIS_L2_DISPATCH_RETRIES", "0")
        monkeypatch.setenv("JARVIS_L2_DISPATCH_RETRIES_BACKGROUND", "5")
        assert vr.resolve_l2_dispatch_budget("background") == 1

    def test_garbage_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_DISPATCH_RETRIES", "not-a-number")
        monkeypatch.setenv("JARVIS_L2_DISPATCH_RETRIES_BACKGROUND", "nan!")
        assert vr.resolve_l2_dispatch_budget("standard") == 2
        assert vr.resolve_l2_dispatch_budget("background") == 1


# ---------------------------------------------------------------------------
# 2. is_sticky_soft_stop — no-progress detector
# ---------------------------------------------------------------------------


class TestIsStickySoftStop:
    def test_identical_consecutive_reason_is_sticky(self):
        assert vr.is_sticky_soft_stop(
            "class_retries_exhausted:env", "class_retries_exhausted:env"
        )

    def test_different_reason_is_progress(self):
        assert not vr.is_sticky_soft_stop(
            "empty_candidates", "generate_error:TypeError"
        )

    def test_first_dispatch_has_no_prior(self):
        assert not vr.is_sticky_soft_stop(None, "empty_candidates")
        assert not vr.is_sticky_soft_stop("", "empty_candidates")


# ---------------------------------------------------------------------------
# 3. Spine pins — the wiring may not silently disappear
# ---------------------------------------------------------------------------


def test_spine_validate_runner_wires_sticky_breaker():
    src = _VR.read_text(encoding="utf-8")
    assert "is_sticky_soft_stop(" in src, (
        "validate_runner no longer consults the sticky-reason breaker — "
        "the 120/120-identical-reason storm class is live again"
    )
    assert "l2_sticky_soft_stop" in src, (
        "validate_runner missing the l2_sticky_soft_stop terminal reason"
    )


def test_spine_validate_runner_wires_route_aware_budget():
    src = _VR.read_text(encoding="utf-8")
    assert "resolve_l2_dispatch_budget(" in src, (
        "validate_runner computes the L2 dispatch budget inline again — "
        "route-aware budgeting unwired"
    )


def test_spine_class_retries_exhausted_is_hard_stop():
    """The taxonomy root cause: class_retries_exhausted IS an exhaustion.
    It must sit in _l2_hook's HARD prefixes so the engine's own verdict
    ('this failure class is out of retries') is honored instead of
    re-dispatched."""
    src = _ORCH.read_text(encoding="utf-8")
    # The prefix must appear inside the hard-stop tuple.
    tuple_start = src.find("_l2_hard_stop_prefixes = (")
    assert tuple_start != -1, "hard-stop prefixes tuple missing from _l2_hook"
    tuple_src = src[tuple_start : src.find(")", tuple_start)]
    assert '"class_retries_exhausted"' in tuple_src, (
        "class_retries_exhausted missing from _l2_hard_stop_prefixes — "
        "a monotonic exhaustion is classified as transient and will be "
        "futilely re-dispatched (the a1-brain-20260705-233225 storm)"
    )
    # And the engine really emits it (taxonomy lockstep, same as the
    # existing Slice 6 spine test for the other four).
    engine_src = _ENGINE.read_text(encoding="utf-8")
    assert 'class_retries_exhausted' in engine_src
