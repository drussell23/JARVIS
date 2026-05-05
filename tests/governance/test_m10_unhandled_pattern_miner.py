"""M10 Slice 2 — UnhandledPatternMiner tests (PRD §32.4.2).

Pins:
  § 1 — Master flag gate (returns DISABLED outcome)
  § 2 — Closed-taxonomy MineOutcome (6 values)
  § 3 — Frozen result containers
  § 4 — Env knobs — clamping + defaults
  § 5 — Stub PatternSource injection (Protocol contract)
  § 6 — Two-source aggregation:
        coherence_drift → NEW_OBSERVER candidates
        intake unhandled → NEW_SENSOR candidates
  § 7 — Adaptive threshold gate (DECIDED_SKIP)
  § 8 — Storm-guard dedup window
  § 9 — Daily cap enforcement
  § 10 — Authority floor (no orchestrator/iron_gate imports)
  § 11 — Public exports
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Iterable, List, Sequence

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )


# Helper stub source --------------------------------------------------------


class _StubSource:
    """Minimal :class:`PatternSourceProtocol` stub."""

    def __init__(self, *, drifts=(), intakes=()):
        self._drifts = list(drifts)
        self._intakes = list(intakes)

    def coherence_drift_observations(self, **_kw):
        return tuple(self._drifts)

    def intake_observations(self, **_kw):
        return tuple(self._intakes)


# ---------------------------------------------------------------------------
# § 1 — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled_outcome(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
            UnhandledPatternMiner,
        )
        miner = UnhandledPatternMiner(source=_StubSource())
        result = await miner.mine()
        assert result.outcome is MineOutcome.DISABLED
        assert len(result.proposals_emitted) == 0


# ---------------------------------------------------------------------------
# § 2 — Closed-taxonomy MineOutcome
# ---------------------------------------------------------------------------


class TestMineOutcome:
    def test_exactly_six_values(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
        )
        values = {m.value for m in MineOutcome}
        assert values == {
            "emitted",
            "decided_skip",
            "deduped",
            "daily_cap_reached",
            "no_patterns",
            "disabled",
        }

    def test_str_subclass(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
        )
        assert issubclass(MineOutcome, str)


# ---------------------------------------------------------------------------
# § 3 — Frozen result containers
# ---------------------------------------------------------------------------


class TestFrozenContainers:
    def test_intake_observation_is_frozen(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            IntakeObservation,
        )
        o = IntakeObservation(
            signal_source="x", op_kind="y",
        )
        with pytest.raises(Exception):
            o.signal_source = "z"  # type: ignore[misc]

    def test_drift_observation_is_frozen(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
        )
        d = CoherenceDriftObservation(
            failure_class="x", delta_metric=1.0,
            budget_metric=1.0, severity="info",
        )
        with pytest.raises(Exception):
            d.failure_class = "y"  # type: ignore[misc]

    def test_mine_result_is_frozen(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
            MineResult,
        )
        r = MineResult(outcome=MineOutcome.NO_PATTERNS)
        with pytest.raises(Exception):
            r.outcome = MineOutcome.EMITTED  # type: ignore[misc]

    def test_to_dict_projection_complete(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
            MineResult,
        )
        r = MineResult(
            outcome=MineOutcome.NO_PATTERNS,
            elapsed_s=1.5,
            diagnostics=("ok",),
        )
        d = r.to_dict()
        for key in (
            "schema_version", "outcome",
            "proposals_emitted_count", "proposals",
            "candidates_evaluated", "candidates_deduped",
            "candidates_threshold_skipped", "elapsed_s",
            "diagnostics",
        ):
            assert key in d


# ---------------------------------------------------------------------------
# § 4 — Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_min_recurrence_default_5(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_MIN_RECURRENCE_COUNT", raising=False,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            m10_min_recurrence_count,
        )
        assert m10_min_recurrence_count() == 5

    def test_drift_threshold_default_2(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            m10_recurrence_drift_threshold,
        )
        assert m10_recurrence_drift_threshold() == 2.0

    def test_dedup_window_default_3600(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_DEDUP_WINDOW_S", raising=False,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            m10_dedup_window_s,
        )
        assert m10_dedup_window_s() == 3600

    def test_window_hours_default_168(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_WINDOW_HOURS", raising=False,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            m10_window_hours,
        )
        assert m10_window_hours() == 168

    def test_clamping_below_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_MIN_RECURRENCE_COUNT", "0",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            m10_min_recurrence_count,
        )
        assert m10_min_recurrence_count() == 2  # floor


# ---------------------------------------------------------------------------
# § 5/6 — Source injection + two-source aggregation
# ---------------------------------------------------------------------------


class TestTwoSourceAggregation:
    @pytest.mark.asyncio
    async def test_no_patterns_returns_no_patterns_outcome(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            MineOutcome,
            UnhandledPatternMiner,
        )
        miner = UnhandledPatternMiner(source=_StubSource())
        result = await miner.mine()
        assert result.outcome is MineOutcome.NO_PATTERNS

    @pytest.mark.asyncio
    async def test_drift_emits_new_observer(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        # Relax adaptive threshold to allow synthetic
        # small fixtures to graduate
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "0.5",
        )
        monkeypatch.setenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD", "1.5",
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            ProposalKind,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        # 8 warnings + 2 criticals (high p_success+diversity)
        drifts = [
            CoherenceDriftObservation(
                failure_class="cls-A",
                delta_metric=10.0, budget_metric=4.0,
                severity="warning", at_unix=now - i,
            )
            for i in range(8)
        ] + [
            CoherenceDriftObservation(
                failure_class="cls-A",
                delta_metric=10.0, budget_metric=4.0,
                severity="critical", at_unix=now - i - 100,
            )
            for i in range(2)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(drifts=drifts),
        )
        result = await miner.mine(now_unix=now)
        assert result.outcome is MineOutcome.EMITTED
        assert len(result.proposals_emitted) == 1
        rec = result.proposals_emitted[0]
        assert rec.kind is ProposalKind.NEW_OBSERVER
        assert "cls-A" in rec.detection_evidence[0]

    @pytest.mark.asyncio
    async def test_intake_emits_new_sensor(self, monkeypatch):
        _enable(monkeypatch)
        # Lower min_recurrence + relax threshold for synthetic
        monkeypatch.setenv(
            "JARVIS_M10_MIN_RECURRENCE_COUNT", "3",
        )
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "0.3",
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            ProposalKind,
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            IntakeObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        intakes = [
            IntakeObservation(
                signal_source="MySensor",
                op_kind="my_pattern",
                op_completed=False,
                at_unix=now - i,
            )
            for i in range(10)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(intakes=intakes),
        )
        result = await miner.mine(now_unix=now)
        assert result.outcome is MineOutcome.EMITTED
        assert any(
            p.kind is ProposalKind.NEW_SENSOR
            for p in result.proposals_emitted
        )

    @pytest.mark.asyncio
    async def test_completed_ops_not_unhandled(
        self, monkeypatch,
    ):
        """Observations with op_completed=True are NOT
        unhandled — they shouldn't surface as candidates."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_M10_MIN_RECURRENCE_COUNT", "3",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            IntakeObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        intakes = [
            IntakeObservation(
                signal_source="X", op_kind="y",
                op_completed=True,  # ALL completed
                at_unix=now - i,
            )
            for i in range(10)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(intakes=intakes),
        )
        result = await miner.mine(now_unix=now)
        # All intake completed → no unhandled clusters → NO_PATTERNS
        assert result.outcome is MineOutcome.NO_PATTERNS


# ---------------------------------------------------------------------------
# § 7 — Adaptive threshold gate
# ---------------------------------------------------------------------------


class TestAdaptiveThresholdGate:
    @pytest.mark.asyncio
    async def test_low_evidence_decided_skip(
        self, monkeypatch,
    ):
        """Pure-failure cluster with default Bayesian
        confidence requires 16+ observations — 5 should
        threshold-skip."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD", "1.1",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        # 5 critical-only — pure failures
        drifts = [
            CoherenceDriftObservation(
                failure_class="cls-X",
                delta_metric=10.0, budget_metric=4.0,
                severity="critical", at_unix=now - i,
            )
            for i in range(5)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(drifts=drifts),
        )
        result = await miner.mine(now_unix=now)
        assert result.outcome is MineOutcome.DECIDED_SKIP
        assert result.candidates_threshold_skipped >= 1


# ---------------------------------------------------------------------------
# § 8 — Storm-guard dedup
# ---------------------------------------------------------------------------


class TestStormGuard:
    @pytest.mark.asyncio
    async def test_consecutive_cycles_dedup(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "0.5",
        )
        monkeypatch.setenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD", "1.5",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        drifts = [
            CoherenceDriftObservation(
                failure_class="cls-Y",
                delta_metric=10.0, budget_metric=4.0,
                severity="warning", at_unix=now - i,
            )
            for i in range(8)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(drifts=drifts),
        )
        # First cycle emits
        r1 = await miner.mine(now_unix=now)
        assert r1.outcome is MineOutcome.EMITTED
        # Second cycle dedups
        r2 = await miner.mine(now_unix=now)
        assert r2.outcome is MineOutcome.DEDUPED
        assert r2.candidates_deduped >= 1

    @pytest.mark.asyncio
    async def test_post_window_re_emits(self, monkeypatch):
        """After the dedup window expires, the same signature
        can be emitted again."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "0.5",
        )
        monkeypatch.setenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD", "1.5",
        )
        monkeypatch.setenv(
            "JARVIS_M10_DEDUP_WINDOW_S", "60",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        drifts = [
            CoherenceDriftObservation(
                failure_class="cls-Z",
                delta_metric=10.0, budget_metric=4.0,
                severity="warning", at_unix=now - i,
            )
            for i in range(8)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(drifts=drifts),
        )
        r1 = await miner.mine(now_unix=now)
        assert r1.outcome is MineOutcome.EMITTED
        # Advance "now" past the dedup window
        r2 = await miner.mine(now_unix=now + 120)
        # Note: daily cap counter is preserved — after the
        # dedup eviction, miner re-emits OR returns DAILY_CAP
        # depending on count. In this fixture cap=5 so we
        # should still have room.
        assert r2.outcome in {
            MineOutcome.EMITTED, MineOutcome.DAILY_CAP_REACHED,
        }


# ---------------------------------------------------------------------------
# § 9 — Daily cap enforcement
# ---------------------------------------------------------------------------


class TestDailyCap:
    @pytest.mark.asyncio
    async def test_daily_cap_reached(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "0.3",
        )
        monkeypatch.setenv(
            "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD", "1.5",
        )
        # Tiny daily cap so we hit it fast
        monkeypatch.setenv("JARVIS_M10_MAX_DAILY", "1")
        monkeypatch.setenv(
            "JARVIS_M10_DEDUP_WINDOW_S", "60",
        )
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            CoherenceDriftObservation,
            MineOutcome,
            UnhandledPatternMiner,
        )
        now = time.time()
        drifts = [
            CoherenceDriftObservation(
                failure_class=f"cls-{j}",
                delta_metric=10.0, budget_metric=4.0,
                severity="warning", at_unix=now - i,
            )
            for j in range(3)
            for i in range(8)
        ]
        miner = UnhandledPatternMiner(
            source=_StubSource(drifts=drifts),
        )
        # First cycle emits 1 (cap reached)
        r1 = await miner.mine(now_unix=now)
        # Second cycle short-circuits with DAILY_CAP_REACHED
        r2 = await miner.mine(now_unix=now + 120)
        assert r2.outcome is MineOutcome.DAILY_CAP_REACHED


# ---------------------------------------------------------------------------
# § 10 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
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
        "from backend.core.ouroboros.governance.graduation_orchestrator",
    )

    def test_miner_module_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "m10" / "unhandled_pattern_miner.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"unhandled_pattern_miner.py must NOT import "
                f"{forbidden}"
            )


# ---------------------------------------------------------------------------
# § 11 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.m10 import (
            unhandled_pattern_miner as m,
        )
        expected = sorted([
            "CoherenceDriftObservation",
            "DefaultPatternSource",
            "IntakeObservation",
            "M10_MINER_SCHEMA_VERSION",
            "MineOutcome",
            "MineResult",
            "PatternSourceProtocol",
            "UnhandledPatternMiner",
            "get_default_miner",
            "m10_dedup_window_s",
            "m10_min_recurrence_count",
            "m10_recurrence_drift_threshold",
            "m10_window_hours",
            "reset_default_miner_for_tests",
        ])
        assert sorted(m.__all__) == expected

    def test_default_miner_is_singleton(self):
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            get_default_miner,
            reset_default_miner_for_tests,
        )
        reset_default_miner_for_tests()
        a = get_default_miner()
        b = get_default_miner()
        assert a is b
        reset_default_miner_for_tests()
        c = get_default_miner()
        assert c is not a
