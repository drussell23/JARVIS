"""
Experience Recorder - Black Box for JARVIS Data Flywheel

This module provides a zero-latency async recorder that captures every user
interaction for RLHF training. The recorded data feeds into reactor-core
for continuous improvement.

Key Features:
- Fire-and-forget async recording (non-blocking)
- Daily JSONL rotation for safe reactor-core access
- LRU cache for outcome linking
- Environment-driven configuration (no hardcoding)
- Batched writes for high throughput

Integration:
    from backend.memory.experience_recorder import get_experience_recorder

    recorder = get_experience_recorder()
    await recorder.start()

    # Record via callbacks (automatic)
    agent.on("session_completed", recorder.create_agent_callbacks()["session_completed"])

    # Or manually
    record_id = await recorder.record(experience)

    # Later, when user provides feedback
    await recorder.update_outcome(record_id, outcome)

Author: JARVIS v5.0 Data Flywheel
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False

from memory.experience_types import (
    ExperienceRecord,
    Outcome,
    OutcomeSignal,
    OutcomeUpdate,
    PromptContext,
    RecorderMetrics,
    ResponseType,
    ToolCategory,
    ToolUsage,
)

logger = logging.getLogger(__name__)


@dataclass
class ExperienceConfig:
    """
    Environment-driven configuration for ExperienceRecorder.

    All settings can be overridden via environment variables,
    following the "no hardcoding" principle.
    """
    # Storage
    output_dir: Path = field(default_factory=lambda: Path(
        os.getenv(
            "JARVIS_EXPERIENCE_DIR",
            str(Path(__file__).parent.parent.parent / "data" / "memories" / "raw")
        )
    ))

    # Queue settings
    queue_max_size: int = field(default_factory=lambda: int(
        os.getenv("JARVIS_EXPERIENCE_QUEUE_SIZE", "1000")
    ))

    # Batching for efficiency
    batch_size: int = field(default_factory=lambda: int(
        os.getenv("JARVIS_EXPERIENCE_BATCH_SIZE", "50")
    ))
    flush_interval: float = field(default_factory=lambda: float(
        os.getenv("JARVIS_EXPERIENCE_FLUSH_INTERVAL", "1.0")
    ))

    # Outcome tracking
    max_pending_outcomes: int = field(default_factory=lambda: int(
        os.getenv("JARVIS_EXPERIENCE_MAX_PENDING", "500")
    ))

    # Feature flags
    enabled: bool = field(default_factory=lambda:
        os.getenv("JARVIS_EXPERIENCE_ENABLED", "true").lower() == "true"
    )

    # Debug mode
    debug: bool = field(default_factory=lambda:
        os.getenv("JARVIS_EXPERIENCE_DEBUG", "false").lower() == "true"
    )

    def __post_init__(self):
        """Ensure output directory exists."""
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls) -> "ExperienceConfig":
        """Create config from environment variables."""
        return cls()


class ExperienceRecorder:
    """
    Zero-latency async Experience Recorder for RLHF training.

    This is the "Black Box" that records JARVIS's life for reactor-core
    to learn from. Every interaction is captured with:
    - User prompt
    - Agent response
    - Tools used
    - Outcome (RLHF signal)

    Design Principles:
    - Fire-and-forget: record() returns immediately, writes in background
    - Daily rotation: reactor-core reads yesterday's data safely
    - Outcome linking: LRU cache links late feedback to records
    - Zero hardcoding: all config via environment variables

    Usage:
        recorder = ExperienceRecorder()
        await recorder.start()

        # Record automatically via callbacks, or manually:
        record_id = await recorder.record(experience)

        # Later, when user provides feedback:
        await recorder.update_outcome(record_id, outcome)

        # Graceful shutdown
        await recorder.stop()
    """

    def __init__(self, config: Optional[ExperienceConfig] = None):
        """
        Initialize the ExperienceRecorder.

        Args:
            config: Configuration (uses environment defaults if None)
        """
        self.config = config or ExperienceConfig.from_environment()

        # Async write queue for zero-latency recording
        self._write_queue: asyncio.Queue[ExperienceRecord] = asyncio.Queue(
            maxsize=self.config.queue_max_size
        )

        # Background writer task
        self._writer_task: Optional[asyncio.Task] = None
        self._running = False

        # Current file handle for daily rotation
        self._current_file: Optional[Any] = None  # aiofiles handle
        self._current_file_path: Optional[Path] = None
        self._current_date: Optional[date] = None

        # In-memory cache for outcome linking (LRU via OrderedDict)
        self._pending_outcomes: OrderedDict[str, ExperienceRecord] = OrderedDict()

        # Metrics for monitoring
        self._metrics = RecorderMetrics()

        # Write timing for batch optimization
        self._last_write_time: float = 0.0
        self._write_times: List[float] = []

        # Callbacks for status updates
        self._callbacks: List[Callable] = []

        logger.info(
            f"[EXPERIENCE-RECORDER] Initialized (enabled={self.config.enabled}, "
            f"output={self.config.output_dir})"
        )

    async def start(self) -> None:
        """Start the background writer task."""
        if not self.config.enabled:
            logger.info("[EXPERIENCE-RECORDER] Disabled via config, not starting")
            return

        if self._running:
            logger.debug("[EXPERIENCE-RECORDER] Already running")
            return

        self._running = True
        self._writer_task = asyncio.create_task(self._write_loop())

        logger.info(
            f"[EXPERIENCE-RECORDER] Started with output dir: {self.config.output_dir}"
        )

    async def stop(self) -> None:
        """Gracefully stop the recorder, flushing pending writes."""
        if not self._running:
            return

        logger.info("[EXPERIENCE-RECORDER] Stopping...")
        self._running = False

        # Flush remaining records
        await self._flush_queue()

        # Cancel writer task
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        # Close file handle
        await self._close_current_file()

        logger.info(
            f"[EXPERIENCE-RECORDER] Stopped. "
            f"Total written: {self._metrics.records_written}"
        )

    async def record(self, experience: ExperienceRecord) -> str:
        """
        Fire-and-forget record. Returns immediately.

        The record is queued for async background writing.
        Returns the record_id for later outcome linking.

        Args:
            experience: The interaction record to save

        Returns:
            record_id for later outcome linking
        """
        if not self.config.enabled:
            return experience.record_id

        # Ensure record has an ID
        if not experience.record_id:
            experience.record_id = str(uuid4())

        # Add to pending outcomes cache (for late feedback linking)
        self._add_to_pending(experience)

        # Non-blocking queue put (fire-and-forget)
        try:
            self._write_queue.put_nowait(experience)
            self._metrics.records_queued += 1
            self._metrics.queue_depth = self._write_queue.qsize()

            if self.config.debug:
                logger.debug(
                    f"[EXPERIENCE-RECORDER] Queued record {experience.record_id[:8]}... "
                    f"(queue={self._metrics.queue_depth})"
                )

        except asyncio.QueueFull:
            # Drop oldest to make room (better than blocking)
            logger.warning("[EXPERIENCE-RECORDER] Queue full, dropping oldest record")
            try:
                await asyncio.wait_for(self._write_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            self._write_queue.put_nowait(experience)
            self._metrics.records_dropped += 1

        return experience.record_id

    async def update_outcome(
        self,
        record_id: str,
        outcome: Outcome
    ) -> bool:
        """
        Update an existing record with outcome data (RLHF signal).

        If the record is still in the pending cache, updates in-memory
        before write. If already written, appends an outcome_update
        record to the JSONL file.

        Args:
            record_id: The record to update
            outcome: The RLHF feedback signal

        Returns:
            True if update succeeded
        """
        if not self.config.enabled:
            return False

        # Calculate latency if not set
        if outcome.latency_to_feedback_ms is None:
            if record_id in self._pending_outcomes:
                original = self._pending_outcomes[record_id]
                latency = (datetime.now() - original.timestamp).total_seconds() * 1000
                outcome.latency_to_feedback_ms = latency

        # Try to update in pending cache first
        if record_id in self._pending_outcomes:
            record = self._pending_outcomes[record_id]
            record.outcome = outcome
            record.outcome_timestamp = datetime.now()
            self._metrics.outcomes_linked += 1

            logger.debug(
                f"[EXPERIENCE-RECORDER] Linked outcome to pending record "
                f"{record_id[:8]}... ({outcome.signal.value})"
            )
            return True

        # Record already written - append outcome update
        update = OutcomeUpdate(
            record_id=record_id,
            outcome=outcome,
            timestamp=datetime.now()
        )

        await self._append_to_file(update.to_jsonl())
        self._metrics.late_outcomes += 1

        logger.debug(
            f"[EXPERIENCE-RECORDER] Late outcome update for {record_id[:8]}... "
            f"({outcome.signal.value})"
        )
        return True

    def _add_to_pending(self, record: ExperienceRecord) -> None:
        """Add record to pending outcomes cache (LRU)."""
        # Remove oldest if at capacity
        while len(self._pending_outcomes) >= self.config.max_pending_outcomes:
            self._pending_outcomes.popitem(last=False)

        self._pending_outcomes[record.record_id] = record
        self._metrics.pending_outcomes_count = len(self._pending_outcomes)

    async def _write_loop(self) -> None:
        """Background loop that writes records to JSONL."""
        while self._running or not self._write_queue.empty():
            try:
                # Batch writes for efficiency
                batch = await self._get_batch()

                if batch:
                    write_start = time.time()
                    await self._write_batch(batch)
                    write_time = (time.time() - write_start) * 1000

                    # Track write times for metrics
                    self._write_times.append(write_time)
                    if len(self._write_times) > 100:
                        self._write_times = self._write_times[-100:]
                    self._metrics.avg_write_time_ms = sum(self._write_times) / len(self._write_times)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._metrics.write_errors += 1
                self._metrics.last_error = str(e)
                logger.error(f"[EXPERIENCE-RECORDER] Write error: {e}")
                await asyncio.sleep(1.0)

    async def _get_batch(self) -> List[ExperienceRecord]:
        """Get a batch of records from the queue."""
        batch: List[ExperienceRecord] = []

        try:
            # Wait for first record (with timeout for flush interval)
            first = await asyncio.wait_for(
                self._write_queue.get(),
                timeout=self.config.flush_interval
            )
            batch.append(first)

            # Grab more if available (up to batch size)
            while len(batch) < self.config.batch_size:
                try:
                    record = self._write_queue.get_nowait()
                    batch.append(record)
                except asyncio.QueueEmpty:
                    break

        except asyncio.TimeoutError:
            # No records in flush interval - that's fine
            pass

        self._metrics.queue_depth = self._write_queue.qsize()
        return batch

    async def _write_batch(self, records: List[ExperienceRecord]) -> None:
        """Write a batch of records to the current day's JSONL file."""
        # Check for daily rotation
        today = date.today()
        if self._current_date != today:
            await self._rotate_file(today)

        # Serialize and write
        lines = [record.to_jsonl() for record in records]
        content = '\n'.join(lines) + '\n'

        await self._append_to_file(content)

        # Remove from pending cache (they're now written)
        for record in records:
            self._pending_outcomes.pop(record.record_id, None)

        self._metrics.records_written += len(records)
        self._metrics.pending_outcomes_count = len(self._pending_outcomes)

        if self.config.debug:
            logger.debug(
                f"[EXPERIENCE-RECORDER] Wrote {len(records)} records to "
                f"{self._current_file_path.name if self._current_file_path else 'unknown'}"
            )

    async def _append_to_file(self, content: str) -> None:
        """Append content to the current file."""
        if not self._current_file:
            await self._rotate_file(date.today())

        if AIOFILES_AVAILABLE and self._current_file:
            await self._current_file.write(content)
            await self._current_file.flush()
        elif self._current_file_path:
            # Fallback to sync write if aiofiles not available
            with open(self._current_file_path, 'a') as f:
                f.write(content)

    async def _rotate_file(self, new_date: date) -> None:
        """Rotate to a new daily file."""
        await self._close_current_file()

        # File naming: YYYY-MM-DD_experiences.jsonl
        filename = f"{new_date.isoformat()}_experiences.jsonl"
        self._current_file_path = self.config.output_dir / filename

        if AIOFILES_AVAILABLE:
            self._current_file = await aiofiles.open(
                self._current_file_path,
                mode='a'
            )
        else:
            # Will use sync fallback in _append_to_file
            self._current_file = None

        self._current_date = new_date
        self._metrics.current_file = str(self._current_file_path)
        self._metrics.files_rotated += 1

        logger.info(f"[EXPERIENCE-RECORDER] Rotated to: {filename}")

    async def _close_current_file(self) -> None:
        """Close the current file handle."""
        if self._current_file and AIOFILES_AVAILABLE:
            try:
                await self._current_file.close()
            except Exception as e:
                logger.debug(f"[EXPERIENCE-RECORDER] Error closing file: {e}")
        self._current_file = None

    async def _flush_queue(self) -> None:
        """Flush all remaining records from the queue."""
        remaining = []
        while not self._write_queue.empty():
            try:
                record = self._write_queue.get_nowait()
                remaining.append(record)
            except asyncio.QueueEmpty:
                break

        if remaining:
            await self._write_batch(remaining)
            logger.debug(f"[EXPERIENCE-RECORDER] Flushed {len(remaining)} records")

    # =========================================================================
    # Integration Hooks
    # =========================================================================

    def create_agent_callbacks(self) -> Dict[str, Callable]:
        """
        Create callbacks compatible with AutonomousAgent.on() system.

        Usage:
            callbacks = recorder.create_agent_callbacks()
            for event, callback in callbacks.items():
                agent.on(event, callback)

        Returns:
            Dict of event_name -> callback_function
        """
        return {
            "session_completed": self._on_session_completed,
            "action_executed": self._on_action_executed,
            "error": self._on_error,
        }

    async def _on_session_completed(self, data: Dict[str, Any]) -> None:
        """Callback for AutonomousAgent session completion."""
        record = self._create_record_from_session(data)
        await self.record(record)

    async def _on_action_executed(self, data: Dict[str, Any]) -> None:
        """Callback for action execution (optional granular tracking)."""
        # This is optional - session_completed captures most data
        # Can be used for detailed tool-level tracking
        if self.config.debug:
            logger.debug(f"[EXPERIENCE-RECORDER] Action executed: {data.get('action', 'unknown')}")

    async def _on_error(self, data: Dict[str, Any]) -> None:
        """Callback for errors during agent execution."""
        record = ExperienceRecord(
            session_id=data.get("session_id", ""),
            timestamp=datetime.now(),
            user_prompt=data.get("goal", ""),
            agent_response=f"Error: {data.get('error', 'Unknown error')}",
            response_type=ResponseType.ERROR,
            execution_time_ms=data.get("execution_time_ms", 0),
            metadata={
                "source": "autonomous_agent",
                "error": str(data.get("error", "")),
                "traceback": data.get("traceback", "")
            }
        )
        await self.record(record)

    def _create_record_from_session(self, data: Dict[str, Any]) -> ExperienceRecord:
        """Create ExperienceRecord from session completion data."""
        # Extract tools used
        tools_used = []
        for tool_data in data.get("tools_used", []):
            tool = ToolUsage(
                tool_name=tool_data.get("name", "unknown"),
                category=self._categorize_tool(tool_data.get("name", "")),
                parameters=tool_data.get("parameters", {}),
                result=tool_data.get("result"),
                success=tool_data.get("success", True),
                execution_time_ms=tool_data.get("execution_time_ms", 0)
            )
            tools_used.append(tool)

        # Build context
        context = PromptContext(
            active_app=data.get("context", {}).get("active_app"),
            screen_locked=data.get("context", {}).get("screen_locked", False),
            previous_interactions=data.get("context", {}).get("interaction_count", 0),
            extra=data.get("context", {})
        )

        return ExperienceRecord(
            session_id=data.get("session_id", ""),
            timestamp=datetime.now(),
            user_prompt=data.get("goal", ""),
            prompt_context=context,
            agent_response=data.get("response", ""),
            response_type=self._determine_response_type(data),
            confidence=data.get("confidence", 0.0),
            tools_used=tools_used,
            reasoning_trace=data.get("reasoning_trace", []),
            execution_time_ms=data.get("execution_time_ms", 0),
            model_name=data.get("model", os.getenv("JARVIS_MODEL", "unknown")),
            token_count=data.get("token_count"),
            metadata={
                "source": "autonomous_agent",
                "success": data.get("success", False),
                "mode": data.get("mode", "unknown")
            }
        )

    def _categorize_tool(self, tool_name: str) -> ToolCategory:
        """Categorize a tool by name."""
        tool_lower = tool_name.lower()

        if any(k in tool_lower for k in ["unlock", "lock", "screen", "app", "window"]):
            return ToolCategory.SYSTEM_CONTROL
        elif any(k in tool_lower for k in ["speak", "say", "tts", "stt", "voice", "listen"]):
            return ToolCategory.VOICE
        elif any(k in tool_lower for k in ["search", "find", "query", "lookup"]):
            return ToolCategory.SEARCH
        elif any(k in tool_lower for k in ["automate", "execute", "run", "script"]):
            return ToolCategory.AUTOMATION
        elif any(k in tool_lower for k in ["memory", "remember", "recall", "forget"]):
            return ToolCategory.MEMORY
        elif any(k in tool_lower for k in ["vision", "analyze", "understand", "read"]):
            return ToolCategory.ANALYSIS
        elif any(k in tool_lower for k in ["message", "email", "notify", "send"]):
            return ToolCategory.COMMUNICATION
        else:
            return ToolCategory.OTHER

    def _determine_response_type(self, data: Dict[str, Any]) -> ResponseType:
        """Determine response type from session data."""
        has_voice = bool(data.get("spoken", False) or data.get("response"))
        has_action = bool(data.get("tools_used"))

        if has_voice and has_action:
            return ResponseType.BOTH
        elif has_voice:
            return ResponseType.VOICE
        elif has_action:
            return ResponseType.ACTION
        else:
            return ResponseType.VOICE  # Default to voice

    # =========================================================================
    # API Methods
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get current recorder metrics."""
        return self._metrics.to_dict()

    def get_safe_files(self) -> List[Dict[str, Any]]:
        """
        Get list of JSONL files safe for reactor-core consumption.

        Returns files from previous days (not today's active file).
        """
        today = date.today().isoformat()
        files = []

        for path in self.config.output_dir.glob("*.jsonl"):
            # Exclude today's file (being actively written)
            if not path.name.startswith(today):
                stat = path.stat()
                files.append({
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "date": path.name.split("_")[0]  # Extract date from filename
                })

        return sorted(files, key=lambda x: x["filename"], reverse=True)

    @property
    def is_running(self) -> bool:
        """Check if recorder is running."""
        return self._running

    @property
    def is_enabled(self) -> bool:
        """Check if recorder is enabled."""
        return self.config.enabled


# =============================================================================
# Singleton Pattern
# =============================================================================

_recorder: Optional[ExperienceRecorder] = None
_recorder_lock = asyncio.Lock()


def get_experience_recorder(
    config: Optional[ExperienceConfig] = None
) -> ExperienceRecorder:
    """
    Get the global ExperienceRecorder singleton.

    Thread-safe singleton pattern for consistent recording
    across all parts of JARVIS.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        ExperienceRecorder instance
    """
    global _recorder

    if _recorder is None:
        _recorder = ExperienceRecorder(config)

    return _recorder


async def get_experience_recorder_async(
    config: Optional[ExperienceConfig] = None
) -> ExperienceRecorder:
    """
    Get the global ExperienceRecorder singleton (async version).

    Ensures thread-safe initialization in async context.
    """
    global _recorder

    async with _recorder_lock:
        if _recorder is None:
            _recorder = ExperienceRecorder(config)

    return _recorder
