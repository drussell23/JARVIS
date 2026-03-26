"""backend/core/ouroboros/consciousness/situational_awareness.py

SAI -- Situational Awareness Intelligence Engine
==================================================

Understands WHERE / WHEN / WHY: environmental, temporal, and causal
awareness synthesised from peer engine signals.

Boundary Principle:
    SAI is a *read-only synthesiser*.  It queries HealthCortex,
    MemoryEngine, and ProphecyEngine via direct method calls but NEVER
    mutates their state.  It receives references to peer engines in its
    constructor; it does not control them.

Design:
    - Maintains TemporalPatterns (recurring time-correlated behaviours)
      and CausalChains (trigger-effect relationships) that persist across
      sessions as JSON on disk.
    - ``get_state()`` builds a ``SituationalState`` snapshot from live
      peer-engine data, internal operation tracking, and temporal risk
      analysis -- all non-blocking.
    - ``assess_situation()`` produces a ``SituationAssessment`` for a
      specific planned change -- timing advice, risk modifiers, posture.
    - A background ``_monitoring_loop`` runs every
      ``JARVIS_SAI_MONITOR_INTERVAL_S`` seconds (default 60) to:
        * Detect temporal patterns from operation history
        * Expire completed causal chains
        * Prune stale patterns past their TTL
    - start() is idempotent; stop() persists state and cancels tasks.
    - All exceptions from peer engines are caught; SAI never crashes its
      parent (TrinityConsciousness).

Thread-safety:
    All public methods are designed for single-event-loop usage.

Environment variables:
    JARVIS_SAI_MONITOR_INTERVAL_S   float   60.0
    JARVIS_SAI_PATTERN_TTL_HOURS    float   336.0  (14 days)
    JARVIS_SAI_CHAIN_MAX_SPAN_S     float   3600.0 (1 hour)
    JARVIS_SAI_FLUSH_INTERVAL_S     float   300.0  (5 minutes)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.SAI")

# ---------------------------------------------------------------------------
# Environment-driven constants
# ---------------------------------------------------------------------------

_MONITOR_INTERVAL_S: float = float(
    os.getenv("JARVIS_SAI_MONITOR_INTERVAL_S", "60.0")
)
_PATTERN_TTL_HOURS: float = float(
    os.getenv("JARVIS_SAI_PATTERN_TTL_HOURS", "336.0")
)
_CHAIN_MAX_SPAN_S: float = float(
    os.getenv("JARVIS_SAI_CHAIN_MAX_SPAN_S", "3600.0")
)
_FLUSH_INTERVAL_S: float = float(
    os.getenv("JARVIS_SAI_FLUSH_INTERVAL_S", "300.0")
)

_PATTERNS_FILENAME = "sai_temporal_patterns.json"
_CHAINS_FILENAME = "sai_causal_chains.json"

_DEFAULT_PERSISTENCE_DIR = (
    Path.home() / ".jarvis" / "ouroboros" / "consciousness"
)

# ---------------------------------------------------------------------------
# Time-of-day buckets
# ---------------------------------------------------------------------------

_TIME_BUCKETS: Tuple[Tuple[int, int, str], ...] = (
    (0, 4, "late_night"),
    (5, 11, "morning"),
    (12, 16, "afternoon"),
    (17, 20, "evening"),
    (21, 23, "night"),
)

# ---------------------------------------------------------------------------
# Activity thresholds (operations in last hour)
# ---------------------------------------------------------------------------

_ACTIVITY_QUIET_MAX = 2
_ACTIVITY_ACTIVE_MAX = 8
# > _ACTIVITY_ACTIVE_MAX => "intense"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TemporalPattern:
    """A recurring time-correlated behavioural pattern.

    Args:
        pattern_id:       UUID.
        pattern_type:     One of "time_of_day", "day_of_week", "post_deploy",
                          "failure_cascade", "peak_activity".
        description:      Human-readable summary.
        evidence:         Timestamps or op_ids that support this pattern.
        confidence:       0.0--1.0.
        first_seen:       Monotonic timestamp when first detected.
        last_seen:        Monotonic timestamp of most recent supporting event.
        occurrence_count: Number of times this pattern has been observed.
    """

    pattern_id: str
    pattern_type: str
    description: str
    evidence: List[str]
    confidence: float
    first_seen: float
    last_seen: float
    occurrence_count: int


@dataclass
class CausalChain:
    """An observed trigger-to-effect causal relationship.

    Args:
        chain_id:       UUID.
        trigger:        What started the chain (e.g. "edit auth/login.py").
        effects:        Observed downstream effects.
        time_span_s:    How long the chain took to unfold.
        confidence:     0.0--1.0.
        observed_count: How many times this chain has been observed.
    """

    chain_id: str
    trigger: str
    effects: List[str]
    time_span_s: float
    confidence: float
    observed_count: int


@dataclass(frozen=True)
class SituationalState:
    """Point-in-time situational snapshot.

    Args:
        time_of_day:           "morning", "afternoon", "evening", "night",
                               "late_night".
        day_type:              "weekday" or "weekend".
        system_load:           "idle", "normal", "busy", "overloaded".
        recent_activity_level: "quiet", "active", "intense".
        active_causal_chains:  Chains still unfolding.
        temporal_risk_factors: Time-based risk strings.
        environmental_flags:   "low_memory", "high_cpu", "vm_offline", etc.
        recommended_posture:   "AGGRESSIVE", "NORMAL", "CAUTIOUS", "DEFENSIVE".
        posture_reason:        Why this posture was chosen.
        timestamp:             time.time() when state was computed.
    """

    time_of_day: str
    day_type: str
    system_load: str
    recent_activity_level: str
    active_causal_chains: Tuple[CausalChain, ...]
    temporal_risk_factors: Tuple[str, ...]
    environmental_flags: Tuple[str, ...]
    recommended_posture: str
    posture_reason: str
    timestamp: float


@dataclass(frozen=True)
class SituationAssessment:
    """Assessment of a planned change in context of the current situation.

    Args:
        target_files:    Files targeted by the planned change.
        timing_advice:   e.g. "Good time for this change -- morning, low activity".
        risk_modifiers:  Situational factors that raise/lower risk.
        posture:         Recommended execution posture.
        active_chains:   Causal chains involving target files.
        confidence:      0.0--1.0.
    """

    target_files: Tuple[str, ...]
    timing_advice: str
    risk_modifiers: Tuple[str, ...]
    posture: str
    active_chains: Tuple[CausalChain, ...]
    confidence: float


# ---------------------------------------------------------------------------
# Internal tracking structs
# ---------------------------------------------------------------------------


@dataclass
class _OperationRecord:
    """Lightweight record of an in-flight or completed operation."""

    op_id: str
    target_files: Tuple[str, ...]
    started_at: float
    ended_at: Optional[float] = None
    success: Optional[bool] = None
    duration_s: Optional[float] = None


# ---------------------------------------------------------------------------
# SituationalAwarenessEngine
# ---------------------------------------------------------------------------


class SituationalAwarenessEngine:
    """SAI -- synthesises environmental, temporal, and causal signals.

    Parameters
    ----------
    health_cortex:
        HealthCortex instance with ``get_snapshot()``.
    memory_engine:
        MemoryEngine instance (for file reputation queries).
    prophecy_engine:
        ProphecyEngine instance (for risk score queries).
    config:
        ConsciousnessConfig with feature flags and tuning knobs.
    persistence_dir:
        Override the default persistence path (for tests).
    comm:
        Optional CommProtocol instance for emitting heartbeat messages.
    """

    def __init__(
        self,
        health_cortex: Any,
        memory_engine: Any,
        prophecy_engine: Any,
        config: Any,
        persistence_dir: Optional[Path] = None,
        comm: Any = None,
    ) -> None:
        self._cortex = health_cortex
        self._memory = memory_engine
        self._prophecy = prophecy_engine
        self._config = config
        self._persistence_dir: Path = persistence_dir or _DEFAULT_PERSISTENCE_DIR
        self._comm = comm

        # Mutable state -- only touched inside the event loop
        self._temporal_patterns: List[TemporalPattern] = []
        self._causal_chains: List[CausalChain] = []
        self._operations: Dict[str, _OperationRecord] = {}
        self._operation_timestamps: List[float] = []  # for activity level
        self._recent_failures: List[float] = []  # timestamps of recent failures

        # Background task handles
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted state and start the monitoring loop.

        Idempotent: second call is a no-op.
        """
        if self._running:
            return

        await self._load_state()
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(), name="sai_monitor"
        )
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="sai_flush"
        )
        self._running = True
        logger.info(
            "[SAI] Started (monitor_interval=%.1fs, pattern_ttl=%.0fh)",
            _MONITOR_INTERVAL_S,
            _PATTERN_TTL_HOURS,
        )

    async def stop(self) -> None:
        """Persist state to disk, cancel background loops."""
        self._running = False
        for task in (self._monitor_task, self._flush_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._monitor_task = None
        self._flush_task = None
        await self._flush_state()
        logger.info("[SAI] Stopped.")

    # ------------------------------------------------------------------
    # Core awareness: get_state()
    # ------------------------------------------------------------------

    def get_state(self) -> SituationalState:
        """Build and return a situational snapshot from live signals.

        This method is synchronous and non-blocking.  All data comes
        from cached peer-engine state and internal tracking.
        """
        now = datetime.now()
        now_ts = time.time()

        # --- Time of day ---
        tod = _hour_to_bucket(now.hour)

        # --- Day type ---
        day_type = "weekend" if now.weekday() >= 5 else "weekday"

        # --- System load (from HealthCortex) ---
        system_load = self._derive_system_load()

        # --- Recent activity level ---
        recent_activity = self._derive_activity_level(now_ts)

        # --- Active causal chains ---
        active_chains = tuple(
            c for c in self._causal_chains
            if c.time_span_s < _CHAIN_MAX_SPAN_S
        )

        # --- Temporal risk factors ---
        risk_factors = self._derive_temporal_risks(tod, day_type, now_ts)

        # --- Environmental flags ---
        env_flags = self._derive_environmental_flags()

        # --- Recommended posture ---
        recent_failure_count = self._count_recent_failures(now_ts, window_s=3600.0)
        posture, reason = self._compute_posture(
            tod=tod,
            day_type=day_type,
            system_load=system_load,
            recent_activity=recent_activity,
            recent_failure_count=recent_failure_count,
            env_flags=env_flags,
        )

        return SituationalState(
            time_of_day=tod,
            day_type=day_type,
            system_load=system_load,
            recent_activity_level=recent_activity,
            active_causal_chains=active_chains,
            temporal_risk_factors=tuple(risk_factors),
            environmental_flags=tuple(env_flags),
            recommended_posture=posture,
            posture_reason=reason,
            timestamp=now_ts,
        )

    # ------------------------------------------------------------------
    # Core awareness: assess_situation()
    # ------------------------------------------------------------------

    def assess_situation(
        self,
        target_files: Tuple[str, ...],
        goal: str,
    ) -> SituationAssessment:
        """Assess the situation for a planned change.

        Parameters
        ----------
        target_files:
            Repo-relative paths of files targeted by the change.
        goal:
            A brief description of the change goal.

        Returns
        -------
        SituationAssessment
            Timing advice, risk modifiers, recommended posture, and
            any active causal chains involving the target files.
        """
        state = self.get_state()

        # Check for active causal chains involving target files
        involved_chains: List[CausalChain] = []
        for chain in self._causal_chains:
            # Check if any target file appears in trigger or effects
            for f in target_files:
                if f in chain.trigger or any(f in e for e in chain.effects):
                    involved_chains.append(chain)
                    break

        # Build timing advice
        timing_advice = self._build_timing_advice(state, target_files, goal)

        # Build risk modifiers
        risk_modifiers = list(state.temporal_risk_factors)
        if involved_chains:
            risk_modifiers.append(
                f"{len(involved_chains)} active causal chain(s) involving target files"
            )
        if state.system_load in ("busy", "overloaded"):
            risk_modifiers.append(
                f"system load is {state.system_load} -- contention risk"
            )
        if state.recent_activity_level == "intense":
            risk_modifiers.append(
                "intense recent activity -- cognitive overload risk"
            )

        # Look up temporal patterns for this time/day
        matching_patterns = self._find_relevant_patterns(state.time_of_day, state.day_type)
        for p in matching_patterns:
            risk_modifiers.append(
                f"pattern [{p.pattern_type}]: {p.description} (confidence {p.confidence:.0%})"
            )

        # Confidence based on available signal quality
        confidence = self._compute_assessment_confidence(state, involved_chains)

        return SituationAssessment(
            target_files=target_files,
            timing_advice=timing_advice,
            risk_modifiers=tuple(risk_modifiers),
            posture=state.recommended_posture,
            active_chains=tuple(involved_chains),
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Operation tracking
    # ------------------------------------------------------------------

    def record_operation_start(
        self,
        op_id: str,
        target_files: Tuple[str, ...],
    ) -> None:
        """Record the start of an operation for temporal pattern detection.

        Parameters
        ----------
        op_id:
            Unique identifier of the pipeline operation.
        target_files:
            Repo-relative paths involved in this operation.
        """
        now = time.time()
        self._operations[op_id] = _OperationRecord(
            op_id=op_id,
            target_files=target_files,
            started_at=now,
        )
        self._operation_timestamps.append(now)
        # Prune old timestamps (keep last 24 hours)
        cutoff = now - 86400.0
        self._operation_timestamps = [
            ts for ts in self._operation_timestamps if ts > cutoff
        ]

    def record_operation_end(
        self,
        op_id: str,
        success: bool,
        duration_s: float,
    ) -> None:
        """Complete an operation record and detect causal chains.

        Parameters
        ----------
        op_id:
            The operation identifier (must have been previously started).
        success:
            Whether the operation succeeded.
        duration_s:
            Wall-clock duration of the operation in seconds.
        """
        now = time.time()
        record = self._operations.get(op_id)
        if record is not None:
            record.ended_at = now
            record.success = success
            record.duration_s = duration_s

        if not success:
            self._recent_failures.append(now)
            # Prune old failure timestamps (keep last 24 hours)
            cutoff = now - 86400.0
            self._recent_failures = [
                ts for ts in self._recent_failures if ts > cutoff
            ]

        # Detect causal chain: if a recent completed op's target files
        # overlap with this op, and both happened within the chain window
        if record is not None:
            self._detect_causal_chains(record)

    def record_causal_observation(
        self,
        trigger: str,
        effect: str,
    ) -> None:
        """Manually record an observed cause-effect relationship.

        If an existing chain with the same trigger exists, the effect is
        appended and observed_count is incremented.  Otherwise a new
        chain is created.

        Parameters
        ----------
        trigger:
            What initiated the chain (e.g. "edit auth/login.py").
        effect:
            The downstream effect observed (e.g. "test_auth failed").
        """
        # Check for existing chain with same trigger
        for chain in self._causal_chains:
            if chain.trigger == trigger:
                if effect not in chain.effects:
                    chain.effects.append(effect)
                chain.observed_count += 1
                chain.confidence = min(
                    0.5 + 0.1 * chain.observed_count, 0.95
                )
                return

        # New chain
        self._causal_chains.append(
            CausalChain(
                chain_id=str(uuid.uuid4()),
                trigger=trigger,
                effects=[effect],
                time_span_s=0.0,
                confidence=0.5,
                observed_count=1,
            )
        )

    # ------------------------------------------------------------------
    # Temporal pattern queries
    # ------------------------------------------------------------------

    def query_temporal_patterns(
        self,
        time_range_hours: int = 24,
    ) -> List[TemporalPattern]:
        """Return temporal patterns observed within the given time range.

        Parameters
        ----------
        time_range_hours:
            Look-back window in hours from now (default 24).

        Returns
        -------
        List[TemporalPattern]
            Matching patterns sorted by confidence descending.
        """
        now_mono = time.monotonic()
        cutoff = now_mono - (time_range_hours * 3600.0)
        result = [
            p for p in self._temporal_patterns
            if p.last_seen >= cutoff
        ]
        result.sort(key=lambda p: p.confidence, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Prompt formatting
    # ------------------------------------------------------------------

    def format_for_prompt(self, assessment: SituationAssessment) -> str:
        """Format a SituationAssessment as a concise text block for LLM prompts.

        Parameters
        ----------
        assessment:
            The assessment to format.

        Returns
        -------
        str
            Multi-line string suitable for injection into a code-generation
            prompt as situational context.
        """
        lines: List[str] = [
            "--- Situational Context (SAI) ---",
            f"Posture: {assessment.posture}",
            f"Timing: {assessment.timing_advice}",
        ]
        if assessment.risk_modifiers:
            lines.append("Risk modifiers:")
            for rm in assessment.risk_modifiers:
                lines.append(f"  - {rm}")
        if assessment.active_chains:
            lines.append(
                f"Active causal chains: {len(assessment.active_chains)}"
            )
            for chain in assessment.active_chains:
                lines.append(
                    f"  - trigger={chain.trigger} -> "
                    f"{len(chain.effects)} effect(s), "
                    f"confidence={chain.confidence:.0%}"
                )
        lines.append(f"Confidence: {assessment.confidence:.0%}")
        lines.append("--- End Situational Context ---")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Health composite
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return a health dict for integration with TrinityConsciousness."""
        return {
            "running": self._running,
            "temporal_patterns": len(self._temporal_patterns),
            "causal_chains": len(self._causal_chains),
            "tracked_operations": len(self._operations),
        }

    # ------------------------------------------------------------------
    # Internal: background loops
    # ------------------------------------------------------------------

    async def _monitoring_loop(self) -> None:
        """Background task: detect patterns, expire chains, prune state."""
        while True:
            try:
                self._detect_temporal_patterns()
                self._expire_completed_chains()
                self._prune_old_patterns()
                self._prune_completed_operations()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[SAI] Error in monitoring loop")
            try:
                await asyncio.sleep(_MONITOR_INTERVAL_S)
            except asyncio.CancelledError:
                raise

    async def _flush_loop(self) -> None:
        """Background task: persist state to disk periodically."""
        while True:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            try:
                await self._flush_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[SAI] Error flushing state")

    # ------------------------------------------------------------------
    # Internal: derive signals from peer engines
    # ------------------------------------------------------------------

    def _derive_system_load(self) -> str:
        """Map HealthCortex overall_score to a load category."""
        try:
            snapshot = self._cortex.get_snapshot()
        except Exception:
            logger.debug("[SAI] HealthCortex.get_snapshot() failed", exc_info=True)
            return "normal"

        if snapshot is None:
            return "normal"

        score = getattr(snapshot, "overall_score", None)
        if score is None:
            return "normal"

        # Lower health score => higher load
        # score 1.0 = fully healthy = idle/normal
        # score 0.0 = all subsystems down = overloaded
        if score >= 0.9:
            return "idle"
        if score >= 0.6:
            return "normal"
        if score >= 0.3:
            return "busy"
        return "overloaded"

    def _derive_activity_level(self, now_ts: float) -> str:
        """Determine recent activity from operation timestamps in last hour."""
        cutoff = now_ts - 3600.0
        recent = sum(1 for ts in self._operation_timestamps if ts > cutoff)
        if recent <= _ACTIVITY_QUIET_MAX:
            return "quiet"
        if recent <= _ACTIVITY_ACTIVE_MAX:
            return "active"
        return "intense"

    def _derive_temporal_risks(
        self,
        tod: str,
        day_type: str,
        now_ts: float,
    ) -> List[str]:
        """Combine temporal patterns with current time to identify risks."""
        risks: List[str] = []

        # Time-of-day risks
        if tod == "late_night":
            # Check if there's a historical failure pattern at night
            night_patterns = [
                p for p in self._temporal_patterns
                if p.pattern_type == "time_of_day"
                and "night" in p.description.lower()
                and p.confidence >= 0.5
            ]
            if night_patterns:
                risks.append("late night -- higher error rate historically")
            else:
                risks.append("late night -- reduced alertness, exercise caution")

        if tod == "night":
            risks.append("evening/night session -- consider deferring risky changes")

        # Weekend risk
        if day_type == "weekend":
            risks.append("weekend -- slower incident response if issues arise")

        # Check for failure cascade patterns active right now
        cascade_patterns = [
            p for p in self._temporal_patterns
            if p.pattern_type == "failure_cascade"
            and p.confidence >= 0.6
        ]
        for cp in cascade_patterns:
            risks.append(f"active failure cascade pattern: {cp.description}")

        return risks

    def _derive_environmental_flags(self) -> List[str]:
        """Extract environmental flags from HealthCortex resource data."""
        flags: List[str] = []
        try:
            snapshot = self._cortex.get_snapshot()
        except Exception:
            logger.debug("[SAI] HealthCortex.get_snapshot() failed", exc_info=True)
            return flags

        if snapshot is None:
            return flags

        resources = getattr(snapshot, "resources", None)
        if resources is not None:
            if getattr(resources, "ram_percent", 0) >= 85.0:
                flags.append("low_memory")
            if getattr(resources, "cpu_percent", 0) >= 80.0:
                flags.append("high_cpu")
            if getattr(resources, "disk_percent", 0) >= 90.0:
                flags.append("low_disk")
            pressure = getattr(resources, "pressure", "NORMAL")
            if pressure in ("CRITICAL", "EMERGENCY"):
                flags.append(f"resource_pressure_{pressure.lower()}")

        # Check for VM / subsystem offline signals
        verdict = getattr(snapshot, "overall_verdict", "HEALTHY")
        if verdict == "CRITICAL":
            flags.append("critical_verdict")
        elif verdict == "DEGRADED":
            flags.append("degraded_system")

        # Check individual subsystem statuses for vm_offline
        for subsys_name in ("prime", "reactor"):
            subsys = getattr(snapshot, subsys_name, None)
            if subsys is not None:
                status = getattr(subsys, "status", "healthy")
                if status in ("unknown", "offline"):
                    flags.append(f"vm_offline_{subsys_name}")

        return flags

    # ------------------------------------------------------------------
    # Internal: posture computation
    # ------------------------------------------------------------------

    def _compute_posture(
        self,
        tod: str,
        day_type: str,
        system_load: str,
        recent_activity: str,
        recent_failure_count: int,
        env_flags: List[str],
    ) -> Tuple[str, str]:
        """Determine recommended execution posture from all signals.

        Returns (posture, reason) tuple.

        Posture rules (evaluated in priority order):
            DEFENSIVE if: late_night + recent_failures > 2, or
                          system_load == "overloaded"
            CAUTIOUS  if: weekend, or evening + high activity, or
                          any environmental_flags present
            AGGRESSIVE if: morning + weekday + idle system + no recent failures
            NORMAL    otherwise
        """
        # DEFENSIVE conditions
        if tod == "late_night" and recent_failure_count > 2:
            return (
                "DEFENSIVE",
                f"late night with {recent_failure_count} recent failures -- "
                f"minimise risk",
            )
        if system_load == "overloaded":
            return (
                "DEFENSIVE",
                "system is overloaded -- defer non-critical operations",
            )

        # CAUTIOUS conditions
        if day_type == "weekend":
            return (
                "CAUTIOUS",
                "weekend -- slower incident response if issues arise",
            )
        if tod == "evening" and recent_activity == "intense":
            return (
                "CAUTIOUS",
                "evening with intense activity -- risk of fatigue-induced errors",
            )
        if env_flags:
            return (
                "CAUTIOUS",
                f"environmental concerns: {', '.join(env_flags)}",
            )

        # AGGRESSIVE conditions
        if (
            tod == "morning"
            and day_type == "weekday"
            and system_load == "idle"
            and recent_failure_count == 0
        ):
            return (
                "AGGRESSIVE",
                "morning weekday, idle system, no recent failures -- "
                "optimal conditions for bold changes",
            )

        # NORMAL
        return (
            "NORMAL",
            f"{tod} {day_type}, {system_load} load, "
            f"{recent_failure_count} recent failure(s)",
        )

    def _count_recent_failures(
        self,
        now_ts: float,
        window_s: float,
    ) -> int:
        """Count operation failures in the given time window."""
        cutoff = now_ts - window_s
        return sum(1 for ts in self._recent_failures if ts > cutoff)

    # ------------------------------------------------------------------
    # Internal: timing advice
    # ------------------------------------------------------------------

    def _build_timing_advice(
        self,
        state: SituationalState,
        target_files: Tuple[str, ...],
        goal: str,
    ) -> str:
        """Build human-readable timing advice for a planned change."""
        parts: List[str] = []

        # Positive signals
        positive: List[str] = []
        negative: List[str] = []

        if state.time_of_day == "morning" and state.day_type == "weekday":
            positive.append("morning weekday")
        if state.system_load == "idle":
            positive.append("idle system")
        if state.recent_activity_level == "quiet":
            positive.append("quiet period")
        if not state.environmental_flags:
            positive.append("no environmental concerns")

        # Negative signals
        if state.time_of_day in ("late_night", "night"):
            negative.append(f"{state.time_of_day} session")
        if state.system_load in ("busy", "overloaded"):
            negative.append(f"{state.system_load} system")
        if state.recent_activity_level == "intense":
            negative.append("intense recent activity")
        if state.environmental_flags:
            negative.append(f"env flags: {', '.join(state.environmental_flags)}")

        if positive and not negative:
            parts.append(
                f"Good time for this change -- {', '.join(positive)}"
            )
        elif negative and not positive:
            parts.append(
                f"Consider deferring -- {', '.join(negative)}"
            )
        elif negative:
            parts.append(
                f"Mixed signals: positives=[{', '.join(positive)}], "
                f"concerns=[{', '.join(negative)}]"
            )
        else:
            parts.append("Neutral timing -- no strong signals either way")

        return ". ".join(parts)

    # ------------------------------------------------------------------
    # Internal: pattern detection
    # ------------------------------------------------------------------

    def _detect_temporal_patterns(self) -> None:
        """Analyse completed operations for recurring temporal patterns.

        Detects:
            - time_of_day: same failure occurring at similar hours
            - failure_cascade: multiple failures in rapid succession
            - peak_activity: unusually high operation density
        """
        now_mono = time.monotonic()
        now_ts = time.time()

        # --- Detect failure cascades ---
        # If 3+ failures in last 30 minutes, that's a cascade
        cascade_cutoff = now_ts - 1800.0
        recent_fails = [ts for ts in self._recent_failures if ts > cascade_cutoff]
        if len(recent_fails) >= 3:
            self._upsert_pattern(
                pattern_type="failure_cascade",
                description=(
                    f"{len(recent_fails)} failures in last 30 minutes"
                ),
                evidence_item=f"ts:{now_ts:.0f}",
                now_mono=now_mono,
            )

        # --- Detect peak activity ---
        activity_cutoff = now_ts - 3600.0
        recent_ops = sum(1 for ts in self._operation_timestamps if ts > activity_cutoff)
        if recent_ops > _ACTIVITY_ACTIVE_MAX * 2:
            self._upsert_pattern(
                pattern_type="peak_activity",
                description=f"{recent_ops} operations in last hour (unusually high)",
                evidence_item=f"count:{recent_ops}@{now_ts:.0f}",
                now_mono=now_mono,
            )

        # --- Detect time-of-day failure patterns ---
        # Check if failures cluster at the current hour
        current_hour = datetime.now().hour
        completed_ops = [
            rec for rec in self._operations.values()
            if rec.ended_at is not None and rec.success is False
        ]
        hour_failures: Dict[int, int] = {}
        for rec in completed_ops:
            if rec.ended_at is not None:
                op_hour = datetime.fromtimestamp(rec.ended_at).hour
                hour_failures[op_hour] = hour_failures.get(op_hour, 0) + 1

        if hour_failures.get(current_hour, 0) >= 3:
            bucket = _hour_to_bucket(current_hour)
            self._upsert_pattern(
                pattern_type="time_of_day",
                description=(
                    f"recurring failures during {bucket} "
                    f"(hour {current_hour}, "
                    f"{hour_failures[current_hour]} occurrences)"
                ),
                evidence_item=f"hour:{current_hour}@{now_ts:.0f}",
                now_mono=now_mono,
            )

    def _upsert_pattern(
        self,
        pattern_type: str,
        description: str,
        evidence_item: str,
        now_mono: float,
    ) -> None:
        """Insert or update a temporal pattern by type and description."""
        for p in self._temporal_patterns:
            if p.pattern_type == pattern_type and p.description == description:
                if evidence_item not in p.evidence:
                    p.evidence.append(evidence_item)
                p.last_seen = now_mono
                p.occurrence_count += 1
                # Confidence grows with observations, capped at 0.95
                p.confidence = min(
                    0.4 + 0.05 * p.occurrence_count, 0.95
                )
                return

        self._temporal_patterns.append(
            TemporalPattern(
                pattern_id=str(uuid.uuid4()),
                pattern_type=pattern_type,
                description=description,
                evidence=[evidence_item],
                confidence=0.4,
                first_seen=now_mono,
                last_seen=now_mono,
                occurrence_count=1,
            )
        )

    def _detect_causal_chains(self, completed: _OperationRecord) -> None:
        """Check if a completed operation is causally related to another.

        A causal chain is detected when two operations share target files
        and the second started after the first, within the chain window.
        """
        if completed.ended_at is None:
            return

        for op_id, other in self._operations.items():
            if op_id == completed.op_id:
                continue
            if other.ended_at is None:
                continue  # still in-flight

            # Check temporal ordering: did 'other' end before 'completed' started?
            if other.ended_at > completed.started_at:
                continue

            # Check if within chain window
            span = completed.started_at - other.ended_at
            if span > _CHAIN_MAX_SPAN_S:
                continue

            # Check file overlap
            overlap = set(completed.target_files) & set(other.target_files)
            if not overlap:
                continue

            # We have a causal relationship
            trigger = f"op:{other.op_id} files:{','.join(sorted(other.target_files))}"
            effect = (
                f"op:{completed.op_id} "
                f"{'succeeded' if completed.success else 'failed'} "
                f"after {span:.0f}s"
            )

            self.record_causal_observation(trigger, effect)

    # ------------------------------------------------------------------
    # Internal: expiry and pruning
    # ------------------------------------------------------------------

    def _expire_completed_chains(self) -> None:
        """Remove causal chains whose time span exceeds the max."""
        self._causal_chains = [
            c for c in self._causal_chains
            if c.time_span_s < _CHAIN_MAX_SPAN_S
        ]

    def _prune_old_patterns(self) -> None:
        """Remove temporal patterns older than the configured TTL."""
        now_mono = time.monotonic()
        ttl_s = _PATTERN_TTL_HOURS * 3600.0
        before = len(self._temporal_patterns)
        self._temporal_patterns = [
            p for p in self._temporal_patterns
            if (now_mono - p.last_seen) < ttl_s
        ]
        pruned = before - len(self._temporal_patterns)
        if pruned > 0:
            logger.debug("[SAI] Pruned %d stale temporal patterns", pruned)

    def _prune_completed_operations(self) -> None:
        """Remove completed operation records older than 24 hours."""
        now = time.time()
        cutoff = now - 86400.0
        stale_ids = [
            op_id
            for op_id, rec in self._operations.items()
            if rec.ended_at is not None and rec.ended_at < cutoff
        ]
        for op_id in stale_ids:
            del self._operations[op_id]

    # ------------------------------------------------------------------
    # Internal: pattern relevance
    # ------------------------------------------------------------------

    def _find_relevant_patterns(
        self,
        tod: str,
        day_type: str,
    ) -> List[TemporalPattern]:
        """Find patterns relevant to the current time and day."""
        relevant: List[TemporalPattern] = []
        for p in self._temporal_patterns:
            if p.confidence < 0.4:
                continue
            if p.pattern_type == "time_of_day" and tod in p.description.lower():
                relevant.append(p)
            elif p.pattern_type == "day_of_week" and day_type in p.description.lower():
                relevant.append(p)
            elif p.pattern_type == "failure_cascade":
                relevant.append(p)
            elif p.pattern_type == "peak_activity":
                relevant.append(p)
        return relevant

    # ------------------------------------------------------------------
    # Internal: assessment confidence
    # ------------------------------------------------------------------

    def _compute_assessment_confidence(
        self,
        state: SituationalState,
        involved_chains: List[CausalChain],
    ) -> float:
        """Compute confidence in the situational assessment.

        Higher confidence when more signals are available:
            - HealthCortex snapshot available: +0.25
            - Operation history exists: +0.25
            - Temporal patterns with high confidence: +0.25
            - Causal chain data: +0.25
        """
        confidence = 0.0

        # HealthCortex signal
        try:
            snapshot = self._cortex.get_snapshot()
            if snapshot is not None:
                confidence += 0.25
        except Exception:
            pass

        # Operation history
        if self._operation_timestamps:
            confidence += 0.25

        # Temporal patterns
        high_conf_patterns = [
            p for p in self._temporal_patterns if p.confidence >= 0.6
        ]
        if high_conf_patterns:
            confidence += 0.25

        # Causal chains
        if involved_chains or self._causal_chains:
            confidence += 0.25

        return min(confidence, 1.0)

    # ------------------------------------------------------------------
    # Persistence: load
    # ------------------------------------------------------------------

    async def _load_state(self) -> None:
        """Load temporal patterns and causal chains from disk."""
        loop = asyncio.get_event_loop()
        await asyncio.gather(
            loop.run_in_executor(None, self._load_patterns),
            loop.run_in_executor(None, self._load_chains),
        )

    def _load_patterns(self) -> None:
        """Synchronous load of temporal patterns from JSON."""
        path = self._persistence_dir / _PATTERNS_FILENAME
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                logger.warning("[SAI] Patterns file is not a list, skipping")
                return
            loaded = 0
            for entry in data:
                try:
                    self._temporal_patterns.append(
                        TemporalPattern(
                            pattern_id=entry["pattern_id"],
                            pattern_type=entry["pattern_type"],
                            description=entry["description"],
                            evidence=list(entry.get("evidence", [])),
                            confidence=float(entry["confidence"]),
                            first_seen=float(entry["first_seen"]),
                            last_seen=float(entry["last_seen"]),
                            occurrence_count=int(entry["occurrence_count"]),
                        )
                    )
                    loaded += 1
                except (KeyError, TypeError, ValueError) as exc:
                    logger.debug("[SAI] Skipping corrupt pattern entry: %s", exc)
            logger.info("[SAI] Loaded %d temporal patterns from %s", loaded, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[SAI] Failed to load patterns: %s", exc)

    def _load_chains(self) -> None:
        """Synchronous load of causal chains from JSON."""
        path = self._persistence_dir / _CHAINS_FILENAME
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                logger.warning("[SAI] Chains file is not a list, skipping")
                return
            loaded = 0
            for entry in data:
                try:
                    self._causal_chains.append(
                        CausalChain(
                            chain_id=entry["chain_id"],
                            trigger=entry["trigger"],
                            effects=list(entry.get("effects", [])),
                            time_span_s=float(entry["time_span_s"]),
                            confidence=float(entry["confidence"]),
                            observed_count=int(entry["observed_count"]),
                        )
                    )
                    loaded += 1
                except (KeyError, TypeError, ValueError) as exc:
                    logger.debug("[SAI] Skipping corrupt chain entry: %s", exc)
            logger.info("[SAI] Loaded %d causal chains from %s", loaded, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[SAI] Failed to load chains: %s", exc)

    # ------------------------------------------------------------------
    # Persistence: flush
    # ------------------------------------------------------------------

    async def _flush_state(self) -> None:
        """Persist temporal patterns and causal chains to disk."""
        loop = asyncio.get_event_loop()
        await asyncio.gather(
            loop.run_in_executor(None, self._flush_patterns),
            loop.run_in_executor(None, self._flush_chains),
        )

    def _flush_patterns(self) -> None:
        """Synchronous flush of temporal patterns to JSON."""
        path = self._persistence_dir / _PATTERNS_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "pattern_id": p.pattern_id,
                    "pattern_type": p.pattern_type,
                    "description": p.description,
                    "evidence": p.evidence,
                    "confidence": p.confidence,
                    "first_seen": p.first_seen,
                    "last_seen": p.last_seen,
                    "occurrence_count": p.occurrence_count,
                }
                for p in self._temporal_patterns
            ]
            path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            logger.debug(
                "[SAI] Flushed %d temporal patterns to %s",
                len(data),
                path,
            )
        except OSError as exc:
            logger.warning("[SAI] Failed to flush patterns: %s", exc)

    def _flush_chains(self) -> None:
        """Synchronous flush of causal chains to JSON."""
        path = self._persistence_dir / _CHAINS_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "chain_id": c.chain_id,
                    "trigger": c.trigger,
                    "effects": c.effects,
                    "time_span_s": c.time_span_s,
                    "confidence": c.confidence,
                    "observed_count": c.observed_count,
                }
                for c in self._causal_chains
            ]
            path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            logger.debug(
                "[SAI] Flushed %d causal chains to %s",
                len(data),
                path,
            )
        except OSError as exc:
            logger.warning("[SAI] Failed to flush chains: %s", exc)


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions -- no side effects)
# ---------------------------------------------------------------------------


def _hour_to_bucket(hour: int) -> str:
    """Map an hour (0-23) to a time-of-day bucket string."""
    for start, end, label in _TIME_BUCKETS:
        if start <= hour <= end:
            return label
    return "night"  # fallback (should not reach)
