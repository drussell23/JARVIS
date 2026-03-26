"""backend/core/ouroboros/consciousness/unified_awareness.py

Unified Awareness Engine (UAE) — Shared Cognitive Bus for Trinity Consciousness
=================================================================================

Fuses CAI (Contextual Awareness) and SAI (Situational Awareness) with signals
from HealthCortex, MemoryEngine, DreamEngine, and ProphecyEngine into a single
unified awareness state.  Every pipeline decision-maker (IntentClassifier,
BrainSelector, IterationPlanner) queries UAE for a holistic picture rather
than pulling from individual engines.

UAE is a **signal fusion layer**, not a decision-maker.  It aggregates,
normalises, and formats awareness context.  The downstream components
(classifier, selector, planner) decide what to do with it.

Boundary Principle
------------------
Deterministic:
    - Awareness level computation (enum from thresholds)
    - Risk posture voting (deterministic fusion of CAI + SAI posture)
    - Composite confidence (weighted average, pure arithmetic)
    - Journal append and rotation (size-based, no randomness)

Agentic:
    - None.  UAE never calls an LLM.  It never generates code.  It never
      modifies code.  It is purely a read-aggregate-format layer.

Environment Variables
---------------------
JARVIS_UAE_FUSION_INTERVAL_S    float   30.0    Background fusion loop tick
JARVIS_UAE_JOURNAL_MAX_BYTES    int     10MB    Journal rotation threshold
JARVIS_UAE_CONFIDENCE_CAI_W     float   0.5     CAI weight in composite confidence
JARVIS_UAE_CONFIDENCE_SAI_W     float   0.5     SAI weight in composite confidence
JARVIS_UAE_ENABLED              bool    true    Kill-switch for background loop

Thread-safety:
    All mutable state is only touched inside the single asyncio event loop.
    Public accessors return snapshots (frozen dataclasses or copies).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.UAE")

# ---------------------------------------------------------------------------
# Constants (all overridable via env)
# ---------------------------------------------------------------------------

_FUSION_INTERVAL_S: float = float(os.getenv("JARVIS_UAE_FUSION_INTERVAL_S", "30.0"))
_JOURNAL_MAX_BYTES: int = int(os.getenv("JARVIS_UAE_JOURNAL_MAX_BYTES", str(10 * 1024 * 1024)))
_CONFIDENCE_CAI_WEIGHT: float = float(os.getenv("JARVIS_UAE_CONFIDENCE_CAI_W", "0.5"))
_CONFIDENCE_SAI_WEIGHT: float = float(os.getenv("JARVIS_UAE_CONFIDENCE_SAI_W", "0.5"))
_ENABLED: bool = os.getenv("JARVIS_UAE_ENABLED", "true").lower().strip() in ("1", "true", "yes", "on")

_DEFAULT_PERSISTENCE_DIR = Path.home() / ".jarvis" / "ouroboros" / "consciousness" / "uae"
_JOURNAL_FILENAME = "uae_journal.jsonl"
_STATE_CACHE_FILENAME = "uae_last_state.json"

# Risk posture priority for fusion voting (higher index = more conservative)
_POSTURE_SEVERITY: Dict[str, int] = {
    "AGGRESSIVE": 0,
    "NORMAL": 1,
    "CAUTIOUS": 2,
    "DEFENSIVE": 3,
}

# Risk level severity for unification (max-wins)
_RISK_LEVEL_SEVERITY: Dict[str, int] = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "CRITICAL": 3,
}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AwarenessLevel(Enum):
    """Graduated awareness levels for the unified awareness state.

    Each level represents an increasing degree of system vigilance:
    DORMANT    - System idle, no active operations.
    OBSERVING  - Passive monitoring, low activity.
    ATTENTIVE  - Active operations, normal awareness.
    FOCUSED    - Complex or risky operation, heightened awareness.
    HYPERAWARE - Emergency or cascading failure, maximum awareness.
    """

    DORMANT = "DORMANT"
    OBSERVING = "OBSERVING"
    ATTENTIVE = "ATTENTIVE"
    FOCUSED = "FOCUSED"
    HYPERAWARE = "HYPERAWARE"


# Ordered from least to most alert — used for transition detection.
_AWARENESS_ORDER: Dict[AwarenessLevel, int] = {
    AwarenessLevel.DORMANT: 0,
    AwarenessLevel.OBSERVING: 1,
    AwarenessLevel.ATTENTIVE: 2,
    AwarenessLevel.FOCUSED: 3,
    AwarenessLevel.HYPERAWARE: 4,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedAwarenessState:
    """Point-in-time holistic awareness snapshot.

    This is the canonical output of ``get_unified_state()``.  It aggregates
    signals from every consciousness engine into a single coherent picture.
    Frozen so it is safe to cache, pass across async boundaries, and hash.

    Fields
    ------
    awareness_level:
        Current graduated awareness level.
    contextual:
        Raw ContextualAwarenessState from CAI (opaque Any to avoid coupling).
    situational:
        Raw SituationalState from SAI (opaque Any to avoid coupling).
    health_verdict:
        Overall health verdict from HealthCortex: HEALTHY | DEGRADED | CRITICAL.
    active_predictions:
        Count of file-risk entries from ProphecyEngine's last analysis.
    memory_insights_available:
        Count of non-expired insights in MemoryEngine.
    dream_blueprints_available:
        Count of fresh ImprovementBlueprints from DreamEngine.
    composite_confidence:
        Weighted fusion of CAI and SAI confidence scores (0.0-1.0).
    risk_posture:
        Unified recommendation: AGGRESSIVE | NORMAL | CAUTIOUS | DEFENSIVE.
    awareness_summary:
        One-sentence human-readable summary combining all signals.
    timestamp:
        Unix epoch float when this snapshot was computed.
    """

    awareness_level: AwarenessLevel
    contextual: Optional[Any]
    situational: Optional[Any]
    health_verdict: str
    active_predictions: int
    memory_insights_available: int
    dream_blueprints_available: int
    composite_confidence: float
    risk_posture: str
    awareness_summary: str
    timestamp: float


@dataclass(frozen=True)
class AwarenessJournalEntry:
    """Persistent record of an awareness state transition.

    Written to the append-only JSONL journal for retrospective analysis.

    Fields
    ------
    entry_id:
        UUID for this journal entry.
    awareness_level:
        The level after the transition.
    risk_posture:
        The posture after the transition.
    trigger:
        What caused this awareness state change (e.g. "fusion_loop",
        "health_transition_DEGRADED", "operation_assess").
    key_signals:
        The signals that drove the decision (e.g. ["health=CRITICAL",
        "active_ops=3", "cai_confidence=0.72"]).
    timestamp:
        Unix epoch float when the transition occurred.
    """

    entry_id: str
    awareness_level: str
    risk_posture: str
    trigger: str
    key_signals: Tuple[str, ...]
    timestamp: float


@dataclass(frozen=True)
class OperationAwareness:
    """Per-operation awareness assessment combining CAI and SAI signals.

    This is the output of ``assess_for_operation()`` — a targeted awareness
    context for a specific set of files and a stated goal, consumed by the
    governance pipeline (IntentClassifier, BrainSelector, providers).

    Fields
    ------
    target_files:
        The files being assessed.
    goal:
        The stated goal for the operation.
    context_assessment:
        ContextAssessment from CAI (opaque Any).
    situation_assessment:
        SituationAssessment from SAI (opaque Any).
    unified_risk_level:
        Maximum of CAI and SAI risk levels: LOW | MEDIUM | HIGH | CRITICAL.
    unified_confidence:
        Minimum of CAI and SAI confidence (conservative fusion).
    suggested_provider_tier:
        "tier0" (most capable) | "tier1" | "tier2" (lightest).
    suggested_thinking_budget:
        "minimal" | "standard" | "extended".
    awareness_prompt_injection:
        Formatted text combining both assessments, ready for generation prompt.
    timestamp:
        Unix epoch float when this assessment was produced.
    """

    target_files: Tuple[str, ...]
    goal: str
    context_assessment: Optional[Any]
    situation_assessment: Optional[Any]
    unified_risk_level: str
    unified_confidence: float
    suggested_provider_tier: str
    suggested_thinking_budget: str
    awareness_prompt_injection: str
    timestamp: float


# ---------------------------------------------------------------------------
# Internal helpers (pure functions)
# ---------------------------------------------------------------------------


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely get an attribute from an engine that may be None or missing."""
    if obj is None:
        return default
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _safe_call(obj: Any, method: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Safely call a method on an engine, returning default on any failure."""
    if obj is None:
        return default
    fn = getattr(obj, method, None)
    if fn is None or not callable(fn):
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


async def _safe_async_call(obj: Any, method: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Safely call an async method on an engine, returning default on any failure."""
    if obj is None:
        return default
    fn = getattr(obj, method, None)
    if fn is None or not callable(fn):
        return default
    try:
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
    except Exception:
        return default


def _extract_confidence(state: Any, attr: str = "confidence") -> float:
    """Extract a confidence value from a state object, defaulting to 0.0."""
    if state is None:
        return 0.0
    val = _safe_getattr(state, attr, 0.0)
    if isinstance(val, (int, float)):
        return float(max(0.0, min(1.0, val)))
    return 0.0


def _extract_posture(state: Any, attr: str = "recommended_posture") -> str:
    """Extract a risk posture string from a state object."""
    if state is None:
        return "NORMAL"
    val = _safe_getattr(state, attr, "NORMAL")
    if isinstance(val, str) and val.upper() in _POSTURE_SEVERITY:
        return val.upper()
    return "NORMAL"


def _extract_risk_level(assessment: Any, attr: str = "risk_level") -> str:
    """Extract a risk level string from an assessment object."""
    if assessment is None:
        return "LOW"
    val = _safe_getattr(assessment, attr, "LOW")
    if isinstance(val, str) and val.upper() in _RISK_LEVEL_SEVERITY:
        return val.upper()
    # Handle enum values
    if hasattr(val, "value"):
        s = str(val.value).upper()
        if s in _RISK_LEVEL_SEVERITY:
            return s
    return "LOW"


def _extract_complexity(assessment: Any, attr: str = "complexity_estimate") -> str:
    """Extract complexity estimate from a CAI assessment."""
    if assessment is None:
        return "low"
    val = _safe_getattr(assessment, attr, "low")
    return str(val).lower() if val else "low"


def _fuse_postures(cai_posture: str, sai_posture: str) -> str:
    """Fuse two risk postures using conservative voting.

    Rules:
    - If either says DEFENSIVE -> DEFENSIVE
    - If either says CAUTIOUS and other doesn't say AGGRESSIVE -> CAUTIOUS
    - If both say AGGRESSIVE -> AGGRESSIVE
    - Else NORMAL
    """
    cai = cai_posture.upper()
    sai = sai_posture.upper()

    if cai == "DEFENSIVE" or sai == "DEFENSIVE":
        return "DEFENSIVE"
    if cai == "CAUTIOUS" or sai == "CAUTIOUS":
        if cai != "AGGRESSIVE" or sai != "AGGRESSIVE":
            return "CAUTIOUS"
    if cai == "AGGRESSIVE" and sai == "AGGRESSIVE":
        return "AGGRESSIVE"
    return "NORMAL"


def _fuse_risk_levels(level_a: str, level_b: str) -> str:
    """Return the higher (more severe) of two risk levels."""
    sev_a = _RISK_LEVEL_SEVERITY.get(level_a.upper(), 0)
    sev_b = _RISK_LEVEL_SEVERITY.get(level_b.upper(), 0)
    if sev_a >= sev_b:
        return level_a.upper()
    return level_b.upper()


def _compute_awareness_level(
    *,
    health_verdict: str,
    active_ops: int,
    risk_posture: str,
    cai_state: Any,
    sai_state: Any,
) -> AwarenessLevel:
    """Determine the awareness level from aggregated signals.

    The computation is fully deterministic:
    - HYPERAWARE: health is CRITICAL or emergency protocols detected
    - FOCUSED: risk_posture is DEFENSIVE or CAUTIOUS with active ops
    - ATTENTIVE: active operations in progress
    - DORMANT: no active ops and system completely idle
    - OBSERVING: passive monitoring (default)
    """
    verdict_upper = health_verdict.upper() if isinstance(health_verdict, str) else "HEALTHY"

    # Emergency state detection
    if verdict_upper == "CRITICAL":
        return AwarenessLevel.HYPERAWARE

    # Check for emergency flags on SAI state
    sai_emergency = _safe_getattr(sai_state, "emergency_active", False)
    if sai_emergency:
        return AwarenessLevel.HYPERAWARE

    # Heightened awareness for conservative postures with active work
    posture_upper = risk_posture.upper() if isinstance(risk_posture, str) else "NORMAL"
    if posture_upper in ("DEFENSIVE", "CAUTIOUS") and active_ops > 0:
        return AwarenessLevel.FOCUSED

    # Degraded health with work in progress
    if verdict_upper == "DEGRADED" and active_ops > 0:
        return AwarenessLevel.FOCUSED

    # Active work at normal risk
    if active_ops > 0:
        return AwarenessLevel.ATTENTIVE

    # System idle detection
    cai_idle = _safe_getattr(cai_state, "is_idle", None)
    sai_idle = _safe_getattr(sai_state, "is_idle", None)
    if cai_idle is True and sai_idle is True:
        return AwarenessLevel.DORMANT

    # No active ops but not confirmed idle
    if active_ops == 0 and verdict_upper == "HEALTHY":
        return AwarenessLevel.DORMANT

    return AwarenessLevel.OBSERVING


def _build_awareness_summary(
    level: AwarenessLevel,
    posture: str,
    health_verdict: str,
    active_predictions: int,
    memory_insights: int,
    dream_blueprints: int,
    composite_confidence: float,
) -> str:
    """Build a one-sentence summary of the current awareness state."""
    parts: List[str] = []

    level_label = level.value.lower()
    parts.append(f"Awareness {level_label} with {posture.lower()} posture")

    factors: List[str] = []
    factors.append(f"health {health_verdict.lower()}")

    if active_predictions > 0:
        factors.append(f"{active_predictions} risk prediction{'s' if active_predictions != 1 else ''}")
    if memory_insights > 0:
        factors.append(f"{memory_insights} memory insight{'s' if memory_insights != 1 else ''}")
    if dream_blueprints > 0:
        factors.append(f"{dream_blueprints} dream blueprint{'s' if dream_blueprints != 1 else ''}")

    factors.append(f"confidence {composite_confidence:.0%}")

    parts.append("(" + ", ".join(factors) + ").")
    return " ".join(parts)


def _journal_entry_to_dict(entry: AwarenessJournalEntry) -> Dict[str, Any]:
    """Serialize a journal entry to a JSON-safe dict."""
    return {
        "entry_id": entry.entry_id,
        "awareness_level": entry.awareness_level,
        "risk_posture": entry.risk_posture,
        "trigger": entry.trigger,
        "key_signals": list(entry.key_signals),
        "timestamp": entry.timestamp,
    }


def _journal_entry_from_dict(d: Dict[str, Any]) -> AwarenessJournalEntry:
    """Deserialize a dict back to an AwarenessJournalEntry."""
    return AwarenessJournalEntry(
        entry_id=d.get("entry_id", str(uuid.uuid4())),
        awareness_level=d.get("awareness_level", "OBSERVING"),
        risk_posture=d.get("risk_posture", "NORMAL"),
        trigger=d.get("trigger", "restored"),
        key_signals=tuple(d.get("key_signals", ())),
        timestamp=float(d.get("timestamp", 0.0)),
    )


def _state_to_dict(state: UnifiedAwarenessState) -> Dict[str, Any]:
    """Serialize a UnifiedAwarenessState to a JSON-safe dict.

    Opaque engine states (contextual, situational) are stored as their
    string representations to avoid coupling to engine-internal types.
    """
    return {
        "awareness_level": state.awareness_level.value,
        "health_verdict": state.health_verdict,
        "active_predictions": state.active_predictions,
        "memory_insights_available": state.memory_insights_available,
        "dream_blueprints_available": state.dream_blueprints_available,
        "composite_confidence": state.composite_confidence,
        "risk_posture": state.risk_posture,
        "awareness_summary": state.awareness_summary,
        "timestamp": state.timestamp,
    }


def _suggest_provider_tier(complexity: str, risk_level: str) -> str:
    """Suggest a provider tier based on complexity and risk.

    Tier 0 (most capable): complex operations with high/critical risk.
    Tier 2 (lightest): simple operations with low risk.
    Tier 1: everything else.
    """
    risk_sev = _RISK_LEVEL_SEVERITY.get(risk_level.upper(), 0)
    is_complex = complexity in ("high", "very_high", "complex")

    if is_complex and risk_sev >= 2:
        return "tier0"
    if not is_complex and risk_sev == 0:
        return "tier2"
    return "tier1"


def _suggest_thinking_budget(awareness_level: AwarenessLevel) -> str:
    """Suggest a thinking budget based on the awareness level.

    FOCUSED / HYPERAWARE -> extended thinking to handle complexity.
    DORMANT             -> minimal (nothing happening).
    Everything else     -> standard.
    """
    if awareness_level in (AwarenessLevel.FOCUSED, AwarenessLevel.HYPERAWARE):
        return "extended"
    if awareness_level == AwarenessLevel.DORMANT:
        return "minimal"
    return "standard"


# ---------------------------------------------------------------------------
# UnifiedAwarenessEngine
# ---------------------------------------------------------------------------


class UnifiedAwarenessEngine:
    """Shared Cognitive Bus — fuses all consciousness engines into one signal.

    This is the primary interface for pipeline components that need holistic
    system awareness.  It queries CAI, SAI, HealthCortex, MemoryEngine,
    DreamEngine, and ProphecyEngine, fuses their signals, and returns a
    unified state that is deterministic and reproducible given the same inputs.

    UAE does NOT make decisions.  It provides context that IntentClassifier
    and BrainSelector consume to make decisions.

    Parameters
    ----------
    cai:
        ContextualAwarenessEngine instance (or any object with
        ``get_state()`` returning an object with ``confidence`` and
        ``recommended_posture`` attrs, and ``assess_context(files, goal)``).
    sai:
        SituationalAwarenessEngine instance (or any object with
        ``get_state()`` returning an object with ``confidence`` and
        ``recommended_posture`` attrs, and ``assess_situation(files, goal)``).
    health_cortex:
        HealthCortex instance (``get_snapshot()`` -> TrinityHealthSnapshot).
    memory_engine:
        MemoryEngine instance (``query(q, max_results)`` -> List[MemoryInsight]).
    dream_engine:
        DreamEngine instance (``get_blueprints(top_n)`` -> List[Blueprint]).
    prophecy_engine:
        ProphecyEngine instance (``get_risk_scores()`` -> Dict[str, float]).
    config:
        ConsciousnessConfig for feature flags and tuning.
    persistence_dir:
        Directory for journal and state cache files.
    comm:
        Optional CommProtocol for emitting heartbeat on level transitions.
    """

    def __init__(
        self,
        cai: Any,
        sai: Any,
        health_cortex: Any,
        memory_engine: Any,
        dream_engine: Any,
        prophecy_engine: Any,
        config: Any,
        persistence_dir: Optional[Path] = None,
        comm: Any = None,
    ) -> None:
        self._cai = cai
        self._sai = sai
        self._cortex = health_cortex
        self._memory = memory_engine
        self._dream = dream_engine
        self._prophecy = prophecy_engine
        self._config = config
        self._comm = comm

        self._persistence_dir: Path = persistence_dir or _DEFAULT_PERSISTENCE_DIR
        self._journal_path: Path = self._persistence_dir / _JOURNAL_FILENAME
        self._state_cache_path: Path = self._persistence_dir / _STATE_CACHE_FILENAME

        # Runtime state
        self._journal: List[AwarenessJournalEntry] = []
        self._last_state: Optional[UnifiedAwarenessState] = None
        self._previous_level: Optional[AwarenessLevel] = None
        self._fusion_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted journal and start the background fusion loop.

        Idempotent: second call is a no-op.
        """
        if self._running:
            return

        await self._load_journal()
        await self._load_state_cache()

        if _ENABLED:
            self._fusion_task = asyncio.create_task(
                self._fusion_loop(), name="uae_fusion_loop"
            )

        self._running = True
        logger.info(
            "[UAE] Started (interval=%.1fs, journal=%s)",
            _FUSION_INTERVAL_S,
            self._journal_path,
        )

    async def stop(self) -> None:
        """Cancel the fusion loop and persist state to disk."""
        self._running = False

        if self._fusion_task is not None and not self._fusion_task.done():
            self._fusion_task.cancel()
            try:
                await self._fusion_task
            except (asyncio.CancelledError, Exception):
                pass
            self._fusion_task = None

        await self._flush_journal()
        await self._flush_state_cache()
        logger.info("[UAE] Stopped.")

    # ------------------------------------------------------------------
    # Core fusion: THE primary API
    # ------------------------------------------------------------------

    def get_unified_state(self) -> UnifiedAwarenessState:
        """Return the most recently computed unified awareness state.

        This is the primary API.  It returns a cached snapshot — the fusion
        loop refreshes it every ``JARVIS_UAE_FUSION_INTERVAL_S`` seconds.

        Returns a default DORMANT state if the fusion loop has not yet
        produced its first snapshot.
        """
        if self._last_state is not None:
            return self._last_state

        # Bootstrap: return a safe default before first fusion tick
        return UnifiedAwarenessState(
            awareness_level=AwarenessLevel.DORMANT,
            contextual=None,
            situational=None,
            health_verdict="HEALTHY",
            active_predictions=0,
            memory_insights_available=0,
            dream_blueprints_available=0,
            composite_confidence=0.0,
            risk_posture="NORMAL",
            awareness_summary="Awareness dormant — awaiting first fusion cycle.",
            timestamp=time.time(),
        )

    async def compute_unified_state(self) -> UnifiedAwarenessState:
        """Compute a fresh unified state by polling all engines.

        Unlike ``get_unified_state()`` which returns a cached snapshot,
        this method actively queries every engine.  Use sparingly in
        hot paths — prefer the cached version.
        """
        now = time.time()

        # Pull CAI state
        cai_state = await _safe_async_call(self._cai, "get_state", default=None)

        # Pull SAI state
        sai_state = await _safe_async_call(self._sai, "get_state", default=None)

        # Pull HealthCortex snapshot
        health_snapshot = _safe_call(self._cortex, "get_snapshot", default=None)
        health_verdict = "HEALTHY"
        if health_snapshot is not None:
            health_verdict = _safe_getattr(health_snapshot, "overall_verdict", "HEALTHY")
            if not isinstance(health_verdict, str):
                health_verdict = "HEALTHY"

        # Count ProphecyEngine risk scores
        risk_scores = _safe_call(self._prophecy, "get_risk_scores", default={})
        active_predictions = len(risk_scores) if isinstance(risk_scores, dict) else 0

        # Count MemoryEngine insights
        memory_results = _safe_call(
            self._memory, "get_pattern_summary", default=None
        )
        memory_insights_available = 0
        if memory_results is not None:
            memory_insights_available = _safe_getattr(
                memory_results, "active_insights", 0
            )
            if not isinstance(memory_insights_available, int):
                memory_insights_available = 0

        # Count DreamEngine blueprints
        dream_blueprints_list = _safe_call(
            self._dream, "get_blueprints", default=[]
        )
        dream_blueprints_available = 0
        if isinstance(dream_blueprints_list, (list, tuple)):
            dream_blueprints_available = len(dream_blueprints_list)

        # Compute composite confidence
        cai_confidence = _extract_confidence(cai_state)
        sai_confidence = _extract_confidence(sai_state)
        total_weight = _CONFIDENCE_CAI_WEIGHT + _CONFIDENCE_SAI_WEIGHT
        if total_weight > 0:
            composite_confidence = (
                cai_confidence * _CONFIDENCE_CAI_WEIGHT
                + sai_confidence * _CONFIDENCE_SAI_WEIGHT
            ) / total_weight
        else:
            composite_confidence = 0.0

        # Compute risk posture
        cai_posture = _extract_posture(cai_state)
        sai_posture = _extract_posture(sai_state)
        risk_posture = _fuse_postures(cai_posture, sai_posture)

        # Compute awareness level
        awareness_level = _compute_awareness_level(
            health_verdict=health_verdict,
            active_ops=active_predictions,
            risk_posture=risk_posture,
            cai_state=cai_state,
            sai_state=sai_state,
        )

        # Build summary
        awareness_summary = _build_awareness_summary(
            level=awareness_level,
            posture=risk_posture,
            health_verdict=health_verdict,
            active_predictions=active_predictions,
            memory_insights=memory_insights_available,
            dream_blueprints=dream_blueprints_available,
            composite_confidence=composite_confidence,
        )

        state = UnifiedAwarenessState(
            awareness_level=awareness_level,
            contextual=cai_state,
            situational=sai_state,
            health_verdict=health_verdict,
            active_predictions=active_predictions,
            memory_insights_available=memory_insights_available,
            dream_blueprints_available=dream_blueprints_available,
            composite_confidence=composite_confidence,
            risk_posture=risk_posture,
            awareness_summary=awareness_summary,
            timestamp=now,
        )

        self._last_state = state
        return state

    # ------------------------------------------------------------------
    # Per-operation awareness assessment
    # ------------------------------------------------------------------

    async def assess_for_operation(
        self,
        target_files: Tuple[str, ...],
        goal: str,
    ) -> OperationAwareness:
        """Produce a targeted awareness context for a specific operation.

        Calls CAI.assess_context() and SAI.assess_situation(), fuses their
        signals, and returns an OperationAwareness with provider and budget
        suggestions plus a formatted prompt injection.

        Parameters
        ----------
        target_files:
            Repo-relative paths of files involved in the operation.
        goal:
            Human-readable goal statement for the operation.

        Returns
        -------
        OperationAwareness
            Unified assessment with suggestions and formatted text.
        """
        now = time.time()

        # Assess via CAI
        context_assessment = await _safe_async_call(
            self._cai, "assess_context", target_files, goal, default=None
        )

        # Assess via SAI
        situation_assessment = await _safe_async_call(
            self._sai, "assess_situation", target_files, goal, default=None
        )

        # Fuse risk levels (max-wins)
        cai_risk = _extract_risk_level(context_assessment)
        sai_risk = _extract_risk_level(situation_assessment)
        unified_risk_level = _fuse_risk_levels(cai_risk, sai_risk)

        # Fuse confidence (min-wins = conservative)
        cai_conf = _extract_confidence(context_assessment)
        sai_conf = _extract_confidence(situation_assessment)
        if cai_conf > 0 and sai_conf > 0:
            unified_confidence = min(cai_conf, sai_conf)
        elif cai_conf > 0:
            unified_confidence = cai_conf
        elif sai_conf > 0:
            unified_confidence = sai_conf
        else:
            unified_confidence = 0.0

        # Determine provider tier from complexity and risk
        complexity = _extract_complexity(context_assessment)
        suggested_provider_tier = _suggest_provider_tier(complexity, unified_risk_level)

        # Determine thinking budget from current awareness level
        current_state = self.get_unified_state()
        suggested_thinking_budget = _suggest_thinking_budget(current_state.awareness_level)

        # Build prompt injection text
        awareness_prompt_injection = self._format_prompt_injection(
            context_assessment=context_assessment,
            situation_assessment=situation_assessment,
            unified_risk_level=unified_risk_level,
            unified_confidence=unified_confidence,
            suggested_provider_tier=suggested_provider_tier,
            current_state=current_state,
        )

        awareness = OperationAwareness(
            target_files=target_files,
            goal=goal,
            context_assessment=context_assessment,
            situation_assessment=situation_assessment,
            unified_risk_level=unified_risk_level,
            unified_confidence=unified_confidence,
            suggested_provider_tier=suggested_provider_tier,
            suggested_thinking_budget=suggested_thinking_budget,
            awareness_prompt_injection=awareness_prompt_injection,
            timestamp=now,
        )

        return awareness

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def record_awareness_transition(self, trigger: str) -> None:
        """Record a state transition in the awareness journal.

        Called automatically by the fusion loop on level changes, but can
        also be called externally for manual triggers.

        Parameters
        ----------
        trigger:
            Description of what caused this awareness state change.
        """
        state = self.get_unified_state()

        key_signals: List[str] = [
            f"level={state.awareness_level.value}",
            f"posture={state.risk_posture}",
            f"health={state.health_verdict}",
            f"confidence={state.composite_confidence:.2f}",
            f"predictions={state.active_predictions}",
            f"insights={state.memory_insights_available}",
            f"blueprints={state.dream_blueprints_available}",
        ]

        entry = AwarenessJournalEntry(
            entry_id=str(uuid.uuid4()),
            awareness_level=state.awareness_level.value,
            risk_posture=state.risk_posture,
            trigger=trigger,
            key_signals=tuple(key_signals),
            timestamp=time.time(),
        )

        self._journal.append(entry)

    def get_journal(self, limit: int = 20) -> List[AwarenessJournalEntry]:
        """Return the most recent awareness journal entries.

        Parameters
        ----------
        limit:
            Maximum number of entries to return (most recent first).

        Returns
        -------
        List[AwarenessJournalEntry]
            Entries sorted newest-first, capped at ``limit``.
        """
        return list(reversed(self._journal[-limit:]))

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_for_prompt(self, awareness: OperationAwareness) -> str:
        """Format an OperationAwareness for injection into a generation prompt.

        Produces a structured Markdown block that can be prepended to a
        provider's system prompt to give the model full operational context.

        Parameters
        ----------
        awareness:
            The OperationAwareness to format.

        Returns
        -------
        str
            Markdown-formatted awareness context.
        """
        current_state = self.get_unified_state()
        return self._format_prompt_injection(
            context_assessment=awareness.context_assessment,
            situation_assessment=awareness.situation_assessment,
            unified_risk_level=awareness.unified_risk_level,
            unified_confidence=awareness.unified_confidence,
            suggested_provider_tier=awareness.suggested_provider_tier,
            current_state=current_state,
        )

    def format_for_voice(self, awareness: OperationAwareness) -> str:
        """Format an OperationAwareness for voice narration.

        Produces a concise one or two sentence summary suitable for TTS.

        Parameters
        ----------
        awareness:
            The OperationAwareness to format.

        Returns
        -------
        str
            Short voice-friendly summary.
        """
        parts: List[str] = []

        level = self.get_unified_state().awareness_level.value.lower()
        risk = awareness.unified_risk_level.lower()

        parts.append(f"Awareness is {level}.")

        if risk in ("high", "critical"):
            parts.append(f"Risk level {risk} detected.")

        tier_labels = {
            "tier0": "highest capability provider",
            "tier1": "standard provider",
            "tier2": "lightweight provider",
        }
        tier_label = tier_labels.get(awareness.suggested_provider_tier, "standard provider")
        parts.append(f"Suggesting {tier_label}.")

        return " ".join(parts)

    # ------------------------------------------------------------------
    # Background fusion loop
    # ------------------------------------------------------------------

    async def _fusion_loop(self) -> None:
        """Background task: compute unified state, detect transitions, journal.

        Runs every ``JARVIS_UAE_FUSION_INTERVAL_S`` seconds.  On each tick:
        1. Compute fresh unified state.
        2. Detect awareness level transitions.
        3. Record journal entry on transitions.
        4. Emit HEARTBEAT via CommProtocol on level changes.
        """
        while True:
            try:
                state = await self.compute_unified_state()

                # Detect level transition
                if self._previous_level is not None and state.awareness_level != self._previous_level:
                    old_level = self._previous_level.value
                    new_level = state.awareness_level.value
                    trigger = f"fusion_loop:{old_level}->{new_level}"

                    self.record_awareness_transition(trigger)

                    logger.info(
                        "[UAE] Awareness transition: %s -> %s (posture=%s, health=%s)",
                        old_level,
                        new_level,
                        state.risk_posture,
                        state.health_verdict,
                    )

                    # Emit heartbeat on transition
                    if self._comm is not None:
                        try:
                            progress_pct = _AWARENESS_ORDER.get(
                                state.awareness_level, 0
                            ) / max(len(_AWARENESS_ORDER) - 1, 1) * 100.0
                            await self._comm.emit_heartbeat(
                                op_id="uae_awareness",
                                phase=f"awareness:{new_level}",
                                progress_pct=progress_pct,
                            )
                        except Exception as exc:
                            logger.debug(
                                "[UAE] CommProtocol heartbeat failed: %s", exc
                            )

                self._previous_level = state.awareness_level

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[UAE] Unexpected error in fusion loop")

            try:
                await asyncio.sleep(_FUSION_INTERVAL_S)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Internal: prompt injection formatting
    # ------------------------------------------------------------------

    def _format_prompt_injection(
        self,
        *,
        context_assessment: Any,
        situation_assessment: Any,
        unified_risk_level: str,
        unified_confidence: float,
        suggested_provider_tier: str,
        current_state: UnifiedAwarenessState,
    ) -> str:
        """Build the Markdown prompt injection block.

        This is used by both ``assess_for_operation()`` and
        ``format_for_prompt()`` to avoid duplication.
        """
        lines: List[str] = []
        lines.append("## Unified Awareness Context")
        lines.append("")

        level = current_state.awareness_level.value
        posture = current_state.risk_posture
        lines.append(f"**Level:** {level} | **Posture:** {posture}")

        # Context summary from CAI
        ctx_summary = _safe_getattr(context_assessment, "summary", None)
        if ctx_summary and isinstance(ctx_summary, str):
            lines.append(f"**Context:** {ctx_summary}")
        else:
            # Try to build from attributes
            ctx_parts: List[str] = []
            hotspot_count = _safe_getattr(context_assessment, "hotspot_count", 0)
            if isinstance(hotspot_count, int) and hotspot_count > 0:
                ctx_parts.append(f"{hotspot_count} hotspot file{'s' if hotspot_count != 1 else ''}")
            coupling = _safe_getattr(context_assessment, "coupling_detected", None)
            if coupling:
                ctx_parts.append(str(coupling))
            if ctx_parts:
                lines.append(f"**Context:** {', '.join(ctx_parts)}")

        # Situation summary from SAI
        sit_summary = _safe_getattr(situation_assessment, "summary", None)
        if sit_summary and isinstance(sit_summary, str):
            lines.append(f"**Situation:** {sit_summary}")

        # Risk factors
        risk_factors: List[str] = []
        cai_risks = _safe_getattr(context_assessment, "risk_factors", None)
        if isinstance(cai_risks, (list, tuple)):
            risk_factors.extend(str(r) for r in cai_risks[:3])
        sai_risks = _safe_getattr(situation_assessment, "risk_factors", None)
        if isinstance(sai_risks, (list, tuple)):
            risk_factors.extend(str(r) for r in sai_risks[:3])
        if risk_factors:
            lines.append(f"**Risk factors:** {'; '.join(risk_factors)}")

        # Provider suggestion
        tier_labels = {
            "tier0": "Tier 0 (most capable model)",
            "tier1": "Tier 1 (J-Prime 7B)",
            "tier2": "Tier 2 (lightweight / cached)",
        }
        tier_label = tier_labels.get(suggested_provider_tier, suggested_provider_tier)
        lines.append(f"**Suggested provider:** {tier_label}")

        # Relevant insights
        insights: List[str] = []
        cai_insights = _safe_getattr(context_assessment, "insights", None)
        if isinstance(cai_insights, (list, tuple)):
            for ins in cai_insights[:4]:
                ins_str = str(ins) if not isinstance(ins, str) else ins
                insights.append(f"- {ins_str}")
        sai_insights = _safe_getattr(situation_assessment, "insights", None)
        if isinstance(sai_insights, (list, tuple)):
            for ins in sai_insights[:4]:
                ins_str = str(ins) if not isinstance(ins, str) else ins
                insights.append(f"- {ins_str}")

        if insights:
            lines.append("")
            lines.append("Relevant insights:")
            lines.extend(insights[:6])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence: Journal (append-only JSONL)
    # ------------------------------------------------------------------

    async def _load_journal(self) -> None:
        """Load journal entries from disk (best-effort)."""
        if not self._journal_path.exists():
            return
        try:
            entries: List[AwarenessJournalEntry] = []
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        entries.append(_journal_entry_from_dict(d))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
            self._journal = entries
            logger.debug("[UAE] Loaded %d journal entries from disk", len(entries))
        except Exception as exc:
            logger.warning("[UAE] Failed to load journal: %s", exc)

    async def _flush_journal(self) -> None:
        """Persist journal entries to disk (best-effort, with rotation)."""
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)

            # Rotate if over size limit
            if self._journal_path.exists():
                try:
                    file_size = self._journal_path.stat().st_size
                    if file_size >= _JOURNAL_MAX_BYTES:
                        rotated = self._journal_path.with_suffix(".jsonl.old")
                        self._journal_path.rename(rotated)
                        logger.info(
                            "[UAE] Journal rotated (%.1f MB -> %s)",
                            file_size / (1024 * 1024),
                            rotated,
                        )
                except OSError:
                    pass

            # Write all current entries
            with open(self._journal_path, "w", encoding="utf-8") as f:
                for entry in self._journal:
                    f.write(json.dumps(_journal_entry_to_dict(entry)) + "\n")

            logger.debug("[UAE] Flushed %d journal entries to disk", len(self._journal))
        except Exception as exc:
            logger.warning("[UAE] Failed to flush journal: %s", exc)

    # ------------------------------------------------------------------
    # Persistence: State cache (latest snapshot)
    # ------------------------------------------------------------------

    async def _load_state_cache(self) -> None:
        """Load the last unified state from disk (best-effort)."""
        if not self._state_cache_path.exists():
            return
        try:
            with open(self._state_cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)

            level_str = d.get("awareness_level", "DORMANT")
            try:
                level = AwarenessLevel(level_str)
            except (ValueError, KeyError):
                level = AwarenessLevel.DORMANT

            self._last_state = UnifiedAwarenessState(
                awareness_level=level,
                contextual=None,  # opaque states not persisted
                situational=None,
                health_verdict=d.get("health_verdict", "HEALTHY"),
                active_predictions=int(d.get("active_predictions", 0)),
                memory_insights_available=int(d.get("memory_insights_available", 0)),
                dream_blueprints_available=int(d.get("dream_blueprints_available", 0)),
                composite_confidence=float(d.get("composite_confidence", 0.0)),
                risk_posture=d.get("risk_posture", "NORMAL"),
                awareness_summary=d.get("awareness_summary", "Restored from cache."),
                timestamp=float(d.get("timestamp", 0.0)),
            )
            self._previous_level = self._last_state.awareness_level
            logger.debug("[UAE] Restored state cache (level=%s)", level_str)
        except Exception as exc:
            logger.warning("[UAE] Failed to load state cache: %s", exc)

    async def _flush_state_cache(self) -> None:
        """Persist the latest unified state to disk (best-effort)."""
        if self._last_state is None:
            return
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            data = _state_to_dict(self._last_state)
            with open(self._state_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug("[UAE] Flushed state cache to disk")
        except Exception as exc:
            logger.warning("[UAE] Failed to flush state cache: %s", exc)
