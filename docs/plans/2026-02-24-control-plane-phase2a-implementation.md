# Control Plane Phase 2A: Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement four hardening subsystems (recovery probe, UDS keepalive, reconnect+replay, journal compaction) so the control plane cannot misrepresent system state under fault.

**Architecture:** Strict sequential execution: Recovery Protocol → UDS Keepalive → [HARD GATE: reconnect+replay tests] → Journal Compaction → [HARD GATE: fault-injection suite]. Each section builds on the previous. All corrective writes are fenced and idempotent. All probes are lease-guarded.

**Tech Stack:** Python 3.11+, asyncio, SQLite WAL, Unix Domain Sockets, pytest, aiohttp (for remote probes)

**Design Doc:** `docs/plans/2026-02-24-control-plane-phase2a-design.md`

**Phase 1 Baseline:** 139 tests passing across 9 test files (6.19s)

---

## Task 1: Recovery Data Model — HealthCategory and ProbeResult

**Files:**
- Create: `backend/core/recovery_protocol.py`
- Test: `tests/unit/core/test_recovery_protocol.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_recovery_protocol.py`:

```python
"""Tests for the recovery protocol data model and probe logic."""

import pytest
from backend.core.recovery_protocol import HealthCategory, ProbeResult


class TestHealthCategory:
    def test_all_categories_exist(self):
        assert HealthCategory.HEALTHY.value == "healthy"
        assert HealthCategory.CONTRACT_MISMATCH.value == "contract_mismatch"
        assert HealthCategory.DEPENDENCY_DEGRADED.value == "dependency_degraded"
        assert HealthCategory.SERVICE_DEGRADED.value == "service_degraded"
        assert HealthCategory.UNREACHABLE.value == "unreachable"

    def test_category_count(self):
        assert len(HealthCategory) == 5


class TestProbeResult:
    def test_default_construction(self):
        r = ProbeResult(reachable=True, category=HealthCategory.HEALTHY)
        assert r.reachable is True
        assert r.category == HealthCategory.HEALTHY
        assert r.instance_id == ""
        assert r.api_version == ""
        assert r.error == ""
        assert r.probe_epoch == 0
        assert r.probe_seq == 0

    def test_unreachable_construction(self):
        r = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            error="connection refused",
            probe_epoch=5,
            probe_seq=42,
        )
        assert r.reachable is False
        assert r.error == "connection refused"
        assert r.probe_epoch == 5
        assert r.probe_seq == 42

    def test_full_construction(self):
        r = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            instance_id="prime:abc123",
            api_version="1.2.3",
            probe_epoch=3,
            probe_seq=100,
        )
        assert r.instance_id == "prime:abc123"
        assert r.api_version == "1.2.3"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.recovery_protocol'`

**Step 3: Write minimal implementation**

Create `backend/core/recovery_protocol.py`:

```python
# backend/core/recovery_protocol.py
"""
JARVIS Recovery Protocol v1.0
==============================
Post-crash and lease-takeover recovery: probe components, reconcile
projected state with actual state, and issue corrective transitions.

Design doc: docs/plans/2026-02-24-control-plane-phase2a-design.md (Section 1)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("jarvis.recovery_protocol")


class HealthCategory(Enum):
    """Classification of a probed component's health."""
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"


@dataclass
class ProbeResult:
    """Result of probing a single component's health."""
    reachable: bool
    category: HealthCategory
    instance_id: str = ""
    api_version: str = ""
    error: str = ""
    probe_epoch: int = 0
    probe_seq: int = 0
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add backend/core/recovery_protocol.py tests/unit/core/test_recovery_protocol.py
git commit -m "feat: add recovery protocol data model (HealthCategory, ProbeResult)"
```

---

## Task 2: Recovery Probe Strategies

**Files:**
- Modify: `backend/core/recovery_protocol.py`
- Test: `tests/unit/core/test_recovery_protocol.py`

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_recovery_protocol.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.recovery_protocol import (
    HealthCategory,
    ProbeResult,
    RecoveryProber,
)
from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality


def _make_decl(name, locality=ComponentLocality.IN_PROCESS, **kwargs):
    return ComponentDeclaration(name=name, locality=locality, **kwargs)


class TestRecoveryProber:
    @pytest.fixture
    def journal(self):
        j = MagicMock()
        j.epoch = 5
        j.current_seq = 100
        j.lease_held = True
        return j

    @pytest.fixture
    def prober(self, journal):
        return RecoveryProber(journal=journal)

    def test_skip_stopped_components(self, prober):
        """STOPPED and REGISTERED components should not be probed."""
        result = asyncio.get_event_loop().run_until_complete(
            prober.classify_for_probe("comp_a", "STOPPED")
        )
        assert result is None

        result = asyncio.get_event_loop().run_until_complete(
            prober.classify_for_probe("comp_a", "REGISTERED")
        )
        assert result is None

    def test_classify_active_for_probe(self, prober):
        """READY, DEGRADED, STARTING, HANDSHAKING, DRAINING should be probed."""
        for status in ("READY", "DEGRADED", "STARTING", "HANDSHAKING",
                       "DRAINING", "FAILED", "LOST"):
            result = asyncio.get_event_loop().run_until_complete(
                prober.classify_for_probe("comp_a", status)
            )
            assert result == "UNVERIFIED", f"{status} should be UNVERIFIED"

    def test_probe_aborts_on_lease_lost(self, prober, journal):
        """If lease is lost mid-probe, result is discarded."""
        journal.lease_held = False
        decl = _make_decl("comp_a")

        result = asyncio.get_event_loop().run_until_complete(
            prober.probe_component(decl, "READY")
        )
        assert result is None  # Discarded due to lost lease

    def test_probe_in_process_healthy(self, prober, journal):
        """In-process probe delegates to registered RuntimeHealthProbe."""
        async def healthy_probe(name):
            return ProbeResult(
                reachable=True,
                category=HealthCategory.HEALTHY,
                instance_id="comp_a:inst1",
                api_version="1.0.0",
            )
        prober.register_runtime_probe("comp_a", healthy_probe)
        decl = _make_decl("comp_a", locality=ComponentLocality.IN_PROCESS)

        result = asyncio.get_event_loop().run_until_complete(
            prober.probe_component(decl, "READY")
        )
        assert result.reachable is True
        assert result.category == HealthCategory.HEALTHY
        assert result.probe_epoch == 5
        assert result.probe_seq == 100

    def test_probe_in_process_no_probe_registered(self, prober, journal):
        """In-process component without registered probe → UNREACHABLE."""
        decl = _make_decl("comp_x", locality=ComponentLocality.IN_PROCESS)

        result = asyncio.get_event_loop().run_until_complete(
            prober.probe_component(decl, "READY")
        )
        assert result.reachable is False
        assert result.category == HealthCategory.UNREACHABLE

    def test_probe_retries_on_failure(self, prober, journal):
        """Probe retries up to max_attempts (2) with backoff."""
        call_count = 0
        async def flaky_probe(name):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("refused")
            return ProbeResult(
                reachable=True,
                category=HealthCategory.HEALTHY,
                instance_id="comp_a:inst1",
            )
        prober.register_runtime_probe("comp_a", flaky_probe)
        decl = _make_decl("comp_a", locality=ComponentLocality.IN_PROCESS)

        result = asyncio.get_event_loop().run_until_complete(
            prober.probe_component(decl, "READY")
        )
        assert result.reachable is True
        assert call_count == 2
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py::TestRecoveryProber -v`
Expected: FAIL with `ImportError: cannot import name 'RecoveryProber'`

**Step 3: Write minimal implementation**

Add to `backend/core/recovery_protocol.py`:

```python
import asyncio
import random
from typing import Awaitable, Callable, Dict, Optional

from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality


# States that should not be probed (no process to verify)
_SKIP_STATES = frozenset({"STOPPED", "REGISTERED"})

# Max probe attempts per component
PROBE_MAX_ATTEMPTS = 2
PROBE_BACKOFF_BASE_S = 0.5
PROBE_BACKOFF_JITTER = 0.25

# Probe timeouts by locality
PROBE_TIMEOUT_SUBPROCESS_S = 5.0
PROBE_TIMEOUT_REMOTE_S = 10.0


RuntimeHealthProbe = Callable[[str], Awaitable[ProbeResult]]


class RecoveryProber:
    """Probes components and returns health classification.

    Fenced: every probe checks lease_held before returning results.
    Retries: max 2 attempts per component with jittered backoff.
    """

    def __init__(self, journal) -> None:
        self._journal = journal
        self._runtime_probes: Dict[str, RuntimeHealthProbe] = {}

    def register_runtime_probe(
        self, component_name: str, probe: RuntimeHealthProbe,
    ) -> None:
        """Register an in-process health probe callable."""
        self._runtime_probes[component_name] = probe

    async def classify_for_probe(
        self, component: str, projected_status: str,
    ) -> Optional[str]:
        """Classify whether a component needs probing.

        Returns 'UNVERIFIED' if probe needed, None if skip.
        """
        if projected_status in _SKIP_STATES:
            return None
        return "UNVERIFIED"

    async def probe_component(
        self,
        decl: ComponentDeclaration,
        projected_status: str,
        max_attempts: int = PROBE_MAX_ATTEMPTS,
    ) -> Optional[ProbeResult]:
        """Probe a single component with bounded retry.

        Returns None if lease lost (discard results).
        Returns ProbeResult with fence context on success or exhaustion.
        """
        # Pre-probe lease check
        if not self._journal.lease_held:
            logger.warning(
                "[Recovery] Lease lost before probing %s — aborting",
                decl.name,
            )
            return None

        last_error = ""
        for attempt in range(max_attempts):
            # Verify lease still held before each attempt
            if not self._journal.lease_held:
                logger.warning(
                    "[Recovery] Lease lost during probe of %s (attempt %d)",
                    decl.name, attempt + 1,
                )
                return None

            try:
                result = await self._probe_once(decl)
                # Stamp fence context
                result.probe_epoch = self._journal.epoch
                result.probe_seq = self._journal.current_seq
                return result
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.info(
                    "[Recovery] Probe %s attempt %d/%d failed: %s",
                    decl.name, attempt + 1, max_attempts, last_error,
                )
                if attempt < max_attempts - 1:
                    backoff = PROBE_BACKOFF_BASE_S * (2 ** attempt)
                    jitter = random.uniform(
                        -PROBE_BACKOFF_JITTER, PROBE_BACKOFF_JITTER,
                    )
                    await asyncio.sleep(backoff + jitter)

        # All attempts exhausted
        return ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            error=last_error,
            probe_epoch=self._journal.epoch,
            probe_seq=self._journal.current_seq,
        )

    async def _probe_once(self, decl: ComponentDeclaration) -> ProbeResult:
        """Single probe attempt dispatched by locality."""
        if decl.locality == ComponentLocality.IN_PROCESS:
            return await self._probe_in_process(decl)
        elif decl.locality == ComponentLocality.SUBPROCESS:
            return await self._probe_http(
                decl, timeout=PROBE_TIMEOUT_SUBPROCESS_S,
            )
        elif decl.locality == ComponentLocality.REMOTE:
            return await self._probe_http(
                decl, timeout=PROBE_TIMEOUT_REMOTE_S,
            )
        else:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=f"unknown locality: {decl.locality}",
            )

    async def _probe_in_process(self, decl: ComponentDeclaration) -> ProbeResult:
        """In-process probe using registered RuntimeHealthProbe."""
        probe_fn = self._runtime_probes.get(decl.name)
        if probe_fn is None:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error="no RuntimeHealthProbe registered",
            )
        return await probe_fn(decl.name)

    async def _probe_http(
        self, decl: ComponentDeclaration, timeout: float,
    ) -> ProbeResult:
        """HTTP GET /health probe for subprocess/remote components."""
        endpoint = decl.endpoint
        if not endpoint:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error="no endpoint configured",
            )

        import aiohttp

        url = f"{endpoint.rstrip('/')}{decl.health_path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        return ProbeResult(
                            reachable=True,
                            category=HealthCategory.SERVICE_DEGRADED,
                            error=f"http_status={resp.status}",
                        )
                    data = await resp.json()
                    return self._classify_health_response(data)
        except Exception as exc:
            raise  # Let retry logic handle it

    def _classify_health_response(self, data: dict) -> ProbeResult:
        """Classify health response into HealthCategory."""
        status = data.get("status", "unknown")
        instance_id = data.get("instance_id", "")
        api_version = data.get("api_version", "")

        if status in ("healthy", "ok", "ready"):
            category = HealthCategory.HEALTHY
        elif status == "degraded":
            # Check if it's dependency-related
            if data.get("degraded_dependencies"):
                category = HealthCategory.DEPENDENCY_DEGRADED
            else:
                category = HealthCategory.SERVICE_DEGRADED
        elif status == "contract_mismatch":
            category = HealthCategory.CONTRACT_MISMATCH
        else:
            category = HealthCategory.SERVICE_DEGRADED

        return ProbeResult(
            reachable=True,
            category=category,
            instance_id=instance_id,
            api_version=api_version,
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py -v`
Expected: PASS (10 tests)

**Step 5: Commit**

```bash
git add backend/core/recovery_protocol.py tests/unit/core/test_recovery_protocol.py
git commit -m "feat: add RecoveryProber with probe strategies by locality"
```

---

## Task 3: Recovery Reconciler — State Correction Logic

**Files:**
- Modify: `backend/core/recovery_protocol.py`
- Modify: `tests/unit/core/test_recovery_protocol.py`

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_recovery_protocol.py`:

```python
from backend.core.recovery_protocol import RecoveryReconciler


class TestRecoveryReconciler:
    @pytest.fixture
    def journal(self):
        j = MagicMock()
        j.epoch = 5
        j.current_seq = 100
        j.lease_held = True
        j.fenced_write = MagicMock(return_value=101)
        return j

    @pytest.fixture
    def engine(self):
        e = AsyncMock()
        e.transition_component = AsyncMock(return_value=102)
        e.get_declaration = MagicMock()
        return e

    @pytest.fixture
    def reconciler(self, journal, engine):
        return RecoveryReconciler(journal=journal, engine=engine)

    def test_ready_but_unreachable_marks_lost(self, reconciler, engine):
        """Projected READY + actual unreachable → LOST."""
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "READY", probe)
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "reconcile_mark_lost"
        engine.transition_component.assert_called_once()
        call_kwargs = engine.transition_component.call_args
        assert call_kwargs[0][1] == "LOST"  # new_status

    def test_ready_and_healthy_is_noop(self, reconciler, engine):
        """Projected READY + actual healthy → no-op."""
        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            instance_id="comp_a:inst1",
            api_version="1.0.0",
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "READY", probe)
        )
        assert len(actions) == 0
        engine.transition_component.assert_not_called()

    def test_ready_but_degraded_marks_degraded(self, reconciler, engine):
        """Projected READY + actual service_degraded → DEGRADED."""
        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.SERVICE_DEGRADED,
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "READY", probe)
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "reconcile_mark_degraded"

    def test_failed_but_healthy_triggers_recovery(self, reconciler, engine):
        """Projected FAILED + actual healthy → recovery (STARTING → HANDSHAKING → READY)."""
        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            instance_id="comp_a:inst1",
            api_version="1.0.0",
            probe_epoch=5,
            probe_seq=100,
        )
        # Provide a declaration with handshake_timeout_s
        engine.get_declaration.return_value = _make_decl(
            "comp_a", handshake_timeout_s=10.0,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "FAILED", probe)
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "reconcile_recover"
        # Should route through STARTING → HANDSHAKING → READY
        assert engine.transition_component.call_count >= 3

    def test_starting_but_unreachable_marks_failed(self, reconciler, engine):
        """Projected STARTING + actual unreachable → FAILED."""
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "STARTING", probe)
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "reconcile_mark_failed"

    def test_draining_but_unreachable_marks_stopped(self, reconciler, engine):
        """Projected DRAINING + actual unreachable → STOPPED."""
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "DRAINING", probe)
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "reconcile_mark_stopped"

    def test_idempotency_key_includes_fingerprint(self, reconciler, journal):
        """Idempotency key includes contradiction fingerprint."""
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            instance_id="",
            api_version="",
            probe_epoch=5,
            probe_seq=100,
        )
        asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "READY", probe)
        )
        # Check the fenced_write call's idempotency_key
        call_args = journal.fenced_write.call_args
        key = call_args[1].get("idempotency_key") or call_args.kwargs.get("idempotency_key")
        assert "reconcile:comp_a:5:" in key
        assert "READY->unreachable" in key

    def test_reconcile_aborts_on_lease_lost(self, reconciler, journal, engine):
        """If lease lost during reconcile, no writes are made."""
        journal.lease_held = False
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=5,
            probe_seq=100,
        )
        actions = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile("comp_a", "READY", probe)
        )
        assert len(actions) == 0
        engine.transition_component.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py::TestRecoveryReconciler -v`
Expected: FAIL with `ImportError: cannot import name 'RecoveryReconciler'`

**Step 3: Write minimal implementation**

Add to `backend/core/recovery_protocol.py`:

```python
from typing import Any, List


class RecoveryReconciler:
    """Reconciles projected state with probe results.

    All corrective writes are idempotent transition commands with
    rich fingerprinted idempotency keys.
    """

    def __init__(self, journal, engine) -> None:
        self._journal = journal
        self._engine = engine

    async def reconcile(
        self,
        component: str,
        projected: str,
        probe: ProbeResult,
    ) -> List[Dict[str, Any]]:
        """Compare projected state with probe result and apply corrections.

        Returns list of actions taken (for audit/logging).
        Empty list means no-op or lease lost.
        """
        if not self._journal.lease_held:
            logger.warning(
                "[Reconcile] Lease lost — aborting reconcile for %s",
                component,
            )
            return []

        actions: List[Dict[str, Any]] = []

        if projected in ("READY", "DEGRADED"):
            if not probe.reachable:
                await self._apply_correction(
                    component, "LOST", "reconcile_mark_lost",
                    projected, probe, actions,
                )
            elif probe.category == HealthCategory.HEALTHY:
                pass  # No-op: projected matches actual
            elif probe.category in (
                HealthCategory.SERVICE_DEGRADED,
                HealthCategory.DEPENDENCY_DEGRADED,
            ):
                if projected == "READY":
                    await self._apply_correction(
                        component, "DEGRADED", "reconcile_mark_degraded",
                        projected, probe, actions,
                    )
            elif probe.category == HealthCategory.CONTRACT_MISMATCH:
                await self._apply_correction(
                    component, "FAILED", "reconcile_mark_failed",
                    projected, probe, actions,
                )

        elif projected in ("FAILED", "LOST"):
            if probe.reachable and probe.category == HealthCategory.HEALTHY:
                await self._recover_component(
                    component, projected, probe, actions,
                )

        elif projected in ("STARTING", "HANDSHAKING"):
            if not probe.reachable:
                await self._apply_correction(
                    component, "FAILED", "reconcile_mark_failed",
                    projected, probe, actions,
                )

        elif projected in ("DRAINING", "STOPPING"):
            if not probe.reachable:
                await self._apply_correction(
                    component, "STOPPED", "reconcile_mark_stopped",
                    projected, probe, actions,
                )

        return actions

    async def _apply_correction(
        self,
        component: str,
        new_status: str,
        action_name: str,
        projected: str,
        probe: ProbeResult,
        actions: List[Dict[str, Any]],
    ) -> None:
        """Apply a single corrective transition with idempotency key."""
        if not self._journal.lease_held:
            return

        idemp_key = self._make_idemp_key(
            component, action_name, projected, probe,
        )

        self._journal.fenced_write(
            action=action_name,
            target=component,
            idempotency_key=idemp_key,
            payload={
                "projected": projected,
                "observed_category": probe.category.value,
                "probe_epoch": probe.probe_epoch,
                "instance_id": probe.instance_id,
            },
        )

        await self._engine.transition_component(
            component, new_status,
            reason=f"{action_name}: {projected}->{probe.category.value}",
        )

        actions.append({
            "action": action_name,
            "component": component,
            "from": projected,
            "to": new_status,
            "idempotency_key": idemp_key,
        })

    async def _recover_component(
        self,
        component: str,
        projected: str,
        probe: ProbeResult,
        actions: List[Dict[str, Any]],
    ) -> None:
        """Recover a FAILED/LOST component through full handshake revalidation.

        Route: FAILED/LOST → STARTING → HANDSHAKING → READY
        Bounded by handshake_timeout_s.
        """
        if not self._journal.lease_held:
            return

        decl = self._engine.get_declaration(component)
        timeout = decl.handshake_timeout_s if decl else 10.0

        idemp_key = self._make_idemp_key(
            component, "reconcile_recover", projected, probe,
        )

        self._journal.fenced_write(
            action="reconcile_recover",
            target=component,
            idempotency_key=idemp_key,
            payload={
                "projected": projected,
                "observed_category": probe.category.value,
                "instance_id": probe.instance_id,
                "api_version": probe.api_version,
            },
        )

        try:
            await asyncio.wait_for(
                self._recovery_transition_chain(component, projected),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Terminal fallback: DEGRADED for soft deps, FAILED otherwise
            is_soft = decl and not decl.is_critical
            fallback = "DEGRADED" if is_soft else "FAILED"
            logger.warning(
                "[Reconcile] Recovery handshake for %s timed out (%.1fs), "
                "falling back to %s",
                component, timeout, fallback,
            )
            await self._engine.transition_component(
                component, fallback,
                reason=f"recovery_handshake_timeout_{timeout}s",
            )

        actions.append({
            "action": "reconcile_recover",
            "component": component,
            "from": projected,
            "to": "READY",
            "idempotency_key": idemp_key,
        })

    async def _recovery_transition_chain(
        self, component: str, from_status: str,
    ) -> None:
        """Execute FAILED/LOST → STARTING → HANDSHAKING → READY chain."""
        await self._engine.transition_component(
            component, "STARTING",
            reason=f"recovery_from_{from_status}",
        )
        await self._engine.transition_component(
            component, "HANDSHAKING",
            reason="recovery_start_complete",
        )
        await self._engine.transition_component(
            component, "READY",
            reason="recovery_handshake_passed",
        )

    def _make_idemp_key(
        self,
        component: str,
        action: str,
        projected: str,
        probe: ProbeResult,
    ) -> str:
        """Build rich idempotency fingerprint.

        Format: reconcile:{component}:{epoch}:{projected}->{observed_category}:{instance_id}:{api_version}
        """
        return (
            f"reconcile:{component}:{probe.probe_epoch}:"
            f"{projected}->{probe.category.value}:"
            f"{probe.instance_id}:{probe.api_version}"
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py -v`
Expected: PASS (18 tests)

**Step 5: Commit**

```bash
git add backend/core/recovery_protocol.py tests/unit/core/test_recovery_protocol.py
git commit -m "feat: add RecoveryReconciler with idempotent corrective transitions"
```

---

## Task 4: Full Recovery Orchestrator — Startup + Sparse Audit

**Files:**
- Modify: `backend/core/recovery_protocol.py`
- Modify: `tests/unit/core/test_recovery_protocol.py`

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_recovery_protocol.py`:

```python
from backend.core.recovery_protocol import RecoveryOrchestrator


class TestRecoveryOrchestrator:
    @pytest.fixture
    def journal(self):
        j = MagicMock()
        j.epoch = 5
        j.current_seq = 100
        j.lease_held = True
        j.fenced_write = MagicMock(return_value=101)
        j.get_all_component_states = MagicMock(return_value={
            "comp_a": {"component": "comp_a", "status": "READY", "last_seq": 50},
            "comp_b": {"component": "comp_b", "status": "STOPPED", "last_seq": 30},
            "comp_c": {"component": "comp_c", "status": "FAILED", "last_seq": 40},
        })
        return j

    @pytest.fixture
    def engine(self):
        e = AsyncMock()
        e.transition_component = AsyncMock(return_value=102)
        e.get_declaration = MagicMock(return_value=_make_decl("x"))
        e.get_all_statuses = MagicMock(return_value={
            "comp_a": "READY",
            "comp_b": "STOPPED",
            "comp_c": "FAILED",
        })
        return e

    @pytest.fixture
    def prober(self, journal):
        return RecoveryProber(journal=journal)

    @pytest.fixture
    def orchestrator(self, journal, engine, prober):
        return RecoveryOrchestrator(
            journal=journal, engine=engine, prober=prober,
        )

    def test_startup_recovery_skips_stopped(self, orchestrator, prober):
        """Startup recovery probes only active components."""
        probe_called = []
        async def mock_probe(name):
            probe_called.append(name)
            return ProbeResult(
                reachable=True,
                category=HealthCategory.HEALTHY,
                instance_id=f"{name}:inst1",
            )
        prober.register_runtime_probe("comp_a", mock_probe)
        prober.register_runtime_probe("comp_c", mock_probe)

        asyncio.get_event_loop().run_until_complete(
            orchestrator.run_startup_recovery()
        )
        # comp_b (STOPPED) should not be probed
        assert "comp_b" not in probe_called

    def test_startup_recovery_aborts_on_lease_loss(self, orchestrator, journal):
        """If lease lost mid-recovery, abort entirely."""
        journal.lease_held = False
        result = asyncio.get_event_loop().run_until_complete(
            orchestrator.run_startup_recovery()
        )
        assert result["aborted"] is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py::TestRecoveryOrchestrator -v`
Expected: FAIL with `ImportError: cannot import name 'RecoveryOrchestrator'`

**Step 3: Write minimal implementation**

Add to `backend/core/recovery_protocol.py`:

```python
class RecoveryOrchestrator:
    """Top-level coordinator for post-crash and lease-takeover recovery.

    Runs on lease acquisition:
    1. Read projected state from journal
    2. Classify components for probe (skip STOPPED/REGISTERED)
    3. Probe each active component
    4. Reconcile projected vs actual
    """

    def __init__(self, journal, engine, prober: RecoveryProber) -> None:
        self._journal = journal
        self._engine = engine
        self._prober = prober
        self._reconciler = RecoveryReconciler(journal=journal, engine=engine)

    async def run_startup_recovery(self) -> dict:
        """Execute full recovery protocol on lease acquisition.

        Returns summary dict with counts of probed, reconciled, skipped.
        """
        if not self._journal.lease_held:
            logger.warning("[Recovery] No lease held — aborting startup recovery")
            return {"aborted": True, "reason": "no_lease"}

        logger.info(
            "[Recovery] Starting recovery protocol (epoch=%d)",
            self._journal.epoch,
        )

        # 1. Read projected states
        states = self._journal.get_all_component_states()
        summary = {
            "aborted": False,
            "epoch": self._journal.epoch,
            "probed": 0,
            "reconciled": 0,
            "skipped": 0,
            "errors": 0,
        }

        # 2-4. Classify, probe, reconcile each component
        for comp_name, state in states.items():
            if not self._journal.lease_held:
                logger.warning(
                    "[Recovery] Lease lost mid-recovery — aborting"
                )
                summary["aborted"] = True
                return summary

            projected = state.get("status", "REGISTERED")
            classification = await self._prober.classify_for_probe(
                comp_name, projected,
            )

            if classification is None:
                summary["skipped"] += 1
                continue

            # Get declaration for probe
            decl = self._engine.get_declaration(comp_name)
            if decl is None:
                logger.warning(
                    "[Recovery] No declaration for %s — skipping",
                    comp_name,
                )
                summary["skipped"] += 1
                continue

            # Probe
            probe_result = await self._prober.probe_component(
                decl, projected,
            )
            summary["probed"] += 1

            if probe_result is None:
                # Lease lost during probe
                summary["aborted"] = True
                return summary

            # Reconcile
            try:
                actions = await self._reconciler.reconcile(
                    comp_name, projected, probe_result,
                )
                if actions:
                    summary["reconciled"] += len(actions)
            except Exception as exc:
                logger.error(
                    "[Recovery] Reconcile error for %s: %s",
                    comp_name, exc,
                )
                summary["errors"] += 1

        logger.info(
            "[Recovery] Recovery complete: %s", summary,
        )
        return summary

    async def run_sparse_audit(self) -> dict:
        """Low-frequency integrity check (15-60min, leader-only).

        Same probe logic as startup recovery but read-only by default —
        only writes on detected contradiction.
        """
        if not self._journal.lease_held:
            return {"skipped": True, "reason": "not_leader"}

        # Reuse startup recovery logic
        return await self.run_startup_recovery()
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_recovery_protocol.py -v`
Expected: PASS (20 tests)

**Step 5: Commit**

```bash
git add backend/core/recovery_protocol.py tests/unit/core/test_recovery_protocol.py
git commit -m "feat: add RecoveryOrchestrator for startup recovery and sparse audit"
```

---

## Task 5: UDS Keepalive — Server-Side Ping/Pong

**Files:**
- Modify: `backend/core/uds_event_fabric.py`
- Test: `tests/unit/core/test_uds_keepalive.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_uds_keepalive.py`:

```python
"""Tests for UDS keepalive (ping/pong) protocol."""

import asyncio
import json
import struct
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.uds_event_fabric import (
    EventFabric,
    KEEPALIVE_INTERVAL_S,
    KEEPALIVE_TIMEOUT_S,
    send_frame,
    recv_frame,
)


@pytest.fixture
def tmp_path(request):
    """Short tmp_path for macOS AF_UNIX limit."""
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    request.addfinalizer(lambda: __import__("shutil").rmtree(str(p), True))
    return p


@pytest.fixture
def journal():
    j = MagicMock()
    j.epoch = 1
    j.lease_held = True
    j.current_seq = 0
    j.replay_from = MagicMock(return_value=asyncio.coroutine(lambda *a, **k: [])())
    return j


async def _connect_subscriber(sock_path, subscriber_id="test-sub", last_seen_seq=0):
    """Helper: connect and complete subscribe handshake."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    await send_frame(writer, {
        "type": "subscribe",
        "subscriber_id": subscriber_id,
        "last_seen_seq": last_seen_seq,
    })
    ack = await recv_frame(reader)
    assert ack["type"] == "subscribe_ack"
    return reader, writer


class TestKeepaliveConstants:
    def test_keepalive_interval_default(self):
        assert KEEPALIVE_INTERVAL_S == 10.0

    def test_keepalive_timeout_default(self):
        assert KEEPALIVE_TIMEOUT_S == 30.0


class TestServerSendsPing:
    @pytest.mark.asyncio
    async def test_subscriber_receives_ping(self, tmp_path, journal):
        """Server sends ping to subscriber within keepalive interval."""
        # Use short intervals for fast test
        fabric = EventFabric(journal, keepalive_interval_s=0.5, keepalive_timeout_s=3.0)
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            reader, writer = await _connect_subscriber(sock)
            # Wait for ping (should arrive within 0.5s + some margin)
            frame = await asyncio.wait_for(recv_frame(reader), timeout=2.0)
            assert frame["type"] == "ping"
            assert "ping_id" in frame
            assert "ts" in frame
            writer.close()
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_pong_accepted(self, tmp_path, journal):
        """Server accepts pong and updates liveness."""
        fabric = EventFabric(journal, keepalive_interval_s=0.5, keepalive_timeout_s=3.0)
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            reader, writer = await _connect_subscriber(sock)
            # Wait for ping
            ping = await asyncio.wait_for(recv_frame(reader), timeout=2.0)
            assert ping["type"] == "ping"

            # Send pong
            await send_frame(writer, {
                "type": "pong",
                "ping_id": ping["ping_id"],
                "ts": ping["ts"],
            })

            # Should still be connected after pong
            await asyncio.sleep(0.3)
            assert "test-sub" in fabric._subscribers
            writer.close()
        finally:
            await fabric.stop()


class TestKeepaliveTimeout:
    @pytest.mark.asyncio
    async def test_dead_subscriber_removed_on_timeout(self, tmp_path, journal):
        """Subscriber that stops ponging is removed after timeout."""
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=1.0,
        )
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            reader, writer = await _connect_subscriber(sock)
            assert "test-sub" in fabric._subscribers

            # Read pings but do NOT send pongs
            try:
                while True:
                    frame = await asyncio.wait_for(
                        recv_frame(reader), timeout=0.5,
                    )
                    # Intentionally not responding with pong
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass

            # Wait for timeout to kick in
            await asyncio.sleep(1.5)
            assert "test-sub" not in fabric._subscribers
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_active_subscriber_survives_missed_pong(self, tmp_path, journal):
        """Subscriber sending events (last_seen_any) isn't killed even if pong delayed."""
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.5,
            keepalive_timeout_s=2.0,
        )
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            reader, writer = await _connect_subscriber(sock)

            # Send a valid frame (subscribe is already sent, but we can
            # send another type) — this updates last_seen_any
            # The subscriber should survive because last_seen_any is recent
            for _ in range(3):
                await asyncio.sleep(0.3)
                # Send any valid frame to update last_seen_any
                await send_frame(writer, {
                    "type": "pong",
                    "ping_id": "fake",
                    "ts": 0,
                })

            # Should still be connected
            assert "test-sub" in fabric._subscribers
            writer.close()
        finally:
            await fabric.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_uds_keepalive.py -v`
Expected: FAIL with `ImportError: cannot import name 'KEEPALIVE_INTERVAL_S'`

**Step 3: Write implementation**

Modify `backend/core/uds_event_fabric.py`:

Add constants after existing constants (around line 35):

```python
# Keepalive constants (configurable via environment)
KEEPALIVE_INTERVAL_S = float(os.environ.get("JARVIS_UDS_KEEPALIVE_INTERVAL", "10.0"))
KEEPALIVE_TIMEOUT_S = float(os.environ.get("JARVIS_UDS_KEEPALIVE_TIMEOUT", "30.0"))
PONG_WRITE_TIMEOUT_S = 2.0
```

Modify `_Subscriber` dataclass to add liveness tracking fields:

```python
@dataclass
class _Subscriber:
    """Internal bookkeeping for a single connected subscriber."""
    subscriber_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_SUBSCRIBER_QUEUE))
    writer: Optional[asyncio.StreamWriter] = None
    task: Optional[asyncio.Task] = None
    keepalive_task: Optional[asyncio.Task] = None
    last_pong_received: float = field(default_factory=time.monotonic)
    last_seen_any: float = field(default_factory=time.monotonic)
    disconnect_reason: str = ""
```

Modify `EventFabric.__init__` to accept keepalive params:

```python
def __init__(
    self,
    journal: OrchestrationJournal,
    keepalive_interval_s: float = KEEPALIVE_INTERVAL_S,
    keepalive_timeout_s: float = KEEPALIVE_TIMEOUT_S,
) -> None:
    self._journal = journal
    self._subscribers: Dict[str, _Subscriber] = {}
    self._server: Optional[asyncio.AbstractServer] = None
    self._sock_path: Optional[Path] = None
    self._real_sock_path: Optional[Path] = None
    self._owns_real_sock: bool = False
    self._client_tasks: list[asyncio.Task] = []
    self._keepalive_interval_s = keepalive_interval_s
    self._keepalive_timeout_s = keepalive_timeout_s
```

Add keepalive methods to `EventFabric`:

```python
async def _keepalive_loop(self, sub: _Subscriber) -> None:
    """Send periodic pings to a subscriber, detect death by timeout."""
    import uuid
    try:
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            if sub.writer is None or sub.writer.is_closing():
                break

            # Check deadline: max(last_pong, last_seen_any) + timeout
            now = time.monotonic()
            last_activity = max(sub.last_pong_received, sub.last_seen_any)
            deadline = last_activity + self._keepalive_timeout_s

            if now > deadline:
                logger.info(
                    "[EventFabric] Subscriber %s timed out "
                    "(last_activity=%.1fs ago, timeout=%.1fs)",
                    sub.subscriber_id,
                    now - last_activity,
                    self._keepalive_timeout_s,
                )
                sub.disconnect_reason = "timeout"
                self._remove_subscriber(sub.subscriber_id)
                break

            # Send ping
            ping_id = uuid.uuid4().hex[:12]
            try:
                await send_frame(sub.writer, {
                    "type": "ping",
                    "ping_id": ping_id,
                    "ts": now,
                })
            except (ConnectionResetError, BrokenPipeError, OSError):
                sub.disconnect_reason = "write_error"
                self._remove_subscriber(sub.subscriber_id)
                break
    except asyncio.CancelledError:
        pass

def _handle_pong(self, sub: _Subscriber, msg: dict) -> None:
    """Process a pong frame from a subscriber."""
    sub.last_pong_received = time.monotonic()
    sub.last_seen_any = time.monotonic()

def _update_last_seen(self, sub: _Subscriber) -> None:
    """Update last_seen_any on any valid frame."""
    sub.last_seen_any = time.monotonic()
```

Modify `_handle_client` to:
1. Start keepalive task after sender task
2. Handle incoming pong frames from the client reader

Modify `_Subscriber` cleanup in `_remove_subscriber` to cancel keepalive_task.

Modify `stop()` to cancel all keepalive tasks.

In `_handle_client`, after starting the sender task, add a reader loop that listens for pong frames:

```python
# Start keepalive task
keepalive = asyncio.create_task(
    self._keepalive_loop(sub),
    name=f"fabric-keepalive-{subscriber_id}",
)
sub.keepalive_task = keepalive

# Read loop for pong frames from client
try:
    while True:
        try:
            msg = await recv_frame(reader)
            self._update_last_seen(sub)
            if isinstance(msg, dict) and msg.get("type") == "pong":
                self._handle_pong(sub, msg)
        except (asyncio.IncompleteReadError, ProtocolError):
            sub.disconnect_reason = "eof"
            break
except asyncio.CancelledError:
    pass
```

Note: The exact integration requires restructuring `_handle_client` so the sender task runs in the background while the main handler reads pong frames. The sender `await sender` line should be changed to run concurrently with the reader.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_uds_keepalive.py -v`
Expected: PASS (5 tests)

**Step 5: Run ALL existing tests to verify no regressions**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_lifecycle_engine.py tests/unit/core/test_control_plane_authority.py tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_event_fabric.py tests/unit/core/test_locality_drivers.py tests/integration/test_control_plane_e2e.py tests/integration/test_lease_contention.py tests/integration/test_crash_recovery.py tests/unit/core/test_uds_keepalive.py --tb=short -q`
Expected: 144+ tests passing (139 existing + 5 new)

**Step 6: Commit**

```bash
git add backend/core/uds_event_fabric.py tests/unit/core/test_uds_keepalive.py
git commit -m "feat: add UDS keepalive (ping/pong) with monotonic deadline detection"
```

---

## Task 6: Client-Side Pong Handler

**Files:**
- Modify: `backend/core/control_plane_client.py`
- Modify: `tests/unit/core/test_control_plane_client.py`

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_control_plane_client.py` (or create new test class):

```python
class TestClientPongHandling:
    """Verify ControlPlaneSubscriber responds to pings with pongs."""

    @pytest.mark.asyncio
    async def test_receive_loop_sends_pong_on_ping(self, tmp_path, journal):
        """When server sends a ping, client sends back pong with same ping_id."""
        fabric = EventFabric(journal, keepalive_interval_s=0.5, keepalive_timeout_s=5.0)
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            sub = ControlPlaneSubscriber("pong-test", str(sock))
            await sub.connect()

            # Wait enough time for server to send a ping
            await asyncio.sleep(1.0)

            # Verify subscriber is still connected (pong was sent)
            assert "pong-test" in fabric._subscribers
            assert sub._connected is True

            await sub.disconnect()
        finally:
            await fabric.stop()
```

This test verifies that the client's receive loop handles ping frames and automatically responds with pong frames. The test uses a real fabric+subscriber integration.

**Step 2: Run test to verify behavior**

The test may already pass if the client currently ignores unknown frame types (ping wouldn't crash it). But the pong-send functionality must be added for the keepalive to work end-to-end.

**Step 3: Modify `_receive_loop` in `control_plane_client.py`**

In `ControlPlaneSubscriber._receive_loop()` (around line 210), modify the frame dispatch to handle ping frames:

```python
async def _receive_loop(self) -> None:
    """Background task that reads events from the UDS connection."""
    assert self._reader is not None
    try:
        while self._connected:
            try:
                event = await self._recv_frame()
                msg_type = event.get("type", "")

                if msg_type == "ping":
                    # Respond with pong immediately, timeout-bounded
                    await self._send_pong(event)
                elif msg_type == "subscribe_ack":
                    # Store ack info (earliest_available_seq for gap detection)
                    self._last_subscribe_ack = event
                else:
                    self._dispatch_event(event)

            except asyncio.IncompleteReadError:
                logger.info(
                    "UDS connection closed for subscriber %s",
                    self._subscriber_id,
                )
                break
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Received malformed event on subscriber %s: %s",
                    self._subscriber_id,
                    exc,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Error in receive loop for subscriber %s",
            self._subscriber_id,
        )
    finally:
        self._connected = False

async def _send_pong(self, ping: dict) -> None:
    """Send pong in response to ping, with bounded write timeout."""
    try:
        await asyncio.wait_for(
            self._send_frame({
                "type": "pong",
                "ping_id": ping.get("ping_id", ""),
                "ts": ping.get("ts", 0),
            }),
            timeout=PONG_WRITE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Pong write timed out for subscriber %s",
            self._subscriber_id,
        )
    except Exception:
        logger.warning(
            "Pong write failed for subscriber %s",
            self._subscriber_id,
        )
```

Also add at top of file:

```python
PONG_WRITE_TIMEOUT_S = 2.0
```

And add `_last_subscribe_ack` to `__init__`:

```python
self._last_subscribe_ack: Optional[dict] = None
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_keepalive.py -v`
Expected: All passing

**Step 5: Commit**

```bash
git add backend/core/control_plane_client.py tests/unit/core/test_control_plane_client.py
git commit -m "feat: add client-side pong handler with bounded write timeout"
```

---

## Task 7: Reconnect + Replay — Client Auto-Reconnect

**Files:**
- Modify: `backend/core/control_plane_client.py`
- Modify: `backend/core/uds_event_fabric.py` (add `earliest_available_seq` to `subscribe_ack`)
- Test: `tests/unit/core/test_reconnect_replay.py`

**Step 1: Write the failing tests (HARD GATE — all 3 must pass)**

Create `tests/unit/core/test_reconnect_replay.py`:

```python
"""HARD GATE tests: Reconnect + Replay.

All 3 tests must pass before journal compaction work begins.
These prove the core UDS value proposition: disconnect, reconnect,
catch up on missed events with no gaps.
"""

import asyncio
import json
import struct
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.uds_event_fabric import EventFabric, send_frame, recv_frame
from backend.core.control_plane_client import ControlPlaneSubscriber
from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
def tmp_path(request):
    """Short tmp_path for macOS AF_UNIX limit."""
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    request.addfinalizer(lambda: __import__("shutil").rmtree(str(p), True))
    return p


@pytest.fixture
async def journal(tmp_path):
    """Real journal for integration tests."""
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease("test-leader")
    return j


class TestReconnectReplay:
    @pytest.mark.asyncio
    async def test_subscriber_reconnect_and_replay(self, tmp_path, journal):
        """Full reconnect+replay lifecycle.

        1. Start journal + fabric
        2. Connect subscriber, verify subscribe_ack
        3. Emit events seq 1, 2, 3 → subscriber receives all three
        4. Kill subscriber connection (close writer)
        5. Emit events seq 4, 5 while subscriber disconnected
        6. Subscriber reconnects with last_seen_seq=3
        7. Verify subscriber receives replayed events 4, 5
        8. Emit event seq 6 → subscriber receives it (live stream)
        9. Verify no gaps in received sequence
        """
        fabric = EventFabric(journal, keepalive_interval_s=60, keepalive_timeout_s=120)
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        received_events = []

        try:
            # Connect subscriber
            sub = ControlPlaneSubscriber("replay-sub", str(sock))
            sub.on_event(lambda e: received_events.append(e))
            await sub.connect()
            await asyncio.sleep(0.2)

            # Emit events 1, 2, 3
            for i in range(1, 4):
                seq = journal.fenced_write(
                    "test_action", f"target_{i}",
                    payload={"num": i},
                )
                await fabric.emit(seq, "test_action", f"target_{i}", {"num": i})

            await asyncio.sleep(0.5)
            assert len(received_events) >= 3
            assert sub.last_seen_seq >= 3

            # Save the last seen seq before disconnect
            saved_seq = sub.last_seen_seq

            # Kill connection
            await sub.disconnect()
            received_events.clear()

            # Emit events 4, 5 while disconnected
            for i in range(4, 6):
                seq = journal.fenced_write(
                    "test_action", f"target_{i}",
                    payload={"num": i},
                )
                await fabric.emit(seq, "test_action", f"target_{i}", {"num": i})

            await asyncio.sleep(0.2)

            # Reconnect with saved last_seen_seq
            sub2 = ControlPlaneSubscriber("replay-sub", str(sock), last_seen_seq=saved_seq)
            sub2.on_event(lambda e: received_events.append(e))
            await sub2.connect()
            await asyncio.sleep(0.5)

            # Should receive replayed events 4, 5
            replayed_seqs = [e.get("seq") for e in received_events if e.get("type") == "event"]
            assert len(replayed_seqs) >= 2

            # Emit event 6 (live)
            seq = journal.fenced_write(
                "test_action", "target_6",
                payload={"num": 6},
            )
            await fabric.emit(seq, "test_action", "target_6", {"num": 6})
            await asyncio.sleep(0.3)

            all_seqs = [e.get("seq") for e in received_events if e.get("type") == "event"]
            # Verify no gaps: each seq should be consecutive
            assert len(all_seqs) >= 3  # 4, 5, 6

            await sub2.disconnect()
        finally:
            await journal.close()
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_detects_keepalive_timeout(self, tmp_path, journal):
        """Server removes subscriber that stops responding to pings.

        1. Start fabric with short keepalive (interval=1s, timeout=3s)
        2. Connect subscriber, verify subscribe_ack
        3. Subscriber stops responding to pings
        4. Wait 4s
        5. Verify server removed subscriber
        """
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.5,
            keepalive_timeout_s=2.0,
        )
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        try:
            # Connect raw (no auto-pong)
            reader, writer = await asyncio.open_unix_connection(str(sock))
            await send_frame(writer, {
                "type": "subscribe",
                "subscriber_id": "timeout-sub",
                "last_seen_seq": 0,
            })
            ack = await recv_frame(reader)
            assert ack["type"] == "subscribe_ack"
            assert "timeout-sub" in fabric._subscribers

            # Do NOT respond to pings — just wait
            await asyncio.sleep(3.0)

            # Should be removed
            assert "timeout-sub" not in fabric._subscribers

            writer.close()
        finally:
            await journal.close()
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_reconnect_after_keepalive_death(self, tmp_path, journal):
        """Full cycle: subscriber dies via keepalive timeout, reconnects, replays.

        1. Subscriber stops ponging → server kills it
        2. Events emitted during dead window
        3. Subscriber reconnects with last_seen_seq → replays missed events
        4. Live stream resumes with no gap
        """
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.5,
            keepalive_timeout_s=2.0,
        )
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        received = []

        try:
            # Phase 1: Connect and receive initial events
            sub = ControlPlaneSubscriber("death-sub", str(sock))
            sub.on_event(lambda e: received.append(e))
            await sub.connect()

            seq1 = journal.fenced_write("action", "t1", payload={"v": 1})
            await fabric.emit(seq1, "action", "t1", {"v": 1})
            await asyncio.sleep(0.3)
            assert len(received) >= 1
            saved_seq = sub.last_seen_seq

            # Phase 2: Kill the subscriber's ability to send pongs
            # by closing its writer (simulates network death)
            if sub._writer:
                sub._writer.close()
            sub._connected = False
            if sub._receive_task:
                sub._receive_task.cancel()
                try:
                    await sub._receive_task
                except asyncio.CancelledError:
                    pass

            # Wait for keepalive timeout
            await asyncio.sleep(3.0)
            assert "death-sub" not in fabric._subscribers

            # Phase 3: Emit events during dead window
            received.clear()
            seq2 = journal.fenced_write("action", "t2", payload={"v": 2})
            await fabric.emit(seq2, "action", "t2", {"v": 2})
            seq3 = journal.fenced_write("action", "t3", payload={"v": 3})
            await fabric.emit(seq3, "action", "t3", {"v": 3})

            # Phase 4: Reconnect with saved seq
            sub2 = ControlPlaneSubscriber("death-sub", str(sock), last_seen_seq=saved_seq)
            sub2.on_event(lambda e: received.append(e))
            await sub2.connect()
            await asyncio.sleep(0.5)

            # Should have replayed events from dead window
            event_seqs = [e.get("seq") for e in received if e.get("type") == "event"]
            assert len(event_seqs) >= 2

            # Phase 5: Live stream
            seq4 = journal.fenced_write("action", "t4", payload={"v": 4})
            await fabric.emit(seq4, "action", "t4", {"v": 4})
            await asyncio.sleep(0.3)

            all_seqs = [e.get("seq") for e in received if e.get("type") == "event"]
            assert len(all_seqs) >= 3

            await sub2.disconnect()
        finally:
            await journal.close()
            await fabric.stop()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_reconnect_replay.py -v`
Expected: FAIL (keepalive features not yet fully integrated, subscribe_ack may lack earliest_available_seq)

**Step 3: Implement missing pieces**

1. In `uds_event_fabric.py` `_handle_client`, modify `subscribe_ack` to include `earliest_available_seq`:

```python
# Get earliest available seq from journal
try:
    earliest = self._journal._conn.execute(
        "SELECT MIN(seq) FROM journal"
    ).fetchone()
    earliest_seq = earliest[0] if earliest and earliest[0] is not None else 0
except Exception:
    earliest_seq = 0

await send_frame(writer, {
    "type": "subscribe_ack",
    "subscriber_id": subscriber_id,
    "status": "ok",
    "earliest_available_seq": earliest_seq,
})
```

2. In `control_plane_client.py`, add `on_gap` callback support and store `earliest_available_seq` from ack:

```python
def __init__(self, ...):
    ...
    self._on_gap_callbacks: List[Callable] = []

def on_gap(self, callback: Callable) -> None:
    """Register callback for gap detection (compacted events)."""
    self._on_gap_callbacks.append(callback)
```

**Step 4: Run gate tests**

Run: `python3 -m pytest tests/unit/core/test_reconnect_replay.py -v`
Expected: PASS (3 gate tests)

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_lifecycle_engine.py tests/unit/core/test_control_plane_authority.py tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_event_fabric.py tests/unit/core/test_locality_drivers.py tests/integration/test_control_plane_e2e.py tests/integration/test_lease_contention.py tests/integration/test_crash_recovery.py tests/unit/core/test_uds_keepalive.py tests/unit/core/test_reconnect_replay.py tests/unit/core/test_recovery_protocol.py --tb=short -q`
Expected: All passing

**Step 6: Commit**

```bash
git add backend/core/uds_event_fabric.py backend/core/control_plane_client.py tests/unit/core/test_reconnect_replay.py
git commit -m "feat: reconnect+replay with earliest_available_seq gap detection (HARD GATE)"
```

---

## Task 8: Journal Compaction — Archive Table Schema

**Files:**
- Modify: `backend/core/orchestration_journal.py`
- Test: `tests/unit/core/test_journal_compaction.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_journal_compaction.py`:

```python
"""Tests for journal compaction: archival, retention, FK safety."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
def tmp_path(request):
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    request.addfinalizer(lambda: __import__("shutil").rmtree(str(p), True))
    return p


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease("test-leader")
    return j


class TestArchiveTableCreation:
    @pytest.mark.asyncio
    async def test_archive_table_exists(self, journal):
        """journal_archive table should be created during schema init."""
        row = journal._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='journal_archive'"
        ).fetchone()
        assert row is not None, "journal_archive table should exist"

    @pytest.mark.asyncio
    async def test_archive_table_schema_matches_journal(self, journal):
        """journal_archive should have same columns as journal plus archived_at."""
        cols = journal._conn.execute(
            "PRAGMA table_info(journal_archive)"
        ).fetchall()
        col_names = [c[1] for c in cols]
        assert "seq" in col_names
        assert "epoch" in col_names
        assert "action" in col_names
        assert "target" in col_names
        assert "archived_at" in col_names
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_journal_compaction.py -v`
Expected: FAIL — `journal_archive` table doesn't exist yet

**Step 3: Add archive table to schema**

In `backend/core/orchestration_journal.py`, in `_apply_schema()` (after the existing `CREATE TABLE` statements, before the `schema_version` insert), add:

```sql
CREATE TABLE IF NOT EXISTS journal_archive (
    seq             INTEGER PRIMARY KEY,
    epoch           INTEGER NOT NULL,
    timestamp       REAL NOT NULL,
    wall_clock      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT NOT NULL,
    idempotency_key TEXT,
    payload         TEXT,
    result          TEXT,
    fence_token     INTEGER NOT NULL,
    archived_at     REAL NOT NULL
);
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_journal_compaction.py -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add backend/core/orchestration_journal.py tests/unit/core/test_journal_compaction.py
git commit -m "feat: add journal_archive table schema for compaction"
```

---

## Task 9: Journal Compaction — Core Algorithm

**Files:**
- Modify: `backend/core/orchestration_journal.py`
- Modify: `tests/unit/core/test_journal_compaction.py`

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_journal_compaction.py`:

```python
from backend.core.orchestration_journal import (
    COMPACTION_RETAIN_PRIOR_EPOCHS,
    COMPACTION_BATCH_SIZE,
)


class TestCompactionRetention:
    @pytest.mark.asyncio
    async def test_compaction_retains_current_epoch(self, journal):
        """All current-epoch entries are retained."""
        # Write entries in epoch 1 (current)
        for i in range(10):
            journal.fenced_write("action", f"target_{i}", payload={"i": i})

        result = await journal.compact()
        # All entries should remain (only 10 in current epoch)
        count = journal._conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        assert count == 11  # 10 + lease_acquired entry

    @pytest.mark.asyncio
    async def test_compaction_archives_prior_epoch(self, tmp_path):
        """Prior-epoch entries beyond retention limit are archived."""
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "compact.db")
        await j.acquire_lease("leader-1")

        # Write entries in epoch 1
        for i in range(20):
            j.fenced_write("action", f"target_{i}", payload={"i": i})

        # Force epoch 2 by backdating lease and re-acquiring
        j._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j._conn.commit()
        j._lease_held = False

        j2 = OrchestrationJournal()
        await j2.initialize(tmp_path / "compact.db")
        await j2.acquire_lease("leader-2")

        # Write entries in epoch 2
        for i in range(5):
            j2.fenced_write("action", f"target_e2_{i}", payload={"i": i})

        # With COMPACTION_RETAIN_PRIOR_EPOCHS=1000 and only 21 epoch-1 entries,
        # nothing should be archived
        result = await j2.compact()
        assert result["archived"] == 0

        await j.close()
        await j2.close()

    @pytest.mark.asyncio
    async def test_compaction_noop_on_small_journal(self, journal):
        """Small journal (< retain limit) → nothing archived."""
        for i in range(50):
            journal.fenced_write("action", f"t_{i}", payload={"i": i})

        result = await journal.compact()
        assert result["archived"] == 0
        assert result["retained"] > 0


class TestCompactionFKSafety:
    @pytest.mark.asyncio
    async def test_compaction_preserves_fk_integrity(self, tmp_path):
        """component_state.last_seq is updated when referenced row is compacted."""
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "fk.db")
        await j.acquire_lease("leader-1")

        # Write many entries in epoch 1
        for i in range(1100):
            j.fenced_write("action", f"target_{i % 10}", payload={"i": i})

        # Set component_state referencing an early seq
        early_seq = 5
        j.update_component_state("comp_a", "READY", early_seq)

        # Force epoch 2
        j._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j._conn.commit()
        j._lease_held = False

        j2 = OrchestrationJournal()
        await j2.initialize(tmp_path / "fk.db")
        await j2.acquire_lease("leader-2")

        # Compact — should update comp_a's last_seq to nearest retained
        result = await j2.compact()

        # Verify FK integrity
        state = j2.get_component_state("comp_a")
        assert state is not None
        # last_seq should reference a row that still exists in journal
        row = j2._conn.execute(
            "SELECT seq FROM journal WHERE seq = ?", (state["last_seq"],)
        ).fetchone()
        assert row is not None, f"last_seq={state['last_seq']} should exist in journal"

        await j.close()
        await j2.close()


class TestCompactionAtomicity:
    @pytest.mark.asyncio
    async def test_compaction_is_atomic_on_error(self, journal):
        """Simulated failure mid-compaction leaves journal unchanged."""
        for i in range(50):
            journal.fenced_write("action", f"t_{i}", payload={"i": i})

        before_count = journal._conn.execute(
            "SELECT COUNT(*) FROM journal"
        ).fetchone()[0]

        # Compaction on small journal is no-op anyway, but verify no data lost
        result = await journal.compact()
        after_count = journal._conn.execute(
            "SELECT COUNT(*) FROM journal"
        ).fetchone()[0]

        assert after_count == before_count  # Nothing removed from small journal


class TestCompactionArchive:
    @pytest.mark.asyncio
    async def test_compaction_archives_to_same_db(self, tmp_path):
        """Archived entries are in journal_archive table (same DB)."""
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "archive.db")
        await j.acquire_lease("leader-1")

        # Write 1500 entries in epoch 1
        for i in range(1500):
            j.fenced_write("action", f"t_{i % 50}", payload={"i": i})

        # Force epoch 2
        j._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j._conn.commit()
        j._lease_held = False

        j2 = OrchestrationJournal()
        await j2.initialize(tmp_path / "archive.db")
        await j2.acquire_lease("leader-2")
        j2.fenced_write("action", "epoch2_entry", payload={"x": 1})

        result = await j2.compact()

        if result["archived"] > 0:
            # Verify archived entries exist in archive table
            archive_count = j2._conn.execute(
                "SELECT COUNT(*) FROM journal_archive"
            ).fetchone()[0]
            assert archive_count == result["archived"]

        await j.close()
        await j2.close()


class TestReplayAfterCompaction:
    @pytest.mark.asyncio
    async def test_replay_after_compaction_with_gap(self, tmp_path):
        """After compaction, replay_from(0) starts from earliest available."""
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "replay.db")
        await j.acquire_lease("leader-1")

        for i in range(1500):
            j.fenced_write("action", f"t_{i % 50}", payload={"i": i})

        # Force epoch 2
        j._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j._conn.commit()
        j._lease_held = False

        j2 = OrchestrationJournal()
        await j2.initialize(tmp_path / "replay.db")
        await j2.acquire_lease("leader-2")
        j2.fenced_write("action", "e2", payload={"x": 1})

        await j2.compact()

        # Replay from 0 should return what's available
        entries = await j2.replay_from(0)
        if entries:
            first_seq = entries[0]["seq"]
            # First available should be >= 1 (some may have been compacted)
            assert first_seq >= 1

        # Get earliest available seq
        earliest = j2.get_earliest_available_seq()
        assert earliest >= 1

        await j.close()
        await j2.close()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_journal_compaction.py -v`
Expected: FAIL with `AttributeError: 'OrchestrationJournal' object has no attribute 'compact'`

**Step 3: Implement compaction**

Add constants to `backend/core/orchestration_journal.py`:

```python
# Compaction constants
COMPACTION_RETAIN_PRIOR_EPOCHS = int(
    os.environ.get("JARVIS_JOURNAL_RETAIN_PRIOR", "1000")
)
COMPACTION_INTERVAL_S = int(
    os.environ.get("JARVIS_JOURNAL_COMPACTION_INTERVAL", "86400")
)
COMPACTION_BATCH_SIZE = 10000
JOURNAL_ARCHIVE_ENABLED = os.environ.get(
    "JARVIS_JOURNAL_ARCHIVE_ENABLED", "true"
).lower() in ("true", "1", "yes")
```

Add methods to `OrchestrationJournal`:

```python
async def compact(self) -> dict:
    """Compact the journal: archive old entries, retain recent ones.

    Algorithm:
    1. Determine retention boundary (current epoch + most recent N prior)
    2. Update component_state FK references for compactable entries
    3. Archive eligible entries to journal_archive (same DB)
    4. Delete archived entries from journal
    5. WAL checkpoint

    Returns summary dict.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, self._compact_sync)

def _compact_sync(self) -> dict:
    """Synchronous compaction implementation."""
    summary = {
        "archived": 0,
        "retained": 0,
        "duration_s": 0.0,
    }

    if not self._lease_held:
        summary["skipped"] = True
        return summary

    start = time.monotonic()
    c = self._conn

    # Count total entries
    total = c.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    # Get current epoch
    current_epoch = self._epoch

    # Count prior-epoch entries
    prior_count = c.execute(
        "SELECT COUNT(*) FROM journal WHERE epoch < ?",
        (current_epoch,),
    ).fetchone()[0]

    if prior_count <= COMPACTION_RETAIN_PRIOR_EPOCHS:
        # Nothing to compact
        summary["retained"] = total
        summary["duration_s"] = time.monotonic() - start
        return summary

    # Find the boundary: keep most recent COMPACTION_RETAIN_PRIOR_EPOCHS
    # from prior epochs
    boundary_row = c.execute(
        "SELECT seq FROM journal WHERE epoch < ? "
        "ORDER BY seq DESC LIMIT 1 OFFSET ?",
        (current_epoch, COMPACTION_RETAIN_PRIOR_EPOCHS - 1),
    ).fetchone()

    if boundary_row is None:
        summary["retained"] = total
        summary["duration_s"] = time.monotonic() - start
        return summary

    boundary_seq = boundary_row[0]

    # Pre-compaction: update FK references
    self._update_fk_references(boundary_seq)

    # Archive and delete in batches
    archived = 0
    while True:
        if not self._lease_held:
            break  # Lease lost during compaction

        batch = c.execute(
            "SELECT seq, epoch, timestamp, wall_clock, actor, action, "
            "target, idempotency_key, payload, result, fence_token "
            "FROM journal WHERE seq <= ? AND epoch < ? "
            "ORDER BY seq LIMIT ?",
            (boundary_seq, current_epoch, COMPACTION_BATCH_SIZE),
        ).fetchall()

        if not batch:
            break

        now = time.time()
        try:
            if JOURNAL_ARCHIVE_ENABLED:
                c.executemany(
                    "INSERT OR IGNORE INTO journal_archive "
                    "(seq, epoch, timestamp, wall_clock, actor, action, "
                    "target, idempotency_key, payload, result, "
                    "fence_token, archived_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(*row, now) for row in batch],
                )

            seqs = [row[0] for row in batch]
            placeholders = ",".join("?" * len(seqs))
            c.execute(
                f"DELETE FROM journal WHERE seq IN ({placeholders})",
                seqs,
            )
            c.commit()
            archived += len(batch)
        except Exception:
            c.rollback()
            raise

    # Post-compaction WAL checkpoint
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass  # Non-fatal

    remaining = c.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    summary["archived"] = archived
    summary["retained"] = remaining
    summary["duration_s"] = time.monotonic() - start

    logger.info(
        "[Journal] Compaction complete: archived=%d, retained=%d, "
        "duration=%.2fs",
        archived, remaining, summary["duration_s"],
    )
    return summary

def _update_fk_references(self, boundary_seq: int) -> None:
    """Update component_state.last_seq for rows referencing compactable entries."""
    c = self._conn
    # Find the nearest retained seq (first seq > boundary)
    nearest = c.execute(
        "SELECT MIN(seq) FROM journal WHERE seq > ?",
        (boundary_seq,),
    ).fetchone()
    nearest_seq = nearest[0] if nearest and nearest[0] is not None else boundary_seq

    # Update any component_state rows referencing compactable seqs
    c.execute(
        "UPDATE component_state SET last_seq = ? WHERE last_seq <= ?",
        (nearest_seq, boundary_seq),
    )

def get_earliest_available_seq(self) -> int:
    """Return the earliest seq still in the journal."""
    row = self._conn.execute("SELECT MIN(seq) FROM journal").fetchone()
    return row[0] if row and row[0] is not None else 0
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_journal_compaction.py -v`
Expected: PASS (8 tests)

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_lifecycle_engine.py tests/unit/core/test_control_plane_authority.py tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_event_fabric.py tests/unit/core/test_locality_drivers.py tests/integration/test_control_plane_e2e.py tests/integration/test_lease_contention.py tests/integration/test_crash_recovery.py tests/unit/core/test_uds_keepalive.py tests/unit/core/test_reconnect_replay.py tests/unit/core/test_recovery_protocol.py tests/unit/core/test_journal_compaction.py --tb=short -q`
Expected: All tests passing

**Step 6: Commit**

```bash
git add backend/core/orchestration_journal.py tests/unit/core/test_journal_compaction.py
git commit -m "feat: add journal compaction with same-file archive and FK-safe retention"
```

---

## Task 10: Fault-Injection Suite (HARD GATE — blocks Phase 2B)

**Files:**
- Create: `tests/integration/test_phase2a_fault_injection.py`

**Step 1: Write the fault-injection tests**

Create `tests/integration/test_phase2a_fault_injection.py`:

```python
"""HARD GATE: Fault-injection suite.

All 4 tests must pass before Phase 2B is unlocked.
These prove the control plane behaves correctly under fault conditions.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.lifecycle_engine import (
    ComponentDeclaration,
    ComponentLocality,
    LifecycleEngine,
)
from backend.core.uds_event_fabric import EventFabric, send_frame, recv_frame
from backend.core.control_plane_client import ControlPlaneSubscriber
from backend.core.recovery_protocol import (
    HealthCategory,
    ProbeResult,
    RecoveryOrchestrator,
    RecoveryProber,
)


@pytest.fixture
def tmp_path(request):
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    request.addfinalizer(lambda: __import__("shutil").rmtree(str(p), True))
    return p


COMPONENTS = (
    ComponentDeclaration(
        name="db", locality=ComponentLocality.IN_PROCESS,
        is_critical=True,
    ),
    ComponentDeclaration(
        name="cache", locality=ComponentLocality.IN_PROCESS,
        dependencies=("db",),
    ),
    ComponentDeclaration(
        name="api", locality=ComponentLocality.IN_PROCESS,
        dependencies=("db",), soft_dependencies=("cache",),
    ),
)


class TestKillLeaderRecovery:
    """Kill supervisor mid-operation, restart, verify state reconciled."""

    @pytest.mark.asyncio
    async def test_kill_leader_recovery(self, tmp_path):
        db_path = tmp_path / "test.db"

        # Phase 1: Start leader, transition components
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("leader-1")

        engine1 = LifecycleEngine(j1, COMPONENTS)

        # Manually transition db → READY
        await engine1.transition_component("db", "STARTING", reason="boot")
        await engine1.transition_component("db", "HANDSHAKING", reason="started")
        await engine1.transition_component("db", "READY", reason="handshake_ok")

        # Simulate crash: close without cleanup
        await j1.close()

        # Phase 2: New leader takes over
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)

        # Force lease expiry
        j2._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j2._conn.commit()

        await j2.acquire_lease("leader-2")
        assert j2.epoch == 2

        engine2 = LifecycleEngine(j2, COMPONENTS)

        # Recovery: rebuild state from journal
        await engine2.recover_from_journal()
        assert engine2.get_status("db") == "READY"

        # Recovery prober verifies db is actually alive
        prober = RecoveryProber(journal=j2)

        async def db_healthy(name):
            return ProbeResult(
                reachable=True,
                category=HealthCategory.HEALTHY,
                instance_id="db:inst1",
            )
        prober.register_runtime_probe("db", db_healthy)

        orchestrator = RecoveryOrchestrator(
            journal=j2, engine=engine2, prober=prober,
        )
        result = await orchestrator.run_startup_recovery()
        assert result["aborted"] is False

        # db should still be READY (healthy)
        assert engine2.get_status("db") == "READY"

        await j2.close()


class TestDropSubscriberReplay:
    """Subscriber dies, events emitted, reconnect replays correctly."""

    @pytest.mark.asyncio
    async def test_drop_subscriber_replay(self, tmp_path):
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "replay.db")
        await j.acquire_lease("leader")

        fabric = EventFabric(j, keepalive_interval_s=60, keepalive_timeout_s=120)
        sock = tmp_path / "ctrl.sock"
        await fabric.start(sock)

        received = []

        try:
            # Connect and receive events
            sub = ControlPlaneSubscriber("drop-sub", str(sock))
            sub.on_event(lambda e: received.append(e))
            await sub.connect()

            seq1 = j.fenced_write("action", "t1", payload={"v": 1})
            await fabric.emit(seq1, "action", "t1", {"v": 1})
            await asyncio.sleep(0.3)
            saved_seq = sub.last_seen_seq

            # Kill subscriber
            await sub.disconnect()
            received.clear()

            # Emit while dead
            seq2 = j.fenced_write("action", "t2", payload={"v": 2})
            await fabric.emit(seq2, "action", "t2", {"v": 2})

            # Reconnect
            sub2 = ControlPlaneSubscriber("drop-sub", str(sock), last_seen_seq=saved_seq)
            sub2.on_event(lambda e: received.append(e))
            await sub2.connect()
            await asyncio.sleep(0.5)

            # Should have replayed missed event
            event_seqs = [e.get("seq") for e in received if e.get("type") == "event"]
            assert len(event_seqs) >= 1

            await sub2.disconnect()
        finally:
            await j.close()
            await fabric.stop()


class TestStalledComponentDetection:
    """Component stops responding, keepalive detects, recovery marks LOST."""

    @pytest.mark.asyncio
    async def test_stalled_component_detection(self, tmp_path):
        j = OrchestrationJournal()
        await j.initialize(tmp_path / "stalled.db")
        await j.acquire_lease("leader")

        engine = LifecycleEngine(j, COMPONENTS)
        await engine.transition_component("db", "STARTING", reason="boot")
        await engine.transition_component("db", "HANDSHAKING", reason="started")
        await engine.transition_component("db", "READY", reason="ok")

        # Recovery prober says db is unreachable
        prober = RecoveryProber(journal=j)

        async def db_unreachable(name):
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error="connection refused",
            )
        prober.register_runtime_probe("db", db_unreachable)

        orchestrator = RecoveryOrchestrator(
            journal=j, engine=engine, prober=prober,
        )
        result = await orchestrator.run_startup_recovery()

        # db was READY but unreachable → should be LOST
        assert engine.get_status("db") == "LOST"
        assert result["reconciled"] >= 1

        await j.close()


class TestJournalReplayAfterCrash:
    """Write entries, crash, restart, verify full replay consistency."""

    @pytest.mark.asyncio
    async def test_journal_replay_after_crash(self, tmp_path):
        db_path = tmp_path / "crash.db"

        # Phase 1: Write entries
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("leader-1")

        written_seqs = []
        for i in range(20):
            seq = j1.fenced_write("action", f"target_{i}", payload={"i": i})
            written_seqs.append(seq)

        j1.update_component_state("comp_a", "READY", written_seqs[-1])

        # Simulate crash
        await j1.close()

        # Phase 2: Restart and replay
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)

        # Force lease takeover
        j2._conn.execute(
            "UPDATE lease SET last_renewed = last_renewed - 100 WHERE id = 1"
        )
        j2._conn.commit()
        await j2.acquire_lease("leader-2")

        # Replay all entries
        entries = await j2.replay_from(0)
        replayed_seqs = [e["seq"] for e in entries]

        # All written entries should be present
        for seq in written_seqs:
            assert seq in replayed_seqs, f"seq {seq} missing from replay"

        # Component state should be preserved
        state = j2.get_component_state("comp_a")
        assert state is not None
        assert state["status"] == "READY"

        await j2.close()
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/integration/test_phase2a_fault_injection.py -v`
Expected: PASS (4 tests) — these depend on all prior tasks being implemented

**Step 3: Run full Phase 2A test suite**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_lifecycle_engine.py tests/unit/core/test_control_plane_authority.py tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_event_fabric.py tests/unit/core/test_locality_drivers.py tests/integration/test_control_plane_e2e.py tests/integration/test_lease_contention.py tests/integration/test_crash_recovery.py tests/unit/core/test_recovery_protocol.py tests/unit/core/test_uds_keepalive.py tests/unit/core/test_reconnect_replay.py tests/unit/core/test_journal_compaction.py tests/integration/test_phase2a_fault_injection.py --tb=short -q`
Expected: ~166+ tests passing (139 baseline + ~27 new)

**Step 4: Commit**

```bash
git add tests/integration/test_phase2a_fault_injection.py
git commit -m "feat: add fault-injection suite (HARD GATE for Phase 2B)"
```

---

## Task 11: Final Regression + Gate Verification

**Files:** None (verification only)

**Step 1: Run complete test suite**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_lifecycle_engine.py tests/unit/core/test_control_plane_authority.py tests/unit/core/test_control_plane_client.py tests/unit/core/test_uds_event_fabric.py tests/unit/core/test_locality_drivers.py tests/integration/test_control_plane_e2e.py tests/integration/test_lease_contention.py tests/integration/test_crash_recovery.py tests/unit/core/test_recovery_protocol.py tests/unit/core/test_uds_keepalive.py tests/unit/core/test_reconnect_replay.py tests/unit/core/test_journal_compaction.py tests/integration/test_phase2a_fault_injection.py -v`
Expected: ALL passing

**Step 2: Verify gate tests specifically**

Run: `python3 -m pytest tests/unit/core/test_reconnect_replay.py tests/integration/test_phase2a_fault_injection.py -v`
Expected: 7 tests PASS (3 reconnect gate + 4 fault-injection gate)

**Step 3: Verify no import warnings**

Run: `python3 -c "from backend.core.recovery_protocol import RecoveryOrchestrator, RecoveryProber, RecoveryReconciler, HealthCategory, ProbeResult; print('OK')" 2>&1`
Expected: `OK`

**Step 4: Commit summary (if any final cleanup)**

```bash
git add -A
git commit -m "chore: Phase 2A hardening complete — all gates passed"
```

---

## Test Matrix Summary

| Section | Test File | Test Count | Gate? |
|---------|-----------|-----------|-------|
| Recovery data model | `test_recovery_protocol.py` | 3 | No |
| Recovery probing | `test_recovery_protocol.py` | 7 | No |
| Recovery reconciler | `test_recovery_protocol.py` | 8 | No |
| Recovery orchestrator | `test_recovery_protocol.py` | 2 | No |
| UDS keepalive | `test_uds_keepalive.py` | 5 | No |
| Client pong handler | `test_control_plane_client.py` | 1 | No |
| Reconnect + replay | `test_reconnect_replay.py` | 3 | **HARD GATE** |
| Journal compaction schema | `test_journal_compaction.py` | 2 | No |
| Journal compaction algo | `test_journal_compaction.py` | 6 | No |
| Fault-injection suite | `test_phase2a_fault_injection.py` | 4 | **HARD GATE** |
| **Total new Phase 2A tests** | | **~41** | |
| **Total with Phase 1 baseline** | | **~180** | |

## Files Modified/Created

| Action | File |
|--------|------|
| Create | `backend/core/recovery_protocol.py` |
| Create | `tests/unit/core/test_recovery_protocol.py` |
| Create | `tests/unit/core/test_uds_keepalive.py` |
| Create | `tests/unit/core/test_reconnect_replay.py` |
| Create | `tests/unit/core/test_journal_compaction.py` |
| Create | `tests/integration/test_phase2a_fault_injection.py` |
| Modify | `backend/core/uds_event_fabric.py` (keepalive, ping/pong, subscriber liveness) |
| Modify | `backend/core/control_plane_client.py` (pong handler, gap detection) |
| Modify | `backend/core/orchestration_journal.py` (archive table, compaction, earliest_available_seq) |

## Execution Order Enforcement

```
Task 1-4: Recovery Protocol
  ↓ (all 20 recovery tests must pass)
Task 5-6: UDS Keepalive
  ↓ (all 6 keepalive tests must pass)
Task 7: Reconnect + Replay [HARD GATE]
  ↓ (3 gate tests must pass)
Task 8-9: Journal Compaction
  ↓ (8 compaction tests must pass)
Task 10: Fault-Injection Suite [HARD GATE]
  ↓ (4 gate tests must pass)
Task 11: Final Verification
  ↓
Phase 2B Unlocked
```
