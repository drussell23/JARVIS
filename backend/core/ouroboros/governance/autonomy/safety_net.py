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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
    _deterministic_key,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.component_health import (
    ComponentHealthTracker,
    ComponentState,
    TransitionReason,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.execution_monitor import (
    ExecutionMonitor,
    ExecutionOutcome,
    ExecutionStatus,
)
from backend.core.ouroboros.governance.autonomy.risk_classifier import (
    OperationRiskClassifier,
    RiskAssessment,
)

logger = logging.getLogger("Ouroboros.SafetyNet")


@dataclass
class SafetyNetConfig:
    """Configuration for ProductionSafetyNet.

    All thresholds are configurable; no hardcoded magic numbers.

    The two probe-failure thresholds are env-tunable so battle-test
    harnesses can raise them above the ambient background noise rate.
    Session bt-2026-04-15-085222 (Session J, 2026-04-15) showed that
    the 13-minute complex-route arc accumulates ~5 probe failures
    from unrelated sandbox_fallback readonly-database retries during
    the run, which trips L3 READ_ONLY_PLANNING mid-L2-repair and
    cancels the in-flight op. Raising
    JARVIS_SAFETY_NET_SEVERE_THRESHOLD to 50+ for battle tests
    keeps L3 escalation reserved for genuinely unhealthy conditions.
    Defaults preserve the pre-patch behavior when env vars are unset.
    """

    probe_failure_escalation_threshold: int = field(
        default_factory=lambda: int(
            os.environ.get("JARVIS_SAFETY_NET_ESCALATION_THRESHOLD", "3")
        )
    )
    probe_failure_severe_threshold: int = field(
        default_factory=lambda: int(
            os.environ.get("JARVIS_SAFETY_NET_SEVERE_THRESHOLD", "5")
        )
    )
    # Anthropic resilience pack 2026-04-25 — auto-recovery from L3 demotion.
    # Without this, a transient Anthropic API instability that triggered
    # REDUCED_AUTONOMY / READ_ONLY_PLANNING demotion stays sticky FOREVER
    # (until session restart). The failure counter resets on success, but
    # no REQUEST_MODE_SWITCH back to FULL_AUTONOMY ever fires.
    #
    # Observed live in F1 Slice 4 S4b (`bt-2026-04-25-085942`): L3 demoted
    # to READ_ONLY_PLANNING at 02:15:57; the seed couldn't reach
    # post-GENERATE seam for the rest of the 30+ minute session even if
    # Anthropic recovered.
    #
    # The fix: track consecutive successes WHILE in degraded state; after
    # `probe_recovery_success_threshold` (default 3) emit
    # REQUEST_MODE_SWITCH back to FULL_AUTONOMY. Default 3 matches the
    # escalation threshold (symmetric: 3 fails to demote, 3 successes to
    # promote). Master-off via threshold=0 → byte-for-byte pre-fix.
    probe_recovery_success_threshold: int = field(
        default_factory=lambda: int(
            os.environ.get("JARVIS_SAFETY_NET_RECOVERY_THRESHOLD", "3")
        )
    )
    rollback_pattern_threshold: int = 2
    rollback_pattern_window_s: float = 3600.0
    human_presence_defer_s: float = 300.0
    incident_rollback_threshold: int = 3
    resource_violation_rate_threshold: float = 0.5
    max_rollback_history: int = 200


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
        # Anthropic resilience pack — track consecutive successes WHILE
        # in degraded state for auto-promotion back to FULL_AUTONOMY.
        # Reset whenever we enter or leave degraded mode (so the count
        # only accumulates within a single recovery window).
        self._consecutive_successes_while_degraded: int = 0
        self._rollback_history: List[Dict[str, Any]] = []
        self._incident_triggered: bool = False
        self._health_tracker: ComponentHealthTracker = ComponentHealthTracker()
        self._execution_monitor: ExecutionMonitor = ExecutionMonitor()
        self._risk_classifier: OperationRiskClassifier = OperationRiskClassifier()
        self._escalated_resource_violation: bool = False

    # ------------------------------------------------------------------
    # Event handler registration
    # ------------------------------------------------------------------

    def register_event_handlers(self, emitter: EventEmitter) -> None:
        """Subscribe to events this service cares about.

        Currently: HEALTH_PROBE_RESULT, OP_ROLLED_BACK, and OP_COMPLETED.
        """
        emitter.subscribe(EventType.HEALTH_PROBE_RESULT, self._on_health_probe)
        emitter.subscribe(EventType.OP_ROLLED_BACK, self._on_rollback)
        emitter.subscribe(EventType.OP_COMPLETED, self._on_op_completed)

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
        component_name = payload.get("component", "system")

        if payload.get("success"):
            # Anthropic resilience pack 2026-04-25 — auto-recovery from
            # L3 demotion. Track consecutive successes WHILE in degraded
            # state. After `probe_recovery_success_threshold` (default 3)
            # emit REQUEST_MODE_SWITCH back to FULL_AUTONOMY. Without this,
            # L3 demotion is sticky until session restart even after the
            # external API recovers.
            _recovery_threshold = self._config.probe_recovery_success_threshold
            _was_degraded = self._escalated_reduced or self._escalated_readonly
            if _recovery_threshold > 0 and _was_degraded:
                self._consecutive_successes_while_degraded += 1
                if self._consecutive_successes_while_degraded >= _recovery_threshold:
                    # Promote back to FULL_AUTONOMY. The L1 governor's
                    # mode switcher honors this via the same CommandBus
                    # path the demotion used (REQUEST_MODE_SWITCH).
                    cmd = CommandEnvelope(
                        source_layer="L3",
                        target_layer="L1",
                        command_type=CommandType.REQUEST_MODE_SWITCH,
                        payload={
                            "target_mode": "FULL_AUTONOMY",
                            "reason": (
                                f"{self._consecutive_successes_while_degraded} "
                                "consecutive probe successes — "
                                "auto-recovery from degraded mode"
                            ),
                            "evidence_count": self._consecutive_successes_while_degraded,
                            "probe_recovery_streak": self._consecutive_successes_while_degraded,
                        },
                        ttl_s=300.0,
                    )
                    self._bus.try_put(cmd)
                    # Reset state so subsequent demotions work fresh.
                    self._escalated_reduced = False
                    self._escalated_readonly = False
                    self._consecutive_successes_while_degraded = 0
                    logger.info(
                        "[SafetyNet] Auto-recovery: L3 promoted to FULL_AUTONOMY "
                        "after %d consecutive probe successes "
                        "(component=%s)",
                        _recovery_threshold, component_name,
                    )
            elif _recovery_threshold <= 0:
                # Recovery DISABLED — preserve byte-for-byte pre-fix
                # semantics: clear escalated flags on every success so
                # subsequent failure bursts can re-emit a demotion command.
                self._escalated_reduced = False
                self._escalated_readonly = False
                self._consecutive_successes_while_degraded = 0
            else:
                # Not degraded + recovery enabled — keep counter at 0
                # (only accumulates in the recovery window).
                self._consecutive_successes_while_degraded = 0
            self._consecutive_failures = 0
            # Update health tracker: success -> ACTIVE with score from payload or 1.0
            score = payload.get("health_score", 1.0)
            self._health_tracker.update(
                component_name,
                ComponentState.ACTIVE,
                health_score=score,
                reason=TransitionReason.RECOVERY,
            )
            return

        self._consecutive_failures += 1
        # Reset recovery streak on any failure — the recovery window
        # only counts CONSECUTIVE successes.
        self._consecutive_successes_while_degraded = 0

        # Update health tracker: failure -> ERROR, decrement score by 0.1
        current = self._health_tracker.get_status(component_name)
        if current is not None:
            new_score = max(0.0, current.health_score - 0.1)
        else:
            new_score = 0.9  # first failure from implicit 1.0
        self._health_tracker.update(
            component_name,
            ComponentState.ERROR,
            health_score=new_score,
            reason=TransitionReason.ERROR,
        )

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
    # Operation completed handler — execution monitoring (Task H3)
    # ------------------------------------------------------------------

    def _on_op_completed(self, event: EventEnvelope) -> None:
        """React to an operation completion event.

        Classifies the execution status, records the outcome in the
        execution monitor, and checks whether the resource violation
        rate has breached the configured threshold.
        """
        payload = event.payload
        status = self._execution_monitor.classify_from_payload(payload)

        outcome = ExecutionOutcome(
            op_id=payload.get("op_id", event.op_id or "unknown"),
            status=status,
            start_ns=time.monotonic_ns() - int(payload.get("duration_ms", 0) * 1_000_000),
            end_ns=time.monotonic_ns(),
            duration_ms=payload.get("duration_ms", 0.0),
            error_message=payload.get("error", ""),
            memory_peak_mb=payload.get("memory_peak_mb", 0.0),
            call_depth=payload.get("call_depth", 0),
        )
        self._execution_monitor.record(outcome)

        # Check for resource violation escalation
        rv_rate = self._execution_monitor.get_resource_violation_rate()
        if (
            rv_rate > self._config.resource_violation_rate_threshold
            and not self._escalated_resource_violation
        ):
            self._escalated_resource_violation = True
            cmd = CommandEnvelope(
                source_layer="L3",
                target_layer="L1",
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "REDUCED_AUTONOMY",
                    "reason": (
                        f"Resource violation rate {rv_rate:.1%} exceeds "
                        f"threshold {self._config.resource_violation_rate_threshold:.1%}"
                    ),
                    "evidence_count": self._execution_monitor.total_recorded,
                    "resource_violation_rate": rv_rate,
                },
                ttl_s=300.0,
            )
            self._bus.try_put(cmd)

    # ------------------------------------------------------------------
    # Execution summary
    # ------------------------------------------------------------------

    def get_execution_summary(self) -> Dict[str, Any]:
        """Return a snapshot of execution monitoring for telemetry.

        Delegates to :meth:`ExecutionMonitor.to_dict`.
        """
        return self._execution_monitor.to_dict()

    # ------------------------------------------------------------------
    # Rollback handler — L3 root cause analysis (Task 10: P1.6)
    # ------------------------------------------------------------------

    def _on_rollback(self, event: EventEnvelope) -> None:
        """Analyze rollback, emit root cause report, detect patterns.

        For every rollback event:
        1. Record in history and prune entries outside the configured window.
        2. Check for a pattern: same reason repeated >= threshold times.
        3. Classify the root cause into a known category.
        4. Classify risk using :class:`OperationRiskClassifier`.
        5. Emit a REPORT_ROLLBACK_CAUSE command to L2 for attribution
           (includes ``risk_level`` and ``risk_score``).
        6. If the count-based incident threshold is reached, trigger incident.
        7. If risk is actionable and no incident yet triggered, trigger
           risk-aware early escalation.
        """
        payload = event.payload
        op_id = payload.get("op_id", "unknown")
        reason = payload.get("rollback_reason", "unknown")
        brain_id = payload.get("brain_id", "unknown")
        affected_files: list = payload.get("affected_files", [])

        now = time.monotonic()
        self._rollback_history.append({
            "op_id": op_id,
            "brain_id": brain_id,
            "reason": reason,
            "ts": now,
            "affected_files": affected_files,
        })

        # Prune old entries outside the configured window
        self._rollback_history = [
            r for r in self._rollback_history
            if now - r["ts"] < self._config.rollback_pattern_window_s
        ]
        # Hard capacity cap to prevent unbounded growth under rapid failures
        if len(self._rollback_history) > self._config.max_rollback_history:
            self._rollback_history = self._rollback_history[-self._config.max_rollback_history:]

        # Check for pattern: same reason repeated
        same_reason = [r for r in self._rollback_history if r["reason"] == reason]
        pattern_match = len(same_reason) >= self._config.rollback_pattern_threshold

        # Classify root cause
        root_cause_class = self._classify_root_cause(reason)

        # Classify risk for escalation decisions
        assessment: RiskAssessment = self._risk_classifier.classify(
            rollback_count=len(self._rollback_history),
            window_s=self._config.rollback_pattern_window_s,
            affected_files=affected_files,
            root_cause_class=root_cause_class,
            pattern_match=pattern_match,
        )

        cmd = CommandEnvelope(
            source_layer="L3",
            target_layer="L2",
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={
                "op_id": op_id,
                "root_cause_class": root_cause_class,
                "affected_files": affected_files,
                "model_used": brain_id,
                "pattern_match": pattern_match,
                "similar_op_ids": [r["op_id"] for r in same_reason if r["op_id"] != op_id],
                "risk_level": assessment.level.value,
                "risk_score": assessment.score,
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)

        # Incident detection: too many rollbacks in window (count-based)
        if (len(self._rollback_history) >= self._config.incident_rollback_threshold
                and not self._incident_triggered):
            self._incident_triggered = True
            incident_cmd = CommandEnvelope(
                source_layer="L3",
                target_layer="L1",
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "READ_ONLY_PLANNING",
                    "reason": f"Incident: {len(self._rollback_history)} rollbacks in {self._config.rollback_pattern_window_s}s",
                    "evidence_count": len(self._rollback_history),
                    "probe_failure_streak": 0,
                },
                ttl_s=300.0,
            )
            self._bus.try_put(incident_cmd)

        # Risk-aware early escalation: if the classifier flags the
        # situation as actionable (HIGH or CRITICAL) and no incident
        # has been triggered yet via the normal count-based path,
        # escalate proactively.
        if assessment.is_actionable and not self._incident_triggered:
            self._incident_triggered = True
            risk_cmd = CommandEnvelope(
                source_layer="L3",
                target_layer="L1",
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "REDUCED_AUTONOMY",
                    "reason": (
                        f"Risk-aware escalation: {assessment.level.value} risk "
                        f"(score={assessment.score:.2f}) — {assessment.reason}"
                    ),
                    "evidence_count": len(self._rollback_history),
                    "risk_level": assessment.level.value,
                    "risk_score": assessment.score,
                },
                ttl_s=300.0,
            )
            self._bus.try_put(risk_cmd)

    # ------------------------------------------------------------------
    # Human presence signal — L3 → L1 (Task 12: P1.8)
    # ------------------------------------------------------------------

    def signal_human_presence(self, is_active: bool, activity_type: str = "unknown") -> None:
        """Signal human presence to L1 submit gate.

        When *is_active* is ``True``, a ``defer_until_ns`` timestamp is
        included so that L1 can defer autonomous operations while a human
        is actively working.  When ``False``, ``defer_until_ns`` is ``0``
        (no deferral).

        Idempotency is keyed on ``(is_active, activity_type)`` so that
        repeated signals with the same logical state are deduplicated by
        the :class:`CommandBus`, even though ``defer_until_ns`` varies
        between calls.
        """
        defer_until = (
            time.monotonic_ns() + int(self._config.human_presence_defer_s * 1e9)
            if is_active
            else 0
        )
        # Build a stable idempotency key from the logical signal identity
        # (source, target, type, is_active, activity_type) so that the
        # volatile defer_until_ns timestamp does not break dedup.
        stable_payload = {
            "is_active": is_active,
            "activity_type": activity_type,
        }
        idemp_key = _deterministic_key(
            "L3", "L1", CommandType.SIGNAL_HUMAN_PRESENCE, stable_payload,
        )
        cmd = CommandEnvelope(
            source_layer="L3",
            target_layer="L1",
            command_type=CommandType.SIGNAL_HUMAN_PRESENCE,
            payload={
                "is_active": is_active,
                "activity_type": activity_type,
                "defer_until_ns": defer_until,
            },
            ttl_s=300.0,
            idempotency_key=idemp_key,
        )
        self._bus.try_put(cmd)

    # ------------------------------------------------------------------
    # Component health summary
    # ------------------------------------------------------------------

    def get_health_summary(self) -> Dict[str, Any]:
        """Return a snapshot of component health for telemetry/dashboards.

        Includes the full tracker ``to_dict()`` output plus aggregate
        health and a list of unhealthy component names.
        """
        summary = self._health_tracker.to_dict()
        summary["aggregate_health"] = self._health_tracker.get_aggregate_health()
        summary["unhealthy_components"] = [
            c.name for c in self._health_tracker.get_unhealthy()
        ]
        return summary

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
