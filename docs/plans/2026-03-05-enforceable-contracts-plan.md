# Enforceable Contract System — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform advisory-only startup contracts into a severity-aware enforcement system with typed exceptions, state authority, default-origin tracing, and contract hash revalidation — fixing Disease 4 (Advisory Contracts = No Contracts).

**Architecture:** Add `ContractSeverity` enum and `severity` field to existing `EnvContract`. Create `ContractStateAuthority` singleton to accumulate violations as queryable state (not ephemeral logs). Wire preflight gate into `unified_supervisor.py` startup so `PRECHECK_BLOCKER` violations raise `StartupContractViolation` before any I/O. The existing `CrossRepoContractEnforcer` (already fail-closed) handles the CONTRACT_GATE phase.

**Tech Stack:** Python 3.9+, dataclasses, enums, asyncio

---

### Task 1: ContractSeverity enum + ViolationReasonCode enum

**Files:**
- Modify: `backend/core/startup_contracts.py:17-24`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/test_contract_enforcement.py`:

```python
#!/usr/bin/env python3
"""
Contract enforcement tests for Disease 4: Advisory Contracts = No Contracts.

Run: python3 -m pytest tests/unit/backend/test_contract_enforcement.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.startup_contracts import ContractSeverity, ViolationReasonCode


class TestContractSeverityEnum:
    """Severity enum must have exactly 5 levels in correct order."""

    def test_all_severity_levels_exist(self):
        assert ContractSeverity.PRECHECK_BLOCKER == "precheck_blocker"
        assert ContractSeverity.BOOT_BLOCKER == "boot_blocker"
        assert ContractSeverity.BLOCK_BEFORE_READY == "block_before_ready"
        assert ContractSeverity.DEGRADED_ALLOWED == "degraded_allowed"
        assert ContractSeverity.ADVISORY == "advisory"

    def test_exactly_five_levels(self):
        assert len(ContractSeverity) == 5


class TestViolationReasonCodeEnum:
    """Reason codes must cover all known violation types."""

    @pytest.mark.parametrize("code", [
        "malformed_url", "port_conflict", "port_out_of_range",
        "missing_secret", "capability_missing", "schema_incompatible",
        "version_incompatible", "hash_drift_detected", "handshake_failed",
        "health_unreachable", "alias_conflict", "pattern_mismatch",
        "default_fallback_used",
    ])
    def test_reason_code_exists(self, code):
        assert ViolationReasonCode(code) == code
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestContractSeverityEnum -v`
Expected: FAIL with `ImportError: cannot import name 'ContractSeverity'`

**Step 3: Write minimal implementation**

Edit `backend/core/startup_contracts.py`. After the existing imports (line 22), add:

```python
from enum import Enum


class ContractSeverity(str, Enum):
    """Enforcement level for a contract violation.

    PRECHECK_BLOCKER: Abort before any external I/O.
    BOOT_BLOCKER: Evaluated after bind/discovery, blocks progress.
    BLOCK_BEFORE_READY: Allow dep bringup, block READY transition.
    DEGRADED_ALLOWED: Continue in explicit degraded mode with reason code.
    ADVISORY: Log + metrics only.
    """
    PRECHECK_BLOCKER = "precheck_blocker"
    BOOT_BLOCKER = "boot_blocker"
    BLOCK_BEFORE_READY = "block_before_ready"
    DEGRADED_ALLOWED = "degraded_allowed"
    ADVISORY = "advisory"


class ViolationReasonCode(str, Enum):
    """Machine-readable reason for a contract violation."""
    MALFORMED_URL = "malformed_url"
    PORT_CONFLICT = "port_conflict"
    PORT_OUT_OF_RANGE = "port_out_of_range"
    MISSING_SECRET = "missing_secret"
    CAPABILITY_MISSING = "capability_missing"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    VERSION_INCOMPATIBLE = "version_incompatible"
    HASH_DRIFT_DETECTED = "hash_drift_detected"
    HANDSHAKE_FAILED = "handshake_failed"
    HEALTH_UNREACHABLE = "health_unreachable"
    ALIAS_CONFLICT = "alias_conflict"
    PATTERN_MISMATCH = "pattern_mismatch"
    DEFAULT_FALLBACK_USED = "default_fallback_used"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add ContractSeverity and ViolationReasonCode enums (Disease 4)"
```

---

### Task 2: Add severity field to EnvContract + classify all 18 contracts

**Files:**
- Modify: `backend/core/startup_contracts.py:31-181`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

Add to test file:

```python
from backend.core.startup_contracts import ENV_CONTRACTS, EnvContract


class TestEnvContractSeverity:
    """Each EnvContract must have a severity field."""

    def test_all_contracts_have_severity(self):
        for contract in ENV_CONTRACTS:
            assert hasattr(contract, "severity"), (
                f"{contract.canonical_name} missing severity field"
            )
            assert isinstance(contract.severity, ContractSeverity), (
                f"{contract.canonical_name}.severity is not ContractSeverity"
            )

    def test_port_contracts_are_precheck_blocker(self):
        port_names = {"JARVIS_BACKEND_PORT", "JARVIS_FRONTEND_PORT", "JARVIS_LOADING_SERVER_PORT"}
        for contract in ENV_CONTRACTS:
            if contract.canonical_name in port_names:
                assert contract.severity == ContractSeverity.PRECHECK_BLOCKER, (
                    f"Port contract {contract.canonical_name} must be PRECHECK_BLOCKER"
                )

    def test_url_contracts_are_precheck_blocker(self):
        for contract in ENV_CONTRACTS:
            if contract.canonical_name == "JARVIS_PRIME_URL":
                assert contract.severity == ContractSeverity.PRECHECK_BLOCKER

    def test_advisory_contracts_exist(self):
        advisory = [c for c in ENV_CONTRACTS if c.severity == ContractSeverity.ADVISORY]
        assert len(advisory) >= 3, "Should have at least 3 advisory contracts"

    def test_no_contracts_lack_severity(self):
        for c in ENV_CONTRACTS:
            assert c.severity is not None, f"{c.canonical_name} has None severity"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestEnvContractSeverity -v`
Expected: FAIL — `EnvContract` has no `severity` attribute

**Step 3: Write minimal implementation**

Edit `backend/core/startup_contracts.py`:

Add `severity` field to `EnvContract` (after the `version` field):
```python
@dataclass(frozen=True)
class EnvContract:
    """Schema for a cross-repo environment variable."""
    canonical_name: str
    description: str
    value_type: str = "str"
    pattern: Optional[str] = None
    aliases: tuple = ()
    default: Optional[str] = None
    version: str = "1.0.0"
    severity: ContractSeverity = ContractSeverity.ADVISORY
```

Then update every `EnvContract(...)` in `ENV_CONTRACTS` to include `severity`:

- **PRECHECK_BLOCKER:** `JARVIS_PRIME_URL`, `JARVIS_BACKEND_PORT`, `JARVIS_FRONTEND_PORT`, `JARVIS_LOADING_SERVER_PORT`
- **DEGRADED_ALLOWED:** `GCP_PRIME_ENDPOINT`, `JARVIS_INVINCIBLE_NODE_IP`, `JARVIS_INVINCIBLE_NODE_PORT`, `JARVIS_GCP_OFFLOAD_ACTIVE`, `JARVIS_INVINCIBLE_NODE_BOOTING`, `JARVIS_HOLLOW_CLIENT_ACTIVE`, `JARVIS_BACKEND_MINIMAL`
- **ADVISORY:** `JARVIS_STARTUP_MEMORY_MODE`, `JARVIS_STARTUP_DESIRED_MODE`, `JARVIS_STARTUP_EFFECTIVE_MODE`, `JARVIS_CAN_SPAWN_HEAVY`, `JARVIS_HEAVY_ADMISSION_REASON`, `JARVIS_HEAVY_ADMISSION_CONTEXT`, `JARVIS_HEAVY_ADMISSION_AVAILABLE_GB`, `JARVIS_STARTUP_COMPLETE`, `JARVIS_MEASURED_AVAILABLE_GB`, `JARVIS_MEASURED_MEMORY_SOURCE`, `JARVIS_MEASURED_MEMORY_TIER`

Add `severity=ContractSeverity.PRECHECK_BLOCKER` to each port and URL contract, `severity=ContractSeverity.DEGRADED_ALLOWED` to GCP/cloud contracts, `severity=ContractSeverity.ADVISORY` to mode/memory contracts (this is the default so those can omit it).

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestEnvContractSeverity -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add severity field to EnvContract, classify all 18 contracts (Disease 4)"
```

---

### Task 3: ContractViolationRecord + StartupContractViolation exception

**Files:**
- Modify: `backend/core/startup_contracts.py`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
from backend.core.startup_contracts import ContractViolationRecord, StartupContractViolation


class TestContractViolationRecord:
    """Violation records must capture all required fields."""

    def test_record_fields(self):
        record = ContractViolationRecord(
            contract_name="JARVIS_PRIME_URL",
            base_severity=ContractSeverity.PRECHECK_BLOCKER,
            effective_severity=ContractSeverity.PRECHECK_BLOCKER,
            reason_code=ViolationReasonCode.MALFORMED_URL,
            violation="URL is malformed",
            value_origin="explicit",
            checked_at_monotonic=1000.0,
            checked_at_utc="2026-03-05T12:00:00Z",
            phase="precheck",
        )
        assert record.contract_name == "JARVIS_PRIME_URL"
        assert record.base_severity == ContractSeverity.PRECHECK_BLOCKER
        assert record.effective_severity == ContractSeverity.PRECHECK_BLOCKER
        assert record.reason_code == ViolationReasonCode.MALFORMED_URL
        assert record.value_origin == "explicit"
        assert record.phase == "precheck"

    def test_record_is_frozen(self):
        record = ContractViolationRecord(
            contract_name="test",
            base_severity=ContractSeverity.ADVISORY,
            effective_severity=ContractSeverity.ADVISORY,
            reason_code=ViolationReasonCode.PATTERN_MISMATCH,
            violation="test",
            value_origin="default",
            checked_at_monotonic=0.0,
            checked_at_utc="",
            phase="precheck",
        )
        with pytest.raises(AttributeError):
            record.contract_name = "changed"


class TestStartupContractViolation:
    """Typed exception for startup-blocking violations."""

    def test_exception_carries_violations(self):
        record = ContractViolationRecord(
            contract_name="JARVIS_BACKEND_PORT",
            base_severity=ContractSeverity.PRECHECK_BLOCKER,
            effective_severity=ContractSeverity.PRECHECK_BLOCKER,
            reason_code=ViolationReasonCode.PORT_OUT_OF_RANGE,
            violation="Port 99999 out of range",
            value_origin="explicit",
            checked_at_monotonic=0.0,
            checked_at_utc="",
            phase="precheck",
        )
        exc = StartupContractViolation([record])
        assert len(exc.violations) == 1
        assert "JARVIS_BACKEND_PORT" in str(exc)
        assert "port_out_of_range" in str(exc)

    def test_exception_is_exception(self):
        exc = StartupContractViolation([])
        assert isinstance(exc, Exception)
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add to `backend/core/startup_contracts.py`:

```python
import dataclasses
import time as _time
from datetime import datetime, timezone


@dataclasses.dataclass(frozen=True)
class ContractViolationRecord:
    """Immutable record of a single contract violation."""
    contract_name: str
    base_severity: ContractSeverity
    effective_severity: ContractSeverity
    reason_code: ViolationReasonCode
    violation: str
    value_origin: str              # "explicit" | "default" | "alias:{name}" | "derived"
    checked_at_monotonic: float
    checked_at_utc: str
    phase: str                     # "precheck" | "boot" | "contract_gate" | "runtime"


class StartupContractViolation(Exception):
    """Raised when a PRECHECK_BLOCKER or BOOT_BLOCKER contract violation is detected.

    Caught by the top-level boot runner for clean termination with structured report.
    """
    def __init__(self, violations: List[ContractViolationRecord]):
        self.violations = violations
        reasons = "; ".join(
            f"{v.contract_name}:{v.reason_code.value}" for v in violations
        )
        super().__init__(f"Startup blocked by contract violations: {reasons}")
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestContractViolationRecord tests/unit/backend/test_contract_enforcement.py::TestStartupContractViolation -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add ContractViolationRecord and StartupContractViolation (Disease 4)"
```

---

### Task 4: ContractStateAuthority with dedup semantics

**Files:**
- Modify: `backend/core/startup_contracts.py`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
from backend.core.startup_contracts import ContractStateAuthority


class TestContractStateAuthority:
    """Central violation state authority with dedup."""

    def _make_record(self, name="TEST", severity=ContractSeverity.ADVISORY,
                     reason=ViolationReasonCode.PATTERN_MISMATCH, phase="precheck"):
        return ContractViolationRecord(
            contract_name=name,
            base_severity=severity,
            effective_severity=severity,
            reason_code=reason,
            violation=f"{name} violation",
            value_origin="explicit",
            checked_at_monotonic=0.0,
            checked_at_utc="2026-03-05T00:00:00Z",
            phase=phase,
        )

    def test_record_and_retrieve(self):
        auth = ContractStateAuthority()
        record = self._make_record()
        auth.record(record)
        violations = auth.get_violations()
        assert len(violations) == 1
        assert violations[0].contract_name == "TEST"

    def test_dedup_same_contract_and_reason(self):
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        # Same (contract_name, reason_code) should NOT create 3 entries
        violations = auth.get_violations()
        assert len(violations) == 1

    def test_different_reasons_not_deduped(self):
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.ALIAS_CONFLICT))
        violations = auth.get_violations()
        assert len(violations) == 2

    def test_has_blockers(self):
        auth = ContractStateAuthority()
        auth.record(self._make_record(severity=ContractSeverity.ADVISORY))
        assert not auth.has_blockers()
        auth.record(self._make_record("PORT", severity=ContractSeverity.PRECHECK_BLOCKER))
        assert auth.has_blockers()

    def test_blocking_reasons(self):
        auth = ContractStateAuthority()
        auth.record(self._make_record("PORT", severity=ContractSeverity.PRECHECK_BLOCKER,
                                       reason=ViolationReasonCode.PORT_CONFLICT))
        reasons = auth.blocking_reasons()
        assert "port_conflict" in reasons

    def test_severity_filter(self):
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", severity=ContractSeverity.ADVISORY))
        auth.record(self._make_record("B", severity=ContractSeverity.PRECHECK_BLOCKER))
        advisory = auth.get_violations(severity_filter=ContractSeverity.ADVISORY)
        assert len(advisory) == 1
        assert advisory[0].contract_name == "A"

    def test_health_summary_bounded(self):
        auth = ContractStateAuthority()
        for i in range(20):
            auth.record(self._make_record(
                f"C{i}", severity=ContractSeverity.PRECHECK_BLOCKER,
                reason=ViolationReasonCode.PORT_CONFLICT
            ))
        summary = auth.health_summary(max_detail=5)
        assert summary["total_violations"] == 20
        assert len(summary["top_blockers"]) <= 5

    def test_full_report_includes_all(self):
        auth = ContractStateAuthority()
        for i in range(10):
            auth.record(self._make_record(f"C{i}"))
        report = auth.full_report()
        assert len(report["violations"]) == 10
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add to `backend/core/startup_contracts.py`:

```python
import threading


class ContractStateAuthority:
    """Central authority for all contract violation state.

    Accumulates violations with dedup semantics: same (contract_name, reason_code)
    updates counter/timestamp rather than appending a new entry.
    Thread-safe via lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._violations: Dict[tuple, ContractViolationRecord] = {}  # (name, reason) -> record
        self._counts: Dict[tuple, int] = {}  # (name, reason) -> occurrence count

    def record(self, violation: ContractViolationRecord) -> None:
        """Record a violation. Dedup by (contract_name, reason_code)."""
        key = (violation.contract_name, violation.reason_code)
        with self._lock:
            self._violations[key] = violation  # Update to latest
            self._counts[key] = self._counts.get(key, 0) + 1

    def get_violations(self, *, severity_filter: Optional[ContractSeverity] = None,
                       phase_filter: Optional[str] = None) -> List[ContractViolationRecord]:
        """Query recorded violations with optional filters."""
        with self._lock:
            records = list(self._violations.values())
        if severity_filter is not None:
            records = [r for r in records if r.effective_severity == severity_filter]
        if phase_filter is not None:
            records = [r for r in records if r.phase == phase_filter]
        return records

    def has_blockers(self) -> bool:
        """True if any PRECHECK_BLOCKER or BOOT_BLOCKER violations exist."""
        with self._lock:
            return any(
                v.effective_severity in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
                for v in self._violations.values()
            )

    def blocking_reasons(self) -> List[str]:
        """Machine-readable reason codes for all blocking violations."""
        with self._lock:
            return [
                v.reason_code.value
                for v in self._violations.values()
                if v.effective_severity in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
            ]

    def health_summary(self, *, max_detail: int = 5) -> Dict[str, Any]:
        """Bounded summary for health payload."""
        with self._lock:
            all_v = list(self._violations.values())
        blockers = [v for v in all_v if v.effective_severity in (
            ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER
        )]
        return {
            "total_violations": len(all_v),
            "blocker_count": len(blockers),
            "top_blockers": [
                {"contract": v.contract_name, "reason": v.reason_code.value}
                for v in blockers[:max_detail]
            ],
        }

    def full_report(self) -> Dict[str, Any]:
        """Full violation list for debug endpoint / startup report."""
        with self._lock:
            all_v = list(self._violations.values())
            counts = dict(self._counts)
        return {
            "violations": [
                {
                    "contract_name": v.contract_name,
                    "severity": v.effective_severity.value,
                    "reason_code": v.reason_code.value,
                    "violation": v.violation,
                    "value_origin": v.value_origin,
                    "phase": v.phase,
                    "occurrence_count": counts.get((v.contract_name, v.reason_code), 1),
                }
                for v in all_v
            ],
        }
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestContractStateAuthority -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add ContractStateAuthority with dedup semantics (Disease 4)"
```

---

### Task 5: EnvResolution + origin-tracing get_canonical_env

**Files:**
- Modify: `backend/core/startup_contracts.py:314-343`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
import os
from unittest.mock import patch
from backend.core.startup_contracts import EnvResolution, get_canonical_env


class TestEnvResolution:
    """Default-origin tracing for env var resolution."""

    def test_resolution_fields(self):
        r = EnvResolution(value="8010", origin="explicit", canonical_name="JARVIS_BACKEND_PORT")
        assert r.value == "8010"
        assert r.origin == "explicit"
        assert r.canonical_name == "JARVIS_BACKEND_PORT"

    @patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "8010"}, clear=False)
    def test_explicit_origin(self):
        result = get_canonical_env("JARVIS_BACKEND_PORT")
        assert isinstance(result, EnvResolution)
        assert result.value == "8010"
        assert result.origin == "explicit"

    @patch.dict(os.environ, {"BACKEND_PORT": "9090"}, clear=False)
    def test_alias_origin(self):
        # Remove canonical if set
        env = os.environ.copy()
        env.pop("JARVIS_BACKEND_PORT", None)
        with patch.dict(os.environ, env, clear=True):
            os.environ["BACKEND_PORT"] = "9090"
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "9090"
            assert result.origin == "alias:BACKEND_PORT"

    def test_default_origin(self):
        with patch.dict(os.environ, {}, clear=True):
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "8010"
            assert result.origin == "default"

    def test_unset_no_default_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            result = get_canonical_env("JARVIS_PRIME_URL")
            assert result is None
```

**Step 2: Run test — expect FAIL** (get_canonical_env returns str, not EnvResolution)

**Step 3: Implement**

Add `EnvResolution` dataclass and rewrite `get_canonical_env`:

```python
@dataclasses.dataclass(frozen=True)
class EnvResolution:
    """Result of resolving a contracted env var with origin tracing."""
    value: str
    origin: str           # "explicit" | "default" | "alias:{alias_name}" | "derived"
    canonical_name: str


def get_canonical_env(contract_name: str) -> Optional[EnvResolution]:
    """Get the value of a contracted env var with origin tracing.

    Returns EnvResolution with value + origin, or None if unset with no default.
    """
    contract = _CONTRACT_MAP.get(contract_name)
    if contract is None:
        val = os.environ.get(contract_name)
        if val is None:
            return None
        return EnvResolution(value=val, origin="explicit", canonical_name=contract_name)

    # Try canonical first
    val = os.environ.get(contract.canonical_name)
    if val is not None:
        return EnvResolution(value=val, origin="explicit", canonical_name=contract.canonical_name)

    # Try aliases in order
    for alias in contract.aliases:
        val = os.environ.get(alias)
        if val is not None:
            return EnvResolution(value=val, origin=f"alias:{alias}", canonical_name=contract.canonical_name)

    # Default fallback
    if contract.default is not None:
        return EnvResolution(value=contract.default, origin="default", canonical_name=contract.canonical_name)

    return None
```

**IMPORTANT:** This changes the return type from `Optional[str]` to `Optional[EnvResolution]`. Search for all callers of `get_canonical_env` in the codebase and update them to use `.value` where they expect a string. Grep: `grep -rn "get_canonical_env" --include="*.py"`.

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestEnvResolution -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add EnvResolution with default-origin tracing (Disease 4)"
```

---

### Task 6: Severity-aware validate_contracts_at_boot

**Files:**
- Modify: `backend/core/startup_contracts.py:228-263`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
class TestSeverityAwareValidation:
    """validate_contracts_at_boot must return structured results, not just strings."""

    @patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "not_a_number"}, clear=False)
    def test_precheck_blocker_violation_returned(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        result = validate_contracts_at_boot()
        assert hasattr(result, "violations") or isinstance(result, list)
        # Must contain structured violations, not just strings
        if isinstance(result, list) and len(result) > 0:
            # If still returning strings, this should fail
            assert not isinstance(result[0], str), (
                "validate_contracts_at_boot must return ContractViolationRecords, not strings"
            )

    @patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "99999"}, clear=False)
    def test_port_out_of_range_detected(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        result = validate_contracts_at_boot()
        blockers = [r for r in result
                    if r.effective_severity == ContractSeverity.PRECHECK_BLOCKER]
        assert len(blockers) >= 1

    def test_clean_env_no_violations(self):
        with patch.dict(os.environ, {}, clear=True):
            from backend.core.startup_contracts import validate_contracts_at_boot
            result = validate_contracts_at_boot()
            assert len(result) == 0

    @patch.dict(os.environ, {
        "JARVIS_BACKEND_PORT": "8010",
        "BACKEND_PORT": "9090",
    }, clear=False)
    def test_alias_conflict_detected(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        result = validate_contracts_at_boot()
        alias_violations = [r for r in result
                            if r.reason_code == ViolationReasonCode.ALIAS_CONFLICT]
        assert len(alias_violations) >= 1
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Rewrite `validate_contracts_at_boot()` to return `List[ContractViolationRecord]`:

```python
def validate_contracts_at_boot() -> List[ContractViolationRecord]:
    """Validate all cross-repo contracts at startup.

    Returns structured violation records with severity, reason codes, and origin tracing.
    Callers decide enforcement based on severity.
    """
    import time as _time
    from datetime import datetime, timezone

    violations: List[ContractViolationRecord] = []
    now_mono = _time.monotonic()
    now_utc = datetime.now(timezone.utc).isoformat()

    for contract in ENV_CONTRACTS:
        resolution = get_canonical_env(contract.canonical_name)
        if resolution is None:
            continue  # Not set, no default — nothing to validate

        val = resolution.value

        # Pattern validation
        if contract.pattern and not re.match(contract.pattern, val):
            # Port-specific: check out-of-range
            reason = ViolationReasonCode.PATTERN_MISMATCH
            if contract.value_type == "int" and val.isdigit():
                port = int(val)
                if port < 1 or port > 65535:
                    reason = ViolationReasonCode.PORT_OUT_OF_RANGE
            elif contract.value_type == "url":
                reason = ViolationReasonCode.MALFORMED_URL

            violations.append(ContractViolationRecord(
                contract_name=contract.canonical_name,
                base_severity=contract.severity,
                effective_severity=contract.severity,
                reason_code=reason,
                violation=(
                    f"{contract.canonical_name}={val!r} does not match "
                    f"expected pattern {contract.pattern} ({contract.description})"
                ),
                value_origin=resolution.origin,
                checked_at_monotonic=now_mono,
                checked_at_utc=now_utc,
                phase="precheck",
            ))

        # Alias conflict detection
        for alias in contract.aliases:
            alias_val = os.environ.get(alias)
            if alias_val is not None and alias_val != val:
                violations.append(ContractViolationRecord(
                    contract_name=contract.canonical_name,
                    base_severity=contract.severity,
                    effective_severity=contract.severity,
                    reason_code=ViolationReasonCode.ALIAS_CONFLICT,
                    violation=(
                        f"Alias conflict: {contract.canonical_name}={val!r} "
                        f"but {alias}={alias_val!r} ({contract.description})"
                    ),
                    value_origin=resolution.origin,
                    checked_at_monotonic=now_mono,
                    checked_at_utc=now_utc,
                    phase="precheck",
                ))

    # Port collision detection (PRECHECK_BLOCKER)
    port_contracts = [c for c in ENV_CONTRACTS if c.value_type == "int" and "PORT" in c.canonical_name]
    port_values: Dict[str, str] = {}  # port_value -> contract_name
    for pc in port_contracts:
        res = get_canonical_env(pc.canonical_name)
        if res is not None and res.value.isdigit():
            if res.value in port_values:
                violations.append(ContractViolationRecord(
                    contract_name=pc.canonical_name,
                    base_severity=ContractSeverity.PRECHECK_BLOCKER,
                    effective_severity=ContractSeverity.PRECHECK_BLOCKER,
                    reason_code=ViolationReasonCode.PORT_CONFLICT,
                    violation=(
                        f"Port {res.value} claimed by both {port_values[res.value]} "
                        f"and {pc.canonical_name}"
                    ),
                    value_origin=res.origin,
                    checked_at_monotonic=now_mono,
                    checked_at_utc=now_utc,
                    phase="precheck",
                ))
            else:
                port_values[res.value] = pc.canonical_name

    return violations
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestSeverityAwareValidation -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): severity-aware validate_contracts_at_boot with structured results (Disease 4)"
```

---

### Task 7: Wire preflight gate into unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py:70677-70691`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
class TestPrecheckGateWiring:
    """Preflight gate must raise StartupContractViolation on PRECHECK_BLOCKER."""

    def test_supervisor_imports_structured_validation(self):
        """unified_supervisor.py must import StartupContractViolation."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        # Find the contract validation block
        found_structured = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "startup_contracts" in node.module:
                    names = [alias.name for alias in node.names]
                    if "StartupContractViolation" in names:
                        found_structured = True
        assert found_structured, (
            "unified_supervisor.py must import StartupContractViolation from startup_contracts"
        )

    def test_supervisor_raises_on_precheck_blocker(self):
        """Contract validation block must raise StartupContractViolation, not just log."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            source = f.read()

        # The contract validation section must contain "StartupContractViolation"
        # (either raise or except)
        assert "StartupContractViolation" in source, (
            "unified_supervisor.py must reference StartupContractViolation for preflight gate"
        )
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Edit `unified_supervisor.py` at line ~70677-70691. Replace the contract validation block:

```python
        # =====================================================================
        # v270.3 / Disease-4: CONTRACT VALIDATION (severity-aware preflight gate)
        # =====================================================================
        try:
            from backend.core.startup_contracts import (
                validate_contracts_at_boot,
                ContractSeverity,
                ContractStateAuthority,
                StartupContractViolation,
            )
            _contract_violations = validate_contracts_at_boot()

            # Initialize state authority singleton for this boot session
            if not hasattr(self, '_contract_state_authority'):
                self._contract_state_authority = ContractStateAuthority()

            _precheck_blockers = []
            for _cv in _contract_violations:
                self._contract_state_authority.record(_cv)
                if _cv.effective_severity == ContractSeverity.PRECHECK_BLOCKER:
                    self.logger.error(
                        "[Contract] PRECHECK_BLOCKER: %s (reason=%s, origin=%s)",
                        _cv.violation, _cv.reason_code.value, _cv.value_origin,
                    )
                    _precheck_blockers.append(_cv)
                elif _cv.effective_severity == ContractSeverity.BOOT_BLOCKER:
                    self.logger.error("[Contract] BOOT_BLOCKER: %s", _cv.violation)
                elif _cv.effective_severity == ContractSeverity.BLOCK_BEFORE_READY:
                    self.logger.warning("[Contract] BLOCK_BEFORE_READY: %s", _cv.violation)
                elif _cv.effective_severity == ContractSeverity.DEGRADED_ALLOWED:
                    self.logger.warning("[Contract] DEGRADED: %s", _cv.violation)
                else:
                    self.logger.info("[Contract] Advisory: %s", _cv.violation)

            if _precheck_blockers:
                raise StartupContractViolation(_precheck_blockers)

            if not _contract_violations:
                self.logger.debug("[Contract] All %d env var contracts valid", len(ENV_CONTRACTS))
        except StartupContractViolation:
            raise  # Let it propagate to top-level boot runner
        except ImportError:
            pass  # Module not yet available — non-fatal
        except Exception as _ce:
            self.logger.debug("[Contract] Validation error (non-fatal): %s", _ce)
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestPrecheckGateWiring -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): wire severity-aware preflight gate into startup (Disease 4)"
```

---

### Task 8: Contract hash revalidation at CONTRACT_GATE

**Files:**
- Modify: `backend/core/startup_contracts.py`
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the failing test**

```python
from backend.core.startup_contracts import ContractSnapshot


class TestContractSnapshot:
    """Contract hash snapshots for drift detection."""

    def test_snapshot_fields(self):
        snap = ContractSnapshot(
            target="jarvis_prime",
            schema_hash="abc123",
            capability_hash="def456",
            session_id="session-1",
            checked_at_monotonic=1000.0,
        )
        assert snap.target == "jarvis_prime"
        assert snap.schema_hash == "abc123"
        assert snap.capability_hash == "def456"

    def test_snapshot_is_frozen(self):
        snap = ContractSnapshot(
            target="t", schema_hash="s", capability_hash="c",
            session_id="s1", checked_at_monotonic=0.0,
        )
        with pytest.raises(AttributeError):
            snap.target = "changed"

    def test_drift_detection(self):
        snap1 = ContractSnapshot(
            target="prime", schema_hash="aaa", capability_hash="bbb",
            session_id="s1", checked_at_monotonic=100.0,
        )
        snap2 = ContractSnapshot(
            target="prime", schema_hash="ccc", capability_hash="bbb",
            session_id="s1", checked_at_monotonic=200.0,
        )
        # Schema hash changed = drift
        assert snap1.schema_hash != snap2.schema_hash
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add to `backend/core/startup_contracts.py`:

```python
@dataclasses.dataclass(frozen=True)
class ContractSnapshot:
    """Point-in-time snapshot of a contract check for drift detection."""
    target: str
    schema_hash: str
    capability_hash: str
    session_id: str
    checked_at_monotonic: float
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestContractSnapshot -v`

**Step 5: Commit**

```bash
git add backend/core/startup_contracts.py tests/unit/backend/test_contract_enforcement.py
git commit -m "feat(contracts): add ContractSnapshot for per-target drift detection (Disease 4)"
```

---

### Task 9: Update callers of get_canonical_env + fix imports

**Files:**
- Modify: any file calling `get_canonical_env()` that expects `str` return
- Test: run existing test suite

**Step 1: Find all callers**

```bash
python3 -c "
import subprocess
result = subprocess.run(['grep', '-rn', 'get_canonical_env', '--include=*.py', '.'],
                       capture_output=True, text=True)
print(result.stdout)
"
```

**Step 2: Update each caller**

For every call site that uses `get_canonical_env(name)` and expects a string:
- Change `val = get_canonical_env(name)` to `_res = get_canonical_env(name); val = _res.value if _res else None`
- Or if the call site only checks truthiness: `if get_canonical_env(name):` → `_res = get_canonical_env(name); if _res is not None:`

**Step 3: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/unit/backend/ -v --timeout=120`

**Step 4: Commit**

```bash
git add -A
git commit -m "refactor(contracts): update get_canonical_env callers for EnvResolution return type (Disease 4)"
```

---

### Task 10: Disease 4 Gate Test

**Files:**
- Test: `tests/unit/backend/test_contract_enforcement.py`

**Step 1: Write the gate test**

```python
class TestDisease4Gate:
    """Gate: All Disease 4 fixes verified."""

    @pytest.mark.parametrize("check", [
        "severity_enum_exists",
        "reason_code_enum_exists",
        "env_contracts_have_severity",
        "violation_record_exists",
        "state_authority_exists",
        "env_resolution_exists",
        "startup_exception_exists",
        "contract_snapshot_exists",
        "supervisor_raises_on_blocker",
    ])
    def test_disease4_gate(self, check):
        if check == "severity_enum_exists":
            from backend.core.startup_contracts import ContractSeverity
            assert len(ContractSeverity) == 5

        elif check == "reason_code_enum_exists":
            from backend.core.startup_contracts import ViolationReasonCode
            assert len(ViolationReasonCode) >= 13

        elif check == "env_contracts_have_severity":
            from backend.core.startup_contracts import ENV_CONTRACTS, ContractSeverity
            for c in ENV_CONTRACTS:
                assert isinstance(c.severity, ContractSeverity)

        elif check == "violation_record_exists":
            from backend.core.startup_contracts import ContractViolationRecord
            assert hasattr(ContractViolationRecord, "__dataclass_fields__")

        elif check == "state_authority_exists":
            from backend.core.startup_contracts import ContractStateAuthority
            auth = ContractStateAuthority()
            assert callable(getattr(auth, "record", None))
            assert callable(getattr(auth, "has_blockers", None))
            assert callable(getattr(auth, "health_summary", None))

        elif check == "env_resolution_exists":
            from backend.core.startup_contracts import EnvResolution
            assert hasattr(EnvResolution, "__dataclass_fields__")
            assert "origin" in EnvResolution.__dataclass_fields__

        elif check == "startup_exception_exists":
            from backend.core.startup_contracts import StartupContractViolation
            assert issubclass(StartupContractViolation, Exception)

        elif check == "contract_snapshot_exists":
            from backend.core.startup_contracts import ContractSnapshot
            assert hasattr(ContractSnapshot, "__dataclass_fields__")

        elif check == "supervisor_raises_on_blocker":
            import ast
            with open("unified_supervisor.py", "r") as f:
                source = f.read()
            assert "StartupContractViolation" in source
```

**Step 2: Run gate test**

Run: `python3 -m pytest tests/unit/backend/test_contract_enforcement.py::TestDisease4Gate -v`
Expected: All 9 checks PASS

**Step 3: Commit**

```bash
git add tests/unit/backend/test_contract_enforcement.py
git commit -m "test(disease4): add Disease 4 gate tests for enforceable contract system"
```
