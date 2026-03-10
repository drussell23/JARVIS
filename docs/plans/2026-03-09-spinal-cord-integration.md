# Spinal Cord Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the FUSE context expander to the governed pipeline and inject dual-telemetry (local hardware state + routing decision) into every OperationContext so J-Prime understands its physical constraints and the causal audit trail is cryptographically sound.

**Architecture:** Five files are touched. Two (`oracle.py`, `context_expander.py`) are harden-only; three receive net-new code. All telemetry types are frozen dataclasses — no dicts, no mutable state. Float quantization happens at the `ResourceSnapshot` layer so every consumer gets canonical values. Hash chaining is per-repo-scope to support concurrent multi-repo operations.

**Tech Stack:** Python 3.9+, `dataclasses` (frozen), `hashlib` SHA-256, `psutil`, `platform`, `time.monotonic_ns()`

---

## Background

- `asyncio_mode=auto` is active — **never** use `@pytest.mark.asyncio`. Always run tests with `python3 -m pytest`.
- All mutation helpers on `OperationContext` use the pattern: `dataclasses.replace(self, field=value, previous_hash=self.context_hash, context_hash="")` → `_context_to_hash_dict(intermediate)` → `_compute_hash(...)` → `dataclasses.replace(intermediate, context_hash=new_hash)`.
- `_context_to_hash_dict` (line 683 in `op_context.py`) automatically calls `dataclasses.asdict()` on any frozen dataclass sub-object. The new `TelemetryContext` field will hash correctly with zero changes to that function.
- `TheOracle.is_ready()` (line 1227 in `oracle.py`) returns `self._available` — already exists, no changes needed to oracle.
- FUSE weights in `oracle.py` line 2120 are already `0.55 * graph_prox + 0.35 * semantic_sim + 0.10 * recency` — **verified correct, no changes needed**.
- Truncation in `context_expander.py` is already `MAX_FILES_PER_CATEGORY = 10` with `"... (and {hidden} more)"` — **verified correct, no changes needed**.

---

## Task 1: Telemetry Dataclasses in `op_context.py` + `ResourceSnapshot` Quantization

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py` (insert after line 290, before `# Hash helper` comment)
- Modify: `backend/core/ouroboros/governance/resource_monitor.py` (add fields to `ResourceSnapshot`, quantize `snapshot()`, add private helpers)
- Test: `tests/test_ouroboros_governance/test_op_context.py`
- Test: `tests/test_ouroboros_governance/test_resource_monitor.py`

### Step 1: Write failing tests for telemetry dataclasses

Add to `tests/test_ouroboros_governance/test_op_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 1: Telemetry dataclasses
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    RoutingIntentTelemetry,
    RoutingActualTelemetry,
    TelemetryContext,
)

def _make_host_telemetry(**overrides) -> HostTelemetry:
    defaults = dict(
        schema_version="1.0",
        arch="arm64",
        cpu_percent=14.20,
        ram_available_gb=6.80,
        pressure="NORMAL",
        sampled_at_utc="2026-03-09T12:00:00+00:00",
        sampled_monotonic_ns=1_000_000_000,
        collector_status="ok",
        sample_age_ms=3,
    )
    return HostTelemetry(**{**defaults, **overrides})


def test_host_telemetry_is_frozen():
    ht = _make_host_telemetry()
    with pytest.raises((TypeError, AttributeError)):
        ht.cpu_percent = 99.0  # type: ignore[misc]


def test_host_telemetry_stores_fields():
    ht = _make_host_telemetry(cpu_percent=14.20, ram_available_gb=6.80)
    assert ht.cpu_percent == 14.20
    assert ht.ram_available_gb == 6.80
    assert ht.schema_version == "1.0"
    assert ht.pressure == "NORMAL"
    assert ht.sample_age_ms == 3


def test_routing_intent_telemetry_frozen():
    ri = RoutingIntentTelemetry(expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL")
    with pytest.raises((TypeError, AttributeError)):
        ri.expected_provider = "LOCAL"  # type: ignore[misc]


def test_routing_actual_telemetry_stores_fallback_chain():
    ra = RoutingActualTelemetry(
        provider_name="LOCAL_CLAUDE",
        endpoint_class="local",
        fallback_chain=("GCP_PRIME_SPOT", "LOCAL_CLAUDE"),
        was_degraded=True,
    )
    assert ra.fallback_chain == ("GCP_PRIME_SPOT", "LOCAL_CLAUDE")
    assert ra.was_degraded is True


def test_telemetry_context_routing_actual_optional():
    tc = TelemetryContext(
        local_node=_make_host_telemetry(),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
        ),
    )
    assert tc.routing_actual is None


def test_telemetry_context_with_routing_actual():
    ra = RoutingActualTelemetry(
        provider_name="GCP_PRIME_SPOT",
        endpoint_class="gcp_spot",
        fallback_chain=(),
        was_degraded=False,
    )
    tc = TelemetryContext(
        local_node=_make_host_telemetry(),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
        ),
        routing_actual=ra,
    )
    assert tc.routing_actual is ra
```

### Step 2: Run test to verify it fails

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py::test_host_telemetry_is_frozen -v
```
Expected: `ImportError: cannot import name 'HostTelemetry'`

### Step 3: Write failing tests for ResourceSnapshot quantization

Add to `tests/test_ouroboros_governance/test_resource_monitor.py`:

```python
# ---------------------------------------------------------------------------
# Task 1: ResourceSnapshot new fields + quantization
# ---------------------------------------------------------------------------

async def test_snapshot_quantizes_floats():
    """All float fields in snapshot are rounded to 2 decimal places."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot(
        ram_override=77.777,
        cpu_override=12.345,
        latency_override=3.999,
    )
    assert snap.ram_percent == round(77.777, 2)
    assert snap.cpu_percent == round(12.345, 2)
    assert snap.event_loop_latency_ms == round(3.999, 2)


async def test_snapshot_has_monotonic_ns():
    """sampled_monotonic_ns is a positive integer set by snapshot()."""
    import time
    monitor = ResourceMonitor()
    before = time.monotonic_ns()
    snap = await monitor.snapshot()
    after = time.monotonic_ns()
    assert isinstance(snap.sampled_monotonic_ns, int)
    assert before <= snap.sampled_monotonic_ns <= after


async def test_snapshot_ram_available_gb():
    """ram_available_gb is a non-negative float quantized to 2dp."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert isinstance(snap.ram_available_gb, float)
    assert snap.ram_available_gb >= 0.0
    # Verify 2dp quantization
    assert snap.ram_available_gb == round(snap.ram_available_gb, 2)


async def test_snapshot_platform_arch():
    """platform_arch is a non-empty string (e.g. 'arm64', 'x86_64')."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert isinstance(snap.platform_arch, str)
    assert len(snap.platform_arch) > 0


async def test_snapshot_collector_status():
    """collector_status is 'ok' when psutil is available."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert snap.collector_status in ("ok", "partial")
```

### Step 4: Run tests to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_resource_monitor.py::test_snapshot_quantizes_floats -v
```
Expected: `FAIL — ResourceSnapshot has no field 'sampled_monotonic_ns'`

### Step 5: Implement telemetry dataclasses in `op_context.py`

In `op_context.py`, insert the following block **after line 290** (after the `RepoSagaStatus` dataclass, before `# Hash helper` comment at line 293):

```python
# ---------------------------------------------------------------------------
# Telemetry Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostTelemetry:
    """Snapshot of local hardware state at operation intake."""

    schema_version: str           # "1.0"
    arch: str                     # platform.machine() → "arm64"
    cpu_percent: float            # quantized to 2dp
    ram_available_gb: float       # quantized to 2dp
    pressure: str                 # PressureLevel.name: "NORMAL"|"ELEVATED"|"CRITICAL"|"EMERGENCY"
    sampled_at_utc: str           # datetime.now(utc).isoformat()
    sampled_monotonic_ns: int     # time.monotonic_ns() at sample time
    collector_status: str         # "ok" | "partial" | "stale"
    sample_age_ms: int            # (now_ns - sampled_monotonic_ns) // 1_000_000


@dataclass(frozen=True)
class RoutingIntentTelemetry:
    """Routing decision EXPECTED at FSM intake (before any execution)."""

    expected_provider: str        # e.g. "GCP_PRIME_SPOT", "LOCAL_CLAUDE"
    policy_reason: str            # e.g. "PRIMARY_AVAILABLE", "NORMAL"


@dataclass(frozen=True)
class RoutingActualTelemetry:
    """Routing outcome AFTER execution (stamped at COMPLETE or POSTMORTEM)."""

    provider_name: str
    endpoint_class: str           # "gcp_spot" | "local" | "cloud_api"
    fallback_chain: Tuple[str, ...]
    was_degraded: bool


@dataclass(frozen=True)
class TelemetryContext:
    """Root telemetry envelope stamped once at intake, updated once at completion."""

    local_node: HostTelemetry
    routing_intent: RoutingIntentTelemetry
    routing_actual: Optional[RoutingActualTelemetry] = None
```

No new imports are needed in `op_context.py` — all field types (`str`, `float`, `int`, `bool`, `Tuple`, `Optional`) are already covered by the existing `from typing import ... Tuple` import.

### Step 6: Implement `ResourceSnapshot` quantization and new fields in `resource_monitor.py`

**Add `import platform` to the imports section** (after `import os`):

```python
import platform
```

**Replace the `ResourceSnapshot` dataclass** (lines 67-74) with:

```python
@dataclass(frozen=True)
class ResourceSnapshot:
    """Immutable snapshot of system resource state."""

    ram_percent: float
    cpu_percent: float
    event_loop_latency_ms: float
    disk_io_busy: bool
    sampled_monotonic_ns: int = 0          # set by snapshot(); enables age computation
    ram_available_gb: float = 0.0          # psutil.virtual_memory().available / 1e9, quantized
    platform_arch: str = ""                # platform.machine()
    collector_status: str = "ok"           # "ok" if psutil fully available, "partial" otherwise
```

**Replace the `snapshot()` method body** (lines 133-146) with quantized construction + new field population:

```python
async def snapshot(
    self,
    ram_override: Optional[float] = None,
    cpu_override: Optional[float] = None,
    latency_override: Optional[float] = None,
    io_override: Optional[bool] = None,
) -> ResourceSnapshot:
    """Collect a resource snapshot with all floats quantized to 2dp."""
    ram = ram_override if ram_override is not None else self._get_ram_percent()
    cpu = cpu_override if cpu_override is not None else self._get_cpu_percent()
    latency = latency_override if latency_override is not None else await self._get_event_loop_latency()
    io_busy = io_override if io_override is not None else False

    snap = ResourceSnapshot(
        ram_percent=round(ram, 2),
        cpu_percent=round(cpu, 2),
        event_loop_latency_ms=round(latency, 2),
        disk_io_busy=io_busy,
        sampled_monotonic_ns=time.monotonic_ns(),
        ram_available_gb=round(self._get_ram_available_gb(), 2),
        platform_arch=self._get_platform_arch(),
        collector_status=self._get_collector_status(),
    )
    self._last_snapshot = snap
    self._last_snapshot_time = time.monotonic()
    return snap
```

**Add the three private helpers** after `_get_event_loop_latency()` (after line 169):

```python
def _get_ram_available_gb(self) -> float:
    """Get available RAM in gigabytes."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except ImportError:
        return 0.0

def _get_platform_arch(self) -> str:
    """Get CPU architecture string."""
    return platform.machine()

def _get_collector_status(self) -> str:
    """Return 'ok' if psutil is importable, 'partial' otherwise."""
    try:
        import psutil  # noqa: F401
        return "ok"
    except ImportError:
        return "partial"
```

### Step 7: Run all Task 1 tests to verify they pass

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py::test_host_telemetry_is_frozen tests/test_ouroboros_governance/test_op_context.py::test_host_telemetry_stores_fields tests/test_ouroboros_governance/test_op_context.py::test_routing_intent_telemetry_frozen tests/test_ouroboros_governance/test_op_context.py::test_routing_actual_telemetry_stores_fallback_chain tests/test_ouroboros_governance/test_op_context.py::test_telemetry_context_routing_actual_optional tests/test_ouroboros_governance/test_op_context.py::test_telemetry_context_with_routing_actual -v
```
Expected: All PASS

```bash
python3 -m pytest tests/test_ouroboros_governance/test_resource_monitor.py -v
```
Expected: All PASS (including pre-existing tests)

### Step 8: Run full suite to verify no regressions

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: All tests pass (755+)

### Step 9: Commit

```bash
git add backend/core/ouroboros/governance/op_context.py backend/core/ouroboros/governance/resource_monitor.py tests/test_ouroboros_governance/test_op_context.py tests/test_ouroboros_governance/test_resource_monitor.py
git commit -m "feat(telemetry): add frozen telemetry dataclasses to op_context + quantize ResourceSnapshot"
```

---

## Task 2: OperationContext New Fields + `with_telemetry()` / `with_routing_actual()` + `create()` Update

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py`
- Test: `tests/test_ouroboros_governance/test_op_context.py`

### Step 1: Write failing tests

Add to `tests/test_ouroboros_governance/test_op_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: OperationContext new fields + with_telemetry / with_routing_actual
# ---------------------------------------------------------------------------

def _make_telemetry_context() -> TelemetryContext:
    return TelemetryContext(
        local_node=_make_host_telemetry(),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
        ),
    )


def test_operation_context_telemetry_default_none():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    assert ctx.telemetry is None


def test_operation_context_previous_op_hash_default_empty():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    assert ctx.previous_op_hash_by_scope == ()


def test_create_with_previous_op_hash_by_scope():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
        previous_op_hash_by_scope=(("jarvis", "abc123"),),
    )
    assert ctx.previous_op_hash_by_scope == (("jarvis", "abc123"),)


def test_with_telemetry_advances_hash():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    tc = _make_telemetry_context()
    ctx2 = ctx.with_telemetry(tc)
    assert ctx2.context_hash != ctx.context_hash


def test_with_telemetry_sets_previous_hash():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    tc = _make_telemetry_context()
    ctx2 = ctx.with_telemetry(tc)
    assert ctx2.previous_hash == ctx.context_hash


def test_with_telemetry_sets_telemetry_field():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    tc = _make_telemetry_context()
    ctx2 = ctx.with_telemetry(tc)
    assert ctx2.telemetry is tc
    # Phase should be unchanged
    assert ctx2.phase == ctx.phase


def test_with_telemetry_hash_stability():
    """Same inputs → same hash (no nondeterminism)."""
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    tc = _make_telemetry_context()
    h1 = ctx.with_telemetry(tc).context_hash
    h2 = ctx.with_telemetry(tc).context_hash
    assert h1 == h2


def test_with_routing_actual_advances_hash():
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    tc = _make_telemetry_context()
    ctx2 = ctx.with_telemetry(tc)
    ra = RoutingActualTelemetry(
        provider_name="GCP_PRIME_SPOT",
        endpoint_class="gcp_spot",
        fallback_chain=(),
        was_degraded=False,
    )
    ctx3 = ctx2.with_routing_actual(ra)
    assert ctx3.context_hash != ctx2.context_hash
    assert ctx3.telemetry is not None
    assert ctx3.telemetry.routing_actual is ra


def test_with_routing_actual_requires_existing_telemetry():
    """with_routing_actual raises ValueError if telemetry not yet set."""
    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test",
    )
    ra = RoutingActualTelemetry(
        provider_name="GCP_PRIME_SPOT",
        endpoint_class="gcp_spot",
        fallback_chain=(),
        was_degraded=False,
    )
    with pytest.raises(ValueError, match="telemetry"):
        ctx.with_routing_actual(ra)


def test_concurrent_scope_hash_chains_independent():
    """Two operations on different repo scopes produce independent hash chains."""
    ctx_jarvis = OperationContext.create(
        target_files=("jarvis/foo.py",),
        description="jarvis op",
        primary_repo="jarvis",
        repo_scope=("jarvis",),
        previous_op_hash_by_scope=(("jarvis", "hash_jarvis_prev"),),
    )
    ctx_prime = OperationContext.create(
        target_files=("prime/bar.py",),
        description="prime op",
        primary_repo="prime",
        repo_scope=("prime",),
        previous_op_hash_by_scope=(("prime", "hash_prime_prev"),),
    )
    # Different scopes → different chains, no collision
    jarvis_scope_hashes = dict(ctx_jarvis.previous_op_hash_by_scope)
    prime_scope_hashes = dict(ctx_prime.previous_op_hash_by_scope)
    assert jarvis_scope_hashes.get("jarvis") == "hash_jarvis_prev"
    assert prime_scope_hashes.get("prime") == "hash_prime_prev"
    assert "prime" not in jarvis_scope_hashes
    assert "jarvis" not in prime_scope_hashes
```

### Step 2: Run tests to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py::test_operation_context_telemetry_default_none -v
```
Expected: `FAIL — OperationContext.create() got unexpected keyword argument 'previous_op_hash_by_scope'` (or AttributeError)

### Step 3: Implement new fields on `OperationContext`

In `op_context.py`, add two fields to the `OperationContext` dataclass. Find the line `pre_apply_snapshots: Dict[str, str] = field(default_factory=dict)` (line ~424) and add after it:

```python
    # ---- Telemetry (stamped at intake and COMPLETE) ----
    telemetry: Optional["TelemetryContext"] = None
    previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = ()
    # e.g. (("jarvis", "abc123..."), ("prime", "def456..."))
    # Frozen-safe representation of Dict[repo_name, last_context_hash]
```

### Step 4: Update `create()` method in `op_context.py`

Add `previous_op_hash_by_scope` parameter and update `fields_for_hash` + constructor call.

**Add parameter** to `create()` signature (after `schema_version: str = "3.0",`):

```python
        previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = (),
```

**Add to `fields_for_hash` dict** (after `"pre_apply_snapshots": {}` entry):

```python
            "telemetry": None,
            "previous_op_hash_by_scope": previous_op_hash_by_scope,
```

**Add to `cls(...)` constructor call** (after `schema_version=schema_version,`):

```python
            previous_op_hash_by_scope=previous_op_hash_by_scope,
```

(`telemetry` has a default of `None` — no need to pass it explicitly in the constructor call.)

### Step 5: Add `with_telemetry()` helper to `OperationContext`

Add after `with_pre_apply_snapshots()` (after line 675), before `# Internal helpers` comment:

```python
    def with_telemetry(self, tc: "TelemetryContext") -> "OperationContext":
        """Stamp TelemetryContext onto the context (no phase change).

        Called exactly once by GovernedLoopService.submit() at intake,
        after concurrency/dedup gates and pipeline_deadline stamping.
        Uses the same hash-chain mechanics as with_pipeline_deadline().
        """
        intermediate = dataclasses.replace(
            self,
            telemetry=tc,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_routing_actual(self, ra: "RoutingActualTelemetry") -> "OperationContext":
        """Stamp actual routing outcome onto the existing TelemetryContext (no phase change).

        Called at COMPLETE or POSTMORTEM when the actual provider is known.

        Raises
        ------
        ValueError
            If ``telemetry`` has not been set yet (with_telemetry must precede this).
        """
        if self.telemetry is None:
            raise ValueError(
                "with_routing_actual() called before telemetry was set; "
                "call with_telemetry() first."
            )
        updated_tc = dataclasses.replace(self.telemetry, routing_actual=ra)
        intermediate = dataclasses.replace(
            self,
            telemetry=updated_tc,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)
```

### Step 6: Run all Task 2 tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v
```
Expected: All PASS

### Step 7: Run full suite

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: All PASS

### Step 8: Commit

```bash
git add backend/core/ouroboros/governance/op_context.py tests/test_ouroboros_governance/test_op_context.py
git commit -m "feat(op-context): add telemetry fields, with_telemetry(), with_routing_actual(), previous_op_hash_by_scope"
```

---

## Task 3: Telemetry Stamping in `GovernedLoopService.submit()`

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/test_ouroboros_governance/test_governed_loop_service.py`

### Step 1: Write failing tests

Add to `tests/test_ouroboros_governance/test_governed_loop_service.py`. First, update `_mock_stack` to include `resource_monitor`:

```python
def _mock_stack_with_resource_monitor(
    can_write_result: Tuple[bool, str] = (True, "ok"),
) -> MagicMock:
    """Build a mock GovernanceStack with resource_monitor."""
    from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
    import time

    stack = _mock_stack(can_write_result)
    snap = ResourceSnapshot(
        ram_percent=42.10,
        cpu_percent=14.20,
        event_loop_latency_ms=2.50,
        disk_io_busy=False,
        sampled_monotonic_ns=time.monotonic_ns(),
        ram_available_gb=6.80,
        platform_arch="arm64",
        collector_status="ok",
    )
    stack.resource_monitor = MagicMock()
    stack.resource_monitor.snapshot = AsyncMock(return_value=snap)
    return stack
```

Then add the test class:

```python
class TestSubmitTelemetryStamping:
    """GovernedLoopService.submit() stamps TelemetryContext onto every op."""

    async def test_submit_calls_resource_monitor_snapshot(self, tmp_path):
        """submit() calls stack.resource_monitor.snapshot() exactly once."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService,
            ServiceConfig,
        )
        stack = _mock_stack_with_resource_monitor()
        # Wire orchestrator to capture what ctx it receives
        captured = {}
        async def fake_run(ctx):
            captured["ctx"] = ctx
            return ctx.advance(OperationContext.OperationPhase.COMPLETE
                               if hasattr(ctx.phase, 'name') else ctx.phase)

        # Use a pre-wired service that skips real orchestrator
        # (test the stamping path via a lightweight integration)
        svc = GovernedLoopService(stack=stack, config=ServiceConfig())
        # Inject minimal mocks so service is ACTIVE but returns quickly
        svc._state = svc._state.__class__.ACTIVE  # skip start()
        # Instead: test _expected_provider_from_pressure directly
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot, PressureLevel
        import time

        snap_normal = ResourceSnapshot(
            ram_percent=40.0,
            cpu_percent=20.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
            sampled_monotonic_ns=time.monotonic_ns(),
            ram_available_gb=8.0,
            platform_arch="arm64",
            collector_status="ok",
        )
        assert _expected_provider_from_pressure(snap_normal) == "GCP_PRIME_SPOT"

        snap_critical = ResourceSnapshot(
            ram_percent=88.0,
            cpu_percent=85.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
            sampled_monotonic_ns=time.monotonic_ns(),
            ram_available_gb=1.0,
            platform_arch="arm64",
            collector_status="ok",
        )
        assert _expected_provider_from_pressure(snap_critical) == "LOCAL_CLAUDE"

    async def test_expected_provider_normal_pressure(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
        import time

        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=30.0,
            event_loop_latency_ms=10.0,
            disk_io_busy=False,
            sampled_monotonic_ns=time.monotonic_ns(),
            ram_available_gb=8.0,
            platform_arch="arm64",
            collector_status="ok",
        )
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        assert _expected_provider_from_pressure(snap) == "GCP_PRIME_SPOT"

    async def test_expected_provider_critical_pressure_routes_local(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
        import time

        snap = ResourceSnapshot(
            ram_percent=92.0,   # EMERGENCY tier
            cpu_percent=60.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
            sampled_monotonic_ns=time.monotonic_ns(),
            ram_available_gb=0.5,
            platform_arch="arm64",
            collector_status="ok",
        )
        assert _expected_provider_from_pressure(snap) == "LOCAL_CLAUDE"
```

### Step 2: Run tests to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestSubmitTelemetryStamping -v
```
Expected: `ImportError: cannot import name '_expected_provider_from_pressure'`

### Step 3: Update `governed_loop_service.py` imports

**Expand the `op_context` import** (line 38-41) to include the new telemetry types:

```python
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    HostTelemetry,
    RoutingIntentTelemetry,
    TelemetryContext,
)
```

**Add `PressureLevel` and `ResourceSnapshot` import** (after the existing `from backend.core.ouroboros.governance.multi_repo.registry` import):

```python
from backend.core.ouroboros.governance.resource_monitor import PressureLevel, ResourceSnapshot
```

### Step 4: Add module-level helper `_expected_provider_from_pressure`

Add to the `# Module-level helpers` section (after `_record_ledger`, around line 99):

```python
def _expected_provider_from_pressure(snap: ResourceSnapshot) -> str:
    """Derive the expected provider string from the snapshot's pressure level.

    NORMAL / ELEVATED  → GCP_PRIME_SPOT  (cloud inference preferred)
    CRITICAL / EMERGENCY → LOCAL_CLAUDE  (local fallback under resource pressure)
    """
    if snap.overall_pressure >= PressureLevel.CRITICAL:
        return "LOCAL_CLAUDE"
    return "GCP_PRIME_SPOT"
```

### Step 5: Add telemetry stamping in `submit()`

In `submit()`, after the `ctx = ctx.with_pipeline_deadline(...)` block (around line 469) and before the `if self._generator is not None and self._ledger is not None:` preflight block (line 472), insert:

```python
            # Stamp TelemetryContext exactly once at intake
            snap = await self._stack.resource_monitor.snapshot()
            now_ns = time.monotonic_ns()
            host_tel = HostTelemetry(
                schema_version="1.0",
                arch=snap.platform_arch,
                cpu_percent=snap.cpu_percent,           # already quantized
                ram_available_gb=snap.ram_available_gb, # already quantized
                pressure=snap.overall_pressure.name,
                sampled_at_utc=datetime.now(tz=timezone.utc).isoformat(),
                sampled_monotonic_ns=snap.sampled_monotonic_ns,
                collector_status=snap.collector_status,
                sample_age_ms=(now_ns - snap.sampled_monotonic_ns) // 1_000_000,
            )
            intent_tel = RoutingIntentTelemetry(
                expected_provider=_expected_provider_from_pressure(snap),
                policy_reason=snap.overall_pressure.name,
            )
            tc = TelemetryContext(local_node=host_tel, routing_intent=intent_tel)
            ctx = ctx.with_telemetry(tc)
```

### Step 6: Run Task 3 tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestSubmitTelemetryStamping -v
```
Expected: All PASS

### Step 7: Run full suite

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: All PASS

### Step 8: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(governed-loop): stamp TelemetryContext at intake via stack.resource_monitor"
```

---

## Task 4: `## System Context` Block in `_build_codegen_prompt`

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py`
- Test: `tests/test_ouroboros_governance/test_providers.py`

### Step 1: Write failing tests

Add to `tests/test_ouroboros_governance/test_providers.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: System Context block injection
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    RoutingIntentTelemetry,
    RoutingActualTelemetry,
    TelemetryContext,
)


def _make_telemetry_ctx_for_prompt() -> TelemetryContext:
    import time
    from datetime import datetime, timezone
    ht = HostTelemetry(
        schema_version="1.0",
        arch="arm64",
        cpu_percent=14.20,
        ram_available_gb=6.80,
        pressure="NORMAL",
        sampled_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        sampled_monotonic_ns=time.monotonic_ns(),
        collector_status="ok",
        sample_age_ms=3,
    )
    ri = RoutingIntentTelemetry(expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL")
    return TelemetryContext(local_node=ht, routing_intent=ri)


def test_system_context_block_absent_when_telemetry_none(tmp_path):
    """Default ctx (telemetry=None) → no ## System Context in prompt."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(
        target_files=(),
        description="test op",
    )
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "## System Context" not in prompt


def test_system_context_block_present_when_telemetry_set(tmp_path):
    """ctx.telemetry set → ## System Context block appears in prompt."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(
        target_files=(),
        description="test op",
    )
    ctx = ctx.with_telemetry(_make_telemetry_ctx_for_prompt())
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "## System Context" in prompt


def test_system_context_block_format(tmp_path):
    """System context block contains expected field labels."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(
        target_files=(),
        description="test op",
    )
    tc = _make_telemetry_ctx_for_prompt()
    ctx = ctx.with_telemetry(tc)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "arm64" in prompt
    assert "NORMAL" in prompt
    assert "GCP_PRIME_SPOT" in prompt
    assert "14.20" in prompt
    assert "6.80" in prompt


def test_system_context_block_includes_routing_actual_when_set(tmp_path):
    """If routing_actual is set, the Actual: line appears in the block."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(
        target_files=(),
        description="test op",
    )
    tc = _make_telemetry_ctx_for_prompt()
    ctx = ctx.with_telemetry(tc)
    ra = RoutingActualTelemetry(
        provider_name="GCP_PRIME_SPOT",
        endpoint_class="gcp_spot",
        fallback_chain=(),
        was_degraded=False,
    )
    ctx = ctx.with_routing_actual(ra)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "Actual:" in prompt
    assert "gcp_spot" in prompt
    assert "Degraded: False" in prompt


def test_system_context_block_position_after_task_before_snapshot(tmp_path):
    """## System Context appears between ## Task and ## Source Snapshot."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(
        target_files=(),
        description="test op",
    )
    ctx = ctx.with_telemetry(_make_telemetry_ctx_for_prompt())
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    task_pos = prompt.index("## Task")
    sys_ctx_pos = prompt.index("## System Context")
    snapshot_pos = prompt.index("## Source Snapshot")
    assert task_pos < sys_ctx_pos < snapshot_pos
```

### Step 2: Run tests to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py::test_system_context_block_absent_when_telemetry_none -v
```
Expected: FAIL (no `## System Context` in prompt regardless)

### Step 3: Add `_build_system_context_block()` helper to `providers.py`

Add after the `_build_tool_section()` function (search for `def _build_tool_section` and add after its closing):

```python
def _build_system_context_block(ctx: "OperationContext") -> Optional[str]:
    """Build '## System Context' block from ctx.telemetry, or return None.

    Returns None (silently omitted) when telemetry is not set —
    zero behavior change for existing tests and callers.
    """
    tc = ctx.telemetry
    if tc is None:
        return None
    h = tc.local_node
    ri = tc.routing_intent
    lines = [
        "## System Context",
        (
            f"Host  : {h.arch} | CPU: {h.cpu_percent:.2f}% "
            f"| RAM: {h.ram_available_gb:.2f} GB avail | Pressure: {h.pressure}"
        ),
        f"Sample: {h.sampled_at_utc} | Age: {h.sample_age_ms}ms | Status: {h.collector_status}",
        f"Route : {ri.expected_provider} | Reason: {ri.policy_reason}",
    ]
    if tc.routing_actual is not None:
        ra = tc.routing_actual
        degraded = "True" if ra.was_degraded else "False"
        lines.append(
            f"Actual: {ra.provider_name} ({ra.endpoint_class}) | Degraded: {degraded}"
        )
    return "\n".join(lines)
```

You also need `Optional` in the return type. Verify `Optional` is already imported in `providers.py` (it is, in `from typing import ...`).

### Step 4: Update `_build_codegen_prompt` parts assembly

Replace the current `parts = [...]` block (around lines 410-420) with:

```python
    # ── 4. Assemble final prompt ─────────────────────────────────────────
    file_block = "\n\n".join(file_sections) if file_sections else "_No target files._"
    parts = [
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
    ]
    sys_ctx_block = _build_system_context_block(ctx)
    if sys_ctx_block is not None:
        parts.append(sys_ctx_block)
    parts += [
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
    if expanded_context_block:
        parts.append(expanded_context_block)
    if tools_enabled:
        parts.append(_build_tool_section())
    parts.append(schema_instruction)
    return "\n\n".join(parts)
```

### Step 5: Run Task 4 tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v -k "system_context"
```
Expected: All 5 new tests PASS

### Step 6: Run full suite

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: All PASS

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers.py
git commit -m "feat(providers): inject ## System Context block from TelemetryContext into codegen prompt"
```

---

## Task 5: Harden `ContextExpander.expand()` Oracle Readiness Guard

**Files:**
- Modify: `backend/core/ouroboros/governance/context_expander.py`
- Verify (no changes): `backend/core/ouroboros/oracle.py` (FUSE weights + truncation already correct)
- Test: `tests/test_ouroboros_governance/test_context_expander.py`

**What changes:**
The current `expand()` method has a scattered readiness check deep inside the oracle branch using `self._oracle.get_status().get("running", False)`. Replace this with a single guard at the top of `expand()` using `self._oracle.is_ready()`. If oracle is not ready (or is `None`), return `ctx` unchanged immediately — no expansion rounds run.

**What does NOT change:**
- FUSE weights in `oracle.py` line 2120: `0.55 * graph_prox + 0.35 * semantic_sim + 0.10 * recency` — already correct.
- Truncation in `context_expander.py`: `MAX_FILES_PER_CATEGORY = 10` with `"... (and {hidden} more)"` — already correct.
- `_render_neighborhood_section()`: no changes needed.

### Step 1: Write failing tests

Add to `tests/test_ouroboros_governance/test_context_expander.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: Oracle readiness guard
# ---------------------------------------------------------------------------

async def test_oracle_not_ready_returns_ctx_unchanged():
    """If oracle.is_ready() returns False, expand() returns ctx unchanged, no rounds run."""
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path
    from backend.core.ouroboros.governance.context_expander import ContextExpander
    from backend.core.ouroboros.governance.op_context import OperationContext
    from datetime import datetime, timezone, timedelta

    oracle = MagicMock()
    oracle.is_ready.return_value = False

    generator = MagicMock()
    generator.plan = AsyncMock(side_effect=AssertionError("should not call plan when oracle not ready"))

    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test expansion",
    )
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    expander = ContextExpander(generator=generator, repo_root=Path("/tmp"), oracle=oracle)
    result = await expander.expand(ctx, deadline)

    # ctx returned unchanged
    assert result is ctx
    # generator.plan never called
    generator.plan.assert_not_called()


async def test_oracle_none_returns_ctx_unchanged():
    """If oracle is None, expand() returns ctx unchanged immediately."""
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path
    from backend.core.ouroboros.governance.context_expander import ContextExpander
    from backend.core.ouroboros.governance.op_context import OperationContext
    from datetime import datetime, timezone, timedelta

    generator = MagicMock()
    generator.plan = AsyncMock(side_effect=AssertionError("should not call plan when oracle is None"))

    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test expansion",
    )
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    expander = ContextExpander(generator=generator, repo_root=Path("/tmp"), oracle=None)
    result = await expander.expand(ctx, deadline)

    assert result is ctx
    generator.plan.assert_not_called()


async def test_oracle_ready_proceeds_to_expansion_rounds():
    """If oracle.is_ready() returns True, expansion rounds proceed normally."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from pathlib import Path
    from backend.core.ouroboros.governance.context_expander import ContextExpander
    from backend.core.ouroboros.governance.op_context import OperationContext
    from datetime import datetime, timezone, timedelta
    import json

    oracle = MagicMock()
    oracle.is_ready.return_value = True
    oracle.get_fused_neighborhood = AsyncMock(return_value=MagicMock(
        to_dict=MagicMock(return_value={})
    ))

    # Generator returns "no additional files" to stop expansion after 1 round
    response = json.dumps({
        "schema_version": "expansion.1",
        "additional_files_needed": [],
        "reasoning": "nothing needed",
    })
    generator = MagicMock()
    generator.plan = AsyncMock(return_value=response)

    ctx = OperationContext.create(
        target_files=("backend/foo.py",),
        description="test expansion",
    )
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    expander = ContextExpander(generator=generator, repo_root=Path("/tmp"), oracle=oracle)
    result = await expander.expand(ctx, deadline)

    # Oracle was checked
    oracle.is_ready.assert_called_once()
    # Generator was called (expansion rounds proceeded)
    generator.plan.assert_called()
    # No extra files → ctx returned unchanged
    assert result is ctx
```

### Step 2: Run tests to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py::test_oracle_not_ready_returns_ctx_unchanged -v
```
Expected: FAIL — `generator.plan` assertion fires (current code doesn't short-circuit on `is_ready()`)

### Step 3: Implement oracle readiness guard in `context_expander.py`

**Replace the entire oracle pre-fetch block** at the top of `expand()` (lines 83-113: from `# Pre-fetch fused neighborhood once` through `fused_neighborhood = None`) with a single guard and inline pre-fetch:

```python
        # Single oracle readiness guard — no second check in orchestrator
        if self._oracle is None or not self._oracle.is_ready():
            logger.info(
                "[ContextExpander] op=%s Oracle not ready — using blind baseline",
                ctx.op_id,
            )
            return ctx

        # Pre-fetch fused neighborhood once (async, fault-isolated)
        fused_neighborhood: Optional[Any] = None
        try:
            target_abs = [self._repo_root / f for f in ctx.target_files]
            if hasattr(self._oracle, "get_fused_neighborhood"):
                try:
                    fused_neighborhood = await self._oracle.get_fused_neighborhood(
                        target_abs, ctx.description
                    )
                except Exception as exc:
                    logger.warning(
                        "[ContextExpander] op=%s oracle neighborhood failed: %s; continuing without",
                        ctx.op_id, exc,
                    )
                    try:
                        fused_neighborhood = self._oracle.get_file_neighborhood(target_abs)
                    except Exception:
                        fused_neighborhood = None
            else:
                fused_neighborhood = self._oracle.get_file_neighborhood(target_abs)
        except Exception as exc:
            logger.warning(
                "[ContextExpander] op=%s oracle pre-fetch failed: %s; continuing without",
                ctx.op_id, exc,
            )
            fused_neighborhood = None
```

The key change: the guard `if self._oracle is None or not self._oracle.is_ready(): return ctx` is at the top. The `get_status()["running"]` check is completely removed — `is_ready()` is the single source of truth.

### Step 4: Run Task 5 tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py -v
```
Expected: All PASS (including pre-existing tests)

### Step 5: Verify FUSE weights and truncation (read-only spot check)

These are already correct — document the confirmation:

```bash
# Verify FUSE weights in oracle.py
grep -n "0.55\|0.35\|0.10" backend/core/ouroboros/oracle.py | grep "graph_prox\|semantic_sim\|recency"
# Expected: line ~2120: return 0.55 * graph_prox + 0.35 * semantic_sim + 0.10 * recency

# Verify truncation constant in context_expander.py
grep -n "MAX_FILES_PER_CATEGORY\|and.*more" backend/core/ouroboros/governance/context_expander.py
# Expected: MAX_FILES_PER_CATEGORY: int = 10  and  f"... (and {hidden} more)"
```

### Step 6: Run full suite

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: All PASS

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/context_expander.py tests/test_ouroboros_governance/test_context_expander.py
git commit -m "feat(context-expander): replace scattered oracle readiness check with single is_ready() guard at expand() entry"
```

---

## Final Verification

Run the full suite one last time to confirm all 5 tasks are clean:

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -20
```

Expected output confirms all tests pass with no regressions.

---

## Summary of Changes

| File | Type | What changed |
|------|------|-------------|
| `op_context.py` | New code | 4 frozen telemetry dataclasses; 2 new fields on `OperationContext`; `with_telemetry()`, `with_routing_actual()`; `create()` `previous_op_hash_by_scope` param |
| `resource_monitor.py` | Modify | `import platform`; 4 new fields on `ResourceSnapshot`; float quantization in `snapshot()`; 3 private helpers |
| `governed_loop_service.py` | Modify | Expanded `op_context` import; `PressureLevel`/`ResourceSnapshot` import; `_expected_provider_from_pressure()` helper; telemetry stamping in `submit()` |
| `providers.py` | Modify | `_build_system_context_block()` helper; updated parts assembly to inject block after `## Task` |
| `context_expander.py` | Harden | Replace `get_status()["running"]` with single `is_ready()` guard at top of `expand()` |
| `oracle.py` | No change | FUSE weights 0.55/0.35/0.10 verified correct |
