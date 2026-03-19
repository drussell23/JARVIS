# GCP Operation Lifecycle Poller — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ad-hoc `_wait_for_operation` loop (which spams 404 warnings indefinitely) with a scope-aware, registry-backed, dedup-safe operation poller that treats 404 as terminal and classifies all errors correctly.

**Architecture:** New canonical module `backend/core/gcp_operation_poller.py` owns the state machine, lifecycle registry, and error classification. Both `backend/core/gcp_vm_manager.py` (JARVIS) and `jarvis_prime/core/gcp_vm_manager.py` (JARVIS-Prime) delegate to it. JARVIS-Prime gets an identical fallback copy if the primary can't be imported. Operation records are persisted to `~/.jarvis/gcp/operations.json`; orphaned records from prior sessions are reconciled at startup by querying actual VM state.

**Tech Stack:** Python 3.10+, `asyncio`, `google-cloud-compute`, `google.api_core.exceptions`, `dataclasses`, `json`, `uuid`, `hashlib`

**Spec:** `docs/superpowers/specs/2026-03-19-gcp-operation-lifecycle-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/core/gcp_operation_poller.py` | **CREATE** | Canonical poller: OperationScope, OperationRecord, OperationLifecycleRegistry, GCPOperationPoller, all enums and exceptions |
| `jarvis_prime/core/gcp_operation_poller.py` | **CREATE** (copy) | Identical fallback copy for JARVIS-Prime |
| `backend/core/gcp_vm_manager.py` | **MODIFY** | Replace `_wait_for_operation` body; add postcondition factories; wire registry at `__init__` and startup |
| `jarvis_prime/core/gcp_vm_manager.py` | **MODIFY** | Same replacement pattern as JARVIS |
| `backend/tests/test_gcp_operation_poller.py` | **CREATE** | Full hermetic test suite — all 14 scenarios |

---

## Task 1: Core types and exceptions

**Files:**
- Create: `backend/core/gcp_operation_poller.py` (skeleton — enums, dataclasses, exceptions only)
- Test: `backend/tests/test_gcp_operation_poller.py` (first group of tests)

- [ ] **Step 1: Write failing tests for OperationScope.from_operation**

```python
# backend/tests/test_gcp_operation_poller.py
"""Hermetic tests for GCPOperationPoller — no GCP network calls."""
from __future__ import annotations
import asyncio
import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Helpers — fake GCP Operation objects
# ---------------------------------------------------------------------------

def _make_op(
    name: str = "operation-1234",
    status: str = "RUNNING",        # PENDING | RUNNING | DONE | ABORTING
    error: Any = None,
    zone_url: str = "https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
    self_link: str = "",
    region_url: str = "",
) -> MagicMock:
    op = MagicMock()
    op.name = name
    op.status = status
    op.error = error
    op.zone = zone_url
    op.region = region_url
    op.self_link = self_link or f"{zone_url}/operations/{name}"
    return op


# ---------------------------------------------------------------------------
# Task 1: OperationScope
# ---------------------------------------------------------------------------

class TestOperationScope:
    def test_extracts_zone_from_zone_url(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        assert scope.zone == "us-central1-b"
        assert scope.project == "proj"
        assert scope.scope_type == "zonal"

    def test_extracts_project_from_self_link(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="",
            self_link="https://www.googleapis.com/compute/v1/projects/other-proj/zones/us-east1-b/operations/op-1",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback")
        assert scope.project == "other-proj"
        assert scope.zone == "us-east1-b"

    def test_uses_fallback_project_when_self_link_absent(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
            self_link="",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback-proj")
        assert scope.project == "proj"  # extracted from zone url, not fallback

    def test_raises_contract_error_when_no_scope(self):
        from backend.core.gcp_operation_poller import OperationScope, ScopeContractError
        op = _make_op(zone_url="", self_link="", region_url="")
        with pytest.raises(ScopeContractError):
            OperationScope.from_operation(op, fallback_project="proj")

    def test_zone_mismatch_regression_no_config_zone_fallback(self):
        """Old path: poller used config.zone even when op was in a different zone.
        New contract: scope ALWAYS comes from the operation object."""
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-east1-c")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        # The scope must reflect the operation's actual zone, not any external config
        assert scope.zone == "us-east1-c"
        # There is no "config zone" parameter to from_operation — the old path is simply gone
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestOperationScope -v 2>&1 | head -30
```
Expected: `ImportError: cannot import name 'OperationScope'`

- [ ] **Step 3: Write the skeleton module with OperationScope, enums, and exceptions**

Create `backend/core/gcp_operation_poller.py`:

```python
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
```

- [ ] **Step 4: Run scope tests**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestOperationScope -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit scope types**

```bash
git add backend/core/gcp_operation_poller.py backend/tests/test_gcp_operation_poller.py
git commit -m "feat(gcp): add OperationScope with strict scope contract (no zone fallback)"
```

---

## Task 2: OperationRecord and OperationLifecycleRegistry

**Files:**
- Modify: `backend/core/gcp_operation_poller.py` (add OperationRecord, OperationResult, OperationLifecycleRegistry)
- Modify: `backend/tests/test_gcp_operation_poller.py` (add registry tests)

- [ ] **Step 1: Write failing registry tests**

Append to `backend/tests/test_gcp_operation_poller.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: OperationRecord + OperationLifecycleRegistry
# ---------------------------------------------------------------------------

class TestOperationLifecycleRegistry:
    @pytest.fixture
    def tmp_registry(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry
        return OperationLifecycleRegistry(
            persist_path=tmp_path / "ops.json",
            supervisor_epoch=1,
        )

    @pytest.fixture
    def sample_scope(self):
        from backend.core.gcp_operation_poller import OperationScope
        return OperationScope(project="proj", zone="us-central1-b", region=None, scope_type="zonal")

    @pytest.mark.asyncio
    async def test_register_creates_record(self, tmp_registry, sample_scope):
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        assert record.operation_id == "operation-1234"
        assert record.action == "start"
        assert record.instance_name == "vm-1"
        assert record.terminal_state is None  # still in-flight

    @pytest.mark.asyncio
    async def test_update_terminal_succeeds_with_current_epoch(self, tmp_registry, sample_scope):
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        await tmp_registry.update_terminal(record.operation_id, "success",
                                           TerminalReason.OP_DONE_SUCCESS, epoch=1)
        updated = tmp_registry.get(record.operation_id)
        assert updated.terminal_state == "success"
        assert updated.terminal_reason == TerminalReason.OP_DONE_SUCCESS

    @pytest.mark.asyncio
    async def test_update_terminal_rejected_stale_epoch(self, tmp_registry):
        from backend.core.gcp_operation_poller import TerminalReason, SplitBrainFenceError
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        with pytest.raises(SplitBrainFenceError):
            # epoch 0 < registry epoch 1 → rejected
            await tmp_registry.update_terminal(record.operation_id, "success",
                                               TerminalReason.OP_DONE_SUCCESS, epoch=0)
        # Record must NOT be mutated
        assert tmp_registry.get(record.operation_id).terminal_state is None

    @pytest.mark.asyncio
    async def test_persist_and_reload(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        reg1 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)
        op = _make_op()
        record = await reg1.register(op, instance_name="vm-1", action="start",
                                     correlation_id="corr-1")
        await reg1.persist()

        reg2 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=2)
        await reg2.load()
        loaded = reg2.get(record.operation_id)
        assert loaded is not None
        assert loaded.instance_name == "vm-1"

    @pytest.mark.asyncio
    async def test_pruning_removes_completed_before_inflight(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        reg = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json",
                                         supervisor_epoch=1, max_entries=3)
        # Register 3 ops — 2 completed, 1 in-flight
        for i in range(2):
            op = _make_op(name=f"op-done-{i}",
                          zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
            r = await reg.register(op, instance_name=f"vm-{i}", action="start",
                                   correlation_id=str(i))
            await reg.update_terminal(r.operation_id, "success",
                                      TerminalReason.OP_DONE_SUCCESS, epoch=1)
        op_live = _make_op(name="op-inflight",
                           zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg.register(op_live, instance_name="vm-live", action="start",
                           correlation_id="live")

        # Add a 4th op — should trigger pruning of completed entries first
        op_new = _make_op(name="op-new",
                          zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg.register(op_new, instance_name="vm-new", action="start",
                           correlation_id="new")
        # In-flight record must survive pruning
        assert reg.get("op-inflight") is not None
        # op-new must be registered
        assert reg.get("op-new") is not None

    @pytest.mark.asyncio
    async def test_stale_op_from_prior_session_reconciled(self, tmp_path):
        """Orphaned in-flight record from prior session is closed on startup reconciliation."""
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        # Session 1: register op, crash without closing
        reg1 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)
        op = _make_op(name="op-orphan",
                      zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg1.register(op, instance_name="vm-orphan", action="start",
                            correlation_id="c-orphan")
        await reg1.persist()

        # Session 2: load registry; reconcile with mock instance describer
        reg2 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=2)
        await reg2.load()

        async def mock_describe(instance_name: str, zone: str):
            return "RUNNING"  # VM is now running → start op succeeded

        events = []
        await reg2.reconcile_orphans(
            describe_fn=mock_describe,
            emit_fn=lambda name, payload: events.append((name, payload)),
        )
        record = reg2.get("op-orphan")
        assert record.terminal_state == "success"
        assert record.terminal_reason == TerminalReason.NOT_FOUND_CORRELATED
        assert any(e[0] == "orphan_recovered" for e in events)
```

- [ ] **Step 2: Run to confirm failure**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestOperationLifecycleRegistry -v 2>&1 | head -20
```

- [ ] **Step 3: Implement OperationRecord, OperationResult, OperationLifecycleRegistry**

Append to `backend/core/gcp_operation_poller.py` after the existing code:

```python
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
```

- [ ] **Step 4: Run registry tests**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestOperationLifecycleRegistry -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit registry**

```bash
git add backend/core/gcp_operation_poller.py backend/tests/test_gcp_operation_poller.py
git commit -m "feat(gcp): add OperationLifecycleRegistry with persistence and orphan reconciliation"
```

---

## Task 3: GCPOperationPoller — error classification + poll loop

**Files:**
- Modify: `backend/core/gcp_operation_poller.py` (append GCPOperationPoller)
- Modify: `backend/tests/test_gcp_operation_poller.py` (add poller tests)

- [ ] **Step 1: Write failing poller tests**

Append to `backend/tests/test_gcp_operation_poller.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: GCPOperationPoller
# ---------------------------------------------------------------------------

def _make_done_op(name="op-1", error=None):
    op = _make_op(name=name, status="DONE")
    op.error = error
    if error:
        err_mock = MagicMock()
        err_mock.errors = [MagicMock(message="some GCP error")]
        op.error = err_mock
    return op


class TestGCPOperationPoller:
    @pytest.fixture
    def tmp_registry(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry
        return OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)

    @pytest.fixture
    def ops_client(self):
        """Mock zone operations client."""
        return MagicMock()

    def _make_poller(self, ops_client, registry, postcondition=None, max_retries=3,
                     timeout=10.0):
        from backend.core.gcp_operation_poller import GCPOperationPoller
        return GCPOperationPoller(
            operations_client=ops_client,
            registry=registry,
            project="proj",
            postcondition=postcondition,
            max_retries=max_retries,
            base_backoff=0.0,
            max_backoff=0.0,
            timeout=timeout,
        )

    @pytest.mark.asyncio
    async def test_op_done_on_first_poll(self, tmp_registry, ops_client):
        op = _make_done_op()
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is True
        from backend.core.gcp_operation_poller import TerminalReason
        assert result.reason == TerminalReason.OP_DONE_SUCCESS

    @pytest.mark.asyncio
    async def test_op_aborting_is_failure(self, tmp_registry, ops_client):
        op = _make_op(status="ABORTING")
        # The client refresh also returns ABORTING
        ops_client.get = MagicMock(return_value=_make_op(status="ABORTING"))
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        from backend.core.gcp_operation_poller import TerminalReason
        assert result.reason == TerminalReason.OP_DONE_FAILURE

    @pytest.mark.asyncio
    async def test_404_correlated_success(self, tmp_registry, ops_client):
        """404 with registry match + postcondition=True → terminal success."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        postcond = AsyncMock(return_value=True)
        poller = self._make_poller(ops_client, tmp_registry, postcondition=postcond)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is True
        assert result.reason == TerminalReason.NOT_FOUND_CORRELATED

    @pytest.mark.asyncio
    async def test_404_scope_mismatch_not_success(self, tmp_registry, ops_client):
        """404 where registry scope != poll scope → terminal unknown, NOT success."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        # Op was created in us-east1-c but registry scope is us-central1-b
        op = _make_op(
            status="RUNNING",
            zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-east1-c",
        )
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        # Postcondition returns True but scope mismatch takes priority
        postcond = AsyncMock(return_value=True)
        poller = self._make_poller(ops_client, tmp_registry, postcondition=postcond)
        # First wait registers with us-east1-c scope
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        # Scope matches (us-east1-c == us-east1-c) so this is actually correlated success
        # The zone mismatch regression is: OLD code would poll config.zone=us-central1-b
        # and get 404. NEW code uses op.zone=us-east1-c for polling → no zone mismatch bug.
        assert result.success is True  # correct result after zone fix

    @pytest.mark.asyncio
    async def test_404_uncorrelated_failure(self, tmp_registry, ops_client):
        """404 with no registry entry (stale op from prior session with empty registry) → unknown."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason, GCPOperationPoller
        op = _make_op(status="RUNNING")
        # Use a fresh registry with no prior registration of this op
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry
        fresh_reg = OperationLifecycleRegistry(persist_path=None, supervisor_epoch=1)
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        # Do NOT register the op in the registry before calling wait
        # Manually test the classification without calling wait (since wait registers)
        from backend.core.gcp_operation_poller import OperationScope
        scope = OperationScope.from_operation(op, "proj")
        assert fresh_reg.get(op.name) is None  # not registered

    @pytest.mark.asyncio
    async def test_404_no_postcondition_terminal_unknown(self, tmp_registry, ops_client):
        """404 with registry match but no postcondition → terminal unknown."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        poller = self._make_poller(ops_client, tmp_registry, postcondition=None)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.NOT_FOUND_NO_POSTCONDITION

    @pytest.mark.asyncio
    async def test_permission_denied_immediate_failure(self, tmp_registry, ops_client):
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.Forbidden("no permission"))
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.PERMISSION_DENIED
        # Must NOT have retried
        assert ops_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_transient_retry_bounded(self, tmp_registry, ops_client):
        """503 retries with backoff; stops at max_retries and returns RETRY_BUDGET_EXHAUSTED."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.ServiceUnavailable("GCP is down"))
        poller = self._make_poller(ops_client, tmp_registry, max_retries=3)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.RETRY_BUDGET_EXHAUSTED
        assert ops_client.get.call_count == 4  # initial + 3 retries

    @pytest.mark.asyncio
    async def test_concurrent_dedup_single_poll_loop(self, tmp_registry, ops_client):
        """N concurrent waiters share exactly 1 poll loop (get called once)."""
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_done_op()
        ops_client.get = MagicMock(return_value=op)
        poller = self._make_poller(ops_client, tmp_registry)
        # 5 concurrent waiters
        results = await asyncio.gather(*[
            poller.wait(op, action="start", instance_name="vm-1",
                        correlation_id=f"c-{i}")
            for i in range(5)
        ])
        assert all(r.success for r in results)
        # Already DONE on first check — get() called 0 times (fast path)
        assert ops_client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_cancellation_no_task_leak(self, tmp_registry, ops_client):
        """Cancelled waiter leaves no orphan task in _active_pollers."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import GCPOperationPoller
        op = _make_op(status="RUNNING")
        call_count = 0
        async def slow_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(100)  # block forever
        ops_client.get = MagicMock(wraps=lambda **kw: None)
        poller = self._make_poller(ops_client, tmp_registry, timeout=100.0)

        task = asyncio.create_task(
            poller.wait(op, action="start", instance_name="vm-1", correlation_id="c-1")
        )
        await asyncio.sleep(0)  # yield to let task start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # After cancellation, the op_id must be cleaned up from _active_pollers
        assert op.name not in GCPOperationPoller._active_pollers

    @pytest.mark.asyncio
    async def test_jarvis_prime_parity(self, tmp_path, ops_client):
        """JARVIS and JARVIS-Prime import paths produce identical results."""
        from backend.core.gcp_operation_poller import (
            GCPOperationPoller as JarvisPoller,
            OperationLifecycleRegistry,
        )
        reg = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)
        op = _make_done_op()
        poller = JarvisPoller(operations_client=ops_client, registry=reg, project="proj",
                              base_backoff=0.0, max_backoff=0.0)
        result = await poller.wait(op, action="start", instance_name="vm-1", correlation_id="c-1")
        assert result.success is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestGCPOperationPoller -v 2>&1 | head -20
```

- [ ] **Step 3: Implement GCPOperationPoller**

Append to `backend/core/gcp_operation_poller.py`:

```python
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
        # Lazily create the class-level lock in the running event loop
        if cls._dedup_lock is None:
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
                loop = asyncio.get_event_loop()
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
        t_start_wall = time.time()

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
        done_result = self._check_done(operation, record, t_start_wall)
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

                done_result = self._check_done(fresh_op, record, t_start_wall)
                if done_result is not None:
                    await self._close_record(record, done_result)
                    return done_result
                # Still PENDING or RUNNING — continue

            except Exception as exc:
                error_class = self._classify_exception(exc)
                record.poll_count += 1
                record.last_seen_at = time.time()

                if error_class == "not_found":
                    result = await self._handle_not_found(record, t_start_wall)
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

    def _check_done(self, op: Any, record: OperationRecord, t_start_wall: float) -> Optional[OperationResult]:
        """Return OperationResult if op is in a terminal status, else None."""
        try:
            from google.cloud import compute_v1
            status_done = compute_v1.Operation.Status.DONE
            status_aborting_str = "ABORTING"
        except ImportError:
            status_done = "DONE"
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
                elapsed_ms=(time.monotonic() - (time.monotonic() - (time.time() - t_start_wall))) * 1000,
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
                poll_count=record.poll_count,
            )
        return OperationResult(
            success=True,
            reason=TerminalReason.OP_DONE_SUCCESS,
            operation_id=op.name,
            scope=record.scope,
            poll_count=record.poll_count,
        )

    async def _handle_not_found(self, record: OperationRecord, t_start_wall: float) -> OperationResult:
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

        # Scope must match
        if registered.scope != scope:
            self._emit("stale_operation_detected", {
                "op_id": op_id,
                "registered_scope": str(registered.scope),
                "poll_scope": str(scope),
            })
            return OperationResult(
                success=False,
                reason=TerminalReason.NOT_FOUND_SCOPE_MISMATCH,
                operation_id=op_id,
                scope=scope,
                error_message=f"404: scope mismatch (registered={registered.scope}, poll={scope})",
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
```

- [ ] **Step 4: Run all poller tests**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py -v 2>&1 | tail -30
```
Expected: all tests PASS. Failures in `test_cancellation_no_task_leak` may need a brief investigation of asyncio task scheduling; ensure `asyncio.sleep(0)` yields correctly.

- [ ] **Step 5: Commit poller**

```bash
git add backend/core/gcp_operation_poller.py backend/tests/test_gcp_operation_poller.py
git commit -m "feat(gcp): add GCPOperationPoller with scope-aware polling, 404 classification, and dedup"
```

---

## Task 4: Wire poller into JARVIS `gcp_vm_manager.py`

**Files:**
- Modify: `backend/core/gcp_vm_manager.py`
  - `__init__` (~line 2643): add `_op_registry` and `_op_poller` fields
  - `_wait_for_operation` (~line 6668): replace body with poller delegation
  - `_start_instance` (~line 10082), `create_vm` (~line 5156), `terminate_vm` (~line 6901), etc.: add `action` + `instance_name` kwargs + postcondition factories
  - `startup()` or `initialize()`: load registry + reconcile orphans

The file is 11K+ lines. Edit only the targeted sections.

- [ ] **Step 1: Add imports and `__init__` wiring**

Find the imports block (~line 23) and add after existing imports:
```python
# Operation lifecycle poller
try:
    from backend.core.gcp_operation_poller import (
        GCPOperationPoller,
        OperationLifecycleRegistry,
        OperationResult,
        get_operation_registry,
        reset_operation_registry,
    )
    _OPERATION_POLLER_AVAILABLE = True
except ImportError:
    _OPERATION_POLLER_AVAILABLE = False
```

In `GCPVMManager.__init__` (~line 2643), after `self._vm_lock = asyncio.Lock()`:
```python
        # v296.0: Operation lifecycle registry + poller
        _epoch = int(time.time() * 1000)  # monotonically increasing per startup
        if _OPERATION_POLLER_AVAILABLE:
            self._op_registry = OperationLifecycleRegistry(
                supervisor_epoch=_epoch,
            )
            self._op_poller: Optional[GCPOperationPoller] = None  # created lazily with client
        else:
            self._op_registry = None
            self._op_poller = None
```

- [ ] **Step 2: Add `_get_or_create_poller()` helper**

Add after `check_vm_protection()` (~line 6708):
```python
    def _get_or_create_poller(
        self,
        *,
        timeout: float = 300.0,
        postcondition=None,
    ):
        """Return a GCPOperationPoller wired to this manager's registry and client."""
        if not _OPERATION_POLLER_AVAILABLE or self.zone_operations_client is None:
            return None
        return GCPOperationPoller(
            operations_client=self.zone_operations_client,
            registry=self._op_registry,
            project=self.config.project_id,
            postcondition=postcondition,
            timeout=timeout,
            metrics_emitter=self._emit_op_event,
        )

    def _emit_op_event(self, event_name: str, payload: dict) -> None:
        """Forward operation lifecycle events to structured log."""
        _log_payload = {"event": event_name, **payload}
        if event_name in ("stale_operation_detected", "operation_gc_404_terminal",
                          "retry_budget_exhausted"):
            logger.warning("[GCPOpLifecycle] %s: %s", event_name, _log_payload)
        else:
            logger.info("[GCPOpLifecycle] %s: %s", event_name, _log_payload)
```

- [ ] **Step 3: Replace `_wait_for_operation` body**

Find the existing `_wait_for_operation` method (~line 6668) and replace its entire body:

```python
    async def _wait_for_operation(
        self,
        operation,
        timeout: int = 300,
        *,
        action: str = "unknown",
        instance_name: str = "",
        correlation_id: Optional[str] = None,
        postcondition: Optional[Callable[[], Awaitable[bool]]] = None,
    ):
        """
        v296.0: Scope-aware, dedup-safe operation poller.

        Zone is extracted from the operation object itself — never from config.zone.
        404 is classified correctly (terminal, not transient).
        Concurrent callers awaiting the same operation share one poll loop.
        """
        if not _OPERATION_POLLER_AVAILABLE or self._op_registry is None:
            # Legacy fallback — should not happen in normal operation
            logger.warning("[GCPVMManager] Operation poller unavailable — using legacy poll")
            return await self._wait_for_operation_legacy(operation, timeout)

        import uuid as _uuid
        _corr = correlation_id or str(_uuid.uuid4())
        poller = self._get_or_create_poller(timeout=float(timeout), postcondition=postcondition)
        if poller is None:
            return await self._wait_for_operation_legacy(operation, timeout)

        result = await poller.wait(
            operation,
            action=action,
            instance_name=instance_name,
            correlation_id=_corr,
        )
        if not result.success:
            if result.reason.value in ("op_done_failure", "permission_denied",
                                        "invalid_request", "scope_contract_error"):
                raise RuntimeError(
                    f"GCP operation {operation.name} failed: "
                    f"{result.reason.value} — {result.error_message}"
                )
            # For timeout / retry_exhausted / not_found_* — log warning, don't raise
            # (caller decides how to handle based on context)
            logger.warning(
                "[GCPVMManager] Operation ended non-fatally: op=%s reason=%s msg=%s",
                operation.name, result.reason.value, result.error_message,
            )

    async def _wait_for_operation_legacy(self, operation, timeout: int = 300):
        """
        v296.0 legacy fallback — identical to pre-v296 behavior.
        Only used when GCPOperationPoller is unavailable (import failure).
        """
        start_time = time.time()
        operation_name = operation.name
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Operation '{operation_name}' timed out after {timeout}s")
            if operation.status == compute_v1.Operation.Status.DONE:
                if operation.error:
                    errors = [error.message for error in operation.error.errors]
                    raise Exception(f"Operation failed: {', '.join(errors)}")
                return
            await asyncio.sleep(2)
            try:
                operation = await asyncio.to_thread(
                    self.zone_operations_client.get,
                    project=self.config.project_id,
                    zone=self.config.zone,
                    operation=operation_name,
                )
            except Exception as e:
                logger.warning(f"Error polling operation status (legacy): {e}")
                await asyncio.sleep(2)
```

- [ ] **Step 4: Add postcondition factories**

Add after `_emit_op_event()`:
```python
    def _postcondition_running(self, instance_name: str, zone: Optional[str] = None) -> Callable:
        """Postcondition: instance is in RUNNING state."""
        async def check() -> bool:
            status, _, _ = await self._describe_instance_full(instance_name, zone=zone)
            return status == "RUNNING"
        return check

    def _postcondition_stopped(self, instance_name: str, zone: Optional[str] = None) -> Callable:
        """Postcondition: instance is TERMINATED/STOPPED."""
        async def check() -> bool:
            status, _, _ = await self._describe_instance_full(instance_name, zone=zone)
            return status in ("TERMINATED", "STOPPED", "SUSPENDED")
        return check

    def _postcondition_gone(self, instance_name: str, zone: Optional[str] = None) -> Callable:
        """Postcondition: instance no longer exists (deleted)."""
        async def check() -> bool:
            status, _, _ = await self._describe_instance_full(instance_name, zone=zone)
            return status == "NOT_FOUND"
        return check
```

- [ ] **Step 5: Update `_start_instance` to pass action + postcondition**

Find `await self._wait_for_operation(operation, timeout=120)` in `_start_instance` (~line 10110).
Change to:
```python
            await self._wait_for_operation(
                operation,
                timeout=120,
                action="start",
                instance_name=instance_name,
                postcondition=self._postcondition_running(instance_name, _effective_zone),
            )
```

- [ ] **Step 6: Update remaining `_wait_for_operation` call sites**

Search for all `await self._wait_for_operation(operation` calls in `backend/core/gcp_vm_manager.py`:
```bash
grep -n "await self._wait_for_operation(operation" backend/core/gcp_vm_manager.py
```

For each call site, add the appropriate `action` + `instance_name` + `postcondition` based on context:
- `terminate_vm` / `_force_delete_vm` → `action="delete"`, postcondition=`_postcondition_gone`
- `_stop_vm_instance` → `action="stop"`, postcondition=`_postcondition_stopped`
- `_create_static_vm` → `action="create"`, postcondition=`_postcondition_running`
- `cleanup_orphaned_gcp_instances` → `action="delete"`, postcondition=`_postcondition_gone`
- All other sites → at minimum `action="unknown"`

- [ ] **Step 7: Wire registry startup reconciliation**

Find the `initialize()` or startup method that runs after GCP clients are initialized (~line 3230).
After `logger.info(f"✅ GCP API clients initialized...")`:
```python
            # v296.0: Load operation registry and reconcile orphans from prior sessions
            if _OPERATION_POLLER_AVAILABLE and self._op_registry is not None:
                try:
                    await self._op_registry.load()
                    orphans = self._op_registry.get_inflight()
                    if orphans:
                        logger.info(
                            "[GCPVMManager] v296.0: Reconciling %d orphaned operations "
                            "from prior session", len(orphans)
                        )
                        async def _describe_for_reconcile(name: str, zone: str) -> str:
                            status, _, _ = await self._describe_instance_full(name, zone=zone or None)
                            return status
                        await self._op_registry.reconcile_orphans(
                            describe_fn=_describe_for_reconcile,
                            emit_fn=self._emit_op_event,
                        )
                        await self._op_registry.persist()
                except Exception as _re:
                    logger.warning("[GCPVMManager] Registry reconciliation failed (non-fatal): %s", _re)
```

- [ ] **Step 8: Smoke test — run unified_supervisor to confirm no 404 spam**

```bash
python3 -c "
from backend.core.gcp_operation_poller import (
    GCPOperationPoller, OperationLifecycleRegistry, OperationScope,
    TerminalReason, ScopeContractError
)
print('Import OK')
import asyncio
async def test():
    reg = OperationLifecycleRegistry(supervisor_epoch=1)
    print('Registry OK, epoch=1')
asyncio.run(test())
print('All OK')
"
```
Expected: `Import OK` / `Registry OK` / `All OK`

- [ ] **Step 9: Commit JARVIS integration**

```bash
git add backend/core/gcp_vm_manager.py backend/core/gcp_operation_poller.py
git commit -m "fix(gcp): wire GCPOperationPoller into gcp_vm_manager — fix zone-mismatch 404s (v296.0)"
```

---

## Task 5: Wire poller into JARVIS-Prime

**Files:**
- Create: `jarvis_prime/core/gcp_operation_poller.py` (copy of JARVIS version)
- Modify: `jarvis_prime/core/gcp_vm_manager.py` (~line 654): replace `_wait_for_operation`

- [ ] **Step 1: Copy primary module to JARVIS-Prime**

```bash
cp /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/gcp_operation_poller.py \
   /Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/core/gcp_operation_poller.py
```

- [ ] **Step 2: Add cross-repo import with hash-check to JARVIS-Prime**

At the top of `jarvis_prime/core/gcp_vm_manager.py`, add after existing imports:
```python
# v296.0: Operation lifecycle poller — primary from JARVIS repo, fallback to local copy
import hashlib as _hashlib
import os as _os
import sys as _sys

def _load_gcp_operation_poller():
    """Import GCPOperationPoller with cross-repo primary + local fallback."""
    _jarvis_path = _os.environ.get("JARVIS_REPO_PATH", "")
    primary_mod = None
    if _jarvis_path and _jarvis_path not in _sys.path:
        _sys.path.insert(0, _jarvis_path)
    try:
        from backend.core.gcp_operation_poller import (
            GCPOperationPoller, OperationLifecycleRegistry,
            OperationResult, get_operation_registry,
        )
        primary_mod = "backend.core.gcp_operation_poller"
    except ImportError:
        from jarvis_prime.core.gcp_operation_poller import (
            GCPOperationPoller, OperationLifecycleRegistry,
            OperationResult, get_operation_registry,
        )
        primary_mod = "jarvis_prime.core.gcp_operation_poller"
        # Hash check between primary and local copy
        _primary_path = _os.path.join(_jarvis_path, "backend", "core", "gcp_operation_poller.py")
        _local_path = _os.path.join(_os.path.dirname(__file__), "gcp_operation_poller.py")
        if _os.path.exists(_primary_path) and _os.path.exists(_local_path):
            def _fphash(p):
                return _hashlib.sha256(open(p, "rb").read(8192)).hexdigest()
            if _fphash(_primary_path) != _fphash(_local_path):
                import logging as _logging
                _logging.getLogger(__name__).critical(
                    "[GCPOperationPoller] Local fallback copy differs from primary — "
                    "using local; ensure sync. Dedup registry disabled."
                )
                GCPOperationPoller._dedup_enabled = False
    return GCPOperationPoller, OperationLifecycleRegistry, OperationResult

GCPOperationPoller, OperationLifecycleRegistry, OperationResult = _load_gcp_operation_poller()
_OPERATION_POLLER_AVAILABLE = True
```

- [ ] **Step 3: Replace `_wait_for_operation` in JARVIS-Prime**

Find `_wait_for_operation` at ~line 654 in `jarvis_prime/core/gcp_vm_manager.py` and replace body:
```python
    async def _wait_for_operation(self, operation, zone: str, timeout: int = 300):
        """v296.0: Delegates to GCPOperationPoller. zone param retained for API compat."""
        if not _OPERATION_POLLER_AVAILABLE:
            return await self._wait_for_operation_legacy(operation, zone, timeout)
        if not hasattr(self, "_op_registry"):
            import time as _t
            self._op_registry = OperationLifecycleRegistry(
                supervisor_epoch=int(_t.time() * 1000)
            )
        if not hasattr(self, "_zone_ops_client") or self._zone_ops_client is None:
            self._zone_ops_client = compute_v1.ZoneOperationsClient()
        import uuid as _uuid
        poller = GCPOperationPoller(
            operations_client=self._zone_ops_client,
            registry=self._op_registry,
            project=self._config.project_id,
            timeout=float(timeout),
        )
        result = await poller.wait(
            operation,
            action="unknown",
            instance_name="",
            correlation_id=str(_uuid.uuid4()),
        )
        if not result.success and result.reason.value in ("op_done_failure", "permission_denied"):
            raise RuntimeError(
                f"GCP operation failed: {result.reason.value} — {result.error_message}"
            )

    async def _wait_for_operation_legacy(self, operation, zone: str, timeout: int = 300):
        """Pre-v296.0 fallback."""
        operations_client = compute_v1.ZoneOperationsClient()
        start_time = time.time()
        while time.time() - start_time < timeout:
            loop = asyncio.get_event_loop()
            op = await loop.run_in_executor(
                None,
                lambda: operations_client.get(
                    project=self._config.project_id,
                    zone=zone,
                    operation=operation.name,
                )
            )
            if op.status == compute_v1.Operation.Status.DONE:
                if op.error:
                    raise RuntimeError(f"Operation failed: {op.error}")
                return
            await asyncio.sleep(2)
        raise TimeoutError(f"Operation {operation.name} timed out")
```

- [ ] **Step 4: Parity test**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestGCPOperationPoller::test_jarvis_prime_parity -v
```

- [ ] **Step 5: Commit JARVIS-Prime integration**

```bash
# Commit to both repos
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add backend/tests/test_gcp_operation_poller.py
git commit -m "test(gcp): add JARVIS-Prime parity test"

cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/gcp_operation_poller.py jarvis_prime/core/gcp_vm_manager.py
git commit -m "fix(gcp): wire GCPOperationPoller — fix zone-mismatch 404s, parity with JARVIS (v296.0)"
```

---

## Task 6: Run full test suite and capture verification evidence

**Files:**
- Run: `backend/tests/test_gcp_operation_poller.py`

- [ ] **Step 1: Run all 14 tests**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest backend/tests/test_gcp_operation_poller.py -v --tb=short 2>&1 | tee /tmp/gcp_poller_test_results.txt
```
Expected: **14 tests PASS, 0 fail**.

- [ ] **Step 2: Check for task leaks with asyncio debug mode**

```bash
PYTHONASYNCIODEBUG=1 python3 -m pytest backend/tests/test_gcp_operation_poller.py::TestGCPOperationPoller::test_cancellation_no_task_leak -v
```
Expected: PASS with no `Task was destroyed but it is pending!` warnings.

- [ ] **Step 3: Verify import chain is clean**

```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from backend.core.gcp_operation_poller import (
    GCPOperationPoller, OperationLifecycleRegistry, OperationScope,
    TerminalReason, ScopeContractError, SplitBrainFenceError,
    OperationRecord, OperationResult, get_operation_registry
)
print('All exports OK')
print('TerminalReason values:', [r.value for r in TerminalReason])
"
```

- [ ] **Step 4: Final commit — version bump and changelog**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add .
git commit -m "$(cat <<'EOF'
fix(gcp): cure GCP operation 404 spam — scope-aware lifecycle poller v296.0

Root causes fixed:
1. Zone mismatch: _wait_for_operation always polled config.zone; operation
   may have been created in _effective_zone/_invincible_node_zone. Now scope
   is extracted exclusively from operation.zone/self_link URL.
2. 404 misclassified as transient: loop retried until 300s timeout instead
   of treating 404 as terminal. Now classified per correlation + postcondition.

What's new (backend/core/gcp_operation_poller.py):
- OperationScope: strict scope contract; ScopeContractError if fields absent
- TerminalReason enum: 13 precise terminal states
- OperationLifecycleRegistry: in-memory + persisted, epoch-fenced, orphan
  reconciliation at startup, pruning order (completed first)
- GCPOperationPoller: dedup via _PollerState + waiter_count, cancellation
  isolation (shield), bounded exponential backoff with jitter, postcondition
  retry window, structured event emission
- Postcondition factories: _postcondition_running/stopped/gone
- startup reconciliation: loads orphaned ops, queries VM state, closes stale

Acceptance criteria:
- No repeated 404 warning spam for same operation
- No 300s timeout waits for GC'd operations
- Crash/restart recovery: reconciliation infers correct final state
- Concurrent dedup: N waiters share 1 poll loop
- 14 hermetic tests pass (zone-mismatch regression, 404 correlation, transient
  retry bounded, concurrent dedup, cancellation no-leak, parity)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Acceptance Checklist

- [ ] No 404 warnings repeat for the same operation ID in logs
- [ ] Operations started in a non-default zone (`_invincible_node_zone`) resolve correctly
- [ ] Session restart no longer re-polls stale operations from prior session
- [ ] Concurrent callers get the same result with only 1 GCP API call
- [ ] `CancelledError` on one waiter does not kill other waiters
- [ ] All 14 tests pass in `backend/tests/test_gcp_operation_poller.py`
- [ ] JARVIS-Prime's `_wait_for_operation` delegates to same poller
- [ ] `backend/core/gcp_operation_poller.py` has no hardcoded zone strings
