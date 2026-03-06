"""TUI startup dashboard data bridge.

Disease 10 Wiring, Task 8.

Consumes ``StartupEvent`` instances from the event bus and maintains
summarised state for the TUI widgets. Two views are provided:

- ``InlineSummary``: compact inline status (current phase, authority,
  budget occupancy, lease status).
- ``DetailSnapshot``: drill-down data (phase timeline, budget contention,
  lease history, handoff trace, invariant results).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.startup_telemetry import EventConsumer, StartupEvent

__all__ = ["TUIBridge", "InlineSummary", "DetailSnapshot"]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class InlineSummary:
    """Compact summary for the inline startup widget."""

    last_resolved_phase: Optional[str] = None
    authority_state: str = "BOOT_POLICY_ACTIVE"
    budget_active: int = 0
    budget_total: int = 0
    budget_hard_used: int = 0
    lease_status: str = "INACTIVE"
    lease_ttl_remaining: float = 0.0
    invariants_pass_count: int = 0
    invariants_total: int = 0


@dataclass
class DetailSnapshot:
    """Detailed drill-down data for the detail panel."""

    phase_timeline: List[Dict[str, Any]] = field(default_factory=list)
    budget_entries: List[Dict[str, Any]] = field(default_factory=list)
    lease_history: List[Dict[str, Any]] = field(default_factory=list)
    handoff_trace: List[Dict[str, Any]] = field(default_factory=list)
    invariant_results: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TUIBridge consumer
# ---------------------------------------------------------------------------


class TUIBridge(EventConsumer):
    """Maintains summarised state for TUI widgets by consuming startup events."""

    def __init__(self) -> None:
        self._summary = InlineSummary()
        self._detail = DetailSnapshot()

    # -- Public accessors ---------------------------------------------------

    @property
    def inline_summary(self) -> InlineSummary:
        return self._summary

    @property
    def detail_snapshot(self) -> DetailSnapshot:
        return self._detail

    # -- EventConsumer implementation ----------------------------------------

    async def consume(self, event: StartupEvent) -> None:
        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler is not None:
            handler(event)
        # Always update authority_state from the event if available
        if event.authority_state:
            self._summary.authority_state = event.authority_state

    # -- Handlers per event type --------------------------------------------

    def _handle_phase_gate(self, event: StartupEvent) -> None:
        status = event.detail.get("status", "")
        if status in ("passed", "skipped"):
            self._summary.last_resolved_phase = event.phase
        self._detail.phase_timeline.append({
            "phase": event.phase,
            "status": status,
            "duration_s": event.detail.get("duration_s"),
            "timestamp": event.timestamp,
        })

    def _handle_budget_acquire(self, event: StartupEvent) -> None:
        self._summary.budget_active += 1
        self._summary.budget_total += 1
        if event.detail.get("hard_slot"):
            self._summary.budget_hard_used += 1
        self._detail.budget_entries.append({
            "type": "acquire",
            "category": event.detail.get("category"),
            "name": event.detail.get("name"),
            "wait_s": event.detail.get("wait_s"),
            "queue_depth": event.detail.get("queue_depth"),
            "timestamp": event.timestamp,
        })

    def _handle_budget_release(self, event: StartupEvent) -> None:
        self._summary.budget_active = max(0, self._summary.budget_active - 1)
        self._detail.budget_entries.append({
            "type": "release",
            "category": event.detail.get("category"),
            "name": event.detail.get("name"),
            "held_s": event.detail.get("held_s"),
            "timestamp": event.timestamp,
        })

    def _handle_lease_acquired(self, event: StartupEvent) -> None:
        self._summary.lease_status = "ACTIVE"
        self._summary.lease_ttl_remaining = event.detail.get("ttl_s", 0.0)
        self._detail.lease_history.append({
            "type": "acquired",
            "host": event.detail.get("host"),
            "port": event.detail.get("port"),
            "epoch": event.detail.get("lease_epoch"),
            "timestamp": event.timestamp,
        })

    def _handle_lease_revoked(self, event: StartupEvent) -> None:
        self._summary.lease_status = "REVOKED"
        self._detail.lease_history.append({
            "type": "revoked",
            "reason": event.detail.get("reason"),
            "timestamp": event.timestamp,
        })

    def _handle_authority_transition(self, event: StartupEvent) -> None:
        self._detail.handoff_trace.append({
            "from_state": event.detail.get("from_state"),
            "to_state": event.detail.get("to_state"),
            "guards_checked": event.detail.get("guards_checked"),
            "duration_ms": event.detail.get("duration_ms"),
            "timestamp": event.timestamp,
        })

    def _handle_invariant_check(self, event: StartupEvent) -> None:
        self._summary.invariants_pass_count = event.detail.get("pass_count", 0)
        self._summary.invariants_total = event.detail.get("total", 0)
        self._detail.invariant_results.append({
            "results": event.detail.get("results", []),
            "timestamp": event.timestamp,
        })
