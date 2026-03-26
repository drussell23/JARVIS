"""ConsciousnessBridge — connects TrinityConsciousness to the governance pipeline.

Wires Zone 6.11 (Consciousness) into Zone 6.8 (GLS) and Zone 6.12 (ProactiveDrive)
without either system importing the other directly.

Integration points:
    1. CLASSIFY phase: detect_regression() feeds risk elevation into classification
    2. GENERATE retry: get_memory_for_planner() feeds fragile-file insights into context
    3. ProactiveDrive: HealthCortex health() gates exploration eligibility
    4. Post-operation: MemoryEngine.ingest_outcome() records op results for future use

All methods are safe to call with None consciousness (graceful no-op).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ConsciousnessBridge:
    """Thin bridge between TrinityConsciousness and the governance pipeline.

    Injected into GLS and ProactiveDriveService. All methods are no-ops
    when consciousness is None (Zone 6.11 disabled or failed to start).
    """

    def __init__(self, consciousness: Any = None) -> None:
        self._consciousness = consciousness

    @property
    def is_active(self) -> bool:
        return self._consciousness is not None

    # ------------------------------------------------------------------
    # Integration 1: CLASSIFY — regression risk elevation
    # ------------------------------------------------------------------

    async def assess_regression_risk(
        self, files_changed: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Ask ProphecyEngine + MemoryEngine if this change is risky.

        Returns a dict with risk_level, confidence, reasoning, recommended_tests.
        Returns None if consciousness is unavailable.
        """
        if self._consciousness is None:
            return None
        try:
            report = await self._consciousness.detect_regression(files_changed)
            if report is None:
                return None
            return {
                "risk_level": report.risk_level,
                "confidence": report.confidence,
                "reasoning": report.reasoning,
                "recommended_tests": list(report.recommended_tests),
                "predicted_failures": list(report.predicted_failures),
            }
        except Exception as exc:
            logger.debug("[ConsciousnessBridge] assess_regression_risk error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Integration 2: GENERATE retry — fragile file memory injection
    # ------------------------------------------------------------------

    def get_fragile_file_context(
        self, file_paths: Tuple[str, ...],
    ) -> str:
        """Get memory insights for fragile files, formatted for prompt injection.

        Returns empty string if consciousness is unavailable or no insights found.
        """
        if self._consciousness is None:
            return ""
        try:
            insights = self._consciousness.get_memory_for_planner(file_paths)
            if not insights:
                return ""

            lines = ["## Consciousness Memory: Fragile File History", ""]
            for insight in insights:
                lines.append(f"- **{insight.file_path}**: {insight.summary}")
                if hasattr(insight, "recommendation") and insight.recommendation:
                    lines.append(f"  Recommendation: {insight.recommendation}")
            lines.append("")
            lines.append(
                "These files have historically been fragile. "
                "Take extra care with changes to them."
            )
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("[ConsciousnessBridge] get_fragile_file_context error: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Integration 3: ProactiveDrive — health gating
    # ------------------------------------------------------------------

    def is_system_healthy_for_exploration(self) -> Tuple[bool, str]:
        """Check if consciousness reports healthy enough for proactive exploration.

        Returns (healthy, reason). If consciousness is unavailable, defaults
        to True (don't block exploration because consciousness is down).
        """
        if self._consciousness is None:
            return True, "consciousness unavailable — defaulting to healthy"
        try:
            health = self._consciousness.health()
            running = health.get("running", False)
            if not running:
                return False, "consciousness not running"

            # Check individual engines
            cortex_ok = health.get("cortex", {}).get("status") != "failed"
            memory_ok = health.get("memory", {}).get("status") != "failed"
            if not cortex_ok:
                return False, "health cortex failed — system may be degraded"
            if not memory_ok:
                return False, "memory engine failed — cannot assess file history"

            return True, "consciousness healthy"
        except Exception as exc:
            logger.debug("[ConsciousnessBridge] health check error: %s", exc)
            return True, f"consciousness health check failed: {exc}"

    # ------------------------------------------------------------------
    # Integration 4: Post-operation — feed results back to memory
    # ------------------------------------------------------------------

    async def record_operation_outcome(
        self,
        op_id: str,
        files_changed: List[str],
        success: bool,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Record operation outcome in MemoryEngine for future regression detection.

        Called by GLS after operation completes (success or failure).
        """
        if self._consciousness is None:
            return
        try:
            await self._consciousness._memory.ingest_outcome(
                op_id=op_id,
                files=files_changed,
                success=success,
                failure_reason=failure_reason,
            )
            logger.debug(
                "[ConsciousnessBridge] Recorded outcome for %s: success=%s",
                op_id, success,
            )
        except Exception as exc:
            logger.debug("[ConsciousnessBridge] record_operation_outcome error: %s", exc)

    # ------------------------------------------------------------------
    # Integration 5: UAE — Unified Awareness for pipeline decisions
    # ------------------------------------------------------------------

    def get_unified_awareness(self) -> Optional[Any]:
        """Get the current unified awareness state (fuses CAI+SAI+all engines).

        Returns UnifiedAwarenessState or None if consciousness/UAE unavailable.
        """
        if self._consciousness is None:
            return None
        try:
            return self._consciousness.get_unified_awareness()
        except Exception:
            return None

    async def assess_operation_awareness(
        self, target_files: Tuple[str, ...], goal: str,
    ) -> Optional[Any]:
        """Get operation-specific awareness from UAE.

        Returns OperationAwareness with unified risk, confidence,
        suggested provider tier, thinking budget, and prompt injection text.
        Returns None if consciousness/UAE unavailable.
        """
        if self._consciousness is None:
            return None
        try:
            return await self._consciousness.assess_operation_awareness(
                target_files, goal,
            )
        except Exception as exc:
            logger.debug("[ConsciousnessBridge] assess_operation_awareness error: %s", exc)
            return None
