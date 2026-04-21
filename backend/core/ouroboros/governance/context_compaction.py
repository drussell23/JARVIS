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
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ContextCompaction")


# ---------------------------------------------------------------------------
# Intent-aware scorer integration (Slice 1 of Production Integration arc)
# ---------------------------------------------------------------------------


def context_compactor_scorer_enabled() -> bool:
    """Master switch for intent-aware compaction path.

    Default: **``true``** (graduated via the Context Preservation real-
    session harness at ``scripts/real_session_graduation.py``). The
    harness drives a 50+ turn simulation with multi-file intent drift,
    100+ tool chunks, concurrent cross-op pressure, auto-pin churn,
    and mid-session kill-switch verification. 37/37 checks pass across
    9 scenarios plus the full 607-test governance suite stays green
    with this flag on. Graduation flips opt-in friction, NOT
    preservation-layer authority — the scorer has always been
    additive.

    Explicit ``=false`` reverts to the legacy regex + last-N partition
    regardless of whether a scorer is attached. This is the runtime
    kill switch.

    During the scorer path, if any prerequisite is missing (no
    ``op_id`` passed to :meth:`ContextCompactor.compact`, no scorer
    attached, or the scorer raises) the compactor silently falls back
    to the legacy partition — never loses data.
    """
    return os.environ.get(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    ).strip().lower() == "true"

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

    def __init__(
        self,
        semantic_strategy: Optional[Any] = None,
        *,
        preservation_scorer: Optional[Any] = None,
        intent_tracker_lookup: Optional[Callable[[str], Any]] = None,
        pin_registry_lookup: Optional[Callable[[str], Any]] = None,
        manifest_lookup: Optional[Callable[[str], Any]] = None,
    ) -> None:
        # Compiled patterns cache: pattern string -> compiled regex
        self._pattern_cache: Dict[str, re.Pattern[str]] = {}
        self._semantic_strategy = semantic_strategy

        # Slice 1 Production Integration: optional intent-aware path.
        # ALL four are optional; when any is missing the compactor falls
        # back to its legacy regex + last-N partition. The flag
        # ``JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED`` gates this path;
        # explicit ``=false`` forces legacy even when attached.
        self._preservation_scorer = preservation_scorer
        self._intent_tracker_lookup = intent_tracker_lookup
        self._pin_registry_lookup = pin_registry_lookup
        self._manifest_lookup = manifest_lookup

    def attach_preservation_scorer(
        self,
        *,
        scorer: Any,
        intent_tracker_lookup: Optional[Callable[[str], Any]] = None,
        pin_registry_lookup: Optional[Callable[[str], Any]] = None,
        manifest_lookup: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """Late-bind the scorer + its lookups. Used when the compactor is
        constructed before the preservation-stack singletons are wired.
        ``None`` on any lookup falls back to module default singletons
        inside :meth:`_partition_via_scorer`."""
        self._preservation_scorer = scorer
        if intent_tracker_lookup is not None:
            self._intent_tracker_lookup = intent_tracker_lookup
        if pin_registry_lookup is not None:
            self._pin_registry_lookup = pin_registry_lookup
        if manifest_lookup is not None:
            self._manifest_lookup = manifest_lookup

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
        *,
        op_id: Optional[str] = None,
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
        # Slice 1 Production Integration: intent-aware path when enabled
        # and fully-wired. Falls back silently to legacy path on ANY
        # missing prerequisite (op_id / scorer / env flag).
        preserved: List[Dict[str, Any]] = []
        compactable: List[Dict[str, Any]] = []
        preserved_keys: List[str] = []
        used_scorer_path = False
        if (
            op_id
            and self._preservation_scorer is not None
            and context_compactor_scorer_enabled()
        ):
            try:
                preserved, compactable, preserved_keys = \
                    self._partition_via_scorer(
                        dialogue_entries, cfg, op_id,
                    )
                used_scorer_path = True
                logger.info(
                    "[ContextCompaction] scorer path op=%s entries=%d "
                    "preserved=%d compactable=%d",
                    op_id, entries_before,
                    len(preserved), len(compactable),
                )
            except Exception as exc:  # noqa: BLE001
                # Fail-closed: never lose data. Fall back to legacy.
                logger.warning(
                    "[ContextCompaction] scorer path raised; falling back "
                    "to legacy partition: %s", exc,
                )
                preserved, compactable, preserved_keys = self._partition(
                    dialogue_entries, cfg,
                )
        else:
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

    def _partition_via_scorer(
        self,
        entries: List[Dict[str, Any]],
        config: CompactionConfig,
        op_id: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Intent-aware partition via :class:`PreservationScorer`.

        Maps each dialogue entry to a :class:`ChunkCandidate`, scores
        against the op's current intent + pin state, and partitions
        into (preserved, compactable, preserved_keys) shapes identical
        to the legacy :meth:`_partition` output so downstream code
        paths are untouched.

        The safety-critical preserve patterns from the legacy config
        still apply — chunks matching them are treated as ``pinned``
        for scoring purposes, so the new path is strictly additive to
        the existing safety floor.
        """
        # Lazy imports — the preservation stack is optional.
        from backend.core.ouroboros.governance.context_intent import (
            ChunkCandidate,
            PreservationScorer as _PScorer,
            intent_tracker_for,
        )
        from backend.core.ouroboros.governance.context_pins import (
            pin_registry_for,
        )

        tracker_lookup = self._intent_tracker_lookup or intent_tracker_for
        pin_lookup = self._pin_registry_lookup or pin_registry_for
        tracker = tracker_lookup(op_id)
        pins = pin_lookup(op_id)

        scorer = self._preservation_scorer
        if not isinstance(scorer, _PScorer):
            # Construction / wiring mismatch — caller injected something
            # that isn't a PreservationScorer. Fall through by raising;
            # compact() catches and uses legacy.
            raise TypeError(
                "preservation_scorer must be a PreservationScorer instance; "
                f"got {type(scorer).__name__}"
            )

        # Build ChunkCandidates from entries. Pins are looked up per
        # chunk_id. Safety-critical regex pins are merged into the pin
        # signal so legacy safety floor is preserved.
        compiled_safety = self._compile_patterns(config.preserve_patterns)
        intent_snapshot = tracker.current_intent()
        candidates: List[ChunkCandidate] = []
        chunk_to_entry: Dict[str, Dict[str, Any]] = {}
        for idx, entry in enumerate(entries):
            chunk_id = _deterministic_chunk_id(entry, idx)
            text = _entry_to_text(entry)
            role = str(
                entry.get("type")
                or entry.get("phase")
                or entry.get("role")
                or "unknown"
            )
            # Safety-floor: a chunk matching any preserve_pattern or
            # explicitly pinned via the pin registry is treated as
            # pinned for score purposes.
            is_pinned = (
                self._matches_any(text, compiled_safety)
                or pins.is_pinned(chunk_id)
            )
            candidates.append(ChunkCandidate(
                chunk_id=chunk_id,
                text=text,
                index_in_sequence=idx,
                role=role,
                pinned=is_pinned,
            ))
            chunk_to_entry[chunk_id] = entry

        # Preserve count → max_chunks budget for the scorer.
        result = scorer.select_preserved(
            candidates,
            intent_snapshot,
            max_chunks=config.preserve_count,
            keep_ratio=0.5,
        )

        kept_entries = [
            chunk_to_entry[s.chunk_id] for s in result.kept
        ]
        compactable_entries = [
            chunk_to_entry[s.chunk_id] for s in result.compacted
        ] + [
            chunk_to_entry[s.chunk_id] for s in result.dropped
        ]
        preserved_keys = [
            f"chunk_id={s.chunk_id}" for s in result.kept
        ]

        # Record manifest (best-effort — never block partitioning).
        try:
            manifest_lookup = self._manifest_lookup
            if manifest_lookup is None:
                from backend.core.ouroboros.governance.context_manifest import (
                    manifest_for,
                )
                manifest_lookup = manifest_for
            manifest = manifest_lookup(op_id)
            manifest.record_pass(
                preservation_result=result,
                intent_snapshot=intent_snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ContextCompaction] manifest record failed: %s", exc,
            )

        return kept_entries, compactable_entries, preserved_keys

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


def _deterministic_chunk_id(entry: Dict[str, Any], index: int) -> str:
    """Derive a stable chunk_id for a dialogue entry.

    Preference order: explicit ``id`` / ``message_id`` field → type+phase+index
    composite → index-only fallback. Deterministic so the scorer's
    per-chunk lookups are stable within a single pass.
    """
    for key in ("id", "message_id", "entry_id", "chunk_id"):
        val = entry.get(key)
        if val is not None and str(val):
            return f"chunk-{val}"
    typ = entry.get("type") or entry.get("phase") or entry.get("role") or "e"
    return f"chunk-{typ}-{index}"


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
