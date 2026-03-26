"""backend/core/ouroboros/consciousness/contextual_awareness.py

CAI — Contextual Awareness Intelligence Engine
===============================================

Synthesises outputs from MemoryEngine, ProphecyEngine, and HealthCortex into
higher-level contextual understanding of what is happening across the codebase,
active operations, and evolving code topology.

CAI is a *read-only synthesiser* — it never controls its peer engines, only
queries them.  It maintains its own persistent insight store that survives across
process lifetimes.

Boundary Principle:
    deterministic — file-analysis, keyword search, pattern matching, insight
                    lifecycle (creation / validation / staleness / pruning).
    agentic       — none.  Pure computation; no LLM calls, no network I/O.

Lifecycle:
    ``start()`` → load persisted insights, launch background analysis loop.
    ``stop()``  → persist insights, cancel background loop.
    Both are idempotent.

Thread-safety:
    All mutable state is only touched inside the single asyncio event loop —
    no locking needed beyond the natural serialisation that ``asyncio`` provides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.CAI")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ANALYSIS_INTERVAL_S: float = float(
    os.environ.get("JARVIS_CAI_ANALYSIS_INTERVAL_S", "300")
)
_DEFAULT_INSIGHT_TTL_HOURS: float = float(
    os.environ.get("JARVIS_CAI_INSIGHT_TTL_HOURS", "168")
)
_STALENESS_THRESHOLD_S: float = 72.0 * 3600.0  # 72 hours in seconds
_HOTSPOT_COUNT: int = 10
_MAX_INSIGHTS: int = 2000  # hard cap to avoid unbounded memory growth
_PERSISTENCE_FILENAME: str = "cai_insights.json"
_DEFAULT_PERSISTENCE_DIR: Path = (
    Path.home() / ".jarvis" / "ouroboros" / "consciousness"
)

# Valid insight categories
_VALID_CATEGORIES: frozenset[str] = frozenset(
    {"codebase_pattern", "dependency_chain", "hotspot", "drift", "coupling"}
)

# Complexity classification thresholds
_COMPLEXITY_THRESHOLDS: Tuple[Tuple[int, str], ...] = (
    (20, "CRITICAL"),
    (10, "TANGLED"),
    (5, "COMPLEX"),
    (0, "CLEAR"),
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextualInsight:
    """A single piece of contextual awareness extracted from codebase analysis.

    Immutable — create a new instance to update any field.
    """

    insight_id: str
    category: str  # one of _VALID_CATEGORIES
    subject: str
    content: str
    confidence: float  # 0.0–1.0
    evidence_files: Tuple[str, ...]
    created_at: float  # time.time() epoch
    last_validated_at: float  # time.time() epoch
    validation_count: int
    stale: bool = False


@dataclass(frozen=True)
class ContextAssessment:
    """Result of ``assess_context()`` — everything the generation layer needs
    to understand the operating context of a proposed change."""

    target_files: Tuple[str, ...]
    relevant_insights: List[ContextualInsight]
    risk_factors: List[str]
    suggested_context_files: List[str]
    complexity_estimate: str  # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    confidence: float  # 0.0–1.0


@dataclass(frozen=True)
class ContextualAwarenessState:
    """Point-in-time snapshot of overall contextual awareness."""

    active_operations: int
    codebase_hotspots: List[str]
    dependency_clusters: Dict[str, List[str]]
    recent_patterns: List[ContextualInsight]
    drift_warnings: List[str]
    overall_context_health: str  # "CLEAR" | "COMPLEX" | "TANGLED" | "CRITICAL"
    timestamp: float


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _insight_to_dict(insight: ContextualInsight) -> Dict[str, Any]:
    """Serialise a ContextualInsight to a JSON-safe dict."""
    return {
        "insight_id": insight.insight_id,
        "category": insight.category,
        "subject": insight.subject,
        "content": insight.content,
        "confidence": insight.confidence,
        "evidence_files": list(insight.evidence_files),
        "created_at": insight.created_at,
        "last_validated_at": insight.last_validated_at,
        "validation_count": insight.validation_count,
        "stale": insight.stale,
    }


def _insight_from_dict(d: Dict[str, Any]) -> ContextualInsight:
    """Deserialise a dict into a ContextualInsight.

    Unknown keys are silently ignored for forward-compatibility.
    """
    return ContextualInsight(
        insight_id=str(d["insight_id"]),
        category=str(d.get("category", "codebase_pattern")),
        subject=str(d.get("subject", "")),
        content=str(d.get("content", "")),
        confidence=float(d.get("confidence", 0.5)),
        evidence_files=tuple(str(f) for f in d.get("evidence_files", ())),
        created_at=float(d.get("created_at", 0.0)),
        last_validated_at=float(d.get("last_validated_at", 0.0)),
        validation_count=int(d.get("validation_count", 0)),
        stale=bool(d.get("stale", False)),
    )


# ---------------------------------------------------------------------------
# ContextualAwarenessEngine
# ---------------------------------------------------------------------------


class ContextualAwarenessEngine:
    """CAI — Contextual Awareness Intelligence engine.

    Peers into MemoryEngine, ProphecyEngine, and HealthCortex to synthesise a
    higher-level understanding of the codebase and operational context.

    Parameters
    ----------
    memory_engine:
        MemoryEngine instance — ``get_file_reputation(path)``,
        ``get_pattern_summary()``, ``query(keyword, max_results)``.
    prophecy_engine:
        ProphecyEngine instance — ``analyze_change(files)``,
        ``get_risk_scores()``.
    health_cortex:
        HealthCortex instance — ``get_snapshot()``.
    config:
        ConsciousnessConfig (``from_env()``).
    persistence_dir:
        Where ``cai_insights.json`` is stored.  Defaults to
        ``~/.jarvis/ouroboros/consciousness/``.
    comm:
        Optional CommProtocol — currently unused; reserved for future
        heartbeat integration.
    """

    def __init__(
        self,
        memory_engine: Any,
        prophecy_engine: Any,
        health_cortex: Any,
        config: Any,
        persistence_dir: Optional[Path] = None,
        comm: Any = None,
    ) -> None:
        self._memory = memory_engine
        self._prophecy = prophecy_engine
        self._cortex = health_cortex
        self._config = config
        self._comm = comm

        self._persistence_dir: Path = persistence_dir or _DEFAULT_PERSISTENCE_DIR
        self._persistence_path: Path = self._persistence_dir / _PERSISTENCE_FILENAME

        # In-memory insight store
        self._insights: List[ContextualInsight] = []

        # Cached dependency clusters (rebuilt by the background loop)
        self._dependency_clusters: Dict[str, List[str]] = {}

        # Background task handle
        self._analysis_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted insights and start the background analysis loop.

        Idempotent: second call is a no-op.
        """
        if self._running:
            return
        self._load_from_disk()
        self._running = True
        self._analysis_task = asyncio.ensure_future(self._analysis_loop())
        logger.info(
            "CAI started: %d insights loaded from %s",
            len(self._insights),
            self._persistence_path,
        )

    async def stop(self) -> None:
        """Cancel the background loop and persist insights to disk.

        Idempotent: safe to call multiple times or when not started.
        """
        self._running = False
        if self._analysis_task is not None:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except (asyncio.CancelledError, Exception):
                pass
            self._analysis_task = None
        self._save_to_disk()
        logger.info("CAI stopped: %d insights persisted", len(self._insights))

    # ------------------------------------------------------------------
    # Public: state snapshot
    # ------------------------------------------------------------------

    def get_state(self) -> ContextualAwarenessState:
        """Return a point-in-time awareness snapshot.

        Non-blocking.  Pulls live data from peer engines where available,
        falling back to cached / empty values on any error.
        """
        now = time.time()

        # Active operations — from HealthCortex snapshot
        active_operations = 0
        try:
            snapshot = self._cortex.get_snapshot()
            if snapshot is not None:
                # Use the overall score as a proxy; specific op count
                # may be exposed by the snapshot's detail dicts.
                details = getattr(snapshot, "jarvis", None)
                if details is not None:
                    d = getattr(details, "details", {})
                    active_operations = int(d.get("active_operations", 0))
        except Exception:
            pass

        # Codebase hotspots — top N files by change_count from MemoryEngine
        hotspots = self._compute_hotspots()

        # Recent patterns — insights from last 24h
        cutoff_24h = now - 86_400.0
        recent = [
            i for i in self._insights
            if not i.stale and i.last_validated_at >= cutoff_24h
        ]

        # Drift warnings — files with declining success rate
        drift_warnings = self._compute_drift_warnings()

        # Overall health classification
        complexity_signals = (
            len(recent)
            + len(drift_warnings)
            + (1 if active_operations > 3 else 0)
        )
        overall_health = self._classify_complexity(complexity_signals)

        return ContextualAwarenessState(
            active_operations=active_operations,
            codebase_hotspots=hotspots,
            dependency_clusters=dict(self._dependency_clusters),
            recent_patterns=recent,
            drift_warnings=drift_warnings,
            overall_context_health=overall_health,
            timestamp=now,
        )

    # ------------------------------------------------------------------
    # Public: context assessment
    # ------------------------------------------------------------------

    async def assess_context(
        self,
        target_files: Tuple[str, ...],
        goal: str,
    ) -> ContextAssessment:
        """Assess what contextual knowledge is relevant for a goal + file set.

        Parameters
        ----------
        target_files:
            Files the operation intends to modify.
        goal:
            Free-text description of the operation's intent.

        Returns
        -------
        ContextAssessment
            Synthesised view of relevant insights, risks, and suggestions.
        """
        risk_factors: List[str] = []
        suggested_context: List[str] = []
        confidence_sum = 0.0
        confidence_count = 0

        # 1) Query MemoryEngine for file reputations
        for fpath in target_files:
            try:
                rep = self._memory.get_file_reputation(fpath)
                if rep is not None and rep.fragility_score > 0.5:
                    risk_factors.append(
                        f"{fpath}: fragility {rep.fragility_score:.2f}, "
                        f"success rate {rep.success_rate:.0%}"
                    )
                if rep is not None and rep.common_co_failures:
                    for co_f in rep.common_co_failures[:3]:
                        if co_f not in target_files and co_f not in suggested_context:
                            suggested_context.append(co_f)
            except Exception:
                pass

        # 2) Check ProphecyEngine for risk scores
        try:
            risk_scores = self._prophecy.get_risk_scores()
            for fpath in target_files:
                score = risk_scores.get(fpath, 0.0)
                if score > 0.6:
                    risk_factors.append(
                        f"{fpath}: prophecy risk score {score:.2f}"
                    )
        except Exception:
            pass

        # 3) Look up insights related to these files
        relevant_insights: List[ContextualInsight] = []
        for fpath in target_files:
            matching = self._find_insights_for_file(fpath)
            for mi in matching:
                if mi not in relevant_insights:
                    relevant_insights.append(mi)
                    confidence_sum += mi.confidence
                    confidence_count += 1

        # 4) Also search by goal keywords
        goal_keywords = [w.lower() for w in goal.split() if len(w) > 3]
        for kw in goal_keywords[:5]:
            for insight in self.query_insights(kw, max_results=3):
                if insight not in relevant_insights:
                    relevant_insights.append(insight)
                    confidence_sum += insight.confidence
                    confidence_count += 1

        # 5) Suggest context from dependency clusters
        for fpath in target_files:
            cluster_files = self._dependency_clusters.get(fpath, [])
            for cf in cluster_files[:3]:
                if cf not in target_files and cf not in suggested_context:
                    suggested_context.append(cf)

        # Compute complexity estimate
        complexity_score = (
            len(target_files)
            + len(risk_factors) * 2
            + len(relevant_insights)
        )
        complexity_estimate = self._classify_complexity(complexity_score)

        overall_confidence = (
            (confidence_sum / confidence_count) if confidence_count > 0 else 0.5
        )

        return ContextAssessment(
            target_files=target_files,
            relevant_insights=relevant_insights,
            risk_factors=risk_factors,
            suggested_context_files=suggested_context,
            complexity_estimate=complexity_estimate,
            confidence=min(1.0, max(0.0, overall_confidence)),
        )

    # ------------------------------------------------------------------
    # Public: insight CRUD
    # ------------------------------------------------------------------

    def record_insight(
        self,
        category: str,
        subject: str,
        content: str,
        evidence_files: Tuple[str, ...],
        confidence: float = 0.5,
    ) -> ContextualInsight:
        """Store a new contextual insight.

        Parameters
        ----------
        category:
            Must be one of: ``codebase_pattern``, ``dependency_chain``,
            ``hotspot``, ``drift``, ``coupling``.
        subject:
            Short label for what this insight is about.
        content:
            The insight body text.
        evidence_files:
            Tuple of file paths that support this insight.
        confidence:
            Initial confidence, clamped to [0.0, 1.0].

        Returns
        -------
        ContextualInsight
            The newly created insight.
        """
        if category not in _VALID_CATEGORIES:
            logger.warning(
                "CAI: unknown category %r, defaulting to 'codebase_pattern'",
                category,
            )
            category = "codebase_pattern"

        now = time.time()
        insight = ContextualInsight(
            insight_id=uuid.uuid4().hex,
            category=category,
            subject=subject,
            content=content,
            confidence=max(0.0, min(1.0, confidence)),
            evidence_files=evidence_files,
            created_at=now,
            last_validated_at=now,
            validation_count=1,
            stale=False,
        )
        self._insights.append(insight)

        # Enforce hard cap
        if len(self._insights) > _MAX_INSIGHTS:
            # Evict oldest stale insights first, then oldest by created_at
            self._insights.sort(
                key=lambda i: (not i.stale, i.last_validated_at)
            )
            self._insights = self._insights[-_MAX_INSIGHTS:]

        return insight

    def validate_insight(self, insight_id: str) -> bool:
        """Bump validation count and refresh last_validated_at for an insight.

        Returns True if the insight was found, False otherwise.
        """
        now = time.time()
        for idx, insight in enumerate(self._insights):
            if insight.insight_id == insight_id:
                self._insights[idx] = ContextualInsight(
                    insight_id=insight.insight_id,
                    category=insight.category,
                    subject=insight.subject,
                    content=insight.content,
                    confidence=insight.confidence,
                    evidence_files=insight.evidence_files,
                    created_at=insight.created_at,
                    last_validated_at=now,
                    validation_count=insight.validation_count + 1,
                    stale=False,
                )
                return True
        return False

    def query_insights(
        self,
        keyword: str,
        max_results: int = 5,
    ) -> List[ContextualInsight]:
        """Keyword search across insights, sorted by confidence desc.

        Stale insights are excluded.

        Parameters
        ----------
        keyword:
            Space-separated keywords matched case-insensitively against
            ``subject``, ``content``, and ``category``.
        max_results:
            Maximum number of results to return.
        """
        keywords = [kw.lower() for kw in keyword.split() if kw]
        if not keywords:
            return []

        matches: List[ContextualInsight] = []
        for insight in self._insights:
            if insight.stale:
                continue
            haystack = (
                insight.subject + " " + insight.content + " " + insight.category
            ).lower()
            if all(kw in haystack for kw in keywords):
                matches.append(insight)

        matches.sort(key=lambda i: i.confidence, reverse=True)
        return matches[:max_results]

    # ------------------------------------------------------------------
    # Public: prompt formatting
    # ------------------------------------------------------------------

    def format_for_prompt(self, assessment: ContextAssessment) -> str:
        """Format a ContextAssessment for injection into a generation prompt.

        Returns a concise, structured text block that a model can consume.
        """
        sections: List[str] = []

        sections.append(
            f"Complexity: {assessment.complexity_estimate} "
            f"(confidence {assessment.confidence:.0%})"
        )

        if assessment.risk_factors:
            sections.append("Risk factors:")
            for rf in assessment.risk_factors:
                sections.append(f"  - {rf}")

        if assessment.relevant_insights:
            sections.append(
                f"Relevant insights ({len(assessment.relevant_insights)}):"
            )
            for insight in assessment.relevant_insights[:5]:
                sections.append(
                    f"  [{insight.category}] {insight.subject}: "
                    f"{insight.content} (conf={insight.confidence:.2f})"
                )

        if assessment.suggested_context_files:
            sections.append("Suggested additional context files:")
            for sf in assessment.suggested_context_files[:10]:
                sections.append(f"  - {sf}")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Background analysis loop
    # ------------------------------------------------------------------

    async def _analysis_loop(self) -> None:
        """Periodic background analysis that discovers patterns and prunes stale insights.

        Runs every ``JARVIS_CAI_ANALYSIS_INTERVAL_S`` seconds (default 300).
        """
        interval = float(
            os.environ.get(
                "JARVIS_CAI_ANALYSIS_INTERVAL_S",
                str(_DEFAULT_ANALYSIS_INTERVAL_S),
            )
        )
        ttl_hours = float(
            os.environ.get(
                "JARVIS_CAI_INSIGHT_TTL_HOURS",
                str(_DEFAULT_INSIGHT_TTL_HOURS),
            )
        )
        ttl_seconds = ttl_hours * 3600.0

        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break

                now = time.time()

                # --- Phase 1: Discover new hotspots from MemoryEngine ---
                self._refresh_hotspot_insights(now)

                # --- Phase 2: Discover coupling from dependency clusters ---
                self._refresh_coupling_insights(now)

                # --- Phase 3: Mark stale insights ---
                stale_count = 0
                for idx, insight in enumerate(self._insights):
                    if insight.stale:
                        continue
                    age_since_validation = now - insight.last_validated_at
                    if age_since_validation > _STALENESS_THRESHOLD_S:
                        self._insights[idx] = ContextualInsight(
                            insight_id=insight.insight_id,
                            category=insight.category,
                            subject=insight.subject,
                            content=insight.content,
                            confidence=insight.confidence,
                            evidence_files=insight.evidence_files,
                            created_at=insight.created_at,
                            last_validated_at=insight.last_validated_at,
                            validation_count=insight.validation_count,
                            stale=True,
                        )
                        stale_count += 1

                # --- Phase 4: Prune expired insights ---
                pre_prune = len(self._insights)
                self._insights = [
                    i for i in self._insights
                    if (now - i.created_at) < ttl_seconds
                ]
                pruned = pre_prune - len(self._insights)

                # --- Phase 5: Persist ---
                self._save_to_disk()

                if stale_count > 0 or pruned > 0:
                    logger.debug(
                        "CAI analysis loop: %d marked stale, %d pruned, "
                        "%d active insights remain",
                        stale_count,
                        pruned,
                        len(self._insights),
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("CAI analysis loop error: %s", exc)
                # Back off briefly to avoid tight spin on persistent errors
                try:
                    await asyncio.sleep(30.0)
                except asyncio.CancelledError:
                    break

    # ------------------------------------------------------------------
    # Internal: hotspot discovery
    # ------------------------------------------------------------------

    def _compute_hotspots(self) -> List[str]:
        """Return top N codebase hotspots from MemoryEngine file reputations.

        Queries file reputations for all files that appear in existing insights'
        evidence_files and returns the top by change_count.
        """
        all_files: set[str] = set()
        for insight in self._insights:
            all_files.update(insight.evidence_files)

        scored: List[Tuple[int, str]] = []
        for fpath in all_files:
            try:
                rep = self._memory.get_file_reputation(fpath)
                if rep is not None and rep.change_count > 0:
                    scored.append((rep.change_count, fpath))
            except Exception:
                continue

        scored.sort(reverse=True)
        return [fpath for _, fpath in scored[:_HOTSPOT_COUNT]]

    def _refresh_hotspot_insights(self, now: float) -> None:
        """Create or validate hotspot insights from MemoryEngine pattern summary."""
        try:
            summary = self._memory.get_pattern_summary()
        except Exception:
            return

        if summary is None:
            return

        for pattern in summary.top_patterns[:_HOTSPOT_COUNT]:
            existing = self._find_insight_by_subject(
                "hotspot", pattern.content[:80]
            )
            if existing is not None:
                self.validate_insight(existing.insight_id)
            else:
                self.record_insight(
                    category="hotspot",
                    subject=pattern.content[:80],
                    content=f"Recurring pattern: {pattern.content}",
                    evidence_files=(),
                    confidence=min(0.9, 0.3 + pattern.evidence_count * 0.05),
                )

    # ------------------------------------------------------------------
    # Internal: coupling / dependency cluster discovery
    # ------------------------------------------------------------------

    def _refresh_coupling_insights(self, now: float) -> None:
        """Discover tightly-coupled file groups from co-failure data.

        Uses MemoryEngine's per-file common_co_failures to build
        dependency clusters.
        """
        clusters: Dict[str, List[str]] = {}
        all_files: set[str] = set()
        for insight in self._insights:
            all_files.update(insight.evidence_files)

        for fpath in all_files:
            try:
                rep = self._memory.get_file_reputation(fpath)
                if rep is None:
                    continue
                co_failures = list(rep.common_co_failures)
                if co_failures:
                    clusters[fpath] = co_failures
                    # Record coupling insight if cluster is non-trivial
                    if len(co_failures) >= 2:
                        subject = f"coupling:{fpath}"
                        existing = self._find_insight_by_subject(
                            "coupling", subject
                        )
                        if existing is not None:
                            self.validate_insight(existing.insight_id)
                        else:
                            self.record_insight(
                                category="coupling",
                                subject=subject,
                                content=(
                                    f"{fpath} tightly coupled with "
                                    f"{', '.join(co_failures[:5])}"
                                ),
                                evidence_files=tuple([fpath] + co_failures[:5]),
                                confidence=0.6,
                            )
            except Exception:
                continue

        self._dependency_clusters = clusters

    # ------------------------------------------------------------------
    # Internal: drift warnings
    # ------------------------------------------------------------------

    def _compute_drift_warnings(self) -> List[str]:
        """Identify files whose success rate is declining.

        Queries MemoryEngine for known files and flags those with
        success_rate below 0.6 and change_count > 2.
        """
        warnings: List[str] = []
        all_files: set[str] = set()
        for insight in self._insights:
            all_files.update(insight.evidence_files)

        for fpath in all_files:
            try:
                rep = self._memory.get_file_reputation(fpath)
                if (
                    rep is not None
                    and rep.change_count > 2
                    and rep.success_rate < 0.6
                ):
                    warnings.append(
                        f"{fpath}: success rate {rep.success_rate:.0%} "
                        f"across {rep.change_count} changes"
                    )
            except Exception:
                continue

        return warnings

    # ------------------------------------------------------------------
    # Internal: insight lookup helpers
    # ------------------------------------------------------------------

    def _find_insights_for_file(self, file_path: str) -> List[ContextualInsight]:
        """Return all non-stale insights whose evidence_files mention *file_path*."""
        return [
            i for i in self._insights
            if not i.stale and file_path in i.evidence_files
        ]

    def _find_insight_by_subject(
        self, category: str, subject: str
    ) -> Optional[ContextualInsight]:
        """Return the first non-stale insight matching category + subject."""
        for insight in self._insights:
            if (
                not insight.stale
                and insight.category == category
                and insight.subject == subject
            ):
                return insight
        return None

    # ------------------------------------------------------------------
    # Internal: complexity classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_complexity(signal_count: int) -> str:
        """Map a numeric complexity signal count to a categorical label."""
        for threshold, label in _COMPLEXITY_THRESHOLDS:
            if signal_count >= threshold:
                return label
        return "CLEAR"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        """Load persisted insights from ``cai_insights.json``.

        I/O errors are caught — an empty insight list is the fallback.
        """
        if not self._persistence_path.exists():
            logger.debug("CAI: no persisted insights at %s", self._persistence_path)
            return
        try:
            raw = self._persistence_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                logger.warning("CAI: persisted data is not a list, ignoring")
                return
            loaded: List[ContextualInsight] = []
            for entry in data:
                try:
                    loaded.append(_insight_from_dict(entry))
                except (KeyError, TypeError, ValueError) as exc:
                    logger.debug("CAI: skipping malformed insight: %s", exc)
            self._insights = loaded
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "CAI: failed to load persisted insights: %s", exc
            )

    def _save_to_disk(self) -> None:
        """Persist the current insight list to ``cai_insights.json``.

        I/O errors are caught and logged — never crash TrinityConsciousness.
        """
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            payload = [_insight_to_dict(i) for i in self._insights]
            tmp_path = self._persistence_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            tmp_path.replace(self._persistence_path)
        except OSError as exc:
            logger.warning("CAI: failed to persist insights: %s", exc)
