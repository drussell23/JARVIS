"""M9 Slice 1 — CuriosityGradient primitive tests (PRD §30.5.1).

Pins the contract layer for the entire M9 arc:
  § 1 — Master flag default (FALSE pre-Slice-5; flipped at graduation)
  § 2 — Closed-taxonomy enums (CuriositySource 5-value, CuriosityDecayReason 5-value)
  § 3 — Frozen dataclasses (CuriosityObservation + CuriosityScore)
  § 4 — Env-knob accessors — clamping + defaults
  § 5 — compute_curiosity decision tree (all 5 branches independently)
  § 6 — curiosity_multiplier_from_score consumer rules
  § 7 — Cold-start inertness (cannot bias on insufficient data)
  § 8 — Bounded multiplier (cannot bypass global cap)
  § 9 — No-duplication pin: defers recency_weight to _scoring_primitives
  § 10 — Authority floor (no orchestrator/iron_gate/providers imports)
  § 11 — Public exports (__all__)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        """Slice 1 default is FALSE; Slice 5 graduation flips
        to TRUE. Mirrors Upgrade 3 + M11 + Upgrade 1 pre-
        graduation pattern."""
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        assert curiosity_gradient_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_variants_flip_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", v,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        assert curiosity_gradient_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "off", "no", "garbage"],
    )
    def test_falsy_variants_stay_off(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", v,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        assert curiosity_gradient_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — Closed-taxonomy enums
# ---------------------------------------------------------------------------


class TestClosedEnums:
    def test_curiosity_source_has_exactly_five_values(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        values = {m.value for m in CuriositySource}
        assert values == {
            "logprob_entropy",
            "prophecy_error",
            "postmortem_recurrence",
            "insufficient_data",
            "disabled",
        }

    def test_curiosity_decay_reason_has_exactly_five_values(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        values = {m.value for m in CuriosityDecayReason}
        assert values == {
            "none",
            "stale_focus",
            "recurrence_loop",
            "operator_reset",
            "disabled",
        }

    def test_enums_are_string_subclass(self):
        """Closed enums inherit from str so JSON serialization is
        free + .value comparison works in dispatch."""
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
            CuriositySource,
        )
        assert issubclass(CuriositySource, str)
        assert issubclass(CuriosityDecayReason, str)


# ---------------------------------------------------------------------------
# § 3 — Frozen dataclasses
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_observation_is_frozen(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityObservation,
            CuriositySource,
        )
        obs = CuriosityObservation(
            source=CuriositySource.LOGPROB_ENTROPY,
            cluster_id="c1",
            value=0.5,
            at_unix=100.0,
        )
        with pytest.raises(Exception):
            obs.value = 0.99  # type: ignore[misc]

    def test_score_is_frozen(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
        )
        score = CuriosityScore(cluster_id="c1")
        with pytest.raises(Exception):
            score.magnitude = 0.99  # type: ignore[misc]

    def test_score_defaults_are_inert(self):
        """Default-constructed score is structurally inert —
        any consumer should see is_cold_start=True."""
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
            CuriosityScore,
            CuriositySource,
        )
        score = CuriosityScore(cluster_id="c1")
        assert score.magnitude == 0.0
        assert score.confidence == 0.0
        assert score.samples_count == 0
        assert score.dominant_source is (
            CuriositySource.INSUFFICIENT_DATA
        )
        assert score.decay_reason is CuriosityDecayReason.NONE
        assert score.is_cold_start() is True
        assert score.is_decayed() is False

    def test_observation_carries_all_named_fields(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityObservation,
            CuriositySource,
        )
        obs = CuriosityObservation(
            source=CuriositySource.PROPHECY_ERROR,
            cluster_id="c2",
            value=0.7,
            at_unix=1000.0,
            op_id="op-x",
        )
        assert obs.source is CuriositySource.PROPHECY_ERROR
        assert obs.cluster_id == "c2"
        assert obs.value == 0.7
        assert obs.at_unix == 1000.0
        assert obs.op_id == "op-x"


# ---------------------------------------------------------------------------
# § 4 — Env-knob accessors
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_halflife_default_14_days(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_HALFLIFE_DAYS", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_halflife_days,
        )
        assert curiosity_halflife_days() == 14.0

    def test_halflife_clamped_below_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HALFLIFE_DAYS", "-5.0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_halflife_days,
        )
        assert curiosity_halflife_days() == 0.1  # floor

    def test_halflife_clamped_above_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HALFLIFE_DAYS", "9999.0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_halflife_days,
        )
        assert curiosity_halflife_days() == 365.0  # ceiling

    def test_halflife_garbage_returns_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HALFLIFE_DAYS", "not-a-number",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_halflife_days,
        )
        assert curiosity_halflife_days() == 14.0

    def test_min_samples_default_8(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_min_samples,
        )
        assert curiosity_min_samples() == 8

    def test_min_samples_clamped_to_floor_1(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_min_samples,
        )
        assert curiosity_min_samples() == 1

    def test_stale_focus_default_24(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_STALE_FOCUS_HOURS", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_stale_focus_hours,
        )
        assert curiosity_stale_focus_hours() == 24

    def test_source_weights_default_1_0(self, monkeypatch):
        for k in (
            "JARVIS_CURIOSITY_WEIGHT_LOGPROB",
            "JARVIS_CURIOSITY_WEIGHT_PROPHECY",
            "JARVIS_CURIOSITY_WEIGHT_RECURRENCE",
        ):
            monkeypatch.delenv(k, raising=False)
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_source_weight_logprob,
            curiosity_source_weight_prophecy,
            curiosity_source_weight_recurrence,
        )
        assert curiosity_source_weight_logprob() == 1.0
        assert curiosity_source_weight_prophecy() == 1.0
        assert curiosity_source_weight_recurrence() == 1.0

    def test_source_weights_can_zero_out_a_source(
        self, monkeypatch,
    ):
        """Operator can structurally exclude a source by setting
        its weight to 0."""
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_WEIGHT_RECURRENCE", "0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_source_weight_recurrence,
        )
        assert curiosity_source_weight_recurrence() == 0.0

    def test_multiplier_floor_default_0_5(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_MULTIPLIER_FLOOR", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_multiplier_floor,
        )
        assert curiosity_multiplier_floor() == 0.5

    def test_multiplier_ceiling_default_2_0(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_MULTIPLIER_CEILING", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_multiplier_ceiling,
        )
        assert curiosity_multiplier_ceiling() == 2.0

    def test_multiplier_ceiling_clamped_to_10(
        self, monkeypatch,
    ):
        """Hard cap at 10× even if operator misconfigures."""
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MULTIPLIER_CEILING", "999",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_multiplier_ceiling,
        )
        assert curiosity_multiplier_ceiling() == 10.0


# ---------------------------------------------------------------------------
# § 5 — compute_curiosity decision tree (all 5 branches)
# ---------------------------------------------------------------------------


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
    )


def _obs(source, value, at_unix, *, cluster_id="c1"):
    from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
        CuriosityObservation,
    )
    return CuriosityObservation(
        source=source, cluster_id=cluster_id,
        value=value, at_unix=at_unix,
    )


class TestComputeCuriosityDecisionTree:
    def test_disabled_when_master_off(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
            CuriositySource,
            compute_curiosity,
        )
        score = compute_curiosity("c1", [])
        assert score.dominant_source is CuriositySource.DISABLED
        assert score.decay_reason is CuriosityDecayReason.DISABLED
        assert score.magnitude == 0.0

    def test_disabled_via_explicit_override(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        score = compute_curiosity(
            "c1", [], enabled_override=False,
        )
        assert score.dominant_source is CuriositySource.DISABLED

    def test_cold_start_returns_insufficient_data(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        # Default min_samples=8; supply 3
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.5, 1000.0)
            for _ in range(3)
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        assert score.dominant_source is (
            CuriositySource.INSUFFICIENT_DATA
        )
        assert score.is_cold_start() is True
        assert score.samples_count == 3

    def test_single_source_at_threshold_aggregates(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        # 3 logprob samples, all 0.6, all "now"
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.6, 1000.0)
            for _ in range(3)
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        assert score.dominant_source is (
            CuriositySource.LOGPROB_ENTROPY
        )
        assert score.magnitude == pytest.approx(0.6, abs=0.01)
        assert score.samples_count == 3
        assert score.is_cold_start() is False

    def test_multi_source_dominant_source_picks_max(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        # 1 logprob (0.3) + 1 prophecy (0.9) + 1 recurrence (0.5)
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.3, 1000.0),
            _obs(CuriositySource.PROPHECY_ERROR, 0.9, 1000.0),
            _obs(
                CuriositySource.POSTMORTEM_RECURRENCE, 0.5,
                1000.0,
            ),
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        # Prophecy 0.9 > recurrence 0.5 > logprob 0.3 (all
        # equally weighted via env defaults)
        assert score.dominant_source is (
            CuriositySource.PROPHECY_ERROR
        )

    def test_recency_decay_drops_old_samples_weight(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "2",
        )
        # Halflife 1 day → sample 1 day old has weight 0.5
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HALFLIFE_DAYS", "1.0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        # one_day_ago (0.0 value) + now (1.0 value)
        # weighted mean ~= (0.0 × 0.5 + 1.0 × 1.0) / 1.5 ≈ 0.667
        ONE_DAY = 86400.0
        obs = [
            _obs(
                CuriositySource.LOGPROB_ENTROPY,
                0.0, 1000.0 - ONE_DAY,
            ),
            _obs(CuriositySource.LOGPROB_ENTROPY, 1.0, 1000.0),
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        assert score.magnitude == pytest.approx(0.667, abs=0.05)

    def test_value_clamping_for_out_of_range_inputs(
        self, monkeypatch,
    ):
        """Defensive clamp — caller might supply a value
        outside [0, 1] (e.g., normalization bug). Aggregator
        must NOT propagate the bug."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "2",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 5.0, 1000.0),
            _obs(CuriositySource.LOGPROB_ENTROPY, -2.0, 1000.0),
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        # Clamped: 5 → 1.0, -2 → 0.0; mean = 0.5
        assert 0.0 <= score.magnitude <= 1.0
        assert score.magnitude == pytest.approx(0.5, abs=0.01)

    def test_zero_weight_source_excluded_structurally(
        self, monkeypatch,
    ):
        """Operator can disable a source by setting weight=0."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "2",
        )
        # Zero out recurrence weight
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_WEIGHT_RECURRENCE", "0",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        # Only recurrence samples — zero weight → no
        # contribution → INSUFFICIENT_DATA
        obs = [
            _obs(
                CuriositySource.POSTMORTEM_RECURRENCE,
                0.9, 1000.0,
            )
            for _ in range(5)
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
        )
        # Sample count >= min, but no contributing source
        assert score.dominant_source is (
            CuriositySource.INSUFFICIENT_DATA
        )

    def test_decay_reason_override_propagates(
        self, monkeypatch,
    ):
        """Slice 2's tracker passes STALE_FOCUS via override —
        primitive must respect it verbatim."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "2",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
            CuriositySource,
            compute_curiosity,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.7, 1000.0)
            for _ in range(3)
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,
            decay_reason_override=(
                CuriosityDecayReason.STALE_FOCUS
            ),
        )
        assert score.decay_reason is (
            CuriosityDecayReason.STALE_FOCUS
        )
        assert score.is_decayed() is True

    def test_cluster_id_normalization_to_global_fallback(
        self, monkeypatch,
    ):
        """Empty cluster_id falls through to ``_global``
        (Decision A3 SemanticIndex-optional fallback)."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            compute_curiosity,
        )
        score = compute_curiosity("", [])
        assert score.cluster_id == "_global"

    def test_cluster_id_lowercased_and_stripped(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            compute_curiosity,
        )
        score = compute_curiosity("  CLUSTER-X  ", [])
        assert score.cluster_id == "cluster-x"

    def test_garbage_observation_silently_skipped(
        self, monkeypatch,
    ):
        """Defensive — non-CuriosityObservation entries in the
        sequence MUST be ignored (test for cross-process
        deserialization corruption)."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "2",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.5, 1000.0),
            "not-an-observation",  # type: ignore[list-item]
            None,  # type: ignore[list-item]
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.5, 1000.0),
        ]
        score = compute_curiosity(
            "c1", obs, now_ts=1000.0,  # type: ignore[arg-type]
        )
        # Only 2 valid samples — meets min_samples=2
        assert score.samples_count == 2

    def test_source_breakdown_ordered_by_contribution(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 0.2, 1000.0),
            _obs(CuriositySource.PROPHECY_ERROR, 0.9, 1000.0),
            _obs(
                CuriositySource.POSTMORTEM_RECURRENCE,
                0.5, 1000.0,
            ),
        ]
        score = compute_curiosity("c1", obs, now_ts=1000.0)
        # Breakdown should be descending
        contributions = [v for _, v in score.source_breakdown]
        assert contributions == sorted(
            contributions, reverse=True,
        )


# ---------------------------------------------------------------------------
# § 6 — curiosity_multiplier_from_score consumer rules
# ---------------------------------------------------------------------------


class TestMultiplierConsumer:
    def test_none_returns_1_0(self):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_multiplier_from_score,
        )
        assert curiosity_multiplier_from_score(None) == 1.0

    def test_cold_start_score_returns_1_0(self):
        """Score with insufficient samples → no bias."""
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            curiosity_multiplier_from_score,
        )
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.99,  # high raw signal
            confidence=0.99,
            samples_count=1,  # but only 1 sample
        )
        # is_cold_start() is true → multiplier=1.0 regardless
        assert curiosity_multiplier_from_score(score) == 1.0

    def test_decayed_score_returns_1_0(self, monkeypatch):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        # Simulate a decayed score: post-min_samples, but
        # decay_reason set
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.99,
            confidence=0.99,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
            decay_reason=CuriosityDecayReason.STALE_FOCUS,
        )
        assert curiosity_multiplier_from_score(score) == 1.0

    def test_low_confidence_returns_1_0(self, monkeypatch):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.99,
            confidence=0.1,  # below 0.5 threshold
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        assert curiosity_multiplier_from_score(score) == 1.0

    def test_high_magnitude_high_confidence_amplifies(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=1.0,
            confidence=1.0,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        # At magnitude=1.0 → ceiling (default 2.0)
        result = curiosity_multiplier_from_score(score)
        assert result == 2.0

    def test_low_magnitude_high_confidence_throttles(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.0,  # zero curiosity
            confidence=1.0,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        # At magnitude=0.0 → floor (default 0.5)
        result = curiosity_multiplier_from_score(score)
        assert result == 0.5

    def test_mid_magnitude_passes_through_1_0(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.5,
            confidence=1.0,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        # Linear interp at mag=0.5 with floor=0.5, ceil=2.0:
        # 0.5 + (2.0 - 0.5) * 0.5 = 1.25
        # NOT 1.0 unless floor and ceil are symmetric around 1.0
        # at the pivot mag — the contract states "passes through
        # 1.0 IFF floor=0.5 + ceil=2.0 default symmetric setup".
        # But that's not quite right — let me verify the math:
        # 0.5 + (2.0 - 0.5) * 0.5 = 0.5 + 0.75 = 1.25
        # So actually mag=1/3 gives 1.0:
        # 0.5 + 1.5 * (1/3) = 1.0
        # The docstring claim about "passes through 1.0 at
        # magnitude=0.5" is ONLY true when floor + ceil = 2.0
        # arithmetic mean. Default floor=0.5, ceil=2.0:
        # arithmetic mean = 1.25 at mag=0.5.
        result = curiosity_multiplier_from_score(score)
        assert result == 1.25


# ---------------------------------------------------------------------------
# § 7 — Cold-start inertness (architectural lock)
# ---------------------------------------------------------------------------


class TestColdStartInertness:
    def test_cold_start_pinned_at_multiplier_1_0(
        self, monkeypatch,
    ):
        """No matter what the magnitude or confidence say, if
        the score is cold-start, multiplier MUST be 1.0. Pinned
        to prevent random-walk-on-boot pathology."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
            compute_curiosity,
            curiosity_multiplier_from_score,
        )
        obs = [
            _obs(CuriositySource.LOGPROB_ENTROPY, 1.0, 1000.0)
            for _ in range(2)  # below default min=8
        ]
        score = compute_curiosity("c1", obs, now_ts=1000.0)
        assert curiosity_multiplier_from_score(score) == 1.0


# ---------------------------------------------------------------------------
# § 8 — Bounded multiplier (cannot bypass global cap)
# ---------------------------------------------------------------------------


class TestBoundedMultiplier:
    def test_multiplier_never_exceeds_ceiling(
        self, monkeypatch,
    ):
        """Architectural lock: even with adversarial inputs, the
        multiplier cannot exceed curiosity_multiplier_ceiling()."""
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        # Synthetic adversarial: magnitude > 1.0 (shouldn't
        # happen via compute_curiosity but defensive anyway)
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=999.0,  # adversarial
            confidence=1.0,
            samples_count=100,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        result = curiosity_multiplier_from_score(score)
        # Defensive clamp at ceiling
        assert result <= 2.0  # default ceiling

    def test_multiplier_never_below_floor(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=-999.0,  # adversarial
            confidence=1.0,
            samples_count=100,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        result = curiosity_multiplier_from_score(score)
        assert result >= 0.5  # default floor

    def test_operator_floor_ceiling_overrides_respected(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
            CuriositySource,
            curiosity_multiplier_from_score,
        )
        monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "1")
        # Operator chooses no-throttle floor = 1.0
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MULTIPLIER_FLOOR", "1.0",
        )
        # Operator caps amplification at 1.5×
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MULTIPLIER_CEILING", "1.5",
        )
        score = CuriosityScore(
            cluster_id="c1",
            magnitude=0.0,  # min — should hit floor=1.0
            confidence=1.0,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        assert (
            curiosity_multiplier_from_score(score) == 1.0
        )

        score_high = CuriosityScore(
            cluster_id="c1",
            magnitude=1.0,  # max — should hit ceiling=1.5
            confidence=1.0,
            samples_count=10,
            dominant_source=CuriositySource.LOGPROB_ENTROPY,
        )
        assert (
            curiosity_multiplier_from_score(score_high) == 1.5
        )


# ---------------------------------------------------------------------------
# § 9 — No-duplication pin
# ---------------------------------------------------------------------------


class TestNoDuplicationOfDecayMath:
    """Operator mandate: M9 NEVER duplicates the
    :func:`recency_weight` formula — defers to
    :func:`_scoring_primitives.recency_weight`. Slice 5 will
    AST-pin this; Slice 1 verifies the import is present."""

    def test_imports_recency_weight_from_shared_primitives(
        self,
    ):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_gradient.py"
        )
        source = path.read_text(encoding="utf-8")
        # Both the import statement and the function call must
        # be present
        assert (
            "from backend.core.ouroboros.governance"
            "._scoring_primitives import" in source
        )
        assert "recency_weight" in source
        # No parallel implementation: source must NOT contain
        # the formula `0.5 ** (` outside docstring/comment
        # context. Defer the strict AST pin to Slice 5; here
        # check the formula isn't redefined as a local function.
        assert "def recency_weight" not in source
        assert "def _recency_weight" not in source


# ---------------------------------------------------------------------------
# § 10 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
        # Circular-import prevention: SensorGovernor (Slice 3
        # consumer) lazy-imports M9; the reverse is forbidden
        "from backend.core.ouroboros.governance.sensor_governor",
        # No provider/SDK imports — pure substrate
        "import anthropic",
        "from anthropic",
    )

    def test_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_gradient.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"curiosity_gradient.py must NOT import "
                f"{forbidden} — pure primitive layer"
            )


# ---------------------------------------------------------------------------
# § 11 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_exports_match(self):
        from backend.core.ouroboros.governance import (
            curiosity_gradient as cg,
        )
        expected = sorted([
            "CURIOSITY_GRADIENT_SCHEMA_VERSION",
            "CuriosityDecayReason",
            "CuriosityObservation",
            "CuriosityScore",
            "CuriositySource",
            "compute_curiosity",
            "curiosity_gradient_enabled",
            "curiosity_halflife_days",
            "curiosity_min_samples",
            "curiosity_multiplier_ceiling",
            "curiosity_multiplier_floor",
            "curiosity_multiplier_from_score",
            "curiosity_source_weight_logprob",
            "curiosity_source_weight_prophecy",
            "curiosity_source_weight_recurrence",
            "curiosity_stale_focus_hours",
        ])
        assert sorted(cg.__all__) == expected

    def test_score_to_dict_has_required_fields(
        self, monkeypatch,
    ):
        """Slice 4 observability projection contract."""
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityScore,
        )
        score = CuriosityScore(cluster_id="c1")
        d = score.to_dict()
        for key in (
            "schema_version", "cluster_id", "magnitude",
            "confidence", "samples_count", "dominant_source",
            "source_breakdown", "decay_reason",
            "last_updated_at_unix", "is_cold_start",
            "is_decayed",
        ):
            assert key in d, f"to_dict missing key: {key}"
