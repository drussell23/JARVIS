"""
Reasoning Chain Orchestrator
============================

Wires ProactiveCommandDetector -> PredictivePlanningAgent -> CoordinatorAgent
into the voice pipeline as a pre-routing layer before MindClient.

Three phases:
  SHADOW      -- run chain, log divergence, don't act
  SOFT_ENABLE -- expand + ask user for confirmation
  FULL_ENABLE -- expand automatically above confidence threshold

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


class ChainPhase(str, Enum):
    """Deployment phase for the reasoning chain."""
    SHADOW = "shadow"
    SOFT_ENABLE = "soft_enable"
    FULL_ENABLE = "full_enable"


def _env_float(key: str, default: float) -> float:
    """Read a float from an environment variable, falling back to *default*."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean from an environment variable."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class ChainConfig:
    """Configuration for the reasoning chain, resolved from env vars."""

    proactive_threshold: float = 0.6
    auto_expand_threshold: float = 0.85
    expansion_timeout: float = 2.0
    phase: ChainPhase = ChainPhase.SHADOW
    active: bool = False

    @classmethod
    def from_env(cls) -> ChainConfig:
        """Build a config from the current environment variables.

        Phase resolution order:
          1. JARVIS_REASONING_CHAIN_ENABLED + _AUTO_EXPAND => FULL_ENABLE
          2. JARVIS_REASONING_CHAIN_ENABLED alone           => SOFT_ENABLE
          3. JARVIS_REASONING_CHAIN_SHADOW                  => SHADOW
          4. Neither flag set                               => SHADOW (inactive)
        """
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
            active=shadow or enabled,
        )

    def is_active(self) -> bool:
        """True if either shadow or enabled flags were set at construction."""
        return self.active


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
        """Fraction of mind_results that report success."""
        if not self.mind_results:
            return 0.0
        successes = sum(1 for r in self.mind_results if r.get("success", False))
        return successes / len(self.mind_results)

    @classmethod
    def not_handled(cls, trace_id: str = "") -> ChainResult:
        """Factory for a result indicating the chain did not handle the command."""
        return cls(
            handled=False,
            phase=ChainPhase.SHADOW,
            trace_id=trace_id,
            original_command="",
        )


@dataclass
class ShadowMetrics:
    """Tracks shadow mode divergence for go/no-go gate evaluation.

    Records detection outcomes, latency, mind-plan quality, and
    user overrides so the system can objectively decide when to
    promote from SHADOW -> SOFT_ENABLE -> FULL_ENABLE.
    """

    total_detections: int = 0
    would_expand_count: int = 0
    actually_expanded_count: int = 0
    _divergences: int = 0

    expansion_accuracy_hits: int = 0
    expansion_accuracy_total: int = 0
    false_positive_count: int = 0
    false_positive_total: int = 0
    latency_samples_ms: List[float] = field(default_factory=list)
    user_override_count: int = 0
    user_override_total: int = 0
    mind_quality_expanded_scores: List[float] = field(default_factory=list)
    mind_quality_single_scores: List[float] = field(default_factory=list)

    # ---- recording helpers ----

    def record_detection(self, would_expand: bool, actually_expanded: bool) -> None:
        """Record one detection event and track divergence."""
        self.total_detections += 1
        if would_expand:
            self.would_expand_count += 1
        if actually_expanded:
            self.actually_expanded_count += 1
        if would_expand != actually_expanded:
            self._divergences += 1

    @property
    def divergence_rate(self) -> float:
        """Fraction of detections where intent and action diverged."""
        if self.total_detections == 0:
            return 0.0
        return self._divergences / self.total_detections

    def record_latency(self, ms: float) -> None:
        """Record one latency sample, keeping at most 1000 recent entries."""
        self.latency_samples_ms.append(ms)
        if len(self.latency_samples_ms) > 1000:
            self.latency_samples_ms = self.latency_samples_ms[-1000:]

    def record_mind_quality(self, expanded_score: float, single_score: float) -> None:
        """Record quality scores for expanded vs single-intent plans."""
        self.mind_quality_expanded_scores.append(expanded_score)
        self.mind_quality_single_scores.append(single_score)
        if len(self.mind_quality_expanded_scores) > 1000:
            self.mind_quality_expanded_scores = self.mind_quality_expanded_scores[-1000:]
            self.mind_quality_single_scores = self.mind_quality_single_scores[-1000:]

    # ---- computed properties ----

    @property
    def mind_quality_regressed(self) -> bool:
        """True when expanded plans score worse on average than single plans.

        Requires at least 10 samples to avoid premature conclusions.
        """
        if len(self.mind_quality_expanded_scores) < 10:
            return False
        avg_expanded = sum(self.mind_quality_expanded_scores) / len(self.mind_quality_expanded_scores)
        avg_single = sum(self.mind_quality_single_scores) / len(self.mind_quality_single_scores)
        return avg_expanded < avg_single

    @property
    def latency_p95_ms(self) -> float:
        """95th percentile latency across recorded samples."""
        if not self.latency_samples_ms:
            return 0.0
        sorted_samples = sorted(self.latency_samples_ms)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    # ---- go/no-go gate ----

    def go_no_go_status(self) -> Dict[str, Any]:
        """Evaluate all promotion gates and return a status dict.

        Gates:
          expansion_accuracy  >= 0.8
          false_positive_rate <= 0.1
          latency_p95_ms      <= 500
          mind_plan_quality    no regression
          user_override_rate  <= 0.2
        """
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
            "expansion_accuracy": {
                "value": ea_rate,
                "threshold": 0.8,
                "pass": ea_rate >= 0.8,
                "n": self.expansion_accuracy_total,
            },
            "false_positive_rate": {
                "value": fp_rate,
                "threshold": 0.1,
                "pass": fp_rate <= 0.1,
                "n": self.false_positive_total,
            },
            "latency_p95_ms": {
                "value": self.latency_p95_ms,
                "threshold": 500,
                "pass": self.latency_p95_ms <= 500,
                "n": len(self.latency_samples_ms),
            },
            "mind_plan_quality": {
                "value": "no_regression" if quality_pass else "regressed",
                "threshold": "no_regression",
                "pass": quality_pass,
                "n": len(self.mind_quality_expanded_scores),
            },
            "user_override_rate": {
                "value": override_rate,
                "threshold": 0.2,
                "pass": override_rate <= 0.2,
                "n": self.user_override_total,
            },
            "all_gates_pass": (
                ea_rate >= 0.8
                and fp_rate <= 0.1
                and self.latency_p95_ms <= 500
                and quality_pass
                and override_rate <= 0.2
            ),
        }


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
