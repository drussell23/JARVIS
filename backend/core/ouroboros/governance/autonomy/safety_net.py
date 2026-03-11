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
    # Rollback handler — L3 root cause analysis (Task 10: P1.6)
    # ------------------------------------------------------------------

    def _on_rollback(self, event: EventEnvelope) -> None:
        """Analyze rollback, emit root cause report, detect patterns.

        For every rollback event:
        1. Record in history and prune entries outside the configured window.
        2. Check for a pattern: same reason repeated >= threshold times.
        3. Classify the root cause into a known category.
        4. Emit a REPORT_ROLLBACK_CAUSE command to L2 for attribution.
        """
        payload = event.payload
        op_id = payload.get("op_id", "unknown")
        reason = payload.get("rollback_reason", "unknown")
        brain_id = payload.get("brain_id", "unknown")

        now = time.monotonic()
        self._rollback_history.append({
            "op_id": op_id,
            "brain_id": brain_id,
            "reason": reason,
            "ts": now,
        })

        # Prune old entries outside the configured window
        self._rollback_history = [
            r for r in self._rollback_history
            if now - r["ts"] < self._config.rollback_pattern_window_s
        ]

        # Check for pattern: same reason repeated
        same_reason = [r for r in self._rollback_history if r["reason"] == reason]
        pattern_match = len(same_reason) >= self._config.rollback_pattern_threshold

        # Classify root cause
        root_cause_class = self._classify_root_cause(reason)

        cmd = CommandEnvelope(
            source_layer="L3",
            target_layer="L2",
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={
                "op_id": op_id,
                "root_cause_class": root_cause_class,
                "affected_files": payload.get("affected_files", []),
                "model_used": brain_id,
                "pattern_match": pattern_match,
                "similar_op_ids": [r["op_id"] for r in same_reason if r["op_id"] != op_id],
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)

    @staticmethod
    def _classify_root_cause(reason: str) -> str:
        """Classify rollback reason into root cause category.

        Categories:
        - validation_failure: validation/verify step failed
        - timeout: operation timed out
        - syntax_error: syntax or parse error in generated code
        - test_failure: test suite failure
        - permission_error: permission or access denied
        - unknown: no recognized pattern
        """
        reason_lower = reason.lower()
        if "validation" in reason_lower or "validate" in reason_lower:
            return "validation_failure"
        if "timeout" in reason_lower:
            return "timeout"
        if "syntax" in reason_lower or "parse" in reason_lower:
            return "syntax_error"
        if "test" in reason_lower:
            return "test_failure"
        if "permission" in reason_lower or "access" in reason_lower:
            return "permission_error"
        return "unknown"
