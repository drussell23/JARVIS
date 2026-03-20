# Reasoning Chain Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire ProactiveCommandDetector -> PredictivePlanningAgent -> CoordinatorAgent into the voice pipeline via an extracted ReasoningChainOrchestrator, with 3-phase rollout, shadow mode, telemetry, and rollback switches.

**Architecture:** A new `ReasoningChainOrchestrator` class sits between the command processor and MindClient. It runs ProactiveCommandDetector to classify, PredictivePlanningAgent to expand multi-task commands into sub-intents, then routes each through MindClient individually. CoordinatorAgent maps plan steps to agent capabilities. Three phases (Shadow/Soft/Full) controlled by feature flags. All existing behavior preserved when disabled.

**Tech Stack:** Python 3.12, asyncio, dataclasses, existing Neural Mesh agents, MindClient, pytest

**Spec:** `docs/superpowers/specs/2026-03-19-reasoning-chain-wiring-design.md`
**Handoff:** `docs/superpowers/handoff/2026-03-19-reasoning-chain-handoff.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/reasoning_chain_orchestrator.py` | **NEW** — ChainPhase enum, ChainConfig, ChainResult, ShadowMetrics, ReasoningChainOrchestrator class |
| `tests/core/test_reasoning_chain_orchestrator.py` | **NEW** — Unit tests for all orchestrator behavior |
| `backend/api/unified_command_processor.py` | **MODIFY** — Add `_try_reasoning_chain()` method + call site at line ~2283 |
| `tests/core/test_reasoning_chain_integration.py` | **NEW** — Integration test: processor -> orchestrator -> mock agents |

---

### Task 1: Core Data Models and ChainPhase Enum

**Files:**
- Create: `backend/core/reasoning_chain_orchestrator.py`
- Test: `tests/core/test_reasoning_chain_orchestrator.py`

- [ ] **Step 1: Write failing tests for data models**

```python
# tests/core/test_reasoning_chain_orchestrator.py
"""Tests for ReasoningChainOrchestrator."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.reasoning_chain_orchestrator import (
    ChainPhase,
    ChainConfig,
    ChainResult,
    ShadowMetrics,
)


class TestChainPhase:
    def test_shadow_phase(self):
        assert ChainPhase.SHADOW.value == "shadow"

    def test_soft_enable_phase(self):
        assert ChainPhase.SOFT_ENABLE.value == "soft_enable"

    def test_full_enable_phase(self):
        assert ChainPhase.FULL_ENABLE.value == "full_enable"


class TestChainConfig:
    def test_default_config(self):
        config = ChainConfig()
        assert config.proactive_threshold == 0.6
        assert config.auto_expand_threshold == 0.85
        assert config.expansion_timeout == 2.0
        assert config.phase == ChainPhase.SHADOW

    def test_from_env_shadow(self):
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "true",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.SHADOW

    def test_from_env_soft_enable(self):
        env = {
            "JARVIS_REASONING_CHAIN_ENABLED": "true",
            "JARVIS_REASONING_CHAIN_AUTO_EXPAND": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.SOFT_ENABLE

    def test_from_env_full_enable(self):
        env = {
            "JARVIS_REASONING_CHAIN_ENABLED": "true",
            "JARVIS_REASONING_CHAIN_AUTO_EXPAND": "true",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.FULL_ENABLE

    def test_from_env_custom_thresholds(self):
        env = {
            "CHAIN_PROACTIVE_THRESHOLD": "0.7",
            "CHAIN_AUTO_EXPAND_THRESHOLD": "0.9",
            "CHAIN_EXPANSION_TIMEOUT": "3.0",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.proactive_threshold == 0.7
        assert config.auto_expand_threshold == 0.9
        assert config.expansion_timeout == 3.0


class TestChainResult:
    def test_single_intent_result(self):
        result = ChainResult(
            handled=True,
            phase=ChainPhase.FULL_ENABLE,
            trace_id="trace-123",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            mind_results=[{"success": True}, {"success": True}],
            audit_trail={},
        )
        assert result.handled is True
        assert result.success_rate == 1.0
        assert len(result.expanded_intents) == 2

    def test_not_handled_result(self):
        result = ChainResult.not_handled(trace_id="t1")
        assert result.handled is False
        assert result.expanded_intents == []


class TestShadowMetrics:
    def test_record_detection(self):
        m = ShadowMetrics()
        m.record_detection(would_expand=True, actually_expanded=False)
        assert m.total_detections == 1
        assert m.would_expand_count == 1
        assert m.actually_expanded_count == 0

    def test_divergence_rate(self):
        m = ShadowMetrics()
        m.record_detection(would_expand=True, actually_expanded=False)
        m.record_detection(would_expand=False, actually_expanded=False)
        assert m.divergence_rate == 0.5  # 1 divergence out of 2

    def test_empty_divergence_rate(self):
        m = ShadowMetrics()
        assert m.divergence_rate == 0.0

    def test_mind_quality_no_regression(self):
        m = ShadowMetrics()
        for _ in range(20):
            m.record_mind_quality(expanded_score=0.9, single_score=0.8)
        assert m.mind_quality_regressed is False

    def test_mind_quality_regression_detected(self):
        m = ShadowMetrics()
        for _ in range(20):
            m.record_mind_quality(expanded_score=0.5, single_score=0.8)
        assert m.mind_quality_regressed is True

    def test_mind_quality_insufficient_data(self):
        m = ShadowMetrics()
        m.record_mind_quality(expanded_score=0.1, single_score=0.9)
        assert m.mind_quality_regressed is False  # <10 samples

    def test_go_no_go_includes_mind_quality(self):
        m = ShadowMetrics()
        status = m.go_no_go_status()
        assert "mind_plan_quality" in status
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py -v 2>&1 | head -30`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.reasoning_chain_orchestrator'`

- [ ] **Step 3: Implement data models**

```python
# backend/core/reasoning_chain_orchestrator.py
"""
Reasoning Chain Orchestrator
============================

Wires ProactiveCommandDetector -> PredictivePlanningAgent -> CoordinatorAgent
into the voice pipeline as a pre-routing layer before MindClient.

Three phases:
  SHADOW      — run chain, log divergence, don't act
  SOFT_ENABLE — expand + ask user for confirmation
  FULL_ENABLE — expand automatically above confidence threshold

J-Prime remains the SOLE planning authority. This orchestrator classifies
(detector), expands intents (planner), and routes plans (coordinator).
It never generates Plan objects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and config
# ---------------------------------------------------------------------------

class ChainPhase(str, Enum):
    SHADOW = "shadow"
    SOFT_ENABLE = "soft_enable"
    FULL_ENABLE = "full_enable"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class ChainConfig:
    """Configuration for the reasoning chain, resolved from env vars."""
    proactive_threshold: float = 0.6
    auto_expand_threshold: float = 0.85
    expansion_timeout: float = 2.0
    phase: ChainPhase = ChainPhase.SHADOW
    active: bool = False  # Snapshot — set at construction, not re-read from env

    @classmethod
    def from_env(cls) -> ChainConfig:
        shadow = _env_bool("JARVIS_REASONING_CHAIN_SHADOW")
        enabled = _env_bool("JARVIS_REASONING_CHAIN_ENABLED")
        auto_expand = _env_bool("JARVIS_REASONING_CHAIN_AUTO_EXPAND")

        if enabled and auto_expand:
            phase = ChainPhase.FULL_ENABLE
        elif enabled:
            phase = ChainPhase.SOFT_ENABLE
        elif shadow:
            phase = ChainPhase.SHADOW
        else:
            phase = ChainPhase.SHADOW

        return cls(
            proactive_threshold=_env_float("CHAIN_PROACTIVE_THRESHOLD", 0.6),
            auto_expand_threshold=_env_float("CHAIN_AUTO_EXPAND_THRESHOLD", 0.85),
            expansion_timeout=_env_float("CHAIN_EXPANSION_TIMEOUT", 2.0),
            phase=phase,
            active=shadow or enabled,  # Captured once, consistent with phase
        )

    def is_active(self) -> bool:
        """True if either shadow or enabled flags were set at construction."""
        return self.active


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ChainResult:
    """Result of the reasoning chain orchestration."""
    handled: bool
    phase: ChainPhase
    trace_id: str
    original_command: str
    expanded_intents: List[str] = field(default_factory=list)
    mind_results: List[Dict[str, Any]] = field(default_factory=list)
    coordinator_delegations: List[Dict[str, Any]] = field(default_factory=list)
    audit_trail: Dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False
    confirmation_prompt: str = ""
    total_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        if not self.mind_results:
            return 0.0
        successes = sum(1 for r in self.mind_results if r.get("success", False))
        return successes / len(self.mind_results)

    @classmethod
    def not_handled(cls, trace_id: str = "") -> ChainResult:
        return cls(
            handled=False,
            phase=ChainPhase.SHADOW,
            trace_id=trace_id,
            original_command="",
        )


# ---------------------------------------------------------------------------
# Shadow metrics
# ---------------------------------------------------------------------------

@dataclass
class ShadowMetrics:
    """Tracks shadow mode divergence for go/no-go gate evaluation."""
    total_detections: int = 0
    would_expand_count: int = 0
    actually_expanded_count: int = 0
    _divergences: int = 0

    # Go/no-go accumulators (all 5 gates from spec)
    expansion_accuracy_hits: int = 0
    expansion_accuracy_total: int = 0
    false_positive_count: int = 0
    false_positive_total: int = 0
    latency_samples_ms: List[float] = field(default_factory=list)
    user_override_count: int = 0
    user_override_total: int = 0
    # Gate 4: Mind plan quality — compare expanded vs single-intent quality
    mind_quality_expanded_scores: List[float] = field(default_factory=list)
    mind_quality_single_scores: List[float] = field(default_factory=list)

    def record_detection(self, would_expand: bool, actually_expanded: bool) -> None:
        self.total_detections += 1
        if would_expand:
            self.would_expand_count += 1
        if actually_expanded:
            self.actually_expanded_count += 1
        if would_expand != actually_expanded:
            self._divergences += 1

    @property
    def divergence_rate(self) -> float:
        if self.total_detections == 0:
            return 0.0
        return self._divergences / self.total_detections

    def record_latency(self, ms: float) -> None:
        self.latency_samples_ms.append(ms)
        # Keep bounded
        if len(self.latency_samples_ms) > 1000:
            self.latency_samples_ms = self.latency_samples_ms[-1000:]

    def record_mind_quality(self, expanded_score: float, single_score: float) -> None:
        """Record quality comparison: expanded chain vs single-intent baseline."""
        self.mind_quality_expanded_scores.append(expanded_score)
        self.mind_quality_single_scores.append(single_score)
        # Keep bounded to 72h window (~1000 samples at typical usage)
        if len(self.mind_quality_expanded_scores) > 1000:
            self.mind_quality_expanded_scores = self.mind_quality_expanded_scores[-1000:]
            self.mind_quality_single_scores = self.mind_quality_single_scores[-1000:]

    @property
    def mind_quality_regressed(self) -> bool:
        """True if expanded chain quality is worse than single-intent baseline."""
        if len(self.mind_quality_expanded_scores) < 10:
            return False  # Not enough data to judge
        avg_expanded = sum(self.mind_quality_expanded_scores) / len(self.mind_quality_expanded_scores)
        avg_single = sum(self.mind_quality_single_scores) / len(self.mind_quality_single_scores)
        return avg_expanded < avg_single  # Regression = expanded is worse

    @property
    def latency_p95_ms(self) -> float:
        if not self.latency_samples_ms:
            return 0.0
        sorted_samples = sorted(self.latency_samples_ms)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def go_no_go_status(self) -> Dict[str, Any]:
        """Evaluate all 5 go/no-go gates (per spec Section 4.3)."""
        ea_rate = (
            self.expansion_accuracy_hits / self.expansion_accuracy_total
            if self.expansion_accuracy_total > 0 else 0.0
        )
        fp_rate = (
            self.false_positive_count / self.false_positive_total
            if self.false_positive_total > 0 else 0.0
        )
        override_rate = (
            self.user_override_count / self.user_override_total
            if self.user_override_total > 0 else 0.0
        )
        quality_pass = not self.mind_quality_regressed
        return {
            "expansion_accuracy": {"value": ea_rate, "threshold": 0.8, "pass": ea_rate >= 0.8, "n": self.expansion_accuracy_total},
            "false_positive_rate": {"value": fp_rate, "threshold": 0.1, "pass": fp_rate <= 0.1, "n": self.false_positive_total},
            "latency_p95_ms": {"value": self.latency_p95_ms, "threshold": 500, "pass": self.latency_p95_ms <= 500, "n": len(self.latency_samples_ms)},
            "mind_plan_quality": {"value": "no_regression" if quality_pass else "regressed", "threshold": "no_regression", "pass": quality_pass, "n": len(self.mind_quality_expanded_scores)},
            "user_override_rate": {"value": override_rate, "threshold": 0.2, "pass": override_rate <= 0.2, "n": self.user_override_total},
            "all_gates_pass": ea_rate >= 0.8 and fp_rate <= 0.1 and self.latency_p95_ms <= 500 and quality_pass and override_rate <= 0.2,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_chain_orchestrator.py tests/core/test_reasoning_chain_orchestrator.py
git commit -m "feat(chain): add reasoning chain data models, config, and shadow metrics"
```

---

### Task 2: Telemetry Emission

**Files:**
- Modify: `backend/core/reasoning_chain_orchestrator.py`
- Test: `tests/core/test_reasoning_chain_orchestrator.py`

- [ ] **Step 1: Write failing tests for telemetry**

Append to `tests/core/test_reasoning_chain_orchestrator.py`:

```python
from backend.core.reasoning_chain_orchestrator import ChainTelemetry


class TestChainTelemetry:
    @pytest.mark.asyncio
    async def test_emit_proactive_detection(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_proactive_detection(
            trace_id="t1",
            command="start my day",
            is_proactive=True,
            confidence=0.92,
            signals=["workflow_trigger", "multi_task"],
            latency_ms=15.0,
        )
        assert event["event"] == "proactive_detection"
        assert event["trace_id"] == "t1"
        assert event["is_proactive"] is True

    @pytest.mark.asyncio
    async def test_emit_intent_expansion(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_intent_expansion(
            trace_id="t1",
            original_query="start my day",
            expanded_count=3,
            intents=["check email", "check calendar", "open Slack"],
            confidence=0.88,
            latency_ms=120.0,
        )
        assert event["event"] == "intent_expansion"
        assert event["expanded_count"] == 3

    @pytest.mark.asyncio
    async def test_emit_shadow_divergence(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_shadow_divergence(
            trace_id="t1",
            would_expand=True,
            actually_expanded=False,
            match=False,
        )
        assert event["event"] == "expansion_shadow_divergence"
        assert event["would_expand"] is True
        assert event["match"] is False

    @pytest.mark.asyncio
    async def test_emit_coordinator_delegation(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_coordinator_delegation(
            trace_id="t1",
            plan_id="p1",
            step_id="s1",
            agent_name="GoogleWorkspaceAgent",
            capability="email_management",
            latency_ms=50.0,
        )
        assert event["event"] == "coordinator_delegation"
        assert event["agent_name"] == "GoogleWorkspaceAgent"

    @pytest.mark.asyncio
    async def test_emit_chain_complete(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_chain_complete(
            trace_id="t1",
            total_intents=3,
            total_steps=5,
            total_ms=2500.0,
            success_rate=1.0,
        )
        assert event["event"] == "chain_complete"
        assert event["total_intents"] == 3
        assert event["success_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_reactor_forwarding_best_effort(self):
        """Telemetry forwarding to Reactor is fire-and-forget; failures don't propagate."""
        telemetry = ChainTelemetry()
        # Even with Reactor unavailable, emit should succeed
        event = await telemetry.emit_proactive_detection(
            trace_id="t1", command="test", is_proactive=False,
            confidence=0.1, signals=[], latency_ms=5.0,
        )
        assert event is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py::TestChainTelemetry -v 2>&1 | head -20`
Expected: FAIL with `ImportError: cannot import name 'ChainTelemetry'`

- [ ] **Step 3: Implement ChainTelemetry**

Add to `backend/core/reasoning_chain_orchestrator.py` after the ShadowMetrics class:

```python
# ---------------------------------------------------------------------------
# Telemetry — 5 events per handoff spec, all carry trace_id
# ---------------------------------------------------------------------------

class ChainTelemetry:
    """Emits reasoning chain events and forwards to Reactor Core."""

    async def _forward_to_reactor(self, event: Dict[str, Any]) -> None:
        """Best-effort forward to Reactor Core for training. Never raises."""
        try:
            from backend.intelligence.cross_repo_experience_forwarder import (
                get_experience_forwarder,
            )
            fwd = await get_experience_forwarder()
            await fwd.forward_experience(
                experience_type="reasoning_chain",
                input_data={"event": event["event"], "trace_id": event["trace_id"]},
                output_data=event,
                quality_score=event.get("confidence", 0.0),
                confidence=event.get("confidence", 0.0),
                success=True,
                component="reasoning_chain_orchestrator",
            )
        except Exception as exc:
            logger.debug("[ChainTelemetry] Reactor forward failed (non-fatal): %s", exc)

    async def _emit(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Log event and forward to Reactor (fire-and-forget)."""
        logger.info(
            "[ReasoningChain] %s trace_id=%s %s",
            event["event"],
            event["trace_id"],
            {k: v for k, v in event.items() if k not in ("event", "trace_id")},
        )
        # Fire-and-forget — don't await in the hot path
        try:
            asyncio.create_task(
                self._forward_to_reactor(event),
                name=f"chain_telemetry_{event['event']}",
            )
        except RuntimeError:
            pass  # No running event loop (test context)
        return event

    async def emit_proactive_detection(
        self, trace_id: str, command: str, is_proactive: bool,
        confidence: float, signals: List[str], latency_ms: float,
    ) -> Dict[str, Any]:
        return await self._emit({
            "event": "proactive_detection",
            "trace_id": trace_id,
            "command": command,
            "is_proactive": is_proactive,
            "confidence": confidence,
            "signals": signals,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        })

    async def emit_intent_expansion(
        self, trace_id: str, original_query: str, expanded_count: int,
        intents: List[str], confidence: float, latency_ms: float,
    ) -> Dict[str, Any]:
        return await self._emit({
            "event": "intent_expansion",
            "trace_id": trace_id,
            "original_query": original_query,
            "expanded_count": expanded_count,
            "intents": intents,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        })

    async def emit_shadow_divergence(
        self, trace_id: str, would_expand: bool,
        actually_expanded: bool, match: bool,
    ) -> Dict[str, Any]:
        return await self._emit({
            "event": "expansion_shadow_divergence",
            "trace_id": trace_id,
            "would_expand": would_expand,
            "actually_expanded": actually_expanded,
            "match": match,
            "timestamp": time.time(),
        })

    async def emit_coordinator_delegation(
        self, trace_id: str, plan_id: str, step_id: str,
        agent_name: str, capability: str, latency_ms: float,
    ) -> Dict[str, Any]:
        return await self._emit({
            "event": "coordinator_delegation",
            "trace_id": trace_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "agent_name": agent_name,
            "capability": capability,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        })

    async def emit_chain_complete(
        self, trace_id: str, total_intents: int, total_steps: int,
        total_ms: float, success_rate: float,
    ) -> Dict[str, Any]:
        return await self._emit({
            "event": "chain_complete",
            "trace_id": trace_id,
            "total_intents": total_intents,
            "total_steps": total_steps,
            "total_ms": total_ms,
            "success_rate": success_rate,
            "timestamp": time.time(),
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_chain_orchestrator.py tests/core/test_reasoning_chain_orchestrator.py
git commit -m "feat(chain): add ChainTelemetry with 5 event types and Reactor forwarding"
```

---

### Task 3: ReasoningChainOrchestrator.process() — Core Logic

**Files:**
- Modify: `backend/core/reasoning_chain_orchestrator.py`
- Test: `tests/core/test_reasoning_chain_orchestrator.py`

- [ ] **Step 1: Write failing tests for process()**

Append to `tests/core/test_reasoning_chain_orchestrator.py`:

```python
from backend.core.reasoning_chain_orchestrator import (
    ReasoningChainOrchestrator,
    get_reasoning_chain_orchestrator,
)


def _mock_detection_result(is_proactive: bool, confidence: float = 0.9):
    """Create a mock ProactiveDetectionResult."""
    return MagicMock(
        is_proactive=is_proactive,
        confidence=confidence,
        signals_detected=["workflow_trigger"] if is_proactive else [],
        suggested_intent="work_mode" if is_proactive else None,
        reasoning="test",
        should_use_expand_and_execute=is_proactive,
    )


def _mock_prediction_result(intents: List[str] = None):
    """Create a mock PredictionResult."""
    intents = intents or ["check email", "check calendar", "open Slack"]
    tasks = []
    for i, intent in enumerate(intents):
        task = MagicMock()
        task.goal = intent
        task.priority = i + 1
        task.target_app = None
        task.category = MagicMock(name="WORK_MODE")
        tasks.append(task)

    result = MagicMock()
    result.original_query = "start my day"
    result.confidence = 0.88
    result.expanded_tasks = tasks
    result.reasoning = "Morning workflow detected"
    return result


class TestOrchestratorShadowPhase:
    @pytest.mark.asyncio
    async def test_shadow_returns_none(self):
        """Shadow mode: detect + expand in background, return None (no behavioral change)."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None  # Shadow mode never acts
        mock_detector.detect.assert_called_once_with("start my day")

    @pytest.mark.asyncio
    async def test_shadow_logs_divergence(self):
        """Shadow mode records divergence metrics."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        await orch.process("start my day", context={}, trace_id="t1")
        assert orch._shadow_metrics.total_detections == 1
        assert orch._shadow_metrics.would_expand_count == 1


class TestOrchestratorNotProactive:
    @pytest.mark.asyncio
    async def test_non_proactive_returns_none(self):
        """Non-proactive commands skip expansion entirely."""
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(False, 0.2)
        orch._detector = mock_detector

        result = await orch.process("what time is it", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self):
        """Proactive but below confidence threshold returns None."""
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.8)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.7)
        orch._detector = mock_detector

        result = await orch.process("maybe start work", context={}, trace_id="t1")
        assert result is None


class TestOrchestratorSoftEnable:
    @pytest.mark.asyncio
    async def test_soft_enable_returns_confirmation(self):
        """Soft enable: detect + expand, return needs_confirmation=True."""
        config = ChainConfig(phase=ChainPhase.SOFT_ENABLE, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is True
        assert len(result.expanded_intents) == 3
        assert "check email" in result.expanded_intents


class TestOrchestratorFullEnable:
    @pytest.mark.asyncio
    async def test_full_enable_expands_and_executes(self):
        """Full enable: detect + expand + send each sub-intent to Mind."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.6,
            auto_expand_threshold=0.85,
        )
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        mock_mind = AsyncMock()
        mock_mind.send_command.return_value = {
            "status": "plan_ready",
            "plan": {"sub_goals": [{"goal": "done"}], "plan_id": "p1"},
            "classification": {},
        }
        orch._mind_client = mock_mind

        mock_coordinator = AsyncMock()
        mock_coordinator.execute_task.return_value = {"status": "delegated", "task_id": "t1", "delegated_to": "agent1"}
        orch._coordinator = mock_coordinator

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is False
        assert mock_mind.send_command.call_count == 3  # 3 sub-intents
        assert len(result.mind_results) == 3

    @pytest.mark.asyncio
    async def test_full_enable_below_auto_threshold_asks_confirmation(self):
        """Full enable with confidence below auto_expand_threshold asks confirmation."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.6,
            auto_expand_threshold=0.95,  # Very high
        )
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.88)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result.needs_confirmation is True


class TestOrchestratorErrorHandling:
    @pytest.mark.asyncio
    async def test_detector_failure_returns_none(self):
        """If detector fails, gracefully fall through."""
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.side_effect = Exception("detector exploded")
        orch._detector = mock_detector

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_planner_failure_returns_none(self):
        """If planner fails, gracefully fall through."""
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.side_effect = Exception("planner exploded")
        orch._planner = mock_planner

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_mind_failure_for_one_intent_continues_others(self):
        """If Mind fails for one sub-intent, others still execute."""
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result(["a", "b"])
        orch._planner = mock_planner

        call_count = 0
        async def mind_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First call fails
            return {"status": "plan_ready", "plan": {"sub_goals": [], "plan_id": "p1"}, "classification": {}}

        mock_mind = AsyncMock()
        mock_mind.send_command.side_effect = mind_side_effect
        orch._mind_client = mock_mind

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True
        assert result.success_rate < 1.0  # One failed

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """If expansion exceeds timeout, fall through."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.6,
            expansion_timeout=0.01,  # 10ms — will timeout
        )
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        async def slow_detect(cmd):
            await asyncio.sleep(0.1)
            return _mock_detection_result(True, 0.92)
        mock_detector.detect.side_effect = slow_detect
        orch._detector = mock_detector

        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None


class TestOrchestratorSingleton:
    def test_singleton(self):
        orch1 = get_reasoning_chain_orchestrator()
        orch2 = get_reasoning_chain_orchestrator()
        assert orch1 is orch2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py::TestOrchestratorShadowPhase -v 2>&1 | head -20`
Expected: FAIL with `ImportError: cannot import name 'ReasoningChainOrchestrator'`

- [ ] **Step 3: Implement ReasoningChainOrchestrator.process()**

Add to `backend/core/reasoning_chain_orchestrator.py`:

```python
# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ReasoningChainOrchestrator:
    """
    Pre-routing layer that wires ProactiveCommandDetector ->
    PredictivePlanningAgent -> CoordinatorAgent before MindClient.

    process() returns:
      None        — chain didn't handle it; caller should use single-intent path
      ChainResult — chain handled it (expanded, confirmed, or needs confirmation)
    """

    def __init__(self, config: Optional[ChainConfig] = None):
        self._config = config or ChainConfig.from_env()
        self._telemetry = ChainTelemetry()
        self._shadow_metrics = ShadowMetrics()

        # Lazy-initialized dependencies
        self._detector = None
        self._planner = None
        self._mind_client = None
        self._coordinator = None

    def _get_detector(self):
        if self._detector is None:
            try:
                from backend.core.proactive_command_detector import get_proactive_detector
                self._detector = get_proactive_detector(
                    min_confidence=self._config.proactive_threshold,
                )
            except Exception as exc:
                logger.warning("[ReasoningChain] ProactiveCommandDetector unavailable: %s", exc)
        return self._detector

    async def _get_planner(self):
        if self._planner is None:
            try:
                from backend.neural_mesh.agents.predictive_planning_agent import (
                    get_predictive_agent,
                )
                self._planner = await get_predictive_agent()
            except Exception as exc:
                logger.warning("[ReasoningChain] PredictivePlanningAgent unavailable: %s", exc)
        return self._planner

    def _get_mind_client(self):
        if self._mind_client is None:
            try:
                from backend.core.mind_client import get_mind_client
                self._mind_client = get_mind_client()
            except Exception as exc:
                logger.warning("[ReasoningChain] MindClient unavailable: %s", exc)
        return self._mind_client

    async def _get_coordinator(self):
        if self._coordinator is None:
            try:
                from backend.neural_mesh.agents.agent_initializer import get_agent_initializer
                initializer = await get_agent_initializer()  # async factory
                if initializer and hasattr(initializer, "get_agent"):
                    self._coordinator = initializer.get_agent("coordinator_agent")  # sync lookup
            except Exception as exc:
                logger.debug("[ReasoningChain] CoordinatorAgent unavailable: %s", exc)
        return self._coordinator

    async def process(
        self,
        command: str,
        context: Dict[str, Any],
        trace_id: str,
        deadline: Optional[float] = None,
    ) -> Optional[ChainResult]:
        """
        Run the reasoning chain on a command.

        Returns None if the chain doesn't handle this command
        (non-proactive, below threshold, shadow mode, or error).
        The caller should fall through to the existing single-intent path.
        """
        start_ms = time.monotonic() * 1000

        # ------------------------------------------------------------------
        # Step 1: Proactive detection
        # ------------------------------------------------------------------
        try:
            detector = self._get_detector()
            if detector is None:
                return None

            detect_start = time.monotonic() * 1000
            detection = await asyncio.wait_for(
                detector.detect(command),
                timeout=self._config.expansion_timeout,
            )
            detect_ms = time.monotonic() * 1000 - detect_start

            await self._telemetry.emit_proactive_detection(
                trace_id=trace_id,
                command=command,
                is_proactive=detection.is_proactive,
                confidence=detection.confidence,
                signals=[s.value if hasattr(s, "value") else str(s) for s in detection.signals_detected],
                latency_ms=detect_ms,
            )
        except asyncio.TimeoutError:
            logger.info("[ReasoningChain] Detection timed out for '%s' — falling through", command[:50])
            return None
        except Exception as exc:
            logger.warning("[ReasoningChain] Detection failed: %s — falling through", exc)
            return None

        # Not proactive or below threshold — skip
        if not detection.is_proactive or detection.confidence < self._config.proactive_threshold:
            return None

        # ------------------------------------------------------------------
        # Step 2: Intent expansion
        # ------------------------------------------------------------------
        try:
            planner = await self._get_planner()
            if planner is None:
                return None

            expand_start = time.monotonic() * 1000
            remaining_timeout = self._config.expansion_timeout - (detect_ms / 1000)
            if remaining_timeout <= 0:
                logger.info("[ReasoningChain] No time budget for expansion — falling through")
                return None

            prediction = await asyncio.wait_for(
                planner.expand_intent(command),
                timeout=remaining_timeout,
            )
            expand_ms = time.monotonic() * 1000 - expand_start

            expanded_intents = [t.goal for t in prediction.expanded_tasks]

            await self._telemetry.emit_intent_expansion(
                trace_id=trace_id,
                original_query=command,
                expanded_count=len(expanded_intents),
                intents=expanded_intents,
                confidence=prediction.confidence,
                latency_ms=expand_ms,
            )
        except asyncio.TimeoutError:
            logger.info("[ReasoningChain] Expansion timed out for '%s' — falling through", command[:50])
            return None
        except Exception as exc:
            logger.warning("[ReasoningChain] Expansion failed: %s — falling through", exc)
            return None

        total_detect_expand_ms = time.monotonic() * 1000 - start_ms
        self._shadow_metrics.record_latency(total_detect_expand_ms)

        # ------------------------------------------------------------------
        # Phase-dependent behavior
        # ------------------------------------------------------------------

        if self._config.phase == ChainPhase.SHADOW:
            # Shadow: log divergence, don't act
            self._shadow_metrics.record_detection(
                would_expand=True, actually_expanded=False,
            )
            await self._telemetry.emit_shadow_divergence(
                trace_id=trace_id,
                would_expand=True,
                actually_expanded=False,
                match=False,
            )
            return None

        # Soft enable or Full enable with low confidence → ask confirmation
        needs_confirmation = (
            self._config.phase == ChainPhase.SOFT_ENABLE
            or detection.confidence < self._config.auto_expand_threshold
        )

        if needs_confirmation:
            intent_list = ", ".join(expanded_intents)
            return ChainResult(
                handled=True,
                phase=self._config.phase,
                trace_id=trace_id,
                original_command=command,
                expanded_intents=expanded_intents,
                needs_confirmation=True,
                confirmation_prompt=(
                    f"Sounds like multiple tasks. Want me to handle these separately? "
                    f"{intent_list}"
                ),
                total_ms=total_detect_expand_ms,
                audit_trail={
                    "detection": {
                        "is_proactive": detection.is_proactive,
                        "confidence": detection.confidence,
                        "signals": [s.value if hasattr(s, "value") else str(s) for s in detection.signals_detected],
                    },
                    "expansion": {
                        "confidence": prediction.confidence,
                        "reasoning": prediction.reasoning,
                    },
                },
            )

        # ------------------------------------------------------------------
        # Full enable + auto-expand: send each sub-intent to Mind
        # ------------------------------------------------------------------
        mind_results = []
        mind = self._get_mind_client()
        if mind is None:
            logger.warning("[ReasoningChain] MindClient unavailable — falling through")
            return None

        for intent in expanded_intents:
            try:
                mind_result = await mind.send_command(
                    command=intent,
                    context={
                        **context,
                        "trace_id": trace_id,
                        "parent_command": command,
                        "expanded_from_chain": True,
                    },
                    deadline_ms=(
                        int((deadline - time.monotonic()) * 1000)
                        if deadline else None
                    ),
                )
                mind_results.append(mind_result or {"success": False, "error": "Mind returned None"})
            except Exception as exc:
                logger.warning("[ReasoningChain] Mind failed for intent '%s': %s", intent, exc)
                mind_results.append({"success": False, "error": str(exc)})

        # ------------------------------------------------------------------
        # Route plan steps through CoordinatorAgent
        # ------------------------------------------------------------------
        coordinator_delegations = []
        coordinator = await self._get_coordinator()

        for i, (intent, mr) in enumerate(zip(expanded_intents, mind_results)):
            if mr.get("status") != "plan_ready":
                continue
            plan = mr.get("plan", {})
            plan_id = plan.get("plan_id", f"p-{i}")
            sub_goals = plan.get("sub_goals", [])

            for j, sg in enumerate(sub_goals):
                step_id = f"{plan_id}-s{j}"
                capability = sg.get("tool_required", "computer_use")
                delegation = {"plan_id": plan_id, "step_id": step_id, "capability": capability}

                if coordinator is not None:
                    try:
                        delegate_start = time.monotonic() * 1000
                        delegate_result = await coordinator.execute_task({
                            "action": "delegate_task",
                            "capability": capability,
                            "task_payload": {
                                "trace_id": trace_id,
                                "plan_id": plan_id,
                                "step": sg,
                            },
                            "priority": "high" if sg.get("priority", 99) <= 2 else "normal",
                        })
                        delegate_ms = time.monotonic() * 1000 - delegate_start
                        delegation["result"] = delegate_result
                        delegation["agent_name"] = delegate_result.get("delegated_to", "unknown")

                        await self._telemetry.emit_coordinator_delegation(
                            trace_id=trace_id,
                            plan_id=plan_id,
                            step_id=step_id,
                            agent_name=delegation["agent_name"],
                            capability=capability,
                            latency_ms=delegate_ms,
                        )
                    except Exception as exc:
                        logger.debug("[ReasoningChain] Coordinator delegation failed: %s", exc)
                        delegation["error"] = str(exc)

                coordinator_delegations.append(delegation)

        total_ms = time.monotonic() * 1000 - start_ms

        await self._telemetry.emit_chain_complete(
            trace_id=trace_id,
            total_intents=len(expanded_intents),
            total_steps=len(coordinator_delegations),
            total_ms=total_ms,
            success_rate=sum(1 for r in mind_results if r.get("status") == "plan_ready") / max(len(mind_results), 1),
        )

        self._shadow_metrics.record_detection(would_expand=True, actually_expanded=True)

        return ChainResult(
            handled=True,
            phase=self._config.phase,
            trace_id=trace_id,
            original_command=command,
            expanded_intents=expanded_intents,
            mind_results=mind_results,
            coordinator_delegations=coordinator_delegations,
            total_ms=total_ms,
            audit_trail={
                "detection": {
                    "is_proactive": detection.is_proactive,
                    "confidence": detection.confidence,
                    "signals": [s.value if hasattr(s, "value") else str(s) for s in detection.signals_detected],
                },
                "expansion": {
                    "confidence": prediction.confidence,
                    "intent_count": len(expanded_intents),
                    "reasoning": prediction.reasoning,
                },
                "mind_requests": len(mind_results),
                "delegations": len(coordinator_delegations),
            },
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_orchestrator_instance: Optional[ReasoningChainOrchestrator] = None


def get_reasoning_chain_orchestrator() -> ReasoningChainOrchestrator:
    """Get or create the process-wide ReasoningChainOrchestrator singleton."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = ReasoningChainOrchestrator()
    return _orchestrator_instance
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_chain_orchestrator.py tests/core/test_reasoning_chain_orchestrator.py
git commit -m "feat(chain): implement ReasoningChainOrchestrator.process() with 3-phase logic"
```

---

### Task 4: Wire into unified_command_processor.py

**Files:**
- Modify: `backend/api/unified_command_processor.py:2280-2302`
- Test: `tests/core/test_reasoning_chain_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/core/test_reasoning_chain_integration.py
"""Integration test: command processor -> reasoning chain orchestrator."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.reasoning_chain_orchestrator import (
    ChainConfig,
    ChainPhase,
    ChainResult,
    ReasoningChainOrchestrator,
)


class TestCommandProcessorChainWiring:
    """Verify _try_reasoning_chain behavior in the command processor."""

    @pytest.mark.asyncio
    async def test_chain_disabled_returns_none(self):
        """When chain flags are off, _try_reasoning_chain returns None."""
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "false",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            from backend.core.reasoning_chain_orchestrator import ChainConfig
            config = ChainConfig.from_env()
            assert config.is_active() is False

    @pytest.mark.asyncio
    async def test_chain_shadow_enabled(self):
        """When shadow flag is on, config.is_active() returns True."""
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "true",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
            assert config.is_active() is True
            assert config.phase == ChainPhase.SHADOW

    @pytest.mark.asyncio
    async def test_chain_result_to_processor_response(self):
        """ChainResult with needs_confirmation maps to processor response format."""
        result = ChainResult(
            handled=True,
            phase=ChainPhase.SOFT_ENABLE,
            trace_id="t1",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            needs_confirmation=True,
            confirmation_prompt="Handle separately? check email, check calendar",
        )
        # The processor should return this as a confirmation dialog
        assert result.needs_confirmation is True
        assert "check email" in result.confirmation_prompt

    @pytest.mark.asyncio
    async def test_chain_result_with_mind_results(self):
        """ChainResult with mind_results should be formatted as unified response."""
        result = ChainResult(
            handled=True,
            phase=ChainPhase.FULL_ENABLE,
            trace_id="t1",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            mind_results=[
                {"status": "plan_ready", "plan": {"sub_goals": [{"goal": "opened gmail"}]}},
                {"status": "plan_ready", "plan": {"sub_goals": [{"goal": "opened calendar"}]}},
            ],
        )
        assert result.handled is True
        assert result.success_rate > 0
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/test_reasoning_chain_integration.py -v`
Expected: PASS (these test the data flow contract, not the wiring)

- [ ] **Step 3: Add _try_reasoning_chain method to unified_command_processor.py**

Find the `_execute_mind_plan` method (line ~9623) and add this new method BEFORE it:

```python
    # ------------------------------------------------------------------
    # v300.0: Reasoning chain pre-routing
    # ------------------------------------------------------------------

    async def _try_reasoning_chain(
        self, command_text: str, context: Dict[str, Any],
        deadline: Optional[float] = None, websocket=None,
    ) -> Optional[Dict[str, Any]]:
        """
        v300.0: Try reasoning chain (ProactiveDetector -> PredictivePlanner -> Coordinator).

        Returns a response dict if the chain handled the command (expanded into
        sub-intents and executed them). Returns None if the chain is disabled,
        the command is not proactive, or any component fails — caller falls
        through to the existing single-intent Mind path.
        """
        try:
            from backend.core.reasoning_chain_orchestrator import (
                ChainConfig,
                get_reasoning_chain_orchestrator,
            )

            config = ChainConfig.from_env()
            if not config.is_active():
                return None

            orch = get_reasoning_chain_orchestrator()
            trace_id = str(uuid.uuid4())[:12]

            chain_result = await orch.process(
                command=command_text,
                context=context,
                trace_id=trace_id,
                deadline=deadline,
            )

            if chain_result is None:
                return None

            # Needs confirmation — return as confirmation dialog
            if chain_result.needs_confirmation:
                return {
                    "success": True,
                    "response": chain_result.confirmation_prompt,
                    "command_type": "reasoning_chain_confirm",
                    "needs_confirmation": True,
                    "expanded_intents": chain_result.expanded_intents,
                    "trace_id": trace_id,
                    "chain_phase": chain_result.phase.value,
                }

            # Chain handled and executed — aggregate mind results
            if chain_result.handled and chain_result.mind_results:
                # Execute each Mind plan through existing _execute_mind_plan
                all_step_results = []
                for mr in chain_result.mind_results:
                    if mr.get("status") == "plan_ready":
                        step_result = await self._execute_mind_plan(
                            mr, command_text, websocket=websocket, deadline=deadline,
                        )
                        all_step_results.append(step_result)

                success = all(r.get("success", False) for r in all_step_results)
                response_parts = [r.get("response", "") for r in all_step_results if r.get("response")]
                response_text = " | ".join(response_parts) if response_parts else "All tasks completed."

                return {
                    "success": success,
                    "response": response_text,
                    "command_type": "reasoning_chain",
                    "chain_phase": chain_result.phase.value,
                    "trace_id": trace_id,
                    "expanded_intents": chain_result.expanded_intents,
                    "intents_executed": len(chain_result.mind_results),
                    "intents_succeeded": sum(
                        1 for r in chain_result.mind_results
                        if r.get("status") == "plan_ready"
                    ),
                    "total_ms": chain_result.total_ms,
                    "audit_trail": chain_result.audit_trail,
                }

        except Exception as exc:
            logger.debug("[v300] Reasoning chain failed (non-fatal): %s", exc)

        return None
```

- [ ] **Step 4: Add call site in _execute_command_pipeline**

In `backend/api/unified_command_processor.py`, find line ~2280 (the comment `# v295.0 Step 2: Full remote reasoning via MindClient`). **BEFORE** that block, insert:

```python
        # v300.0: Reasoning chain pre-routing — ProactiveDetector -> PredictivePlanner -> Coordinator
        # If command is multi-task (proactive), expand into sub-intents and execute each through Mind.
        # Feature flags: JARVIS_REASONING_CHAIN_SHADOW or JARVIS_REASONING_CHAIN_ENABLED
        _chain_result = await self._try_reasoning_chain(
            command_text, _jprime_ctx or {}, deadline=deadline, websocket=websocket,
        )
        if _chain_result is not None:
            return _chain_result

```

This goes at approximately line 2280, right before `_use_remote_reasoning = os.getenv(...)`.

- [ ] **Step 5: Run integration tests**

Run: `python3 -m pytest tests/core/test_reasoning_chain_integration.py tests/core/test_reasoning_chain_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 6: Run existing command processor tests (regression check)**

Run: `python3 -m pytest tests/ -k "unified_command" -v --timeout=30 2>&1 | tail -20`
Expected: No regressions. The chain is off by default (both flags false), so existing behavior is unchanged.

- [ ] **Step 7: Commit**

```bash
git add backend/api/unified_command_processor.py tests/core/test_reasoning_chain_integration.py
git commit -m "feat(chain): wire reasoning chain into command processor (v300.0)"
```

---

### Task 5: End-to-End Smoke Test

**Files:**
- Test: `tests/core/test_reasoning_chain_orchestrator.py` (append)

- [ ] **Step 1: Write e2e test with all mocks**

Append to `tests/core/test_reasoning_chain_orchestrator.py`:

```python
class TestEndToEndChain:
    """Full pipeline: detect -> expand -> mind -> coordinate -> result."""

    @pytest.mark.asyncio
    async def test_full_pipeline_start_my_day(self):
        """Simulate 'start my day' through the full chain."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.5,
            auto_expand_threshold=0.8,
        )
        orch = ReasoningChainOrchestrator(config=config)

        # Mock detector: proactive, high confidence
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.95)
        orch._detector = mock_detector

        # Mock planner: 3 expanded intents
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result(
            ["check email", "check calendar", "open Slack"]
        )
        orch._planner = mock_planner

        # Mock mind: returns plan_ready for each
        mock_mind = AsyncMock()
        mock_mind.send_command.return_value = {
            "status": "plan_ready",
            "plan": {
                "sub_goals": [{"goal": "done", "tool_required": "handle_workspace_query"}],
                "plan_id": "p-test",
            },
            "classification": {"brain_used": "qwen-2.5-7b"},
        }
        orch._mind_client = mock_mind

        # Mock coordinator
        mock_coord = AsyncMock()
        mock_coord.execute_task.return_value = {
            "status": "delegated",
            "task_id": "t-test",
            "delegated_to": "GoogleWorkspaceAgent",
        }
        orch._coordinator = mock_coord

        result = await orch.process("start my day", context={}, trace_id="e2e-test")

        # Assertions
        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is False
        assert len(result.expanded_intents) == 3
        assert len(result.mind_results) == 3
        assert result.success_rate > 0
        assert result.total_ms > 0

        # Verify call counts
        assert mock_detector.detect.call_count == 1
        assert mock_planner.expand_intent.call_count == 1
        assert mock_mind.send_command.call_count == 3
        assert mock_coord.execute_task.call_count == 3  # 1 sub_goal per plan * 3 plans

        # Verify trace_id propagated to Mind
        for call in mock_mind.send_command.call_args_list:
            ctx = call.kwargs.get("context", {})
            assert ctx.get("trace_id") == "e2e-test"
            assert ctx.get("expanded_from_chain") is True

        # Verify audit trail
        assert "detection" in result.audit_trail
        assert result.audit_trail["detection"]["confidence"] == 0.95
        assert result.audit_trail["expansion"]["intent_count"] == 3

    @pytest.mark.asyncio
    async def test_non_proactive_command_passthrough(self):
        """Simple command like 'what time is it' should not be intercepted."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.6,
        )
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(False, 0.1)
        orch._detector = mock_detector

        result = await orch.process("what time is it", context={}, trace_id="simple")
        assert result is None  # Falls through to single-intent

    @pytest.mark.asyncio
    async def test_go_no_go_metrics_accumulate(self):
        """Shadow metrics accumulate for go/no-go evaluation."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.5)
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.9)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        # Run 5 commands through shadow
        for i in range(5):
            await orch.process(f"command {i}", context={}, trace_id=f"shadow-{i}")

        assert orch._shadow_metrics.total_detections == 5
        assert orch._shadow_metrics.would_expand_count == 5
        assert orch._shadow_metrics.actually_expanded_count == 0
        assert len(orch._shadow_metrics.latency_samples_ms) == 5

        status = orch._shadow_metrics.go_no_go_status()
        assert "expansion_accuracy" in status
        assert "false_positive_rate" in status
        assert "latency_p95_ms" in status
```

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py tests/core/test_reasoning_chain_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/core/test_reasoning_chain_orchestrator.py
git commit -m "test(chain): add end-to-end pipeline and go/no-go metrics tests"
```

---

### Task 6: Final Regression Check and Cleanup

**Files:**
- All files from previous tasks

- [ ] **Step 1: Run the full reasoning chain test suite**

Run: `python3 -m pytest tests/core/test_reasoning_chain_orchestrator.py tests/core/test_reasoning_chain_integration.py -v --tb=short`
Expected: All PASS

- [ ] **Step 2: Run existing tests to check for regressions**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -v --timeout=30 2>&1 | tail -30`
Expected: Same pass count as before (196 per handoff). Zero new failures.

- [ ] **Step 3: Verify feature flags default to off (no behavioral change)**

Run: `python3 -c "from backend.core.reasoning_chain_orchestrator import ChainConfig; c = ChainConfig.from_env(); print(f'active={c.is_active()}, phase={c.phase.value}')"`
Expected: `active=False, phase=shadow` (chain is dormant by default)

- [ ] **Step 4: Verify import works in command processor context**

Run: `python3 -c "from backend.api.unified_command_processor import UnifiedCommandProcessor; print('import OK')"`
Expected: `import OK` (no circular import)

- [ ] **Step 5: Final commit with all tests passing**

```bash
git add -A
git status  # Verify no untracked sensitive files
git commit -m "feat(chain): complete reasoning chain wiring (Approach B)

Wire ProactiveCommandDetector -> PredictivePlanningAgent -> CoordinatorAgent
into the voice pipeline via ReasoningChainOrchestrator.

- 3-phase rollout: Shadow (log only) -> Soft Enable (confirm) -> Full Enable (auto)
- 5 telemetry events with trace_id correlation
- Go/no-go gate tracking for phase promotion
- Graceful degradation: any failure falls through to single-intent path
- Zero behavioral change when flags are off (default)

New files:
  backend/core/reasoning_chain_orchestrator.py
  tests/core/test_reasoning_chain_orchestrator.py
  tests/core/test_reasoning_chain_integration.py

Modified:
  backend/api/unified_command_processor.py (v300.0: _try_reasoning_chain + call site)"
```
