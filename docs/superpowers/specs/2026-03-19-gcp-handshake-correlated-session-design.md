# GCP Readiness Handshake — Correlated Session, Failure Taxonomy & Autonomous Recovery

**Date:** 2026-03-19
**Version:** v297.0
**Status:** Approved for implementation
**Spec authors:** Engineering
**Related specs:** `2026-03-19-gcp-operation-lifecycle-design.md`

---

## 1. Problem Statement

### 1.1 Immediate Failure (Observed)

Every JARVIS startup emits the following log chain:

```
WARNING  backend.core.gcp_vm_manager      [InvincibleNode] Cannot read VM metadata for .
WARNING  backend.core.gcp_readiness_lease  Handshake step capabilities failed:
                                           metadata_unavailable_golden_exists
                                           (class=ReadinessFailureClass.SCHEMA_MISMATCH)
WARNING  backend.core.startup_routing_policy  GCP handshake failed: lease acquisition failed:
                                              schema_mismatch
```

This is a **false negative**. The VM is healthy and running. The failure is caused by:

1. `GCPVMReadinessProber.probe_capabilities()` calls `vm_manager.check_lineage("", None)` — empty instance name, no metadata.
2. `_check_vm_golden_image_lineage()` receives `vm_metadata=None` and enters a conservative fallback path: "if a golden image exists, recommend recreation."
3. The prober hardcodes `ReadinessFailureClass.SCHEMA_MISMATCH` regardless of the actual reason string returned.
4. The routing policy receives a generic `handshake_failed` signal and falls back to LOCAL/CLOUD with no differentiation, no autonomous recovery.

### 1.2 Root Diseases (Not Symptoms)

| # | Disease | Effect |
|---|---------|--------|
| D1 | No correlated session across handshake steps — HEALTH acquires live instance identity but does not pass it to CAPABILITIES or WARM_MODEL | CAPABILITIES always probes with empty context → false negative on every startup |
| D2 | `check_lineage("", None)` is a legal call — no contract enforcement | Callers omit required context silently |
| D3 | `metadata_fetch_failed` (transient infra) classified identically to `vm_not_from_golden_image` (genuine lineage mismatch) — both produce `SCHEMA_MISMATCH` | Wrong failure class → wrong recovery path → transient blip triggers fallback that should trigger RETRY_SHORT |
| D4 | Routing policy branches on generic `handshake_failed`, not on `(step, failure_class)` | `LINEAGE_MISMATCH` (should trigger autonomous background recreation) is treated identically to `NETWORK` (should retry in 30s) |
| D5 | `LINEAGE_MISMATCH` with `should_recreate=True` is computed, logged, and discarded — no autonomous remediation | Human must intervene; system stays on LOCAL/CLOUD until manual restart |
| D6 | GCP health probe scheduled at startup regardless of CPU/memory pressure | 100% CPU during model loading starves the event loop, causing health check timeout (15s), which feeds back as a spurious HEALTH failure |
| D7 | No boot-time contract check between JARVIS and JARVIS-Prime | API version drift discovered at first inference request, not at startup |

---

## 2. Architecture

### 2.1 File Map

No new files. All changes extend existing modules at correct ownership boundaries.

| File | Change Type | Responsibility |
|------|-------------|----------------|
| `backend/core/gcp_readiness_lease.py` | Extend | Add `HandshakeSession`; expand `ReadinessFailureClass`; thread session through `acquire()`; update `ReadinessProber` ABC |
| `backend/core/gcp_vm_readiness_prober.py` | Rewrite internals | `probe_health()` populates session with instance identity; `probe_capabilities()` consumes session; classify failure_class from reason string |
| `backend/core/gcp_vm_manager.py` | Harden contract | `check_lineage()` raises `ContractViolationError` on empty name; `_check_vm_golden_image_lineage()` auto-fetches metadata before conservative fallback; returns `metadata_fetch_failed` as distinct reason |
| `backend/core/startup_routing_policy.py` | Extend | Add `RecoveryStrategy` enum; add `(HandshakeStep, ReadinessFailureClass) → RecoveryStrategy` matrix; fire `RECREATE_VM_ASYNC` as background task + continue fallback |
| `backend/core/startup_orchestrator.py` | Extend | Add `_ProbeReadinessBudget` gate before GCP probe; wire `RECREATE_VM_ASYNC` to `vm_manager.ensure_static_vm_ready()` background task; upgrade routing on completion |

---

## 3. Component Designs

### 3.1 HandshakeSession

**Location:** `backend/core/gcp_readiness_lease.py`
**Cures:** D1

```python
@dataclass
class HandshakeSession:
    """Correlated context for a single lease acquisition attempt.

    Created by GCPReadinessLease.acquire() before the first probe step.
    Populated progressively: HEALTH writes instance identity;
    CAPABILITIES and WARM_MODEL read it. No step re-derives identity.
    """
    session_id: str                                      # uuid4 — ties all three steps
    lease_id: str                                        # uuid4 — owning lease
    # Populated by HEALTH step
    instance_name: str = ""
    instance_id: str = ""                                # GCP numeric ID
    zone: str = ""
    endpoint: str = ""                                   # "host:port" that passed health
    # Timing
    started_at: float = field(default_factory=time.monotonic)
    # step_timestamps intentionally omitted — unused per YAGNI; add when observability
    # requires per-step duration breakdown
```

**Removed from session:** `should_recreate` and `recreate_reason`. The routing matrix drives the `RECREATE_VM_ASYNC` decision exclusively — these fields would be populated but never consumed. Recovery action is determined by `(step, failure_class)` from `HandshakeResult`, not by session state.

**Invariant:** By the time `probe_capabilities()` is called, `session.instance_name` is non-empty. Enforced by `acquire()` which validates the session after HEALTH before calling CAPABILITIES.

---

### 3.2 ReadinessProber ABC Update

**Location:** `backend/core/gcp_readiness_lease.py`
**Cures:** D1

```python
class ReadinessProber(ABC):
    @abstractmethod
    async def probe_health(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,
    ) -> HandshakeResult: ...

    @abstractmethod
    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,   # session.instance_name is guaranteed non-empty
    ) -> HandshakeResult: ...

    @abstractmethod
    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,
    ) -> HandshakeResult: ...
```

---

### 3.3 Failure Taxonomy Expansion

**Location:** `backend/core/gcp_readiness_lease.py`
**Cures:** D2, D3

```python
class ReadinessFailureClass(str, Enum):
    # Existing — retained for backwards compatibility
    NETWORK            = "network"            # TCP/DNS connectivity failure
    QUOTA              = "quota"              # GCP quota exceeded
    RESOURCE           = "resource"           # CPU/memory insufficient on VM
    PREEMPTION         = "preemption"         # Preemptible VM killed
    TIMEOUT            = "timeout"            # Step exceeded time budget
    SCHEMA_MISMATCH    = "schema_mismatch"    # Retained for genuine lineage mismatch (external consumers)
    # New — precise classification
    TRANSIENT_INFRA    = "transient_infra"    # Metadata fetch failed, GCP API blip — retry
    LINEAGE_MISMATCH   = "lineage_mismatch"   # VM from wrong/outdated golden image — recreate
    CONTRACT_VIOLATION = "contract_violation" # Programming error: empty instance name
```

**Reason string → FailureClass mapping** (in `GCPVMReadinessProber`, not in `gcp_vm_manager`).
The mapping also drives routing strategy when the failure class is looked up in the matrix.
**Note on `metadata_unavailable_golden_exists`:** The existing code already sets `should_recreate=True` for this reason. Retrying with `RETRY_SHORT` would re-enter the same `None`-metadata path and loop. `RECREATE_VM_ASYNC` is correct — can't verify lineage, but a golden image exists that the VM could be built from.

| Reason returned by `check_lineage` | `should_recreate` | Mapped FailureClass | Recovery |
|------------------------------------|-------------------|---------------------|----------|
| `metadata_fetch_failed` | False | `TRANSIENT_INFRA` | RETRY_SHORT |
| `metadata_unavailable_golden_exists` | True | `TRANSIENT_INFRA` | RECREATE_VM_ASYNC (see note) |
| `metadata_unavailable_no_golden` | False | `TRANSIENT_INFRA` | RETRY_SHORT |
| `vm_not_from_golden_image` | True | `LINEAGE_MISMATCH` | RECREATE_VM_ASYNC |
| `golden_image_outdated` | True | `LINEAGE_MISMATCH` | RECREATE_VM_ASYNC |
| `golden_image_matches` | False | — (PASS) | — |
| `golden_image_disabled` | False | — (PASS) | — |
| `golden_image_stale` | False | — (PASS) | — |
| `no_golden_image_available` | False | — (PASS) | — |

---

### 3.4 Contract Enforcement on `check_lineage`

**Location:** `backend/core/gcp_vm_manager.py`
**Cures:** D2

```python
class ContractViolationError(Exception):
    """Raised when check_lineage() is called with a contract violation.

    Callers must provide a non-empty instance_name acquired from the
    HEALTH step's instance identity probe. Passing "" is a programming
    error, not a recoverable condition.
    """

async def check_lineage(
    self, instance_name: str, vm_metadata: Optional[Dict[str, str]] = None
) -> Tuple[bool, str]:
    """Public API for readiness prober.

    Raises ContractViolationError if instance_name is empty.
    When vm_metadata is None (not passed), auto-fetches before lineage check.
    Returns (should_recreate: bool, reason: str).
    """
    if not instance_name:
        raise ContractViolationError(
            "check_lineage() requires a non-empty instance_name. "
            "The HEALTH step must populate session.instance_name before "
            "capabilities probe is called."
        )
    return await self._check_vm_golden_image_lineage(instance_name, vm_metadata)
```

**Metadata auto-fetch** in `_check_vm_golden_image_lineage`:

```python
# When vm_metadata is not provided, attempt to fetch before conservative fallback
if vm_metadata is None:
    if instance_name:
        try:
            _, vm_metadata, _ = await self._describe_instance_full(instance_name)
        except Exception as e:
            # Genuine fetch failure — distinct from lineage mismatch
            _log.warning(
                "[InvincibleNode] metadata_fetch_failed for %s: %s",
                instance_name, e,
            )
            return False, "metadata_fetch_failed"   # TRANSIENT_INFRA, not SCHEMA_MISMATCH

    # Still None after fetch attempt (instance truly not available)
    _log.warning(
        "⚠️ [InvincibleNode] Cannot read VM metadata for %s. "
        "Checking golden image availability to decide.", instance_name,
    )
    try:
        builder = self.get_golden_image_builder()
        latest = await builder.get_latest_golden_image()
        if latest and not latest.is_stale(self.config.golden_image_max_age_days):
            return True, "metadata_unavailable_golden_exists"   # TRANSIENT_INFRA
    except Exception as e:
        _log.debug("[InvincibleNode] Golden image check failed: %s", e)
    return False, "metadata_unavailable_no_golden"              # TRANSIENT_INFRA
```

---

### 3.5 Routing Strategy Matrix

**Location:** `backend/core/startup_routing_policy.py`
**Cures:** D4, D5

```python
class RecoveryStrategy(str, Enum):
    RETRY_SHORT       = "retry_short"       # Retry within 30s — transient network/infra
    RETRY_LONG        = "retry_long"        # Retry in 60-120s — resource contention
    RECREATE_VM_ASYNC = "recreate_vm_async" # Fire background recreation, use fallback now
    FALLBACK_LOCAL    = "fallback_local"    # Use local Llama, no further GCP retry
    FALLBACK_CLOUD    = "fallback_cloud"    # Use Claude API, no further GCP retry
    ABORT             = "abort"             # Permanent — do not retry

_RECOVERY_MATRIX: Dict[
    Tuple[HandshakeStep, ReadinessFailureClass], RecoveryStrategy
] = {
    # HEALTH step
    (HandshakeStep.HEALTH, ReadinessFailureClass.NETWORK):            RecoveryStrategy.RETRY_SHORT,
    (HandshakeStep.HEALTH, ReadinessFailureClass.TIMEOUT):            RecoveryStrategy.RETRY_SHORT,
    (HandshakeStep.HEALTH, ReadinessFailureClass.RESOURCE):           RecoveryStrategy.RETRY_LONG,
    (HandshakeStep.HEALTH, ReadinessFailureClass.PREEMPTION):         RecoveryStrategy.RETRY_LONG,
    (HandshakeStep.HEALTH, ReadinessFailureClass.QUOTA):              RecoveryStrategy.FALLBACK_CLOUD,
    # CAPABILITIES step
    (HandshakeStep.CAPABILITIES, ReadinessFailureClass.TRANSIENT_INFRA):   RecoveryStrategy.RETRY_SHORT,
    (HandshakeStep.CAPABILITIES, ReadinessFailureClass.LINEAGE_MISMATCH):  RecoveryStrategy.RECREATE_VM_ASYNC,
    (HandshakeStep.CAPABILITIES, ReadinessFailureClass.SCHEMA_MISMATCH):   RecoveryStrategy.RECREATE_VM_ASYNC,
    (HandshakeStep.CAPABILITIES, ReadinessFailureClass.CONTRACT_VIOLATION): RecoveryStrategy.ABORT,
    # WARM_MODEL step
    (HandshakeStep.WARM_MODEL, ReadinessFailureClass.NETWORK):        RecoveryStrategy.RETRY_SHORT,
    (HandshakeStep.WARM_MODEL, ReadinessFailureClass.TIMEOUT):        RecoveryStrategy.RETRY_SHORT,
    (HandshakeStep.WARM_MODEL, ReadinessFailureClass.RESOURCE):       RecoveryStrategy.RETRY_LONG,
}
# Default for unmatched (step, class): RecoveryStrategy.FALLBACK_LOCAL
# Any unmatched combination logs a WARNING before returning default.
_RECOVERY_MATRIX_DEFAULT = RecoveryStrategy.FALLBACK_LOCAL

# Maximum consecutive RETRY_SHORT/RETRY_LONG outcomes before escalating to
# FALLBACK_LOCAL. Prevents retry storms on persistent but non-fatal failures.
MAX_RETRY_ATTEMPTS_PER_HANDSHAKE = 3
```

**`RECREATE_VM_ASYNC` execution path:**

```
routing_policy.select_strategy(step, failure_class)
    → RecoveryStrategy.RECREATE_VM_ASYNC
    → immediately select FALLBACK_LOCAL (or FALLBACK_CLOUD if local unavailable)
    → fire asyncio.create_task(
          startup_orchestrator._recreate_vm_and_upgrade_routing(session)
      )

_recreate_vm_and_upgrade_routing(session):
    # ensure_static_vm_ready signature (existing, extended with recreate flag):
    #   async def ensure_static_vm_ready(
    #       self,
    #       port: Optional[int] = None,
    #       timeout: Optional[float] = None,
    #       progress_callback: Optional[Callable] = None,
    #       activity_callback: Optional[Callable] = None,
    #       recreate: bool = False,          # NEW: force recreation, skip start-existing path
    #   ) -> Tuple[bool, Optional[str], str]:
    #       Returns: (success, endpoint_host_or_None, status_message)
    #
    # The `recreate=True` flag is a new parameter added by this spec.
    # When True, the method bypasses the "restart existing instance" path and
    # goes directly to the recreation flow (delete → recreate from golden).

    success, host, status = await vm_manager.ensure_static_vm_ready(recreate=True)
    if success and host:
        # Use existing signal method — parse port from session or use config default
        port = session.endpoint.split(":")[-1] if ":" in session.endpoint else vm_manager.config.port
        routing_policy.signal_gcp_vm_ready(host, int(port))
        # Routing upgrades to GCP_PRIME seamlessly
    else:
        _log.warning("[RecreateVM] Recreation failed: %s — staying on fallback", status)
```

**`signal_gcp_vm_ready(host, port)`** is the existing method on `StartupRoutingPolicy`. This spec does not add a new method — it uses the existing signal.

The background task is bounded: it uses `asyncio.shield()` on the recreation future so that if the startup coroutine is cancelled, recreation continues independently. Concurrent `RECREATE_VM_ASYNC` calls are guarded by the existing `DistributedLockManager` (key: `gcp_vm_recreate`) — see residual risks.

---

### 3.6 Startup Backpressure Gate

**Location:** `backend/core/startup_orchestrator.py`
**Cures:** D6

```python
class _ProbeReadinessBudget:
    """Gates GCP probe scheduling behind CPU/memory thresholds.

    Reads CPU and memory directly via psutil (same library already used by
    IntelligentMemoryController in process_cleanup_manager.py). Does NOT
    depend on the controller being initialized — safe to call before Zone 5.
    Does not block indefinitely — falls through with warning after MAX_WAIT.
    """
    CPU_THRESHOLD  = 85.0   # percent — don't probe while CPU > 85%
    MEM_THRESHOLD  = 88.0   # percent — don't probe while memory > 88%
    POLL_INTERVAL  = 2.0    # seconds between pressure checks
    MAX_WAIT       = 60.0   # seconds before probing regardless (safety valve)

    @staticmethod
    def _read_current_pressure() -> Tuple[float, float]:
        """Returns (cpu_percent, mem_percent).

        Uses psutil directly. `interval=None` returns cached value from last
        psutil.cpu_percent() call, which is safe and non-blocking.
        Falls back to (0.0, 0.0) if psutil is unavailable (test environments).
        """
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return cpu, mem
        except Exception:
            return 0.0, 0.0   # fail open — don't block probe if psutil unavailable

    async def wait_for_probe_slot(self) -> bool:
        """Await CPU/memory relief. Returns True if slot acquired, False if timed out."""
        cpu, mem = self._read_current_pressure()
        deadline = time.monotonic() + self.MAX_WAIT
        while time.monotonic() < deadline:
            if cpu <= self.CPU_THRESHOLD and mem <= self.MEM_THRESHOLD:
                return True
            await asyncio.sleep(self.POLL_INTERVAL)
            cpu, mem = self._read_current_pressure()
        _log.warning(
            "[ProbeGate] Max wait exceeded (%.0fs) — probing under pressure "
            "(cpu=%.1f%%, mem=%.1f%%)", self.MAX_WAIT, cpu, mem,
        )
        return False
```

The gate applies **only to the initial probe scheduling**. Retries driven by `RETRY_SHORT`/`RETRY_LONG` strategies bypass the gate — the system is already in fallback, GCP probe is background work, not time-critical.

---

### 3.7 Observability

**Structured log fields** on every `HandshakeResult`:

```
[GCP_LEASE] session=<uuid> step=CAPABILITIES result=FAIL
            class=TRANSIENT_INFRA reason=metadata_fetch_failed
            instance=jarvis-prime-stable zone=us-central1-b
            elapsed_ms=234 strategy=RETRY_SHORT
```

**Terminal acquisition log:**

```
[GCP_LEASE] session=<uuid> TERMINAL outcome=failed_at_CAPABILITIES
            final_strategy=RETRY_SHORT next_attempt_in=30s
            instance=jarvis-prime-stable total_ms=1847
```

**Metrics counters** (thread-safe, appended to existing counter infrastructure):

| Counter | Labels |
|---------|--------|
| `gcp_handshake_step_total` | `step`, `result`, `failure_class` |
| `gcp_recovery_strategy_total` | `strategy` |
| `gcp_recreate_vm_async_triggered_total` | `trigger_reason` |
| `gcp_probe_gate_waited_total` | `outcome` (`acquired`, `timed_out`) |

---

### 3.8 Cross-Repo Compatibility Guard

**Location:** `backend/core/gcp_vm_readiness_prober.py` (HEALTH step extension)
**Cures:** D7
**Scope:** JARVIS ↔ JARVIS-Prime only (Reactor Core is separate CI/CD)

**Capabilities cache interaction:** `GCPVMReadinessProber` has a TTL-based cache for CAPABILITIES probe results (verified in code). This cache must be **invalidated at the start of every new `HandshakeSession`** to prevent a cached result from bypassing the `session.instance_name` fix on retries. Implementation: in `acquire()`, before calling `probe_capabilities()`, call `prober.invalidate_capabilities_cache()` (new method, one line: `self._capabilities_cache = None`).

---

JARVIS-Prime exposes `/v1/contract` (new endpoint on Prime server):

```json
{
  "api_version": "1.3.0",
  "schema_version": "2026-03-19",
  "model_capabilities": ["inference", "vision"],
  "min_client_version": "1.2.0"
}
```

JARVIS-side constants (module-level in prober — a code contract, not config):

```python
_MIN_PRIME_API_VERSION = (1, 2, 0)
_REQUIRED_CAPABILITIES = frozenset({"inference"})
```

**Soft/hard fail boundary for `/v1/contract`:**

| Condition | Treatment | Rationale |
|-----------|-----------|-----------|
| Endpoint returns 404 / connection refused | **Soft fail** — log INFO, skip check, continue HEALTH | Expected: Prime not yet updated to expose endpoint |
| JSON parse error or unexpected shape | **Soft fail** — log WARNING, skip check, continue HEALTH | Defensive: don't break on schema drift in contract endpoint itself |
| `api_version` below `_MIN_PRIME_API_VERSION` | **Hard fail** — `ABORT` + `CONTRACT_VIOLATION` | Deployment mismatch; no retry (not a transient condition) |
| Required capability missing from `model_capabilities` | **Hard fail** — `ABORT` + `CONTRACT_VIOLATION` | Same — deployment mismatch |
| Network timeout on `/v1/contract` fetch | **Soft fail** — log WARNING, skip check, continue HEALTH | Health endpoint passed; contract fetch timeout is non-fatal |

The contract check is a one-shot check within a session — no retry on hard fail.

---

## 4. Data Flow

```
GCPReadinessLease.acquire(host, port)
    │
    ├── session = HandshakeSession(session_id=uuid4(), lease_id=self.id)
    │
    ├── result = prober.probe_health(host, port, timeout, session)
    │   ├── HTTP GET /v1/health → verdict
    │   ├── GCP API: describe_instance(host_ip) → instance_name, instance_id, zone
    │   ├── HTTP GET /v1/contract → api_version check (optional, soft fail)
    │   ├── session.instance_name = <populated>
    │   └── → HandshakeResult
    │
    ├── [validate: session.instance_name must be non-empty before next step]
    │
    ├── [prober.invalidate_capabilities_cache()]  ← clears TTL cache before each session
    │
    ├── result = prober.probe_capabilities(host, port, timeout, session)
    │   ├── vm_manager.check_lineage(session.instance_name, None)
    │   │   ├── [ContractViolationError if instance_name empty — never happens post-fix]
    │   │   ├── _describe_instance_full(instance_name) → vm_metadata
    │   │   └── → (should_recreate, reason_string)
    │   ├── _REASON_TO_FAILURE_CLASS[reason_string] → failure_class
    │   └── → HandshakeResult(failure_class=<correct>)
    │         [session.should_recreate removed — routing matrix drives action]
    │
    ├── result = prober.probe_warm_model(host, port, timeout, session)
    │   └── HTTP POST /v1/warm_check
    │
    └── [all pass] → lease ACTIVE, session stored on lease for routing reference

startup_routing_policy.signal_gcp_handshake_failed(step, failure_class, session)
    ├── strategy = _RECOVERY_MATRIX.get((step, failure_class), DEFAULT)
    ├── if strategy == RECREATE_VM_ASYNC:
    │   ├── select FALLBACK_LOCAL immediately
    │   └── asyncio.create_task(_recreate_vm_and_upgrade_routing(session))
    ├── if strategy in (RETRY_SHORT, RETRY_LONG):
    │   └── schedule next probe attempt with backoff
    └── if strategy in (FALLBACK_LOCAL, FALLBACK_CLOUD, ABORT):
        └── select target, no further GCP probing
```

---

## 5. Test Requirements

All tests must be hermetic (no real GCP calls).

| # | Test | What it verifies |
|---|------|------------------|
| T1 | `test_empty_instance_name_raises_contract_violation` | `check_lineage("", None)` raises `ContractViolationError` |
| T2 | `test_health_populates_session_instance_name` | After `probe_health()`, `session.instance_name` is non-empty |
| T3 | `test_capabilities_uses_session_instance_name` | `probe_capabilities()` passes `session.instance_name` to `check_lineage()`, not `""` |
| T4 | `test_metadata_fetch_failure_classified_transient_infra` | When `_describe_instance_full` raises, reason=`metadata_fetch_failed`, class=`TRANSIENT_INFRA` |
| T5 | `test_genuine_lineage_mismatch_classified_correctly` | When `check_lineage` returns `vm_not_from_golden_image`, class=`LINEAGE_MISMATCH` |
| T6 | `test_routing_matrix_lineage_mismatch_triggers_recreate_async` | Strategy for `(CAPABILITIES, LINEAGE_MISMATCH)` is `RECREATE_VM_ASYNC` |
| T7 | `test_routing_matrix_transient_infra_triggers_retry_short` | Strategy for `(CAPABILITIES, TRANSIENT_INFRA)` is `RETRY_SHORT` |
| T8 | `test_recreate_vm_async_fires_background_task_and_continues_fallback` | On `RECREATE_VM_ASYNC`, routing returns `FALLBACK_LOCAL` immediately AND fires background task |
| T9 | `test_routing_upgrades_to_gcp_prime_after_recreation_success` | Background recreation completes → routing policy upgrades to `GCP_PRIME` |
| T10 | `test_probe_gate_delays_probe_under_cpu_pressure` | With CPU=100%, probe is delayed until pressure drops |
| T11 | `test_probe_gate_falls_through_after_max_wait` | Gate expires after 60s, probe fires with warning |
| T12 | `test_contract_check_abort_on_version_mismatch` | API version below minimum → `ABORT` + `CONTRACT_VIOLATION` |
| T13 | `test_correlated_session_id_in_all_step_logs` | All three step logs contain the same `session_id` |
| T14 | `test_recovery_matrix_has_entries_for_expected_health_classes` | Matrix has entries for exactly `(HEALTH, NETWORK)`, `(HEALTH, TIMEOUT)`, `(HEALTH, RESOURCE)`, `(HEALTH, PREEMPTION)`, `(HEALTH, QUOTA)` — the five classes reachable at HEALTH step; new classes `TRANSIENT_INFRA`, `LINEAGE_MISMATCH` fall to default at HEALTH |
| T15 | `test_retry_strategy_bounded_by_max_attempts` | After `MAX_RETRY_ATTEMPTS_PER_HANDSHAKE = 3` consecutive `RETRY_SHORT` results, orchestrator escalates to `FALLBACK_LOCAL` — does not loop forever |

---

## 6. Residual Risks & Next Hardening Backlog

| Risk | Severity | Next step |
|------|----------|-----------|
| `/v1/contract` endpoint not yet on JARVIS-Prime — health step will skip check silently | Medium | Add endpoint to JARVIS-Prime in v298.0 |
| `_describe_instance_full` reverse-lookup by IP may be slow if GCP API latency is high — adds latency to HEALTH step | Low | Add timeout (3s default) and cache result for 60s |
| `ensure_static_vm_ready(recreate=True)` is not idempotent under concurrent calls — two `RECREATE_VM_ASYNC` fires could race | Medium | Guard with distributed lock (already have DLM in `distributed_lock_manager.py`) |
| Background recreation task has no cancellation path if supervisor shuts down mid-recreation | Low | Wire to supervisor lifecycle via `asyncio.shield()` + cleanup handler |
| `_RECOVERY_MATRIX` defaults to `FALLBACK_LOCAL` — this is safe but means some `(step, class)` combos are silently swallowed without a warning | Low | Add warning log when default is used (unmatched combination) |
| Reactor Core contract validation out of scope — version drift possible | Medium | Spec separately (v299.0+) |
