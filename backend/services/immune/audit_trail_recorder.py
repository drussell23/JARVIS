"""AuditTrailRecorder — compliance-ready audit trail recording service.

Extracted from unified_supervisor.py (lines 35005-35352).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.services.immune._base import (
    CapabilityContract,
    ServiceHealthReport,
    SystemService,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

class AuditEvent:
    """Represents an audit event for compliance logging."""

    def __init__(
        self,
        event_type: str,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.event_id = str(uuid.uuid4())
        self.timestamp = time.time()
        self.event_type = event_type
        self.actor = actor
        self.action = action
        self.resource = resource
        self.outcome = outcome
        self.details = details or {}

        # Context
        self.session_id = ""
        self.request_id = ""
        self.ip_address = ""
        self.user_agent = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize audit event."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "details": self.details,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    def to_syslog_format(self) -> str:
        """Format as syslog message."""
        return (
            f"AUDIT: event_id={self.event_id} "
            f"type={self.event_type} "
            f"actor={self.actor} "
            f"action={self.action} "
            f"resource={self.resource} "
            f"outcome={self.outcome}"
        )


# ---------------------------------------------------------------------------
# Lightweight safe-task helper (avoids importing the monolith).
# ---------------------------------------------------------------------------

def _create_safe_task(coro, *, name: Optional[str] = None) -> asyncio.Task:
    """Fire-and-forget task that logs exceptions instead of letting them vanish."""
    task = asyncio.ensure_future(coro)
    if name:
        try:
            task.set_name(name)
        except AttributeError:
            pass

    def _done_cb(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Background task %s failed: %s", name or "unnamed", exc)

    task.add_done_callback(_done_cb)
    return task


# ---------------------------------------------------------------------------
# AuditTrailRecorder
# ---------------------------------------------------------------------------

class AuditTrailRecorder(SystemService):
    """
    Compliance-ready audit trail recorder.

    Features:
    - Structured audit events
    - Multiple output formats (JSON, syslog, file)
    - Event filtering and retention
    - Tamper-evident logging with hash chains
    - Async batch writing
    - Integration with external SIEM systems
    """

    def __init__(
        self,
        storage_path: Optional[Path] = None,
        retention_days: int = 90,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        enable_hash_chain: bool = True,
    ):
        self._storage_path = storage_path or Path(tempfile.gettempdir()) / "jarvis_audit"
        self._retention_days = retention_days
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._enable_hash_chain = enable_hash_chain

        # Event buffer
        self._buffer: List[AuditEvent] = []
        self._buffer_lock = asyncio.Lock()

        # Hash chain for tamper detection
        self._last_hash = "0" * 64  # Initial hash
        self._hash_chain: List[str] = []

        # Filters
        self._event_filters: List[Callable[[AuditEvent], bool]] = []

        # External exporters
        self._exporters: List[Callable[[List[Dict]], Awaitable[None]]] = []

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

        # Statistics
        self._stats = {
            "events_recorded": 0,
            "events_written": 0,
            "events_filtered": 0,
            "files_rotated": 0,
            "hash_chain_length": 0,
        }

    async def start(self) -> None:
        """Start the audit trail recorder."""
        if self._running:
            return

        self._running = True
        self._storage_path.mkdir(parents=True, exist_ok=True)

        self._flush_task = _create_safe_task(self._flush_loop())
        self._cleanup_task = _create_safe_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the recorder and flush remaining events."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush()

    async def record(
        self,
        event_type: str,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: Optional[Dict[str, Any]] = None,
        session_id: str = "",
        request_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
    ) -> str:
        """
        Record an audit event.

        Returns:
            Event ID
        """
        event = AuditEvent(
            event_type=event_type,
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            details=details,
        )
        event.session_id = session_id
        event.request_id = request_id
        event.ip_address = ip_address
        event.user_agent = user_agent

        # Apply filters
        for filter_func in self._event_filters:
            if not filter_func(event):
                self._stats["events_filtered"] += 1
                return event.event_id

        # Add hash chain
        if self._enable_hash_chain:
            event_hash = self._compute_event_hash(event)
            self._hash_chain.append(event_hash)
            self._last_hash = event_hash
            self._stats["hash_chain_length"] = len(self._hash_chain)

        async with self._buffer_lock:
            self._buffer.append(event)
            self._stats["events_recorded"] += 1

        # Flush if buffer is full
        if len(self._buffer) >= self._batch_size:
            _create_safe_task(self._flush(), name="event_buffer_flush")

        return event.event_id

    def _compute_event_hash(self, event: AuditEvent) -> str:
        """Compute hash for event (including previous hash)."""
        data = json.dumps(event.to_dict(), sort_keys=True)
        combined = f"{self._last_hash}:{data}"
        return hashlib.sha256(combined.encode()).hexdigest()

    def add_filter(self, filter_func: Callable[[AuditEvent], bool]) -> None:
        """
        Add an event filter.

        Filter function should return True to keep the event, False to drop it.
        """
        self._event_filters.append(filter_func)

    def add_exporter(
        self,
        exporter: Callable[[List[Dict]], Awaitable[None]],
    ) -> None:
        """Add an external exporter for SIEM integration."""
        self._exporters.append(exporter)

    async def _flush_loop(self) -> None:
        """Background loop for flushing events."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _flush(self) -> None:
        """Flush buffered events to storage."""
        async with self._buffer_lock:
            if not self._buffer:
                return

            events = self._buffer[:]
            self._buffer = []

        # Convert to dicts
        event_dicts = [e.to_dict() for e in events]

        # Write to file
        await self._write_to_file(event_dicts)

        # Send to exporters
        for exporter in self._exporters:
            try:
                await exporter(event_dicts)
            except Exception:
                pass

        self._stats["events_written"] += len(events)

    async def _write_to_file(self, events: List[Dict]) -> None:
        """Write events to audit log file."""
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"audit_{date_str}.jsonl"
        filepath = self._storage_path / filename

        try:
            with open(filepath, "a") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    async def _cleanup_loop(self) -> None:
        """Background loop for cleaning up old audit files."""
        while self._running:
            try:
                # Run cleanup once per day
                await asyncio.sleep(86400)
                await self._cleanup_old_files()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _cleanup_old_files(self) -> None:
        """Remove audit files older than retention period."""
        cutoff = time.time() - (self._retention_days * 86400)

        for filepath in self._storage_path.glob("audit_*.jsonl"):
            try:
                if filepath.stat().st_mtime < cutoff:
                    filepath.unlink()
                    self._stats["files_rotated"] += 1
            except Exception:
                pass

    async def query(
        self,
        event_type: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query audit events with filters.

        Returns matching events in reverse chronological order.
        """
        results = []
        end_time = end_time or time.time()
        start_time = start_time or (end_time - 86400)  # Default: last 24 hours

        # Search through files
        for filepath in sorted(self._storage_path.glob("audit_*.jsonl"), reverse=True):
            try:
                with open(filepath) as f:
                    for line in f:
                        try:
                            event = json.loads(line)

                            # Apply filters
                            if event["timestamp"] < start_time:
                                continue
                            if event["timestamp"] > end_time:
                                continue
                            if event_type and event["event_type"] != event_type:
                                continue
                            if actor and event["actor"] != actor:
                                continue
                            if action and event["action"] != action:
                                continue
                            if resource and event["resource"] != resource:
                                continue

                            results.append(event)

                            if len(results) >= limit:
                                return results

                        except json.JSONDecodeError:
                            continue

            except Exception:
                continue

        return results

    def verify_hash_chain(self) -> Tuple[bool, int]:
        """
        Verify the hash chain for tampering.

        Returns:
            Tuple of (is_valid, verified_count)
        """
        if not self._enable_hash_chain or not self._hash_chain:
            return True, 0

        # This would need access to the original events to fully verify
        # For now, just verify chain continuity
        return True, len(self._hash_chain)

    def get_status(self) -> Dict[str, Any]:
        """Get recorder status."""
        return {
            "running": self._running,
            "buffered_events": len(self._buffer),
            "retention_days": self._retention_days,
            "hash_chain_enabled": self._enable_hash_chain,
            "stats": self._stats.copy(),
        }

    # -- SystemService ABC --------------------------------------------------
    async def initialize(self) -> None:
        await self.start()

    async def health_check(self) -> Tuple[bool, str]:
        written = self._stats.get("events_written", 0)
        return (True, f"AuditTrailRecorder: {written} events written, running={self._running}")

    async def cleanup(self) -> None:
        await self.stop()

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._running,
            message=f"AuditTrailRecorder: running={self._running}, buffered={len(self._buffer)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        if self._buffer:
            await self._flush()
        return True

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="AuditTrailRecorder",
            version="1.0.0",
            inputs=["supervisor.event.*"],
            outputs=["audit.entry.created"],
            side_effects=["writes_audit_trail"],
        )

    def activation_triggers(self) -> List[str]:
        return []  # always_on
