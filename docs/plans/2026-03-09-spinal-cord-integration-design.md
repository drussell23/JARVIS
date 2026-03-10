# Spinal Cord Integration Design
## FUSE Context Expander + Dual-Telemetry + Concurrent Hash Chaining

> **Status:** Approved for implementation
> **Date:** 2026-03-09
> **Scope:** VERIFY and HARDEN existing wiring; add telemetry schema; enforce FUSE weights and truncation

---

## Goal

Wire the FUSE context expander (already staged) to the governed pipeline, and inject dual-telemetry (local hardware state + routing decision) into every OperationContext so J-Prime understands its physical constraints and the causal audit trail is cryptographically sound.

---

## Architecture

Five files are touched. Two (oracle.py, context_expander.py) are harden-only. Three receive new code.

```
op_context.py          ← new frozen telemetry dataclasses + previous_op_hash_by_scope
resource_monitor.py    ← float quantization + sampled_monotonic_ns + ram_available_gb
governed_loop_service.py ← submit() stamps TelemetryContext via stack.resource_monitor
providers.py           ← _build_codegen_prompt renders ## System Context block
context_expander.py    ← harden: oracle readiness guard, 0.55/0.35/0.10 weights, truncation
```

---

## Component 1: Telemetry Types in `op_context.py`

### New frozen dataclasses (inserted before OperationContext)

```python
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
    sample_age_ms: int            # (time.monotonic_ns() - sampled_monotonic_ns) // 1_000_000


@dataclass(frozen=True)
class RoutingIntentTelemetry:
    """Routing decision EXPECTED at FSM intake (before any execution)."""
    expected_provider: str        # e.g. "GCP_PRIME_SPOT", "LOCAL_CLAUDE"
    policy_reason: str            # e.g. "PRIMARY_AVAILABLE", "LOCAL_MEMORY_PRESSURE"


@dataclass(frozen=True)
class RoutingActualTelemetry:
    """Routing outcome AFTER execution (stamped at COMPLETE or POSTMORTEM)."""
    provider_name: str            # actual provider used
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

### OperationContext additions

Two new optional fields with safe defaults (all existing callsites unaffected):

```python
telemetry: Optional[TelemetryContext] = None
previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = ()
# e.g. (("jarvis", "abc123..."), ("prime", "def456..."))
# Frozen-safe representation of Dict[repo_name, last_context_hash]
```

### New mutation helpers

Same hash-chain pattern as `with_pipeline_deadline()`:

```python
def with_telemetry(self, tc: TelemetryContext) -> OperationContext:
    """Stamp TelemetryContext (no phase change). Called once at intake."""

def with_routing_actual(self, ra: RoutingActualTelemetry) -> OperationContext:
    """Stamp actual routing outcome (no phase change). Called at COMPLETE."""
```

### `create()` update

Add `previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = ()` parameter so the Ledger can inject cross-op causality when spawning a new context.

---

## Component 2: Float Quantization in `resource_monitor.py`

`ResourceSnapshot` gains three new fields:

```python
sampled_monotonic_ns: int = 0    # set by snapshot(); enables age computation
ram_available_gb: float = 0.0    # psutil.virtual_memory().available / 1e9, quantized
platform_arch: str = ""          # platform.machine()
collector_status: str = "ok"     # "ok" if psutil available, "partial" otherwise
```

In `ResourceMonitor.snapshot()`, quantize ALL floats before instantiation:

```python
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
```

New private helpers: `_get_ram_available_gb()`, `_get_platform_arch()`, `_get_collector_status()`.

### Why this layer

Quantization at source (not in HostTelemetry) means every consumer of ResourceSnapshot gets canonical values. Hash churn is eliminated at the measurement layer, not patched downstream.

---

## Component 3: Telemetry Stamping in `governed_loop_service.py`

In `submit()`, after the concurrency gate and before `_preflight_check`:

```python
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

`routing_actual` is stamped at the end of `_run_operation()` in the orchestrator when the actual provider is known.

### Concurrent hash chaining

The Ledger maintains `_op_hashes_by_scope: Dict[str, str]` in memory. At each new op submission:

```python
ctx = OperationContext.create(
    ...,
    previous_op_hash_by_scope=tuple(self._ledger.last_hashes_for_scope(ctx.repo_scope)),
)
```

At COMPLETE/APPLY, Ledger updates:
```python
for repo in ctx.repo_scope:
    self._op_hashes_by_scope[repo] = ctx.context_hash
```

Parallel ops on different repo scopes never contend — each has its own hash chain per repo key.

---

## Component 4: Prompt Injection in `providers.py`

`_build_codegen_prompt()` gains a `## System Context` section rendered when `ctx.telemetry is not None`, inserted in the `parts` list immediately after the task description, before any file sections:

```
## System Context
Host  : arm64 macOS | CPU: 14.20% | RAM: 6.80 GB avail | Pressure: NORMAL
Sample: 2026-03-09T12:00:00Z | Age: 3ms | Status: ok
Route : GCP_PRIME_SPOT (gcp_spot) | Reason: PRIMARY_AVAILABLE | Degraded: False
```

If `telemetry` is None the section is silently omitted — zero behavior change for existing tests.

---

## Component 5: FUSE Context Expander — Harden Only

### Oracle readiness guard (single location: `context_expander.py`)

```python
async def expand(self, ctx, deadline):
    if self._oracle is None or not self._oracle.is_ready():
        logger.info("[ContextExpander] Oracle not ready — using blind baseline")
        return ctx   # advance to GENERATE unchanged; orchestrator does NOT check readiness
```

The orchestrator does NOT add a readiness check. One location, no drift.

### Scoring weights verification

`oracle.py` `get_fused_neighborhood()` must use:
```
final_score = 0.55 * graph_proximity + 0.35 * semantic_similarity + 0.10 * recency
```
If the current weights differ, update them.

### Truncation enforcement

`FileNeighborhood` rendering must hard-cap at 10 paths per category:
```python
cap = 10
if len(paths) > cap:
    n_extra = len(paths) - cap
    rendered = paths[:cap] + [f"... (and {n_extra} more)"]
```
Applies to every structural category (imports, importers, callers, callees) and `semantic_support`.

---

## Testing Strategy

- All new dataclasses: unit tests for hash stability (same inputs → same `context_hash`), quantization (float inputs produce 2dp outputs), and `sample_age_ms` computation
- `with_telemetry` / `with_routing_actual`: verify hash chain advances correctly
- `previous_op_hash_by_scope`: concurrent ledger test — two ops on different scopes don't clobber each other's chain
- `_build_codegen_prompt` with and without telemetry: telemetry block present/absent
- ContextExpander cold-start fallback: oracle.is_ready()=False → returns ctx unchanged, no exception
- FUSE weights: mock graph + semantic scores, assert weighted output matches 0.55/0.35/0.10 formula
- Truncation: feed >10 paths per category, assert "... (and N more)" appended

---

## Non-Goals (explicitly excluded)

- Voice authentication integration (separate track)
- External limbs (Gmail, Chrome, AppleScript)
- Live file watcher / incremental oracle updates (existing `_oracle_index_loop` is sufficient)
- Changes to `TheOracle.__init__` or multi-repo seeding (already wired correctly)
