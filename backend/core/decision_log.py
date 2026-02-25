"""
JARVIS Decision Audit Log v1.0
===============================
Bounded in-memory ring buffer for structured logging of adaptive decisions.

Records decisions like:
  - VM termination (cost waste, memory pressure normalized, max lifetime)
  - Routing changes (GCP promote, GCP demote, mode degradation)
  - Mode transitions (startup memory mode changes)

Queryable by decision_type and time range for runtime auditability.
Thread-safe via threading.Lock (not asyncio.Lock — used from both sync and async).

NOT a persistent database. Data is lost on restart. For persistence,
consumers can periodically snapshot via to_dicts().

v271.0: Created as part of Phase 8 — state authority and decision auditing.
"""

import collections
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from backend.core.trace_envelope import TraceEnvelope
    _TRACE_ENVELOPE_AVAILABLE = True
except ImportError:
    _TRACE_ENVELOPE_AVAILABLE = False
    TraceEnvelope = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ENTRIES = _env_int("JARVIS_DECISION_LOG_MAX_ENTRIES", 500)

# Decision type constants (avoids string typos across consumers)
DECISION_VM_TERMINATION = "vm_termination"
DECISION_ROUTING_PROMOTE = "routing_promote"
DECISION_ROUTING_DEMOTE = "routing_demote"
DECISION_MODE_TRANSITION = "mode_transition"


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class DecisionRecord:
    """A single adaptive decision record."""

    decision_type: str           # e.g. DECISION_VM_TERMINATION
    reason: str                  # Human-readable reason
    inputs: Dict[str, Any]       # Key measurements/context that informed the decision
    outcome: str                 # What happened ("terminated", "promoted", etc.)
    timestamp: float = field(default_factory=time.time)
    component: str = ""          # Which module made the decision
    metadata: Dict[str, Any] = field(default_factory=dict)
    envelope: Optional[Any] = None  # TraceEnvelope when available

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for inspection/export."""
        d = {
            "decision_type": self.decision_type,
            "reason": self.reason,
            "inputs": self.inputs,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "component": self.component,
            "metadata": self.metadata,
        }
        if self.envelope is not None:
            try:
                d["envelope"] = self.envelope.to_dict()
            except Exception:
                pass
        return d


# ---------------------------------------------------------------------------
# Core Class
# ---------------------------------------------------------------------------

class DecisionLog:
    """
    Bounded ring buffer of DecisionRecords.

    Thread-safe. Can be called from sync code (gcp_vm_manager monitoring loop)
    and async code (via record_decision() convenience function).
    """

    _instance: Optional["DecisionLog"] = None
    _instance_lock = threading.Lock()

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES):
        self._buffer: collections.deque = collections.deque(maxlen=max(1, max_entries))
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}  # decision_type -> cumulative count
        self._total_appended: int = 0    # cumulative count of all appends (survives eviction)
        self._last_flushed_total: int = 0  # cumulative count at last successful flush

    @classmethod
    def get_instance(cls) -> "DecisionLog":
        """Singleton accessor (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -----------------------------------------------------------------------
    # Write API
    # -----------------------------------------------------------------------

    def record(
        self,
        decision_type: str,
        reason: str,
        inputs: Dict[str, Any],
        outcome: str,
        component: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        envelope: Optional[Any] = None,
    ) -> DecisionRecord:
        """
        Record a decision. Thread-safe.

        Returns the created DecisionRecord.
        """
        rec = DecisionRecord(
            decision_type=decision_type,
            reason=reason,
            inputs=inputs,
            outcome=outcome,
            component=component,
            metadata=metadata or {},
            envelope=envelope,
        )
        with self._lock:
            self._buffer.append(rec)
            self._total_appended += 1
            self._counters[decision_type] = self._counters.get(decision_type, 0) + 1
        logger.debug(
            "[DecisionLog] %s: %s -> %s (%s)",
            decision_type, reason, outcome, component,
        )
        return rec

    # -----------------------------------------------------------------------
    # Query API
    # -----------------------------------------------------------------------

    def query(
        self,
        decision_type: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 50,
    ) -> List[DecisionRecord]:
        """
        Query decisions by type and/or time range.
        Returns newest-first (reverse chronological).
        Thread-safe.
        """
        results: List[DecisionRecord] = []
        with self._lock:
            for rec in reversed(self._buffer):
                if decision_type is not None and rec.decision_type != decision_type:
                    continue
                if since is not None and rec.timestamp < since:
                    continue
                if until is not None and rec.timestamp > until:
                    continue
                results.append(rec)
                if len(results) >= limit:
                    break
        return results

    def get_counts(self) -> Dict[str, int]:
        """Get cumulative decision counts by type. Thread-safe."""
        with self._lock:
            return dict(self._counters)

    def get_recent(self, n: int = 10) -> List[DecisionRecord]:
        """Get the N most recent decisions (newest-first). Thread-safe."""
        with self._lock:
            items = list(self._buffer)
        return list(reversed(items[-n:]))

    def to_dicts(self) -> List[Dict[str, Any]]:
        """Serialize all entries for export/snapshot. Thread-safe."""
        with self._lock:
            return [rec.to_dict() for rec in self._buffer]

    @property
    def size(self) -> int:
        """Current number of entries in the buffer."""
        return len(self._buffer)

    # -----------------------------------------------------------------------
    # Persistence API
    # -----------------------------------------------------------------------

    def flush_to_jsonl(self, decisions_dir) -> int:
        """Flush new records to date-partitioned JSONL. Returns count flushed.

        Uses cumulative counters to survive ring buffer eviction correctly.
        On error, returns 0 and records remain available for retry.
        """
        from pathlib import Path
        try:
            from backend.core.trace_store import JSONLWriter
        except ImportError:
            logger.debug("trace_store not available, skipping JSONL flush")
            return 0

        decisions_dir = Path(decisions_dir)
        with self._lock:
            records = list(self._buffer)
            total_now = self._total_appended
            last_flushed = self._last_flushed_total

        # How many new records since last flush?
        unflushed_count = total_now - last_flushed
        # But we can only flush what's still in the buffer (eviction may have dropped some)
        available = min(unflushed_count, len(records))
        new_records = records[-available:] if available > 0 else []

        if not new_records:
            return 0

        try:
            writer = JSONLWriter(decisions_dir / f"{time.strftime('%Y%m%d')}.jsonl")
            for rec in new_records:
                writer.append(rec.to_dict())
            with self._lock:
                self._last_flushed_total = total_now
            return len(new_records)
        except Exception:
            logger.debug("Failed to flush decisions to JSONL", exc_info=True)
            return 0


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def get_decision_log() -> DecisionLog:
    """Get the global DecisionLog singleton."""
    return DecisionLog.get_instance()


def record_decision(
    decision_type: str,
    reason: str,
    inputs: Dict[str, Any],
    outcome: str,
    component: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    envelope: Optional[Any] = None,
) -> Optional[DecisionRecord]:
    """
    Module-level convenience function. Safe to call from anywhere.
    Returns None if logging fails (never raises).
    """
    try:
        return get_decision_log().record(
            decision_type=decision_type,
            reason=reason,
            inputs=inputs,
            outcome=outcome,
            component=component,
            metadata=metadata,
            envelope=envelope,
        )
    except Exception:
        return None
