"""
GCP Operation Lifecycle Poller v1.0
====================================
Scope-aware, registry-backed, dedup-safe GCP zone/region/global operation poller.

Replaces ad-hoc _wait_for_operation() loops in gcp_vm_manager.py.
Canonical implementation shared by JARVIS and JARVIS-Prime.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Awaitable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ScopeContractError(Exception):
    """Operation object lacks the zone/selfLink needed to infer its scope."""

class SplitBrainFenceError(Exception):
    """Attempted to update an operation record with a stale supervisor epoch."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TerminalReason(str, Enum):
    OP_DONE_SUCCESS              = "op_done_success"
    OP_DONE_FAILURE              = "op_done_failure"
    NOT_FOUND_CORRELATED         = "not_found_correlated"
    NOT_FOUND_UNCORRELATED       = "not_found_uncorrelated"
    NOT_FOUND_SCOPE_MISMATCH     = "not_found_scope_mismatch"
    NOT_FOUND_NO_POSTCONDITION   = "not_found_no_postcondition"
    NOT_FOUND_POSTCONDITION_FAIL = "not_found_postcondition_fail"
    PERMISSION_DENIED            = "permission_denied"
    INVALID_REQUEST              = "invalid_request"
    RETRY_BUDGET_EXHAUSTED       = "retry_budget_exhausted"
    TIMEOUT                      = "timeout"
    CANCELLED                    = "cancelled"
    SCOPE_CONTRACT_ERROR         = "scope_contract_error"


# ---------------------------------------------------------------------------
# OperationScope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperationScope:
    project: str
    zone: Optional[str]     # set for zonal operations
    region: Optional[str]   # set for regional operations
    scope_type: str         # "zonal" | "regional" | "global"

    @classmethod
    def from_operation(cls, op: Any, fallback_project: str) -> "OperationScope":
        """
        Extract scope exclusively from operation.zone, operation.region, or
        operation.self_link URL.  Never accepts a caller-supplied default zone.
        Raises ScopeContractError if none of those fields provides usable scope.
        """
        zone_url: str = getattr(op, "zone", "") or ""
        region_url: str = getattr(op, "region", "") or ""
        self_link: str = getattr(op, "self_link", "") or ""

        # Parse zone from zone URL or self_link
        zone = cls._extract_segment(zone_url, "zones")
        if not zone:
            zone = cls._extract_segment(self_link, "zones")

        # Parse region from region URL or self_link
        region = cls._extract_segment(region_url, "regions")
        if not region:
            region = cls._extract_segment(self_link, "regions")

        # Extract project from any available URL
        project = cls._extract_project(zone_url or region_url or self_link) or fallback_project

        if zone:
            return cls(project=project, zone=zone, region=None, scope_type="zonal")
        if region:
            return cls(project=project, zone=None, region=region, scope_type="regional")

        # Check if self_link indicates global scope
        if self_link and "/global/" in self_link:
            return cls(project=project, zone=None, region=None, scope_type="global")

        raise ScopeContractError(
            f"Cannot infer operation scope from op.zone={zone_url!r}, "
            f"op.region={region_url!r}, op.self_link={self_link!r}. "
            "Operation object must contain at least one scope field."
        )

    @staticmethod
    def _extract_segment(url: str, segment_type: str) -> Optional[str]:
        """Extract the value after /zones/ or /regions/ from a GCP URL."""
        if not url:
            return None
        marker = f"/{segment_type}/"
        idx = url.find(marker)
        if idx == -1:
            return None
        rest = url[idx + len(marker):]
        return rest.split("/")[0] or None

    @staticmethod
    def _extract_project(url: str) -> Optional[str]:
        """Extract project from /projects/<project>/... URL."""
        marker = "/projects/"
        idx = url.find(marker)
        if idx == -1:
            return None
        rest = url[idx + len(marker):]
        return rest.split("/")[0] or None


# ---------------------------------------------------------------------------
# OperationRecord
# ---------------------------------------------------------------------------

@dataclass
class OperationRecord:
    operation_id: str
    scope: OperationScope
    instance_name: str
    action: str
    created_at: float           # time.time() when op was returned from GCP
    first_seen_at: float        # when registry first registered it
    last_seen_at: float
    poll_count: int = 0
    terminal_state: Optional[str] = None
    terminal_reason: Optional[TerminalReason] = None
    correlation_id: str = ""
    supervisor_epoch: int = 0
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "scope": dataclasses.asdict(self.scope),
            "instance_name": self.instance_name,
            "action": self.action,
            "created_at": self.created_at,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "poll_count": self.poll_count,
            "terminal_state": self.terminal_state,
            "terminal_reason": self.terminal_reason.value if self.terminal_reason else None,
            "correlation_id": self.correlation_id,
            "supervisor_epoch": self.supervisor_epoch,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OperationRecord":
        scope_d = d.get("scope", {})
        scope = OperationScope(
            project=scope_d.get("project", ""),
            zone=scope_d.get("zone"),
            region=scope_d.get("region"),
            scope_type=scope_d.get("scope_type", "zonal"),
        )
        tr_raw = d.get("terminal_reason")
        return cls(
            operation_id=d["operation_id"],
            scope=scope,
            instance_name=d.get("instance_name", ""),
            action=d.get("action", "unknown"),
            created_at=d.get("created_at", 0.0),
            first_seen_at=d.get("first_seen_at", 0.0),
            last_seen_at=d.get("last_seen_at", 0.0),
            poll_count=d.get("poll_count", 0),
            terminal_state=d.get("terminal_state"),
            terminal_reason=TerminalReason(tr_raw) if tr_raw else None,
            correlation_id=d.get("correlation_id", ""),
            supervisor_epoch=d.get("supervisor_epoch", 0),
            error_message=d.get("error_message"),
        )


# ---------------------------------------------------------------------------
# OperationResult
# ---------------------------------------------------------------------------

@dataclass
class OperationResult:
    success: bool
    reason: TerminalReason
    operation_id: str
    scope: Optional[OperationScope] = None
    error_message: Optional[str] = None
    elapsed_ms: float = 0.0
    poll_count: int = 0


# ---------------------------------------------------------------------------
# OperationLifecycleRegistry
# ---------------------------------------------------------------------------

_MAX_RECORD_AGE_S = 86_400  # 24 hours

class OperationLifecycleRegistry:
    """
    In-memory + persisted lifecycle registry for GCP zone operations.

    Thread-safe via asyncio.Lock (all public methods are async coroutines).
    Persists to a JSON file; load/persist are best-effort (failure is non-fatal).
    """

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        supervisor_epoch: int = 0,
        max_record_age_s: float = _MAX_RECORD_AGE_S,
        max_entries: int = 1000,
    ) -> None:
        self._records: Dict[str, OperationRecord] = {}
        self._lock = asyncio.Lock()
        self._persist_path = persist_path or (
            Path.home() / ".jarvis" / "gcp" / "operations.json"
        )
        self._supervisor_epoch = supervisor_epoch
        self._max_record_age_s = max_record_age_s
        self._max_entries = max_entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def register(
        self,
        op: Any,
        *,
        instance_name: str,
        action: str,
        correlation_id: str,
    ) -> OperationRecord:
        """Register a new in-flight operation. Idempotent by op_id."""
        try:
            scope = OperationScope.from_operation(op, fallback_project="unknown")
        except ScopeContractError:
            scope = OperationScope(project="unknown", zone=None, region=None,
                                   scope_type="global")
        now = time.time()
        record = OperationRecord(
            operation_id=op.name,
            scope=scope,
            instance_name=instance_name,
            action=action,
            created_at=now,
            first_seen_at=now,
            last_seen_at=now,
            correlation_id=correlation_id,
            supervisor_epoch=self._supervisor_epoch,
        )
        async with self._lock:
            if op.name not in self._records:
                self._records[op.name] = record
                await self._maybe_prune_locked()
            return self._records[op.name]

    def get(self, operation_id: str) -> Optional[OperationRecord]:
        """Synchronous read — safe because asyncio.Lock is not held for reads."""
        return self._records.get(operation_id)

    async def update_terminal(
        self,
        operation_id: str,
        state: str,
        reason: TerminalReason,
        epoch: int,
    ) -> None:
        """
        Close an operation record as terminal.
        Raises SplitBrainFenceError if epoch < record.supervisor_epoch.
        """
        async with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                return  # Already gone — idempotent
            if epoch < record.supervisor_epoch:
                raise SplitBrainFenceError(
                    f"[SplitBrainFence] Rejected update for {operation_id}: "
                    f"incoming_epoch={epoch} < record_epoch={record.supervisor_epoch}"
                )
            record.terminal_state = state
            record.terminal_reason = reason
            record.last_seen_at = time.time()
        # Persist asynchronously (best-effort)
        asyncio.ensure_future(self._persist_safe())

    def get_inflight(self) -> List[OperationRecord]:
        return [r for r in self._records.values() if r.terminal_state is None]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist(self) -> None:
        """Write registry to disk. Best-effort — logs warning on failure."""
        await self._persist_safe()

    async def _persist_safe(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {op_id: r.to_dict() for op_id, r in self._records.items()}
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._persist_path)
            # Also write cross-repo shared state (best-effort)
            cross_repo_path = Path.home() / ".jarvis" / "cross_repo" / "gcp" / "operations.json"
            cross_repo_path.parent.mkdir(parents=True, exist_ok=True)
            cross_repo_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            _log.warning("[OperationRegistry] Persist failed (non-fatal): %s", e)

    async def load(self) -> None:
        """Load registry from disk. Prunes stale records. Best-effort."""
        try:
            if not self._persist_path.exists():
                return
            data = json.loads(self._persist_path.read_text())
            now = time.time()
            async with self._lock:
                for op_id, d in data.items():
                    record = OperationRecord.from_dict(d)
                    # Prune records older than max_record_age_s
                    if now - record.first_seen_at > self._max_record_age_s:
                        continue
                    self._records[op_id] = record
        except Exception as e:
            _log.warning("[OperationRegistry] Load failed (starting empty): %s", e)

    # ------------------------------------------------------------------
    # Orphan reconciliation
    # ------------------------------------------------------------------

    async def reconcile_orphans(
        self,
        describe_fn: Callable[[str, str], Awaitable[str]],
        emit_fn: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        """
        For each orphaned in-flight record from a prior session, query actual VM state
        and close the record with the correct terminal reason.

        describe_fn(instance_name, zone) -> status string
            ("RUNNING" | "STOPPED" | "TERMINATED" | "STAGING" | "PROVISIONING" | "NOT_FOUND" | "ERROR")

        emit_fn(event_name, payload) - optional metric/event callback
        """
        orphans = list(self.get_inflight())
        _log.info("[OperationRegistry] Reconciling %d orphaned in-flight records", len(orphans))

        _emit = emit_fn or (lambda n, p: None)

        for record in orphans:
            zone = record.scope.zone or ""
            try:
                status = await describe_fn(record.instance_name, zone)
            except Exception as e:
                status = "ERROR"
                _log.warning("[OperationRegistry] describe failed for %s: %s",
                             record.operation_id, e)

            outcome = self._reconcile_outcome(status, record.action)
            try:
                await self.update_terminal(
                    record.operation_id,
                    state=outcome["state"],
                    reason=outcome["reason"],
                    epoch=self._supervisor_epoch,
                )
                event = "orphan_recovered" if outcome["state"] == "success" else "reconcile_fail"
                _emit(event, {
                    "op_id": record.operation_id,
                    "action": record.action,
                    "instance_status": status,
                    "inferred_state": outcome["state"],
                })
                _log.info("[OperationRegistry] Reconciled %s: instance=%s action=%s → %s",
                          record.operation_id, record.instance_name, record.action, outcome["state"])
            except SplitBrainFenceError:
                pass  # Record already closed by another path

    @staticmethod
    def _reconcile_outcome(instance_status: str, action: str) -> dict:
        """Return {"state": ..., "reason": ...} based on instance_status × action."""
        s = instance_status.upper()
        # Normalise partial statuses
        running = s in ("RUNNING",)
        starting = s in ("STAGING", "PROVISIONING")
        stopped = s in ("TERMINATED", "STOPPED", "SUSPENDED")
        gone = s == "NOT_FOUND"
        error = s in ("ERROR", "ABORTING")

        if error:
            return {"state": "error", "reason": TerminalReason.NOT_FOUND_POSTCONDITION_FAIL}

        action_l = action.lower()
        if action_l in ("start", "create"):
            if running or starting:
                return {"state": "success", "reason": TerminalReason.NOT_FOUND_CORRELATED}
            if gone and action_l == "create":
                return {"state": "error", "reason": TerminalReason.NOT_FOUND_POSTCONDITION_FAIL}
            return {"state": "error", "reason": TerminalReason.NOT_FOUND_POSTCONDITION_FAIL}

        if action_l in ("stop", "terminate"):
            if stopped:
                return {"state": "success", "reason": TerminalReason.NOT_FOUND_CORRELATED}
            return {"state": "error", "reason": TerminalReason.NOT_FOUND_POSTCONDITION_FAIL}

        if action_l == "delete":
            if gone:
                return {"state": "success", "reason": TerminalReason.NOT_FOUND_CORRELATED}
            return {"state": "error", "reason": TerminalReason.NOT_FOUND_POSTCONDITION_FAIL}

        # Unknown action: conservative
        if running:
            return {"state": "success", "reason": TerminalReason.NOT_FOUND_CORRELATED}
        return {"state": "error", "reason": TerminalReason.NOT_FOUND_UNCORRELATED}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _maybe_prune_locked(self) -> None:
        """Call only while _lock is held."""
        if len(self._records) <= self._max_entries:
            return
        now = time.time()
        # Step 1: remove completed records older than max_record_age_s
        for op_id in [k for k, r in self._records.items()
                      if r.terminal_state is not None
                      and now - r.last_seen_at > self._max_record_age_s]:
            del self._records[op_id]
        if len(self._records) <= self._max_entries:
            return
        # Step 2: remove oldest completed by last_seen_at
        completed = sorted(
            [r for r in self._records.values() if r.terminal_state is not None],
            key=lambda r: r.last_seen_at,
        )
        for r in completed:
            if len(self._records) <= self._max_entries:
                break
            del self._records[r.operation_id]
        if len(self._records) <= self._max_entries:
            return
        # Step 3: forcibly close oldest in-flight (emergency only)
        inflight = sorted(self.get_inflight(), key=lambda r: r.last_seen_at)
        for r in inflight:
            if len(self._records) <= self._max_entries:
                break
            r.terminal_state = "timeout_pruned"
            r.terminal_reason = TerminalReason.TIMEOUT
            _log.warning("[OperationRegistry] Emergency-pruned in-flight op: %s", r.operation_id)
