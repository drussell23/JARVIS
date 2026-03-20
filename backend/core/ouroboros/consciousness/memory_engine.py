"""backend/core/ouroboros/consciousness/memory_engine.py

MemoryEngine — Cross-Session Learning Store
=============================================

Ingests terminal outcomes from the OperationLedger, distils them into
MemoryInsight objects, tracks per-file reputation, and persists everything
to disk across process lifetimes.

Design:
    - ``ingest_outcome(op_id)`` is the sole write path: it reads the ledger,
      filters to terminal entries, builds or merges a MemoryInsight, and
      updates FileReputation counters.
    - ``query(query, max_results)`` is a lightweight keyword search over the
      in-memory insight list, sorted by confidence descending.
    - ``get_file_reputation(file_path)`` returns live FileReputation with
      sensible defaults for unknown files.
    - ``get_pattern_summary()`` aggregates across all insights.
    - TTL decay is applied lazily on every read path (query / summary).
    - Persistence uses three files in ``persistence_dir``:
        insights.jsonl    — append-only, rotated at 50 MB
        file_reputations.json
        patterns.json     — cached summary (best-effort, not authoritative)
    - All disk errors are caught and logged; in-memory state is never
      corrupted by I/O failures (TC32).
    - TC08: when the git HEAD changes since last scan, insights whose
      ``last_seen_utc`` was produced against the old HEAD are invalidated
      (confidence zeroed and they fall through the expired filter).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.consciousness.types import (
    FileReputation,
    MemoryInsight,
    PatternSummary,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.APPLIED,
        OperationState.FAILED,
        OperationState.ROLLED_BACK,
        OperationState.BLOCKED,
    }
)

_INSIGHTS_FILENAME = "insights.jsonl"
_REPUTATIONS_FILENAME = "file_reputations.json"
_PATTERNS_FILENAME = "patterns.json"
_INSIGHTS_ROTATION_BYTES = 50 * 1024 * 1024  # 50 MB

_DEFAULT_TTL_HOURS = float(os.getenv("JARVIS_CONSCIOUSNESS_MEMORY_TTL_HOURS", "168.0"))
_DEFAULT_CONFIDENCE_BASE = 0.5
_CONFIDENCE_GROWTH_PER_EVIDENCE = 0.05
_MAX_CONFIDENCE = 0.95


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _days_past_ttl(insight: MemoryInsight) -> float:
    """Return how many days past TTL the insight is (0.0 if not yet expired)."""
    last = _parse_utc(insight.last_seen_utc)
    if last is None:
        return 0.0
    expiry = last + timedelta(hours=insight.ttl_hours)
    now = datetime.now(timezone.utc)
    if now <= expiry:
        return 0.0
    return (now - expiry).total_seconds() / 86_400.0


def _effective_confidence(insight: MemoryInsight) -> float:
    """Return confidence with TTL decay applied."""
    days = _days_past_ttl(insight)
    if days <= 0.0:
        return insight.confidence
    return insight.decay_confidence(days)


def _insight_to_dict(insight: MemoryInsight) -> Dict[str, Any]:
    return {
        "insight_id": insight.insight_id,
        "category": insight.category,
        "content": insight.content,
        "confidence": insight.confidence,
        "evidence_count": insight.evidence_count,
        "last_seen_utc": insight.last_seen_utc,
        "ttl_hours": insight.ttl_hours,
    }


def _insight_from_dict(d: Dict[str, Any]) -> MemoryInsight:
    return MemoryInsight(
        insight_id=d["insight_id"],
        category=d["category"],
        content=d["content"],
        confidence=float(d["confidence"]),
        evidence_count=int(d["evidence_count"]),
        last_seen_utc=d["last_seen_utc"],
        ttl_hours=float(d.get("ttl_hours", _DEFAULT_TTL_HOURS)),
    )


def _get_git_head(repo_path: Optional[str] = None) -> Optional[str]:
    """Return the current git HEAD SHA, or None on any error."""
    try:
        cwd = repo_path or os.getcwd()
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _classify_category(entries: List[LedgerEntry]) -> str:
    """Classify insight category from terminal ledger entries."""
    terminal = [e for e in entries if e.state in _TERMINAL_STATES]
    if not terminal:
        return "unknown"
    final_state = terminal[-1].state
    if final_state == OperationState.APPLIED:
        return "success_pattern"
    if final_state in (OperationState.FAILED, OperationState.ROLLED_BACK):
        return "failure_pattern"
    if final_state == OperationState.BLOCKED:
        return "failure_pattern"
    return "unknown"


def _extract_target_files(entries: List[LedgerEntry]) -> Tuple[str, ...]:
    """Extract referenced file paths from ledger entry data payloads."""
    files: List[str] = []
    for entry in entries:
        data = entry.data or {}
        # Support various keys used across providers
        for key in ("target_files", "files", "changed_files", "patch_files"):
            val = data.get(key)
            if isinstance(val, (list, tuple)):
                files.extend(str(f) for f in val if f)
            elif isinstance(val, str) and val:
                files.append(val)
        # Also check nested "patch" dicts
        patch = data.get("patch", {})
        if isinstance(patch, dict):
            patch_files = list(patch.keys())
            files.extend(f for f in patch_files if f)
    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return tuple(unique)


def _build_content_summary(entries: List[LedgerEntry], category: str, op_id: str) -> str:
    """Build a human-readable content summary for an insight."""
    terminal = [e for e in entries if e.state in _TERMINAL_STATES]
    final = terminal[-1] if terminal else entries[-1] if entries else None
    state_name = final.state.value if final else "unknown"
    reason = ""
    if final:
        data = final.data or {}
        reason = (
            data.get("reason")
            or data.get("error")
            or data.get("message")
            or ""
        )
    files = _extract_target_files(entries)
    file_hint = f" affecting {files[0]}" if files else ""
    reason_hint = f": {reason}" if reason else ""
    return f"op {op_id[:12]} {state_name}{file_hint}{reason_hint} [{category}]"


def _compute_insight_id(op_id: str, category: str) -> str:
    """Deterministic insight ID scoped to op + category."""
    raw = f"{op_id}:{category}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _fragility_score(stats: Dict[str, Any]) -> float:
    """Derive a 0–1 fragility score from raw file stats."""
    change_count: int = stats.get("change_count", 0)
    success_count: int = stats.get("success_count", 0)
    failure_count: int = stats.get("failure_count", 0)
    blast_total: int = stats.get("blast_total", 0)

    total = success_count + failure_count
    if total == 0:
        return 0.0

    failure_rate = failure_count / total
    avg_blast = (blast_total / total) if total > 0 else 0
    blast_factor = min(1.0, avg_blast / 10.0)
    churn_factor = min(1.0, change_count / 20.0)

    score = (failure_rate * 0.6) + (blast_factor * 0.25) + (churn_factor * 0.15)
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# MemoryEngine
# ---------------------------------------------------------------------------


class MemoryEngine:
    """Cross-session learning store for the Trinity Consciousness layer.

    Parameters
    ----------
    ledger:
        Any object with ``async get_history(op_id) -> List[LedgerEntry]``.
        Typically an ``OperationLedger`` instance.
    persistence_dir:
        Directory where ``insights.jsonl``, ``file_reputations.json``, and
        ``patterns.json`` are written.
    repo_path:
        Optional path to the git repo root used for HEAD tracking (TC08).
        Defaults to ``os.getcwd()``.
    """

    def __init__(
        self,
        ledger: Any,
        persistence_dir: Path,
        repo_path: Optional[str] = None,
    ) -> None:
        self._ledger = ledger
        self._persistence_dir = Path(persistence_dir)
        self._repo_path = repo_path

        self._insights: List[MemoryInsight] = []
        # Keyed by file_path str; sub-keys: change_count, success_count,
        # failure_count, blast_total, co_failures (Counter-like dict)
        self._file_stats: Dict[str, Dict[str, Any]] = {}
        self._last_known_head: Optional[str] = None

        self._persistence_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted insights + reputations from disk."""
        self._load_insights_from_disk()
        self._load_reputations_from_disk()
        self._last_known_head = _get_git_head(self._repo_path)
        logger.info(
            "MemoryEngine started: %d insights loaded, HEAD=%s",
            len(self._insights),
            self._last_known_head,
        )

    async def stop(self) -> None:
        """Flush in-memory state to disk."""
        try:
            self._flush_reputations_to_disk()
        except OSError as exc:
            logger.error("MemoryEngine: cannot flush reputations on stop: %s", exc)
        try:
            self._flush_patterns_to_disk()
        except OSError as exc:
            logger.error("MemoryEngine: cannot flush patterns on stop: %s", exc)
        logger.info("MemoryEngine stopped: state flushed to %s", self._persistence_dir)

    # ------------------------------------------------------------------
    # Core write path
    # ------------------------------------------------------------------

    async def ingest_outcome(self, op_id: str) -> None:
        """Read ledger entries for *op_id*, build a MemoryInsight, and update
        file reputations.

        Only terminal-state entries (APPLIED, FAILED, ROLLED_BACK, BLOCKED)
        are processed.  If no terminal entry exists, the call is a no-op.

        TC08: before ingesting, checks whether git HEAD has changed; if so,
        stale insights are invalidated.
        """
        self._maybe_invalidate_on_head_change()

        try:
            entries: List[LedgerEntry] = await self._ledger.get_history(op_id)
        except Exception as exc:
            logger.warning("MemoryEngine: ledger read failed for %s: %s", op_id, exc)
            return

        terminal = [e for e in entries if e.state in _TERMINAL_STATES]
        if not terminal:
            logger.debug("MemoryEngine: no terminal entries for op %s — skipping", op_id)
            return

        insight = self._build_insight(entries)
        if insight is not None:
            self._merge_or_append_insight(insight)
            try:
                self._append_insight_to_disk(insight)
            except OSError as exc:
                logger.error(
                    "MemoryEngine: disk write failed (TC32 — in-memory continues): %s", exc
                )

        target_files = _extract_target_files(entries)
        success = terminal[-1].state == OperationState.APPLIED
        blast_radius = len(target_files)
        self._update_file_reputation(target_files, success, blast_radius)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def query(self, query: str, max_results: int = 5) -> List[MemoryInsight]:
        """Keyword search over insights, sorted by effective confidence desc.

        Expired (zero-confidence) insights are excluded from results.

        Parameters
        ----------
        query:
            Space-separated keywords matched case-insensitively against
            ``content`` and ``category``.
        max_results:
            Maximum number of results to return.
        """
        keywords = [kw.lower() for kw in query.split() if kw]
        now_iso = _utcnow_iso()
        results: List[Tuple[float, MemoryInsight]] = []
        for insight in self._insights:
            effective = _effective_confidence(insight)
            if effective <= 0.0:
                continue
            if keywords:
                haystack = (insight.content + " " + insight.category).lower()
                if not all(kw in haystack for kw in keywords):
                    continue
            results.append((effective, insight))
        results.sort(key=lambda t: t[0], reverse=True)
        return [ins for _, ins in results[:max_results]]

    def get_file_reputation(self, file_path: str) -> FileReputation:
        """Return reputation for *file_path*, with sensible defaults if unknown."""
        stats = self._file_stats.get(file_path)
        if stats is None:
            return FileReputation(
                file_path=file_path,
                change_count=0,
                success_rate=1.0,
                avg_blast_radius=0,
                common_co_failures=(),
                fragility_score=0.0,
            )
        change_count = stats.get("change_count", 0)
        success_count = stats.get("success_count", 0)
        failure_count = stats.get("failure_count", 0)
        blast_total = stats.get("blast_total", 0)
        total = success_count + failure_count
        success_rate = (success_count / total) if total > 0 else 1.0
        avg_blast = int(round(blast_total / total)) if total > 0 else 0
        co_counter: Dict[str, int] = stats.get("co_failures", {})
        sorted_co = sorted(co_counter.items(), key=lambda x: x[1], reverse=True)
        common_co = tuple(path for path, _ in sorted_co[:5])
        fragility = _fragility_score(stats)
        return FileReputation(
            file_path=file_path,
            change_count=change_count,
            success_rate=success_rate,
            avg_blast_radius=avg_blast,
            common_co_failures=common_co,
            fragility_score=fragility,
        )

    def get_pattern_summary(self) -> PatternSummary:
        """Aggregate all insights into a PatternSummary.

        Top patterns are sorted by ``evidence_count`` desc (top 10).
        """
        now_iso = _utcnow_iso()
        active: List[MemoryInsight] = []
        archived: List[MemoryInsight] = []
        for insight in self._insights:
            if insight.is_expired(now_iso):
                archived.append(insight)
            else:
                active.append(insight)
        top = sorted(self._insights, key=lambda i: i.evidence_count, reverse=True)[:10]
        return PatternSummary(
            top_patterns=tuple(top),
            total_insights=len(self._insights),
            active_insights=len(active),
            archived_insights=len(archived),
        )

    # ------------------------------------------------------------------
    # Internal: insight building & merging
    # ------------------------------------------------------------------

    def _build_insight(self, entries: List[LedgerEntry]) -> Optional[MemoryInsight]:
        """Build a MemoryInsight from a list of ledger entries.

        Returns None if no terminal entry is found.
        """
        terminal = [e for e in entries if e.state in _TERMINAL_STATES]
        if not terminal:
            return None

        op_id = entries[0].op_id if entries else "unknown"
        category = _classify_category(entries)
        content = _build_content_summary(entries, category, op_id)
        insight_id = _compute_insight_id(op_id, category)
        last_seen = _utcnow_iso()

        # Check if we already have an insight with this ID to compute evidence
        existing = self._find_insight_by_id(insight_id)
        if existing is not None:
            evidence_count = existing.evidence_count + 1
            confidence = min(
                _MAX_CONFIDENCE,
                existing.confidence + _CONFIDENCE_GROWTH_PER_EVIDENCE,
            )
        else:
            evidence_count = 1
            confidence = _DEFAULT_CONFIDENCE_BASE

        return MemoryInsight(
            insight_id=insight_id,
            category=category,
            content=content,
            confidence=confidence,
            evidence_count=evidence_count,
            last_seen_utc=last_seen,
            ttl_hours=_DEFAULT_TTL_HOURS,
        )

    def _merge_or_append_insight(self, insight: MemoryInsight) -> None:
        """Replace existing insight with same ID, or append if new."""
        for i, existing in enumerate(self._insights):
            if existing.insight_id == insight.insight_id:
                self._insights[i] = insight
                return
        self._insights.append(insight)

    def _find_insight_by_id(self, insight_id: str) -> Optional[MemoryInsight]:
        for insight in self._insights:
            if insight.insight_id == insight_id:
                return insight
        return None

    # ------------------------------------------------------------------
    # Internal: file reputation
    # ------------------------------------------------------------------

    def _update_file_reputation(
        self,
        target_files: Tuple[str, ...],
        success: bool,
        blast_radius: int = 0,
    ) -> None:
        """Update per-file counters.

        blast_radius defaults to the number of target_files when not provided
        separately; callers may override.
        """
        for file_path in target_files:
            if file_path not in self._file_stats:
                self._file_stats[file_path] = {
                    "change_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "blast_total": 0,
                    "co_failures": {},
                }
            stats = self._file_stats[file_path]
            stats["change_count"] += 1
            if success:
                stats["success_count"] += 1
            else:
                stats["failure_count"] += 1
            stats["blast_total"] += blast_radius

        # Record co-failure pairs (only on failure)
        if not success and len(target_files) > 1:
            for file_path in target_files:
                stats = self._file_stats[file_path]
                co_counter = stats["co_failures"]
                for other in target_files:
                    if other != file_path:
                        co_counter[other] = co_counter.get(other, 0) + 1

    # ------------------------------------------------------------------
    # Internal: TC08 HEAD invalidation
    # ------------------------------------------------------------------

    def _maybe_invalidate_on_head_change(self) -> None:
        """If git HEAD has changed, remove insights that are now stale.

        Stale insights are those with confidence > 0 that were produced
        against the old HEAD (detected via HEAD mismatch).  We zero their
        confidence by removing them — they will be rebuilt from fresh ledger
        data as new ops complete.
        """
        current_head = _get_git_head(self._repo_path)
        if current_head is None:
            return
        if self._last_known_head is None:
            self._last_known_head = current_head
            return
        if current_head == self._last_known_head:
            return

        logger.info(
            "MemoryEngine: HEAD changed %s -> %s, invalidating stale insights",
            self._last_known_head,
            current_head,
        )
        # Retain insights whose category is time-based (not file-specific)
        retained = [
            ins for ins in self._insights
            if ins.category in ("timing_pattern",)
        ]
        removed = len(self._insights) - len(retained)
        self._insights = retained
        self._last_known_head = current_head
        logger.info("MemoryEngine: invalidated %d stale insights after HEAD change", removed)

    # ------------------------------------------------------------------
    # Internal: disk persistence
    # ------------------------------------------------------------------

    def _insights_path(self) -> Path:
        return self._persistence_dir / _INSIGHTS_FILENAME

    def _reputations_path(self) -> Path:
        return self._persistence_dir / _REPUTATIONS_FILENAME

    def _patterns_path(self) -> Path:
        return self._persistence_dir / _PATTERNS_FILENAME

    def _load_insights_from_disk(self) -> None:
        """Load insights.jsonl from disk, skipping malformed lines (TC32)."""
        path = self._insights_path()
        if not path.exists():
            return
        loaded: List[MemoryInsight] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("MemoryEngine: cannot read insights file: %s", exc)
            return

        skipped = 0
        for lineno, raw in enumerate(lines, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                insight = _insight_from_dict(d)
                loaded.append(insight)
            except Exception as exc:
                logger.warning(
                    "MemoryEngine: skipping malformed insight at line %d: %s",
                    lineno,
                    exc,
                )
                skipped += 1

        # Deduplicate by insight_id, keeping the last occurrence (most recent)
        seen_ids: Dict[str, int] = {}
        for i, ins in enumerate(loaded):
            seen_ids[ins.insight_id] = i
        self._insights = [loaded[i] for i in sorted(seen_ids.values())]

        if skipped:
            logger.warning("MemoryEngine: skipped %d malformed lines in insights.jsonl", skipped)

    def _append_insight_to_disk(self, insight: MemoryInsight) -> None:
        """Append a single insight as a JSON line (TC32: swallow IOError)."""
        path = self._insights_path()
        try:
            # Rotate if over size limit
            if path.exists() and path.stat().st_size >= _INSIGHTS_ROTATION_BYTES:
                rotated = path.with_suffix(f".{int(time.time())}.jsonl")
                path.rename(rotated)
                logger.info("MemoryEngine: rotated insights file to %s", rotated)
            line = json.dumps(_insight_to_dict(insight), sort_keys=True) + "\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            logger.error(
                "MemoryEngine: disk write failed (TC32 — in-memory continues): %s", exc
            )

    def _load_reputations_from_disk(self) -> None:
        """Load file_reputations.json from disk."""
        path = self._reputations_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._file_stats = data
        except Exception as exc:
            logger.warning("MemoryEngine: cannot load reputations: %s", exc)

    def _flush_reputations_to_disk(self) -> None:
        """Persist file_reputations.json (TC32: swallow IOError)."""
        path = self._reputations_path()
        try:
            path.write_text(
                json.dumps(self._file_stats, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("MemoryEngine: cannot flush reputations: %s", exc)

    def _flush_patterns_to_disk(self) -> None:
        """Persist a snapshot of the current pattern summary (best-effort)."""
        path = self._patterns_path()
        try:
            summary = self.get_pattern_summary()
            data = {
                "total_insights": summary.total_insights,
                "active_insights": summary.active_insights,
                "archived_insights": summary.archived_insights,
                "top_patterns": [
                    _insight_to_dict(ins) for ins in summary.top_patterns
                ],
            }
            path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.error("MemoryEngine: cannot flush patterns: %s", exc)
