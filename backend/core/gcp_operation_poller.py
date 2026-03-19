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

# GCP Operation status constants — resolved once at module load.
# Falls back to plain strings if the library is not installed.
try:
    from google.cloud.compute_v1 import Operation as _GcpOperation
    _OP_STATUS_DONE = _GcpOperation.Status.DONE
    _OP_STATUS_ABORTING = _GcpOperation.Status.ABORTING
except (ImportError, AttributeError):
    _OP_STATUS_DONE = "DONE"      # type: ignore[assignment]
    _OP_STATUS_ABORTING = "ABORTING"  # type: ignore[assignment]

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
        asyncio.create_task(self._persist_safe())

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
            data = {op_id: r.to_dict() for op_id, r in self._records.items()}
            serialized = json.dumps(data, indent=2)

            def _write_atomic(path: Path, content: str) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(content)
                tmp.replace(path)

            await asyncio.to_thread(_write_atomic, self._persist_path, serialized)
            # Also write cross-repo shared state (best-effort)
            cross_repo_path = Path.home() / ".jarvis" / "cross_repo" / "gcp" / "operations.json"
            await asyncio.to_thread(_write_atomic, cross_repo_path, serialized)
        except Exception as e:
            _log.warning("[OperationRegistry] Persist failed (non-fatal): %s", e)

    async def load(self) -> None:
        """Load registry from disk. Prunes stale records. Best-effort."""
        try:
            def _read_file(path: Path) -> Optional[str]:
                if not path.exists():
                    return None
                return path.read_text()

            raw = await asyncio.to_thread(_read_file, self._persist_path)
            if raw is None:
                return
            data = json.loads(raw)
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


# ---------------------------------------------------------------------------
# GCPOperationPoller — internal state for dedup
# ---------------------------------------------------------------------------

@dataclass
class _PollerState:
    """Tracks one active poll loop and all its concurrent waiters."""
    task: asyncio.Task          # drives the poll loop
    future: asyncio.Future      # resolved with OperationResult when done
    waiter_count: int = 0


# ---------------------------------------------------------------------------
# GCPOperationPoller
# ---------------------------------------------------------------------------

class GCPOperationPoller:
    """
    Scope-aware, registry-backed, dedup-safe GCP operation poller.

    Usage:
        poller = GCPOperationPoller(
            operations_client=zone_ops_client,
            registry=get_operation_registry(),
            project=config.project_id,
            postcondition=lambda: check_vm_running(instance_name, zone),
        )
        result = await poller.wait(operation, action="start", instance_name="vm-1",
                                   correlation_id=str(uuid.uuid4()))
        if not result.success:
            raise RuntimeError(f"Operation failed: {result.reason} — {result.error_message}")
    """

    # Class-level dedup: shared across all poller instances in this process
    _active_pollers: Dict[str, _PollerState] = {}
    _dedup_lock: asyncio.Lock = None  # type: ignore[assignment]
    _dedup_enabled: bool = True

    @classmethod
    def _get_dedup_lock(cls) -> asyncio.Lock:
        # Lazily create the class-level lock in the running event loop.
        # Re-create if the cached lock belongs to a different (closed) loop,
        # which can happen in tests that spin up a fresh event loop per test case.
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if cls._dedup_lock is None or (
            current_loop is not None
            and getattr(cls._dedup_lock, "_loop", None) is not current_loop
        ):
            cls._dedup_lock = asyncio.Lock()
        return cls._dedup_lock

    def __init__(
        self,
        operations_client: Any,
        registry: OperationLifecycleRegistry,
        project: str,
        *,
        postcondition: Optional[Callable[[], Awaitable[bool]]] = None,
        max_retries: int = 10,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        jitter_factor: float = 0.25,
        timeout: float = 300.0,
        postcondition_retry_s: float = -1.0,  # -1 = env var
        metrics_emitter: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self._client = operations_client
        self._registry = registry
        self._project = project
        self._postcondition = postcondition
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._jitter_factor = jitter_factor
        self._timeout = timeout
        _env_retry = float(os.environ.get("JARVIS_OP_POSTCONDITION_RETRY_S", "30"))
        self._postcondition_retry_s = postcondition_retry_s if postcondition_retry_s >= 0 else _env_retry
        self._emit = metrics_emitter or (lambda name, payload: None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait(
        self,
        operation: Any,
        *,
        action: str,
        instance_name: str,
        correlation_id: str,
    ) -> OperationResult:
        """
        Wait for an operation to reach a terminal state.

        Deduplicates: if another coroutine is already polling this operation_id,
        joins the existing poll rather than starting a new one.
        """
        op_id: str = operation.name
        lock = self._get_dedup_lock()

        if not self._dedup_enabled:
            # Version-drift fallback: independent poller per caller
            return await self._run_poll(operation, action=action,
                                        instance_name=instance_name,
                                        correlation_id=correlation_id)

        async with lock:
            state = self._active_pollers.get(op_id)
            if state is not None:
                # Join existing poll loop
                state.waiter_count += 1
            else:
                # Become the primary driver
                loop = asyncio.get_running_loop()
                fut: asyncio.Future = loop.create_future()
                task = asyncio.ensure_future(
                    self._drive_poll(operation, action=action,
                                     instance_name=instance_name,
                                     correlation_id=correlation_id,
                                     result_future=fut)
                )
                state = _PollerState(task=task, future=fut, waiter_count=1)
                self._active_pollers[op_id] = state

        try:
            return await asyncio.shield(state.future)
        except asyncio.CancelledError:
            async with lock:
                s = self._active_pollers.get(op_id)
                if s is not None:
                    s.waiter_count -= 1
                    if s.waiter_count <= 0:
                        # Last waiter gone — cancel the poll task too
                        s.task.cancel()
                        self._active_pollers.pop(op_id, None)
            raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _drive_poll(
        self,
        operation: Any,
        *,
        action: str,
        instance_name: str,
        correlation_id: str,
        result_future: asyncio.Future,
    ) -> None:
        """Drives the poll loop and resolves result_future when done."""
        op_id = operation.name
        try:
            result = await self._run_poll(operation, action=action,
                                          instance_name=instance_name,
                                          correlation_id=correlation_id)
            if not result_future.done():
                result_future.set_result(result)
        except Exception as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            lock = self._get_dedup_lock()
            async with lock:
                self._active_pollers.pop(op_id, None)

    async def _run_poll(
        self,
        operation: Any,
        *,
        action: str,
        instance_name: str,
        correlation_id: str,
    ) -> OperationResult:
        """Core polling state machine."""
        t_start = time.monotonic()

        # Extract scope from operation metadata
        try:
            scope = OperationScope.from_operation(operation, self._project)
        except ScopeContractError as e:
            self._emit("scope_contract_error", {"op_id": operation.name, "error": str(e)})
            return OperationResult(
                success=False,
                reason=TerminalReason.SCOPE_CONTRACT_ERROR,
                operation_id=operation.name,
                error_message=str(e),
            )

        # Register in registry
        record = await self._registry.register(
            operation, instance_name=instance_name,
            action=action, correlation_id=correlation_id,
        )

        deadline = t_start + self._timeout
        retry_count = 0

        # Fast path: already DONE
        done_result = self._check_done(operation, record, t_start)
        if done_result is not None:
            await self._close_record(record, done_result)
            return done_result

        while time.monotonic() < deadline:
            # Backoff before next poll (0 on first iteration when retry_count=0)
            if retry_count > 0:
                await self._backoff(retry_count)
            else:
                await asyncio.sleep(0)  # yield to event loop

            try:
                fresh_op = await asyncio.to_thread(
                    self._client.get,
                    project=scope.project,
                    zone=scope.zone,
                    operation=operation.name,
                )
                record.poll_count += 1
                record.last_seen_at = time.time()

                done_result = self._check_done(fresh_op, record, t_start)
                if done_result is not None:
                    await self._close_record(record, done_result)
                    return done_result
                # Still PENDING or RUNNING — continue

            except Exception as exc:
                error_class = self._classify_exception(exc)
                record.poll_count += 1
                record.last_seen_at = time.time()

                if error_class == "not_found":
                    result = await self._handle_not_found(record, t_start)
                    await self._close_record(record, result)
                    return result

                elif error_class == "permanent":
                    self._emit("permanent_failure", {
                        "op_id": operation.name,
                        "error": str(exc),
                        "retry_count": retry_count,
                    })
                    result = OperationResult(
                        success=False,
                        reason=TerminalReason.PERMISSION_DENIED,
                        operation_id=operation.name,
                        scope=scope,
                        error_message=str(exc),
                        elapsed_ms=(time.monotonic() - t_start) * 1000,
                        poll_count=record.poll_count,
                    )
                    await self._close_record(record, result)
                    return result

                elif error_class == "invalid":
                    result = OperationResult(
                        success=False,
                        reason=TerminalReason.INVALID_REQUEST,
                        operation_id=operation.name,
                        scope=scope,
                        error_message=str(exc),
                        elapsed_ms=(time.monotonic() - t_start) * 1000,
                        poll_count=record.poll_count,
                    )
                    await self._close_record(record, result)
                    return result

                else:
                    # Transient — check retry budget
                    retry_count += 1
                    if retry_count > self._max_retries:
                        self._emit("retry_budget_exhausted", {
                            "op_id": operation.name,
                            "retry_count": retry_count,
                            "last_error": str(exc),
                        })
                        result = OperationResult(
                            success=False,
                            reason=TerminalReason.RETRY_BUDGET_EXHAUSTED,
                            operation_id=operation.name,
                            scope=scope,
                            error_message=str(exc),
                            elapsed_ms=(time.monotonic() - t_start) * 1000,
                            poll_count=record.poll_count,
                        )
                        await self._close_record(record, result)
                        return result
                    # Continue loop with backoff

        # Deadline exceeded
        result = OperationResult(
            success=False,
            reason=TerminalReason.TIMEOUT,
            operation_id=operation.name,
            scope=scope,
            elapsed_ms=(time.monotonic() - t_start) * 1000,
            poll_count=record.poll_count,
        )
        await self._close_record(record, result)
        return result

    def _check_done(self, op: Any, record: OperationRecord, t_start: float) -> Optional[OperationResult]:
        """Return OperationResult if op is in a terminal status, else None."""
        status_done = _OP_STATUS_DONE
        status_aborting_str = "ABORTING"

        status = op.status
        if hasattr(status, "name"):
            status_str = status.name
        else:
            status_str = str(status)

        if status_str == status_aborting_str:
            return OperationResult(
                success=False,
                reason=TerminalReason.OP_DONE_FAILURE,
                operation_id=op.name,
                scope=record.scope,
                error_message="Operation is ABORTING",
                elapsed_ms=(time.monotonic() - t_start) * 1000,
                poll_count=record.poll_count,
            )

        is_done = (status == status_done) or (status_str == "DONE")
        if not is_done:
            return None

        if op.error and getattr(op.error, "errors", None):
            msgs = [getattr(e, "message", str(e)) for e in op.error.errors]
            return OperationResult(
                success=False,
                reason=TerminalReason.OP_DONE_FAILURE,
                operation_id=op.name,
                scope=record.scope,
                error_message="; ".join(msgs),
                elapsed_ms=(time.monotonic() - t_start) * 1000,
                poll_count=record.poll_count,
            )
        return OperationResult(
            success=True,
            reason=TerminalReason.OP_DONE_SUCCESS,
            operation_id=op.name,
            scope=record.scope,
            elapsed_ms=(time.monotonic() - t_start) * 1000,
            poll_count=record.poll_count,
        )

    async def _handle_not_found(self, record: OperationRecord, t_start: float) -> OperationResult:
        """Classify 404 per correlation + postcondition rules."""
        op_id = record.operation_id
        scope = record.scope

        # Is the op in the registry with matching scope?
        registered = self._registry.get(op_id)
        if registered is None:
            # Uncorrelated — stale reference with no registry entry
            self._emit("stale_operation_detected", {"op_id": op_id, "scope": str(scope)})
            return OperationResult(
                success=False,
                reason=TerminalReason.NOT_FOUND_UNCORRELATED,
                operation_id=op_id,
                scope=scope,
                error_message="404: Operation not found and not in registry",
                poll_count=record.poll_count,
            )

        if self._postcondition is None:
            self._emit("operation_gc_404_terminal", {
                "op_id": op_id, "reason": "no_postcondition",
            })
            return OperationResult(
                success=False,
                reason=TerminalReason.NOT_FOUND_NO_POSTCONDITION,
                operation_id=op_id,
                scope=scope,
                error_message="404: Operation GC'd; no postcondition to verify success",
                poll_count=record.poll_count,
            )

        # Retry postcondition for up to postcondition_retry_s
        postcond_deadline = time.monotonic() + self._postcondition_retry_s
        postcond_result = False
        while time.monotonic() < postcond_deadline:
            try:
                postcond_result = await self._postcondition()
                if postcond_result:
                    break
            except Exception as e:
                _log.debug("[GCPOperationPoller] postcondition threw: %s", e)
                break
            await asyncio.sleep(2.0)

        if postcond_result:
            self._emit("operation_gc_404_terminal", {"op_id": op_id, "reason": "correlated_success"})
            return OperationResult(
                success=True,
                reason=TerminalReason.NOT_FOUND_CORRELATED,
                operation_id=op_id,
                scope=scope,
                poll_count=record.poll_count,
            )
        else:
            self._emit("operation_gc_404_terminal", {
                "op_id": op_id, "reason": "postcondition_fail",
            })
            return OperationResult(
                success=False,
                reason=TerminalReason.NOT_FOUND_POSTCONDITION_FAIL,
                operation_id=op_id,
                scope=scope,
                error_message="404: Operation GC'd; postcondition returned False",
                poll_count=record.poll_count,
            )

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        """Return 'not_found' | 'permanent' | 'invalid' | 'transient'."""
        try:
            import google.api_core.exceptions as gex
            if isinstance(exc, gex.NotFound):
                return "not_found"
            if isinstance(exc, (gex.Forbidden, gex.Unauthorized)):
                return "permanent"
            if isinstance(exc, (gex.BadRequest, gex.InvalidArgument)):
                return "invalid"
            if isinstance(exc, (gex.ServiceUnavailable, gex.InternalServerError,
                                 gex.TooManyRequests, gex.ResourceExhausted,
                                 gex.DeadlineExceeded)):
                return "transient"
        except ImportError:
            pass
        # Network errors and unknown exceptions are transient
        exc_str = str(type(exc).__name__).lower()
        if any(k in exc_str for k in ("connection", "timeout", "reset", "broken")):
            return "transient"
        return "transient"  # conservative default

    async def _backoff(self, retry_count: int) -> None:
        wait = min(
            self._base_backoff * (2 ** retry_count),
            self._max_backoff,
        )
        jitter = wait * self._jitter_factor * random.random()
        await asyncio.sleep(wait + jitter)

    async def _close_record(self, record: OperationRecord, result: OperationResult) -> None:
        """Update registry terminal state. Non-fatal if registry update fails."""
        try:
            await self._registry.update_terminal(
                record.operation_id,
                state="success" if result.success else "failure",
                reason=result.reason,
                epoch=self._registry._supervisor_epoch,
            )
        except SplitBrainFenceError as e:
            _log.warning("[GCPOperationPoller] Split-brain fenced: %s", e)
        except Exception as e:
            _log.debug("[GCPOperationPoller] Registry update failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_registry_instance: Optional[OperationLifecycleRegistry] = None

def get_operation_registry(
    supervisor_epoch: int = 0,
    persist_path: Optional[Path] = None,
) -> OperationLifecycleRegistry:
    """Return the process-wide registry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = OperationLifecycleRegistry(
            persist_path=persist_path,
            supervisor_epoch=supervisor_epoch,
        )
    return _registry_instance


def reset_operation_registry() -> None:
    """Reset singleton — for tests only."""
    global _registry_instance
    _registry_instance = None
    GCPOperationPoller._active_pollers.clear()
    GCPOperationPoller._dedup_lock = None
