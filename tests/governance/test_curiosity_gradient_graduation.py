"""M9 Slice 5 — Graduation regression tests (CLOSES M9).

Pins:
  * Master flag default-TRUE post graduation
  * /curiosity REPL surface (auto-discovered via register_verbs)
  * 6 FlagRegistry seeds installed
  * 5 AST shipped-code-invariants pins HOLD against shipped code
  * Producer bridge: 3 entry points + lazy-import safety
  * CoherenceAuditor RECURRENCE_DRIFT wire-up present

Mirrors Upgrade 1 Slice 5 + M11 Slice 5 graduation discipline.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        assert curiosity_gradient_enabled() is True

    def test_explicit_false_instant_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        assert curiosity_gradient_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — /curiosity REPL auto-discovery
# ---------------------------------------------------------------------------


class TestCuriosityREPLGraduation:
    def test_register_verbs_auto_discovers(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1


# ---------------------------------------------------------------------------
# § 3 — FlagRegistry seeds (6 entries)
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_master_seed_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        master = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_GRADIENT_ENABLED"
        )
        assert master.default is True

    def test_halflife_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_HALFLIFE_DAYS"
        )
        assert spec.default == 14.0

    def test_min_samples_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_MIN_SAMPLES"
        )
        assert spec.default == 8

    def test_stale_focus_hours_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_STALE_FOCUS_HOURS"
        )
        assert spec.default == 24

    def test_multiplier_floor_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_MULTIPLIER_FLOOR"
        )
        assert spec.default == 0.5

    def test_multiplier_ceiling_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_CURIOSITY_MULTIPLIER_CEILING"
        )
        assert spec.default == 2.0


# ---------------------------------------------------------------------------
# § 4 — AST shipped-code-invariants pins (5 entries)
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_five_m9_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _REGISTRY,
            _register_seed_invariants,
        )
        _register_seed_invariants()
        m9_pins = {
            k for k in _REGISTRY
            if (
                "curiosity" in k
                or k == "sensor_governor_curiosity_lazy_imported"
            )
        }
        assert len(m9_pins) == 5, (
            f"Expected 5 M9 pins, got {len(m9_pins)}: "
            f"{sorted(m9_pins)}"
        )

    def test_all_m9_pins_pass_against_shipped_code(self):
        """The 5 M9 pins MUST hold against the live source.
        If any tripped, the graduation contract regressed."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _register_seed_invariants,
            validate_all,
        )
        _register_seed_invariants()
        violations = validate_all()
        m9_violations = [
            v for v in violations
            if (
                "curiosity" in v.invariant_name
                or v.invariant_name == (
                    "sensor_governor_curiosity_lazy_imported"
                )
            )
        ]
        assert m9_violations == [], (
            f"M9 AST pins regressed: "
            f"{[v.invariant_name for v in m9_violations]}"
        )


# ---------------------------------------------------------------------------
# § 5 — Producer bridge
# ---------------------------------------------------------------------------


class TestProducerBridge:
    def test_three_entry_points_exist(self):
        from backend.core.ouroboros.governance import (
            curiosity_producer_bridge as bridge,
        )
        assert callable(bridge.feed_logprob_entropy)
        assert callable(bridge.feed_prophecy_error)
        assert callable(bridge.feed_recurrence_drift)

    def test_master_off_returns_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_producer_bridge import (  # noqa: E501
            feed_logprob_entropy,
            feed_prophecy_error,
            feed_recurrence_drift,
        )
        assert (
            feed_logprob_entropy(
                region_or_path="x", entropy_normalized=0.5,
            ) is False
        )
        assert (
            feed_prophecy_error(
                region_or_path="x", predicted_risk=0.5,
                verify_passed=True,
            ) is False
        )
        assert (
            feed_recurrence_drift(
                region_or_path="x", recurrence_count=5,
            ) is False
        )

    def test_master_on_record_succeeds(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "c"),
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            reset_default_collector_for_tests,
        )
        reset_default_collector_for_tests()
        from backend.core.ouroboros.governance.curiosity_producer_bridge import (  # noqa: E501
            feed_logprob_entropy,
        )
        result = feed_logprob_entropy(
            region_or_path="explicit-label",
            entropy_normalized=0.7,
            op_id="op-x",
        )
        assert result is True

    def test_prophecy_error_computes_abs_diff(
        self, monkeypatch, tmp_path,
    ):
        """High predicted_risk + verify_passed (actual=0) →
        large error → high curiosity. Low predicted_risk +
        verify_passed → small error → low curiosity."""
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "c"),
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.curiosity_producer_bridge import (  # noqa: E501
            feed_prophecy_error,
        )
        reset_default_collector_for_tests()
        # Predicted high (0.9) but verify_passed → error 0.9
        feed_prophecy_error(
            region_or_path="wrong-prediction",
            predicted_risk=0.9, verify_passed=True,
        )
        score = get_default_collector().score_for_cluster(
            "wrong-prediction",
        )
        assert score.magnitude == pytest.approx(0.9, abs=0.05)

    def test_recurrence_drift_normalizes_via_weight_score(
        self, monkeypatch, tmp_path,
    ):
        """High recurrence_count saturates via log-scale at the
        collector via weight_score."""
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "c"),
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.curiosity_producer_bridge import (  # noqa: E501
            feed_recurrence_drift,
        )
        reset_default_collector_for_tests()
        feed_recurrence_drift(
            region_or_path="recurring-failure",
            recurrence_count=50,
        )
        score = get_default_collector().score_for_cluster(
            "recurring-failure",
        )
        # Weight-score normalization caps near 1.0 for high counts
        assert 0.5 <= score.magnitude <= 1.0


# ---------------------------------------------------------------------------
# § 6 — CoherenceAuditor wire-up presence
# ---------------------------------------------------------------------------


class TestCoherenceAuditorWireUp:
    def test_recurrence_drift_calls_bridge(self):
        """coherence_auditor.py MUST lazy-import + call
        feed_recurrence_drift at the RECURRENCE_DRIFT emission
        site. Catches refactors that drop the wire-up."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "coherence_auditor.py"
        )
        source = path.read_text(encoding="utf-8")
        assert (
            "curiosity_producer_bridge" in source
        ), (
            "coherence_auditor.py must lazy-import "
            "curiosity_producer_bridge — RECURRENCE_DRIFT "
            "wire-up regressed"
        )
        assert "feed_recurrence_drift" in source


# ---------------------------------------------------------------------------
# § 7 — Producer bridge authority floor
# ---------------------------------------------------------------------------


class TestBridgeAuthorityFloor:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.sensor_governor",
    )

    def test_bridge_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_producer_bridge.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden
