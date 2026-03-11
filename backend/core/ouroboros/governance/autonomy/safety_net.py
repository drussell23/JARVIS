"""backend/core/ouroboros/governance/autonomy/safety_net.py

Production Safety Net — L3 Safety & Reliability Service.

Monitors health probes, analyzes rollback patterns, detects incidents,
and signals human presence. All outputs are advisory CommandEnvelopes
routed to L1 via the CommandBus.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

logger = logging.getLogger("Ouroboros.SafetyNet")


@dataclass
class SafetyNetConfig:
    """Configuration for ProductionSafetyNet.

    All thresholds are configurable; no hardcoded magic numbers.
    """

    probe_failure_escalation_threshold: int = 3
    probe_failure_severe_threshold: int = 5
    rollback_pattern_threshold: int = 2
    rollback_pattern_window_s: float = 3600.0
    human_presence_defer_s: float = 300.0


class ProductionSafetyNet:
    """L3 — Safety & Reliability. Advisory only.

    Tracks consecutive health probe failures and emits
    ``REQUEST_MODE_SWITCH`` commands when thresholds are breached.

    Designed for extensibility: rollback root cause analysis,
    incident auto-trigger, and human presence signal will be
    added in subsequent tasks (10-12).
    """

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[SafetyNetConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or SafetyNetConfig()
        self._consecutive_failures: int = 0
        self._escalated_reduced: bool = False
        self._escalated_readonly: bool = False
        self._rollback_history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Event handler registration
    # ------------------------------------------------------------------

    def register_event_handlers(self, emitter: EventEmitter) -> None:
        """Subscribe to events this service cares about.

        Currently: HEALTH_PROBE_RESULT and OP_ROLLED_BACK.
        """
        emitter.subscribe(EventType.HEALTH_PROBE_RESULT, self._on_health_probe)
        emitter.subscribe(EventType.OP_ROLLED_BACK, self._on_rollback)

    # ------------------------------------------------------------------
    # Health probe handler
    # ------------------------------------------------------------------

    def _on_health_probe(self, event: EventEnvelope) -> None:
        """React to a health probe result event.

        On success: reset consecutive failure count and escalation flags.
        On failure: increment count and emit mode switch commands if
        thresholds are breached.

        Escalation levels:
          - At ``probe_failure_escalation_threshold``: REDUCED_AUTONOMY
          - At ``probe_failure_severe_threshold``: READ_ONLY_PLANNING
        """
        payload = event.payload
        if payload.get("success"):
            self._consecutive_failures = 0
            self._escalated_reduced = False
            self._escalated_readonly = False
            return

        self._consecutive_failures += 1

        # Severe escalation check first (higher threshold)
        if (
            self._consecutive_failures >= self._config.probe_failure_severe_threshold
            and not self._escalated_readonly
        ):
            self._escalated_readonly = True
            cmd = CommandEnvelope(
                source_layer="L3",
                target_layer="L1",
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "READ_ONLY_PLANNING",
                    "reason": (
                        f"{self._consecutive_failures} consecutive probe failures"
                    ),
                    "evidence_count": self._consecutive_failures,
                    "probe_failure_streak": self._consecutive_failures,
                },
                ttl_s=300.0,
            )
            self._bus.try_put(cmd)

        # Standard escalation check
        elif (
            self._consecutive_failures >= self._config.probe_failure_escalation_threshold
            and not self._escalated_reduced
        ):
            self._escalated_reduced = True
            cmd = CommandEnvelope(
                source_layer="L3",
                target_layer="L1",
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "REDUCED_AUTONOMY",
                    "reason": (
                        f"{self._consecutive_failures} consecutive probe failures"
                    ),
                    "evidence_count": self._consecutive_failures,
                    "probe_failure_streak": self._consecutive_failures,
                },
                ttl_s=300.0,
            )
            self._bus.try_put(cmd)

    # ------------------------------------------------------------------
    # Rollback handler (stub — extended in Task 10)
    # ------------------------------------------------------------------

    def _on_rollback(self, event: EventEnvelope) -> None:
        """Track rollback events for pattern analysis.

        This is a minimal implementation that records rollback history.
        Full root cause analysis is added in Task 10.
        """
        self._rollback_history.append(
            {
                "op_id": event.payload.get("op_id"),
                "brain_id": event.payload.get("brain_id"),
                "reason": event.payload.get("rollback_reason"),
                "ts": time.monotonic(),
            }
        )
        # Prune entries older than the configured window
        now = time.monotonic()
        self._rollback_history = [
            r
            for r in self._rollback_history
            if now - r["ts"] < self._config.rollback_pattern_window_s
        ]
