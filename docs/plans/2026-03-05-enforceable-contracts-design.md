# Enforceable Contract System — Design Document

**Date:** 2026-03-05
**Disease:** #4 — Advisory Contracts = No Contracts
**Root Cause:** Authority timing + enforcement stratification. Advisory checks run early but are non-binding; strict checks run later but aren't unified with env contract severity model.

---

## Severity Model

```python
class ContractSeverity(str, Enum):
    PRECHECK_BLOCKER = "precheck_blocker"        # Abort before any external I/O
    BOOT_BLOCKER = "boot_blocker"                # Evaluated after bind/discovery, blocks progress
    BLOCK_BEFORE_READY = "block_before_ready"    # Allow dep bringup, block READY transition
    DEGRADED_ALLOWED = "degraded_allowed"        # Continue in explicit degraded mode with reason code
    ADVISORY = "advisory"                         # Log + metrics only
```

### Classification of Existing EnvContracts

| Severity | Contracts | Rationale |
|----------|-----------|-----------|
| PRECHECK_BLOCKER | Malformed `JARVIS_PRIME_URL`, invalid port ranges on `JARVIS_BACKEND_PORT`/`JARVIS_FRONTEND_PORT`/`JARVIS_LOADING_SERVER_PORT`, duplicate port claims, required secrets (per profile) | Must fail before any external I/O or bind attempt |
| BOOT_BLOCKER | Port already bound (detected at bind time), schema version incompatible (detected after first health fetch) | Can only be evaluated after bind/discovery |
| BLOCK_BEFORE_READY | `JARVIS_PRIME_REQUIRED_CAPABILITIES` (when Prime required), `JARVIS_REACTOR_REQUIRED_CAPABILITIES` (when Reactor required) | Must validate before workload starts; conditional on component being enabled |
| DEGRADED_ALLOWED | `JARVIS_INVINCIBLE_NODE_IP`, `GCP_PRIME_ENDPOINT`, `JARVIS_PRIME_REQUIRED_CAPABILITIES` (when Prime NOT required) | Fallback exists; continue degraded with reason code |
| ADVISORY | `JARVIS_STARTUP_MEMORY_MODE`, `JARVIS_STARTUP_DESIRED_MODE`, telemetry toggles, UX hints | Non-critical tuning/optional features |

### Conditional Severity

`JARVIS_PRIME_REQUIRED_CAPABILITIES` is BLOCK_BEFORE_READY only when `JARVIS_CONTRACT_REQUIRE_PRIME=true`. Otherwise DEGRADED_ALLOWED with explicit reason. This requires an `effective_severity` field computed at runtime:

```python
@dataclasses.dataclass(frozen=True)
class ContractViolationRecord:
    contract_name: str
    base_severity: ContractSeverity
    effective_severity: ContractSeverity  # May differ from base due to context
    reason_code: ViolationReasonCode      # Machine-readable enum
    violation: str                         # Human-readable description
    value_origin: str                      # "explicit" | "default" | "alias:{name}" | "derived"
    checked_at_monotonic: float            # For ordering/duration
    checked_at_utc: str                    # ISO 8601 for observability
    phase: str                             # "precheck" | "boot" | "contract_gate" | "runtime"
```

---

## Reason Code Enum

```python
class ViolationReasonCode(str, Enum):
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

---

## Enforcement Sequencing

```
PRECHECK --> DEPENDENCY_BRINGUP --> CONTRACT_GATE --> READY
   |              |                     |              |
   |  PRECHECK_BLOCKER = abort          |              |
   |              |  BOOT_BLOCKER = abort              |
   |              |                     |              |
   |              |  BLOCK_BEFORE_READY = block READY  |
   |              |                     |              |
   +-- DEGRADED_ALLOWED = set reason, continue --------+
   +-- ADVISORY = log only ----------------------------+
```

### PRECHECK (before any external I/O)

- Run severity-aware `validate_contracts_at_boot()`
- PRECHECK_BLOCKER violations raise `StartupContractViolation` (typed exception, NOT `sys.exit()`)
- Top-level boot runner catches and terminates cleanly with structured error report
- BLOCK_BEFORE_READY violations accumulated into `ContractStateAuthority`
- DEGRADED_ALLOWED violations set degradation reason code, continue
- ADVISORY violations log warning

### DEPENDENCY_BRINGUP (existing phases 1-6)

- No change to existing startup phases
- BOOT_BLOCKER violations (port bind failure, etc.) raise `StartupContractViolation`

### CONTRACT_GATE (before READY transition)

- Run `CrossRepoContractEnforcer.check_many()` (already exists, already fail-closed)
- Revalidate BLOCK_BEFORE_READY env contracts that need running services
- **Contract hash revalidation:** per-target, epoch-bound. Store `{target, schema_hash, capability_hash, session_id, checked_at}`. Fail on drift with explicit target attribution
- If any required cross-repo targets fail -> block READY
- If any BLOCK_BEFORE_READY violations remain unresolved -> block READY

### READY

- All contracts satisfied (or explicitly degraded with reason codes)
- Runtime drift monitor continues (existing `ContractDriftMonitor`)

---

## Typed Exception (Not sys.exit)

```python
class StartupContractViolation(Exception):
    """Raised when a contract violation at PRECHECK_BLOCKER or BOOT_BLOCKER severity is detected."""
    def __init__(self, violations: List[ContractViolationRecord]):
        self.violations = violations
        reasons = "; ".join(f"{v.contract_name}:{v.reason_code.value}" for v in violations)
        super().__init__(f"Startup blocked by contract violations: {reasons}")
```

Top-level boot runner catches this and emits structured error report before clean termination.

---

## ContractStateAuthority

Singleton that accumulates all violation records with dedup semantics.

```python
class ContractStateAuthority:
    """Central authority for all contract violation state. Queryable, not ephemeral."""

    def record(self, violation: ContractViolationRecord) -> None:
        """Record a violation. Dedup: same contract+reason_code updates counter/timestamp, doesn't append."""

    def get_violations(self, *, severity_filter: Optional[ContractSeverity] = None,
                       phase_filter: Optional[str] = None) -> List[ContractViolationRecord]:
        """Query recorded violations with optional filters."""

    def has_blockers(self) -> bool:
        """True if any PRECHECK_BLOCKER or BOOT_BLOCKER violations exist."""

    def blocking_reasons(self) -> List[str]:
        """Machine-readable reason codes for all blocking violations."""

    def health_summary(self, *, max_detail: int = 5) -> Dict[str, Any]:
        """Bounded summary for health payload. Top-N blockers by default."""

    def full_report(self) -> Dict[str, Any]:
        """Full violation list for debug endpoint / startup report."""
```

### Dedup Semantics

Same violation repeating (e.g., runtime revalidation loop) updates counters and timestamps, does NOT append unbounded entries. Key: `(contract_name, reason_code)`.

### Health Payload Exposure

- `/health` includes `contract_violations` field with bounded summary (count + top-N blockers)
- Full detail available behind debug endpoint or startup report JSON
- Prevents huge health responses while maintaining observability

---

## Default-Origin Tracing

```python
@dataclasses.dataclass(frozen=True)
class EnvResolution:
    value: str
    origin: str           # "explicit" | "default" | "alias:{alias_name}" | "derived"
    canonical_name: str
```

`get_canonical_env()` updated to return `EnvResolution`. Callers that only need the value use `get_canonical_env(...).value`. Origin tracing lets startup report show when a typo caused default fallback (`origin=default` instead of `origin=explicit`).

---

## Required Secrets Registry

Explicit per-profile secret requirements, not implicit:

```python
REQUIRED_SECRETS: Dict[str, List[str]] = {
    "prod": ["JARVIS_API_KEY", "GCP_SERVICE_ACCOUNT_KEY"],
    "staging": ["JARVIS_API_KEY"],
    "dev": [],  # No secrets required in dev
}
```

Profile determined by `JARVIS_ENVIRONMENT` env var (default: "dev"). Missing required secrets are PRECHECK_BLOCKER.

---

## Contract Hash Revalidation

At CONTRACT_GATE, per-target revalidation:

```python
@dataclasses.dataclass(frozen=True)
class ContractSnapshot:
    target: str
    schema_hash: str
    capability_hash: str
    session_id: str
    checked_at_monotonic: float
```

Compare snapshot from initial check against current state. Drift detected = `HASH_DRIFT_DETECTED` violation with target attribution.

---

## Testing Strategy

### Unit Tests (`tests/unit/backend/test_contract_enforcement.py`)

- Severity classification: each of 18 env contracts has correct severity
- Preflight blocker: PRECHECK_BLOCKER violation raises `StartupContractViolation`
- Gate blocking: BLOCK_BEFORE_READY violations prevent READY transition
- Origin tracing: typo alias falls back to default, surfaces `origin=default`
- Dedup: rerunning validation doesn't duplicate violation state (idempotency)
- Conditional severity: capability contracts change effective_severity based on component enabled state
- Reason codes: each violation type produces correct machine-readable code
- Health summary: bounded detail (max_detail respected)

### Integration Tests

- Full phase transition: precheck -> bringup -> gate -> READY
- Precheck abort: structured error report emitted
- Degraded-allowed path: reaches READY with degraded state and reason codes
- Temporal drift: contract changes between precheck and gate detected

---

## Files Changed

| File | Change |
|------|--------|
| `backend/core/startup_contracts.py` | Add `ContractSeverity`, `ViolationReasonCode`, `ContractViolationRecord`, `ContractStateAuthority`, `EnvResolution`, `StartupContractViolation`. Add `severity` field to `EnvContract`. Update `validate_contracts_at_boot()` to return structured results. Update `get_canonical_env()` to return `EnvResolution`. Add required secrets registry. |
| `unified_supervisor.py` | Wire preflight gate (PRECHECK_BLOCKER raises typed exception). Accumulate BLOCK_BEFORE_READY into state authority. Add contract hash revalidation at CONTRACT_GATE. Export violations to health payload. Top-level boot runner catches `StartupContractViolation`. |
| `tests/unit/backend/test_contract_enforcement.py` | New: severity classification, preflight blocking, gate blocking, origin tracing, dedup, conditional severity, reason codes, health summary bounds |

---

## Scope Boundary

See `docs/plans/2026-03-05-contract-enforcement-future-work.md` for items explicitly deferred:
- Capability proof checks (active probes)
- Port ownership lease/epoch fencing
- Semantic contract tests + field-level compatibility map
- Bootstrap watchdog separation
