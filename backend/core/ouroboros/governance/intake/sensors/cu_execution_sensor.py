"""
CUExecutionSensor — Observes CU (Computer Use) step execution telemetry
and detects recurring failure patterns for Ouroboros self-improvement.

Pillar 6 (Neuroplasticity) + Pillar 7 (Absolute Observability):
  Deterministic: Pattern counting, rolling window, threshold check.
  Agentic: The governance pipeline decides HOW to fix the detected pattern.

Flow:
  ActionDispatcher completes a CU task
    -> calls CUExecutionSensor.record(result)
    -> sensor tracks failure patterns in a rolling window
    -> when a pattern recurs >= GRADUATION_THRESHOLD times,
       emits an IntentEnvelope to the Ouroboros intake router
    -> governance pipeline routes it to a brain for fix generation
    -> fix targets cu_task_planner.py / cu_step_executor.py

This is the organism's nervous system for CU execution quality.
The sensor detects pain; Ouroboros heals the wound.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-driven, Manifesto Section 5)
# ---------------------------------------------------------------------------

# How many times a failure pattern must recur before emitting an envelope
_GRADUATION_THRESHOLD = int(
    os.environ.get("JARVIS_CU_FAILURE_THRESHOLD", "3")
)

# Rolling window for pattern tracking (seconds)
_WINDOW_S = float(os.environ.get("JARVIS_CU_FAILURE_WINDOW_S", "86400"))  # 24h

# Cooldown after emitting an envelope for a pattern (avoid spamming)
_EMIT_COOLDOWN_S = float(
    os.environ.get("JARVIS_CU_EMIT_COOLDOWN_S", "3600")
)  # 1 hour


# ---------------------------------------------------------------------------
# Telemetry record
# ---------------------------------------------------------------------------


@dataclass
class CUExecutionRecord:
    """One CU task execution result."""

    goal: str
    success: bool
    steps_completed: int
    steps_total: int
    elapsed_s: float
    error: Optional[str] = None
    is_messaging: bool = False
    contact: Optional[str] = None
    app: Optional[str] = None
    layers_used: Dict[str, int] = field(default_factory=dict)
    antipatterns_blocked: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def failure_signature(self) -> str:
        """Unique signature for this failure pattern (dedup key).

        Groups failures by: task type + error category + app context.
        """
        parts = []
        if self.is_messaging:
            parts.append("messaging")
            if self.app:
                parts.append(self.app.lower())
        else:
            # First 3 words of goal for generic tasks
            words = self.goal.split()[:3]
            parts.append("_".join(w.lower() for w in words))

        if self.error:
            # Normalize error to a category
            err = self.error.lower()
            if "target" in err or "not found" in err:
                parts.append("target_miss")
            elif "timeout" in err:
                parts.append("timeout")
            elif "vision" in err or "layer" in err:
                parts.append("vision_fail")
            else:
                parts.append("other")
        else:
            parts.append("partial")

        return ":".join(parts)


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class CUExecutionSensor:
    """Ouroboros intake sensor for CU execution telemetry.

    Follows the sensor protocol:
      - async start()      — no-op (event-driven, not polling)
      - stop()             — clears state
      - record(result)     — ingest one CU execution result
    """

    _instance: Optional["CUExecutionSensor"] = None

    def __new__(cls, **kwargs: Any) -> "CUExecutionSensor":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        router: Any = None,
        repo: str = "jarvis",
    ) -> None:
        if self._initialized:
            # Allow re-wiring the router after construction
            if router is not None:
                self._router = router
            return
        self._initialized = True
        self._router = router
        self._repo = repo

        # Pattern tracking: signature -> list of timestamps
        self._failure_window: Dict[str, List[float]] = defaultdict(list)
        # Track when we last emitted for each signature (cooldown)
        self._last_emitted: Dict[str, float] = {}
        # Total records for observability
        self._total_records = 0
        self._total_failures = 0
        self._total_envelopes_emitted = 0

        logger.info("[CUExecutionSensor] initialized")

    async def start(self) -> None:
        """No-op — this sensor is event-driven, not polling."""
        logger.info("[CUExecutionSensor] started (event-driven mode)")

    def stop(self) -> None:
        """Clear tracking state."""
        self._failure_window.clear()
        self._last_emitted.clear()
        logger.info("[CUExecutionSensor] stopped")

    # ------------------------------------------------------------------
    # Public API: called by ActionDispatcher after CU execution
    # ------------------------------------------------------------------

    async def record(self, rec: CUExecutionRecord) -> None:
        """Record a CU execution result.

        On success: logs telemetry.
        On failure: tracks the failure pattern and emits an IntentEnvelope
        when the graduation threshold is reached.
        """
        self._total_records += 1

        if rec.success:
            logger.debug(
                "[CUExecutionSensor] Success: '%s' (%d steps, %.1fs)",
                rec.goal[:60],
                rec.steps_completed,
                rec.elapsed_s,
            )
            return

        self._total_failures += 1
        sig = rec.failure_signature

        # Add to rolling window
        now = time.time()
        self._failure_window[sig].append(now)

        # Prune entries outside the window
        cutoff = now - _WINDOW_S
        self._failure_window[sig] = [
            t for t in self._failure_window[sig] if t > cutoff
        ]

        count = len(self._failure_window[sig])
        logger.info(
            "[CUExecutionSensor] Failure #%d for pattern '%s': %s",
            count,
            sig,
            rec.error or "partial completion",
        )

        # Check graduation threshold
        if count >= _GRADUATION_THRESHOLD:
            # Check cooldown
            last = self._last_emitted.get(sig, 0)
            if now - last < _EMIT_COOLDOWN_S:
                logger.debug(
                    "[CUExecutionSensor] Pattern '%s' graduated but in cooldown (%.0fs remaining)",
                    sig,
                    _EMIT_COOLDOWN_S - (now - last),
                )
                return

            await self._emit_envelope(sig, rec, count)

    # ------------------------------------------------------------------
    # Envelope emission
    # ------------------------------------------------------------------

    async def _emit_envelope(
        self,
        signature: str,
        latest: CUExecutionRecord,
        occurrence_count: int,
    ) -> None:
        """Emit an IntentEnvelope to Ouroboros for a graduated failure pattern."""
        if self._router is None:
            logger.warning(
                "[CUExecutionSensor] No router wired — cannot emit envelope for '%s'",
                signature,
            )
            return

        description = (
            f"CU execution failure pattern detected ({occurrence_count}x in "
            f"{_WINDOW_S / 3600:.0f}h): {signature}. "
        )

        if latest.is_messaging:
            description += (
                f"Messaging task on {latest.app or 'unknown app'} "
                f"for contact '{latest.contact or 'unknown'}'. "
            )

        if latest.error:
            description += f"Last error: {latest.error}. "

        description += (
            f"Steps completed: {latest.steps_completed}/{latest.steps_total}. "
            f"Goal: '{latest.goal[:80]}'"
        )

        # Target the CU planner and executor for fixes
        target_files = (
            "backend/vision/cu_task_planner.py",
            "backend/vision/cu_step_executor.py",
        )

        evidence = {
            "signature": signature,
            "occurrence_count": occurrence_count,
            "latest_goal": latest.goal,
            "latest_error": latest.error,
            "steps_completed": latest.steps_completed,
            "steps_total": latest.steps_total,
            "is_messaging": latest.is_messaging,
            "contact": latest.contact,
            "app": latest.app,
            "layers_used": latest.layers_used,
            "antipatterns_blocked": latest.antipatterns_blocked,
            "window_hours": _WINDOW_S / 3600,
            "threshold": _GRADUATION_THRESHOLD,
        }

        envelope = make_envelope(
            source="cu_execution",
            description=description,
            target_files=target_files,
            repo=self._repo,
            confidence=min(0.95, 0.5 + occurrence_count * 0.1),
            urgency="high" if occurrence_count >= 5 else "normal",
            evidence=evidence,
            requires_human_ack=False,
        )

        try:
            result = await self._router.ingest(envelope)
            self._last_emitted[signature] = time.time()
            self._total_envelopes_emitted += 1
            logger.info(
                "[CUExecutionSensor] Envelope emitted for '%s' → %s (count=%d)",
                signature,
                result,
                occurrence_count,
            )
        except Exception as exc:
            logger.warning(
                "[CUExecutionSensor] Envelope emission failed for '%s': %s",
                signature,
                exc,
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return sensor stats for the telemetry dashboard."""
        return {
            "total_records": self._total_records,
            "total_failures": self._total_failures,
            "total_envelopes_emitted": self._total_envelopes_emitted,
            "active_patterns": len(self._failure_window),
            "pattern_counts": {
                sig: len(timestamps)
                for sig, timestamps in self._failure_window.items()
            },
        }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def get_cu_execution_sensor() -> CUExecutionSensor:
    """Get or create the singleton CUExecutionSensor."""
    return CUExecutionSensor()
