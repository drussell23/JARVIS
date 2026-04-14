"""
Context Compaction — Bounded Context Compression for Long Operations
====================================================================

When an operation's dialogue/context grows beyond a configurable threshold,
older entries are compressed into a deterministic summary while preserving
recent entries and any entry matching safety-critical patterns (errors,
security events, approvals).

No model inference. Summaries are deterministic (counting + grouping).

Hook Integration
----------------
If the :class:`LifecycleHookEngine` singleton is available, ``PRE_COMPACT``
and ``POST_COMPACT`` hooks are fired around the compaction. Hooks that
return ``allow=False`` on ``PRE_COMPACT`` will abort compaction.

Configuration
-------------
All thresholds are read from environment variables with sensible defaults:

- ``JARVIS_COMPACT_MAX_ENTRIES``: trigger threshold (default 50)
- ``JARVIS_COMPACT_PRESERVE_COUNT``: always keep the N most recent (default 10)
- ``JARVIS_COMPACT_PRESERVE_PATTERNS``: comma-separated regex patterns
  for entries that must never be compacted (default:
  ``error,security,approval,critical,break_glass``)
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ContextCompaction")

# ---------------------------------------------------------------------------
# Default configuration from environment
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ENTRIES: int = int(
    os.environ.get("JARVIS_COMPACT_MAX_ENTRIES", "50")
)
_DEFAULT_PRESERVE_COUNT: int = int(
    os.environ.get("JARVIS_COMPACT_PRESERVE_COUNT", "10")
)
_DEFAULT_PRESERVE_PATTERNS_RAW: str = os.environ.get(
    "JARVIS_COMPACT_PRESERVE_PATTERNS",
    "error,security,approval,critical,break_glass",
)
_DEFAULT_PRESERVE_PATTERNS: Tuple[str, ...] = tuple(
    p.strip() for p in _DEFAULT_PRESERVE_PATTERNS_RAW.split(",") if p.strip()
)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionConfig:
    """Governs when and how context compaction occurs.

    Attributes
    ----------
    max_context_entries:
        Trigger threshold. Compaction is recommended when the entry count
        exceeds this value.
    preserve_count:
        The N most recent entries are always kept, regardless of content.
    preserve_patterns:
        Regex patterns. Any entry whose serialised representation matches
        at least one pattern is preserved even if it falls outside the
        recent window.
    """

    max_context_entries: int = _DEFAULT_MAX_ENTRIES
    preserve_count: int = _DEFAULT_PRESERVE_COUNT
    preserve_patterns: Tuple[str, ...] = _DEFAULT_PRESERVE_PATTERNS

    @classmethod
    def from_env(cls) -> CompactionConfig:
        """Build a config purely from environment variables."""
        return cls(
            max_context_entries=_DEFAULT_MAX_ENTRIES,
            preserve_count=_DEFAULT_PRESERVE_COUNT,
            preserve_patterns=_DEFAULT_PRESERVE_PATTERNS,
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a compaction pass.

    Attributes
    ----------
    entries_before:
        Total entry count before compaction.
    entries_after:
        Total entry count after compaction (preserved + 1 summary entry).
    entries_compacted:
        How many entries were compressed into the summary.
    summary:
        Human-readable summary of the compacted entries.
    preserved_keys:
        Identifiers (phase names, types, or indices) of entries that
        survived compaction.
    """

    entries_before: int = 0
    entries_after: int = 0
    entries_compacted: int = 0
    summary: str = ""
    preserved_keys: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compactor
# ---------------------------------------------------------------------------


class ContextCompactor:
    """Deterministic context compaction engine.

    Compresses older dialogue entries into a summary while preserving
    recent entries and safety-critical matches. No model inference by
    default.

    An optional ``semantic_strategy`` may be injected at construction. When
    present and enabled, the strategy is invoked *in parallel with* the
    deterministic summarizer in shadow mode (result discarded, telemetry
    written), and *in place of* the deterministic summarizer in live mode.
    On any strategy failure — timeout, anti-hallucination rejection, circuit
    open — :meth:`_build_summary` falls back to the deterministic path. This
    is the Phase 0 hook for the Functions-not-Agents reseating (Manifesto §5).
    """

    def __init__(self, semantic_strategy: Optional[Any] = None) -> None:
        # Compiled patterns cache: pattern string -> compiled regex
        self._pattern_cache: Dict[str, re.Pattern[str]] = {}
        self._semantic_strategy = semantic_strategy

    # -- Public API ---------------------------------------------------------

    def should_compact(
        self,
        dialogue_entries: List[Dict[str, Any]],
        config: Optional[CompactionConfig] = None,
    ) -> bool:
        """Return True if the entry count exceeds the compaction threshold."""
        cfg = config or CompactionConfig.from_env()
        return len(dialogue_entries) > cfg.max_context_entries

    async def compact(
        self,
        dialogue_entries: List[Dict[str, Any]],
        config: Optional[CompactionConfig] = None,
    ) -> CompactionResult:
        """Compact *dialogue_entries* according to *config*.

        Steps:
          1. Fire ``PRE_COMPACT`` hook (if engine available). Abort on block.
          2. Partition entries into preserved vs. compactable.
          3. Build deterministic summary of compactable entries.
          4. Fire ``POST_COMPACT`` hook.
          5. Return :class:`CompactionResult`.

        The caller is responsible for replacing its entry list with the
        compacted version (summary entry + preserved entries).
        """
        cfg = config or CompactionConfig.from_env()
        entries_before = len(dialogue_entries)

        # --- PRE_COMPACT hook ---
        hook_engine = _get_hook_engine_safe()
        if hook_engine is not None:
            from backend.core.ouroboros.governance.lifecycle_hooks import (
                HookEvent,
            )
            pre_results = await hook_engine.fire(
                HookEvent.PRE_COMPACT,
                {
                    "entries_count": entries_before,
                    "max_context_entries": cfg.max_context_entries,
                    "preserve_count": cfg.preserve_count,
                },
            )
            # If any hook blocked, abort compaction
            if any(not r.allow for r in pre_results):
                feedback = "; ".join(r.feedback for r in pre_results if not r.allow)
                logger.info(
                    "[ContextCompaction] PRE_COMPACT hook blocked compaction: %s",
                    feedback,
                )
                return CompactionResult(
                    entries_before=entries_before,
                    entries_after=entries_before,
                    entries_compacted=0,
                    summary=f"Compaction blocked by hook: {feedback}",
                    preserved_keys=[],
                )

        # --- Partition entries ---
        preserved, compactable, preserved_keys = self._partition(
            dialogue_entries, cfg,
        )

        if not compactable:
            logger.debug(
                "[ContextCompaction] Nothing to compact (%d entries, all preserved)",
                entries_before,
            )
            return CompactionResult(
                entries_before=entries_before,
                entries_after=entries_before,
                entries_compacted=0,
                summary="",
                preserved_keys=preserved_keys,
            )

        # --- Build summary ---
        deterministic_summary = self._build_summary(compactable)
        summary = await self._build_semantic_or_fallback(
            compactable, deterministic_summary,
        )
        entries_compacted = len(compactable)
        entries_after = len(preserved) + 1  # +1 for the summary entry

        result = CompactionResult(
            entries_before=entries_before,
            entries_after=entries_after,
            entries_compacted=entries_compacted,
            summary=summary,
            preserved_keys=preserved_keys,
        )

        logger.info(
            "[ContextCompaction] Compacted %d -> %d entries (%d removed)",
            entries_before, entries_after, entries_compacted,
        )

        # --- POST_COMPACT hook ---
        if hook_engine is not None:
            from backend.core.ouroboros.governance.lifecycle_hooks import (
                HookEvent,
            )
            await hook_engine.fire(
                HookEvent.POST_COMPACT,
                {
                    "entries_before": entries_before,
                    "entries_after": entries_after,
                    "entries_compacted": entries_compacted,
                    "summary": summary,
                },
            )

        return result

    # -- Internal -----------------------------------------------------------

    def _partition(
        self,
        entries: List[Dict[str, Any]],
        config: CompactionConfig,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Split entries into (preserved, compactable, preserved_keys).

        Preservation rules (applied in order):
          1. The last ``config.preserve_count`` entries are always kept.
          2. Any entry matching at least one ``config.preserve_patterns``
             regex is kept.
        """
        total = len(entries)
        preserve_count = min(config.preserve_count, total)

        # Recent entries (unconditionally preserved)
        recent_entries = entries[total - preserve_count:] if preserve_count > 0 else []
        older_entries = entries[:total - preserve_count]

        # Indices of older entries that match preserve patterns
        preserved_from_older: List[Dict[str, Any]] = []
        compactable: List[Dict[str, Any]] = []

        compiled_patterns = self._compile_patterns(config.preserve_patterns)

        for entry in older_entries:
            entry_text = _entry_to_text(entry)
            if self._matches_any(entry_text, compiled_patterns):
                preserved_from_older.append(entry)
            else:
                compactable.append(entry)

        # Preserved = pattern-matched older entries + recent entries (in order)
        preserved = preserved_from_older + recent_entries

        # Build preserved keys for reporting
        preserved_keys: List[str] = []
        for entry in preserved:
            key = _entry_key(entry)
            preserved_keys.append(key)

        return preserved, compactable, preserved_keys

    def _build_summary(self, entries: List[Dict[str, Any]]) -> str:
        """Deterministic summary: group by type, count per type.

        No model inference. Pure counting and formatting.
        """
        type_counter: Counter[str] = Counter()
        phases_seen: Counter[str] = Counter()
        earliest_ts: Optional[float] = None
        latest_ts: Optional[float] = None

        for entry in entries:
            entry_type = (
                entry.get("type")
                or entry.get("phase")
                or entry.get("role")
                or "unknown"
            )
            type_counter[str(entry_type)] += 1

            phase = entry.get("phase")
            if phase:
                phases_seen[str(phase)] += 1

            ts = entry.get("timestamp") or entry.get("ts") or entry.get("time")
            if isinstance(ts, (int, float)):
                if earliest_ts is None or ts < earliest_ts:
                    earliest_ts = ts
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts

        total = len(entries)
        parts: List[str] = [f"Compacted {total} entries"]

        # Type breakdown
        type_parts = [f"{count} {typ}" for typ, count in type_counter.most_common()]
        if type_parts:
            parts.append(": " + ", ".join(type_parts))

        # Phase coverage
        if phases_seen:
            phase_list = sorted(phases_seen.keys())
            parts.append(f". Phases covered: {', '.join(phase_list)}")

        # Time span
        if earliest_ts is not None and latest_ts is not None:
            span_s = latest_ts - earliest_ts
            if span_s > 0:
                parts.append(f". Time span: {span_s:.1f}s")

        return "".join(parts)

    async def _build_semantic_or_fallback(
        self,
        entries: List[Dict[str, Any]],
        deterministic_summary: str,
    ) -> str:
        """Delegate to ``semantic_strategy`` if injected and enabled.

        Behavior contract:
          * No strategy → return ``deterministic_summary`` unchanged.
          * Shadow mode → always return ``deterministic_summary`` (strategy
            still runs for telemetry).
          * Live mode, strategy accepts → return strategy summary.
          * Live mode, strategy rejects/errors → return
            ``deterministic_summary`` (the caller is expected to swallow all
            exceptions and return a result with ``accepted=False``).
        """
        strategy = self._semantic_strategy
        if strategy is None or not getattr(strategy, "enabled", False):
            return deterministic_summary
        try:
            result = await strategy.summarize(entries, deterministic_summary)
        except Exception:
            logger.exception(
                "[ContextCompaction] semantic strategy raised — falling back to deterministic",
            )
            return deterministic_summary
        semantic = getattr(result, "summary", None)
        if semantic:
            return semantic
        return deterministic_summary

    def _compile_patterns(
        self, patterns: Tuple[str, ...],
    ) -> List[re.Pattern[str]]:
        """Compile and cache regex patterns."""
        compiled: List[re.Pattern[str]] = []
        for pat in patterns:
            if pat not in self._pattern_cache:
                try:
                    self._pattern_cache[pat] = re.compile(pat, re.IGNORECASE)
                except re.error:
                    logger.warning(
                        "[ContextCompaction] Invalid preserve pattern %r -- skipping",
                        pat,
                    )
                    continue
            compiled.append(self._pattern_cache[pat])
        return compiled

    def _matches_any(
        self,
        text: str,
        patterns: List[re.Pattern[str]],
    ) -> bool:
        """Return True if *text* matches at least one compiled pattern."""
        return any(p.search(text) for p in patterns)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_to_text(entry: Dict[str, Any]) -> str:
    """Flatten an entry dict to a searchable text string."""
    parts: List[str] = []
    for key in ("type", "phase", "role", "reasoning", "content", "data", "message"):
        val = entry.get(key)
        if val is not None:
            parts.append(str(val))
    return " ".join(parts) if parts else str(entry)


def _entry_key(entry: Dict[str, Any]) -> str:
    """Extract a human-readable key from an entry for reporting."""
    for key in ("op_id", "phase", "type", "role", "id"):
        val = entry.get(key)
        if val is not None:
            return f"{key}={val}"
    return f"idx={id(entry)}"


def _get_hook_engine_safe() -> Any:
    """Return the LifecycleHookEngine singleton, or None if unavailable.

    Lazy import to avoid circular dependencies.
    """
    try:
        from backend.core.ouroboros.governance.lifecycle_hooks import (
            get_hook_engine,
        )
        return get_hook_engine()
    except Exception:
        return None
