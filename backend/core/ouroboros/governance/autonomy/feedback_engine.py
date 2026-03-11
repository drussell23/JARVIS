"""backend/core/ouroboros/governance/autonomy/feedback_engine.py

Autonomy Feedback Engine — L2 Decision Intelligence: Curriculum Consumption.

The FeedbackEngine is the L2 "Decision Intelligence" service.  It consumes
signals (curriculum files, reactor events, canary outcomes) and emits advisory
CommandEnvelopes to L1 via the CommandBus.

This module implements the curriculum consumption path.  Tasks 5-7 will add
reactor consumption, attribution scoring, and canary/brain feedback.

**Single-writer invariant**: This module NEVER mutates op_context, ledger,
filesystem content, or trust tiers directly.  It only reads signal files and
writes advisory commands to the CommandBus plus its own cursor state.

Design:
    - Scans event_dir for ``curriculum_*.json`` files.
    - Tracks already-processed filenames in an in-memory set + on-disk cursor.
    - For each new curriculum file, parses ``top_k`` entries and creates
      ``GENERATE_BACKLOG_ENTRY`` commands on the CommandBus.
    - Cursor is persisted atomically to ``state_dir/feedback_engine_cursor.json``
      after each successful scan so that restarts skip already-seen files.
    - Malformed files are logged and skipped without crashing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

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

    async def score_attribution_once(self, persistence: Any) -> None:
        """Score model attribution for each active brain and emit events.

        For each active brain_id returned by ``persistence.get_active_brain_ids()``,
        fetches outcome records via ``persistence.get_records_by_model_and_task()``,
        computes a success rate, and emits an ``ATTRIBUTION_SCORED`` event through
        the event emitter.

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

        try:
            brain_ids = await persistence.get_active_brain_ids()
        except Exception:
            logger.warning(
                "FeedbackEngine: persistence.get_active_brain_ids() raised — "
                "skipping attribution scoring",
                exc_info=True,
            )
            return

        window_hours = _DEFAULT_ATTRIBUTION_WINDOW_HOURS

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

            success_count = sum(
                1 for r in records if r.get("outcome") == "success"
            )
            success_rate = success_count / sample_size

            event = EventEnvelope(
                source_layer="L2",
                event_type=EventType.ATTRIBUTION_SCORED,
                payload={
                    "brain_id": brain_id,
                    "success_rate": success_rate,
                    "sample_size": sample_size,
                    "window_hours": window_hours,
                },
            )

            await self._event_emitter.emit(event)

            logger.debug(
                "FeedbackEngine: attribution scored for brain_id=%s — "
                "success_rate=%.3f sample_size=%d",
                brain_id,
                success_rate,
                sample_size,
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
    # Internals — cursor persistence
    # ------------------------------------------------------------------

    def _cursor_path(self) -> Path:
        return self._config.state_dir / _CURSOR_FILENAME

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
