"""M9 Slice 3 — SensorGovernor + CuriosityGradient consumer tests
(PRD §30.5.1).

Pins the additive Slice 3 contract:
  § 1 — `SensorBudgetSpec.curiosity_aware` field default-False
  § 2 — `_curiosity_multiplier_for` lazy-import safety
  § 3 — `_weighted_cap` curiosity composition (multiplicative)
  § 4 — `request_budget` cluster_id parameter (additive, opt-in)
  § 5 — Pre-graduation behavior preserved (M9 off → multiplier 1.0)
  § 6 — Bounded multiplier cannot bypass global cap
  § 7 — Three target sensors marked curiosity-aware in seed
  § 8 — BudgetDecision exposes curiosity fields (observability)
  § 9 — Authority floor preserved (no eager M9 import in governor)
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _enable_governor(monkeypatch):
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")


def _enable_m9(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "cur"),
    )
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
    )


# ---------------------------------------------------------------------------
# § 1 — SensorBudgetSpec.curiosity_aware default-False
# ---------------------------------------------------------------------------


class TestCuriosityAwareField:
    def test_default_is_false(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec,
        )
        spec = SensorBudgetSpec(
            sensor_name="x", base_cap_per_hour=10,
        )
        assert spec.curiosity_aware is False

    def test_can_be_set_true(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec,
        )
        spec = SensorBudgetSpec(
            sensor_name="x", base_cap_per_hour=10,
            curiosity_aware=True,
        )
        assert spec.curiosity_aware is True


# ---------------------------------------------------------------------------
# § 2 — _curiosity_multiplier_for lazy-import safety
# ---------------------------------------------------------------------------


class TestCuriosityMultiplierFor:
    def test_none_cluster_id_returns_1_0(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            _curiosity_multiplier_for,
        )
        mult, cid = _curiosity_multiplier_for(None)
        assert mult == 1.0
        assert cid is None

    def test_empty_cluster_id_returns_1_0(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            _curiosity_multiplier_for,
        )
        mult, cid = _curiosity_multiplier_for("")
        assert mult == 1.0
        assert cid is None

    def test_m9_master_off_returns_1_0(self, monkeypatch):
        """When M9 master flag is off, score returns DISABLED →
        multiplier=1.0 + cluster_id=cid still passed back for
        operator-explainability. Defensive — even M9 off should
        give a well-formed (1.0, cid) tuple."""
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            _curiosity_multiplier_for,
        )
        mult, cid = _curiosity_multiplier_for("backend")
        assert mult == 1.0
        # cid resolves regardless of M9 master flag
        assert cid in ("backend", None)

    def test_m9_on_cold_start_returns_1_0(
        self, monkeypatch, tmp_path,
    ):
        """M9 on but no observations yet → INSUFFICIENT_DATA →
        multiplier=1.0."""
        _enable_m9(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            reset_default_collector_for_tests,
        )
        reset_default_collector_for_tests()
        from backend.core.ouroboros.governance.sensor_governor import (
            _curiosity_multiplier_for,
        )
        mult, cid = _curiosity_multiplier_for("untracked-cluster")
        assert mult == 1.0


# ---------------------------------------------------------------------------
# § 3 — _weighted_cap curiosity composition
# ---------------------------------------------------------------------------


class TestWeightedCapComposition:
    def test_default_multiplier_byte_identical(self):
        """multiplier=1.0 (default) must produce identical cap
        as pre-Slice-3 formula."""
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        spec = SensorBudgetSpec(
            sensor_name="x", base_cap_per_hour=100,
        )
        g = SensorGovernor()
        cap_default = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=False,
        )
        cap_explicit_1 = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=False,
            curiosity_multiplier=1.0,
        )
        assert cap_default == cap_explicit_1

    def test_multiplier_amplifies(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        spec = SensorBudgetSpec(
            sensor_name="x", base_cap_per_hour=100,
        )
        g = SensorGovernor()
        cap_low = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=False,
            curiosity_multiplier=0.5,
        )
        cap_high = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=False,
            curiosity_multiplier=2.0,
        )
        assert cap_low < cap_high
        # Bounded by floor=1
        assert cap_low >= 1
        # Within bounds — 100 * 2.0 = 200
        assert cap_high == 200

    def test_curiosity_composes_with_brake(self):
        """Curiosity multiplier composes BEFORE emergency brake;
        both factors apply."""
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        spec = SensorBudgetSpec(
            sensor_name="x", base_cap_per_hour=100,
        )
        g = SensorGovernor()
        # base=100, curiosity=2.0, brake=0.2 (default reduction)
        # → 100 * 2.0 * 0.2 = 40
        cap = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=True,
            curiosity_multiplier=2.0,
        )
        # The brake reduction is env-tunable; just verify it's
        # smaller than no-brake
        cap_no_brake = g._weighted_cap(
            spec, Urgency.STANDARD, posture=None, brake=False,
            curiosity_multiplier=2.0,
        )
        assert cap < cap_no_brake


# ---------------------------------------------------------------------------
# § 4 — request_budget cluster_id parameter (additive)
# ---------------------------------------------------------------------------


class TestRequestBudgetClusterIdParam:
    def test_request_budget_works_without_cluster_id(
        self, monkeypatch,
    ):
        """Existing API shape preserved — request_budget without
        cluster_id keyword behaves as pre-Slice-3."""
        _enable_governor(monkeypatch)
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="x", base_cap_per_hour=10,
            ),
        )
        decision = g.request_budget("x", Urgency.STANDARD)
        assert decision.allowed is True
        assert decision.curiosity_multiplier == 1.0
        assert decision.curiosity_cluster_id is None

    def test_non_curiosity_aware_sensor_ignores_cluster_id(
        self, monkeypatch, tmp_path,
    ):
        """Sensor with curiosity_aware=False MUST NOT consult
        the collector even when cluster_id is supplied."""
        _enable_governor(monkeypatch)
        _enable_m9(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        reset_default_collector_for_tests()
        # Pre-populate a high-magnitude score
        import time as _time
        coll = get_default_collector()
        for _ in range(8):
            coll.record_logprob_entropy(
                "hot-cluster", 1.0, at_unix=_time.time(),
            )
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="not-aware",
                base_cap_per_hour=10,
                curiosity_aware=False,
            ),
        )
        decision = g.request_budget(
            "not-aware", Urgency.STANDARD,
            cluster_id="hot-cluster",
        )
        # Non-aware sensor ignores curiosity entirely
        assert decision.curiosity_multiplier == 1.0
        assert decision.curiosity_cluster_id is None

    def test_curiosity_aware_sensor_with_high_curiosity(
        self, monkeypatch, tmp_path,
    ):
        _enable_governor(monkeypatch)
        _enable_m9(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        reset_default_collector_for_tests()
        # Pre-populate high-magnitude multi-source score:
        # - 4 logprob (max value) + 4 prophecy + 4 recurrence
        import time as _time
        coll = get_default_collector()
        now = _time.time()
        for i in range(4):
            coll.record_logprob_entropy(
                "hot", 1.0, at_unix=now + i * 0.01,
            )
            coll.record_prophecy_error(
                "hot", 1.0, at_unix=now + i * 0.01 + 0.001,
            )
            coll.record_recurrence_drift(
                "hot", 50, at_unix=now + i * 0.01 + 0.002,
            )
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="aware",
                base_cap_per_hour=100,
                curiosity_aware=True,
            ),
        )
        decision = g.request_budget(
            "aware", Urgency.STANDARD, cluster_id="hot",
        )
        # High curiosity → multiplier > 1.0
        assert decision.curiosity_multiplier > 1.0
        assert decision.curiosity_cluster_id == "hot"
        # Cap should be amplified
        assert decision.weighted_cap > 100

    def test_curiosity_aware_sensor_with_no_data_defaults_to_1(
        self, monkeypatch, tmp_path,
    ):
        """No observations for cluster → cold-start →
        multiplier=1.0."""
        _enable_governor(monkeypatch)
        _enable_m9(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        reset_default_collector_for_tests()
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="aware",
                base_cap_per_hour=100,
                curiosity_aware=True,
            ),
        )
        decision = g.request_budget(
            "aware", Urgency.STANDARD, cluster_id="never-recorded",
        )
        assert decision.curiosity_multiplier == 1.0


# ---------------------------------------------------------------------------
# § 5 — Pre-graduation behavior preserved
# ---------------------------------------------------------------------------


class TestPreGraduationByteIdentity:
    def test_m9_master_off_no_curiosity_effect(
        self, monkeypatch, tmp_path,
    ):
        """When M9 master flag is OFF, even a curiosity-aware
        sensor with cluster_id must see multiplier=1.0."""
        _enable_governor(monkeypatch)
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="aware",
                base_cap_per_hour=100,
                curiosity_aware=True,
            ),
        )
        decision = g.request_budget(
            "aware", Urgency.STANDARD, cluster_id="hot",
        )
        # M9 off → multiplier 1.0 regardless of opt-in
        assert decision.curiosity_multiplier == 1.0
        assert decision.weighted_cap == 100  # base cap byte-identical


# ---------------------------------------------------------------------------
# § 6 — Bounded multiplier cannot bypass global cap
# ---------------------------------------------------------------------------


class TestBoundedMultiplier:
    def test_global_cap_still_enforced_with_high_curiosity(
        self, monkeypatch, tmp_path,
    ):
        """Even with maximum curiosity boost (2× default),
        the GLOBAL emission cap is independent — gremaining
        check uses the un-curiosity-modified gcap."""
        _enable_governor(monkeypatch)
        _enable_m9(monkeypatch, tmp_path)
        # Tiny global cap so we can exhaust it
        monkeypatch.setenv(
            "JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR", "5",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec, SensorGovernor, Urgency,
        )
        reset_default_collector_for_tests()
        # Maximum-curiosity score
        import time as _time
        coll = get_default_collector()
        now = _time.time()
        for i in range(8):
            coll.record_logprob_entropy(
                "hot", 1.0, at_unix=now + i * 0.001,
            )
        g = SensorGovernor()
        g.register(
            SensorBudgetSpec(
                sensor_name="aware",
                base_cap_per_hour=100,
                curiosity_aware=True,
            ),
        )
        # Exhaust the global cap by recording emissions
        for _ in range(5):
            g.record_emission("aware", Urgency.STANDARD)
        # Now request another slot — should be denied even
        # though sensor cap (curiosity-amplified) has headroom
        decision = g.request_budget(
            "aware", Urgency.STANDARD, cluster_id="hot",
        )
        assert decision.allowed is False
        assert (
            decision.reason_code
            == "governor.global_cap_exhausted"
        )


# ---------------------------------------------------------------------------
# § 7 — Three target sensors marked curiosity-aware in seed
# ---------------------------------------------------------------------------


class TestTargetSensorsCuriosityAware:
    def test_opportunity_miner_is_curiosity_aware(self):
        from backend.core.ouroboros.governance.sensor_governor_seed import (  # noqa: E501
            SEED_SPECS,
        )
        miner = next(
            s for s in SEED_SPECS
            if s.sensor_name == "OpportunityMinerSensor"
        )
        assert miner.curiosity_aware is True

    def test_proactive_exploration_is_curiosity_aware(self):
        from backend.core.ouroboros.governance.sensor_governor_seed import (  # noqa: E501
            SEED_SPECS,
        )
        exp = next(
            s for s in SEED_SPECS
            if s.sensor_name == "ProactiveExplorationSensor"
        )
        assert exp.curiosity_aware is True

    def test_capability_gap_is_curiosity_aware(self):
        from backend.core.ouroboros.governance.sensor_governor_seed import (  # noqa: E501
            SEED_SPECS,
        )
        gap = next(
            s for s in SEED_SPECS
            if s.sensor_name == "CapabilityGapSensor"
        )
        assert gap.curiosity_aware is True

    def test_other_sensors_are_not_curiosity_aware(self):
        """Conservative opt-in — only the 3 target sensors gain
        the bias. All others stay at default False."""
        from backend.core.ouroboros.governance.sensor_governor_seed import (  # noqa: E501
            SEED_SPECS,
        )
        target = {
            "OpportunityMinerSensor",
            "ProactiveExplorationSensor",
            "CapabilityGapSensor",
        }
        for spec in SEED_SPECS:
            if spec.sensor_name in target:
                continue
            assert spec.curiosity_aware is False, (
                f"{spec.sensor_name} should not be curiosity-"
                f"aware in Slice 3 — only 3 target sensors opt in"
            )


# ---------------------------------------------------------------------------
# § 8 — BudgetDecision exposes curiosity fields
# ---------------------------------------------------------------------------


class TestBudgetDecisionObservability:
    def test_curiosity_fields_in_to_dict(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            BudgetDecision, Urgency,
        )
        d = BudgetDecision(
            allowed=True, sensor_name="x",
            urgency=Urgency.STANDARD,
            posture=None, weighted_cap=10,
            current_count=0, remaining=10,
            reason_code="governor.ok",
            curiosity_multiplier=1.5,
            curiosity_cluster_id="hot",
        )
        proj = d.to_dict()
        assert proj["curiosity_multiplier"] == 1.5
        assert proj["curiosity_cluster_id"] == "hot"

    def test_curiosity_defaults_in_to_dict(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            BudgetDecision, Urgency,
        )
        d = BudgetDecision(
            allowed=True, sensor_name="x",
            urgency=Urgency.STANDARD,
            posture=None, weighted_cap=10,
            current_count=0, remaining=10,
            reason_code="governor.ok",
        )
        proj = d.to_dict()
        assert proj["curiosity_multiplier"] == 1.0
        assert proj["curiosity_cluster_id"] is None


# ---------------------------------------------------------------------------
# § 9 — Authority floor preserved
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_governor_does_not_eagerly_import_m9(self):
        """M9 modules MUST be lazy-imported inside
        _curiosity_multiplier_for, never at module load time.
        Pinned by source-grep so a refactor that moves the
        import to top-of-file trips immediately."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "sensor_governor.py"
        )
        source = path.read_text(encoding="utf-8")
        # Module-level imports start with "^from " or "^import "
        # at column 0. Both M9 imports MUST live inside a function
        # body (indented). We check that no top-level line begins
        # with the M9 import.
        for line in source.splitlines():
            assert not line.startswith(
                "from backend.core.ouroboros.governance"
                ".curiosity_gradient",
            ), "M9 must be lazy-imported"
            assert not line.startswith(
                "from backend.core.ouroboros.governance"
                ".curiosity_collector",
            ), "M9 must be lazy-imported"
        # And we DO see the imports indented inside the helper
        assert "_curiosity_multiplier_for" in source
        assert "from backend.core.ouroboros.governance.curiosity_collector" in source
        assert "from backend.core.ouroboros.governance.curiosity_gradient" in source
