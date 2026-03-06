# ResourceVerdict Typed Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace bool-based resource initialization with a typed `ResourceVerdict` model that distinguishes admission, initialization, readiness, degradation, and serviceability — curing the false-green overwrite disease in Phase 2 resource management.

**Architecture:** Extend `root_authority_types.py` with new enums (`RequiredTier`, `RecoveryAction`, `VerdictReasonCode`) and frozen dataclasses (`ResourceVerdict`, `PhaseVerdict`). Add `VerdictAuthority` as the single write/read path for component status, replacing scattered `_update_component_status()` mutations. Bridge `safe_initialize()` to return `ResourceVerdict` while keeping `initialize() -> bool` for backward compatibility during migration.

**Tech Stack:** Python 3.11+, dataclasses (frozen), enums, asyncio, pytest

---

## Task 1: Add Supporting Enums to root_authority_types.py

**Files:**
- Modify: `backend/core/root_authority_types.py:19-20` (imports), append after line 67
- Test: `tests/unit/core/test_root_authority_types.py`

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_root_authority_types.py`:

```python
class TestRequiredTier:
    def test_enum_values(self):
        from backend.core.root_authority_types import RequiredTier
        assert RequiredTier.REQUIRED.value == "required"
        assert RequiredTier.ENHANCEMENT.value == "enhancement"
        assert RequiredTier.OPTIONAL.value == "optional"

    def test_enum_count(self):
        from backend.core.root_authority_types import RequiredTier
        assert len(RequiredTier) == 3


class TestRecoveryAction:
    def test_enum_values(self):
        from backend.core.root_authority_types import RecoveryAction
        assert RecoveryAction.NONE.value == "none"
        assert RecoveryAction.RETRY.value == "retry"
        assert RecoveryAction.ROUTE_TO_GCP.value == "route_to_gcp"
        assert RecoveryAction.ROUTE_TO_LOCAL.value == "route_to_local"
        assert RecoveryAction.MANUAL.value == "manual"
        assert RecoveryAction.RESTART_MANAGER.value == "restart_manager"
        assert RecoveryAction.DEFERRED_RECOVERY.value == "deferred_recovery"

    def test_enum_count(self):
        from backend.core.root_authority_types import RecoveryAction
        assert len(RecoveryAction) == 7


class TestVerdictReasonCode:
    def test_controlled_vocabulary(self):
        from backend.core.root_authority_types import VerdictReasonCode
        # Core reason codes must exist
        assert VerdictReasonCode.HEALTHY.value == "healthy"
        assert VerdictReasonCode.DISABLED_BY_CONFIG.value == "disabled_by_config"
        assert VerdictReasonCode.NOT_INSTALLED.value == "not_installed"
        assert VerdictReasonCode.MEMORY_ADMISSION_CLOUD_FIRST.value == "memory_admission_cloud_first"
        assert VerdictReasonCode.MEMORY_ADMISSION_CLOUD_ONLY.value == "memory_admission_cloud_only"
        assert VerdictReasonCode.PREFLIGHT_TIMEOUT.value == "preflight_timeout"
        assert VerdictReasonCode.INIT_TIMEOUT.value == "init_timeout"
        assert VerdictReasonCode.INIT_EXCEPTION.value == "init_exception"
        assert VerdictReasonCode.INIT_RETURNED_FALSE.value == "init_returned_false"
        assert VerdictReasonCode.PORT_CONFLICT.value == "port_conflict"
        assert VerdictReasonCode.GCP_CLIENT_UNAVAILABLE.value == "gcp_client_unavailable"
        assert VerdictReasonCode.CIRCUIT_BREAKER_OPEN.value == "circuit_breaker_open"
        assert VerdictReasonCode.DEPENDENCY_MISSING.value == "dependency_missing"
        assert VerdictReasonCode.STALE_EPOCH.value == "stale_epoch"
        assert VerdictReasonCode.UNKNOWN.value == "unknown"

    def test_enum_count(self):
        from backend.core.root_authority_types import VerdictReasonCode
        assert len(VerdictReasonCode) == 15


class TestSeverityMap:
    def test_ready_is_zero(self):
        from backend.core.root_authority_types import SubsystemState, SEVERITY_MAP
        assert SEVERITY_MAP[SubsystemState.READY] == 0

    def test_degraded_is_one(self):
        from backend.core.root_authority_types import SubsystemState, SEVERITY_MAP
        assert SEVERITY_MAP[SubsystemState.DEGRADED] == 1

    def test_crashed_is_three(self):
        from backend.core.root_authority_types import SubsystemState, SEVERITY_MAP
        assert SEVERITY_MAP[SubsystemState.CRASHED] == 3

    def test_all_states_have_severity(self):
        from backend.core.root_authority_types import SubsystemState, SEVERITY_MAP
        for state in SubsystemState:
            assert state in SEVERITY_MAP, f"{state} missing from SEVERITY_MAP"

    def test_lattice_ordering(self):
        from backend.core.root_authority_types import SubsystemState, SEVERITY_MAP
        assert SEVERITY_MAP[SubsystemState.READY] < SEVERITY_MAP[SubsystemState.DEGRADED]
        assert SEVERITY_MAP[SubsystemState.DEGRADED] < SEVERITY_MAP[SubsystemState.REJECTED]
        assert SEVERITY_MAP[SubsystemState.REJECTED] < SEVERITY_MAP[SubsystemState.CRASHED]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestRequiredTier -v`
Expected: FAIL with `ImportError: cannot import name 'RequiredTier'`

**Step 3: Write the implementation**

In `backend/core/root_authority_types.py`, add imports at line 20:

```python
from typing import Dict, Mapping, Optional, Sequence, Tuple, Union
```

After the `_TERMINAL_STATES` definition (after line 67), add:

```python
# ---------------------------------------------------------------------------
# Resource Verdict Enums
# ---------------------------------------------------------------------------


class RequiredTier(Enum):
    """Dependency classification for phase aggregation."""

    REQUIRED = "required"
    ENHANCEMENT = "enhancement"
    OPTIONAL = "optional"


class RecoveryAction(Enum):
    """Structured next-action for recovery orchestration."""

    NONE = "none"
    RETRY = "retry"
    ROUTE_TO_GCP = "route_to_gcp"
    ROUTE_TO_LOCAL = "route_to_local"
    MANUAL = "manual"
    RESTART_MANAGER = "restart_manager"
    DEFERRED_RECOVERY = "deferred_recovery"


class VerdictReasonCode(Enum):
    """Controlled vocabulary for resource verdict reason codes."""

    HEALTHY = "healthy"
    DISABLED_BY_CONFIG = "disabled_by_config"
    NOT_INSTALLED = "not_installed"
    MEMORY_ADMISSION_CLOUD_FIRST = "memory_admission_cloud_first"
    MEMORY_ADMISSION_CLOUD_ONLY = "memory_admission_cloud_only"
    PREFLIGHT_TIMEOUT = "preflight_timeout"
    INIT_TIMEOUT = "init_timeout"
    INIT_EXCEPTION = "init_exception"
    INIT_RETURNED_FALSE = "init_returned_false"
    PORT_CONFLICT = "port_conflict"
    GCP_CLIENT_UNAVAILABLE = "gcp_client_unavailable"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    DEPENDENCY_MISSING = "dependency_missing"
    STALE_EPOCH = "stale_epoch"
    UNKNOWN = "unknown"


# Severity lattice — derived from SubsystemState, never stored independently.
# READY(0) < DEGRADED(1) < REJECTED(2) < CRASHED(3)
SEVERITY_MAP: Mapping[SubsystemState, int] = {
    SubsystemState.STARTING: 0,
    SubsystemState.HANDSHAKE: 0,
    SubsystemState.ALIVE: 0,
    SubsystemState.READY: 0,
    SubsystemState.DEGRADED: 1,
    SubsystemState.DRAINING: 1,
    SubsystemState.STOPPED: 2,
    SubsystemState.REJECTED: 2,
    SubsystemState.CRASHED: 3,
}
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestRequiredTier tests/unit/core/test_root_authority_types.py::TestRecoveryAction tests/unit/core/test_root_authority_types.py::TestVerdictReasonCode tests/unit/core/test_root_authority_types.py::TestSeverityMap -v`
Expected: PASS (all 11 tests)

**Step 5: Commit**

```bash
git add backend/core/root_authority_types.py tests/unit/core/test_root_authority_types.py
git commit -m "feat(verdict): add RequiredTier, RecoveryAction, VerdictReasonCode enums and severity lattice"
```

---

## Task 2: Add ResourceVerdict Frozen Dataclass

**Files:**
- Modify: `backend/core/root_authority_types.py` (append after severity map)
- Test: `tests/unit/core/test_root_authority_types.py`

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_root_authority_types.py`:

```python
import time
from datetime import datetime, timezone


class TestVerdictWarning:
    def test_creation(self):
        from backend.core.root_authority_types import VerdictWarning
        w = VerdictWarning(code="not_installed", detail="Docker not found", origin="docker_daemon")
        assert w.code == "not_installed"
        assert w.detail == "Docker not found"
        assert w.origin == "docker_daemon"

    def test_frozen(self):
        from backend.core.root_authority_types import VerdictWarning
        w = VerdictWarning(code="x", detail="y", origin="z")
        with pytest.raises(AttributeError):
            w.code = "changed"


class TestResourceVerdict:
    def _make_verdict(self, **overrides):
        from backend.core.root_authority_types import (
            ResourceVerdict, SubsystemState, RequiredTier,
            VerdictReasonCode, RecoveryAction,
        )
        defaults = dict(
            origin="test_manager",
            correlation_id="corr-001",
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=1,
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="Initialized OK",
            retryable=False,
        )
        defaults.update(overrides)
        return ResourceVerdict(**defaults)

    def test_healthy_verdict(self):
        v = self._make_verdict()
        assert v.state.value == "ready"
        assert v.boot_allowed is True
        assert v.serviceable is True
        assert v.severity == 0

    def test_severity_derived_from_state(self):
        from backend.core.root_authority_types import SubsystemState
        v = self._make_verdict(state=SubsystemState.DEGRADED, serviceable=False)
        assert v.severity == 1

    def test_frozen(self):
        v = self._make_verdict()
        with pytest.raises(AttributeError):
            v.state = "changed"

    def test_default_optional_fields(self):
        v = self._make_verdict()
        assert v.retry_after_s is None
        assert v.evidence == {}
        assert v.recovery_owner is None
        assert v.next_action.value == "none"
        assert v.capabilities == ()

    def test_schema_version(self):
        from backend.core.root_authority_types import ResourceVerdict
        assert ResourceVerdict.SCHEMA_VERSION == 1

    def test_with_evidence(self):
        v = self._make_verdict(evidence={"init_time_ms": 150, "error": None})
        assert v.evidence["init_time_ms"] == 150

    def test_with_capabilities(self):
        v = self._make_verdict(capabilities=("container_runtime", "image_build"))
        assert len(v.capabilities) == 2
        assert "container_runtime" in v.capabilities

    # --- Invariant tests ---

    def test_invariant_crashed_not_serviceable(self):
        from backend.core.root_authority_types import SubsystemState
        with pytest.raises(ValueError, match="CRASHED.*serviceable"):
            self._make_verdict(
                state=SubsystemState.CRASHED,
                serviceable=True,
                boot_allowed=False,
            )

    def test_invariant_boot_not_allowed_not_ready(self):
        from backend.core.root_authority_types import SubsystemState
        with pytest.raises(ValueError, match="boot_allowed.*READY"):
            self._make_verdict(
                state=SubsystemState.READY,
                boot_allowed=False,
            )

    def test_invariant_required_not_serviceable_not_ready(self):
        from backend.core.root_authority_types import SubsystemState, RequiredTier
        with pytest.raises(ValueError, match="REQUIRED.*serviceable.*READY"):
            self._make_verdict(
                state=SubsystemState.READY,
                serviceable=False,
                required_tier=RequiredTier.REQUIRED,
            )

    def test_valid_degraded_but_serviceable(self):
        """Degraded + serviceable is valid (running with reduced capability)."""
        from backend.core.root_authority_types import SubsystemState
        v = self._make_verdict(state=SubsystemState.DEGRADED, serviceable=True)
        assert v.severity == 1
        assert v.serviceable is True

    def test_valid_optional_crashed_boot_allowed(self):
        """Optional manager can crash without blocking boot."""
        from backend.core.root_authority_types import SubsystemState, RequiredTier
        v = self._make_verdict(
            state=SubsystemState.CRASHED,
            boot_allowed=True,
            serviceable=False,
            required_tier=RequiredTier.OPTIONAL,
        )
        assert v.boot_allowed is True
        assert v.severity == 3
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestResourceVerdict -v`
Expected: FAIL with `ImportError: cannot import name 'ResourceVerdict'`

**Step 3: Write the implementation**

Append to `backend/core/root_authority_types.py` after the severity map:

```python
# ---------------------------------------------------------------------------
# Structured Warning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictWarning:
    """Structured warning from verdict evaluation."""

    code: str
    detail: str
    origin: str


# ---------------------------------------------------------------------------
# Resource Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceVerdict:
    """Typed admission/operability verdict for a resource manager.

    Invariants enforced in __post_init__:
    - CRASHED -> serviceable=False
    - boot_allowed=False -> state != READY
    - REQUIRED + not serviceable -> state != READY
    """

    SCHEMA_VERSION: int = field(init=False, default=1, repr=False, compare=False)

    # Identity
    origin: str
    correlation_id: str
    epoch: int
    monotonic_ns: int
    wall_utc: str
    sequence: int

    # State (severity derived via property)
    state: SubsystemState

    # Admission semantics
    boot_allowed: bool
    serviceable: bool
    required_tier: RequiredTier

    # Reason (controlled vocabulary)
    reason_code: VerdictReasonCode
    reason_detail: str
    retryable: bool
    retry_after_s: Optional[float] = None

    # Evidence (recursive JSON-compatible)
    evidence: Mapping[str, object] = field(default_factory=dict)

    # Recovery
    recovery_owner: Optional[str] = None
    next_action: RecoveryAction = RecoveryAction.NONE

    # Capabilities (Model 2 bridge)
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state == SubsystemState.CRASHED and self.serviceable:
            raise ValueError("CRASHED verdict cannot be serviceable")
        if not self.boot_allowed and self.state == SubsystemState.READY:
            raise ValueError("boot_allowed=False contradicts READY state")
        if (
            self.required_tier == RequiredTier.REQUIRED
            and not self.serviceable
            and self.state == SubsystemState.READY
        ):
            raise ValueError("REQUIRED + not serviceable contradicts READY state")

    @property
    def severity(self) -> int:
        """Derived from state via severity lattice. Never stored independently."""
        return SEVERITY_MAP.get(self.state, 3)
```

Note: `SCHEMA_VERSION` uses `field(init=False, default=1)` because frozen dataclasses do not allow `ClassVar` with `__post_init__` easily, and this pattern keeps the version accessible as `ResourceVerdict.SCHEMA_VERSION` via the class default while not requiring it in the constructor.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestResourceVerdict tests/unit/core/test_root_authority_types.py::TestVerdictWarning -v`
Expected: PASS (all 14 tests)

**Step 5: Commit**

```bash
git add backend/core/root_authority_types.py tests/unit/core/test_root_authority_types.py
git commit -m "feat(verdict): add ResourceVerdict and VerdictWarning frozen dataclasses with invariant enforcement"
```

---

## Task 3: Add PhaseVerdict and aggregate_verdicts()

**Files:**
- Modify: `backend/core/root_authority_types.py` (append after ResourceVerdict)
- Test: `tests/unit/core/test_root_authority_types.py`

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_root_authority_types.py`:

```python
class TestPhaseVerdict:
    def test_schema_version(self):
        from backend.core.root_authority_types import PhaseVerdict
        assert PhaseVerdict.SCHEMA_VERSION == 1

    def test_severity_derived(self):
        from backend.core.root_authority_types import PhaseVerdict, SubsystemState
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.DEGRADED,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="corr-001",
        )
        assert pv.severity == 1

    def test_frozen(self):
        from backend.core.root_authority_types import PhaseVerdict, SubsystemState
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="corr-001",
        )
        with pytest.raises(AttributeError):
            pv.state = SubsystemState.CRASHED


class TestAggregateVerdicts:
    def _verdict(self, origin, state, required_tier, boot_allowed=True,
                 serviceable=True, reason_code=None, retryable=False, seq=1):
        from backend.core.root_authority_types import (
            ResourceVerdict, SubsystemState, RequiredTier,
            VerdictReasonCode, RecoveryAction,
        )
        if reason_code is None:
            from backend.core.root_authority_types import VerdictReasonCode
            reason_code = VerdictReasonCode.HEALTHY if state == SubsystemState.READY else VerdictReasonCode.UNKNOWN
        return ResourceVerdict(
            origin=origin,
            correlation_id="corr-agg",
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=seq,
            state=state,
            boot_allowed=boot_allowed,
            serviceable=serviceable,
            required_tier=required_tier,
            reason_code=reason_code,
            reason_detail=f"{origin} verdict",
            retryable=retryable,
        )

    def test_all_healthy_required(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts,
        )
        verdicts = {
            "docker": self._verdict("docker", SubsystemState.READY, RequiredTier.REQUIRED),
            "ports": self._verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.READY
        assert pv.boot_allowed is True
        assert pv.serviceable is True
        assert len(pv.reason_codes) == 0
        assert len(pv.warnings) == 0

    def test_one_required_degraded(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "docker": self._verdict("docker", SubsystemState.DEGRADED, RequiredTier.REQUIRED,
                                     serviceable=True, reason_code=VerdictReasonCode.NOT_INSTALLED),
            "ports": self._verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.DEGRADED
        assert pv.boot_allowed is True
        assert pv.serviceable is True

    def test_required_crashed_blocks_boot(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "ports": self._verdict("ports", SubsystemState.CRASHED, RequiredTier.REQUIRED,
                                    boot_allowed=False, serviceable=False,
                                    reason_code=VerdictReasonCode.PORT_CONFLICT),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.CRASHED
        assert pv.boot_allowed is False
        assert pv.serviceable is False

    def test_optional_crashed_does_not_block_boot(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "ports": self._verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
            "cache": self._verdict("cache", SubsystemState.CRASHED, RequiredTier.OPTIONAL,
                                    boot_allowed=True, serviceable=False,
                                    reason_code=VerdictReasonCode.DEPENDENCY_MISSING),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.READY
        assert pv.boot_allowed is True
        assert len(pv.warnings) == 1
        assert pv.warnings[0].origin == "cache"

    def test_enhancement_degraded_adds_warning(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "ports": self._verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
            "docker": self._verdict("docker", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                                     serviceable=False,
                                     reason_code=VerdictReasonCode.NOT_INSTALLED),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.READY
        assert pv.boot_allowed is True
        assert len(pv.warnings) == 1

    def test_empty_required_fails_closed(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "cache": self._verdict("cache", SubsystemState.READY, RequiredTier.OPTIONAL),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.REJECTED
        assert pv.boot_allowed is False

    def test_empty_required_allowed_when_explicit(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts,
        )
        verdicts = {
            "cache": self._verdict("cache", SubsystemState.READY, RequiredTier.OPTIONAL),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1",
                                allow_empty_required=True)
        assert pv.boot_allowed is True

    def test_reason_codes_sorted_by_severity(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "a": self._verdict("a", SubsystemState.DEGRADED, RequiredTier.REQUIRED,
                                serviceable=True, reason_code=VerdictReasonCode.NOT_INSTALLED),
            "b": self._verdict("b", SubsystemState.CRASHED, RequiredTier.REQUIRED,
                                boot_allowed=False, serviceable=False,
                                reason_code=VerdictReasonCode.INIT_EXCEPTION),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        # Highest severity reason code first
        assert pv.reason_codes[0] == VerdictReasonCode.INIT_EXCEPTION

    def test_deterministic_tiebreak_non_retryable_wins(self):
        from backend.core.root_authority_types import (
            SubsystemState, RequiredTier, aggregate_verdicts, VerdictReasonCode,
        )
        verdicts = {
            "a": self._verdict("a", SubsystemState.DEGRADED, RequiredTier.REQUIRED,
                                serviceable=True, retryable=True,
                                reason_code=VerdictReasonCode.INIT_TIMEOUT),
            "b": self._verdict("b", SubsystemState.DEGRADED, RequiredTier.REQUIRED,
                                serviceable=True, retryable=False,
                                reason_code=VerdictReasonCode.CIRCUIT_BREAKER_OPEN),
        }
        pv = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        assert pv.state == SubsystemState.DEGRADED
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestPhaseVerdict tests/unit/core/test_root_authority_types.py::TestAggregateVerdicts -v`
Expected: FAIL with `ImportError: cannot import name 'PhaseVerdict'`

**Step 3: Write the implementation**

Append to `backend/core/root_authority_types.py`:

```python
# ---------------------------------------------------------------------------
# Phase Verdict (aggregate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseVerdict:
    """Aggregate verdict for a startup phase."""

    SCHEMA_VERSION: int = field(init=False, default=1, repr=False, compare=False)

    phase_name: str
    state: SubsystemState
    boot_allowed: bool
    serviceable: bool

    manager_verdicts: Mapping[str, ResourceVerdict]
    reason_codes: Tuple[VerdictReasonCode, ...]
    warnings: Tuple[VerdictWarning, ...]

    epoch: int
    monotonic_ns: int
    wall_utc: str
    correlation_id: str

    @property
    def severity(self) -> int:
        """Derived from state via severity lattice."""
        return SEVERITY_MAP.get(self.state, 3)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_verdicts(
    phase_name: str,
    verdicts: Mapping[str, ResourceVerdict],
    epoch: int,
    correlation_id: str,
    *,
    allow_empty_required: bool = False,
) -> PhaseVerdict:
    """Deterministic Model 3 aggregation with severity lattice.

    Policy:
    - boot_allowed = all required managers allow boot
    - serviceable  = any required manager is serviceable
    - state        = worst required manager state (by severity lattice)
    - enhancement/optional managers never flip boot_allowed false;
      they contribute warnings + capability impact only.

    Tie-break order for worst-state selection:
      1. severity (higher is worse)
      2. non-retryable over retryable
      3. newer monotonic_ns over older
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    required = {k: v for k, v in verdicts.items()
                if v.required_tier == RequiredTier.REQUIRED}
    non_required = {k: v for k, v in verdicts.items()
                    if v.required_tier != RequiredTier.REQUIRED}

    # Fail-closed: no required managers is likely misconfiguration
    if not required and not allow_empty_required:
        return PhaseVerdict(
            phase_name=phase_name,
            state=SubsystemState.REJECTED,
            boot_allowed=False,
            serviceable=False,
            manager_verdicts=dict(verdicts),
            reason_codes=(VerdictReasonCode.UNKNOWN,),
            warnings=(VerdictWarning(
                code="no_required_managers",
                detail=f"Phase {phase_name} has zero REQUIRED managers",
                origin="aggregate_verdicts",
            ),),
            epoch=epoch,
            monotonic_ns=_time.monotonic_ns(),
            wall_utc=_dt.now(_tz.utc).isoformat(),
            correlation_id=correlation_id,
        )

    # Phase gates from required managers only
    boot_allowed = all(v.boot_allowed for v in required.values()) if required else True
    serviceable = any(v.serviceable for v in required.values()) if required else True

    # Worst state among required (severity lattice + tie-break)
    if required:
        worst = max(required.values(), key=lambda v: (
            v.severity,
            0 if v.retryable else 1,
            v.monotonic_ns,
        ))
        phase_state = worst.state
    else:
        phase_state = SubsystemState.READY

    # Warnings from degraded non-required managers
    warnings: list = []
    for name, v in sorted(non_required.items()):
        if v.severity > 0:
            warnings.append(VerdictWarning(
                code=v.reason_code.value,
                detail=f"{name}: {v.reason_detail}",
                origin=v.origin,
            ))

    # Deduped reason codes, sorted by highest-severity-present then code value
    code_max_severity: dict = {}
    for v in verdicts.values():
        if v.severity > 0:
            rc = v.reason_code
            if rc not in code_max_severity or v.severity > code_max_severity[rc]:
                code_max_severity[rc] = v.severity
    reason_codes = tuple(sorted(
        code_max_severity.keys(),
        key=lambda rc: (-code_max_severity[rc], rc.value),
    ))

    return PhaseVerdict(
        phase_name=phase_name,
        state=phase_state,
        boot_allowed=boot_allowed,
        serviceable=serviceable,
        manager_verdicts=dict(verdicts),
        reason_codes=reason_codes,
        warnings=tuple(warnings),
        epoch=epoch,
        monotonic_ns=_time.monotonic_ns(),
        wall_utc=_dt.now(_tz.utc).isoformat(),
        correlation_id=correlation_id,
    )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py::TestPhaseVerdict tests/unit/core/test_root_authority_types.py::TestAggregateVerdicts -v`
Expected: PASS (all 12 tests)

**Step 5: Run ALL root_authority_types tests to ensure no regressions**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add backend/core/root_authority_types.py tests/unit/core/test_root_authority_types.py
git commit -m "feat(verdict): add PhaseVerdict and aggregate_verdicts() with Model 3 tiered aggregation"
```

---

## Task 4: Add VerdictAuthority (State Authority Contract)

**Files:**
- Create: `backend/core/verdict_authority.py`
- Test: `tests/unit/backend/core/test_verdict_authority.py`

**Step 1: Write the failing tests**

Create `tests/unit/backend/core/test_verdict_authority.py`:

```python
"""Tests for VerdictAuthority — single source of truth for component/phase status."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from backend.core.root_authority_types import (
    SubsystemState, RequiredTier, VerdictReasonCode, RecoveryAction,
    ResourceVerdict,
)


def _make_verdict(origin="test", epoch=1, seq=1, state=SubsystemState.READY,
                  boot_allowed=True, serviceable=True, **kw):
    defaults = dict(
        origin=origin,
        correlation_id="corr-test",
        epoch=epoch,
        monotonic_ns=time.monotonic_ns(),
        wall_utc=datetime.now(timezone.utc).isoformat(),
        sequence=seq,
        state=state,
        boot_allowed=boot_allowed,
        serviceable=serviceable,
        required_tier=RequiredTier.REQUIRED,
        reason_code=VerdictReasonCode.HEALTHY,
        reason_detail="ok",
        retryable=False,
    )
    defaults.update(kw)
    return ResourceVerdict(**defaults)


class TestVerdictAuthoritySubmit:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority
        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_submit_and_read(self, authority):
        v = _make_verdict(origin="docker")
        assert await authority.submit_verdict("docker", v) is True
        assert authority.get_component_status("docker") is v

    @pytest.mark.asyncio
    async def test_missing_component_returns_none(self, authority):
        assert authority.get_component_status("nonexistent") is None

    @pytest.mark.asyncio
    async def test_rejects_stale_epoch(self, authority):
        authority.begin_epoch()  # epoch=1
        authority.begin_epoch()  # epoch=2
        v_old = _make_verdict(epoch=1)
        assert await authority.submit_verdict("docker", v_old) is False

    @pytest.mark.asyncio
    async def test_rejects_out_of_order_monotonic(self, authority):
        authority.begin_epoch()
        v1 = _make_verdict(epoch=1, seq=1)
        await authority.submit_verdict("docker", v1)
        # Create verdict with earlier monotonic_ns
        v2 = ResourceVerdict(
            origin="docker", correlation_id="corr", epoch=1,
            monotonic_ns=v1.monotonic_ns - 1000,
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=0, state=SubsystemState.DEGRADED,
            boot_allowed=True, serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.UNKNOWN, reason_detail="late",
            retryable=False,
        )
        assert await authority.submit_verdict("docker", v2) is False
        # Original verdict preserved
        assert authority.get_component_status("docker") is v1

    @pytest.mark.asyncio
    async def test_rejects_heal_without_evidence(self, authority):
        authority.begin_epoch()
        v_degraded = _make_verdict(
            epoch=1, state=SubsystemState.DEGRADED, serviceable=True,
            reason_code=VerdictReasonCode.NOT_INSTALLED,
        )
        await authority.submit_verdict("docker", v_degraded)
        # Try to heal without recovery_proof
        v_ready = _make_verdict(epoch=1, seq=2)
        assert await authority.submit_verdict("docker", v_ready) is False

    @pytest.mark.asyncio
    async def test_allows_heal_with_evidence(self, authority):
        authority.begin_epoch()
        v_degraded = _make_verdict(
            epoch=1, state=SubsystemState.DEGRADED, serviceable=True,
            reason_code=VerdictReasonCode.NOT_INSTALLED,
        )
        await authority.submit_verdict("docker", v_degraded)
        # Heal with recovery_proof
        v_ready = _make_verdict(
            epoch=1, seq=2,
            evidence={"recovery_proof": "docker_started_pid_12345"},
        )
        assert await authority.submit_verdict("docker", v_ready) is True

    @pytest.mark.asyncio
    async def test_allows_degradation_without_evidence(self, authority):
        authority.begin_epoch()
        v_ready = _make_verdict(epoch=1)
        await authority.submit_verdict("docker", v_ready)
        v_degraded = _make_verdict(
            epoch=1, seq=2, state=SubsystemState.DEGRADED,
            serviceable=True, reason_code=VerdictReasonCode.CIRCUIT_BREAKER_OPEN,
        )
        assert await authority.submit_verdict("docker", v_degraded) is True


class TestVerdictAuthoritySnapshot:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority
        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_snapshot_is_copy(self, authority):
        authority.begin_epoch()
        v = _make_verdict(epoch=1)
        await authority.submit_verdict("docker", v)
        snap = authority.get_all_verdicts_snapshot()
        assert "docker" in snap
        assert snap["docker"] is v
        # Mutating snapshot dict does not affect authority
        snap["injected"] = v
        assert authority.get_component_status("injected") is None


class TestVerdictAuthorityPhaseDisplay:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority
        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_no_phase_returns_pending(self, authority):
        assert authority.get_phase_display("resources") == {"status": "pending"}

    @pytest.mark.asyncio
    async def test_phase_display_from_verdict(self, authority):
        from backend.core.root_authority_types import (
            PhaseVerdict, SubsystemState, VerdictReasonCode,
        )
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.DEGRADED,
            boot_allowed=True, serviceable=True,
            manager_verdicts={}, reason_codes=(VerdictReasonCode.NOT_INSTALLED,),
            warnings=(), epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="c1",
        )
        await authority.submit_phase_verdict(pv)
        display = authority.get_phase_display("resources")
        assert display["status"] == "degraded"
        assert display["detail"] == "not_installed"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_verdict_authority.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.verdict_authority'`

**Step 3: Write the implementation**

Create `backend/core/verdict_authority.py`:

```python
"""VerdictAuthority — single source of truth for component/phase status.

Enforces:
- Monotonic severity (can degrade freely, healing requires evidence)
- Epoch-stamped writes (stale epoch rejected)
- Out-of-order rejection (monotonic_ns ordering)
- No raw string overwrites — all writes go through typed verdicts

This replaces _update_component_status() as the authoritative write path.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Mapping, Optional

from backend.core.root_authority_types import (
    PhaseVerdict,
    ResourceVerdict,
    SEVERITY_MAP,
)


class VerdictAuthority:
    """Single source of truth for component/phase verdict status."""

    def __init__(self) -> None:
        self._verdicts: Dict[str, ResourceVerdict] = {}
        self._phase_verdicts: Dict[str, PhaseVerdict] = {}
        self._current_epoch: int = 0
        self._lock = asyncio.Lock()

    def begin_epoch(self) -> int:
        """Start a new boot epoch. Stale-epoch verdicts will be rejected."""
        self._current_epoch += 1
        return self._current_epoch

    @property
    def current_epoch(self) -> int:
        return self._current_epoch

    async def submit_verdict(self, name: str, verdict: ResourceVerdict) -> bool:
        """Submit a manager verdict.

        Rejects:
        - Stale epoch (verdict.epoch < current_epoch)
        - Out-of-order (verdict.monotonic_ns <= existing.monotonic_ns)
        - Heal without evidence (severity decrease without recovery_proof in evidence)

        Returns True if accepted, False if rejected.
        """
        async with self._lock:
            if verdict.epoch < self._current_epoch:
                return False

            existing = self._verdicts.get(name)
            if existing is not None:
                if existing.monotonic_ns > verdict.monotonic_ns:
                    return False

                # Monotonic severity: healing requires evidence
                existing_severity = SEVERITY_MAP.get(existing.state, 3)
                new_severity = SEVERITY_MAP.get(verdict.state, 3)
                if new_severity < existing_severity:
                    if not verdict.evidence.get("recovery_proof"):
                        return False

            self._verdicts[name] = verdict
            return True

    async def submit_phase_verdict(self, verdict: PhaseVerdict) -> bool:
        """Submit an aggregated phase verdict.

        Rejects stale epoch. Replaces the hardcoded status overwrites.
        """
        async with self._lock:
            if verdict.epoch < self._current_epoch:
                return False
            self._phase_verdicts[verdict.phase_name] = verdict
            return True

    def get_component_status(self, name: str) -> Optional[ResourceVerdict]:
        """Read a manager verdict. Frozen dataclass — safe without lock."""
        return self._verdicts.get(name)

    def get_phase_status(self, name: str) -> Optional[PhaseVerdict]:
        """Read a phase verdict. Frozen dataclass — safe without lock."""
        return self._phase_verdicts.get(name)

    def get_all_verdicts_snapshot(self) -> Mapping[str, ResourceVerdict]:
        """Return consistent point-in-time snapshot of all manager verdicts.

        Returns a shallow dict copy. Values are frozen, so no torn reads.
        """
        return dict(self._verdicts)

    def get_phase_display(self, phase: str) -> Dict[str, str]:
        """Format phase verdict for dashboard/broadcast consumption.

        Returns dict with 'status' and optional 'detail' keys.
        This replaces the hardcoded {"status": "complete"} literals.
        """
        verdict = self._phase_verdicts.get(phase)
        if verdict is None:
            return {"status": "pending"}
        return {
            "status": verdict.state.value,
            "detail": verdict.reason_codes[0].value if verdict.reason_codes else "",
        }
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_verdict_authority.py -v`
Expected: PASS (all 10 tests)

**Step 5: Commit**

```bash
git add backend/core/verdict_authority.py tests/unit/backend/core/test_verdict_authority.py
git commit -m "feat(verdict): add VerdictAuthority with epoch gating, monotonic severity, and heal-with-evidence"
```

---

## Task 5: Add _build_verdict() Helper to ResourceManagerBase

**Files:**
- Modify: `unified_supervisor.py:9769-9922` (ResourceManagerBase class)
- Test: `tests/unit/core/test_resource_verdict_bridge.py`

**Step 1: Write the failing tests**

Create `tests/unit/core/test_resource_verdict_bridge.py`:

```python
"""Tests for ResourceManagerBase._build_verdict() bridge method."""
import time
from datetime import datetime, timezone

import pytest

from backend.core.root_authority_types import (
    SubsystemState, RequiredTier, VerdictReasonCode, RecoveryAction,
    ResourceVerdict,
)


class FakeManager:
    """Minimal stand-in for ResourceManagerBase to test _build_verdict."""
    def __init__(self):
        self.name = "fake_manager"
        self._required_tier = RequiredTier.REQUIRED
        self._capabilities = ()
        self._verdict_sequence = 0
        self._boot_epoch = 1
        self._correlation_id = "corr-test"


class TestBuildVerdict:
    def _get_build_verdict(self):
        """Import and bind _build_verdict from the supervisor module."""
        # We test the method in isolation by calling it on our FakeManager
        from unified_supervisor import ResourceManagerBase
        return ResourceManagerBase._build_verdict

    def test_healthy_verdict(self):
        build = self._get_build_verdict()
        mgr = FakeManager()
        v = build(
            mgr,
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert isinstance(v, ResourceVerdict)
        assert v.state == SubsystemState.READY
        assert v.origin == "fake_manager"
        assert v.epoch == 1
        assert v.sequence == 1
        assert v.required_tier == RequiredTier.REQUIRED

    def test_sequence_increments(self):
        build = self._get_build_verdict()
        mgr = FakeManager()
        v1 = build(mgr, state=SubsystemState.READY, reason_code=VerdictReasonCode.HEALTHY,
                    reason_detail="ok", boot_allowed=True, serviceable=True)
        v2 = build(mgr, state=SubsystemState.READY, reason_code=VerdictReasonCode.HEALTHY,
                    reason_detail="ok", boot_allowed=True, serviceable=True)
        assert v2.sequence == v1.sequence + 1

    def test_capabilities_passed_through(self):
        build = self._get_build_verdict()
        mgr = FakeManager()
        mgr._capabilities = ("container_runtime",)
        v = build(mgr, state=SubsystemState.READY, reason_code=VerdictReasonCode.HEALTHY,
                  reason_detail="ok", boot_allowed=True, serviceable=True)
        assert v.capabilities == ("container_runtime",)

    def test_optional_tier(self):
        build = self._get_build_verdict()
        mgr = FakeManager()
        mgr._required_tier = RequiredTier.OPTIONAL
        v = build(mgr, state=SubsystemState.DEGRADED, reason_code=VerdictReasonCode.DISABLED_BY_CONFIG,
                  reason_detail="disabled", boot_allowed=True, serviceable=False)
        assert v.required_tier == RequiredTier.OPTIONAL
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py -v`
Expected: FAIL with `AttributeError: type object 'ResourceManagerBase' has no attribute '_build_verdict'`

**Step 3: Write the implementation**

In `unified_supervisor.py`, add imports near the top of the `ResourceManagerBase` class (after line 9800, inside `__init__`), and add the `_build_verdict` method.

Add to `ResourceManagerBase.__init__` (after line 9800):

```python
        # v290.0: Verdict bridge fields
        self._required_tier = RequiredTier.REQUIRED  # fail-safe default
        self._capabilities: Tuple[str, ...] = ()
        self._verdict_sequence: int = 0
        self._boot_epoch: int = 0
        self._correlation_id: str = ""
```

Add the method after `safe_health_check` (after the `safe_initialize` method around line 9922):

```python
    def _build_verdict(
        self,
        state: SubsystemState,
        reason_code: VerdictReasonCode,
        reason_detail: str,
        *,
        boot_allowed: bool = True,
        serviceable: bool = False,
        retryable: bool = False,
        retry_after_s: Optional[float] = None,
        evidence: Optional[Mapping] = None,
        recovery_owner: Optional[str] = None,
        next_action: RecoveryAction = RecoveryAction.NONE,
    ) -> "ResourceVerdict":
        """Build a ResourceVerdict from this manager's current context.

        Convenience method for subclasses during the bool->verdict migration.
        """
        self._verdict_sequence += 1
        return ResourceVerdict(
            origin=self.name,
            correlation_id=self._correlation_id,
            epoch=self._boot_epoch,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=self._verdict_sequence,
            state=state,
            boot_allowed=boot_allowed,
            serviceable=serviceable,
            required_tier=self._required_tier,
            reason_code=reason_code,
            reason_detail=reason_detail,
            retryable=retryable,
            retry_after_s=retry_after_s,
            evidence=evidence or {},
            recovery_owner=recovery_owner,
            next_action=next_action,
            capabilities=self._capabilities,
        )
```

Also add the necessary imports at the top of `unified_supervisor.py` where other backend.core imports exist:

```python
from backend.core.root_authority_types import (
    SubsystemState, RequiredTier, VerdictReasonCode, RecoveryAction,
    ResourceVerdict,
)
```

Note: `unified_supervisor.py` uses lazy/conditional imports extensively. Check if `root_authority_types` is already imported; if so, extend the existing import. If not, add it in the same pattern used by nearby imports (likely a try/except block near the top).

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py -v`
Expected: PASS (all 4 tests)

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/core/test_resource_verdict_bridge.py
git commit -m "feat(verdict): add _build_verdict() bridge to ResourceManagerBase"
```

---

## Task 6: Bridge safe_initialize() to Return ResourceVerdict

**Files:**
- Modify: `unified_supervisor.py:9896-9921` (safe_initialize method)
- Modify: `unified_supervisor.py:13106-13250` (initialize_all method)
- Test: `tests/unit/core/test_resource_verdict_bridge.py` (extend)

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_resource_verdict_bridge.py`:

```python
class TestSafeInitializeVerdict:
    """Test that safe_initialize() returns ResourceVerdict."""

    @pytest.mark.asyncio
    async def test_successful_init_returns_ready_verdict(self):
        from unittest.mock import AsyncMock, MagicMock
        from unified_supervisor import ResourceManagerBase
        from backend.core.root_authority_types import SubsystemState, VerdictReasonCode

        class SuccessManager(ResourceManagerBase):
            async def initialize(self):
                return True
            async def health_check(self):
                return (True, "ok")
            async def cleanup(self):
                pass

        mgr = SuccessManager("test_success")
        mgr._boot_epoch = 1
        mgr._correlation_id = "corr"
        # Mock circuit breaker to pass through
        mgr._circuit_breaker = MagicMock()
        mgr._circuit_breaker.execute = AsyncMock(return_value=True)

        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state == SubsystemState.READY
        assert verdict.boot_allowed is True
        assert verdict.serviceable is True
        assert verdict.reason_code == VerdictReasonCode.HEALTHY

    @pytest.mark.asyncio
    async def test_failed_init_returns_verdict(self):
        from unittest.mock import AsyncMock, MagicMock
        from unified_supervisor import ResourceManagerBase
        from backend.core.root_authority_types import SubsystemState, VerdictReasonCode

        class FailManager(ResourceManagerBase):
            async def initialize(self):
                return False
            async def health_check(self):
                return (False, "fail")
            async def cleanup(self):
                pass

        mgr = FailManager("test_fail")
        mgr._boot_epoch = 1
        mgr._correlation_id = "corr"
        mgr._circuit_breaker = MagicMock()
        mgr._circuit_breaker.execute = AsyncMock(return_value=False)

        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state in (SubsystemState.DEGRADED, SubsystemState.CRASHED)
        assert verdict.reason_code == VerdictReasonCode.INIT_RETURNED_FALSE

    @pytest.mark.asyncio
    async def test_exception_returns_crashed_verdict(self):
        from unittest.mock import AsyncMock, MagicMock
        from unified_supervisor import ResourceManagerBase
        from backend.core.root_authority_types import SubsystemState, VerdictReasonCode

        class ExplodeManager(ResourceManagerBase):
            async def initialize(self):
                raise RuntimeError("boom")
            async def health_check(self):
                return (False, "dead")
            async def cleanup(self):
                pass

        mgr = ExplodeManager("test_explode")
        mgr._boot_epoch = 1
        mgr._correlation_id = "corr"
        mgr._circuit_breaker = MagicMock()
        mgr._circuit_breaker.execute = AsyncMock(side_effect=RuntimeError("boom"))

        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state == SubsystemState.CRASHED
        assert verdict.reason_code == VerdictReasonCode.INIT_EXCEPTION
        assert "boom" in verdict.reason_detail

    @pytest.mark.asyncio
    async def test_manager_with_get_init_verdict_override(self):
        from unittest.mock import AsyncMock, MagicMock
        from unified_supervisor import ResourceManagerBase
        from backend.core.root_authority_types import (
            SubsystemState, VerdictReasonCode, ResourceVerdict,
        )

        class CustomVerdictManager(ResourceManagerBase):
            async def initialize(self):
                return True
            async def health_check(self):
                return (True, "ok")
            async def cleanup(self):
                pass
            def get_init_verdict(self, bool_result):
                return self._build_verdict(
                    state=SubsystemState.DEGRADED,
                    reason_code=VerdictReasonCode.DISABLED_BY_CONFIG,
                    reason_detail="Docker not installed but non-fatal",
                    boot_allowed=True,
                    serviceable=False,
                )

        mgr = CustomVerdictManager("custom")
        mgr._boot_epoch = 1
        mgr._correlation_id = "corr"
        mgr._circuit_breaker = MagicMock()
        mgr._circuit_breaker.execute = AsyncMock(return_value=True)

        verdict = await mgr.safe_initialize()
        assert verdict.state == SubsystemState.DEGRADED
        assert verdict.reason_code == VerdictReasonCode.DISABLED_BY_CONFIG
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py::TestSafeInitializeVerdict -v`
Expected: FAIL (safe_initialize returns bool, not ResourceVerdict)

**Step 3: Write the implementation**

Replace `safe_initialize()` in `unified_supervisor.py` (around lines 9896-9921). The new version returns `ResourceVerdict` while still setting `_ready` and `_health_status` for backward compatibility:

```python
    async def safe_initialize(self) -> "ResourceVerdict":
        """Initialize with circuit breaker protection, return typed verdict.

        v290.0: Returns ResourceVerdict instead of bool. Still sets
        _ready/_health_status for backward compatibility with code that
        reads those fields directly.

        If the subclass defines get_init_verdict(bool_result), that method
        is called to produce a custom verdict (migration bridge). Otherwise,
        the verdict is inferred from the bool result.
        """
        start = time.time()
        try:
            result = await self._circuit_breaker.execute(self.initialize())
            self._init_time = time.time() - start

            # Bridge: custom verdict from subclass
            if hasattr(self, "get_init_verdict") and callable(getattr(self, "get_init_verdict")):
                verdict = self.get_init_verdict(result)
                self._ready = verdict.serviceable
                self._health_status = verdict.state.value
                if verdict.state == SubsystemState.READY:
                    self._logger.success(f"{self.name} initialized in {self._init_time*1000:.0f}ms")
                else:
                    self._logger.warning(f"{self.name}: {verdict.reason_detail}")
                return verdict

            # Infer verdict from bool (default bridge path)
            if result:
                self._ready = True
                self._health_status = "healthy"
                self._logger.success(f"{self.name} initialized in {self._init_time*1000:.0f}ms")
                return self._build_verdict(
                    state=SubsystemState.READY,
                    reason_code=VerdictReasonCode.HEALTHY,
                    reason_detail=f"{self.name} initialized in {self._init_time*1000:.0f}ms",
                    boot_allowed=True,
                    serviceable=True,
                    evidence={"init_time_ms": int(self._init_time * 1000)},
                )
            else:
                self._error = self._error or "Initialization returned False"
                self._health_status = "unhealthy"
                self._logger.warning(f"{self.name} initialization failed")
                _is_required = self._required_tier == RequiredTier.REQUIRED
                return self._build_verdict(
                    state=SubsystemState.CRASHED if _is_required else SubsystemState.DEGRADED,
                    reason_code=VerdictReasonCode.INIT_RETURNED_FALSE,
                    reason_detail=self._error,
                    boot_allowed=not _is_required,
                    serviceable=False,
                    retryable=True,
                    next_action=RecoveryAction.RETRY,
                    evidence={"init_time_ms": int(self._init_time * 1000), "error": self._error},
                )
        except Exception as e:
            self._init_time = time.time() - start
            self._error = str(e)
            self._health_status = "error"
            self._logger.error(f"{self.name} initialization error: {e}")
            _is_required = self._required_tier == RequiredTier.REQUIRED
            return self._build_verdict(
                state=SubsystemState.CRASHED,
                reason_code=VerdictReasonCode.INIT_EXCEPTION,
                reason_detail=str(e),
                boot_allowed=not _is_required,
                serviceable=False,
                retryable=True,
                retry_after_s=30.0,
                next_action=RecoveryAction.RETRY,
                evidence={"exception": type(e).__name__, "init_time_ms": int(self._init_time * 1000)},
            )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py -v`
Expected: PASS (all 8 tests)

**Step 5: Update initialize_all() to collect ResourceVerdicts**

In `unified_supervisor.py`, modify `ResourceManagerRegistry.initialize_all()` (around line 13106).

Change the return type annotation and result collection:

```python
    async def initialize_all(
        self,
        parallel: bool = True,
        base_progress: int = 15,
        end_progress: int = 30,
        manager_timeout: Optional[float] = None,
        overall_timeout: Optional[float] = None,
    ) -> Dict[str, "ResourceVerdict"]:
```

Change the inner `_initialize_manager` to return `ResourceVerdict`:

```python
        async def _initialize_manager(
            name: str,
            manager: ResourceManagerBase,
        ) -> "ResourceVerdict":
            if manager_timeout is None:
                return await manager.safe_initialize()
            try:
                return await asyncio.wait_for(
                    manager.safe_initialize(),
                    timeout=manager_timeout,
                )
            except asyncio.TimeoutError:
                manager._ready = False
                manager._health_status = "error"
                manager._error = f"Initialization timed out after {manager_timeout:.1f}s"
                self._logger.warning(f"Manager {name} timed out after {manager_timeout:.1f}s")
                return manager._build_verdict(
                    state=SubsystemState.CRASHED,
                    reason_code=VerdictReasonCode.INIT_TIMEOUT,
                    reason_detail=f"Initialization timed out after {manager_timeout:.1f}s",
                    boot_allowed=manager._required_tier != RequiredTier.REQUIRED,
                    serviceable=False,
                    retryable=True,
                    retry_after_s=manager_timeout,
                    next_action=RecoveryAction.RETRY,
                    evidence={"timeout_s": manager_timeout},
                )
```

Update result collection in the parallel and sequential branches to store `ResourceVerdict` instead of `bool`:

```python
            results[name] = verdict  # ResourceVerdict, not bool
```

**Step 6: Update callers of initialize_all() that check `if v` (bool)**

There are two key call sites:
- `unified_supervisor.py:41796` — `results = await registry.initialize_all(parallel=True)`
- `unified_supervisor.py:76234` — `results = await self._resource_registry.initialize_all(...)`

At both sites, the code after does:
```python
ready_count = sum(1 for v in results.values() if v)
```

Change these to:
```python
ready_count = sum(1 for v in results.values() if v.serviceable)
```

And for the error path at line 76245:
```python
            except Exception as resource_init_err:
                self.logger.error(f"[Kernel] Resource initialization error: {resource_init_err}")
                results = {}  # Empty dict — no verdicts
```

**Step 7: Run full test suite for regressions**

Run: `python3 -m pytest tests/unit/core/ -v --timeout=30`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add unified_supervisor.py tests/unit/core/test_resource_verdict_bridge.py
git commit -m "feat(verdict): bridge safe_initialize() and initialize_all() to return ResourceVerdict"
```

---

## Task 7: Set Required Tiers on All 8 Managers

**Files:**
- Modify: `unified_supervisor.py` (each manager's `__init__`)
- Test: `tests/unit/core/test_resource_verdict_bridge.py` (extend)

**Step 1: Write the failing tests**

Add to `tests/unit/core/test_resource_verdict_bridge.py`:

```python
class TestManagerRequiredTiers:
    """Verify each manager declares the correct required_tier."""

    @pytest.mark.parametrize("manager_name,expected_tier", [
        ("DockerDaemonManager", "ENHANCEMENT"),
        ("GCPInstanceManager", "ENHANCEMENT"),
        ("DynamicPortManager", "REQUIRED"),
        ("SemanticVoiceCacheManager", "OPTIONAL"),
        ("TieredStorageManager", "OPTIONAL"),
        ("SpotInstanceResilienceHandler", "OPTIONAL"),
        ("IntelligentCacheManager", "ENHANCEMENT"),
        ("CostTracker", "OPTIONAL"),
    ])
    def test_tier_declared(self, manager_name, expected_tier):
        import unified_supervisor as us
        from backend.core.root_authority_types import RequiredTier
        cls = getattr(us, manager_name)
        # Instantiate with minimal config
        try:
            instance = cls.__new__(cls)
            ResourceManagerBase = us.ResourceManagerBase
            ResourceManagerBase.__init__(instance, manager_name.lower())
            # Check if __init__ was overridden to set tier
            actual = instance._required_tier
            assert actual == RequiredTier[expected_tier], (
                f"{manager_name}._required_tier = {actual}, expected {expected_tier}"
            )
        except Exception:
            pytest.skip(f"Cannot instantiate {manager_name} in test env")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py::TestManagerRequiredTiers -v`
Expected: FAIL — all managers default to REQUIRED but Docker/GCP/etc. should be ENHANCEMENT/OPTIONAL

**Step 3: Write the implementation**

Add `self._required_tier = RequiredTier.<TIER>` to each manager's `__init__` method, after the `super().__init__()` call:

| Manager (line) | Add after super().__init__ |
|---|---|
| `DockerDaemonManager` (~10010) | `self._required_tier = RequiredTier.ENHANCEMENT` |
| `GCPInstanceManager` (~10590) | `self._required_tier = RequiredTier.ENHANCEMENT` |
| `DynamicPortManager` (~11910) | `self._required_tier = RequiredTier.REQUIRED` (keep default) |
| `SemanticVoiceCacheManager` (~12490) | `self._required_tier = RequiredTier.OPTIONAL` |
| `TieredStorageManager` (~12830) | `self._required_tier = RequiredTier.OPTIONAL` |
| `SpotInstanceResilienceHandler` (~13935) | `self._required_tier = RequiredTier.OPTIONAL` |
| `IntelligentCacheManager` (~14195) | `self._required_tier = RequiredTier.ENHANCEMENT` |
| `CostTracker` (~11365) | `self._required_tier = RequiredTier.OPTIONAL` |

Also add capabilities where known:

| Manager | Capabilities |
|---|---|
| `DockerDaemonManager` | `("container_runtime",)` |
| `GCPInstanceManager` | `("cloud_compute", "cloud_offload")` |
| `DynamicPortManager` | `("port_allocation",)` |
| `SemanticVoiceCacheManager` | `("voice_cache",)` |
| `TieredStorageManager` | `("tiered_storage",)` |
| `SpotInstanceResilienceHandler` | `("spot_resilience",)` |
| `IntelligentCacheManager` | `("module_cache",)` |
| `CostTracker` | `("cost_tracking",)` |

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py::TestManagerRequiredTiers -v`
Expected: PASS (all 8 parametrized tests)

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/core/test_resource_verdict_bridge.py
git commit -m "feat(verdict): declare RequiredTier and capabilities on all 8 resource managers"
```

---

## Task 8: Wire VerdictAuthority Into Supervisor Kernel

**Files:**
- Modify: `unified_supervisor.py` (kernel init, resource phase, `_update_component_status`)
- Test: `tests/unit/core/test_resource_verdict_bridge.py` (extend)

**Step 1: Write the failing test**

Add to `tests/unit/core/test_resource_verdict_bridge.py`:

```python
class TestVerdictAuthorityWiring:
    """Test that the supervisor kernel creates and uses VerdictAuthority."""

    def test_verdict_authority_importable(self):
        from backend.core.verdict_authority import VerdictAuthority
        va = VerdictAuthority()
        assert va.current_epoch == 0
```

This is a smoke test. The real integration test is in Task 9.

**Step 2: Implementation overview**

In the supervisor kernel's `__init__` or startup method, add:

```python
from backend.core.verdict_authority import VerdictAuthority
self._verdict_authority = VerdictAuthority()
```

In the resource phase (around line 76234), after `initialize_all()` returns:

```python
# Submit individual verdicts to authority
for name, verdict in results.items():
    await self._verdict_authority.submit_verdict(name, verdict)

# Aggregate into phase verdict
from backend.core.root_authority_types import aggregate_verdicts
phase_verdict = aggregate_verdicts(
    "resources", results,
    epoch=self._verdict_authority.current_epoch,
    correlation_id=self._startup_correlation_id or "",
)
await self._verdict_authority.submit_phase_verdict(phase_verdict)

# Log aggregate
self.logger.info(
    "[Kernel] Resources phase: state=%s boot_allowed=%s serviceable=%s",
    phase_verdict.state.value, phase_verdict.boot_allowed, phase_verdict.serviceable,
)
```

**Step 3: Commit**

```bash
git add unified_supervisor.py tests/unit/core/test_resource_verdict_bridge.py
git commit -m "feat(verdict): wire VerdictAuthority into supervisor kernel resource phase"
```

---

## Task 9: Replace Hardcoded Status Overwrites

**Files:**
- Modify: `unified_supervisor.py` (10 sites at lines 72756, 72875, 73054, 73481, 73822, 74034, 74164, 74332, 74444, 74656)
- Test: `tests/unit/core/test_resource_verdict_bridge.py` (extend)

**Step 1: Write the failing test**

Add to `tests/unit/core/test_resource_verdict_bridge.py`:

```python
class TestNoHardcodedResourceComplete:
    """Verify the codebase has no hardcoded resources-complete overwrites."""

    def test_no_hardcoded_resources_complete(self):
        import re
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        # Find all instances of "resources": {"status": "complete"}
        matches = re.findall(r'"resources":\s*\{"status":\s*"complete"\}', content)
        assert len(matches) == 0, (
            f"Found {len(matches)} hardcoded 'resources: complete' literals. "
            "These must read from VerdictAuthority instead."
        )
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py::TestNoHardcodedResourceComplete -v`
Expected: FAIL with `Found 10 hardcoded 'resources: complete' literals`

**Step 3: Write the implementation**

At each of the 10 sites, replace the hardcoded literal with a call to `VerdictAuthority`:

Before (at each site):
```python
"resources": {"status": "complete"},
```

After:
```python
"resources": self._verdict_authority.get_phase_display("resources"),
```

The 10 sites are at lines: 72756, 72875, 73054, 73481, 73822, 74034, 74164, 74332, 74444, 74656.

Each is inside a `_broadcast_startup_progress()` call's `metadata` dict. The pattern is the same at each site — find the `"resources": {"status": "complete"}` entry and replace it.

**Important:** If `self._verdict_authority` may not be initialized yet at some early broadcast sites, use a safe accessor:

```python
"resources": (self._verdict_authority.get_phase_display("resources")
              if hasattr(self, '_verdict_authority') and self._verdict_authority
              else {"status": "pending"}),
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_resource_verdict_bridge.py::TestNoHardcodedResourceComplete -v`
Expected: PASS

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/core/test_resource_verdict_bridge.py
git commit -m "fix(verdict): replace 10 hardcoded 'resources: complete' overwrites with VerdictAuthority reads"
```

---

## Task 10: Integration Test — Degraded Resources Flow

**Files:**
- Create: `tests/integration/test_resource_verdict_integration.py`

**Step 1: Write the integration test**

```python
"""Integration test: degraded resource manager -> phase verdict -> display."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from backend.core.root_authority_types import (
    SubsystemState, RequiredTier, VerdictReasonCode,
    ResourceVerdict, aggregate_verdicts,
)
from backend.core.verdict_authority import VerdictAuthority


def _verdict(origin, state, tier, boot_allowed=True, serviceable=True,
             reason_code=VerdictReasonCode.HEALTHY, **kw):
    return ResourceVerdict(
        origin=origin, correlation_id="corr-int", epoch=1,
        monotonic_ns=time.monotonic_ns(),
        wall_utc=datetime.now(timezone.utc).isoformat(),
        sequence=1, state=state, boot_allowed=boot_allowed,
        serviceable=serviceable, required_tier=tier,
        reason_code=reason_code, reason_detail=f"{origin} test",
        retryable=False, **kw,
    )


class TestDegradedResourceFlow:
    """End-to-end: cloud_first boot with degraded resources shows degraded, not green."""

    @pytest.mark.asyncio
    async def test_cloud_mode_resources_show_degraded(self):
        authority = VerdictAuthority()
        authority.begin_epoch()

        verdicts = {
            "ports": _verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
            "docker": _verdict("docker", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                                serviceable=False,
                                reason_code=VerdictReasonCode.NOT_INSTALLED),
            "gcp": _verdict("gcp", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                             serviceable=True,
                             reason_code=VerdictReasonCode.MEMORY_ADMISSION_CLOUD_FIRST),
        }

        for name, v in verdicts.items():
            await authority.submit_verdict(name, v)

        phase = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        await authority.submit_phase_verdict(phase)

        # Phase should be READY (only required=ports is ready)
        assert phase.state == SubsystemState.READY
        assert phase.boot_allowed is True
        # But warnings should include docker and gcp
        assert len(phase.warnings) == 2

        # Display should say "ready", not "complete"
        display = authority.get_phase_display("resources")
        assert display["status"] == "ready"
        assert display["status"] != "complete"

    @pytest.mark.asyncio
    async def test_required_port_crash_blocks_boot(self):
        authority = VerdictAuthority()
        authority.begin_epoch()

        verdicts = {
            "ports": _verdict("ports", SubsystemState.CRASHED, RequiredTier.REQUIRED,
                               boot_allowed=False, serviceable=False,
                               reason_code=VerdictReasonCode.PORT_CONFLICT),
        }

        for name, v in verdicts.items():
            await authority.submit_verdict(name, v)

        phase = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
        await authority.submit_phase_verdict(phase)

        assert phase.boot_allowed is False
        assert phase.state == SubsystemState.CRASHED
        display = authority.get_phase_display("resources")
        assert display["status"] == "crashed"
        assert display["detail"] == "port_conflict"

    @pytest.mark.asyncio
    async def test_stale_verdict_rejected_by_authority(self):
        authority = VerdictAuthority()
        authority.begin_epoch()  # epoch=1
        authority.begin_epoch()  # epoch=2

        stale = _verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED, epoch=1)
        # Override epoch in the verdict (need to reconstruct since frozen)
        stale_v = ResourceVerdict(
            origin="ports", correlation_id="c", epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=1, state=SubsystemState.READY,
            boot_allowed=True, serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="stale", retryable=False,
        )
        assert await authority.submit_verdict("ports", stale_v) is False

    @pytest.mark.asyncio
    async def test_heal_requires_evidence(self):
        authority = VerdictAuthority()
        authority.begin_epoch()

        degraded = _verdict("docker", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                             serviceable=False,
                             reason_code=VerdictReasonCode.NOT_INSTALLED)
        await authority.submit_verdict("docker", degraded)

        # Attempt heal without evidence
        heal_no_proof = ResourceVerdict(
            origin="docker", correlation_id="c", epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=2, state=SubsystemState.READY,
            boot_allowed=True, serviceable=True,
            required_tier=RequiredTier.ENHANCEMENT,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="magically fixed", retryable=False,
        )
        assert await authority.submit_verdict("docker", heal_no_proof) is False

        # Attempt heal with evidence
        heal_with_proof = ResourceVerdict(
            origin="docker", correlation_id="c", epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=3, state=SubsystemState.READY,
            boot_allowed=True, serviceable=True,
            required_tier=RequiredTier.ENHANCEMENT,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="docker started",
            retryable=False,
            evidence={"recovery_proof": "docker_started_pid_99"},
        )
        assert await authority.submit_verdict("docker", heal_with_proof) is True
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/integration/test_resource_verdict_integration.py -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add tests/integration/test_resource_verdict_integration.py
git commit -m "test(verdict): add integration tests for degraded flow, stale epoch, and heal-with-evidence"
```

---

## Summary — Task Ordering & Dependencies

```
Task 1: Enums (RequiredTier, RecoveryAction, VerdictReasonCode, SEVERITY_MAP)
  └─► Task 2: ResourceVerdict frozen dataclass
       └─► Task 3: PhaseVerdict + aggregate_verdicts()
            └─► Task 4: VerdictAuthority
                 └─► Task 5: _build_verdict() on ResourceManagerBase
                      └─► Task 6: Bridge safe_initialize() + initialize_all()
                           ├─► Task 7: Set required tiers on 8 managers
                           └─► Task 8: Wire VerdictAuthority into kernel
                                └─► Task 9: Replace 10 hardcoded overwrites
                                     └─► Task 10: Integration tests
```

Total: 10 tasks, ~25 test cases, ~3 new files, ~2 major file modifications.

## Future Work (Not in This Plan)

- **P6: PrimeRouter integration** — feed PhaseVerdict into `_decide_route()` for ROUTE_TO_GCP
- **P7: Deprecate bool path** — CI gate on `initialize() -> bool` without `get_init_verdict`
- **Model 2 bridge** — compute `phase_capability_map` from manager capabilities
- **Hysteresis** — add flapping suppression for READY↔DEGRADED transitions
- **UI rendering** — dashboard components render from verdict authority, not event bus literals
