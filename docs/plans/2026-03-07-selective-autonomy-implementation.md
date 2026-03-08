# Selective Autonomy (Layer 4) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the selective autonomy layer that gates autonomous operations through trust graduation, per-signal autonomy tiers, and a multi-system autonomy gate integrating CAI/UAE/SAI context.

**Architecture:** Four modules under `backend/core/ouroboros/governance/autonomy/`. AutonomyTier enum + frozen config dataclasses define the per-signal autonomy levels. AutonomyGate checks CAI (cognitive load, work context), UAE (pattern confidence), and SAI (resource pressure, system state) before proceeding. TrustGraduator tracks operational history and promotes/demotes tiers based on success/failure. AutonomyState persists tier data to `~/.jarvis/autonomy/state.json` across process restarts. Lightweight snapshot dataclasses abstract the intelligence systems so Layer 4 doesn't import their full implementations.

**Tech Stack:** Python 3.9+, asyncio, existing `DegradationController`, existing `IntentSignal`, existing `RiskTier`, JSON persistence.

**Design doc:** `docs/plans/2026-03-07-autonomous-layers-design.md` §5 (Layer 4)

**Existing code to build on:**
- `backend/core/ouroboros/governance/degradation.py`: `DegradationMode`, `DegradationController.safe_auto_allowed`
- `backend/core/ouroboros/governance/risk_engine.py`: `RiskTier` (SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED)
- `backend/core/ouroboros/governance/intent/signals.py`: `IntentSignal` with `.source`, `.repo`, `.target_files`, `.stable`
- `backend/core/ouroboros/governance/governed_loop_service.py`: `GovernedLoopService.submit(ctx, trigger_source=)`

---

## Task 1: AutonomyTier Enum + Snapshot Dataclasses

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/tiers.py`
- Create: `tests/governance/autonomy/__init__.py`
- Create: `tests/governance/autonomy/test_tiers.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/autonomy/test_tiers.py"""
import pytest


class TestAutonomyTier:
    def test_four_tiers_exist(self):
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        assert AutonomyTier.OBSERVE.value == "observe"
        assert AutonomyTier.SUGGEST.value == "suggest"
        assert AutonomyTier.GOVERNED.value == "governed"
        assert AutonomyTier.AUTONOMOUS.value == "autonomous"

    def test_tier_ordering(self):
        """Tiers have a defined progression order."""
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier,
            TIER_ORDER,
        )

        assert TIER_ORDER.index(AutonomyTier.OBSERVE) < TIER_ORDER.index(
            AutonomyTier.SUGGEST
        )
        assert TIER_ORDER.index(AutonomyTier.SUGGEST) < TIER_ORDER.index(
            AutonomyTier.GOVERNED
        )
        assert TIER_ORDER.index(AutonomyTier.GOVERNED) < TIER_ORDER.index(
            AutonomyTier.AUTONOMOUS
        )


class TestCognitiveLoad:
    def test_ordering(self):
        from backend.core.ouroboros.governance.autonomy.tiers import CognitiveLoad

        assert CognitiveLoad.LOW < CognitiveLoad.MEDIUM < CognitiveLoad.HIGH


class TestWorkContext:
    def test_values(self):
        from backend.core.ouroboros.governance.autonomy.tiers import WorkContext

        assert WorkContext.CODING.value == "coding"
        assert WorkContext.MEETINGS.value == "meetings"


class TestCAISnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import (
            CAISnapshot,
            CognitiveLoad,
            WorkContext,
        )

        snap = CAISnapshot(
            cognitive_load=CognitiveLoad.LOW,
            work_context=WorkContext.CODING,
            safety_level="SAFE",
        )
        with pytest.raises(AttributeError):
            snap.cognitive_load = CognitiveLoad.HIGH  # type: ignore[misc]


class TestUAESnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import UAESnapshot

        snap = UAESnapshot(confidence=0.85)
        with pytest.raises(AttributeError):
            snap.confidence = 0.5  # type: ignore[misc]


class TestSAISnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import SAISnapshot

        snap = SAISnapshot(
            ram_percent=45.0,
            system_locked=False,
            anomaly_detected=False,
        )
        with pytest.raises(AttributeError):
            snap.ram_percent = 90.0  # type: ignore[misc]


class TestGraduationMetrics:
    def test_defaults(self):
        from backend.core.ouroboros.governance.autonomy.tiers import GraduationMetrics

        m = GraduationMetrics()
        assert m.observations == 0
        assert m.false_positives == 0
        assert m.successful_ops == 0
        assert m.rollback_count == 0
        assert m.postmortem_streak == 0
        assert m.human_confirmations == 0


class TestSignalAutonomyConfig:
    def test_frozen_with_defaults(self):
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier,
            CognitiveLoad,
            GraduationMetrics,
            SignalAutonomyConfig,
            WorkContext,
        )

        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure",
            repo="jarvis",
            canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED,
            graduation_metrics=GraduationMetrics(),
        )
        assert config.defer_during_cognitive_load == CognitiveLoad.HIGH
        assert config.defer_during_work_context == (WorkContext.MEETINGS,)
        assert config.require_user_active is False
        with pytest.raises(AttributeError):
            config.current_tier = AutonomyTier.OBSERVE  # type: ignore[misc]

    def test_config_key(self):
        """config_key is (trigger_source, repo, canary_slice)."""
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier,
            GraduationMetrics,
            SignalAutonomyConfig,
        )

        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure",
            repo="jarvis",
            canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED,
            graduation_metrics=GraduationMetrics(),
        )
        assert config.config_key == ("intent:test_failure", "jarvis", "tests/")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/autonomy/test_tiers.py -v`
Expected: FAIL — module does not exist

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/autonomy/tiers.py

Autonomy tier definitions, intelligence snapshots, and per-signal configuration.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AutonomyTier(enum.Enum):
    """Selective autonomy levels for the self-programming pipeline."""

    OBSERVE = "observe"
    SUGGEST = "suggest"
    GOVERNED = "governed"
    AUTONOMOUS = "autonomous"


TIER_ORDER: Tuple[AutonomyTier, ...] = (
    AutonomyTier.OBSERVE,
    AutonomyTier.SUGGEST,
    AutonomyTier.GOVERNED,
    AutonomyTier.AUTONOMOUS,
)


class CognitiveLoad(enum.IntEnum):
    """User cognitive load levels (from CAI)."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2


class WorkContext(enum.Enum):
    """User work context categories (from CAI)."""

    CODING = "coding"
    REVIEWING = "reviewing"
    MEETINGS = "meetings"
    IDLE = "idle"


# ---------------------------------------------------------------------------
# Intelligence Snapshots — lightweight abstractions over CAI/UAE/SAI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CAISnapshot:
    """Point-in-time context from Context Awareness Intelligence."""

    cognitive_load: CognitiveLoad
    work_context: WorkContext
    safety_level: str  # "SAFE" | "CAUTION" | "UNSAFE"


@dataclass(frozen=True)
class UAESnapshot:
    """Point-in-time context from Unified Awareness Engine."""

    confidence: float  # 0.0 -- 1.0, historical pattern confidence


@dataclass(frozen=True)
class SAISnapshot:
    """Point-in-time context from Situational Awareness Intelligence."""

    ram_percent: float
    system_locked: bool
    anomaly_detected: bool


# ---------------------------------------------------------------------------
# Graduation Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraduationMetrics:
    """Tracks operational history for trust graduation decisions."""

    observations: int = 0
    false_positives: int = 0
    successful_ops: int = 0
    rollback_count: int = 0
    postmortem_streak: int = 0
    human_confirmations: int = 0


# ---------------------------------------------------------------------------
# Per-Signal Autonomy Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalAutonomyConfig:
    """Autonomy configuration for a (trigger_source, repo, canary_slice) triple."""

    trigger_source: str
    repo: str
    canary_slice: str
    current_tier: AutonomyTier
    graduation_metrics: GraduationMetrics

    # CAI overrides
    defer_during_cognitive_load: CognitiveLoad = CognitiveLoad.HIGH
    defer_during_work_context: Tuple[WorkContext, ...] = (WorkContext.MEETINGS,)
    require_user_active: bool = False

    @property
    def config_key(self) -> Tuple[str, str, str]:
        """Unique key for this config: (trigger_source, repo, canary_slice)."""
        return (self.trigger_source, self.repo, self.canary_slice)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/autonomy/test_tiers.py -v`
Expected: 10 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/__init__.py backend/core/ouroboros/governance/autonomy/tiers.py tests/governance/autonomy/__init__.py tests/governance/autonomy/test_tiers.py
git commit -m "feat(autonomy): add AutonomyTier enum, snapshots, and SignalAutonomyConfig"
```

---

## Task 2: AutonomyGate — should_proceed() Decision Function

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/gate.py`
- Create: `tests/governance/autonomy/test_gate.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/autonomy/test_gate.py"""
import pytest
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier,
    CAISnapshot,
    CognitiveLoad,
    GraduationMetrics,
    SAISnapshot,
    SignalAutonomyConfig,
    UAESnapshot,
    WorkContext,
)


def _make_config(tier: AutonomyTier = AutonomyTier.GOVERNED) -> SignalAutonomyConfig:
    return SignalAutonomyConfig(
        trigger_source="intent:test_failure",
        repo="jarvis",
        canary_slice="tests/",
        current_tier=tier,
        graduation_metrics=GraduationMetrics(),
    )


def _cai(
    load: CognitiveLoad = CognitiveLoad.LOW,
    ctx: WorkContext = WorkContext.CODING,
    safety: str = "SAFE",
) -> CAISnapshot:
    return CAISnapshot(cognitive_load=load, work_context=ctx, safety_level=safety)


def _uae(confidence: float = 0.85) -> UAESnapshot:
    return UAESnapshot(confidence=confidence)


def _sai(
    ram: float = 45.0, locked: bool = False, anomaly: bool = False,
) -> SAISnapshot:
    return SAISnapshot(ram_percent=ram, system_locked=locked, anomaly_detected=anomaly)


class TestGateObserveTierBlocks:
    @pytest.mark.asyncio
    async def test_observe_tier_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(AutonomyTier.OBSERVE), _cai(), _uae(), _sai()
        )
        assert proceed is False
        assert reason == "tier:observe_only"


class TestGateCognitiveLoadBlocks:
    @pytest.mark.asyncio
    async def test_high_cognitive_load_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(load=CognitiveLoad.HIGH), _uae(), _sai()
        )
        assert proceed is False
        assert reason == "cai:cognitive_load_high"


class TestGateWorkContextBlocks:
    @pytest.mark.asyncio
    async def test_meeting_context_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(ctx=WorkContext.MEETINGS), _uae(), _sai()
        )
        assert proceed is False
        assert reason == "cai:in_meeting"


class TestGateMemoryPressureBlocks:
    @pytest.mark.asyncio
    async def test_high_ram_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(), _uae(), _sai(ram=95.0)
        )
        assert proceed is False
        assert reason == "sai:memory_pressure"


class TestGateScreenLockedBlocks:
    @pytest.mark.asyncio
    async def test_screen_locked_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(), _uae(), _sai(locked=True)
        )
        assert proceed is False
        assert reason == "sai:screen_locked"


class TestGateLowConfidenceBlocks:
    @pytest.mark.asyncio
    async def test_low_uae_confidence_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(), _uae(confidence=0.3), _sai()
        )
        assert proceed is False
        assert reason == "uae:low_pattern_confidence"


class TestGateCrossSystemDisagreement:
    @pytest.mark.asyncio
    async def test_cai_safe_but_sai_anomaly_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(),
            _cai(safety="SAFE"),
            _uae(),
            _sai(anomaly=True),
        )
        assert proceed is False
        assert reason == "disagreement:cai_safe_sai_anomaly"


class TestGateProceeds:
    @pytest.mark.asyncio
    async def test_all_clear_proceeds(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(), _uae(), _sai()
        )
        assert proceed is True
        assert reason == "proceed"

    @pytest.mark.asyncio
    async def test_suggest_tier_proceeds(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(AutonomyTier.SUGGEST), _cai(), _uae(), _sai()
        )
        assert proceed is True
        assert reason == "proceed"

    @pytest.mark.asyncio
    async def test_medium_cognitive_load_proceeds(self):
        """MEDIUM load is below the HIGH threshold, so it should proceed."""
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(
            _make_config(), _cai(load=CognitiveLoad.MEDIUM), _uae(), _sai()
        )
        assert proceed is True
        assert reason == "proceed"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/autonomy/test_gate.py -v`
Expected: FAIL — module does not exist

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/autonomy/gate.py

Autonomy Gate — multi-system decision function.

Checks CAI (cognitive load, work context), UAE (pattern confidence), and
SAI (resource pressure, system state) before allowing an autonomous operation.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import logging
from typing import Tuple

from .tiers import (
    AutonomyTier,
    CAISnapshot,
    SAISnapshot,
    SignalAutonomyConfig,
    UAESnapshot,
)

logger = logging.getLogger(__name__)

# RAM threshold above which we block (configurable via env in future)
_RAM_PRESSURE_THRESHOLD = 90.0

# UAE confidence below which we block
_UAE_CONFIDENCE_THRESHOLD = 0.6


class AutonomyGate:
    """Multi-system gate for autonomous operations.

    Evaluates a 6-step priority chain to decide whether an operation
    should proceed or defer.
    """

    async def should_proceed(
        self,
        config: SignalAutonomyConfig,
        cai: CAISnapshot,
        uae: UAESnapshot,
        sai: SAISnapshot,
    ) -> Tuple[bool, str]:
        """Decide whether to auto-proceed or defer.

        Returns (proceed, reason_code).
        """
        # 1. Autonomy tier check
        if config.current_tier is AutonomyTier.OBSERVE:
            return False, "tier:observe_only"

        # 2. CAI: Cognitive load
        if cai.cognitive_load >= config.defer_during_cognitive_load:
            return False, "cai:cognitive_load_high"

        # 3. CAI: Work context
        if cai.work_context in config.defer_during_work_context:
            return False, "cai:in_meeting"

        # 4. SAI: Resource pressure
        if sai.ram_percent > _RAM_PRESSURE_THRESHOLD:
            return False, "sai:memory_pressure"

        # 5. SAI: Screen locked
        if sai.system_locked:
            return False, "sai:screen_locked"

        # 6. UAE: Historical pattern confidence
        if uae.confidence < _UAE_CONFIDENCE_THRESHOLD:
            return False, "uae:low_pattern_confidence"

        # 7. Cross-system agreement
        if sai.anomaly_detected and cai.safety_level == "SAFE":
            return False, "disagreement:cai_safe_sai_anomaly"

        return True, "proceed"
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/autonomy/test_gate.py -v`
Expected: 10 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/gate.py tests/governance/autonomy/test_gate.py
git commit -m "feat(autonomy): add AutonomyGate with multi-system should_proceed()"
```

---

## Task 3: TrustGraduator — Tier Promotion and Demotion

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/graduator.py`
- Create: `tests/governance/autonomy/test_graduator.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/autonomy/test_graduator.py"""
import pytest
from dataclasses import replace
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier,
    GraduationMetrics,
    SignalAutonomyConfig,
)


def _make_config(
    tier: AutonomyTier = AutonomyTier.OBSERVE,
    metrics: GraduationMetrics = None,
) -> SignalAutonomyConfig:
    return SignalAutonomyConfig(
        trigger_source="intent:test_failure",
        repo="jarvis",
        canary_slice="tests/",
        current_tier=tier,
        graduation_metrics=metrics or GraduationMetrics(),
    )


class TestGraduatorRegistration:
    def test_register_and_get(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        config = _make_config()
        grad.register(config)
        retrieved = grad.get_config("intent:test_failure", "jarvis", "tests/")
        assert retrieved.current_tier == AutonomyTier.OBSERVE

    def test_get_unknown_returns_none(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        assert grad.get_config("unknown", "unknown", "unknown") is None


class TestGraduatorObserveToSuggest:
    def test_promotes_after_20_observations_5_confirmations(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(
            observations=20,
            false_positives=0,
            human_confirmations=5,
        )
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result == AutonomyTier.SUGGEST

    def test_no_promote_with_false_positives(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(
            observations=20,
            false_positives=1,
            human_confirmations=5,
        )
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result is None

    def test_no_promote_insufficient_observations(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(
            observations=15,
            false_positives=0,
            human_confirmations=5,
        )
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result is None


class TestGraduatorSuggestToGoverned:
    def test_promotes_after_30_successful_ops_low_rollback(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(successful_ops=30, rollback_count=1)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.SUGGEST, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result == AutonomyTier.GOVERNED

    def test_no_promote_high_rollback_rate(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        # rollback_count / successful_ops = 2/30 = 6.7% > 5%
        metrics = GraduationMetrics(successful_ops=30, rollback_count=2)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.SUGGEST, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result is None


class TestGraduatorGovernedToAutonomous:
    def test_promotes_after_50_ops_zero_rollbacks(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(successful_ops=50, rollback_count=0)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result == AutonomyTier.AUTONOMOUS

    def test_no_promote_with_rollbacks(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(successful_ops=50, rollback_count=1)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result is None


class TestGraduatorAlreadyAutonomous:
    def test_no_promote_beyond_autonomous(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        metrics = GraduationMetrics(successful_ops=100, rollback_count=0)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.AUTONOMOUS, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result is None


class TestGraduatorDemotion:
    def test_rollback_demotes_autonomous_to_governed(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.AUTONOMOUS))
        new_tier = grad.demote(
            "intent:test_failure", "jarvis", "tests/", "rollback"
        )
        assert new_tier == AutonomyTier.GOVERNED

    def test_postmortem_streak_demotes_to_suggest(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED))
        new_tier = grad.demote(
            "intent:test_failure", "jarvis", "tests/", "postmortem_streak"
        )
        assert new_tier == AutonomyTier.SUGGEST

    def test_anomaly_demotes_to_observe(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED))
        new_tier = grad.demote(
            "intent:test_failure", "jarvis", "tests/", "anomaly"
        )
        assert new_tier == AutonomyTier.OBSERVE

    def test_break_glass_demotes_all_to_observe(self):
        from backend.core.ouroboros.governance.autonomy.graduator import (
            TrustGraduator,
        )

        grad = TrustGraduator()
        grad.register(
            _make_config(AutonomyTier.AUTONOMOUS)
        )
        grad.register(
            SignalAutonomyConfig(
                trigger_source="intent:stack_trace",
                repo="prime",
                canary_slice="tests/",
                current_tier=AutonomyTier.GOVERNED,
                graduation_metrics=GraduationMetrics(),
            )
        )
        grad.break_glass_reset()
        c1 = grad.get_config("intent:test_failure", "jarvis", "tests/")
        c2 = grad.get_config("intent:stack_trace", "prime", "tests/")
        assert c1.current_tier == AutonomyTier.OBSERVE
        assert c2.current_tier == AutonomyTier.OBSERVE
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/autonomy/test_graduator.py -v`
Expected: FAIL — module does not exist

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/autonomy/graduator.py

Trust Graduator — tier promotion and demotion engine.

Tracks operational history per (trigger_source, repo, canary_slice) triple
and promotes/demotes autonomy tiers based on the Trust Graduation Model.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, Optional, Tuple

from .tiers import (
    AutonomyTier,
    GraduationMetrics,
    SignalAutonomyConfig,
    TIER_ORDER,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graduation Criteria
# ---------------------------------------------------------------------------

# OBSERVE -> SUGGEST
_MIN_OBSERVATIONS = 20
_MAX_FALSE_POSITIVES = 0
_MIN_HUMAN_CONFIRMATIONS = 5

# SUGGEST -> GOVERNED
_MIN_SUCCESSFUL_OPS_GOVERNED = 30
_MAX_ROLLBACK_RATE = 0.05  # 5%

# GOVERNED -> AUTONOMOUS
_MIN_SUCCESSFUL_OPS_AUTONOMOUS = 50
_MAX_ROLLBACKS_AUTONOMOUS = 0

# Demotion targets
_DEMOTION_MAP: Dict[str, AutonomyTier] = {
    "rollback": AutonomyTier.GOVERNED,
    "postmortem_streak": AutonomyTier.SUGGEST,
    "anomaly": AutonomyTier.OBSERVE,
    "break_glass": AutonomyTier.OBSERVE,
}

ConfigKey = Tuple[str, str, str]


class TrustGraduator:
    """Manages autonomy tier promotions and demotions."""

    def __init__(self) -> None:
        self._configs: Dict[ConfigKey, SignalAutonomyConfig] = {}

    def register(self, config: SignalAutonomyConfig) -> None:
        """Register or replace a signal autonomy config."""
        self._configs[config.config_key] = config

    def get_config(
        self, trigger_source: str, repo: str, canary_slice: str,
    ) -> Optional[SignalAutonomyConfig]:
        """Get config for a triple, or None if not registered."""
        return self._configs.get((trigger_source, repo, canary_slice))

    def all_configs(self) -> Tuple[SignalAutonomyConfig, ...]:
        """Return all registered configs."""
        return tuple(self._configs.values())

    def check_graduation(
        self, trigger_source: str, repo: str, canary_slice: str,
    ) -> Optional[AutonomyTier]:
        """Check if a triple qualifies for promotion.

        Returns the new tier if promotion is warranted, None otherwise.
        Does NOT apply the promotion — call promote() to persist it.
        """
        config = self._configs.get((trigger_source, repo, canary_slice))
        if config is None:
            return None

        tier = config.current_tier
        metrics = config.graduation_metrics

        if tier == AutonomyTier.OBSERVE:
            if (
                metrics.observations >= _MIN_OBSERVATIONS
                and metrics.false_positives <= _MAX_FALSE_POSITIVES
                and metrics.human_confirmations >= _MIN_HUMAN_CONFIRMATIONS
            ):
                return AutonomyTier.SUGGEST
        elif tier == AutonomyTier.SUGGEST:
            if metrics.successful_ops >= _MIN_SUCCESSFUL_OPS_GOVERNED:
                rollback_rate = (
                    metrics.rollback_count / metrics.successful_ops
                    if metrics.successful_ops > 0
                    else 1.0
                )
                if rollback_rate <= _MAX_ROLLBACK_RATE:
                    return AutonomyTier.GOVERNED
        elif tier == AutonomyTier.GOVERNED:
            if (
                metrics.successful_ops >= _MIN_SUCCESSFUL_OPS_AUTONOMOUS
                and metrics.rollback_count <= _MAX_ROLLBACKS_AUTONOMOUS
            ):
                return AutonomyTier.AUTONOMOUS

        return None

    def promote(
        self, trigger_source: str, repo: str, canary_slice: str,
        new_tier: AutonomyTier,
    ) -> SignalAutonomyConfig:
        """Apply a promotion to a config. Returns the updated config."""
        key = (trigger_source, repo, canary_slice)
        config = self._configs[key]
        updated = replace(config, current_tier=new_tier)
        self._configs[key] = updated
        logger.info(
            "Trust graduation: %s %s -> %s",
            key, config.current_tier.value, new_tier.value,
        )
        return updated

    def demote(
        self, trigger_source: str, repo: str, canary_slice: str,
        reason: str,
    ) -> AutonomyTier:
        """Demote a config based on a trigger reason.

        Returns the new tier.
        """
        key = (trigger_source, repo, canary_slice)
        config = self._configs[key]
        target_tier = _DEMOTION_MAP.get(reason, AutonomyTier.OBSERVE)

        # Never promote via demotion — only go down
        current_idx = TIER_ORDER.index(config.current_tier)
        target_idx = TIER_ORDER.index(target_tier)
        if target_idx >= current_idx:
            target_tier = TIER_ORDER[max(0, current_idx - 1)]

        # Reset metrics on demotion
        updated = replace(
            config,
            current_tier=target_tier,
            graduation_metrics=GraduationMetrics(),
        )
        self._configs[key] = updated
        logger.info(
            "Trust demotion: %s %s -> %s (reason=%s)",
            key, config.current_tier.value, target_tier.value, reason,
        )
        return target_tier

    def break_glass_reset(self) -> None:
        """Demote ALL configs to OBSERVE (break-glass emergency)."""
        for key, config in self._configs.items():
            self._configs[key] = replace(
                config,
                current_tier=AutonomyTier.OBSERVE,
                graduation_metrics=GraduationMetrics(),
            )
        logger.warning("Break-glass reset: all autonomy tiers demoted to OBSERVE")
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/autonomy/test_graduator.py -v`
Expected: 11 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/graduator.py tests/governance/autonomy/test_graduator.py
git commit -m "feat(autonomy): add TrustGraduator with promotion/demotion/break-glass"
```

---

## Task 4: AutonomyState — JSON Persistence

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/state.py`
- Create: `tests/governance/autonomy/test_state.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/autonomy/test_state.py"""
import json
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier,
    GraduationMetrics,
    SignalAutonomyConfig,
)


def _make_config(
    tier: AutonomyTier = AutonomyTier.GOVERNED,
    source: str = "intent:test_failure",
    repo: str = "jarvis",
) -> SignalAutonomyConfig:
    return SignalAutonomyConfig(
        trigger_source=source,
        repo=repo,
        canary_slice="tests/",
        current_tier=tier,
        graduation_metrics=GraduationMetrics(successful_ops=10),
    )


class TestStateSaveLoad:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState

        state_file = tmp_path / "autonomy" / "state.json"
        state = AutonomyState(state_path=state_file)

        configs = (_make_config(), _make_config(AutonomyTier.OBSERVE, repo="prime"))
        state.save(configs)

        loaded = state.load()
        assert len(loaded) == 2
        assert loaded[0].current_tier == AutonomyTier.GOVERNED
        assert loaded[0].graduation_metrics.successful_ops == 10
        assert loaded[1].current_tier == AutonomyTier.OBSERVE

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState

        state = AutonomyState(state_path=tmp_path / "missing" / "state.json")
        loaded = state.load()
        assert loaded == ()

    def test_load_corrupted_file_returns_empty(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState

        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json {{{")
        state = AutonomyState(state_path=state_file)
        loaded = state.load()
        assert loaded == ()


class TestStateReset:
    def test_reset_deletes_file(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState

        state_file = tmp_path / "state.json"
        state = AutonomyState(state_path=state_file)
        state.save((_make_config(),))
        assert state_file.exists()

        state.reset()
        assert not state_file.exists()

    def test_reset_missing_file_is_noop(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState

        state = AutonomyState(state_path=tmp_path / "nonexistent.json")
        state.reset()  # Should not raise
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/autonomy/test_state.py -v`
Expected: FAIL — module does not exist

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/autonomy/state.py

Autonomy State Persistence — saves/loads tier data to JSON.

State persists to ~/.jarvis/autonomy/state.json by default.
Survives process restarts. Break-glass reset clears the file.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .tiers import (
    AutonomyTier,
    CognitiveLoad,
    GraduationMetrics,
    SignalAutonomyConfig,
    WorkContext,
)

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path.home() / ".jarvis" / "autonomy" / "state.json"


class AutonomyState:
    """Persists autonomy tier configurations to JSON."""

    def __init__(self, state_path: Path = _DEFAULT_STATE_PATH) -> None:
        self._path = state_path

    def save(self, configs: Tuple[SignalAutonomyConfig, ...]) -> None:
        """Save all configs to the state file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: List[Dict[str, Any]] = []
        for config in configs:
            data.append({
                "trigger_source": config.trigger_source,
                "repo": config.repo,
                "canary_slice": config.canary_slice,
                "current_tier": config.current_tier.value,
                "graduation_metrics": {
                    "observations": config.graduation_metrics.observations,
                    "false_positives": config.graduation_metrics.false_positives,
                    "successful_ops": config.graduation_metrics.successful_ops,
                    "rollback_count": config.graduation_metrics.rollback_count,
                    "postmortem_streak": config.graduation_metrics.postmortem_streak,
                    "human_confirmations": config.graduation_metrics.human_confirmations,
                },
                "defer_during_cognitive_load": config.defer_during_cognitive_load.name,
                "defer_during_work_context": [
                    wc.value for wc in config.defer_during_work_context
                ],
                "require_user_active": config.require_user_active,
            })
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Autonomy state saved: %d configs to %s", len(data), self._path)

    def load(self) -> Tuple[SignalAutonomyConfig, ...]:
        """Load configs from the state file. Returns () if missing/corrupt."""
        if not self._path.exists():
            return ()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Autonomy state corrupted, returning empty: %s", exc)
            return ()

        configs: List[SignalAutonomyConfig] = []
        for entry in raw:
            try:
                metrics = GraduationMetrics(**entry["graduation_metrics"])
                config = SignalAutonomyConfig(
                    trigger_source=entry["trigger_source"],
                    repo=entry["repo"],
                    canary_slice=entry["canary_slice"],
                    current_tier=AutonomyTier(entry["current_tier"]),
                    graduation_metrics=metrics,
                    defer_during_cognitive_load=CognitiveLoad[
                        entry.get("defer_during_cognitive_load", "HIGH")
                    ],
                    defer_during_work_context=tuple(
                        WorkContext(v)
                        for v in entry.get("defer_during_work_context", ["meetings"])
                    ),
                    require_user_active=entry.get("require_user_active", False),
                )
                configs.append(config)
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed autonomy config: %s", exc)

        return tuple(configs)

    def reset(self) -> None:
        """Delete the state file (break-glass reset)."""
        if self._path.exists():
            self._path.unlink()
            logger.info("Autonomy state reset: %s deleted", self._path)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/autonomy/test_state.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/state.py tests/governance/autonomy/test_state.py
git commit -m "feat(autonomy): add AutonomyState JSON persistence"
```

---

## Task 5: Package Exports + Governance Wiring

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/__init__.py`
- Create: `tests/governance/autonomy/test_exports.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: Write the failing test**

```python
"""tests/governance/autonomy/test_exports.py"""


def test_autonomy_public_api():
    from backend.core.ouroboros.governance.autonomy import (
        AutonomyTier,
        TIER_ORDER,
        CognitiveLoad,
        WorkContext,
        CAISnapshot,
        UAESnapshot,
        SAISnapshot,
        GraduationMetrics,
        SignalAutonomyConfig,
        AutonomyGate,
        TrustGraduator,
        AutonomyState,
    )
    assert AutonomyTier is not None
    assert AutonomyGate is not None
    assert TrustGraduator is not None
    assert AutonomyState is not None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_exports.py -v`
Expected: FAIL — `__init__.py` does not re-export

**Step 3: Write package init and wire governance**

`backend/core/ouroboros/governance/autonomy/__init__.py`:

```python
"""Public API for the selective autonomy layer."""
from .tiers import (
    AutonomyTier,
    TIER_ORDER,
    CognitiveLoad,
    WorkContext,
    CAISnapshot,
    UAESnapshot,
    SAISnapshot,
    GraduationMetrics,
    SignalAutonomyConfig,
)
from .gate import AutonomyGate
from .graduator import TrustGraduator
from .state import AutonomyState

__all__ = [
    "AutonomyTier",
    "TIER_ORDER",
    "CognitiveLoad",
    "WorkContext",
    "CAISnapshot",
    "UAESnapshot",
    "SAISnapshot",
    "GraduationMetrics",
    "SignalAutonomyConfig",
    "AutonomyGate",
    "TrustGraduator",
    "AutonomyState",
]
```

Add to the bottom of `backend/core/ouroboros/governance/__init__.py` (after the multi-repo block):

```python
# --- Selective Autonomy (Layer 4) ---
from backend.core.ouroboros.governance.autonomy import (
    AutonomyTier,
    TIER_ORDER,
    CognitiveLoad,
    WorkContext,
    CAISnapshot,
    UAESnapshot,
    SAISnapshot,
    GraduationMetrics,
    SignalAutonomyConfig,
    AutonomyGate,
    TrustGraduator,
    AutonomyState,
)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_exports.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/__init__.py backend/core/ouroboros/governance/__init__.py tests/governance/autonomy/test_exports.py
git commit -m "feat(autonomy): export public API and wire into governance package"
```

---

## Task 6: E2E Integration Tests

**Files:**
- Create: `tests/governance/autonomy/test_e2e_autonomy.py`

**Step 1: Write the integration tests**

```python
"""tests/governance/autonomy/test_e2e_autonomy.py

End-to-end: Full trust graduation lifecycle and demotion with gate checks.
"""
import pytest
from dataclasses import replace
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier,
    CAISnapshot,
    CognitiveLoad,
    GraduationMetrics,
    SAISnapshot,
    SignalAutonomyConfig,
    UAESnapshot,
    WorkContext,
)
from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
from backend.core.ouroboros.governance.autonomy.state import AutonomyState


def _cai(
    load: CognitiveLoad = CognitiveLoad.LOW,
    ctx: WorkContext = WorkContext.CODING,
    safety: str = "SAFE",
) -> CAISnapshot:
    return CAISnapshot(cognitive_load=load, work_context=ctx, safety_level=safety)


def _uae(confidence: float = 0.85) -> UAESnapshot:
    return UAESnapshot(confidence=confidence)


def _sai(
    ram: float = 45.0, locked: bool = False, anomaly: bool = False,
) -> SAISnapshot:
    return SAISnapshot(ram_percent=ram, system_locked=locked, anomaly_detected=anomaly)


class TestE2EGraduationLifecycle:
    @pytest.mark.asyncio
    async def test_full_graduation_observe_to_autonomous(self, tmp_path):
        """Signal graduates through all 4 tiers with gate checks at each level."""
        gate = AutonomyGate()
        grad = TrustGraduator()
        state = AutonomyState(state_path=tmp_path / "state.json")

        # Start at OBSERVE
        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure",
            repo="jarvis",
            canary_slice="tests/",
            current_tier=AutonomyTier.OBSERVE,
            graduation_metrics=GraduationMetrics(),
        )
        grad.register(config)

        # Gate should block OBSERVE
        proceed, reason = await gate.should_proceed(config, _cai(), _uae(), _sai())
        assert proceed is False
        assert reason == "tier:observe_only"

        # Accumulate metrics for OBSERVE -> SUGGEST
        promoted_metrics = GraduationMetrics(
            observations=20, false_positives=0, human_confirmations=5,
        )
        grad.register(replace(config, graduation_metrics=promoted_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.SUGGEST
        suggest_config = grad.promote(
            "intent:test_failure", "jarvis", "tests/", new_tier,
        )

        # Gate should allow SUGGEST
        proceed, reason = await gate.should_proceed(
            suggest_config, _cai(), _uae(), _sai()
        )
        assert proceed is True

        # Accumulate metrics for SUGGEST -> GOVERNED
        governed_metrics = GraduationMetrics(successful_ops=30, rollback_count=1)
        grad.register(replace(suggest_config, graduation_metrics=governed_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.GOVERNED
        governed_config = grad.promote(
            "intent:test_failure", "jarvis", "tests/", new_tier,
        )

        # Accumulate metrics for GOVERNED -> AUTONOMOUS
        auto_metrics = GraduationMetrics(successful_ops=50, rollback_count=0)
        grad.register(replace(governed_config, graduation_metrics=auto_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.AUTONOMOUS
        auto_config = grad.promote(
            "intent:test_failure", "jarvis", "tests/", new_tier,
        )
        assert auto_config.current_tier == AutonomyTier.AUTONOMOUS

        # Save and reload state
        state.save(grad.all_configs())
        loaded = state.load()
        assert len(loaded) == 1
        assert loaded[0].current_tier == AutonomyTier.AUTONOMOUS


class TestE2EDemotionAndBreakGlass:
    @pytest.mark.asyncio
    async def test_rollback_demotes_then_break_glass_resets(self, tmp_path):
        """Rollback demotes, break-glass resets all to OBSERVE."""
        gate = AutonomyGate()
        grad = TrustGraduator()
        state = AutonomyState(state_path=tmp_path / "state.json")

        # Register two configs at different tiers
        grad.register(SignalAutonomyConfig(
            trigger_source="intent:test_failure",
            repo="jarvis",
            canary_slice="tests/",
            current_tier=AutonomyTier.AUTONOMOUS,
            graduation_metrics=GraduationMetrics(successful_ops=50),
        ))
        grad.register(SignalAutonomyConfig(
            trigger_source="intent:test_failure",
            repo="prime",
            canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED,
            graduation_metrics=GraduationMetrics(successful_ops=30),
        ))

        # Rollback demotes AUTONOMOUS -> GOVERNED
        new_tier = grad.demote(
            "intent:test_failure", "jarvis", "tests/", "rollback",
        )
        assert new_tier == AutonomyTier.GOVERNED

        # Break-glass resets ALL to OBSERVE
        grad.break_glass_reset()
        c1 = grad.get_config("intent:test_failure", "jarvis", "tests/")
        c2 = grad.get_config("intent:test_failure", "prime", "tests/")
        assert c1.current_tier == AutonomyTier.OBSERVE
        assert c2.current_tier == AutonomyTier.OBSERVE

        # Gate blocks everything at OBSERVE
        proceed, reason = await gate.should_proceed(c1, _cai(), _uae(), _sai())
        assert proceed is False

        # Save reset state
        state.save(grad.all_configs())
        loaded = state.load()
        assert all(c.current_tier == AutonomyTier.OBSERVE for c in loaded)

        # State reset clears file
        state.reset()
        assert not (tmp_path / "state.json").exists()
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/governance/autonomy/test_e2e_autonomy.py -v`
Expected: 2 PASSED

**Step 3: Commit**

```bash
git add tests/governance/autonomy/test_e2e_autonomy.py
git commit -m "test(autonomy): add E2E integration tests for graduation and demotion"
```

---

## Task 7: Full Test Suite Verification

**Step 1: Run the autonomy test suite**

Run: `python3 -m pytest tests/governance/autonomy/ -v`
Expected: All tests pass (~38 tests)

**Step 2: Run the full governance regression suite**

Run: `python3 -m pytest tests/governance/ -v`
Expected: All tests pass (170+ tests, no regressions)

---

## Summary

| Task | Module | Tests | Purpose |
|------|--------|-------|---------|
| 1 | `tiers.py` | 10 | AutonomyTier, snapshots, SignalAutonomyConfig |
| 2 | `gate.py` | 10 | Multi-system autonomy gate |
| 3 | `graduator.py` | 11 | Tier promotion/demotion/break-glass |
| 4 | `state.py` | 5 | JSON persistence |
| 5 | `__init__.py` | 1 | Package exports |
| 6 | E2E tests | 2 | Full lifecycle + demotion |
| 7 | Suite run | -- | Regression check |

**Total: ~39 tests across 7 tasks, 4 new source files.**
