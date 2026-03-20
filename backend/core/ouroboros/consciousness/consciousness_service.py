"""backend/core/ouroboros/consciousness/consciousness_service.py

TrinityConsciousness — Zone 6.11 self-awareness layer composing 4 engines.

Design:
    - Composes HealthCortex, MemoryEngine, DreamEngine, and ProphecyEngine into
      a unified service with a deterministic startup/shutdown lifecycle.
    - Startup is phased:
        Phase 1 (parallel): HealthCortex + MemoryEngine (foundation, no GPU)
        Phase 2 (parallel): DreamEngine + ProphecyEngine (GPU-optional)
        Phase 3: Optional morning briefing via safe_say
    - Shutdown is reverse-phased: GPU engines first, then foundation.
    - Cross-engine integrations:
        * get_memory_for_planner (TC19): feeds MemoryInsight list to IterationPlanner
          by filtering for fragile files and querying memory.
        * detect_regression (TC28): combines ProphecyEngine risk analysis with
          MemoryEngine file reputation to elevate risk when historical failure
          rate is poor.
    - Morning briefing (TC25): composes a natural-language summary from
      HealthCortex snapshot, DreamEngine blueprints, and MemoryEngine patterns.
    - All engine stop failures are caught so a single engine crash never
      prevents the others from flushing state (TC33).
    - start() is idempotent; second call is a no-op.

Thread-safety:
    All public methods are designed for single-event-loop usage.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.consciousness.types import (
    MemoryInsight,
    ProphecyReport,
)

logger = logging.getLogger(__name__)


class TrinityConsciousness:
    """Zone 6.11: Self-awareness layer composing 4 engines.

    Parameters
    ----------
    health_cortex:
        HealthCortex instance with ``start()``, ``stop()``, ``get_snapshot()``,
        ``get_trend()``.
    memory_engine:
        MemoryEngine instance with ``start()``, ``stop()``, ``ingest_outcome()``,
        ``query()``, ``get_file_reputation()``, ``get_pattern_summary()``.
    dream_engine:
        DreamEngine instance with ``start()``, ``stop()``, ``get_blueprints()``,
        ``get_blueprint()``, ``discard_stale()``.
    prophecy_engine:
        ProphecyEngine instance with ``start()``, ``stop()``,
        ``analyze_change()``, ``get_risk_scores()``.
    config:
        ConsciousnessConfig with feature flags and tuning knobs.
    comm:
        Optional CommProtocol instance for emitting intent/heartbeat messages.
    say_fn:
        Optional async callable for TTS announcements (e.g. safe_say).
    """

    def __init__(
        self,
        health_cortex: Any,
        memory_engine: Any,
        dream_engine: Any,
        prophecy_engine: Any,
        config: Any,
        comm: Any = None,
        say_fn: Any = None,
    ) -> None:
        self._cortex = health_cortex
        self._memory = memory_engine
        self._dream = dream_engine
        self._prophecy = prophecy_engine
        self._config = config
        self._comm = comm
        self._say_fn = say_fn
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all engines in phased order, then deliver morning briefing.

        Phase 1 (parallel): HealthCortex + MemoryEngine — foundation engines
        Phase 2 (parallel): DreamEngine + ProphecyEngine — GPU-optional engines
        Phase 3: Morning briefing via say_fn (best-effort, never blocks startup)

        Idempotent: second call is a no-op (TC26).
        """
        if self._running:
            return

        # Phase 1: foundation engines (no GPU needed)
        await asyncio.gather(
            self._cortex.start(),
            self._memory.start(),
        )

        # Phase 2: GPU-optional engines
        await asyncio.gather(
            self._dream.start(),
            self._prophecy.start(),
        )

        self._running = True

        # Phase 3: morning briefing (best-effort)
        if self._config.briefing_on_startup:
            try:
                await self._announce_briefing()
            except Exception as exc:
                logger.warning("Morning briefing failed: %s", exc)

    async def stop(self) -> None:
        """Stop all engines in reverse phase order.

        GPU engines (dream, prophecy) are stopped first, then foundation
        engines (memory, cortex).  Individual engine failures are caught
        so all engines get a chance to flush state (TC33).
        """
        self._running = False

        # Phase 2 engines first (GPU-optional)
        for engine in (self._dream, self._prophecy):
            try:
                await engine.stop()
            except Exception as exc:
                logger.warning(
                    "Engine %s stop failed: %s",
                    type(engine).__name__,
                    exc,
                )

        # Phase 1 engines (foundation)
        for engine in (self._memory, self._cortex):
            try:
                await engine.stop()
            except Exception as exc:
                logger.warning(
                    "Engine %s stop failed: %s",
                    type(engine).__name__,
                    exc,
                )

    # ------------------------------------------------------------------
    # Health composite
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return a composite health dict covering all engine statuses.

        Each engine reports one of:
            "active"   — running and feature-enabled
            "stopped"  — service not running
            "disabled" — feature flag off (dream_enabled / prophecy_enabled)
        """
        if not self._running:
            return {
                "running": False,
                "cortex": "stopped",
                "memory": "stopped",
                "dream": "disabled" if not self._config.dream_enabled else "stopped",
                "prophecy": "disabled" if not self._config.prophecy_enabled else "stopped",
            }
        return {
            "running": True,
            "cortex": "active",
            "memory": "active",
            "dream": "active" if self._config.dream_enabled else "disabled",
            "prophecy": "active" if self._config.prophecy_enabled else "disabled",
        }

    # ------------------------------------------------------------------
    # Morning briefing (TC25)
    # ------------------------------------------------------------------

    async def _announce_briefing(self) -> None:
        """Compose and announce a morning briefing via say_fn."""
        snapshot = self._cortex.get_snapshot()
        blueprints = self._dream.get_blueprints(top_n=3)
        patterns = self._memory.get_pattern_summary()

        briefing = self._compose_briefing(snapshot, blueprints, patterns)

        if self._say_fn is not None:
            try:
                await self._say_fn(briefing, source="consciousness")
            except Exception:
                logger.debug("say_fn failed for briefing")

    def _compose_briefing(
        self,
        snapshot: Any,
        blueprints: List[Any],
        patterns: Any,
    ) -> str:
        """Build a natural-language briefing string from engine outputs."""
        parts: List[str] = []

        # Health score
        if snapshot is not None:
            score_pct = int(snapshot.overall_score * 100)
            parts.append(f"Trinity health is {score_pct}%.")

        # Blueprints from dream engine
        if blueprints:
            parts.append(
                f"I pre-analyzed {len(blueprints)} improvement"
                f" {'opportunities' if len(blueprints) != 1 else 'opportunity'}."
            )
            parts.append(f"Top priority: {blueprints[0].title}")

        # Patterns from memory engine
        if patterns is not None and patterns.active_insights > 0:
            parts.append(
                f"{patterns.active_insights} active insights in memory."
            )

        return " ".join(parts)

    # ------------------------------------------------------------------
    # Cross-engine integration: Memory -> Planner (TC19)
    # ------------------------------------------------------------------

    def get_memory_for_planner(
        self, file_paths: Tuple[str, ...]
    ) -> List[MemoryInsight]:
        """Feed memory insights to IterationPlanner for fragile files.

        For each file with fragility_score > 0.5, queries the MemoryEngine
        for relevant historical insights and returns the combined list.

        Parameters
        ----------
        file_paths:
            Tuple of repo-relative file paths to check.

        Returns
        -------
        List[MemoryInsight]
            Insights relevant to the fragile files.  Empty list if no
            fragile files are found.
        """
        insights: List[MemoryInsight] = []
        for f in file_paths:
            rep = self._memory.get_file_reputation(f)
            if rep is not None and rep.fragility_score > 0.5:
                results = self._memory.query(f, max_results=3)
                insights.extend(results)
        return insights

    # ------------------------------------------------------------------
    # Cross-engine integration: Memory + Prophecy -> Regression (TC28)
    # ------------------------------------------------------------------

    async def detect_regression(
        self, files_changed: List[str]
    ) -> Optional[ProphecyReport]:
        """Cross-engine regression detection combining prophecy and memory.

        Runs ProphecyEngine.analyze_change() for the base risk assessment,
        then enriches with MemoryEngine file reputation.  If any changed file
        has a historical success_rate < 0.5, the report risk_level is elevated
        to "high" (unless already "high" or "critical").

        Parameters
        ----------
        files_changed:
            Repo-relative paths of files in the proposed change.

        Returns
        -------
        ProphecyReport
            The (possibly elevated) risk report.
        """
        report = await self._prophecy.analyze_change(files_changed)

        # Enrich with memory reputation
        for f in files_changed:
            rep = self._memory.get_file_reputation(f)
            if rep is not None and rep.success_rate < 0.5:
                # This file historically fails — boost risk
                if report.risk_level in ("low", "medium"):
                    return ProphecyReport(
                        change_id=report.change_id,
                        risk_level="high",
                        predicted_failures=report.predicted_failures,
                        confidence=min(report.confidence + 0.1, 0.6),
                        reasoning=(
                            report.reasoning
                            + f" [memory: {f} has {rep.success_rate:.0%} success rate]"
                        ),
                        recommended_tests=report.recommended_tests,
                    )

        return report
