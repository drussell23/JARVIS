# Disease 8 Cure: Reactive State Propagation — Wave 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add policy-hook enforcement (cross-key invariants), post-replay invariant auditing, schema violation tracking, rejection observability, and lifecycle-critical path annotations to the ReactiveStateStore built in Wave 0.

**Architecture:** A `PolicyHook` callable protocol validates cross-key invariants at write time (step 5 in the pipeline) using a read-only state snapshot. An `AuditLog` tracks schema violations and post-replay invariant audit findings. Rejection counters provide per-(key, reason) observability. Path annotations prevent `block_bounded` overflow policy on lifecycle-critical writes.

**Tech Stack:** Python 3.9+, stdlib only (dataclasses, typing, collections, logging). Builds on Wave 0 modules.

**Design doc:** `docs/plans/2026-03-05-reactive-state-propagation-design.md` — Sections 4 (Authority Integration), 5 (Journal replay), 7 (Watchers), and Appendix A.

**Wave 0 code (already built):**
- `backend/core/reactive_state/types.py` — StateEntry, WriteResult, WriteStatus (includes POLICY_REJECTED), WriteRejection, JournalEntry
- `backend/core/reactive_state/schemas.py` — KeySchema (validate, coerce), SchemaRegistry
- `backend/core/reactive_state/ownership.py` — OwnershipRule, OwnershipRegistry
- `backend/core/reactive_state/journal.py` — AppendOnlyJournal (SQLite WAL)
- `backend/core/reactive_state/watchers.py` — WatcherManager (subscribe, unsubscribe, notify)
- `backend/core/reactive_state/manifest.py` — OWNERSHIP_RULES, KEY_SCHEMAS, CONSISTENCY_GROUPS, builders
- `backend/core/reactive_state/store.py` — ReactiveStateStore (write pipeline steps 1-4 + 6-7, no step 5 yet)

**Existing state_authority.py** (`backend/core/state_authority.py`):
- Declares authoritative sources for state concepts and validates consistency after-the-fact
- Has `ConsistencyResult`, `StateDeclaration`, validators like `_validate_gcp_vm_readiness()`
- Wave 1 creates a NEW policy interface in `reactive_state/` for write-time enforcement — the existing `state_authority.py` will delegate to it in a future wave

---

## Task 1: Policy hook protocol and invariant rules

**Files:**
- Create: `backend/core/reactive_state/policy.py`
- Test: `tests/unit/core/reactive_state/test_policy.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_policy.py
"""Tests for policy hook protocol and cross-key invariant rules."""
from __future__ import annotations

import pytest

from backend.core.reactive_state.policy import (
    PolicyResult,
    PolicyRule,
    PolicyEngine,
    gcp_offload_requires_ip,
    gcp_offload_requires_port,
    hollow_requires_offload,
)
from backend.core.reactive_state.types import StateEntry


def _entry(key: str, value: object, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key, value=value, version=version, epoch=1,
        writer="test", origin="explicit",
        updated_at_mono=0.0, updated_at_unix_ms=0,
    )


class TestPolicyResult:
    def test_ok(self) -> None:
        r = PolicyResult.ok()
        assert r.allowed is True
        assert r.reason is None

    def test_rejected(self) -> None:
        r = PolicyResult.rejected("gcp.node_ip must be set")
        assert r.allowed is False
        assert r.reason == "gcp.node_ip must be set"


class TestGcpOffloadRequiresIp:
    def test_allows_offload_true_with_ip(self) -> None:
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "10.0.0.1")}
        result = gcp_offload_requires_ip("gcp.offload_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_offload_true_without_ip(self) -> None:
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "")}
        result = gcp_offload_requires_ip("gcp.offload_active", True, snapshot)
        assert result.allowed is False
        assert "node_ip" in result.reason

    def test_allows_offload_false_without_ip(self) -> None:
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "")}
        result = gcp_offload_requires_ip("gcp.offload_active", False, snapshot)
        assert result.allowed is True

    def test_ignores_other_keys(self) -> None:
        result = gcp_offload_requires_ip("memory.tier", "abundant", {})
        assert result.allowed is True

    def test_allows_offload_true_with_no_ip_entry(self) -> None:
        # If gcp.node_ip hasn't been written yet, reject
        result = gcp_offload_requires_ip("gcp.offload_active", True, {})
        assert result.allowed is False


class TestGcpOffloadRequiresPort:
    def test_allows_offload_true_with_port(self) -> None:
        snapshot = {
            "gcp.node_ip": _entry("gcp.node_ip", "10.0.0.1"),
            "gcp.node_port": _entry("gcp.node_port", 8000),
        }
        result = gcp_offload_requires_port("gcp.offload_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_offload_true_without_port(self) -> None:
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "10.0.0.1")}
        result = gcp_offload_requires_port("gcp.offload_active", True, snapshot)
        assert result.allowed is False


class TestHollowRequiresOffload:
    def test_allows_hollow_when_offload_active(self) -> None:
        snapshot = {"gcp.offload_active": _entry("gcp.offload_active", True)}
        result = hollow_requires_offload("hollow.client_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_hollow_when_offload_inactive(self) -> None:
        snapshot = {"gcp.offload_active": _entry("gcp.offload_active", False)}
        result = hollow_requires_offload("hollow.client_active", True, snapshot)
        assert result.allowed is False

    def test_allows_hollow_false_always(self) -> None:
        snapshot = {"gcp.offload_active": _entry("gcp.offload_active", False)}
        result = hollow_requires_offload("hollow.client_active", False, snapshot)
        assert result.allowed is True


class TestPolicyEngine:
    def test_empty_engine_allows_all(self) -> None:
        engine = PolicyEngine()
        result = engine.evaluate("any.key", "any_value", {})
        assert result.allowed is True

    def test_engine_runs_all_rules(self) -> None:
        engine = PolicyEngine()
        engine.add_rule(gcp_offload_requires_ip)
        engine.add_rule(gcp_offload_requires_port)
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "")}
        result = engine.evaluate("gcp.offload_active", True, snapshot)
        assert result.allowed is False
        # First failing rule stops evaluation
        assert "node_ip" in result.reason

    def test_engine_passes_when_all_rules_pass(self) -> None:
        engine = PolicyEngine()
        engine.add_rule(gcp_offload_requires_ip)
        snapshot = {"gcp.node_ip": _entry("gcp.node_ip", "10.0.0.1")}
        result = engine.evaluate("gcp.offload_active", True, snapshot)
        assert result.allowed is True

    def test_build_default_engine(self) -> None:
        from backend.core.reactive_state.policy import build_default_policy_engine
        engine = build_default_policy_engine()
        assert len(engine.rules) >= 3
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_policy.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/reactive_state/policy.py
"""Cross-key policy enforcement for the ReactiveStateStore.

Policy rules are pure functions that validate proposed writes against
the current state snapshot. They are:
- Side-effect-free and deterministic (no I/O, no network, no clock)
- Receive a read-only snapshot dict, never call store.read() directly
- Called as step 5 in the write pipeline (after CAS, before journal append)
- NOT re-run during journal replay (replay trusts the journal)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from backend.core.reactive_state.types import StateEntry

# Type alias for a policy rule function
PolicyRuleFn = Callable[[str, Any, Dict[str, StateEntry]], "PolicyResult"]


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of a policy rule evaluation."""
    allowed: bool
    reason: Optional[str] = None

    @classmethod
    def ok(cls) -> PolicyResult:
        return cls(allowed=True)

    @classmethod
    def rejected(cls, reason: str) -> PolicyResult:
        return cls(allowed=False, reason=reason)


class PolicyRule:
    """Named wrapper around a policy rule function."""

    def __init__(self, name: str, fn: PolicyRuleFn) -> None:
        self.name = name
        self.fn = fn

    def evaluate(self, key: str, value: Any, snapshot: Dict[str, StateEntry]) -> PolicyResult:
        return self.fn(key, value, snapshot)


# ---------------------------------------------------------------------------
# Concrete invariant rules
# ---------------------------------------------------------------------------

def gcp_offload_requires_ip(
    key: str, value: Any, snapshot: Dict[str, StateEntry],
) -> PolicyResult:
    """gcp.offload_active=True requires gcp.node_ip to be non-empty."""
    if key != "gcp.offload_active" or value is not True:
        return PolicyResult.ok()
    ip_entry = snapshot.get("gcp.node_ip")
    if ip_entry is None or not ip_entry.value:
        return PolicyResult.rejected(
            "gcp.offload_active=True requires gcp.node_ip to be set"
        )
    return PolicyResult.ok()


def gcp_offload_requires_port(
    key: str, value: Any, snapshot: Dict[str, StateEntry],
) -> PolicyResult:
    """gcp.offload_active=True requires gcp.node_port to be set."""
    if key != "gcp.offload_active" or value is not True:
        return PolicyResult.ok()
    port_entry = snapshot.get("gcp.node_port")
    if port_entry is None:
        return PolicyResult.rejected(
            "gcp.offload_active=True requires gcp.node_port to be set"
        )
    return PolicyResult.ok()


def hollow_requires_offload(
    key: str, value: Any, snapshot: Dict[str, StateEntry],
) -> PolicyResult:
    """hollow.client_active=True requires gcp.offload_active=True."""
    if key != "hollow.client_active" or value is not True:
        return PolicyResult.ok()
    offload_entry = snapshot.get("gcp.offload_active")
    if offload_entry is None or offload_entry.value is not True:
        return PolicyResult.rejected(
            "hollow.client_active=True requires gcp.offload_active=True"
        )
    return PolicyResult.ok()


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

class PolicyEngine:
    """Evaluates a set of policy rules against a proposed write."""

    def __init__(self) -> None:
        self._rules: List[PolicyRuleFn] = []

    @property
    def rules(self) -> List[PolicyRuleFn]:
        return list(self._rules)

    def add_rule(self, rule: PolicyRuleFn) -> None:
        self._rules.append(rule)

    def evaluate(
        self, key: str, value: Any, snapshot: Dict[str, StateEntry],
    ) -> PolicyResult:
        """Run all rules. First rejection wins (short-circuit)."""
        for rule in self._rules:
            result = rule(key, value, snapshot)
            if not result.allowed:
                return result
        return PolicyResult.ok()


def build_default_policy_engine() -> PolicyEngine:
    """Create a PolicyEngine with all built-in invariant rules."""
    engine = PolicyEngine()
    engine.add_rule(gcp_offload_requires_ip)
    engine.add_rule(gcp_offload_requires_port)
    engine.add_rule(hollow_requires_offload)
    return engine
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_policy.py -v`
Expected: All 17 tests PASS

**Step 5: Commit**

```bash
git add backend/core/reactive_state/policy.py tests/unit/core/reactive_state/test_policy.py
git commit -m "feat(disease8): add PolicyEngine with cross-key invariant rules (Wave 1, Task 1)"
```

---

## Task 2: Wire policy hook into store write pipeline

**Files:**
- Modify: `backend/core/reactive_state/store.py`
- Test: `tests/unit/core/reactive_state/test_store_policy.py`

The store currently has steps 1-4 + 6-7. We add step 5 (policy validation) between CAS check and journal append. The store constructor accepts an optional `PolicyEngine`.

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_store_policy.py
"""Tests for policy hook integration in ReactiveStateStore."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import (
    PolicyEngine,
    PolicyResult,
    build_default_policy_engine,
)
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import WriteStatus


@pytest.fixture
def store_with_policy(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "policy.db",
        epoch=1,
        session_id="policy-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


class TestPolicyEnforcement:
    def test_offload_rejected_without_ip(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        # gcp.node_ip is "" (default), so offload_active=True should be rejected
        entry = store.read("gcp.offload_active")
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED
        assert result.rejection is not None
        assert result.rejection.reason == WriteStatus.POLICY_REJECTED

    def test_offload_allowed_with_ip(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        # Set IP first
        ip_entry = store.read("gcp.node_ip")
        store.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip_entry.version, writer="gcp_controller")
        # Now offload should succeed
        entry = store.read("gcp.offload_active")
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

    def test_hollow_rejected_without_offload(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        entry = store.read("hollow.client_active")
        result = store.write(
            key="hollow.client_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED

    def test_hollow_allowed_with_offload(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        # Set IP, then offload, then hollow
        ip_entry = store.read("gcp.node_ip")
        store.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip_entry.version, writer="gcp_controller")
        offload_entry = store.read("gcp.offload_active")
        store.write(key="gcp.offload_active", value=True, expected_version=offload_entry.version, writer="gcp_controller")
        hollow_entry = store.read("hollow.client_active")
        result = store.write(
            key="hollow.client_active",
            value=True,
            expected_version=hollow_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

    def test_no_policy_engine_skips_validation(self, tmp_path: Path) -> None:
        """Store without policy_engine should skip step 5."""
        s = ReactiveStateStore(
            journal_path=tmp_path / "no_policy.db",
            epoch=1,
            session_id="no-policy",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            # No policy_engine
        )
        s.open()
        s.initialize_defaults()
        # Should succeed even without IP set
        entry = s.read("gcp.offload_active")
        result = s.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        s.close()

    def test_policy_rejected_does_not_increment_revision(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        rev_before = store.global_revision()
        entry = store.read("gcp.offload_active")
        store.write(key="gcp.offload_active", value=True, expected_version=entry.version, writer="gcp_controller")
        assert store.global_revision() == rev_before  # no change

    def test_watcher_not_notified_on_policy_rejection(self, store_with_policy: ReactiveStateStore) -> None:
        store = store_with_policy
        changes = []
        store.watch("gcp.*", lambda old, new: changes.append(new.key))
        entry = store.read("gcp.offload_active")
        store.write(key="gcp.offload_active", value=True, expected_version=entry.version, writer="gcp_controller")
        # Watcher should NOT have been called (policy rejected before journal commit)
        # Filter out any notifications from initialize_defaults
        offload_changes = [c for c in changes if c == "gcp.offload_active"]
        assert len(offload_changes) == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_policy.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'policy_engine'`

**Step 3: Modify store.py to add policy_engine parameter and step 5**

Add to `ReactiveStateStore.__init__`:
```python
from backend.core.reactive_state.policy import PolicyEngine
# ...
def __init__(self, ..., policy_engine: Optional[PolicyEngine] = None) -> None:
    # ... existing init ...
    self._policy_engine = policy_engine
```

Add step 5 to `write()` method — insert between step 4 (CAS check) and step 6 (journal append), still inside the `with self._lock:` block:

```python
            # Step 5: Policy validation (if engine configured)
            if self._policy_engine is not None:
                snapshot = dict(self._entries)  # read-only copy
                policy_result = self._policy_engine.evaluate(key, value, snapshot)
                if not policy_result.allowed:
                    return self._reject(
                        key, writer, WriteStatus.POLICY_REJECTED, expected_version
                    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_policy.py -v`
Expected: All 7 tests PASS

**Step 5: Run full suite to check for regressions**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS (existing tests don't pass `policy_engine`, so they keep working with `None` default)

**Step 6: Commit**

```bash
git add backend/core/reactive_state/store.py tests/unit/core/reactive_state/test_store_policy.py
git commit -m "feat(disease8): wire PolicyEngine into store write pipeline as step 5 (Wave 1, Task 2)"
```

---

## Task 3: Audit module — schema violation tracking and post-replay audit

**Files:**
- Create: `backend/core/reactive_state/audit.py`
- Test: `tests/unit/core/reactive_state/test_audit.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_audit.py
"""Tests for audit log — schema violations and post-replay invariant audit."""
from __future__ import annotations

from backend.core.reactive_state.audit import (
    AuditFinding,
    AuditSeverity,
    SchemaViolation,
    AuditLog,
    post_replay_invariant_audit,
)
from backend.core.reactive_state.types import StateEntry


def _entry(key: str, value: object, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key, value=value, version=version, epoch=1,
        writer="test", origin="explicit",
        updated_at_mono=0.0, updated_at_unix_ms=0,
    )


class TestSchemaViolation:
    def test_frozen(self) -> None:
        v = SchemaViolation(
            key="memory.tier",
            original_value="nonexistent",
            coerced_value="unknown",
            schema_version=1,
            policy="default_with_violation",
            global_revision=42,
        )
        assert v.key == "memory.tier"
        import pytest
        with pytest.raises(AttributeError):
            v.key = "other"  # type: ignore[misc]


class TestAuditFinding:
    def test_frozen(self) -> None:
        f = AuditFinding(
            severity=AuditSeverity.WARNING,
            category="cross_key_invariant",
            key="gcp.offload_active",
            message="offload is True but node_ip is empty",
            snapshot_revision=10,
        )
        assert f.severity == AuditSeverity.WARNING
        assert f.category == "cross_key_invariant"


class TestAuditLog:
    def test_record_violation(self) -> None:
        log = AuditLog()
        log.record_violation(SchemaViolation(
            key="memory.tier",
            original_value="nonexistent",
            coerced_value="unknown",
            schema_version=1,
            policy="default_with_violation",
            global_revision=42,
        ))
        assert len(log.violations) == 1
        assert log.violations[0].key == "memory.tier"

    def test_record_finding(self) -> None:
        log = AuditLog()
        log.record_finding(AuditFinding(
            severity=AuditSeverity.ERROR,
            category="replay_invariant",
            key="gcp.offload_active",
            message="invariant violated",
            snapshot_revision=10,
        ))
        assert len(log.findings) == 1

    def test_has_critical_findings(self) -> None:
        log = AuditLog()
        assert log.has_critical_findings() is False
        log.record_finding(AuditFinding(
            severity=AuditSeverity.WARNING,
            category="test",
            key="k",
            message="warn",
            snapshot_revision=0,
        ))
        assert log.has_critical_findings() is False
        log.record_finding(AuditFinding(
            severity=AuditSeverity.ERROR,
            category="test",
            key="k",
            message="err",
            snapshot_revision=0,
        ))
        assert log.has_critical_findings() is True

    def test_bounded_history(self) -> None:
        log = AuditLog(max_violations=5)
        for i in range(10):
            log.record_violation(SchemaViolation(
                key=f"k.{i}",
                original_value="x",
                coerced_value="y",
                schema_version=1,
                policy="default_with_violation",
                global_revision=i,
            ))
        assert len(log.violations) == 5


class TestPostReplayInvariantAudit:
    def test_clean_state_passes(self) -> None:
        snapshot = {
            "gcp.offload_active": _entry("gcp.offload_active", False),
            "gcp.node_ip": _entry("gcp.node_ip", ""),
            "hollow.client_active": _entry("hollow.client_active", False),
        }
        findings = post_replay_invariant_audit(snapshot, global_revision=5)
        errors = [f for f in findings if f.severity == AuditSeverity.ERROR]
        assert len(errors) == 0

    def test_detects_offload_without_ip(self) -> None:
        snapshot = {
            "gcp.offload_active": _entry("gcp.offload_active", True),
            "gcp.node_ip": _entry("gcp.node_ip", ""),
            "hollow.client_active": _entry("hollow.client_active", False),
        }
        findings = post_replay_invariant_audit(snapshot, global_revision=5)
        errors = [f for f in findings if f.severity == AuditSeverity.ERROR]
        assert len(errors) >= 1
        assert any("node_ip" in f.message for f in errors)

    def test_detects_hollow_without_offload(self) -> None:
        snapshot = {
            "gcp.offload_active": _entry("gcp.offload_active", False),
            "gcp.node_ip": _entry("gcp.node_ip", ""),
            "hollow.client_active": _entry("hollow.client_active", True),
        }
        findings = post_replay_invariant_audit(snapshot, global_revision=5)
        errors = [f for f in findings if f.severity == AuditSeverity.ERROR]
        assert len(errors) >= 1
        assert any("hollow" in f.message.lower() for f in errors)

    def test_returns_findings_not_raises(self) -> None:
        """Post-replay audit flags violations, never raises."""
        snapshot = {
            "gcp.offload_active": _entry("gcp.offload_active", True),
            "gcp.node_ip": _entry("gcp.node_ip", ""),
            "hollow.client_active": _entry("hollow.client_active", True),
        }
        # Should not raise, just return findings
        findings = post_replay_invariant_audit(snapshot, global_revision=5)
        assert len(findings) >= 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/reactive_state/audit.py
"""Post-replay invariant audit and schema violation tracking.

The audit log records:
- Schema violations (e.g., unknown enum coerced via default_with_violation policy)
- Post-replay invariant findings (cross-key invariant violations detected after
  journal replay — flagged as warnings/errors, never rejected)

Post-replay audit runs AFTER replay and does NOT reject replayed state.
It flags violations for operator awareness and monitoring.
"""
from __future__ import annotations

import enum
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.core.reactive_state.types import StateEntry

logger = logging.getLogger(__name__)


class AuditSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SchemaViolation:
    """Record of a value that was coerced due to schema policy."""
    key: str
    original_value: Any
    coerced_value: Any
    schema_version: int
    policy: str  # "default_with_violation" | "map_to:<value>"
    global_revision: int


@dataclass(frozen=True)
class AuditFinding:
    """A single finding from invariant auditing."""
    severity: AuditSeverity
    category: str  # "cross_key_invariant" | "replay_invariant" | "schema_violation"
    key: str
    message: str
    snapshot_revision: int


class AuditLog:
    """Bounded log of schema violations and audit findings."""

    def __init__(self, max_violations: int = 1000, max_findings: int = 1000) -> None:
        self._violations: deque[SchemaViolation] = deque(maxlen=max_violations)
        self._findings: deque[AuditFinding] = deque(maxlen=max_findings)

    def record_violation(self, violation: SchemaViolation) -> None:
        self._violations.append(violation)
        logger.info(
            "Schema violation: key=%s original=%r coerced=%r policy=%s rev=%d",
            violation.key, violation.original_value, violation.coerced_value,
            violation.policy, violation.global_revision,
        )

    def record_finding(self, finding: AuditFinding) -> None:
        self._findings.append(finding)
        log_fn = logger.error if finding.severity == AuditSeverity.ERROR else logger.warning
        log_fn(
            "Audit finding [%s]: %s key=%s rev=%d",
            finding.severity.value, finding.message, finding.key, finding.snapshot_revision,
        )

    @property
    def violations(self) -> List[SchemaViolation]:
        return list(self._violations)

    @property
    def findings(self) -> List[AuditFinding]:
        return list(self._findings)

    def has_critical_findings(self) -> bool:
        return any(f.severity == AuditSeverity.ERROR for f in self._findings)


def post_replay_invariant_audit(
    snapshot: Dict[str, StateEntry],
    global_revision: int,
) -> List[AuditFinding]:
    """Run cross-key invariant checks against a replayed state snapshot.

    Returns a list of findings — never raises. The caller decides
    whether to block READY based on severity.
    """
    findings: List[AuditFinding] = []

    # Invariant: gcp.offload_active=True requires gcp.node_ip to be non-empty
    offload = snapshot.get("gcp.offload_active")
    ip = snapshot.get("gcp.node_ip")
    if offload is not None and offload.value is True:
        if ip is None or not ip.value:
            findings.append(AuditFinding(
                severity=AuditSeverity.ERROR,
                category="cross_key_invariant",
                key="gcp.offload_active",
                message="gcp.offload_active is True but gcp.node_ip is empty after replay",
                snapshot_revision=global_revision,
            ))

    # Invariant: hollow.client_active=True requires gcp.offload_active=True
    hollow = snapshot.get("hollow.client_active")
    if hollow is not None and hollow.value is True:
        if offload is None or offload.value is not True:
            findings.append(AuditFinding(
                severity=AuditSeverity.ERROR,
                category="cross_key_invariant",
                key="hollow.client_active",
                message="hollow.client_active is True but gcp.offload_active is not True after replay",
                snapshot_revision=global_revision,
            ))

    return findings
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_audit.py -v`
Expected: All 11 tests PASS

**Step 5: Commit**

```bash
git add backend/core/reactive_state/audit.py tests/unit/core/reactive_state/test_audit.py
git commit -m "feat(disease8): add AuditLog with schema violations and post-replay invariant audit (Wave 1, Task 3)"
```

---

## Task 4: Wire post-replay audit into store.open()

**Files:**
- Modify: `backend/core/reactive_state/store.py`
- Test: `tests/unit/core/reactive_state/test_store_audit.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_store_audit.py
"""Tests for post-replay audit integration in ReactiveStateStore."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import WriteStatus


def _make_store(tmp_path: Path, *, epoch: int = 1, audit_log: AuditLog = None) -> ReactiveStateStore:
    return ReactiveStateStore(
        journal_path=tmp_path / "audit.db",
        epoch=epoch,
        session_id=f"audit-test-{epoch}",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
        audit_log=audit_log,
    )


class TestPostReplayAudit:
    def test_clean_replay_no_findings(self, tmp_path: Path) -> None:
        """Normal state after replay produces no audit findings."""
        audit = AuditLog()
        s = _make_store(tmp_path, audit_log=audit)
        s.open()
        s.initialize_defaults()
        s.close()

        # Reopen — replay runs audit
        audit2 = AuditLog()
        s2 = _make_store(tmp_path, epoch=2, audit_log=audit2)
        s2.open()
        assert audit2.has_critical_findings() is False
        s2.close()

    def test_inconsistent_replay_produces_findings(self, tmp_path: Path) -> None:
        """If journal contains inconsistent state, audit flags it."""
        # Write a clean store with offload=False
        s = _make_store(tmp_path)
        s.open()
        s.initialize_defaults()
        # Set IP and then offload
        ip = s.read("gcp.node_ip")
        s.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip.version, writer="gcp_controller")
        offload = s.read("gcp.offload_active")
        s.write(key="gcp.offload_active", value=True, expected_version=offload.version, writer="gcp_controller")
        s.close()

        # Now tamper with the journal to create inconsistency:
        # Set gcp.node_ip back to "" without clearing offload
        db = tmp_path / "audit.db"
        conn = sqlite3.connect(str(db))
        max_rev = conn.execute("SELECT MAX(global_revision) FROM state_journal").fetchone()[0]
        new_rev = max_rev + 1
        conn.execute(
            "INSERT INTO state_journal (global_revision, key, value, previous_value, "
            "version, epoch, writer, writer_session_id, origin, consistency_group, "
            "timestamp_unix_ms, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_rev, "gcp.node_ip", json.dumps(""), json.dumps("10.0.0.1"),
             3, 1, "gcp_controller", "tamper", "explicit", None, 0, "tampered"),
        )
        conn.commit()
        conn.close()

        # Reopen — replay should detect inconsistency
        audit2 = AuditLog()
        s2 = _make_store(tmp_path, epoch=2, audit_log=audit2)
        s2.open()
        assert audit2.has_critical_findings() is True
        findings = audit2.findings
        errors = [f for f in findings if f.severity == AuditSeverity.ERROR]
        assert len(errors) >= 1
        assert any("node_ip" in f.message for f in errors)
        s2.close()

    def test_no_audit_log_skips_audit(self, tmp_path: Path) -> None:
        """Store without audit_log should not crash on open."""
        s = ReactiveStateStore(
            journal_path=tmp_path / "no_audit.db",
            epoch=1,
            session_id="no-audit",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
        )
        s.open()
        s.initialize_defaults()
        s.close()

    def test_audit_log_accessible(self, tmp_path: Path) -> None:
        audit = AuditLog()
        s = _make_store(tmp_path, audit_log=audit)
        s.open()
        assert s.audit_log is audit
        s.close()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_audit.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'audit_log'`

**Step 3: Modify store.py**

Add to `__init__`:
```python
from backend.core.reactive_state.audit import AuditLog, post_replay_invariant_audit
# ...
def __init__(self, ..., audit_log: Optional[AuditLog] = None) -> None:
    # ... existing init ...
    self._audit_log = audit_log
```

Add property:
```python
@property
def audit_log(self) -> Optional[AuditLog]:
    return self._audit_log
```

Modify `_replay()` to run audit after replay:
```python
def _replay(self) -> None:
    entries = self._journal.read_since(1)
    for je in entries:
        self._entries[je.key] = StateEntry(...)
    # Post-replay invariant audit
    if self._audit_log is not None and entries:
        findings = post_replay_invariant_audit(
            dict(self._entries), self._journal.latest_revision()
        )
        for finding in findings:
            self._audit_log.record_finding(finding)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_audit.py -v`
Expected: All 4 tests PASS

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add backend/core/reactive_state/store.py tests/unit/core/reactive_state/test_store_audit.py
git commit -m "feat(disease8): wire post-replay invariant audit into store.open() (Wave 1, Task 4)"
```

---

## Task 5: Rejection counters and observability

**Files:**
- Modify: `backend/core/reactive_state/store.py`
- Test: `tests/unit/core/reactive_state/test_store_observability.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_store_observability.py
"""Tests for rejection counters and observability."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import WriteStatus


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "obs.db",
        epoch=1,
        session_id="obs-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


class TestRejectionCounters:
    def test_initial_counters_empty(self, store: ReactiveStateStore) -> None:
        stats = store.rejection_stats()
        assert stats == {}

    def test_ownership_rejection_counted(self, store: ReactiveStateStore) -> None:
        store.write(key="gcp.offload_active", value=True, expected_version=0, writer="supervisor")
        stats = store.rejection_stats()
        assert ("gcp.offload_active", "OWNERSHIP_REJECTED") in stats
        assert stats[("gcp.offload_active", "OWNERSHIP_REJECTED")] == 1

    def test_multiple_rejections_accumulated(self, store: ReactiveStateStore) -> None:
        for _ in range(3):
            store.write(key="gcp.offload_active", value=True, expected_version=0, writer="supervisor")
        stats = store.rejection_stats()
        assert stats[("gcp.offload_active", "OWNERSHIP_REJECTED")] == 3

    def test_different_reasons_tracked_separately(self, store: ReactiveStateStore) -> None:
        # Ownership rejection
        store.write(key="gcp.offload_active", value=True, expected_version=0, writer="supervisor")
        # Schema rejection
        store.write(key="gcp.offload_active", value="not_bool", expected_version=0, writer="gcp_controller")
        stats = store.rejection_stats()
        assert ("gcp.offload_active", "OWNERSHIP_REJECTED") in stats
        assert ("gcp.offload_active", "SCHEMA_INVALID") in stats

    def test_policy_rejection_counted(self, store: ReactiveStateStore) -> None:
        entry = store.read("gcp.offload_active")
        store.write(key="gcp.offload_active", value=True, expected_version=entry.version, writer="gcp_controller")
        stats = store.rejection_stats()
        assert ("gcp.offload_active", "POLICY_REJECTED") in stats

    def test_successful_writes_not_counted(self, store: ReactiveStateStore) -> None:
        entry = store.read("gcp.node_ip")
        store.write(key="gcp.node_ip", value="10.0.0.1", expected_version=entry.version, writer="gcp_controller")
        stats = store.rejection_stats()
        # No rejections for gcp.node_ip
        assert all(k[0] != "gcp.node_ip" for k in stats)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_observability.py -v`
Expected: FAIL — `AttributeError: 'ReactiveStateStore' object has no attribute 'rejection_stats'`

**Step 3: Modify store.py**

Add to `__init__`:
```python
from collections import Counter
# ...
self._rejection_counters: Counter = Counter()
```

Add method:
```python
def rejection_stats(self) -> Dict[tuple, int]:
    """Return rejection counts as {(key, reason_value): count}."""
    return dict(self._rejection_counters)
```

Modify `_reject()` to increment counter:
```python
def _reject(self, key, writer, reason, attempted_version) -> WriteResult:
    self._rejection_counters[(key, reason.value)] += 1
    # ... rest of existing _reject code ...
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_observability.py -v`
Expected: All 6 tests PASS

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add backend/core/reactive_state/store.py tests/unit/core/reactive_state/test_store_observability.py
git commit -m "feat(disease8): add rejection counters for per-key observability (Wave 1, Task 5)"
```

---

## Task 6: Update package exports and Wave 1 integration test

**Files:**
- Modify: `backend/core/reactive_state/__init__.py`
- Test: `tests/unit/core/reactive_state/test_wave1_integration.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_wave1_integration.py
"""Wave 1 integration — policy + audit + observability end-to-end."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.reactive_state import (
    ReactiveStateStore,
    WriteStatus,
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.policy import build_default_policy_engine


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    audit = AuditLog()
    s = ReactiveStateStore(
        journal_path=tmp_path / "w1_int.db",
        epoch=1,
        session_id="w1-integration",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
        audit_log=audit,
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


class TestWave1Integration:
    def test_full_gcp_activation_sequence(self, store: ReactiveStateStore) -> None:
        """Correct sequence: set IP -> set offload -> set hollow."""
        # Step 1: Set IP
        ip = store.read("gcp.node_ip")
        r1 = store.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip.version, writer="gcp_controller")
        assert r1.status == WriteStatus.OK

        # Step 2: Set offload (now allowed — IP is set)
        offload = store.read("gcp.offload_active")
        r2 = store.write(key="gcp.offload_active", value=True, expected_version=offload.version, writer="gcp_controller")
        assert r2.status == WriteStatus.OK

        # Step 3: Set hollow (now allowed — offload is active)
        hollow = store.read("hollow.client_active")
        r3 = store.write(key="hollow.client_active", value=True, expected_version=hollow.version, writer="gcp_controller")
        assert r3.status == WriteStatus.OK

    def test_wrong_sequence_rejected(self, store: ReactiveStateStore) -> None:
        """Out-of-order: set hollow without offload -> POLICY_REJECTED."""
        hollow = store.read("hollow.client_active")
        r = store.write(key="hollow.client_active", value=True, expected_version=hollow.version, writer="gcp_controller")
        assert r.status == WriteStatus.POLICY_REJECTED

        # Rejection counted
        stats = store.rejection_stats()
        assert ("hollow.client_active", "POLICY_REJECTED") in stats

    def test_replay_audit_detects_inconsistency(self, tmp_path: Path) -> None:
        """Write consistent state, tamper journal, reopen -> audit finds error."""
        import json
        import sqlite3

        db_path = tmp_path / "replay_audit.db"
        audit1 = AuditLog()
        s1 = ReactiveStateStore(
            journal_path=db_path, epoch=1, session_id="s1",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
            audit_log=audit1,
        )
        s1.open()
        s1.initialize_defaults()
        # Set IP and offload correctly
        ip = s1.read("gcp.node_ip")
        s1.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip.version, writer="gcp_controller")
        offload = s1.read("gcp.offload_active")
        s1.write(key="gcp.offload_active", value=True, expected_version=offload.version, writer="gcp_controller")
        s1.close()

        # Tamper: clear IP in journal
        conn = sqlite3.connect(str(db_path))
        max_rev = conn.execute("SELECT MAX(global_revision) FROM state_journal").fetchone()[0]
        conn.execute(
            "INSERT INTO state_journal (global_revision, key, value, previous_value, "
            "version, epoch, writer, writer_session_id, origin, consistency_group, "
            "timestamp_unix_ms, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (max_rev + 1, "gcp.node_ip", json.dumps(""), json.dumps("10.0.0.1"),
             3, 1, "gcp_controller", "tamper", "explicit", None, 0, "tampered"),
        )
        conn.commit()
        conn.close()

        # Reopen with new audit log
        audit2 = AuditLog()
        s2 = ReactiveStateStore(
            journal_path=db_path, epoch=2, session_id="s2",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
            audit_log=audit2,
        )
        s2.open()
        assert audit2.has_critical_findings() is True
        s2.close()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_wave1_integration.py -v`
Expected: All 3 tests PASS (if Tasks 1-5 are complete — this is a validation test)

**Step 3: Update __init__.py exports**

Read the current `__init__.py` first, then add the new exports:

```python
# backend/core/reactive_state/__init__.py
"""Reactive State Propagation — Disease 8 cure.

Replaces 23+ environment variables used for cross-component state
with a versioned, observable, typed, CAS-protected state store.
"""
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import PolicyEngine, build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import (
    StateEntry,
    WriteResult,
    WriteStatus,
)

__all__ = [
    "AuditLog",
    "AuditSeverity",
    "PolicyEngine",
    "ReactiveStateStore",
    "StateEntry",
    "WriteResult",
    "WriteStatus",
    "build_default_policy_engine",
    "build_ownership_registry",
    "build_schema_registry",
]
```

**Step 4: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/reactive_state/__init__.py tests/unit/core/reactive_state/test_wave1_integration.py
git commit -m "feat(disease8): update exports and add Wave 1 integration test (Wave 1, Task 6)"
```

---

## Summary

| Task | Module | Tests | What it builds |
|------|--------|-------|---------------|
| 1 | `policy.py` | 17 | PolicyResult, PolicyEngine, 3 invariant rules, builder |
| 2 | `store.py` (modify) | 7 | Step 5 policy hook in write pipeline |
| 3 | `audit.py` | 11 | SchemaViolation, AuditFinding, AuditLog, post_replay_invariant_audit |
| 4 | `store.py` (modify) | 4 | Wire audit into _replay(), audit_log property |
| 5 | `store.py` (modify) | 6 | Rejection counters per (key, reason) |
| 6 | `__init__.py` + integration | 3 | Updated exports, end-to-end GCP activation sequence test |
| **Total** | **3 new + 2 modified** | **~48** | **Complete Wave 1** |

**What's ready after Wave 1:**
- Cross-key invariant enforcement at write time (step 5 in pipeline)
- Post-replay audit that flags inconsistent journal state
- Schema violation tracking with bounded history
- Rejection counters for monitoring
- Full GCP activation sequence test (IP -> offload -> hollow)

**What Wave 2 adds (next plan):**
- UMF `state.changed` event emission after journal commit
- `last_published_revision` cursor in journal DB
- Background reconciler for crash recovery
- Cross-repo event subscription
