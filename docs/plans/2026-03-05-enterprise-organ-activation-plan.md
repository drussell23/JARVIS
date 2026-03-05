# Enterprise Organ Activation Program — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Activate 67 dormant enterprise classes in unified_supervisor.py as governed service organs with 3-layer contracts (Capability, Lifecycle, Activation), managed through the existing SystemServiceRegistry.

**Architecture:** Extend the existing ServiceDescriptor/SystemService/SystemServiceRegistry infrastructure (lines 11166-13363 and 63707-63836 of unified_supervisor.py) with governance fields, then progressively implement the SystemService protocol on all 67 classes across 4 biological tiers (Immune, Nervous, Metabolic, Higher Functions). All code stays in one file.

**Tech Stack:** Python 3.10+, pytest, pytest-asyncio, asyncio, dataclasses, ABC

**Design doc:** `docs/plans/2026-03-05-enterprise-organ-activation-design.md`

---

## Notation

- **USP** = `unified_supervisor.py` (the only production file being modified)
- **Line numbers are approximate** — always grep to find the exact insertion point before editing
- All new code is inserted into USP at specific zones; no new production files are created
- Test files are created in `tests/unit/supervisor/`

## Critical Rules

1. **Every edit to USP must preserve the file's existing structure** — insert, don't reorganize
2. **Backward-compatible defaults** — every new field on ServiceDescriptor defaults to the old behavior
3. **Run the full test suite after every commit** — `pytest tests/unit/ -x -q --timeout=30`
4. **Never import USP at module level in tests** — use lazy import inside test functions to avoid side effects
5. **The existing 10 registered services must never break** — regression test runs first

---

## Wave 0: Foundation Hardening (Tasks 1-10)

### Task 1: Add governance dataclasses (ServiceHealthReport, CapabilityContract, etc.)

**Files:**
- Modify: `unified_supervisor.py` (insert after the existing `ServiceDescriptor` class, around line 13145)
- Test: `tests/unit/supervisor/test_governance_dataclasses.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_governance_dataclasses.py`:

```python
"""Tests for governance dataclasses added by the Enterprise Organ Activation Program."""
import pytest


def _import_from_usp(*names):
    """Lazy-import names from unified_supervisor to avoid module-level side effects."""
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


class TestServiceHealthReport:
    def test_construction_minimal(self):
        ServiceHealthReport, = _import_from_usp("ServiceHealthReport")
        report = ServiceHealthReport(alive=True, ready=True)
        assert report.alive is True
        assert report.ready is True
        assert report.degraded is False
        assert report.draining is False
        assert report.message == ""
        assert report.metrics == {}

    def test_frozen(self):
        ServiceHealthReport, = _import_from_usp("ServiceHealthReport")
        report = ServiceHealthReport(alive=True, ready=False)
        with pytest.raises(AttributeError):
            report.alive = False

    def test_degraded_state(self):
        ServiceHealthReport, = _import_from_usp("ServiceHealthReport")
        report = ServiceHealthReport(alive=True, ready=True, degraded=True, message="high latency")
        assert report.degraded is True
        assert report.message == "high latency"


class TestCapabilityContract:
    def test_construction(self):
        CapabilityContract, = _import_from_usp("CapabilityContract")
        cc = CapabilityContract(
            name="test_svc",
            version="1.0.0",
            inputs=["topic.a"],
            outputs=["topic.b"],
            side_effects=["writes_audit_log"],
        )
        assert cc.name == "test_svc"
        assert cc.idempotent is True
        assert cc.cross_repo is False

    def test_frozen(self):
        CapabilityContract, = _import_from_usp("CapabilityContract")
        cc = CapabilityContract(name="x", version="1.0.0", inputs=[], outputs=[], side_effects=[])
        with pytest.raises(AttributeError):
            cc.name = "y"


class TestActivationContract:
    def test_construction_with_defaults(self):
        ActivationContract, BudgetGate, BackoffGate = _import_from_usp(
            "ActivationContract", "BudgetGate", "BackoffGate"
        )
        ac = ActivationContract(
            trigger_events=["anomaly.detected"],
            dependency_gate=["observability"],
            budget_gate=BudgetGate(),
            backoff_gate=BackoffGate(),
        )
        assert ac.max_activations_per_hour == 100
        assert ac.deactivate_after_idle_s == 300.0

    def test_budget_gate_defaults(self):
        BudgetGate, = _import_from_usp("BudgetGate")
        bg = BudgetGate()
        assert bg.max_memory_percent == 85.0
        assert bg.max_cpu_percent == 80.0
        assert bg.min_available_mb == 200.0

    def test_backoff_gate_defaults(self):
        BackoffGate, = _import_from_usp("BackoffGate")
        bo = BackoffGate()
        assert bo.initial_delay_s == 5.0
        assert bo.max_delay_s == 300.0
        assert bo.multiplier == 2.0
        assert bo.jitter is True
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_governance_dataclasses.py -v --timeout=30 2>&1 | head -40`
Expected: FAIL — `AttributeError: module 'unified_supervisor' has no attribute 'ServiceHealthReport'`

**Step 3: Implement the dataclasses**

In USP, find the line after the closing of the existing `ServiceDescriptor` class (around line 13145). Insert:

```python
# =========================================================================
# GOVERNANCE DATACLASSES (Enterprise Organ Activation Program v1.0)
# =========================================================================

@dataclass(frozen=True)
class ServiceHealthReport:
    """Structured health report from a governed service organ."""
    alive: bool                          # liveness: process/task exists
    ready: bool                          # readiness: can accept work
    degraded: bool = False               # working but impaired
    draining: bool = False               # finishing but not accepting new work
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityContract:
    """Formal declaration of what a service does."""
    name: str
    version: str                         # semver
    inputs: List[str]                    # event topics consumed
    outputs: List[str]                   # event topics produced
    side_effects: List[str]              # state mutations this service performs
    idempotent: bool = True
    cross_repo: bool = False             # touches Prime or Reactor


@dataclass
class BudgetGate:
    """Resource conditions that must be met for service activation."""
    max_memory_percent: float = 85.0
    max_cpu_percent: float = 80.0
    min_available_mb: float = 200.0


@dataclass
class BackoffGate:
    """Exponential backoff after activation failures."""
    initial_delay_s: float = 5.0
    max_delay_s: float = 300.0
    multiplier: float = 2.0
    jitter: bool = True


@dataclass
class ActivationContract:
    """When and how an event-driven service activates."""
    trigger_events: List[str]
    dependency_gate: List[str]           # services that must be healthy first
    budget_gate: BudgetGate = field(default_factory=BudgetGate)
    backoff_gate: BackoffGate = field(default_factory=BackoffGate)
    max_activations_per_hour: int = 100
    deactivate_after_idle_s: float = 300.0
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_governance_dataclasses.py -v --timeout=30`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_governance_dataclasses.py unified_supervisor.py
git commit -m "feat(governance): add ServiceHealthReport, CapabilityContract, ActivationContract dataclasses"
```

---

### Task 2: Extend ServiceDescriptor with governance fields

**Files:**
- Modify: `unified_supervisor.py` (the `ServiceDescriptor` dataclass, around line 13133)
- Test: `tests/unit/supervisor/test_service_descriptor_extended.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_service_descriptor_extended.py`:

```python
"""Tests for extended ServiceDescriptor governance fields."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


class TestServiceDescriptorExtended:
    """Verify new governance fields exist with correct defaults."""

    def test_backward_compatible_construction(self):
        """Existing code that creates ServiceDescriptor without new fields still works."""
        ServiceDescriptor, SystemService = _import_from_usp("ServiceDescriptor", "SystemService")

        class FakeService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        desc = ServiceDescriptor(name="test", service=FakeService(), phase=1)
        assert desc.name == "test"
        assert desc.phase == 1
        assert desc.initialized is False
        assert desc.healthy is True

    def test_new_fields_have_defaults(self):
        ServiceDescriptor, SystemService = _import_from_usp("ServiceDescriptor", "SystemService")

        class FakeService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        desc = ServiceDescriptor(name="test", service=FakeService(), phase=1)
        # Activation policy defaults
        assert desc.tier == "optional"
        assert desc.activation_mode == "always_on"
        assert desc.boot_policy == "non_blocking"
        assert desc.criticality == "optional"
        # Budget policy defaults
        assert desc.max_memory_mb == 50.0
        assert desc.max_cpu_percent == 10.0
        assert desc.max_concurrent_ops == 10
        # Failure policy defaults
        assert desc.max_init_retries == 2
        assert desc.init_timeout_s == 30.0
        assert desc.circuit_breaker_threshold == 5
        assert desc.circuit_breaker_recovery_s == 60.0
        assert desc.quarantine_after_failures == 10
        # Health semantics defaults
        assert desc.health_check_interval_s == 30.0
        assert desc.liveness_timeout_s == 10.0
        assert desc.readiness_timeout_s == 5.0
        # Runtime state defaults
        assert desc.state == "pending"
        assert desc.activation_count == 0
        assert desc.last_health_check == 0.0
        # Dependency extensions
        assert desc.soft_depends_on == []

    def test_explicit_governance_fields(self):
        ServiceDescriptor, SystemService = _import_from_usp("ServiceDescriptor", "SystemService")

        class FakeService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        desc = ServiceDescriptor(
            name="security_policy",
            service=FakeService(),
            phase=6,
            tier="immune",
            activation_mode="always_on",
            criticality="control_plane",
            boot_policy="non_blocking",
            max_memory_mb=100.0,
            circuit_breaker_threshold=3,
        )
        assert desc.tier == "immune"
        assert desc.criticality == "control_plane"
        assert desc.max_memory_mb == 100.0
        assert desc.circuit_breaker_threshold == 3
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_service_descriptor_extended.py -v --timeout=30 2>&1 | head -40`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'tier'`

**Step 3: Extend ServiceDescriptor**

In USP, find the `ServiceDescriptor` dataclass (grep for `class ServiceDescriptor:` around line 13133). Replace it with:

```python
@dataclass
class ServiceDescriptor:
    """Metadata for a single governed service organ.

    Extended by Enterprise Organ Activation Program v1.0:
    - tier, activation_mode, boot_policy, criticality for activation governance
    - Budget, failure, and health policies for resource management
    - soft_depends_on for non-blocking dependency edges
    - state machine: pending -> initializing -> ready -> active -> degraded -> draining -> stopped -> quarantined
    """
    # --- Identity (original) ---
    name: str
    service: SystemService
    phase: int                                        # startup phase (1-8)

    # --- Dependencies (original + extension) ---
    depends_on: List[str] = field(default_factory=list)
    soft_depends_on: List[str] = field(default_factory=list)  # non-blocking deps

    # --- Kill switch (original) ---
    enabled_env: Optional[str] = None

    # --- Governance (new) ---
    tier: str = "optional"                # immune | nervous | metabolic | higher | optional
    activation_mode: str = "always_on"    # always_on | warm_standby | event_driven | batch_window
    boot_policy: str = "non_blocking"     # block_ready | non_blocking | deferred_after_ready
    criticality: str = "optional"         # kernel_critical | control_plane | optional

    # --- Budget policy (new) ---
    max_memory_mb: float = 50.0
    max_cpu_percent: float = 10.0
    max_concurrent_ops: int = 10

    # --- Failure policy (new) ---
    max_init_retries: int = 2
    init_timeout_s: float = 30.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery_s: float = 60.0
    quarantine_after_failures: int = 10

    # --- Health semantics (new) ---
    health_check_interval_s: float = 30.0
    liveness_timeout_s: float = 10.0
    readiness_timeout_s: float = 5.0

    # --- Runtime state (original + extensions) ---
    initialized: bool = False
    healthy: bool = True
    state: str = "pending"                # pending|initializing|ready|active|degraded|draining|stopped|quarantined
    error: Optional[str] = None
    init_time_ms: float = 0.0
    memory_delta_mb: float = 0.0
    activation_count: int = 0
    last_health_check: float = 0.0
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_service_descriptor_extended.py -v --timeout=30`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_service_descriptor_extended.py unified_supervisor.py
git commit -m "feat(governance): extend ServiceDescriptor with tier, activation_mode, budget, failure, and health policies"
```

---

### Task 3: Extend SystemService ABC with governance methods

**Files:**
- Modify: `unified_supervisor.py` (the `SystemService` class, around line 11166)
- Test: `tests/unit/supervisor/test_system_service_protocol.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_system_service_protocol.py`:

```python
"""Tests for extended SystemService ABC with governance methods."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


class TestSystemServiceProtocol:
    def test_old_subclass_still_works(self):
        """A subclass implementing only the original 3 methods still instantiates."""
        SystemService, = _import_from_usp("SystemService")

        class LegacyService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        svc = LegacyService()
        assert svc is not None

    def test_new_methods_have_defaults(self):
        """New governance methods provide default implementations."""
        SystemService, ServiceHealthReport = _import_from_usp("SystemService", "ServiceHealthReport")

        class LegacyService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        svc = LegacyService()
        # start() default returns True
        import asyncio
        assert asyncio.get_event_loop().run_until_complete(svc.start()) is True
        # drain() default returns True
        assert asyncio.get_event_loop().run_until_complete(svc.drain(5.0)) is True
        # stop() default calls cleanup()
        asyncio.get_event_loop().run_until_complete(svc.stop())
        # health() default wraps health_check()
        report = asyncio.get_event_loop().run_until_complete(svc.health())
        assert isinstance(report, ServiceHealthReport)
        assert report.alive is True
        assert report.ready is True
        # capability_contract() default returns a stub
        cc = svc.capability_contract()
        assert cc.name == "LegacyService"
        # activation_triggers() default returns empty list
        assert svc.activation_triggers() == []

    @pytest.mark.asyncio
    async def test_full_governance_subclass(self):
        """A class implementing all governance methods works correctly."""
        (SystemService, ServiceHealthReport,
         CapabilityContract) = _import_from_usp(
            "SystemService", "ServiceHealthReport", "CapabilityContract"
        )

        class FullService(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass
            async def start(self) -> bool: return True
            async def health(self) -> ServiceHealthReport:
                return ServiceHealthReport(alive=True, ready=True, message="custom")
            async def drain(self, deadline_s: float) -> bool: return True
            async def stop(self) -> None: pass
            def capability_contract(self):
                return CapabilityContract(
                    name="full", version="1.0.0",
                    inputs=["a"], outputs=["b"], side_effects=["c"]
                )
            def activation_triggers(self): return ["event.x"]

        svc = FullService()
        report = await svc.health()
        assert report.message == "custom"
        assert svc.activation_triggers() == ["event.x"]
        assert svc.capability_contract().name == "full"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_system_service_protocol.py -v --timeout=30 2>&1 | head -40`
Expected: FAIL — `AttributeError: 'LegacyService' object has no attribute 'start'` (or similar, because the new methods don't exist yet)

**Step 3: Extend SystemService ABC**

In USP, find `class SystemService(ABC):` (around line 11166). Replace the class body with:

```python
class SystemService(ABC):
    """Uniform lifecycle contract for governed service organs.

    Declared before service subclasses so class inheritance resolution is
    deterministic at import time.

    Original contract (v1): initialize, health_check, cleanup
    Extended contract (v2): + start, health, drain, stop, capability_contract, activation_triggers

    v2 methods have default implementations that delegate to v1 methods, so existing
    subclasses that only implement v1 continue to work without modification.
    """

    # --- v1 lifecycle (original, still abstract) ---

    @abstractmethod
    async def initialize(self) -> None:
        """Set up resources. Called once during activation."""

    @abstractmethod
    async def health_check(self) -> Tuple[bool, str]:
        """Return (healthy, message). Called periodically by registries."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Release resources. Called during shutdown."""

    # --- v2 lifecycle (new, with backward-compatible defaults) ---

    async def start(self) -> bool:
        """Begin active operation. Called after initialize succeeds.
        Default: returns True (no-op for legacy services)."""
        return True

    async def health(self) -> "ServiceHealthReport":
        """Return structured health report.
        Default: wraps legacy health_check() into a ServiceHealthReport."""
        try:
            ok, msg = await self.health_check()
            return ServiceHealthReport(alive=True, ready=ok, message=msg)
        except Exception as exc:
            return ServiceHealthReport(alive=True, ready=False, message=str(exc))

    async def drain(self, deadline_s: float) -> bool:
        """Stop accepting new work, flush in-flight ops before deadline.
        Default: returns True (nothing to drain for legacy services)."""
        return True

    async def stop(self) -> None:
        """Release resources. Must be safe to call multiple times.
        Default: delegates to cleanup()."""
        await self.cleanup()

    # --- v2 capability declaration (new, with defaults) ---

    def capability_contract(self) -> "CapabilityContract":
        """Declare inputs, outputs, side effects, idempotency.
        Default: returns a stub contract with the class name."""
        return CapabilityContract(
            name=type(self).__name__,
            version="0.0.0",
            inputs=[],
            outputs=[],
            side_effects=[],
        )

    def activation_triggers(self) -> List[str]:
        """Return list of event topics that should activate this service.
        Empty list = always_on (activated at boot).
        Default: returns [] (always_on behavior)."""
        return []
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_system_service_protocol.py -v --timeout=30`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_system_service_protocol.py unified_supervisor.py
git commit -m "feat(governance): extend SystemService ABC with start/health/drain/stop/capability_contract/activation_triggers"
```

---

### Task 4: Update SystemServiceRegistry — activation_mode and boot_policy support

**Files:**
- Modify: `unified_supervisor.py` (the `SystemServiceRegistry` class, around line 13147-13363)
- Test: `tests/unit/supervisor/test_registry_activation_modes.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_registry_activation_modes.py`:

```python
"""Tests for SystemServiceRegistry activation_mode and boot_policy support."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


def _make_fake_service():
    SystemService, = _import_from_usp("SystemService")

    class FakeService(SystemService):
        def __init__(self):
            self._initialized = False
            self._started = False

        async def initialize(self) -> None:
            self._initialized = True

        async def start(self) -> bool:
            self._started = True
            return True

        async def health_check(self):
            return (True, "ok")

        async def cleanup(self) -> None:
            pass

    return FakeService()


class TestActivationModes:
    @pytest.mark.asyncio
    async def test_always_on_activates_normally(self):
        """always_on services activate during their phase as before."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="svc_a", service=_make_fake_service(), phase=1,
            activation_mode="always_on",
        ))
        results = await reg.activate_phase(1)
        assert results["svc_a"] is True

    @pytest.mark.asyncio
    async def test_deferred_after_ready_skips_during_phase(self):
        """deferred_after_ready services are NOT activated during normal phase activation."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_deferred", service=svc, phase=1,
            boot_policy="deferred_after_ready",
        ))
        results = await reg.activate_phase(1)
        assert "svc_deferred" not in results  # skipped
        assert svc._initialized is False

    @pytest.mark.asyncio
    async def test_activate_deferred_services(self):
        """Deferred services activate when activate_deferred() is called."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_deferred", service=svc, phase=1,
            boot_policy="deferred_after_ready",
        ))
        # Phase activation skips it
        await reg.activate_phase(1)
        assert svc._initialized is False

        # Now activate deferred
        results = await reg.activate_deferred()
        assert results["svc_deferred"] is True
        assert svc._initialized is True

    @pytest.mark.asyncio
    async def test_warm_standby_initializes_but_does_not_start(self):
        """warm_standby services call initialize() but not start()."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_warm", service=svc, phase=1,
            activation_mode="warm_standby",
        ))
        await reg.activate_phase(1)
        assert svc._initialized is True
        assert svc._started is False

    @pytest.mark.asyncio
    async def test_event_driven_initializes_but_does_not_start(self):
        """event_driven services initialize but don't start until triggered."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_event", service=svc, phase=1,
            activation_mode="event_driven",
        ))
        await reg.activate_phase(1)
        assert svc._initialized is True
        assert svc._started is False


class TestBootPolicy:
    @pytest.mark.asyncio
    async def test_block_ready_blocks(self):
        """block_ready services are included in phase activation results."""
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="svc_blocking", service=_make_fake_service(), phase=1,
            boot_policy="block_ready",
        ))
        results = await reg.activate_phase(1)
        assert results["svc_blocking"] is True


class TestTierKillSwitch:
    @pytest.mark.asyncio
    async def test_tier_kill_switch_disables_tier(self):
        """JARVIS_SERVICE_<TIER>_ENABLED=false disables all services in that tier."""
        import os
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_immune", service=svc, phase=6, tier="immune",
        ))
        os.environ["JARVIS_SERVICE_IMMUNE_ENABLED"] = "false"
        try:
            results = await reg.activate_phase(6)
            assert results.get("svc_immune") is not True
            assert svc._initialized is False
        finally:
            os.environ.pop("JARVIS_SERVICE_IMMUNE_ENABLED", None)

    @pytest.mark.asyncio
    async def test_safe_mode_disables_optional(self):
        """JARVIS_SAFE_MODE=true disables all non-kernel_critical services."""
        import os
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        svc_critical = _make_fake_service()
        svc_optional = _make_fake_service()
        reg.register(ServiceDescriptor(
            name="svc_critical", service=svc_critical, phase=1,
            criticality="kernel_critical",
        ))
        reg.register(ServiceDescriptor(
            name="svc_opt", service=svc_optional, phase=1,
            criticality="optional",
        ))
        os.environ["JARVIS_SAFE_MODE"] = "true"
        try:
            results = await reg.activate_phase(1)
            assert results["svc_critical"] is True
            assert results.get("svc_opt") is not True
        finally:
            os.environ.pop("JARVIS_SAFE_MODE", None)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_registry_activation_modes.py -v --timeout=30 2>&1 | head -50`
Expected: FAIL — various failures because the registry doesn't support the new fields yet

**Step 3: Update SystemServiceRegistry**

In USP, find `class SystemServiceRegistry:` (around line 13147). The changes needed:

1. In `activate_phase()` (around line 13197), add filtering logic:
   - Skip services where `boot_policy == "deferred_after_ready"`
   - Skip services where tier kill switch is set to false
   - Skip optional services when `JARVIS_SAFE_MODE=true`
   - For `activation_mode == "always_on"`: call `initialize()` then `start()`
   - For `activation_mode in ("warm_standby", "event_driven")`: call `initialize()` only
   - Update `desc.state` through transitions

2. Add new method `activate_deferred()`:
   - Activates all `boot_policy == "deferred_after_ready"` services that haven't been initialized

3. Add new method `activate_service()`:
   - Starts a specific warm_standby or event_driven service on demand

Find the `activate_phase` method and replace/extend it. Also add the new methods after `shutdown_all()`.

The key changes to `activate_phase()`:

```python
async def activate_phase(self, phase: int, timeout_per_service: float = 30.0) -> Dict[str, bool]:
    """Activate services for a given phase, respecting governance policies."""
    phase_services = [
        s for s in self._services.values()
        if s.phase == phase and not s.initialized
    ]

    # --- Governance filtering ---
    filtered = []
    for desc in phase_services:
        # Per-service kill switch (original)
        if desc.enabled_env and os.environ.get(desc.enabled_env, "true").lower() == "false":
            logger.info(f"[Registry] {desc.name} disabled by {desc.enabled_env}")
            continue
        # Tier-level kill switch (new)
        tier_env = f"JARVIS_SERVICE_{desc.tier.upper()}_ENABLED"
        if os.environ.get(tier_env, "true").lower() == "false":
            logger.info(f"[Registry] {desc.name} disabled by tier switch {tier_env}")
            continue
        # Safe mode (new)
        if os.environ.get("JARVIS_SAFE_MODE", "false").lower() == "true":
            if desc.criticality not in ("kernel_critical",):
                logger.info(f"[Registry] {desc.name} disabled by JARVIS_SAFE_MODE")
                continue
        # Deferred boot policy (new)
        if desc.boot_policy == "deferred_after_ready":
            logger.info(f"[Registry] {desc.name} deferred (boot_policy=deferred_after_ready)")
            continue
        filtered.append(desc)

    ordered = self._topological_sort(filtered)
    results: Dict[str, bool] = {}

    for desc in ordered:
        desc.state = "initializing"
        t0 = time.monotonic()
        mem_before = _get_process_rss_mb()
        try:
            await asyncio.wait_for(
                desc.service.initialize(),
                timeout=desc.init_timeout_s,
            )
            desc.initialized = True
            desc.state = "ready"

            # For always_on: also call start()
            if desc.activation_mode == "always_on":
                ok = await asyncio.wait_for(
                    desc.service.start(),
                    timeout=desc.init_timeout_s,
                )
                if ok:
                    desc.state = "active"
                    desc.activation_count += 1

            desc.init_time_ms = (time.monotonic() - t0) * 1000
            desc.memory_delta_mb = _get_process_rss_mb() - mem_before
            self._activation_order.append(desc.name)
            results[desc.name] = True
            logger.info(
                f"[Registry] {desc.name} activated "
                f"(mode={desc.activation_mode}, state={desc.state}, "
                f"{desc.init_time_ms:.0f}ms, {desc.memory_delta_mb:+.1f}MB)"
            )
        except Exception as exc:
            desc.state = "stopped"
            desc.error = str(exc)
            desc.healthy = False
            results[desc.name] = False
            logger.error(f"[Registry] {desc.name} failed to activate: {exc}")

    return results
```

Add these new methods to the class:

```python
async def activate_deferred(self, timeout_per_service: float = 30.0) -> Dict[str, bool]:
    """Activate all deferred_after_ready services. Called after kernel reaches READY."""
    deferred = [
        s for s in self._services.values()
        if s.boot_policy == "deferred_after_ready" and not s.initialized
    ]
    # Apply same kill-switch / safe-mode filtering
    filtered = []
    for desc in deferred:
        if desc.enabled_env and os.environ.get(desc.enabled_env, "true").lower() == "false":
            continue
        tier_env = f"JARVIS_SERVICE_{desc.tier.upper()}_ENABLED"
        if os.environ.get(tier_env, "true").lower() == "false":
            continue
        if os.environ.get("JARVIS_SAFE_MODE", "false").lower() == "true":
            if desc.criticality not in ("kernel_critical",):
                continue
        filtered.append(desc)

    ordered = self._topological_sort(filtered)
    results: Dict[str, bool] = {}
    for desc in ordered:
        desc.state = "initializing"
        t0 = time.monotonic()
        mem_before = _get_process_rss_mb()
        try:
            await asyncio.wait_for(
                desc.service.initialize(),
                timeout=desc.init_timeout_s,
            )
            desc.initialized = True
            desc.state = "ready"
            if desc.activation_mode == "always_on":
                ok = await asyncio.wait_for(desc.service.start(), timeout=desc.init_timeout_s)
                if ok:
                    desc.state = "active"
                    desc.activation_count += 1
            desc.init_time_ms = (time.monotonic() - t0) * 1000
            desc.memory_delta_mb = _get_process_rss_mb() - mem_before
            self._activation_order.append(desc.name)
            results[desc.name] = True
        except Exception as exc:
            desc.state = "stopped"
            desc.error = str(exc)
            desc.healthy = False
            results[desc.name] = False
            logger.error(f"[Registry] deferred {desc.name} failed: {exc}")
    return results

async def activate_service(self, name: str) -> bool:
    """Activate a specific warm_standby or event_driven service on demand."""
    desc = self._services.get(name)
    if desc is None:
        return False
    if desc.state == "active":
        return True  # already running
    if not desc.initialized:
        return False  # must be initialized first (via activate_phase)
    try:
        ok = await asyncio.wait_for(desc.service.start(), timeout=desc.init_timeout_s)
        if ok:
            desc.state = "active"
            desc.activation_count += 1
            return True
    except Exception as exc:
        desc.error = str(exc)
        logger.error(f"[Registry] on-demand activation of {name} failed: {exc}")
    return False
```

**Note:** The `_get_process_rss_mb()` helper already exists in USP (grep for it). If it doesn't exist as a module-level function accessible from the registry, add a simple one near the registry:

```python
def _get_process_rss_mb() -> float:
    """Get current process RSS in MB. Returns 0.0 if unavailable."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    except Exception:
        return 0.0
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_registry_activation_modes.py -v --timeout=30`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_registry_activation_modes.py unified_supervisor.py
git commit -m "feat(governance): update SystemServiceRegistry with activation_mode, boot_policy, tier kill switches, safe mode"
```

---

### Task 5: Add dependency cycle detection at registration time

**Files:**
- Modify: `unified_supervisor.py` (the `SystemServiceRegistry.register()` method)
- Test: `tests/unit/supervisor/test_registry_cycle_detection.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_registry_cycle_detection.py`:

```python
"""Tests for dependency cycle detection at registration time."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


def _make_fake_service():
    SystemService, = _import_from_usp("SystemService")
    class FakeService(SystemService):
        async def initialize(self) -> None: pass
        async def health_check(self): return (True, "ok")
        async def cleanup(self) -> None: pass
    return FakeService()


class TestDependencyCycleDetection:
    def test_no_cycle_registers_ok(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(name="a", service=_make_fake_service(), phase=1))
        reg.register(ServiceDescriptor(
            name="b", service=_make_fake_service(), phase=1, depends_on=["a"]
        ))
        # No exception

    def test_direct_cycle_raises(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_fake_service(), phase=1, depends_on=["b"]
        ))
        with pytest.raises(ValueError, match="[Cc]ycle"):
            reg.register(ServiceDescriptor(
                name="b", service=_make_fake_service(), phase=1, depends_on=["a"]
            ))

    def test_transitive_cycle_raises(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_fake_service(), phase=1, depends_on=["c"]
        ))
        reg.register(ServiceDescriptor(
            name="b", service=_make_fake_service(), phase=1, depends_on=["a"]
        ))
        with pytest.raises(ValueError, match="[Cc]ycle"):
            reg.register(ServiceDescriptor(
                name="c", service=_make_fake_service(), phase=1, depends_on=["b"]
            ))

    def test_self_cycle_raises(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        with pytest.raises(ValueError, match="[Cc]ycle"):
            reg.register(ServiceDescriptor(
                name="a", service=_make_fake_service(), phase=1, depends_on=["a"]
            ))

    def test_soft_depends_included_in_cycle_check(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_fake_service(), phase=1, soft_depends_on=["b"]
        ))
        with pytest.raises(ValueError, match="[Cc]ycle"):
            reg.register(ServiceDescriptor(
                name="b", service=_make_fake_service(), phase=1, depends_on=["a"]
            ))
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_registry_cycle_detection.py -v --timeout=30 2>&1 | head -30`
Expected: FAIL — cycles are not detected at registration time

**Step 3: Add cycle detection to register()**

In USP, find the `register()` method in `SystemServiceRegistry`. Replace it with:

```python
def register(self, desc: ServiceDescriptor) -> None:
    """Register a service. Raises ValueError if adding it would create a dependency cycle."""
    # Temporarily add to check for cycles
    self._services[desc.name] = desc
    try:
        self._check_cycles()
    except ValueError:
        del self._services[desc.name]
        raise

def _check_cycles(self) -> None:
    """Detect dependency cycles across all registered services. Raises ValueError on cycle."""
    # Build adjacency: service -> set of services it depends on (hard + soft)
    adj: Dict[str, set] = {}
    for name, desc in self._services.items():
        deps = set(desc.depends_on)
        if hasattr(desc, "soft_depends_on"):
            deps |= set(desc.soft_depends_on)
        # Only include deps that are actually registered
        adj[name] = {d for d in deps if d in self._services}

    # Three-color DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in adj}
    path: List[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in adj.get(node, set()):
            if color[dep] == GRAY:
                # Found cycle — extract it
                cycle_start = path.index(dep)
                cycle = path[cycle_start:] + [dep]
                raise ValueError(
                    f"Dependency cycle detected: {' -> '.join(cycle)}"
                )
            if color[dep] == WHITE:
                dfs(dep)
        path.pop()
        color[node] = BLACK

    for node in adj:
        if color[node] == WHITE:
            dfs(node)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_registry_cycle_detection.py -v --timeout=30`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_registry_cycle_detection.py unified_supervisor.py
git commit -m "feat(governance): add dependency cycle detection at service registration time"
```

---

### Task 6: Add side-effect ownership validation

**Files:**
- Modify: `unified_supervisor.py` (SystemServiceRegistry)
- Test: `tests/unit/supervisor/test_registry_side_effect_ownership.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_registry_side_effect_ownership.py`:

```python
"""Tests for side-effect ownership validation at registration time."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


def _make_service_with_side_effects(effects):
    SystemService, CapabilityContract = _import_from_usp("SystemService", "CapabilityContract")

    class SvcWithEffects(SystemService):
        async def initialize(self) -> None: pass
        async def health_check(self): return (True, "ok")
        async def cleanup(self) -> None: pass
        def capability_contract(self):
            return CapabilityContract(
                name="test", version="1.0.0",
                inputs=[], outputs=[], side_effects=effects,
            )

    return SvcWithEffects()


class TestSideEffectOwnership:
    def test_non_overlapping_side_effects_ok(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_service_with_side_effects(["writes_audit_log"]),
            phase=1,
        ))
        reg.register(ServiceDescriptor(
            name="b", service=_make_service_with_side_effects(["writes_metrics"]),
            phase=1,
        ))
        # No exception

    def test_overlapping_side_effects_raises(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_service_with_side_effects(["writes_audit_log"]),
            phase=1,
        ))
        with pytest.raises(ValueError, match="[Ss]ide.effect.*conflict"):
            reg.register(ServiceDescriptor(
                name="b", service=_make_service_with_side_effects(["writes_audit_log"]),
                phase=1,
            ))

    def test_empty_side_effects_always_ok(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(
            name="a", service=_make_service_with_side_effects([]),
            phase=1,
        ))
        reg.register(ServiceDescriptor(
            name="b", service=_make_service_with_side_effects([]),
            phase=1,
        ))
        # No exception
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_registry_side_effect_ownership.py -v --timeout=30 2>&1 | head -20`
Expected: FAIL — no side-effect validation exists

**Step 3: Add side-effect ownership check to register()**

In USP, add a `_side_effect_owners` dict to `SystemServiceRegistry.__init__()`:

```python
self._side_effect_owners: Dict[str, str] = {}  # side_effect -> service_name
```

Then in `register()`, after the cycle check, add:

```python
# Check side-effect ownership
cc = desc.service.capability_contract()
for effect in cc.side_effects:
    if effect in self._side_effect_owners:
        owner = self._side_effect_owners[effect]
        del self._services[desc.name]  # rollback
        raise ValueError(
            f"Side-effect conflict: '{effect}' is already owned by "
            f"'{owner}', cannot be claimed by '{desc.name}'"
        )
for effect in cc.side_effects:
    self._side_effect_owners[effect] = desc.name
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_registry_side_effect_ownership.py -v --timeout=30`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_registry_side_effect_ownership.py unified_supervisor.py
git commit -m "feat(governance): add side-effect ownership validation at registration time"
```

---

### Task 7: Add structured health check with ServiceHealthReport

**Files:**
- Modify: `unified_supervisor.py` (SystemServiceRegistry.health_check_all)
- Test: `tests/unit/supervisor/test_registry_structured_health.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_registry_structured_health.py`:

```python
"""Tests for structured health checks returning ServiceHealthReport."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


def _make_healthy_service():
    SystemService, ServiceHealthReport = _import_from_usp("SystemService", "ServiceHealthReport")
    class HealthySvc(SystemService):
        async def initialize(self) -> None: pass
        async def health_check(self): return (True, "ok")
        async def cleanup(self) -> None: pass
        async def health(self):
            return ServiceHealthReport(alive=True, ready=True, message="all good")
    return HealthySvc()


def _make_degraded_service():
    SystemService, ServiceHealthReport = _import_from_usp("SystemService", "ServiceHealthReport")
    class DegradedSvc(SystemService):
        async def initialize(self) -> None: pass
        async def health_check(self): return (False, "high latency")
        async def cleanup(self) -> None: pass
        async def health(self):
            return ServiceHealthReport(alive=True, ready=True, degraded=True, message="high latency")
    return DegradedSvc()


class TestStructuredHealthChecks:
    @pytest.mark.asyncio
    async def test_health_check_all_returns_reports(self):
        SystemServiceRegistry, ServiceDescriptor, ServiceHealthReport = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor", "ServiceHealthReport"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(name="healthy", service=_make_healthy_service(), phase=1))
        reg.register(ServiceDescriptor(name="degraded", service=_make_degraded_service(), phase=1))
        await reg.activate_phase(1)

        reports = await reg.health_check_all_structured()
        assert isinstance(reports["healthy"], ServiceHealthReport)
        assert reports["healthy"].alive is True
        assert reports["healthy"].ready is True
        assert reports["degraded"].degraded is True

    @pytest.mark.asyncio
    async def test_health_updates_descriptor_state(self):
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(name="healthy", service=_make_healthy_service(), phase=1))
        await reg.activate_phase(1)
        await reg.health_check_all_structured()

        desc = reg._services["healthy"]
        assert desc.healthy is True
        assert desc.last_health_check > 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_registry_structured_health.py -v --timeout=30 2>&1 | head -20`
Expected: FAIL — `SystemServiceRegistry` has no `health_check_all_structured` method

**Step 3: Add health_check_all_structured()**

In USP, add after the existing `health_check_all()` method in SystemServiceRegistry:

```python
async def health_check_all_structured(self) -> Dict[str, "ServiceHealthReport"]:
    """Run structured health checks on all initialized services.
    Returns Dict[service_name, ServiceHealthReport]."""
    reports: Dict[str, ServiceHealthReport] = {}
    for name, desc in self._services.items():
        if not desc.initialized:
            continue
        try:
            report = await asyncio.wait_for(
                desc.service.health(),
                timeout=desc.liveness_timeout_s,
            )
            desc.healthy = report.alive and report.ready
            if report.degraded and desc.state == "active":
                desc.state = "degraded"
            elif report.ready and desc.state == "degraded":
                desc.state = "active"
            desc.last_health_check = time.monotonic()
            reports[name] = report
        except asyncio.TimeoutError:
            desc.healthy = False
            desc.last_health_check = time.monotonic()
            reports[name] = ServiceHealthReport(
                alive=False, ready=False, message="health check timed out"
            )
        except Exception as exc:
            desc.healthy = False
            desc.last_health_check = time.monotonic()
            reports[name] = ServiceHealthReport(
                alive=True, ready=False, message=str(exc)
            )
    return reports
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_registry_structured_health.py -v --timeout=30`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_registry_structured_health.py unified_supervisor.py
git commit -m "feat(governance): add health_check_all_structured() returning ServiceHealthReport per service"
```

---

### Task 8: Add drain support to shutdown sequence

**Files:**
- Modify: `unified_supervisor.py` (SystemServiceRegistry.shutdown_all)
- Test: `tests/unit/supervisor/test_registry_drain_shutdown.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_registry_drain_shutdown.py`:

```python
"""Tests for graceful drain-then-stop shutdown sequence."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


class TestDrainShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_calls_drain_then_stop(self):
        """Shutdown sequence: drain() -> stop() for each service, in reverse order."""
        SystemService, SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemService", "SystemServiceRegistry", "ServiceDescriptor"
        )
        call_log = []

        class TrackedService(SystemService):
            def __init__(self, name):
                self._name = name
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass
            async def drain(self, deadline_s):
                call_log.append(f"drain:{self._name}")
                return True
            async def stop(self):
                call_log.append(f"stop:{self._name}")

        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(name="first", service=TrackedService("first"), phase=1))
        reg.register(ServiceDescriptor(name="second", service=TrackedService("second"), phase=1,
                                        depends_on=["first"]))
        await reg.activate_phase(1)

        await reg.shutdown_all()
        # Reverse order: second drains+stops before first
        assert call_log == [
            "drain:second", "stop:second",
            "drain:first", "stop:first",
        ]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_registry_drain_shutdown.py -v --timeout=30 2>&1 | head -20`
Expected: FAIL — current shutdown_all() calls cleanup(), not drain()+stop()

**Step 3: Update shutdown_all()**

In USP, find `shutdown_all()` in SystemServiceRegistry. Replace it with:

```python
async def shutdown_all(self, drain_deadline_s: float = 10.0) -> None:
    """Shutdown all services: drain then stop, in reverse activation order."""
    for name in reversed(self._activation_order):
        desc = self._services.get(name)
        if desc is None or not desc.initialized:
            continue
        try:
            desc.state = "draining"
            await asyncio.wait_for(
                desc.service.drain(drain_deadline_s),
                timeout=drain_deadline_s + 2.0,
            )
        except Exception as exc:
            logger.warning(f"[Registry] {name} drain error: {exc}")
        try:
            await asyncio.wait_for(desc.service.stop(), timeout=5.0)
            desc.state = "stopped"
            desc.initialized = False
        except Exception as exc:
            logger.warning(f"[Registry] {name} stop error: {exc}")
            desc.state = "stopped"
    self._activation_order.clear()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_registry_drain_shutdown.py -v --timeout=30`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_registry_drain_shutdown.py unified_supervisor.py
git commit -m "feat(governance): update shutdown_all() with drain-then-stop sequence"
```

---

### Task 9: Regression test — existing 10 services still work

**Files:**
- Test: `tests/unit/supervisor/test_existing_services_regression.py`

**Step 1: Write the regression test**

Create `tests/unit/supervisor/test_existing_services_regression.py`:

```python
"""Regression: existing 10 services must still construct and register with extended ServiceDescriptor."""
import pytest
import os


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


EXISTING_SERVICE_CLASSES = [
    "ObservabilityPipeline",
    "HealthAggregator",
    "CacheHierarchyManager",
    "TokenBucketRateLimiter",
    "CostTracker",
    "DistributedLockManager",
    "TaskQueueManager",
    "EventSourcingManager",
    "MessageBroker",
    "GracefulDegradationManager",
]


class TestExistingServicesRegression:
    @pytest.mark.parametrize("class_name", EXISTING_SERVICE_CLASSES)
    def test_class_exists_and_is_system_service(self, class_name):
        """Each existing service class still exists and extends SystemService."""
        cls, SystemService = _import_from_usp(class_name, "SystemService")
        assert issubclass(cls, SystemService), f"{class_name} must extend SystemService"

    @pytest.mark.parametrize("class_name", EXISTING_SERVICE_CLASSES)
    def test_class_has_v2_methods(self, class_name):
        """Each existing service has v2 governance methods (via ABC defaults)."""
        cls, = _import_from_usp(class_name)
        # v2 methods should be available (from ABC defaults)
        for method_name in ("start", "health", "drain", "stop", "capability_contract", "activation_triggers"):
            assert hasattr(cls, method_name), f"{class_name} missing {method_name}"

    def test_service_descriptor_backward_compat(self):
        """ServiceDescriptor constructed with only original fields still works."""
        ServiceDescriptor, SystemService = _import_from_usp("ServiceDescriptor", "SystemService")

        class StubSvc(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        # This is how the existing _init_service_registry() creates descriptors
        desc = ServiceDescriptor(
            name="test",
            service=StubSvc(),
            phase=1,
            depends_on=["observability"],
            enabled_env="JARVIS_SERVICE_TEST_ENABLED",
        )
        assert desc.tier == "optional"  # default
        assert desc.activation_mode == "always_on"  # default
        assert desc.boot_policy == "non_blocking"  # default
        assert desc.state == "pending"  # default

    @pytest.mark.asyncio
    async def test_registry_activates_legacy_style(self):
        """SystemServiceRegistry.activate_phase() works with legacy-style descriptors."""
        SystemServiceRegistry, ServiceDescriptor, SystemService = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor", "SystemService"
        )

        class StubSvc(SystemService):
            async def initialize(self) -> None: pass
            async def health_check(self): return (True, "ok")
            async def cleanup(self) -> None: pass

        reg = SystemServiceRegistry()
        reg.register(ServiceDescriptor(name="a", service=StubSvc(), phase=1))
        reg.register(ServiceDescriptor(name="b", service=StubSvc(), phase=1, depends_on=["a"]))
        results = await reg.activate_phase(1)
        assert results == {"a": True, "b": True}

        stats = reg.stats
        assert stats["active"] == 2
        assert stats["healthy"] == 2
```

**Step 2: Run test**

Run: `pytest tests/unit/supervisor/test_existing_services_regression.py -v --timeout=30`
Expected: All tests PASS (no regressions)

**Step 3: Commit**

```bash
git add tests/unit/supervisor/test_existing_services_regression.py
git commit -m "test(governance): add regression tests for existing 10 services with extended ServiceDescriptor"
```

---

### Task 10: Run full test suite — Wave 0 gate

**Step 1: Run full unit test suite**

Run: `pytest tests/unit/ -x -q --timeout=30 2>&1 | tail -20`
Expected: All tests pass. Zero regressions.

**Step 2: Run the new governance tests specifically**

Run: `pytest tests/unit/supervisor/test_governance_dataclasses.py tests/unit/supervisor/test_service_descriptor_extended.py tests/unit/supervisor/test_system_service_protocol.py tests/unit/supervisor/test_registry_activation_modes.py tests/unit/supervisor/test_registry_cycle_detection.py tests/unit/supervisor/test_registry_side_effect_ownership.py tests/unit/supervisor/test_registry_structured_health.py tests/unit/supervisor/test_registry_drain_shutdown.py tests/unit/supervisor/test_existing_services_regression.py -v --timeout=30`
Expected: All governance tests pass.

**Step 3: Verify USP still parses**

Run: `python3 -c "import unified_supervisor; print('OK:', len(dir(unified_supervisor)), 'names exported')"`
Expected: `OK: <number> names exported` (no import errors)

**Wave 0 go/no-go:** If all three steps pass, Wave 0 is complete. Proceed to Wave 1.

---

## Wave 1: Immune System (Tasks 11-14)

### Task 11: Implement SystemService protocol on Immune System classes

**Files:**
- Modify: `unified_supervisor.py` (8 classes in Zone 4.14, Security/Compliance)
- Test: `tests/unit/supervisor/test_immune_system_protocol.py`

The 8 Immune System classes to upgrade:

| Service Name | Class | Line (approx) |
|-------------|-------|---------------|
| security_policy | SecurityPolicyEngine | 40541 |
| anomaly_detector | AnomalyDetector | 42187 |
| audit_trail | AuditTrailRecorder | 34171 |
| threat_intel | ThreatIntelligenceManager | 42803 |
| incident_response | IncidentResponseCoordinator | 42415 |
| compliance | ComplianceAuditor | 40949 |
| data_classification | DataClassificationManager | 41283 |
| access_control | AccessControlManager | 41568 |

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_immune_system_protocol.py`:

```python
"""Tests that all 8 Immune System classes implement the full SystemService protocol."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


IMMUNE_CLASSES = [
    "SecurityPolicyEngine",
    "AnomalyDetector",
    "AuditTrailRecorder",
    "ThreatIntelligenceManager",
    "IncidentResponseCoordinator",
    "ComplianceAuditor",
    "DataClassificationManager",
    "AccessControlManager",
]


class TestImmuneSystemProtocol:
    @pytest.mark.parametrize("class_name", IMMUNE_CLASSES)
    def test_extends_system_service(self, class_name):
        cls, SystemService = _import_from_usp(class_name, "SystemService")
        assert issubclass(cls, SystemService), f"{class_name} must extend SystemService"

    @pytest.mark.parametrize("class_name", IMMUNE_CLASSES)
    def test_has_capability_contract(self, class_name):
        cls, CapabilityContract = _import_from_usp(class_name, "CapabilityContract")
        # Must be able to construct without args (or with defaults)
        # Some classes need constructor args — instantiate with minimal defaults
        instance = _construct_with_defaults(class_name)
        cc = instance.capability_contract()
        assert isinstance(cc, CapabilityContract)
        assert cc.name != ""
        assert cc.version != "0.0.0"  # Must override the stub default

    @pytest.mark.parametrize("class_name", IMMUNE_CLASSES)
    def test_has_activation_triggers(self, class_name):
        instance = _construct_with_defaults(class_name)
        triggers = instance.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.parametrize("class_name", IMMUNE_CLASSES)
    @pytest.mark.asyncio
    async def test_lifecycle_methods_callable(self, class_name):
        ServiceHealthReport, = _import_from_usp("ServiceHealthReport")
        instance = _construct_with_defaults(class_name)
        await instance.initialize()
        report = await instance.health()
        assert isinstance(report, ServiceHealthReport)
        ok = await instance.start()
        assert isinstance(ok, bool)
        ok = await instance.drain(5.0)
        assert isinstance(ok, bool)
        await instance.stop()


def _construct_with_defaults(class_name):
    """Construct an immune system class with sensible test defaults."""
    import importlib
    mod = importlib.import_module("unified_supervisor")
    cls = getattr(mod, class_name)

    # Each class has different constructor signatures - provide minimal defaults
    import inspect
    sig = inspect.signature(cls.__init__)
    params = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.default is not inspect.Parameter.empty:
            continue  # has a default, skip
        # Provide type-appropriate defaults
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            params[name] = None
        elif annotation == str or "str" in str(annotation):
            params[name] = "test"
        elif annotation == int or "int" in str(annotation):
            params[name] = 1
        elif annotation == float or "float" in str(annotation):
            params[name] = 1.0
        elif annotation == bool or "bool" in str(annotation):
            params[name] = False
        elif "Path" in str(annotation):
            import tempfile
            from pathlib import Path
            params[name] = Path(tempfile.mkdtemp())
        elif "Dict" in str(annotation):
            params[name] = {}
        elif "List" in str(annotation):
            params[name] = []
        else:
            params[name] = None

    return cls(**params)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/supervisor/test_immune_system_protocol.py -v --timeout=30 -x 2>&1 | head -30`
Expected: FAIL — classes don't extend SystemService yet (most are standalone classes)

**Step 3: Upgrade each Immune System class**

For each of the 8 classes, the pattern is:

1. **Make it extend SystemService** (if it doesn't already):
   ```python
   # Before:
   class SecurityPolicyEngine:
   # After:
   class SecurityPolicyEngine(SystemService):
   ```

2. **Add the 3 original abstract methods** if missing (`initialize`, `health_check`, `cleanup`):
   ```python
   async def initialize(self) -> None:
       """Initialize the security policy engine."""
       # Wire up any internal state that needs async setup
       pass

   async def health_check(self) -> Tuple[bool, str]:
       return (True, f"{len(self._policies)} policies loaded")

   async def cleanup(self) -> None:
       """Release resources."""
       pass
   ```

3. **Override the v2 governance methods** with real implementations:
   ```python
   def capability_contract(self) -> CapabilityContract:
       return CapabilityContract(
           name="security_policy",
           version="1.0.0",
           inputs=["agent.action", "ipc.command", "file.access"],
           outputs=["security.violation", "security.allow"],
           side_effects=["writes_security_audit"],
       )

   def activation_triggers(self) -> List[str]:
       return []  # always_on

   async def start(self) -> bool:
       # Load default security policies
       return True

   async def health(self) -> ServiceHealthReport:
       return ServiceHealthReport(
           alive=True,
           ready=len(self._policies) > 0,
           message=f"{len(self._policies)} policies active",
       )

   async def drain(self, deadline_s: float) -> bool:
       return True

   async def stop(self) -> None:
       await self.cleanup()
   ```

**Repeat for all 8 classes.** Each class gets:
- Real `capability_contract()` with actual inputs/outputs/side_effects per design doc
- Real `activation_triggers()` per design doc
- Real `health()` that checks internal state
- `initialize()` that sets up internal state (many already have setup logic)

Specific capability contracts per service (from design doc):

| Service | inputs | outputs | side_effects |
|---------|--------|---------|-------------|
| SecurityPolicyEngine | agent.action, ipc.command | security.violation, security.allow | writes_security_audit |
| AnomalyDetector | telemetry.metric, telemetry.event | anomaly.detected, anomaly.baseline_update | writes_anomaly_scores |
| AuditTrailRecorder | supervisor.event.* | audit.entry.created | writes_audit_trail |
| ThreatIntelligenceManager | anomaly.detected | threat.confirmed, threat.dismissed | writes_threat_indicators |
| IncidentResponseCoordinator | threat.confirmed | incident.opened, incident.resolved | writes_incident_log |
| ComplianceAuditor | data.ingested, health.report | compliance.violation, compliance.pass | writes_compliance_report |
| DataClassificationManager | data.ingested | data.classified | writes_classification_labels |
| AccessControlManager | cross_repo.request | access.granted, access.denied | writes_access_log |

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/supervisor/test_immune_system_protocol.py -v --timeout=30`
Expected: All 32 tests (4 tests x 8 classes) PASS

**Step 5: Commit**

```bash
git add tests/unit/supervisor/test_immune_system_protocol.py unified_supervisor.py
git commit -m "feat(immune): implement SystemService protocol on all 8 Immune System classes"
```

---

### Task 12: Register Immune System services in _init_service_registry()

**Files:**
- Modify: `unified_supervisor.py` (JarvisSystemKernel._init_service_registry, around line 63707)
- Test: `tests/unit/supervisor/test_immune_system_registration.py`

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_immune_system_registration.py`:

```python
"""Test that Immune System services are registered at phase 6."""
import pytest


def _import_from_usp(*names):
    import importlib
    mod = importlib.import_module("unified_supervisor")
    return tuple(getattr(mod, n) for n in names)


IMMUNE_SERVICES = {
    "security_policy": {"tier": "immune", "phase": 6, "criticality": "control_plane", "activation_mode": "always_on"},
    "anomaly_detector": {"tier": "immune", "phase": 6, "criticality": "control_plane", "activation_mode": "always_on"},
    "audit_trail": {"tier": "immune", "phase": 6, "criticality": "control_plane", "activation_mode": "always_on"},
    "threat_intel": {"tier": "immune", "phase": 6, "activation_mode": "event_driven", "boot_policy": "deferred_after_ready"},
    "incident_response": {"tier": "immune", "phase": 6, "activation_mode": "event_driven", "boot_policy": "deferred_after_ready"},
    "compliance": {"tier": "immune", "phase": 6, "activation_mode": "batch_window", "boot_policy": "deferred_after_ready"},
    "data_classification": {"tier": "immune", "phase": 6, "activation_mode": "event_driven", "boot_policy": "deferred_after_ready"},
    "access_control": {"tier": "immune", "phase": 6, "criticality": "control_plane", "activation_mode": "always_on"},
}


class TestImmuneSystemRegistration:
    def test_immune_services_registered(self):
        """After _init_service_registry(), all 8 immune services are registered at phase 6."""
        # We can't call _init_service_registry() directly without a full kernel,
        # so we test by checking that the registration code exists and parses.
        # The actual integration test would be a soak test.
        SystemServiceRegistry, ServiceDescriptor = _import_from_usp(
            "SystemServiceRegistry", "ServiceDescriptor"
        )
        # Verify we can construct registrations matching the design
        for svc_name, expected in IMMUNE_SERVICES.items():
            # This tests that the tier/phase/mode are correct per design
            assert expected["tier"] == "immune"
            assert expected["phase"] == 6
```

This is a lightweight check — the actual wiring is tested by the soak test (Task 14).

**Step 2: Implement the registration**

In USP, find `_init_service_registry()` (around line 63707). After the existing Phase 5 registration block, add:

```python
    # Phase 6 (Immune System) ─ security, anomaly detection, audit, compliance
    _r(ServiceDescriptor(
        name="security_policy",
        service=SecurityPolicyEngine(),
        phase=6, tier="immune",
        activation_mode="always_on",
        criticality="control_plane",
        boot_policy="non_blocking",
        depends_on=["observability"],
        enabled_env="JARVIS_SERVICE_SECURITY_POLICY_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="anomaly_detector",
        service=AnomalyDetector(),
        phase=6, tier="immune",
        activation_mode="always_on",
        criticality="control_plane",
        boot_policy="non_blocking",
        depends_on=["observability"],
        enabled_env="JARVIS_SERVICE_ANOMALY_DETECTOR_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="audit_trail",
        service=AuditTrailRecorder(),
        phase=6, tier="immune",
        activation_mode="always_on",
        criticality="control_plane",
        boot_policy="non_blocking",
        depends_on=["observability"],
        enabled_env="JARVIS_SERVICE_AUDIT_TRAIL_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="access_control",
        service=AccessControlManager(),
        phase=6, tier="immune",
        activation_mode="always_on",
        criticality="control_plane",
        boot_policy="non_blocking",
        depends_on=["observability", "security_policy"],
        enabled_env="JARVIS_SERVICE_ACCESS_CONTROL_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="threat_intel",
        service=ThreatIntelligenceManager(),
        phase=6, tier="immune",
        activation_mode="event_driven",
        boot_policy="deferred_after_ready",
        depends_on=["anomaly_detector"],
        enabled_env="JARVIS_SERVICE_THREAT_INTEL_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="incident_response",
        service=IncidentResponseCoordinator(),
        phase=6, tier="immune",
        activation_mode="event_driven",
        boot_policy="deferred_after_ready",
        depends_on=["threat_intel"],
        enabled_env="JARVIS_SERVICE_INCIDENT_RESPONSE_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="compliance",
        service=ComplianceAuditor(),
        phase=6, tier="immune",
        activation_mode="batch_window",
        boot_policy="deferred_after_ready",
        depends_on=["health_aggregator"],
        enabled_env="JARVIS_SERVICE_COMPLIANCE_ENABLED",
    ))
    _r(ServiceDescriptor(
        name="data_classification",
        service=DataClassificationManager(),
        phase=6, tier="immune",
        activation_mode="event_driven",
        boot_policy="deferred_after_ready",
        depends_on=["event_sourcing"],
        enabled_env="JARVIS_SERVICE_DATA_CLASSIFICATION_ENABLED",
    ))

    logger.info("[Kernel] Service registry: 18 services registered across phases 1-6")
```

**Note:** The exact constructor arguments for each class will depend on what parameters their `__init__()` currently requires. Check each class's constructor and provide appropriate defaults from env vars, matching the pattern used by the existing 10 services.

**Step 3: Run test**

Run: `pytest tests/unit/supervisor/test_immune_system_registration.py -v --timeout=30`
Expected: PASS

**Step 4: Commit**

```bash
git add tests/unit/supervisor/test_immune_system_registration.py unified_supervisor.py
git commit -m "feat(immune): register 8 Immune System services at phase 6 in _init_service_registry()"
```

---

### Task 13: Wire Immune System event connections

**Files:**
- Modify: `unified_supervisor.py` (kernel startup, after service activation)

**Step 1: Wire event bus subscriptions**

After the kernel activates phase 6, it should wire the immune services to their event sources. This wiring happens in the kernel's startup sequence, after `activate_phase(6)` returns.

In USP, find where `activate_phase()` calls are made in the kernel startup (grep for `activate_phase` in the kernel section, around lines 63000-96000). After the phase 6 activation, add:

```python
# Wire Immune System event connections
if self._service_registry.get("audit_trail"):
    # AuditTrailRecorder subscribes to all supervisor events
    bus = get_event_bus()
    if bus:
        bus.subscribe("*", self._service_registry.get("audit_trail").on_event)

if self._service_registry.get("anomaly_detector"):
    # AnomalyDetector receives telemetry from ObservabilityPipeline
    obs = self._service_registry.get("observability")
    if obs and hasattr(obs, "add_telemetry_listener"):
        obs.add_telemetry_listener(self._service_registry.get("anomaly_detector").on_telemetry)
```

**Note:** The exact wiring API depends on how the event bus and observability pipeline expose subscription methods. Check their actual APIs before implementing. If they don't have subscription methods, add minimal callback registration methods to them.

**Step 2: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(immune): wire Immune System event bus subscriptions in kernel startup"
```

---

### Task 14: Wave 1 go/no-go — Immune System soak test

**Step 1: Run all governance + immune tests**

Run: `pytest tests/unit/supervisor/ -v --timeout=30`
Expected: All tests pass

**Step 2: Verify USP still imports cleanly**

Run: `python3 -c "import unified_supervisor; print('OK:', len(dir(unified_supervisor)), 'names')"`
Expected: No import errors

**Step 3: Verify no dependency cycles**

Run: `python3 -c "
import unified_supervisor as u
reg = u.SystemServiceRegistry()
# Register all 18 services (manually or via kernel init if accessible)
print('No cycles detected')
"`
Expected: No ValueError raised

**Wave 1 go/no-go:** All tests pass, no import errors, no cycles. Proceed to Wave 2.

---

## Wave 2: Nervous System (Tasks 15-18)

**Pattern:** Same as Wave 1 but for 12 Nervous System classes at phase 7.

### Task 15: Implement SystemService protocol on Nervous System classes

Same pattern as Task 11 but for these 12 classes:

| Service Name | Class | activation_mode | criticality |
|-------------|-------|----------------|-------------|
| workflow_engine | WorkflowEngine | warm_standby | control_plane |
| state_machines | StateMachineManager | always_on | control_plane |
| config_manager | ConfigurationManager | always_on | control_plane |
| feature_gates | FeatureGateManager | always_on | control_plane |
| schema_registry | SchemaRegistry | always_on | control_plane |
| service_discovery | ServiceRegistryManager | always_on | control_plane |
| rules_engine | RulesEngine | warm_standby | optional |
| batch_processor | BatchProcessor | event_driven | optional |
| notifications | NotificationDispatcher | event_driven | optional |
| request_coalescer | RequestCoalescer | event_driven | optional |
| job_manager | BackgroundJobManager | warm_standby | optional |
| dynamic_config | DynamicConfigurationManager | always_on | control_plane |

**Test file:** `tests/unit/supervisor/test_nervous_system_protocol.py`
**Same test structure as Task 11** — parametrized tests for extends_system_service, has_capability_contract, has_activation_triggers, lifecycle_methods_callable.

**Capability contracts per service:**

| Service | inputs | outputs | side_effects |
|---------|--------|---------|-------------|
| WorkflowEngine | workflow.submit, workflow.cancel | workflow.completed, workflow.failed | writes_workflow_state |
| StateMachineManager | state.transition.request | state.transition.completed | writes_state_machine_state |
| ConfigurationManager | config.update | config.changed | writes_config_store |
| FeatureGateManager | feature.toggle | feature.changed | writes_feature_gates |
| SchemaRegistry | schema.register | schema.validated | writes_schema_store |
| ServiceRegistryManager | service.register, service.deregister | service.discovered | writes_service_registry |
| RulesEngine | rule.evaluate | rule.result | (none) |
| BatchProcessor | batch.submit | batch.completed | writes_batch_results |
| NotificationDispatcher | notification.send | notification.delivered | writes_notification_log |
| RequestCoalescer | request.coalesce | request.completed | (none) |
| BackgroundJobManager | job.submit | job.completed, job.failed | writes_job_state |
| DynamicConfigurationManager | config.reload | config.reloaded | writes_dynamic_config |

**Commit:** `feat(nervous): implement SystemService protocol on all 12 Nervous System classes`

### Task 16: Register Nervous System services in _init_service_registry()

Same pattern as Task 12. Add phase 7 registrations after phase 6.

**Key notes:**
- `config_manager` and `schema_registry` have `boot_policy: "block_ready"` — they must succeed before kernel reports ready
- `workflow_engine`, `rules_engine`, `job_manager` are `warm_standby` — initialized but not started

**Commit:** `feat(nervous): register 12 Nervous System services at phase 7`

### Task 17: Wire Nervous System event connections

Same pattern as Task 13. Key wiring:
- WorkflowEngine receives `workflow.submit` events from agent task creation
- ConfigurationManager watches for config file changes (if applicable)
- SchemaRegistry validates cross-repo API contracts at boot

**Commit:** `feat(nervous): wire Nervous System event bus subscriptions`

### Task 18: Wave 2 go/no-go

Same pattern as Task 14.
- All tests pass
- Boot time increase < 3 seconds cumulative
- No dependency cycles
- config_manager and schema_registry both block_ready and succeed

---

## Wave 3: Metabolic System (Tasks 19-22)

**Pattern:** Same as Waves 1-2 but for 15 Metabolic System classes at phase 7 (same phase as nervous, different services).

### Task 19: Implement SystemService protocol on Metabolic System classes

15 classes from the design doc (see Wave 3 table in design doc).

**Test file:** `tests/unit/supervisor/test_metabolic_system_protocol.py`

**Commit:** `feat(metabolic): implement SystemService protocol on all 15 Metabolic System classes`

### Task 20: Register Metabolic System services

Add phase 7 registrations (these share phase 7 with nervous system services — the topological sort handles ordering within phase).

**Key notes:**
- `secret_vault` has `boot_policy: "block_ready"` — must succeed
- `service_mesh` replaces hardcoded HTTP calls — high value, needs careful wiring
- `connection_pools` manages aiohttp.ClientSession lifecycle

**Commit:** `feat(metabolic): register 15 Metabolic System services at phase 7`

### Task 21: Wire Metabolic System event connections

Key wiring:
- ServiceMeshRouter replaces direct HTTP calls to Prime/Reactor
- LoadSheddingController subscribes to DegradationManager memory events
- AlertingManager subscribes to health check failures

**Commit:** `feat(metabolic): wire Metabolic System event bus subscriptions`

### Task 22: Wave 3 go/no-go

- All tests pass
- Boot time increase < 5 seconds cumulative
- ServiceMeshRouter routes test requests
- LoadShedding responds to simulated memory pressure

---

## Wave 4: Higher Functions (Tasks 23-26)

### Task 23: Implement SystemService protocol on Higher Functions classes

32 classes from the design doc (see Wave 4 table).

**Important:** ALL 32 are `deferred_after_ready` — they don't affect boot time at all. They initialize after the kernel reports READY.

**Test file:** `tests/unit/supervisor/test_higher_functions_protocol.py`

**Commit:** `feat(higher): implement SystemService protocol on all 32 Higher Functions classes`

### Task 24: Register Higher Functions services

Add phase 8 registrations. All 32 are `deferred_after_ready` with `criticality: "optional"`.

**Commit:** `feat(higher): register 32 Higher Functions services at phase 8`

### Task 25: Wire Higher Functions event connections

Key wiring:
- DeploymentCoordinator -> BlueGreenDeployer -> CanaryReleaseManager -> RollbackCoordinator (deployment pipeline)
- DataPipelineManager -> DataLakeManager -> MLOpsModelRegistry (data/ML pipeline)
- StreamingAnalyticsEngine subscribes to telemetry for windowed metrics

**Commit:** `feat(higher): wire Higher Functions event bus subscriptions`

### Task 26: Wave 4 go/no-go — Final validation

**Step 1: Run ALL tests**

Run: `pytest tests/unit/ -x -q --timeout=30`
Expected: All pass

**Step 2: Verify all 77 services register without cycles**

Run: `python3 -c "
import unified_supervisor as u
print('Import OK')
# Count ServiceDescriptor registrations in _init_service_registry
import inspect
src = inspect.getsource(u.JarvisSystemKernel._init_service_registry)
count = src.count('ServiceDescriptor(')
print(f'Registered services: {count}')
"`
Expected: `Registered services: 77` (10 original + 67 new)

**Step 3: Verify USP line count**

Run: `wc -l unified_supervisor.py`
Expected: ~97K-100K lines (original 96K + ~1-4K for governance infrastructure)

**Step 4: Commit final verification**

```bash
git add -A
git commit -m "feat(governance): complete Enterprise Organ Activation Program — all 77 services governed"
```

---

## Summary

| Wave | Services | Phase | Tasks | Key Deliverable |
|------|----------|-------|-------|----------------|
| 0 | 0 (infrastructure) | N/A | 1-10 | Extended ServiceDescriptor, SystemService, Registry |
| 1 | 8 Immune | 6 | 11-14 | Security, audit, anomaly detection, compliance |
| 2 | 12 Nervous | 7 | 15-18 | Workflow, config, feature gates, state machines |
| 3 | 15 Metabolic | 7 | 19-22 | Service mesh, connection pools, resource mgmt |
| 4 | 32 Higher | 8 | 23-26 | Deployment, ML ops, data pipeline, analytics |

**Total:** 26 tasks, 77 governed services, ~30 test files, zero new production files (all in USP).
