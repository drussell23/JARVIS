"""P0.5 — Cross-session direction memory — graduation pin suite.

Mirrors the P0 PostmortemRecall graduation pin pattern (PRD §11 Layer 4
prep). Pins the post-graduation contract for the entire P0.5 stack
(``git_momentum`` + ``arc_context`` + ``DirectionInferrer.infer`` arc
kwarg + ``PostureObserver`` wiring + ``/posture explain`` rendering).
If any pin breaks:

  * Unintentional regression — fix the change.
  * Intentional rollback — update the pin per the embedded instructions.

Pin coverage:

  (A) Master flag default — post-graduation == True
      (pre-graduation pin renamed per its embedded instruction)
  (B) Hot-revert path — explicit ``false`` disables bounded-nudge
      application byte-for-byte
  (C) Authority invariants — no banned imports across the three new
      P0.5 modules (``git_momentum``, ``arc_context``, and unchanged
      arc-file pin coverage on ``direction_inferrer``)
  (D) Schema invariants — ``ArcContextSignal`` is frozen,
      ``MAX_NUDGE_PER_POSTURE`` is the literal 0.10 cap,
      ``PostureReading.arc_context`` is optional (back-compat)
  (E) Wiring source-grep pins — ``PostureObserver.run_one_cycle`` builds
      arc_context, ``DirectionInferrer.infer`` accepts the kwarg,
      ``_render_arc_context_section`` exists on the REPL
  (F) Bounded-nudge math invariants — saturating-input nudges stay
      within MAX_NUDGE_PER_POSTURE; clear-winner posture stable under
      max nudge
  (G) Backwards-compat — pre-Slice-2 callers without kwarg unchanged;
      ``to_dict()`` omits ``arc_context`` when None
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.arc_context import (
    MAX_NUDGE_PER_POSTURE,
    ArcContextSignal,
)
from backend.core.ouroboros.governance.direction_inferrer import (
    DirectionInferrer,
    arc_context_enabled,
)
from backend.core.ouroboros.governance.git_momentum import MomentumSnapshot
from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    SignalBundle,
    baseline_bundle,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (A) Master flag default — post-graduation pin
# ---------------------------------------------------------------------------


def test_master_flag_default_true_post_graduation(monkeypatch):
    """JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED defaults True
    post-graduation (Slice 3, 2026-04-26).

    Hot-revert: ``export JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED=false``.
    If this test fails AND P0.5 has been intentionally rolled back:
    rename to test_master_flag_default_false (and flip the assertion +
    the source-grep pin in (E)) per the same discipline P0 used."""
    monkeypatch.delenv(
        "JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", raising=False,
    )
    assert arc_context_enabled() is True


def test_pin_master_env_reader_default_true_literal():
    """Source-grep pin: the helper literal-defaults to True."""
    src = _read("backend/core/ouroboros/governance/direction_inferrer.py")
    assert (
        '_env_bool("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", True)' in src
    ), (
        "Master flag default literal moved or changed. If P0.5 was rolled "
        "back, update both the source AND this pin (rename to "
        "test_pin_master_env_reader_default_false_literal)."
    )


# ---------------------------------------------------------------------------
# (B) Hot-revert path
# ---------------------------------------------------------------------------


def test_hot_revert_explicit_false_disables_application(monkeypatch):
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", "false")
    assert arc_context_enabled() is False


def test_hot_revert_byte_identical_scores_with_flag_off(monkeypatch):
    """Pin: when flag is off via hot-revert, calling ``infer`` with arc
    context produces byte-for-byte the same scores as without it."""
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", "false")
    di = DirectionInferrer()
    bundle = baseline_bundle()
    arc = ArcContextSignal(
        momentum=MomentumSnapshot(
            commit_count=10, type_counts={"fix": 10},
        ),
        lss_verify_ratio=0.0,
    )
    r_with = di.infer(bundle, arc_context=arc)
    r_without = di.infer(bundle)
    assert r_with.all_scores == r_without.all_scores
    assert r_with.signal_bundle_hash == r_without.signal_bundle_hash


# ---------------------------------------------------------------------------
# (C) Authority invariants — banned-import grep
# ---------------------------------------------------------------------------


_BANNED_AUTHORITY_IMPORTS = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]

_P0_5_NEW_MODULES = [
    "backend/core/ouroboros/governance/git_momentum.py",
    "backend/core/ouroboros/governance/arc_context.py",
]


@pytest.mark.parametrize("relpath", _P0_5_NEW_MODULES)
def test_p0_5_module_no_authority_imports(relpath):
    """PRD §12.2: every P0.5 module is read-only / advisory + must not
    import any authority module."""
    src = _read(relpath)
    for imp in _BANNED_AUTHORITY_IMPORTS:
        assert imp not in src, (
            f"banned authority import in {relpath}: {imp}"
        )


# ---------------------------------------------------------------------------
# (D) Schema invariants
# ---------------------------------------------------------------------------


def test_arc_context_signal_is_frozen_dataclass():
    """ArcContextSignal must stay hashable / immutable so PostureReading
    can carry it through (PostureReading is also frozen)."""
    signal = ArcContextSignal()
    with pytest.raises(Exception):
        signal.lss_verify_ratio = 0.5  # type: ignore[misc]


def test_max_nudge_constant_is_010():
    """Pin: the bounded-nudge constant is the documented 0.10 cap.
    Changing this value invalidates the "cannot override clear winner"
    safety pin in (F) — both must be reviewed together."""
    assert MAX_NUDGE_PER_POSTURE == 0.10


def test_posture_reading_arc_context_field_is_optional():
    """Back-compat pin: pre-Slice-2 callers that construct PostureReading
    without arc_context must continue to work."""
    reading = PostureReading(
        posture=Posture.MAINTAIN,
        confidence=0.0,
        evidence=(),
        inferred_at=0.0,
        signal_bundle_hash="abc",
        all_scores=(),
    )
    assert reading.arc_context is None
    assert "arc_context" not in reading.to_dict()


# ---------------------------------------------------------------------------
# (E) Wiring source-grep pins
# ---------------------------------------------------------------------------


def test_pin_posture_observer_builds_arc_context():
    """``PostureObserver.run_one_cycle`` must call build_arc_context +
    pass the result to ``infer``."""
    src = _read("backend/core/ouroboros/governance/posture_observer.py")
    assert "from backend.core.ouroboros.governance.arc_context import" in src
    assert "build_arc_context" in src
    assert "arc_context=arc_ctx" in src or "arc_context = arc_ctx" in src
    assert "[PostureObserver] arc_context=" in src


def test_pin_inferrer_accepts_arc_context_kwarg():
    src = _read("backend/core/ouroboros/governance/direction_inferrer.py")
    assert "from backend.core.ouroboros.governance.arc_context import ArcContextSignal" in src
    assert "arc_context: Optional[ArcContextSignal]" in src
    assert "arc_context_enabled()" in src
    assert "arc_context.suggest_nudge()" in src


def test_pin_repl_renders_arc_context_section():
    src = _read("backend/core/ouroboros/governance/posture_repl.py")
    assert "_render_arc_context_section" in src
    assert "Arc Context (P0.5" in src
    assert "APPLIED to scores" in src
    assert "OBSERVED ONLY" in src


# ---------------------------------------------------------------------------
# (F) Bounded-nudge math invariants
# ---------------------------------------------------------------------------


def test_saturating_input_nudge_stays_bounded():
    """Pathological max input — every counter at saturation, both LSS
    extremes — must still produce per-posture nudges ≤ MAX cap."""
    arc = ArcContextSignal(
        momentum=MomentumSnapshot(
            commit_count=999,
            type_counts={
                "feat": 999, "fix": 999, "refactor": 999, "docs": 999,
                "chore": 999, "test": 999,
            },
        ),
        lss_verify_ratio=0.0,
    )
    nudges = arc.suggest_nudge()
    for posture, nudge in nudges.items():
        assert 0.0 <= nudge <= MAX_NUDGE_PER_POSTURE, (
            f"nudge for {posture} out of bounds: {nudge}"
        )


def test_clear_winner_stable_under_maximum_arc_nudge(monkeypatch):
    """Even with the most adversarial arc context (max HARDEN nudge),
    a strong-EXPLORE bundle must remain EXPLORE post-graduation."""
    monkeypatch.delenv(
        "JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", raising=False,
    )  # default True
    di = DirectionInferrer()
    strong_explore = SignalBundle(
        feat_ratio=0.95, fix_ratio=0.0, refactor_ratio=0.05,
        test_docs_ratio=0.0, postmortem_failure_rate=0.0,
        iron_gate_reject_rate=0.0, l2_repair_rate=0.0,
        open_ops_normalized=0.0, session_lessons_infra_ratio=0.0,
        time_since_last_graduation_inv=0.5, cost_burn_normalized=0.1,
        worktree_orphan_count=0,
    )
    arc_max_harden = ArcContextSignal(
        momentum=MomentumSnapshot(
            commit_count=100, type_counts={"fix": 100},
        ),
        lss_verify_ratio=0.0,
    )
    reading = di.infer(strong_explore, arc_context=arc_max_harden)
    assert reading.posture == Posture.EXPLORE


# ---------------------------------------------------------------------------
# (G) Backwards-compat
# ---------------------------------------------------------------------------


def test_legacy_caller_without_arc_kwarg_unchanged():
    """The single-arg infer signature (legacy callers) must keep working
    indefinitely — no kwargs required."""
    di = DirectionInferrer()
    reading = di.infer(baseline_bundle())
    assert reading.arc_context is None


def test_to_dict_omits_arc_context_field_when_none():
    di = DirectionInferrer()
    reading = di.infer(baseline_bundle())
    d = reading.to_dict()
    assert "arc_context" not in d


def test_to_dict_includes_arc_context_when_present():
    di = DirectionInferrer()
    arc = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=3, type_counts={"feat": 3}),
        lss_verify_ratio=0.8,
    )
    with patch(
        "backend.core.ouroboros.governance.direction_inferrer.arc_context_enabled",
        return_value=False,
    ):
        reading = di.infer(baseline_bundle(), arc_context=arc)
    d = reading.to_dict()
    assert d["arc_context"]["has_momentum"] is True
    assert d["arc_context"]["momentum_commits"] == 3
    assert d["arc_context"]["lss_verify_ratio"] == pytest.approx(0.8)
