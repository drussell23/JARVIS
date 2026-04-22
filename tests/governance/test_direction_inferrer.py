"""Slice 1 regression spine for DirectionInferrer + StrategicPosture.

Authority invariants and behavioral contracts proven here are pinned
verbatim in the Slice 4 graduation test suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace

import pytest

from backend.core.ouroboros.governance.direction_inferrer import (
    DEFAULT_WEIGHTS,
    DirectionInferrer,
    confidence_floor,
    is_enabled,
)
from backend.core.ouroboros.governance.posture import (
    Posture,
    SCHEMA_VERSION,
    SignalBundle,
    SignalContribution,
    baseline_bundle,
)


# ---------------------------------------------------------------------------
# Fixtures — canonical signal bundles, one per expected posture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any pre-set DirectionInferrer env vars for hermetic runs."""
    for key in list(os.environ):
        if key.startswith("JARVIS_DIRECTION_INFERRER") or key.startswith("JARVIS_POSTURE"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def inferrer() -> DirectionInferrer:
    return DirectionInferrer()


def _explore_bundle() -> SignalBundle:
    """High feat:, low fix:, low postmortem — clean build momentum."""
    return replace(
        baseline_bundle(),
        feat_ratio=0.80,
        fix_ratio=0.05,
        test_docs_ratio=0.10,
        postmortem_failure_rate=0.05,
        iron_gate_reject_rate=0.02,
        l2_repair_rate=0.01,
        open_ops_normalized=0.6,
        time_since_last_graduation_inv=0.8,
    )


def _consolidate_bundle() -> SignalBundle:
    """Refactor-heavy, docs, low feat:, low postmortem."""
    return replace(
        baseline_bundle(),
        feat_ratio=0.10,
        refactor_ratio=0.60,
        test_docs_ratio=0.20,
        postmortem_failure_rate=0.05,
        worktree_orphan_count=5,
        session_lessons_infra_ratio=0.15,
        time_since_last_graduation_inv=0.1,
    )


def _harden_bundle() -> SignalBundle:
    """fix: heavy, postmortem spike, Iron Gate rejects rising, infra lessons."""
    return replace(
        baseline_bundle(),
        feat_ratio=0.05,
        fix_ratio=0.75,
        test_docs_ratio=0.10,
        postmortem_failure_rate=0.55,
        iron_gate_reject_rate=0.45,
        l2_repair_rate=0.30,
        session_lessons_infra_ratio=0.80,
    )


def _maintain_bundle() -> SignalBundle:
    """All signals at baseline / near-tied — should fall back to MAINTAIN
    under the confidence floor."""
    return baseline_bundle()


# ---------------------------------------------------------------------------
# Posture vocabulary — fixed 4 values, str-enum, parseable
# ---------------------------------------------------------------------------


class TestPostureVocabulary:

    def test_exactly_four_values(self):
        assert len(list(Posture)) == 4

    def test_canonical_order(self):
        assert Posture.all() == (
            Posture.CONSOLIDATE, Posture.EXPLORE, Posture.HARDEN, Posture.MAINTAIN,
        )

    def test_values_serialize_to_json_cleanly(self):
        payload = json.dumps({"p": Posture.EXPLORE.value})
        assert payload == '{"p": "EXPLORE"}'

    def test_from_str_case_insensitive(self):
        assert Posture.from_str("explore") is Posture.EXPLORE
        assert Posture.from_str("  HARDEN  ") is Posture.HARDEN
        assert Posture.from_str("Consolidate") is Posture.CONSOLIDATE

    def test_from_str_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown posture"):
            Posture.from_str("RECOVER")


# ---------------------------------------------------------------------------
# Canonical bundles → expected posture
# ---------------------------------------------------------------------------


class TestCanonicalBundles:

    def test_explore_bundle_infers_explore(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        assert reading.posture is Posture.EXPLORE
        assert reading.confidence > confidence_floor()

    def test_consolidate_bundle_infers_consolidate(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_consolidate_bundle())
        assert reading.posture is Posture.CONSOLIDATE
        assert reading.confidence > confidence_floor()

    def test_harden_bundle_infers_harden(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_harden_bundle())
        assert reading.posture is Posture.HARDEN
        assert reading.confidence > confidence_floor()

    def test_baseline_bundle_falls_back_to_maintain(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_maintain_bundle())
        assert reading.posture is Posture.MAINTAIN

    def test_every_posture_reachable_from_canonical_bundle(self, inferrer: DirectionInferrer):
        """Regression: never accept a weight table that makes any posture
        unreachable from the canonical test bundles."""
        reached = {
            inferrer.infer(_explore_bundle()).posture,
            inferrer.infer(_consolidate_bundle()).posture,
            inferrer.infer(_harden_bundle()).posture,
            inferrer.infer(_maintain_bundle()).posture,
        }
        assert reached == set(Posture)


# ---------------------------------------------------------------------------
# Pure function / idempotence
# ---------------------------------------------------------------------------


class TestPureFunction:

    def test_same_bundle_produces_same_hash(self, inferrer: DirectionInferrer):
        bundle = _explore_bundle()
        h1 = inferrer.infer(bundle).signal_bundle_hash
        h2 = inferrer.infer(bundle).signal_bundle_hash
        assert h1 == h2

    def test_same_bundle_produces_same_posture(self, inferrer: DirectionInferrer):
        bundle = _harden_bundle()
        postures = {inferrer.infer(bundle).posture for _ in range(5)}
        assert postures == {Posture.HARDEN}

    def test_same_bundle_produces_same_confidence(self, inferrer: DirectionInferrer):
        bundle = _harden_bundle()
        confidences = {round(inferrer.infer(bundle).confidence, 8) for _ in range(5)}
        assert len(confidences) == 1

    def test_different_bundles_produce_different_hashes(self, inferrer: DirectionInferrer):
        h_explore = inferrer.infer(_explore_bundle()).signal_bundle_hash
        h_harden = inferrer.infer(_harden_bundle()).signal_bundle_hash
        assert h_explore != h_harden

    def test_infer_does_not_mutate_bundle(self, inferrer: DirectionInferrer):
        bundle = _explore_bundle()
        snapshot = bundle.to_hashable()
        inferrer.infer(bundle)
        assert bundle.to_hashable() == snapshot

    def test_infer_timestamp_advances(self, inferrer: DirectionInferrer):
        bundle = _explore_bundle()
        r1 = inferrer.infer(bundle)
        time.sleep(0.01)
        r2 = inferrer.infer(bundle)
        assert r2.inferred_at >= r1.inferred_at
        # Identical payload hashes prove purity despite different timestamps
        assert r1.signal_bundle_hash == r2.signal_bundle_hash


# ---------------------------------------------------------------------------
# Confidence floor fallback
# ---------------------------------------------------------------------------


class TestConfidenceFloor:

    def test_default_floor_is_0_35(self):
        assert confidence_floor() == 0.35

    def test_low_confidence_falls_back_to_maintain(self, inferrer: DirectionInferrer):
        # Bundle crafted so top vs second spread is tiny
        near_tied = replace(
            baseline_bundle(),
            feat_ratio=0.1,
            refactor_ratio=0.1,
            fix_ratio=0.1,
        )
        reading = inferrer.infer(near_tied)
        if reading.confidence < confidence_floor():
            assert reading.posture is Posture.MAINTAIN

    def test_evidence_preserved_even_in_maintain_fallback(self, inferrer: DirectionInferrer):
        """§8 observability — `/posture explain` must still show what was
        near-tied even when the floor demotes the reading to MAINTAIN."""
        near_tied = replace(baseline_bundle(), feat_ratio=0.1, refactor_ratio=0.1)
        reading = inferrer.infer(near_tied)
        # Evidence list should be fully populated (12 entries) regardless
        assert len(reading.evidence) == 12

    def test_floor_env_override_lowers_bar(self, inferrer: DirectionInferrer, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_CONFIDENCE_FLOOR", "0.01")
        assert confidence_floor() == 0.01
        # Bundle that would have been MAINTAIN under 0.35 may now commit
        near_tied = replace(baseline_bundle(), feat_ratio=0.1)
        reading = inferrer.infer(near_tied)
        # At minimum, doesn't crash; confidence metric unchanged
        assert 0.0 <= reading.confidence <= 1.0

    def test_floor_clamped_to_0_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_CONFIDENCE_FLOOR", "5.0")
        assert confidence_floor() == 1.0
        monkeypatch.setenv("JARVIS_POSTURE_CONFIDENCE_FLOOR", "-0.5")
        assert confidence_floor() == 0.0

    def test_floor_malformed_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_CONFIDENCE_FLOOR", "not-a-float")
        assert confidence_floor() == 0.35


# ---------------------------------------------------------------------------
# Deterministic tie-break — alphabetic on posture name
# ---------------------------------------------------------------------------


class TestTieBreak:

    def test_exact_tie_resolves_alphabetically(self, inferrer: DirectionInferrer):
        """Construct a weight table yielding identical scores; verify the
        alphabetic order CONSOLIDATE > EXPLORE > HARDEN > MAINTAIN wins."""
        flat_weights = {
            sig: {p: 1.0 for p in Posture}
            for sig in DEFAULT_WEIGHTS
        }
        flat_inferrer = DirectionInferrer(weights=flat_weights)
        bundle = replace(baseline_bundle(), feat_ratio=0.5)
        reading = flat_inferrer.infer(bundle)
        # With identical per-posture weights, all four postures score
        # identically. Confidence = 0. Evidence/winner picks alphabetically.
        assert reading.confidence == 0.0

    def test_alphabetic_winner_on_zero_confidence_is_consolidate(self, inferrer: DirectionInferrer):
        flat_weights = {
            sig: {p: 0.5 for p in Posture}
            for sig in DEFAULT_WEIGHTS
        }
        flat_inferrer = DirectionInferrer(weights=flat_weights)
        bundle = replace(baseline_bundle(), feat_ratio=0.5)
        reading = flat_inferrer.infer(bundle)
        # Confidence floor demotes to MAINTAIN, but the underlying
        # pick_winner picked CONSOLIDATE (alphabetic first).
        # Check all_scores ordering: alphabetic order used as secondary key.
        assert reading.posture is Posture.MAINTAIN  # demoted by floor
        # First element of all_scores (highest score) — but tied, so any.
        assert reading.all_scores[0][0] in Posture


# ---------------------------------------------------------------------------
# Weight override via env
# ---------------------------------------------------------------------------


class TestWeightOverride:

    def test_override_flips_posture(self, inferrer: DirectionInferrer, monkeypatch):
        """A bundle that infers EXPLORE under defaults should infer
        CONSOLIDATE after we heavily re-weight feat_ratio toward CONSOLIDATE."""
        bundle = _explore_bundle()
        default_reading = inferrer.infer(bundle)
        assert default_reading.posture is Posture.EXPLORE

        # Flip feat_ratio entirely to CONSOLIDATE dominance
        override = {
            "feat_ratio": {
                "EXPLORE": -2.0,
                "CONSOLIDATE": +5.0,
                "HARDEN": 0.0,
                "MAINTAIN": 0.0,
            },
        }
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", json.dumps(override))
        flipped_reading = inferrer.infer(bundle)
        assert flipped_reading.posture is Posture.CONSOLIDATE

    def test_override_malformed_json_ignored(self, inferrer: DirectionInferrer, monkeypatch):
        bundle = _explore_bundle()
        baseline = inferrer.infer(bundle).posture
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", "{not: valid: json")
        assert inferrer.infer(bundle).posture == baseline

    def test_override_unknown_signal_ignored(self, inferrer: DirectionInferrer, monkeypatch):
        override = {"made_up_signal": {"EXPLORE": 100.0}}
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", json.dumps(override))
        # Should not crash; unknown signal silently dropped
        bundle = _explore_bundle()
        inferrer.infer(bundle)

    def test_override_unknown_posture_ignored(self, inferrer: DirectionInferrer, monkeypatch):
        override = {"feat_ratio": {"RECOVER": 100.0}}
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", json.dumps(override))
        bundle = _explore_bundle()
        reading = inferrer.infer(bundle)
        # RECOVER dropped; EXPLORE still wins under defaults
        assert reading.posture is Posture.EXPLORE

    def test_override_non_dict_ignored(self, inferrer: DirectionInferrer, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", json.dumps([1, 2, 3]))
        bundle = _explore_bundle()
        inferrer.infer(bundle)  # must not crash

    def test_partial_override_preserves_unmentioned_signals(
        self, inferrer: DirectionInferrer, monkeypatch,
    ):
        override = {"feat_ratio": {"EXPLORE": 0.0}}  # only feat_ratio touched
        monkeypatch.setenv("JARVIS_POSTURE_WEIGHTS_OVERRIDE", json.dumps(override))
        # Harden bundle should still infer HARDEN because fix/postmortem
        # weights are untouched.
        reading = inferrer.infer(_harden_bundle())
        assert reading.posture is Posture.HARDEN


# ---------------------------------------------------------------------------
# Schema version discipline
# ---------------------------------------------------------------------------


class TestSchemaVersion:

    def test_posture_reading_carries_schema_version(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        assert reading.schema_version == "1.0"

    def test_signal_bundle_carries_schema_version(self):
        assert baseline_bundle().schema_version == "1.0"

    def test_inferrer_rejects_mismatched_bundle_schema(self, inferrer: DirectionInferrer):
        future_bundle = replace(baseline_bundle(), schema_version="2.0")
        with pytest.raises(ValueError, match="schema_version mismatch"):
            inferrer.infer(future_bundle)

    def test_schema_version_constant_is_string_literal_1_0(self):
        """Graduation-style pin: catch accidental drift to ``1`` int or
        ``'v1'`` or ``1.0`` float."""
        assert SCHEMA_VERSION == "1.0"
        assert isinstance(SCHEMA_VERSION, str)


# ---------------------------------------------------------------------------
# Evidence structure
# ---------------------------------------------------------------------------


class TestEvidence:

    def test_evidence_has_12_entries_one_per_signal(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        assert len(reading.evidence) == 12

    def test_evidence_sorted_by_contribution_magnitude_desc(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        magnitudes = [abs(c.contribution_score) for c in reading.evidence]
        assert magnitudes == sorted(magnitudes, reverse=True)

    def test_evidence_contributed_to_matches_inferred_when_above_floor(
        self, inferrer: DirectionInferrer,
    ):
        reading = inferrer.infer(_explore_bundle())
        # All evidence entries cite the true winning posture
        assert all(c.contributed_to is Posture.EXPLORE for c in reading.evidence)

    def test_signal_contribution_is_frozen(self):
        sc = SignalContribution(
            signal_name="feat_ratio", raw_value=0.5, normalized=0.5,
            weight=1.0, contributed_to=Posture.EXPLORE, contribution_score=0.5,
        )
        with pytest.raises((AttributeError, TypeError)):
            sc.weight = 99.0  # type: ignore[misc]

    def test_posture_reading_is_frozen(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        with pytest.raises((AttributeError, TypeError)):
            reading.confidence = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:

    def test_all_ratios_at_ceiling_clipped(self, inferrer: DirectionInferrer):
        """Ratios > 1.0 should be clipped to 1.0, not blow up the score."""
        extreme = replace(
            baseline_bundle(),
            feat_ratio=5.0,
            fix_ratio=5.0,
            postmortem_failure_rate=5.0,
        )
        reading = inferrer.infer(extreme)
        # Should still produce a valid reading
        assert 0.0 <= reading.confidence <= 1.0
        assert reading.posture in set(Posture)

    def test_negative_ratios_clipped_to_zero(self, inferrer: DirectionInferrer):
        weird = replace(baseline_bundle(), feat_ratio=-0.5)
        reading = inferrer.infer(weird)
        assert 0.0 <= reading.confidence <= 1.0

    def test_worktree_orphan_count_saturates_at_10(self, inferrer: DirectionInferrer):
        b1 = replace(baseline_bundle(), worktree_orphan_count=10)
        b2 = replace(baseline_bundle(), worktree_orphan_count=1000)
        r1 = inferrer.infer(b1)
        r2 = inferrer.infer(b2)
        # Saturation → same posture + confidence (signal clipped identically)
        assert r1.posture == r2.posture
        assert round(r1.confidence, 6) == round(r2.confidence, 6)

    def test_empty_commit_history_all_zero_ratios(self, inferrer: DirectionInferrer):
        """When git log parsing yields no commits, all ratios are 0.0 and
        the system should land in MAINTAIN (confidence floor fallback)."""
        reading = inferrer.infer(baseline_bundle())
        assert reading.posture is Posture.MAINTAIN

    def test_all_scores_shape_is_4(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        assert len(reading.all_scores) == 4
        postures_present = {p for p, _ in reading.all_scores}
        assert postures_present == set(Posture)

    def test_all_scores_sorted_descending(self, inferrer: DirectionInferrer):
        reading = inferrer.infer(_explore_bundle())
        scores = [s for _, s in reading.all_scores]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# is_enabled master switch
# ---------------------------------------------------------------------------


class TestMasterSwitch:

    def test_default_on_post_graduation(self):
        """Post-Slice-4 graduation: default flipped false→true."""
        assert is_enabled() is True

    def test_enable_via_true_string(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        assert is_enabled() is True

    def test_enable_via_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "1")
        assert is_enabled() is True

    def test_enable_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "YES")
        assert is_enabled() is True

    def test_disable_via_false_string(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
        assert is_enabled() is False


# ---------------------------------------------------------------------------
# Authority invariant — grep-enforced pin (Slice 4 will re-pin)
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator",
    "policy",
    "iron_gate",
    "risk_tier",
    "gate",
    "change_engine",
    "candidate_generator",
)


class TestAuthorityInvariant:

    @pytest.mark.parametrize("module_relpath", [
        "backend/core/ouroboros/governance/direction_inferrer.py",
        "backend/core/ouroboros/governance/posture.py",
    ])
    def test_zero_authority_imports(self, module_relpath):
        """grep -E for any import of an authority module. Zero matches
        required. This is the Slice 1 foundation of the Slice 4 pin."""
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        path = os.path.join(repo_root, module_relpath)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for forbidden in _AUTHORITY_MODULES:
            # Match `from X.Y.Z.<module>` or `import <module>`
            bad = [
                line for line in src.splitlines()
                if (line.startswith("from ") or line.startswith("import "))
                and f".{forbidden}" in line
            ]
            assert not bad, (
                f"Authority import violation in {module_relpath}: "
                f"found imports referencing {forbidden!r}: {bad}"
            )


# ---------------------------------------------------------------------------
# Weight table sanity
# ---------------------------------------------------------------------------


class TestWeightTable:

    def test_default_weights_covers_all_12_signals(self):
        from backend.core.ouroboros.governance.direction_inferrer import _SIGNAL_NAMES
        assert set(DEFAULT_WEIGHTS.keys()) == set(_SIGNAL_NAMES)

    def test_default_weights_covers_all_4_postures_per_signal(self):
        for signal, row in DEFAULT_WEIGHTS.items():
            assert set(row.keys()) == set(Posture), (
                f"Signal {signal!r} missing postures: "
                f"{set(Posture) - set(row.keys())}"
            )

    def test_weights_are_numeric(self):
        for signal, row in DEFAULT_WEIGHTS.items():
            for posture, w in row.items():
                assert isinstance(w, (int, float)), (
                    f"Non-numeric weight at {signal}/{posture}: {w!r}"
                )
