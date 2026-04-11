"""backend/core/ouroboros/governance/autonomy/feedback_engine.py

Autonomy Feedback Engine — L2 Decision Intelligence: Curriculum & Reactor Consumption.

The FeedbackEngine is the L2 "Decision Intelligence" service.  It consumes
signals (curriculum files, reactor events, canary outcomes) and emits advisory
CommandEnvelopes to L1 via the CommandBus.

This module implements the curriculum consumption path and reactor event
consumption path.  Task 7 will add canary/brain feedback.

**Single-writer invariant**: This module NEVER mutates op_context, ledger,
filesystem content, or trust tiers directly.  It only reads signal files and
writes advisory commands to the CommandBus plus its own cursor state.

Design:
    - Scans event_dir for ``curriculum_*.json`` and ``reactor_*.json`` files.
    - Tracks already-processed filenames in an in-memory set + on-disk cursor.
    - For each new curriculum file, parses ``top_k`` entries and creates
      ``GENERATE_BACKLOG_ENTRY`` commands on the CommandBus.
    - For each new reactor file, parses the event and creates
      ``GENERATE_BACKLOG_ENTRY`` commands for supported event types
      (currently: ``model_promoted``).  Unknown event types are silently
      ignored (debug-logged).
    - Cursor is persisted atomically to ``state_dir/feedback_engine_cursor.json``
      after each successful scan so that restarts skip already-seen files.
    - Malformed files are logged and skipped without crashing.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback

logger = logging.getLogger(__name__)

# Default TTL for curriculum-generated backlog commands (30 minutes).
_DEFAULT_CMD_TTL_S: float = 1800.0

# Cursor filename (lives in state_dir).
_CURSOR_FILENAME: str = "feedback_engine_cursor.json"

# Minimum number of records required to score a brain's attribution.
_MIN_ATTRIBUTION_SAMPLE_SIZE: int = 3

# Default time window (hours) for attribution record queries.
_DEFAULT_ATTRIBUTION_WINDOW_HOURS: float = 24.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class FeedbackEngineConfig:
    """Configuration for :class:`AutonomyFeedbackEngine`.

    Parameters
    ----------
    event_dir:
        Directory where curriculum / reactor JSON signal files are written.
    state_dir:
        Directory where the engine persists its cursor (seen-files state).
    max_backlog_entries_per_curriculum:
        Maximum number of ``top_k`` entries consumed from a single curriculum
        file.  Excess entries are silently dropped.
    attribution_interval_s:
        Interval in seconds between attribution scoring runs (future use).
    """

    event_dir: Path
    state_dir: Path
    max_backlog_entries_per_curriculum: int = 5
    attribution_interval_s: float = 1800.0


# ---------------------------------------------------------------------------
# AutonomyFeedbackEngine
# ---------------------------------------------------------------------------


class AutonomyFeedbackEngine:
    """L2 Decision Intelligence: consumes curriculum signals and emits advisory commands.

    Parameters
    ----------
    command_bus:
        The L1 CommandBus to put generated commands onto.
    config:
        Engine configuration (directories, limits).
    event_emitter:
        Optional L1 EventEmitter for subscribing to outcome events (future use).
    """

    def __init__(
        self,
        command_bus: CommandBus,
        config: FeedbackEngineConfig,
        event_emitter: Optional[Any] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config
        self._event_emitter = event_emitter
        self._seen_files: Set[str] = set()

        # Canary -> brain feedback tracking (Task 7)
        self._rollback_counts: Dict[str, int] = defaultdict(int)
        self._brain_hint_threshold: int = 3

        # Selection engine for scoring and ranking brain candidates
        try:
            from backend.core.ouroboros.governance.autonomy.selection_strategies import (
                SelectionEngine,
                SelectionStrategy,
            )
            self._selection_engine = SelectionEngine(default_strategy=SelectionStrategy.TOURNAMENT)
        except ImportError:
            self._selection_engine = None

        # Load persisted cursor on construction
        self._load_cursor()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def consume_curriculum_once(self) -> int:
        """Scan ``event_dir`` for new curriculum files and emit backlog commands.

        Returns the total number of commands successfully put onto the bus.

        Already-seen files (from previous calls or a prior engine instance's
        persisted cursor) are skipped.  The cursor is persisted after scanning.
        """
        event_dir = self._config.event_dir
        if not event_dir.is_dir():
            logger.warning(
                "FeedbackEngine: event_dir does not exist: %s", event_dir,
            )
            return 0

        # Discover curriculum files
        curriculum_files = sorted(event_dir.glob("curriculum_*.json"))

        total_emitted = 0

        for curriculum_path in curriculum_files:
            filename = curriculum_path.name

            if filename in self._seen_files:
                logger.debug(
                    "FeedbackEngine: skipping already-seen file %s", filename,
                )
                continue

            emitted = self._process_curriculum_file(curriculum_path)
            total_emitted += emitted

            # Mark as seen regardless of whether parsing succeeded — we do not
            # want to retry a permanently-malformed file every scan cycle.
            self._seen_files.add(filename)

        # Persist cursor after the full scan
        self._persist_cursor()

        if total_emitted > 0:
            logger.info(
                "FeedbackEngine: curriculum scan complete — %d command(s) emitted",
                total_emitted,
            )

        return total_emitted

    async def consume_reactor_events_once(self) -> int:
        """Scan ``event_dir`` for new reactor event files and emit backlog commands.

        Returns the total number of commands successfully put onto the bus.

        Supported reactor event types:

        - ``model_promoted``: A newly-promoted model from the JARVIS-Reactor
          training pipeline.  Generates a ``GENERATE_BACKLOG_ENTRY`` command so
          that L1 can schedule validation work for the new model.

        Unknown event types are silently ignored (debug-logged).  Malformed
        files are skipped without crashing.  The cursor is persisted after
        scanning so that restarts skip already-seen files.
        """
        event_dir = self._config.event_dir
        if not event_dir.is_dir():
            logger.warning(
                "FeedbackEngine: event_dir does not exist: %s", event_dir,
            )
            return 0

        # Discover reactor files
        reactor_files = sorted(event_dir.glob("reactor_*.json"))

        total_emitted = 0

        for reactor_path in reactor_files:
            filename = reactor_path.name

            if filename in self._seen_files:
                logger.debug(
                    "FeedbackEngine: skipping already-seen reactor file %s",
                    filename,
                )
                continue

            emitted = self._process_reactor_file(reactor_path)
            total_emitted += emitted

            # Mark as seen regardless of whether parsing succeeded — we do not
            # want to retry a permanently-malformed file every scan cycle.
            self._seen_files.add(filename)

        # Persist cursor after the full scan
        self._persist_cursor()

        if total_emitted > 0:
            logger.info(
                "FeedbackEngine: reactor scan complete — %d command(s) emitted",
                total_emitted,
            )

        return total_emitted

    async def score_attribution_once(self, persistence: Any) -> None:
        """Score model attribution for each active brain and emit events.

        Supports two persistence shapes:

        - legacy test/dummy shape:
          ``get_active_brain_ids()`` + ``get_records_by_model_and_task()``
        - real PerformanceRecordPersistence shape:
          ``load_records(model_id=None, limit=N, since=datetime)``

        Computes success rate and average quality score, then emits an
        ``ATTRIBUTION_SCORED`` event through the event emitter.

        Parameters
        ----------
        persistence:
            Duck-typed object with async methods:
            - ``get_active_brain_ids() -> List[str]``
            - ``get_records_by_model_and_task(brain_id, window_hours) -> List[dict]``

        Fault isolation:
            If *persistence* raises at any point, the error is logged as a
            warning and does not propagate.  Individual brain failures do not
            prevent scoring of other brains.
        """
        if self._event_emitter is None:
            return

        window_hours = _DEFAULT_ATTRIBUTION_WINDOW_HOURS
        try:
            records_by_brain = await self._load_attribution_records(
                persistence,
                window_hours=window_hours,
            )
        except Exception:
            logger.warning(
                "FeedbackEngine: failed to load attribution records — "
                "skipping attribution scoring",
                exc_info=True,
            )
            return

        for brain_id, records in records_by_brain.items():
            sample_size = len(records)
            if sample_size < _MIN_ATTRIBUTION_SAMPLE_SIZE:
                logger.debug(
                    "FeedbackEngine: brain_id=%s has %d record(s), below "
                    "minimum %d — skipping attribution",
                    brain_id,
                    sample_size,
                    _MIN_ATTRIBUTION_SAMPLE_SIZE,
                )
                continue

            success_count = sum(1 for r in records if self._record_success(r))
            success_rate = success_count / sample_size
            avg_quality_score = sum(self._record_quality_score(r) for r in records) / sample_size

            event = EventEnvelope(
                source_layer="L2",
                event_type=EventType.ATTRIBUTION_SCORED,
                payload={
                    "brain_id": brain_id,
                    "success_rate": success_rate,
                    "avg_quality_score": avg_quality_score,
                    "sample_size": sample_size,
                    "window_hours": window_hours,
                },
            )

            await self._event_emitter.emit(event)

            logger.debug(
                "FeedbackEngine: attribution scored for brain_id=%s — "
                "success_rate=%.3f quality=%.3f sample_size=%d",
                brain_id,
                success_rate,
                avg_quality_score,
                sample_size,
            )

    async def _load_attribution_records(
        self,
        persistence: Any,
        *,
        window_hours: float,
    ) -> Dict[str, List[Any]]:
        if hasattr(persistence, "load_records"):
            since = datetime.now(tz=timezone.utc) - timedelta(hours=window_hours)
            loaded = await persistence.load_records(limit=200, since=since)
            return {
                str(brain_id): list(records)
                for brain_id, records in loaded.items()
            }

        try:
            brain_ids = await persistence.get_active_brain_ids()
        except Exception:
            logger.warning(
                "FeedbackEngine: persistence.get_active_brain_ids() raised — "
                "skipping attribution scoring",
                exc_info=True,
            )
            return {}

        records_by_brain: Dict[str, List[Any]] = {}
        for brain_id in brain_ids:
            try:
                records = await persistence.get_records_by_model_and_task(
                    brain_id,
                    window_hours=window_hours,
                )
            except Exception:
                logger.warning(
                    "FeedbackEngine: failed to fetch records for brain_id=%s — "
                    "skipping",
                    brain_id,
                    exc_info=True,
                )
                continue
            records_by_brain[str(brain_id)] = list(records)
        return records_by_brain

    @staticmethod
    def _record_success(record: Any) -> bool:
        if isinstance(record, dict):
            if "success" in record:
                return bool(record["success"])
            return str(record.get("outcome", "")).lower() == "success"
        if hasattr(record, "success"):
            return bool(getattr(record, "success"))
        return str(getattr(record, "outcome", "")).lower() == "success"

    @staticmethod
    def _record_quality_score(record: Any) -> float:
        if isinstance(record, dict):
            value = record.get("avg_quality_score", record.get("quality_score", record.get("code_quality_score", 0.0)))
        else:
            value = getattr(
                record,
                "code_quality_score",
                getattr(record, "quality_score", getattr(record, "avg_quality_score", 0.0)),
            )
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Public API — canary -> brain feedback (Task 7)
    # ------------------------------------------------------------------

    def register_event_handlers(self, emitter: Any) -> None:
        """Subscribe to L1 outcome events for canary -> brain feedback.

        Subscribes to:
        - ``OP_ROLLED_BACK`` -> :meth:`_on_op_rolled_back`
        - ``OP_COMPLETED`` -> :meth:`_on_op_completed`

        Parameters
        ----------
        emitter:
            An :class:`EventEmitter` (or duck-typed equivalent with a
            ``subscribe(event_type, handler)`` method).
        """
        emitter.subscribe(EventType.OP_ROLLED_BACK, self._on_op_rolled_back)
        emitter.subscribe(EventType.OP_COMPLETED, self._on_op_completed)
        logger.info(
            "FeedbackEngine: registered canary->brain event handlers "
            "(threshold=%d)",
            self._brain_hint_threshold,
        )

    def _on_op_rolled_back(self, event: EventEnvelope) -> None:
        """Handle an ``OP_ROLLED_BACK`` event (sync handler).

        Increments the rollback count for the brain identified in the event
        payload.  When the count reaches the threshold, emits an
        ``ADJUST_BRAIN_HINT`` command advising L1 to reduce the brain's
        routing weight.
        """
        brain_id: str = event.payload.get("brain_id", "")
        if not brain_id:
            logger.warning(
                "FeedbackEngine: OP_ROLLED_BACK event missing brain_id — "
                "ignoring (event_id=%s)",
                event.event_id,
            )
            return

        self._rollback_counts[brain_id] += 1
        count = self._rollback_counts[brain_id]

        logger.debug(
            "FeedbackEngine: rollback count for brain_id=%s is now %d "
            "(threshold=%d)",
            brain_id,
            count,
            self._brain_hint_threshold,
        )

        if count >= self._brain_hint_threshold and count % self._brain_hint_threshold == 0:
            cmd = CommandEnvelope(
                source_layer="L2",
                target_layer="L1",
                command_type=CommandType.ADJUST_BRAIN_HINT,
                payload={
                    "brain_id": brain_id,
                    "weight_delta": -0.1,
                    "evidence_window_ops": count,
                    "canary_slice": "tests/",
                    "reason": (
                        f"Brain {brain_id!r} has accumulated {count} rollback(s) "
                        f"(threshold={self._brain_hint_threshold}) — "
                        f"advising weight reduction"
                    ),
                },
                ttl_s=_DEFAULT_CMD_TTL_S,
            )

            accepted = self._bus.try_put(cmd)
            if accepted:
                logger.info(
                    "FeedbackEngine: emitted ADJUST_BRAIN_HINT for brain_id=%s "
                    "(rollback_count=%d)",
                    brain_id,
                    count,
                )
            else:
                logger.warning(
                    "FeedbackEngine: bus rejected ADJUST_BRAIN_HINT for "
                    "brain_id=%s (dedup or backpressure)",
                    brain_id,
                )

    def _on_op_completed(self, event: EventEnvelope) -> None:
        """Handle an ``OP_COMPLETED`` event (sync handler).

        Decays the rollback count for the brain identified in the event
        payload by 1, flooring at zero.  This rewards successful operations
        and prevents stale rollback counts from triggering hints
        indefinitely.
        """
        brain_id: str = event.payload.get("brain_id", "")
        if not brain_id:
            logger.debug(
                "FeedbackEngine: OP_COMPLETED event missing brain_id — "
                "ignoring (event_id=%s)",
                event.event_id,
            )
            return

        current = self._rollback_counts.get(brain_id, 0)
        self._rollback_counts[brain_id] = max(0, current - 1)

        logger.debug(
            "FeedbackEngine: decayed rollback count for brain_id=%s: %d -> %d",
            brain_id,
            current,
            self._rollback_counts[brain_id],
        )

    # ------------------------------------------------------------------
    # Internals — curriculum processing
    # ------------------------------------------------------------------

    def _process_curriculum_file(self, path: Path) -> int:
        """Parse a single curriculum file and put commands on the bus.

        Returns the count of commands successfully enqueued.
        """
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "FeedbackEngine: failed to parse curriculum file %s: %s",
                path.name,
                exc,
            )
            return 0

        if not isinstance(data, dict):
            logger.warning(
                "FeedbackEngine: curriculum file %s is not a JSON object", path.name,
            )
            return 0

        top_k: List[Dict[str, Any]] = data.get("top_k", [])
        if not isinstance(top_k, list):
            logger.warning(
                "FeedbackEngine: curriculum file %s has non-list top_k", path.name,
            )
            return 0

        max_entries = self._config.max_backlog_entries_per_curriculum
        emitted = 0

        for entry in top_k[:max_entries]:
            if not isinstance(entry, dict):
                logger.debug(
                    "FeedbackEngine: skipping non-dict entry in %s", path.name,
                )
                continue

            cmd = self._build_backlog_command(entry, source_curriculum_id=path.name)
            accepted = self._bus.try_put(cmd)
            if accepted:
                emitted += 1
            else:
                logger.debug(
                    "FeedbackEngine: bus rejected command for %s (dedup or backpressure)",
                    entry.get("task_type", "<unknown>"),
                )

        return emitted

    @staticmethod
    def _build_backlog_command(
        entry: Dict[str, Any],
        source_curriculum_id: str,
    ) -> CommandEnvelope:
        """Create a GENERATE_BACKLOG_ENTRY command from a curriculum top_k entry."""
        payload: Dict[str, Any] = {
            "task_type": entry.get("task_type", "unknown"),
            "priority": entry.get("priority", 3),
            "failure_rate": entry.get("failure_rate", 0.0),
            "source_curriculum_id": source_curriculum_id,
            "description": entry.get("description", ""),
            "target_files": entry.get("target_files", []),
            "repo": entry.get("repo", "jarvis"),
        }

        return CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload=payload,
            ttl_s=_DEFAULT_CMD_TTL_S,
        )

    # ------------------------------------------------------------------
    # Internals — reactor processing
    # ------------------------------------------------------------------

    # Reactor event types that this engine knows how to handle.
    _SUPPORTED_REACTOR_EVENTS = frozenset({"model_promoted"})

    def _process_reactor_file(self, path: Path) -> int:
        """Parse a single reactor event file and put commands on the bus.

        Returns the count of commands successfully enqueued (0 or 1).
        """
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "FeedbackEngine: failed to parse reactor file %s: %s",
                path.name,
                exc,
            )
            return 0

        if not isinstance(data, dict):
            logger.warning(
                "FeedbackEngine: reactor file %s is not a JSON object",
                path.name,
            )
            return 0

        event_type = data.get("event_type", "")

        if event_type not in self._SUPPORTED_REACTOR_EVENTS:
            logger.debug(
                "FeedbackEngine: ignoring unknown reactor event_type=%r in %s",
                event_type,
                path.name,
            )
            return 0

        # Dispatch by event type
        if event_type == "model_promoted":
            return self._handle_model_promoted(data, source_file=path.name)

        return 0  # pragma: no cover — defensive fallback

    def _handle_model_promoted(
        self,
        data: Dict[str, Any],
        source_file: str,
    ) -> int:
        """Handle a ``model_promoted`` reactor event.

        Creates a ``GENERATE_BACKLOG_ENTRY`` command so that L1 can schedule
        validation work for the newly promoted model.

        Returns 1 if the command was accepted, 0 otherwise.
        """
        payload: Dict[str, Any] = {
            "description": data.get("description", ""),
            "task_type": "code_improvement",
            "source_event": "model_promoted",
            "model_id": data.get("model_id", ""),
            "previous_model_id": data.get("previous_model_id", ""),
            "target_files": data.get("target_files", []),
            "repo": data.get("repo", "jarvis"),
        }

        cmd = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload=payload,
            ttl_s=_DEFAULT_CMD_TTL_S,
        )

        accepted = self._bus.try_put(cmd)
        if accepted:
            logger.debug(
                "FeedbackEngine: reactor model_promoted command enqueued "
                "(model_id=%s, source=%s)",
                data.get("model_id", "<unknown>"),
                source_file,
            )
            return 1

        logger.debug(
            "FeedbackEngine: bus rejected reactor command from %s "
            "(dedup or backpressure)",
            source_file,
        )
        return 0

    # ------------------------------------------------------------------
    # Internals — cursor persistence
    # ------------------------------------------------------------------

    def _cursor_path(self) -> Path:
        # Iron Gate compliance: sandbox_fallback catches PermissionError on
        # the state_dir and routes to .ouroboros/state/sandbox_fallback/.
        primary = self._config.state_dir / _CURSOR_FILENAME
        return sandbox_fallback(primary)

    def _load_cursor(self) -> None:
        """Load the persisted cursor from disk, populating ``_seen_files``."""
        cursor_path = self._cursor_path()
        if not cursor_path.exists():
            return

        try:
            raw = cursor_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "FeedbackEngine: corrupted cursor file %s, starting fresh: %s",
                cursor_path,
                exc,
            )
            return

        if isinstance(data, dict):
            seen = data.get("seen_files", [])
            if isinstance(seen, list):
                self._seen_files = set(seen)
                logger.info(
                    "FeedbackEngine: loaded cursor with %d seen file(s)",
                    len(self._seen_files),
                )

    def _persist_cursor(self) -> None:
        """Atomically write the cursor to disk."""
        cursor_path = self._cursor_path()

        # Ensure state_dir exists
        cursor_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "seen_files": sorted(self._seen_files),
        }

        # Write to a temp file then rename for atomicity
        tmp_path = cursor_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(cursor_path)
        except OSError as exc:
            logger.error(
                "FeedbackEngine: failed to persist cursor to %s: %s",
                cursor_path,
                exc,
            )
