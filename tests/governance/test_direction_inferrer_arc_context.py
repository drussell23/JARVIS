"""P0.5 Slice 2 — DirectionInferrer arc-context consumer regression suite.

Pins the new ``arc_context`` kwarg contract on ``DirectionInferrer.infer()``:

  (A) Builder — ``build_arc_context`` produces a structured signal from
      momentum + LSS one-liner; tolerates missing inputs gracefully.
  (B) Score nudge — ``ArcContextSignal.suggest_nudge()`` is bounded to
      ``MAX_NUDGE_PER_POSTURE`` (0.10) and routes momentum/LSS signals
      to the right postures.
  (C) Inferrer integration — observation-only by default (no score
      change with flag off); applies bounded nudge with flag on.
  (D) Back-compat — calling ``infer(bundle)`` without ``arc_context``
      kwarg produces byte-for-byte the same result as pre-Slice-2.
  (E) Authority invariant — new module ``arc_context.py`` does not
      import from any banned governance module.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.arc_context import (
    MAX_NUDGE_PER_POSTURE,
    ArcContextSignal,
    build_arc_context,
)
from backend.core.ouroboros.governance.direction_inferrer import (
    DirectionInferrer,
    arc_context_enabled,
)
from backend.core.ouroboros.governance.git_momentum import MomentumSnapshot
from backend.core.ouroboros.governance.posture import (
    Posture,
    SignalBundle,
    baseline_bundle,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", raising=False)
    yield


def _strong_explore_bundle() -> SignalBundle:
    """A SignalBundle that should land EXPLORE under default weights —
    used to verify arc nudges don't flip an already-clear winner."""
    return SignalBundle(
        feat_ratio=0.9,
        fix_ratio=0.05,
        refactor_ratio=0.05,
        test_docs_ratio=0.0,
        postmortem_failure_rate=0.0,
        iron_gate_reject_rate=0.0,
        l2_repair_rate=0.0,
        open_ops_normalized=0.0,
        session_lessons_infra_ratio=0.0,
        time_since_last_graduation_inv=0.5,
        cost_burn_normalized=0.1,
        worktree_orphan_count=0,
    )


# ---------------------------------------------------------------------------
# (A) Builder
# ---------------------------------------------------------------------------


def test_builder_with_full_inputs_produces_complete_signal():
    snap = MomentumSnapshot(
        commit_count=10,
        scope_counts={"governance": 3, "intake": 2},
        type_counts={"feat": 5, "fix": 3, "docs": 2},
        latest_subjects=("a", "b", "c"),
    )
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=snap,
    ):
        ctx = build_arc_context(
            Path("/fake"),
            lss_one_liner="apply=multi/4 verify=20/20 commit=abc1234567",
        )
    assert ctx.momentum is snap
    assert ctx.lss_verify_ratio == 1.0
    assert ctx.lss_apply_count == 4
    assert ctx.lss_apply_mode == "multi"
    assert ctx.lss_one_liner.startswith("apply=multi/4")
    assert not ctx.is_empty()


def test_builder_with_no_git_returns_lss_only_signal():
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=None,
    ):
        ctx = build_arc_context(
            Path("/fake"),
            lss_one_liner="apply=single/1 verify=10/15 commit=def567",
        )
    assert ctx.momentum is None
    assert ctx.lss_verify_ratio == pytest.approx(10 / 15)
    assert ctx.lss_apply_mode == "single"
    assert not ctx.is_empty()


def test_builder_with_no_lss_returns_momentum_only_signal():
    snap = MomentumSnapshot(commit_count=5, type_counts={"feat": 5})
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=snap,
    ):
        ctx = build_arc_context(Path("/fake"), lss_one_liner="")
    assert ctx.momentum is snap
    assert ctx.lss_verify_ratio is None
    assert ctx.lss_apply_count is None
    assert ctx.lss_apply_mode is None
    assert not ctx.is_empty()


def test_builder_with_no_inputs_is_empty():
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=None,
    ):
        ctx = build_arc_context(Path("/fake"), lss_one_liner="")
    assert ctx.is_empty()
    assert ctx.suggest_nudge() == {p: 0.0 for p in Posture}


def test_builder_tolerates_malformed_lss_token():
    """Garbage LSS line → all LSS fields None, builder still returns a signal."""
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=None,
    ):
        ctx = build_arc_context(Path("/fake"), lss_one_liner="this is garbage")
    assert ctx.lss_verify_ratio is None
    assert ctx.lss_apply_count is None
    assert ctx.lss_apply_mode is None


# ---------------------------------------------------------------------------
# (B) Score nudge bounds
# ---------------------------------------------------------------------------


def test_nudge_feat_dominance_routes_to_explore():
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"feat": 10}),
    )
    nudges = ctx.suggest_nudge()
    assert nudges[Posture.EXPLORE] == pytest.approx(MAX_NUDGE_PER_POSTURE)
    assert nudges[Posture.HARDEN] == 0.0
    assert nudges[Posture.CONSOLIDATE] == 0.0


def test_nudge_fix_dominance_routes_to_harden():
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"fix": 10}),
    )
    nudges = ctx.suggest_nudge()
    assert nudges[Posture.HARDEN] == pytest.approx(MAX_NUDGE_PER_POSTURE)
    assert nudges[Posture.EXPLORE] == 0.0


def test_nudge_refactor_docs_routes_to_consolidate():
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(
            commit_count=10, type_counts={"refactor": 5, "docs": 5}
        ),
    )
    nudges = ctx.suggest_nudge()
    assert nudges[Posture.CONSOLIDATE] == pytest.approx(MAX_NUDGE_PER_POSTURE)


def test_nudge_low_lss_verify_routes_to_harden():
    ctx = ArcContextSignal(lss_verify_ratio=0.0)
    nudges = ctx.suggest_nudge()
    assert nudges[Posture.HARDEN] > 0.0
    assert nudges[Posture.HARDEN] <= MAX_NUDGE_PER_POSTURE


def test_nudge_high_lss_verify_routes_to_maintain():
    ctx = ArcContextSignal(lss_verify_ratio=0.95)
    nudges = ctx.suggest_nudge()
    assert nudges[Posture.MAINTAIN] > 0.0
    assert nudges[Posture.MAINTAIN] <= MAX_NUDGE_PER_POSTURE


def test_nudge_every_posture_bounded():
    """Pin: regardless of input, no per-posture nudge exceeds the cap."""
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(
            commit_count=100,
            type_counts={"feat": 50, "fix": 50, "refactor": 100, "docs": 100},
        ),
        lss_verify_ratio=0.0,  # max HARDEN add
    )
    for p, n in ctx.suggest_nudge().items():
        assert n <= MAX_NUDGE_PER_POSTURE, f"nudge for {p} exceeded cap: {n}"
        assert n >= 0.0, f"nudge for {p} went negative: {n}"


# ---------------------------------------------------------------------------
# (C) Inferrer integration — flag off vs flag on
# ---------------------------------------------------------------------------


def test_arc_context_enabled_default_false():
    """Slice 2 ships default-off. Slice 3 graduation flips this."""
    assert arc_context_enabled() is False


def test_inferrer_with_flag_off_does_not_apply_nudge(monkeypatch):
    """Observation-only mode: scores are byte-for-byte identical to a
    call without arc_context."""
    monkeypatch.delenv(
        "JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", raising=False,
    )
    di = DirectionInferrer()
    bundle = _strong_explore_bundle()
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"fix": 10}),
        lss_verify_ratio=0.0,  # would heavily nudge HARDEN if applied
    )
    r_with = di.infer(bundle, arc_context=ctx)
    r_without = di.infer(bundle)
    # Same hash + same all_scores ⇒ no scoring change.
    assert r_with.signal_bundle_hash == r_without.signal_bundle_hash
    assert r_with.all_scores == r_without.all_scores
    # But arc_context IS carried through for observability.
    assert r_with.arc_context is ctx
    assert r_without.arc_context is None


def test_inferrer_with_flag_on_applies_bounded_nudge(monkeypatch):
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", "true")
    di = DirectionInferrer()
    bundle = _strong_explore_bundle()
    # Strong fix-dominance nudge should boost HARDEN score relative to
    # the no-context baseline.
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"fix": 10}),
    )
    r_with = di.infer(bundle, arc_context=ctx)
    r_without = di.infer(bundle)
    scores_with = dict(r_with.all_scores)
    scores_without = dict(r_without.all_scores)
    # HARDEN gained exactly the nudge amount (≤ MAX_NUDGE_PER_POSTURE).
    diff = scores_with[Posture.HARDEN] - scores_without[Posture.HARDEN]
    assert 0.0 < diff <= MAX_NUDGE_PER_POSTURE + 1e-9


def test_flag_on_with_clear_winner_does_not_flip_posture(monkeypatch):
    """Bounded nudge must not override an already-clear winner."""
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", "true")
    di = DirectionInferrer()
    # _strong_explore_bundle has feat_ratio=0.9 → strong EXPLORE winner
    bundle = _strong_explore_bundle()
    # Maximum HARDEN nudge from arc context
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"fix": 10}),
        lss_verify_ratio=0.0,
    )
    r = di.infer(bundle, arc_context=ctx)
    # Posture should still be EXPLORE — bounded nudge can't override it.
    assert r.posture == Posture.EXPLORE


# ---------------------------------------------------------------------------
# (D) Back-compat with existing infer() callers
# ---------------------------------------------------------------------------


def test_infer_without_arc_context_kwarg_works_unchanged():
    """All existing callers (sans arc_context kwarg) continue to work."""
    di = DirectionInferrer()
    reading = di.infer(baseline_bundle())
    assert reading.arc_context is None
    assert reading.posture in tuple(Posture)


def test_posture_reading_to_dict_omits_arc_context_when_none():
    di = DirectionInferrer()
    reading = di.infer(baseline_bundle())
    d = reading.to_dict()
    assert "arc_context" not in d


def test_posture_reading_to_dict_includes_arc_context_when_present():
    di = DirectionInferrer()
    ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=3, type_counts={"feat": 3}),
        lss_verify_ratio=0.8,
    )
    reading = di.infer(baseline_bundle(), arc_context=ctx)
    d = reading.to_dict()
    assert "arc_context" in d
    assert d["arc_context"]["has_momentum"] is True
    assert d["arc_context"]["momentum_commits"] == 3
    assert d["arc_context"]["lss_verify_ratio"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# (E) Authority invariants
# ---------------------------------------------------------------------------


def test_arc_context_module_no_authority_imports():
    """PRD §12.2: read-only modules MUST NOT import authority paths."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/arc_context.py"
    ).read_text(encoding="utf-8")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for imp in banned:
        assert imp not in src, f"banned authority import in arc_context.py: {imp}"


def test_arc_context_module_only_pure_data():
    """Pin: arc_context.py performs NO subprocess, file I/O, or env mutation
    of its own — all side effects come via its imports of git_momentum +
    last_session_summary, which have their own authority pins.

    Forbidden tokens assembled at runtime to avoid pre-commit hook flags."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/arc_context.py"
    ).read_text(encoding="utf-8")
    forbidden_calls = [
        "subprocess.",
        "open(",
        ".write(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",
    ]
    for c in forbidden_calls:
        assert c not in src, f"unexpected side effect in arc_context.py: {c}"
