# Routing Unification & Type Safety Hardening (C+ Design)

**Date:** 2026-03-05
**Status:** Approved
**Repos:** JARVIS-AI-Agent, JARVIS-Prime, Reactor-Core
**Approach:** C+ (Layered + Contract-First Gates)

## Problem Statement

Two classes of systemic disease:

1. **Type safety crashes:** `NoneType.lower()` in 3 vision files, `dict.to_tuple()` in predictive engine, `properties=None` in scene graph — all from unvalidated data crossing typed boundaries.

2. **Routing split-brain:** Four independent routing systems (ModelRouter, PrimeRouter, CapabilityRouter, ModelRegistry+ModelSelector) make contradictory decisions. J-Prime is excluded from vision by a hardcoded provider set. `jprime_llava` is defined in config but never loaded. Direct client construction bypasses routing policy.

## Root Causes

| # | Disease | Root Cause | Files |
|---|---------|------------|-------|
| 1 | `NoneType.lower()` in state_intelligence | Dict keys can be None, `.lower()` called without check | state_intelligence.py:601-602,933 |
| 2 | `NoneType.lower()` in feedback_aware_vision | Callback returns None, `.lower()` called directly | feedback_aware_vision.py:179-186 |
| 3 | `dict.to_tuple()` in predictive engine | TransitionMatrix accepts any object, no type enforcement | predictive_precomputation_engine.py:217 |
| 4 | `SceneGraphNode(properties=None)` | Explicit None bypasses default_factory | semantic_scene_graph.py:124 |
| 5 | Hardcoded vision provider exclusion | `vision_providers = {CLAUDE, PRIME_CLOUD_RUN}` blocks PRIME_API | unified_model_serving.py:2419-2422 |
| 6 | J-Prime not in registry | `_load_gcp_models()` skips jprime_llava from config | model_registry.py:249 |
| 7 | Four routing systems, no authority | ModelRouter, PrimeRouter, CapabilityRouter, Registry don't cross-validate | Multiple files |
| 8 | Direct client construction bypasses policy | `PrimeAPIClient()` called directly at lines 2586,2612 | unified_model_serving.py |

## Ownership Model

Prime is the authoritative publisher of routing-capability data. JARVIS is the consumer.

```
Prime (PUBLISHER)              JARVIS (CONSUMER)
  Owns capability manifest  ->  Reads validated snapshot
  Publishes on /capabilities ->  Caches at boot + periodic refresh
  Versions with contract_hash -> Validates hash against expectations
  Evolves capabilities       ->  Deprecation-aware consumption
```

Bootstrap: JARVIS holds a versioned bootstrap snapshot (last-known-good manifest) for startup before Prime is available. Replaced by live data once Prime publishes.

Validation: Prime's manifest = "advertised capabilities." JARVIS tracks "verified capabilities" via circuit breaker feedback. Capability failing 3x gets demoted regardless of manifest.

## Routing Authority Hierarchy

Not consolidation — delegation:

```
ModelRouter (POLICY AUTHORITY)
  reads capability data FROM -> ProviderManifest (Prime-published)
  reads health status FROM   -> PrimeRouter (HEALTH AUTHORITY)
  reads model metadata FROM  -> ModelRegistry (DATA AUTHORITY)
  absorbs                    -> CapabilityRouter (circuit breakers move here)
```

PrimeRouter: endpoint health + failover. Never decides WHICH provider, only WHETHER reachable.
ModelRegistry: model definitions from config, state tracking. Never decides routing.
CapabilityRouter: absorbed into ModelRouter. Thin shim for import compatibility.

---

## Phase 0: Invariants & Safety Rails

### Purpose
Establish authoritative source of truth for routing policy, capability taxonomy, and contract schemas BEFORE touching implementation. Prevents refactor drift.

### Contract Package

```
backend/contracts/                    # Neutral contract module
  __init__.py
  capability_taxonomy.py              # Stable string IDs with deprecation metadata
  contract_version.py                 # min_supported, max_supported, current
  routing_authority.py                # Authority declarations + policy fingerprint
  manifest_schema.py                  # Schema for Prime's capability manifest
  non_functional_invariants.py        # Timeout ownership, cancellation, idempotency

tests/contracts/
  test_routing_invariants.py          # AST-based static analysis
  test_schema_compatibility.py        # Local schema validation per repo
  test_contract_fingerprint.py        # Policy hash consistency
```

### Key Decisions

**Capabilities as stable string IDs (not Enum):**
Enums break during partial upgrades. Capabilities are `@dataclass(frozen=True)` with `id: str`, `deprecated: bool`, `deprecated_by: Optional[str]`, `since_version: str`.

**Contract version with rolling compatibility:**
`ContractVersion` exposes `current`, `min_supported`, `max_supported`. Enables N/N-1 rolling upgrades without lockstep deploys.

**Provider capability matrix from Prime (not hand-maintained):**
Runtime caches a read-only snapshot. Prime is the source of truth via `/capabilities` endpoint.

**Non-functional invariants declared up front:**
- Timeout ownership: which system owns which timeout
- Cancellation policy: propagate, abandon, or shield per context
- Idempotency scope: request_id, boot_session_id, manifest_hash
- Reason codes: structured codes for routing fallback and contract failure

**AST-based static invariant test:**
`test_routing_invariants.py` uses `ast.parse()` to detect direct `PrimeAPIClient()` / `PrimeCloudRunClient()` construction outside factory allowlist. Not grep-based.

**Policy fingerprinting:**
Every routing decision logs `policy_hash` and `manifest_version`. Health endpoints include `contract_hash` and `manifest_age_s`.

---

## Phase 1: Type/Boundary Stabilization (Issues 1-4)

### Principle
Fix at boundaries, not scattered defensive clutter. Every crash traces to a boundary where untyped data enters a typed system.

### 1.1: Vision Intelligence Boundary Adapter

New file: `backend/vision/intelligence/boundary_adapters.py`

```python
def safe_state_key(key: Any) -> str:
    """Normalize state transition keys at ingestion boundary."""
    if key is None:
        return "__none__"
    return str(key)

def safe_text(value: Any) -> str:
    """Normalize text values at ingestion boundary."""
    if value is None:
        return ""
    return str(value)
```

Applied at ingestion points only:
- `state_intelligence.py` ~line 595: normalize keys when building transition_matrix
- `feedback_aware_vision.py` ~line 172: normalize callback return before branching

### 1.2: SceneGraphNode Properties Guard

`semantic_scene_graph.py` ~line 127: `__post_init__` validation:
```python
def __post_init__(self):
    if self.properties is None:
        self.properties = {}
```

### 1.3: StateVector/Dict Boundary in Predictive Engine

`predictive_precomputation_engine.py` ~line 217: type enforcement at `TransitionMatrix.add_state()`:
```python
def add_state(self, state: StateVector) -> int:
    if not isinstance(state, StateVector):
        raise TypeError(f"Expected StateVector, got {type(state).__name__}")
```

Plus: migration guard in `get_predictions()` that skips entries failing isinstance check.

### 1.4: Pickle Cache Versioning

Versioned cache envelope with `CACHE_SCHEMA_VERSION`. Stale cache discarded with warning, not crash.

### Gate A Criteria
- Zero `NoneType.lower()` crashes with None/malformed payloads
- Type-safety tests pass
- No new broad `except Exception`
- Boundary adapter tests cover None, empty string, non-string inputs

---

## Phase 2: Routing Authority Establishment (Issues 5-7)

### 2.1: Remove Hardcoded Vision Provider Exclusion

`unified_model_serving.py` ~line 2419-2422: replace hardcoded set with manifest-driven check:
```python
if request.require_vision:
    result = [
        p for p in result
        if self._provider_supports_capability(p, "vision")
    ]
```

`_provider_supports_capability` reads from cached ProviderManifest. Returns `True` on missing manifest (bootstrap safety — circuit breaker handles actual failures).

### 2.2: Register jprime_llava in ModelRegistry

`model_registry.py` in `_load_gcp_models()`: data-driven model loading:
```python
for model_name, model_conf in models_config.items():
    if model_name in self.models:
        continue
    self._load_model_from_config(model_name, model_conf, ModelDeployment.GCP_ONLY)
```

Any model in `hybrid_config.yaml` automatically materializes. No manual per-model registration.

### 2.3: ModelRouter Consumes PrimeRouter Health

ModelRouter queries PrimeRouter for health via `set_health_authority()` injection:
```python
def _is_provider_healthy(self, provider: ModelProvider) -> bool:
    if self._prime_router is None:
        return True
    endpoint = endpoint_map.get(provider)
    if endpoint is None:
        return True
    return self._prime_router.is_endpoint_healthy(endpoint)
```

PrimeRouter gains `is_endpoint_healthy(endpoint_name) -> bool` read-only query.

### 2.4: Absorb CapabilityRouter

Circuit breaker logic moves to ModelRouter. `capability_router.py` becomes thin compatibility shim delegating to `get_model_serving()`.

### 2.5: Shadow-Routing Parity Metrics

During transition, run both old and new routing logic:
```python
if self._shadow_routing_enabled:
    old_result = self._get_providers_legacy(request)
    if set(new_result) != set(old_result):
        logger.warning(f"Routing parity mismatch: old={old_result} new={new_result}")
```

Enabled via `JARVIS_SHADOW_ROUTING=true`. Disabled after Gate B.

### Gate B Criteria
- Shadow routing: zero parity mismatches for vision requests
- `jprime_llava` in `ModelRegistry.models` after boot
- Vision with healthy J-Prime routes to PRIME_API
- Vision with unhealthy J-Prime falls to CLAUDE via circuit breaker
- No hardcoded `vision_providers` sets remain

---

## Phase 2.5: Supervisor Contract Gate

### Purpose
`unified_supervisor.py` blocks "ready" unless cross-repo contract/version/capability handshake passes.

### Architecture

```
Phase 5 (Trinity) completes
    |
_validate_cross_repo_contracts()
    |- Fetch Prime /capabilities manifest
    |- Fetch Reactor /contract_version
    |- Compare against local ContractVersion (min/max/current)
    |- Verify policy_hash consistency
    |- Result: PASS / DEGRADED / FAIL
    |
Phase 6+ continues (or blocks with reason code)
```

### Contract Validation

- Incompatible Prime version: fail-fast with reason `version_incompatible`
- Unreachable Prime: continue as DEGRADED (not crash)
- Compatible Prime: PASS, manifest cached, routing uses it
- Hash mismatch: logged with both hashes

### N/N-1 Compatibility

Deploy new JARVIS with `current=(0,3,0), min_supported=(0,2,0)` -> old Prime at (0,2,0) compatible.
Then deploy Prime to (0,3,0). Then bump JARVIS min_supported.

### Gate C Criteria
- Incompatible version: startup fails-fast with structured reason
- Unreachable service: startup continues DEGRADED
- Compatible: PASS, manifest cached
- Hash mismatch logged with both hashes

---

## Phase 3: Bypass Elimination (Issue 8)

### Factory Token Enforcement

Client constructors require `_factory_token` parameter (module-private string):
```python
class PrimeAPIClient:
    def __init__(self, *, _factory_token: str = None, **kwargs):
        if _factory_token != _FACTORY_SECRET:
            raise RuntimeError("Direct construction prohibited. Use router.get_client()")
```

### Factory Method on ModelRouter

```python
class ModelRouter:
    def get_client(self, provider: ModelProvider) -> BaseClient:
        if not self._provider_supports_capability(provider, capability):
            raise CapabilityMismatch(...)
        return self._client_registry[provider]
```

### AST Lint in CI

`test_routing_invariants.py` AST-based scan becomes CI gate.

### Gate D Criteria
- AST scan: zero direct construction outside allowlist
- Runtime: direct construction raises RuntimeError
- All callers migrated to factory

---

## File Manifest

| Phase | File | Repo | New/Edit |
|-------|------|------|----------|
| 0 | backend/contracts/__init__.py | JARVIS | New |
| 0 | backend/contracts/capability_taxonomy.py | JARVIS | New |
| 0 | backend/contracts/contract_version.py | JARVIS | New |
| 0 | backend/contracts/routing_authority.py | JARVIS | New |
| 0 | backend/contracts/manifest_schema.py | JARVIS | New |
| 0 | backend/contracts/non_functional_invariants.py | JARVIS | New |
| 0 | tests/contracts/test_routing_invariants.py | JARVIS | New |
| 0 | tests/contracts/test_schema_compatibility.py | JARVIS | New |
| 0 | tests/contracts/test_contract_fingerprint.py | JARVIS | New |
| 1 | backend/vision/intelligence/boundary_adapters.py | JARVIS | New |
| 1 | backend/vision/intelligence/state_intelligence.py | JARVIS | Edit |
| 1 | backend/vision/intelligence/feedback_aware_vision.py | JARVIS | Edit |
| 1 | backend/vision/intelligence/semantic_scene_graph.py | JARVIS | Edit |
| 1 | backend/vision/intelligence/predictive_precomputation_engine.py | JARVIS | Edit |
| 2 | backend/intelligence/unified_model_serving.py | JARVIS | Edit |
| 2 | backend/intelligence/model_registry.py | JARVIS | Edit |
| 2 | backend/core/capability_router.py | JARVIS | Edit (shim) |
| 2 | backend/core/prime_router.py | JARVIS | Edit |
| 2 | jarvis_prime server entry point | JARVIS-Prime | Edit |
| 2.5 | unified_supervisor.py | JARVIS | Edit |
| 2.5 | jarvis_prime server entry point | JARVIS-Prime | Edit |
| 2.5 | reactor-core entry point | Reactor-Core | Edit |
| 3 | backend/intelligence/unified_model_serving.py | JARVIS | Edit |
| 3 | tests/contracts/test_routing_invariants.py | JARVIS | Edit |

15 new files, 10 edited files across 3 repos. Zero unnecessary duplicates.

## Verification Gates

| Gate | After | Criteria |
|------|-------|----------|
| A | Phase 1 | Zero NoneType crashes, typed boundaries, no broad except |
| B | Phase 2 | Shadow routing parity, J-Prime selected for vision, circuit breaker fallback |
| C | Phase 2.5 | Contract gate blocks incompatible versions, degrades gracefully |
| D | Phase 3 | AST scan clean, runtime factory enforcement |
| Soak | All | 48h restart cycles, email/vision/reconnect under load, zero critical regressions |

## Advanced Gaps for Post-Soak Assessment

| # | Gap | Risk |
|---|-----|------|
| 1 | Authority caching split-brain (stale local cache resurrects old routing) | Medium |
| 2 | Warm-up false readiness (health "up" before capability usable) | Medium |
| 3 | Cancellation leakage (timed-out phases leave orphan tasks) | High |
| 4 | Fallback recursion loops (fallback re-enters primary, amplifies load) | Medium |
| 5 | Idempotency scope mismatch (per-component, not globally unique) | Low |
| 6 | Contract downgrade hazards (strict bump without compat window deadlocks) | Medium |
| 7 | Backpressure inversion (queue growth starves supervisor heartbeat) | High |
| 8 | Event ordering ambiguity (cross-repo async reorders causal events) | Medium |
| 9 | Supervisor self-healing paradox (no external watchdog) | High |
| 10 | Config drift via env overlays (env vars silently override policy) | Medium |
