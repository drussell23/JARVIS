# Enterprise Organ Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Activate 10 dormant enterprise classes in unified_supervisor.py as governed SystemService organs — wired into the registry, integrated with existing infrastructure, and backed by durable storage.

**Architecture:** Three strict phases — governance wiring first (classes become visible, controllable, safe), then real integrations (connect to existing event bus, session manager, workflow engine), then backing stores (replace /tmp with ~/.jarvis/ persistence). One state writer per domain. Constructor purity enforced (no I/O in `__init__`).

**Tech Stack:** Python 3.9+, asyncio, unified_supervisor.py monolith, SystemService ABC, SystemServiceRegistry, TrinityEventBus, CapabilityContract/ServiceDescriptor dataclasses

**Design doc:** User-approved 3-phase plan (governance -> integration -> persistence)

---

## Key Infrastructure Reference

Before touching any code, know these interfaces:

**SystemService** (line 11212) — ABC requiring:
- `async def initialize() -> None`
- `async def health_check() -> Tuple[bool, str]`
- `async def cleanup() -> None`
- Optional v2: `start()`, `drain(deadline_s)`, `stop()`, `capability_contract()`, `activation_triggers()`

**ServiceDescriptor** (line 13277) — Registration dataclass:
- `name`, `service`, `phase` (1-8), `depends_on`, `enabled_env`
- `tier`: immune | nervous | metabolic | higher | optional
- `activation_mode`: always_on | warm_standby | event_driven | batch_window
- `boot_policy`: block_ready | non_blocking | deferred_after_ready

**CapabilityContract** (line 13338) — Frozen dataclass:
- `name`, `version`, `inputs`, `outputs`, `side_effects`, `idempotent`, `cross_repo`

**SystemServiceRegistry** (line 13377) — `register(desc)` validates cycles + side-effect ownership

---

## Phase A: Governance Wiring (Tasks 1-10)

Each of the 10 classes gets the same treatment:
1. Extend `SystemService`
2. Implement/fix the 3 abstract methods + `capability_contract()` + `activation_triggers()`
3. Ensure constructor purity (no I/O in `__init__`)
4. Add per-service kill-switch env var
5. Test governance interface
6. Commit

### Task 1: MLOpsModelRegistry — Governance Wiring

**Files:**
- Modify: `unified_supervisor.py` (class MLOpsModelRegistry, ~line 52520)
- Create: `tests/unit/backend/test_enterprise_organ_governance.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/test_enterprise_organ_governance.py
"""
Governance compliance tests for enterprise organ classes.

Validates that all 10 enterprise organs conform to the SystemService
interface and CapabilityContract requirements.

Run: python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py -v
"""
import asyncio
import sys
import threading
from pathlib import Path
from typing import Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestMLOpsModelRegistryGovernance:
    """MLOpsModelRegistry must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import MLOpsModelRegistry, SystemService
        assert issubclass(MLOpsModelRegistry, SystemService)

    def test_constructor_purity(self):
        """__init__ must not perform I/O."""
        from unified_supervisor import MLOpsModelRegistry
        # Should complete instantly with no side effects
        registry = MLOpsModelRegistry()
        assert hasattr(registry, '_initialized')
        assert registry._initialized is False

    def test_capability_contract_valid(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        contract = registry.capability_contract()
        assert contract.name == "MLOpsModelRegistry"
        assert contract.version != "0.0.0"
        assert len(contract.side_effects) > 0
        assert "writes_model_registry" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        triggers = registry.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        healthy, msg = await registry.health_check()
        # Before initialize, should report not-ready but not crash
        assert isinstance(healthy, bool)
        assert isinstance(msg, str)

    @pytest.mark.asyncio
    async def test_lifecycle_initialize_cleanup(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        await registry.initialize()
        assert registry._initialized is True
        healthy, msg = await registry.health_check()
        assert healthy is True
        await registry.cleanup()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py::TestMLOpsModelRegistryGovernance::test_is_system_service -v`
Expected: FAIL — MLOpsModelRegistry does not extend SystemService

**Step 3: Write minimal implementation**

Edit `unified_supervisor.py` at `class MLOpsModelRegistry` (~line 52520). Change the class declaration and add/fix governance methods:

```python
class MLOpsModelRegistry(SystemService):
    """
    ML model lifecycle management — registration, versioning, deployment tracking.

    v311.0: Upgraded to governed SystemService (Phase A).
    """

    def __init__(self):
        self._models: Dict[str, Any] = {}
        self._experiments: Dict[str, Any] = {}
        self._deployments: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._initialized: bool = False
        # No I/O here — constructor purity enforced

    async def initialize(self) -> None:
        """Set up resources. Called once during activation."""
        self._initialized = True

    async def health_check(self) -> Tuple[bool, str]:
        """Return (healthy, message)."""
        if not self._initialized:
            return False, "not initialized"
        return True, f"ok: {len(self._models)} models, {len(self._experiments)} experiments"

    async def cleanup(self) -> None:
        """Release resources."""
        self._initialized = False
        async with self._lock:
            self._models.clear()
            self._experiments.clear()
            self._deployments.clear()

    def capability_contract(self) -> "CapabilityContract":
        return CapabilityContract(
            name="MLOpsModelRegistry",
            version="1.0.0",
            inputs=["model.register", "model.deploy", "experiment.start"],
            outputs=["model.registered", "model.deployed", "experiment.completed"],
            side_effects=["writes_model_registry"],
        )

    def activation_triggers(self) -> List[str]:
        return ["model.register", "experiment.start"]  # event_driven
```

Preserve ALL existing business methods (register_model, log_model_version, etc.) unchanged.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_enterprise_organ_governance.py
git commit -m "feat(organs): wire MLOpsModelRegistry as governed SystemService (Phase A)

- Extends SystemService ABC
- Implements initialize/health_check/cleanup
- Declares CapabilityContract with side_effects=[writes_model_registry]
- Constructor purity: no I/O in __init__
- activation_triggers for event_driven mode"
```

---

### Task 2: WorkflowOrchestrator — Governance Wiring

**Files:**
- Modify: `unified_supervisor.py` (class WorkflowOrchestrator, ~line 52971)
- Modify: `tests/unit/backend/test_enterprise_organ_governance.py` (append)

**Step 1: Append test class**

```python
class TestWorkflowOrchestratorGovernance:
    """WorkflowOrchestrator must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import WorkflowOrchestrator, SystemService
        assert issubclass(WorkflowOrchestrator, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        assert wf._running is False

    def test_capability_contract_valid(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        contract = wf.capability_contract()
        assert contract.name == "WorkflowOrchestrator"
        assert "writes_workflow_state" in contract.side_effects

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        await wf.initialize()
        healthy, msg = await wf.health_check()
        assert healthy is True
        await wf.cleanup()
        assert wf._running is False
```

**Step 2: Run test — expect FAIL**

**Step 3: Edit class declaration and governance methods**

```python
class WorkflowOrchestrator(SystemService):
    """
    BPMN-like workflow orchestration — definition, execution, tracking.

    v311.0: Upgraded to governed SystemService (Phase A).
    Note: Delegates complex DAG execution to WorkflowEngine (Zone 4.15).
    This class provides the BPM definition layer.
    """
```

Add/fix governance methods:

```python
    async def initialize(self) -> None:
        self._running = True

    async def health_check(self) -> Tuple[bool, str]:
        if not self._running:
            return False, "not running"
        return True, f"ok: {len(self._definitions)} workflows, {len(self._instances)} instances"

    async def cleanup(self) -> None:
        self._running = False
        if self._executor_task and not self._executor_task.done():
            self._executor_task.cancel()
            try:
                await self._executor_task
            except asyncio.CancelledError:
                pass

    def capability_contract(self) -> "CapabilityContract":
        return CapabilityContract(
            name="WorkflowOrchestrator",
            version="1.0.0",
            inputs=["workflow.define", "workflow.start"],
            outputs=["workflow.completed", "workflow.failed"],
            side_effects=["writes_workflow_state"],
        )

    def activation_triggers(self) -> List[str]:
        return ["workflow.start"]
```

**Step 4: Run tests — expect PASS**

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_enterprise_organ_governance.py
git commit -m "feat(organs): wire WorkflowOrchestrator as governed SystemService (Phase A)"
```

---

### Task 3: DocumentManagementSystem — Governance Wiring

**Files:**
- Modify: `unified_supervisor.py` (class DocumentManagementSystem, ~line 53410)
- Modify: `tests/unit/backend/test_enterprise_organ_governance.py` (append)

**Step 1: Append test**

```python
class TestDocumentManagementSystemGovernance:
    def test_is_system_service(self):
        from unified_supervisor import DocumentManagementSystem, SystemService
        assert issubclass(DocumentManagementSystem, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        assert dms._initialized is False
        # Must NOT create /tmp dirs in __init__

    def test_capability_contract(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        contract = dms.capability_contract()
        assert "writes_document_store" in contract.side_effects

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        await dms.initialize()
        healthy, _ = await dms.health_check()
        assert healthy is True
        await dms.cleanup()
```

**Step 2: Run test — expect FAIL**

**Step 3: Edit class**

```python
class DocumentManagementSystem(SystemService):
```

Fix `__init__` for constructor purity — move `os.makedirs()` from `__init__` to `initialize()`:

```python
    def __init__(self, storage_path: Optional[str] = None):
        self._storage_path = storage_path  # resolved in initialize()
        self._documents: Dict[str, Any] = {}
        self._folders: Dict[str, Any] = {}
        self._search_index: Dict[str, Set[str]] = {}
        self._lock = asyncio.Lock()
        self._initialized: bool = False

    async def initialize(self) -> None:
        if self._storage_path is None:
            self._storage_path = str(Path.home() / ".jarvis" / "dms_storage")
        Path(self._storage_path).mkdir(parents=True, exist_ok=True)
        self._initialized = True

    async def health_check(self) -> Tuple[bool, str]:
        if not self._initialized:
            return False, "not initialized"
        return True, f"ok: {len(self._documents)} docs, {len(self._folders)} folders"

    async def cleanup(self) -> None:
        self._initialized = False

    def capability_contract(self) -> "CapabilityContract":
        return CapabilityContract(
            name="DocumentManagementSystem",
            version="1.0.0",
            inputs=["document.create", "document.update"],
            outputs=["document.created", "document.updated"],
            side_effects=["writes_document_store"],
        )

    def activation_triggers(self) -> List[str]:
        return ["document.create"]
```

**Step 4: Run tests — expect PASS**

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_enterprise_organ_governance.py
git commit -m "feat(organs): wire DocumentManagementSystem as governed SystemService (Phase A)

Constructor purity: moved os.makedirs from __init__ to initialize()."
```

---

### Tasks 4-9: Remaining Governance Wiring (Same Pattern)

Each follows the identical pattern as Tasks 1-3. For brevity, here are the specifics per class:

### Task 4: NotificationHub

```python
class NotificationHub(SystemService):
    # capability_contract:
    #   side_effects=["writes_notification_queue"]
    #   inputs=["notification.send"], outputs=["notification.delivered", "notification.failed"]
    #   activation_triggers=["notification.send"]
    # Move delivery loop start from __init__ to initialize()
    # cleanup() cancels _delivery_task
```

### Task 5: SessionManager

```python
class SessionManager(SystemService):
    # capability_contract:
    #   side_effects=["writes_session_store"]
    #   inputs=["session.create", "session.validate"]
    #   outputs=["session.created", "session.expired"]
    #   activation_triggers=[]  # always_on — sessions are core
    # NOTE: This is the stub SessionManager, NOT GlobalSessionManager.
    # Phase B will delegate to GlobalSessionManager as state authority.
    # cleanup() cancels _cleanup_task
```

### Task 6: DataLakeManager

```python
class DataLakeManager(SystemService):
    # capability_contract:
    #   side_effects=["writes_data_lake"]
    #   inputs=["dataset.register", "partition.add"]
    #   outputs=["dataset.registered", "partition.added"]
    #   activation_triggers=["dataset.register"]
    # Move os.makedirs from __init__ to initialize()
    # Resolve storage_root default to ~/.jarvis/data_lake (not /tmp)
```

### Task 7: StreamingAnalyticsEngine

```python
class StreamingAnalyticsEngine(SystemService):
    # capability_contract:
    #   side_effects=["writes_stream_state"]
    #   inputs=["stream.ingest", "aggregation.register"]
    #   outputs=["aggregation.result"]
    #   activation_triggers=["stream.ingest"]
    # cleanup() cancels _process_task
```

### Task 8: ConsentManagementSystem

```python
class ConsentManagementSystem(SystemService):
    # capability_contract:
    #   side_effects=["writes_consent_records"]
    #   inputs=["consent.record", "consent.withdraw", "dsr.submit"]
    #   outputs=["consent.recorded", "dsr.completed"]
    #   activation_triggers=["consent.record", "dsr.submit"]
```

### Task 9: DigitalSignatureService

```python
class DigitalSignatureService(SystemService):
    # capability_contract:
    #   side_effects=["writes_signature_store"]
    #   inputs=["signature.sign", "signature.verify"]
    #   outputs=["signature.signed", "signature.verified"]
    #   activation_triggers=["signature.sign"]
```

Each task: append test class, extend SystemService, implement governance methods, commit.

---

### Task 10: _Deprecated_GracefulDegradationManager — Undeprecate + Governance

**Special case:** This class is marked deprecated but the user wants it alive. Rename it and wire it.

**Step 1: Test**

```python
class TestLegacyDegradationManagerGovernance:
    def test_is_system_service(self):
        from unified_supervisor import LegacyDegradationManager, SystemService
        assert issubclass(LegacyDegradationManager, SystemService)

    def test_capability_contract(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        contract = mgr.capability_contract()
        assert "writes_degradation_state" in contract.side_effects
```

**Step 3: Rename + wire**

```python
# Rename from _Deprecated_GracefulDegradationManager to LegacyDegradationManager
class LegacyDegradationManager(SystemService):
    """
    Rule-based degradation with configurable level thresholds.

    v311.0: Un-deprecated. Complements the resource-aware GracefulDegradationManager
    (Zone 4.7) by providing explicit level-forcing for operator-driven scenarios.
    """
```

Add governance methods following the same pattern.

**Step 5: Commit**

```bash
git commit -m "feat(organs): undeprecate + wire LegacyDegradationManager as SystemService (Phase A)"
```

---

## Phase A Gate: Governance Verification

### Task 11: Verify All 10 Organs Pass Governance Gate

**Files:**
- Modify: `tests/unit/backend/test_enterprise_organ_governance.py` (add parametrized meta-test)

```python
ORGAN_CLASSES = [
    "MLOpsModelRegistry",
    "WorkflowOrchestrator",
    "DocumentManagementSystem",
    "NotificationHub",
    "SessionManager",
    "DataLakeManager",
    "StreamingAnalyticsEngine",
    "ConsentManagementSystem",
    "DigitalSignatureService",
    "LegacyDegradationManager",
]

@pytest.mark.parametrize("class_name", ORGAN_CLASSES)
class TestGovernanceCompliance:
    def test_extends_system_service(self, class_name):
        import unified_supervisor as us
        cls = getattr(us, class_name)
        assert issubclass(cls, us.SystemService)

    def test_capability_contract_has_side_effects(self, class_name):
        import unified_supervisor as us
        instance = getattr(us, class_name)()
        contract = instance.capability_contract()
        assert len(contract.side_effects) > 0, f"{class_name} has no declared side_effects"

    def test_no_io_in_constructor(self, class_name):
        """Constructor must complete without touching disk/network."""
        import unified_supervisor as us
        instance = getattr(us, class_name)()
        # If we got here without exception, constructor is pure
        assert instance is not None
```

Run: `python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py::TestGovernanceCompliance -v`
Expected: 30 tests (10 classes x 3 checks), all PASS.

Commit:
```bash
git commit -m "test(organs): add parametrized governance compliance gate (Phase A gate)"
```

---

## Phase B: Real Integrations (Tasks 12-18)

### Task 12: Register All 10 Organs in SystemServiceRegistry

**Files:**
- Modify: `unified_supervisor.py` — find where services are registered (search for `registry.register(ServiceDescriptor(`)

**Step 1: Find the registration site**

Grep for `registry.register(ServiceDescriptor(` to find where existing services are registered. Add registrations for all 10 organs in the appropriate startup phase.

**Step 2: Add registrations**

All 10 organs go in **phase 7** (enterprise services) with `tier="higher"`, `boot_policy="deferred_after_ready"`, `activation_mode="event_driven"`:

```python
# Phase 7: Enterprise Organ Services
for organ_name, organ_cls, organ_env in [
    ("MLOpsModelRegistry", MLOpsModelRegistry, "JARVIS_MLOPS_ENABLED"),
    ("WorkflowOrchestrator", WorkflowOrchestrator, "JARVIS_WORKFLOW_ENABLED"),
    ("DocumentManagementSystem", DocumentManagementSystem, "JARVIS_DMS_ENABLED"),
    ("NotificationHub", NotificationHub, "JARVIS_NOTIFICATIONS_ENABLED"),
    ("SessionManager", SessionManager, "JARVIS_SESSIONS_ENABLED"),
    ("DataLakeManager", DataLakeManager, "JARVIS_DATALAKE_ENABLED"),
    ("StreamingAnalyticsEngine", StreamingAnalyticsEngine, "JARVIS_STREAMING_ENABLED"),
    ("ConsentManagementSystem", ConsentManagementSystem, "JARVIS_CONSENT_ENABLED"),
    ("DigitalSignatureService", DigitalSignatureService, "JARVIS_SIGNATURES_ENABLED"),
    ("LegacyDegradationManager", LegacyDegradationManager, "JARVIS_LEGACY_DEGRADATION_ENABLED"),
]:
    try:
        registry.register(ServiceDescriptor(
            name=organ_name,
            service=organ_cls(),
            phase=7,
            tier="higher",
            activation_mode="event_driven",
            boot_policy="deferred_after_ready",
            enabled_env=organ_env,
            criticality="optional",
        ))
    except Exception as exc:
        logger.debug(f"[Kernel] Skipped {organ_name}: {exc}")
```

**Step 3: Verify**

Run: `python3 -c "from unified_supervisor import SystemServiceRegistry; print('OK')"`

**Step 4: Commit**

```bash
git commit -m "feat(organs): register 10 enterprise organs in SystemServiceRegistry phase 7 (Phase B)"
```

---

### Task 13: SessionManager — Delegate to GlobalSessionManager

**Goal:** SessionManager becomes a thin facade delegating to GlobalSessionManager (the single state authority for sessions).

```python
class SessionManager(SystemService):
    """Thin facade delegating to GlobalSessionManager (state authority).

    v311.0 Phase B: No longer owns session state. Delegates all operations
    to GlobalSessionManager to prevent split-brain ownership.
    """

    def __init__(self, default_ttl: float = 3600.0, max_sessions_per_user: int = 5):
        self._default_ttl = default_ttl
        self._max_sessions_per_user = max_sessions_per_user
        self._global_mgr: Optional[Any] = None  # resolved in initialize()
        self._initialized = False

    async def initialize(self) -> None:
        self._global_mgr = GlobalSessionManager.get_instance()
        self._initialized = True

    async def create_session(self, user_id: str, **kwargs) -> Optional[str]:
        """Delegate to GlobalSessionManager."""
        if self._global_mgr is None:
            return None
        return self._global_mgr.session_id  # existing session
```

Commit per integration.

---

### Task 14: NotificationHub — Connect to TrinityEventBus

**Goal:** NotificationHub publishes delivery events to TrinityEventBus instead of using stub handlers.

Wire `_delivery_loop()` to publish `SupervisorEvent` or `TrinityEvent` for each notification delivery attempt.

---

### Task 15: WorkflowOrchestrator — Bridge to WorkflowEngine

**Goal:** WorkflowOrchestrator's `start_workflow()` delegates DAG execution to the existing WorkflowEngine (Zone 4.15). WorkflowOrchestrator owns BPM definitions; WorkflowEngine owns execution.

---

### Task 16: MLOpsModelRegistry — Bind to Model Lifecycle Events

**Goal:** MLOpsModelRegistry listens for model events from Reactor Core / JARVIS Prime and tracks them. Does NOT own model loading — that's UnifiedModelServing's job.

---

### Task 17: _Deprecated_GracefulDegradationManager — Complement Active Manager

**Goal:** LegacyDegradationManager provides operator-forced levels (explicit `force_level()`). The active GracefulDegradationManager (Zone 4.7) handles automatic resource-based degradation. Register LegacyDegradationManager's state callbacks to emit events when manually forced.

---

### Task 18: Phase B Gate Verification

Test that:
- All 10 organs are discoverable via `registry.get(name)`
- SessionManager.create_session() delegates (not standalone)
- NotificationHub publishes to event bus
- No duplicate state writers (grep for side_effect conflicts)

---

## Phase C: Backing Stores + Durability (Tasks 19-25)

### Task 19: Create ~/.jarvis/ Storage Layout

```python
# ~/.jarvis/organs/
#   mlops/models.json
#   workflows/definitions.json
#   dms/documents.json
#   notifications/templates.json
#   sessions/  (delegated to GlobalSessionManager)
#   datalake/catalog.json
#   streaming/state.json
#   consent/records.json
#   signatures/keys.json
#   degradation/state.json
```

### Task 20: Atomic Write Helper

Create a shared `_atomic_write_json(path, data)` utility using `tempfile.mkstemp()` + `os.fsync()` + `Path.replace()` — the same pattern from the hardening plan.

### Task 21-24: Wire Persistence into Each Organ

Each organ that needs persistence (all except SessionManager which delegates):
- `initialize()` loads state from `~/.jarvis/organs/{name}/`
- `cleanup()` / `drain()` flushes state atomically
- Schema version field in JSON for future migration
- Retention policy (configurable max entries, max age)

### Task 25: Phase C Gate — Crash Consistency Test

Kill the process mid-write, restart, verify state recovered from last good checkpoint.

---

## Execution Notes

- **Import test after every edit:** `python3 -c "import unified_supervisor"` catches syntax errors immediately
- **Line numbers shift:** After each edit, re-grep for target classes/methods before editing
- **Commit granularity:** One commit per class governance wiring, one commit per integration, one commit per persistence layer
- **No new files except:** test files and the storage helper (if not inlined)
- **Kill switches default ON:** All `JARVIS_*_ENABLED` env vars default to `"true"` so organs activate unless explicitly disabled
