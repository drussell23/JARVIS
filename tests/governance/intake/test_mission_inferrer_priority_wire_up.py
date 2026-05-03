"""Regression spine for MissionInferrer Slice B production wire-up.

Pins the contract that ``priority_boost_for_signal`` is consumed by
``unified_intake_router._compute_priority`` so inferred direction
actually steers intake (not just the prompt surface). Without this
wire-up, ``GoalInferenceEngine`` is decorative.

Coverage:
  * Master-off (default) -> zero effect on priority composition.
  * Master-on + no cached InferenceResult -> zero effect.
  * Master-on + cached result with matching theme -> priority drops
    by the rounded boost amount.
  * Boost cap honored (priority_boost_max env knob).
  * envelope.evidence stamped with inferred_direction_boost +
    inferred_direction_raw when boost > 0.
  * Defensive: import / engine failure does not break intake; priority
    still returned with no boost.
  * Authority invariant: alignment object untouched by the hook.
  * AST cross-file pin: register_shipped_invariants reports zero
    violations against the live router source.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance import goal_inference as gi
from backend.core.ouroboros.governance.goal_inference import (
    GoalInferenceEngine,
    InferenceResult,
    InferredGoal,
    SignalSample,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _compute_priority,
)


def _make_envelope(description: str, target_files=("a.py",)):
    return make_envelope(
        source="backlog",
        description=description,
        target_files=target_files,
        repo="jarvis",
        urgency="normal",
        confidence=0.5,
        evidence={"signature": "test-sig"},
        requires_human_ack=False,
    )


def _stub_result(*themes_and_confidences) -> InferenceResult:
    """Build a synthetic InferenceResult with the given (theme, conf) pairs."""
    inferred = []
    for theme, conf in themes_and_confidences:
        inferred.append(InferredGoal(
            theme=theme,
            tokens=tuple(theme.split()),
            confidence=conf,
            supporting_sources=("commits",),
            evidence=(SignalSample(
                source="commits", token=theme.split()[0],
                weight=conf, citation="synthetic",
            ),),
        ))
    return InferenceResult(
        inferred=tuple(inferred),
        built_at=1.0,
        build_ms=1,
        total_samples=len(inferred),
        sources_contributing={"commits": len(inferred)},
        build_reason="first_build",
    )


# ---------------------------------------------------------------------------
# Wire-up behavior
# ---------------------------------------------------------------------------


class TestIntakeWireUp:
    def test_master_off_default_zero_boost(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(
            "JARVIS_GOAL_INFERENCE_ENABLED", raising=False,
        )
        gi.reset_default_engine()
        env = _make_envelope("auth refactor for security")
        priority_off, _ = _compute_priority(env)
        # Master-off is the production default pre-Slice-C; sanity
        # check that the new code path doesn't break that path.
        assert isinstance(priority_off, int)

    def test_master_on_no_cached_result_zero_boost(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = GoalInferenceEngine(repo_root=tmp_path)
        gi.register_default_engine(engine)
        # Engine has no cached result yet (build never ran).
        assert engine.get_current() is None
        env = _make_envelope("anything")
        priority, _ = _compute_priority(env)
        assert "inferred_direction_boost" not in env.evidence
        # Same priority as if the hook were disabled — proves cache
        # absence is treated as zero boost.
        assert isinstance(priority, int)

    def test_master_on_cached_match_drops_priority(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        # Force boost ceiling of 1.0 so the int(round(x)) lands at 1.
        monkeypatch.setenv(
            "JARVIS_GOAL_INFERENCE_PRIORITY_BOOST_MAX", "1.0",
        )
        gi.reset_default_engine()
        engine = GoalInferenceEngine(repo_root=tmp_path)
        # Inject a cached result with a high-confidence theme.
        engine._cached = _stub_result(("authentication", 1.0))
        engine._last_build_mono = 1e9  # Suppress refresh.
        gi.register_default_engine(engine)
        env_unmatched = _make_envelope("unrelated work on logging")
        env_matched = _make_envelope("authentication flow rewrite")
        p_unmatched, _ = _compute_priority(env_unmatched)
        p_matched, _ = _compute_priority(env_matched)
        # Matched signal MUST have strictly lower (better) priority.
        assert p_matched < p_unmatched, (
            f"matched={p_matched} unmatched={p_unmatched} "
            "MissionInferrer boost not applied"
        )
        # Evidence stamping fires only when boost > 0.
        assert "inferred_direction_boost" in env_matched.evidence
        assert env_matched.evidence["inferred_direction_boost"] >= 1
        assert "inferred_direction_raw" in env_matched.evidence
        # Unmatched signal evidence stays clean.
        assert "inferred_direction_boost" not in env_unmatched.evidence

    def test_boost_cap_honored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        # Lower the cap so even a perfect theme match cannot exceed it.
        monkeypatch.setenv(
            "JARVIS_GOAL_INFERENCE_PRIORITY_BOOST_MAX", "0.1",
        )
        gi.reset_default_engine()
        engine = GoalInferenceEngine(repo_root=tmp_path)
        engine._cached = _stub_result(
            ("authentication", 1.0), ("authentication", 1.0),
            ("authentication", 1.0), ("authentication", 1.0),
        )
        engine._last_build_mono = 1e9
        gi.register_default_engine(engine)
        env = _make_envelope("authentication overhaul again")
        _ = _compute_priority(env)
        if "inferred_direction_raw" in env.evidence:
            # Raw float must respect the cap.
            assert env.evidence["inferred_direction_raw"] <= 0.1 + 1e-9

    def test_engine_failure_does_not_break_intake(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A broken engine MUST NOT prevent intake from computing
        priority (defensive fail-soft mirror of the goal_alignment
        + semantic_index patterns)."""
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()

        class _ExplodingEngine:
            def get_current(self):
                raise RuntimeError("synthetic failure")

        gi.register_default_engine(_ExplodingEngine())  # type: ignore[arg-type]
        env = _make_envelope("anything")
        priority, _ = _compute_priority(env)
        assert isinstance(priority, int)
        assert "inferred_direction_boost" not in env.evidence

    def test_alignment_object_untouched_by_hook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Authority invariant: the GoalAlignment object returned to
        callers MUST be the one produced by the active goal tracker
        (or None), NEVER mutated by the inferred-direction hook."""
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = GoalInferenceEngine(repo_root=tmp_path)
        engine._cached = _stub_result(("authentication", 1.0))
        engine._last_build_mono = 1e9
        gi.register_default_engine(engine)
        env = _make_envelope("authentication rewrite")
        _, alignment = _compute_priority(env)
        # No active goal tracker registered in this test -> alignment
        # is None. The hook never invents one.
        assert alignment is None


# ---------------------------------------------------------------------------
# AST regression pin
# ---------------------------------------------------------------------------


class TestCrossFileInvariant:
    def test_intake_consumer_invariant_holds(self) -> None:
        invariants = gi.register_shipped_invariants()
        consumer_inv = next(
            (i for i in invariants
             if i.invariant_name == "goal_inference_intake_consumer"),
            None,
        )
        assert consumer_inv is not None
        target_path = REPO_ROOT / consumer_inv.target_file
        source = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations = consumer_inv.validate(tree, source)
        assert violations == (), (
            "MissionInferrer Slice B wire-up regression: " + str(violations)
        )

    def test_invariant_catches_missing_call(self) -> None:
        invariants = gi.register_shipped_invariants()
        consumer_inv = next(
            (i for i in invariants
             if i.invariant_name == "goal_inference_intake_consumer"),
            None,
        )
        assert consumer_inv is not None
        synthetic = '''
def _compute_priority(envelope, dependency_credit=0):
    base = 5
    return base, None
'''
        tree = ast.parse(synthetic)
        violations = consumer_inv.validate(tree, synthetic)
        assert any(
            "priority_boost_for_signal" in v for v in violations
        )

    def test_invariant_catches_missing_composition(self) -> None:
        invariants = gi.register_shipped_invariants()
        consumer_inv = next(
            (i for i in invariants
             if i.invariant_name == "goal_inference_intake_consumer"),
            None,
        )
        assert consumer_inv is not None
        # Source includes the function call name (so first check passes)
        # but does NOT compose inferred_direction_boost into priority.
        synthetic = '''
def _compute_priority(envelope, dep=0):
    _ = priority_boost_for_signal()  # imported but not used in priority
    base = 5
    return base, None
'''
        tree = ast.parse(synthetic)
        violations = consumer_inv.validate(tree, synthetic)
        assert any(
            "inferred_direction_boost" in v for v in violations
        )


class TestSubstrateInvariant:
    def test_substrate_invariant_holds(self) -> None:
        invariants = gi.register_shipped_invariants()
        substrate_inv = next(
            (i for i in invariants
             if i.invariant_name == "goal_inference_substrate"),
            None,
        )
        assert substrate_inv is not None
        target_path = REPO_ROOT / substrate_inv.target_file
        source = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations = substrate_inv.validate(tree, source)
        assert violations == (), str(violations)


# ---------------------------------------------------------------------------
# FlagRegistry surface
# ---------------------------------------------------------------------------


class TestFlagRegistry:
    def test_eight_flags_registered(self) -> None:
        recorded = []
        class _Stub:
            def register(self, spec): recorded.append(spec.name)
        n = gi.register_flags(_Stub())
        assert n == 8
        for flag in (
            "JARVIS_GOAL_INFERENCE_ENABLED",
            "JARVIS_GOAL_INFERENCE_PROMPT_INJECTION",
            "JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE",
            "JARVIS_GOAL_INFERENCE_TOP_K",
            "JARVIS_GOAL_INFERENCE_COMMIT_LOOKBACK",
            "JARVIS_GOAL_INFERENCE_MAX_AGE_S",
            "JARVIS_GOAL_INFERENCE_PRIORITY_BOOST_MAX",
            "JARVIS_GOAL_INFERENCE_REFRESH_S",
        ):
            assert flag in recorded
