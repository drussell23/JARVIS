# Phase 2: Trinity Autonomy Wiring — Design Document

**Date:** 2026-03-03
**Approach:** Guarded Event-First (Approach A)
**Scope:** Wire & Harden existing primitives across JARVIS, JARVIS-Prime, Reactor-Core
**Semantics:** Effectively-once with reconciliation (not absolute exactly-once)

---

## Context

Phase 1 hardened the JARVIS Body for autonomous workspace operations (policy engine,
durable write ledger, idempotency, doctor diagnostics). Phase 2 wires the Trinity
loop: Body emits autonomy lifecycle events → forwarded via existing transport →
Reactor ingests with strict classification → Prime consumes policy constraints and
returns structured plans → Supervisor validates contracts at boot.

### Guardrails (enforced before event rollout)

1. **Read-only autonomy mode default** — no autonomous writes until contract gate passes
2. **Minimal contract sanity check at boot** — schema/version presence before full negotiation

---

## 1. Canonical Autonomy Event Vocabulary

Events ride inside `ExperienceEvent.metadata` (strict extension, not loose metadata).

### Event Types

| Type | When | Training Label | Trainable? |
|---|---|---|---|
| `intent_written` | Pre-write journal entry created | — | Exclude |
| `committed` | Write succeeded, journal committed | SUCCESS | Yes |
| `failed` | Write failed, journal marked failed | FAILURE | Yes |
| `policy_denied` | Autonomy policy blocked action | INFRASTRUCTURE | No |
| `deduplicated` | Idempotency key suppressed duplicate | — | Exclude |
| `superseded` | Stale intent from crash marked superseded | RECONCILE_REQUIRED | No (until reconciled) |
| `no_journal_lease` | Write rejected, no durable backing | INFRASTRUCTURE | No |

### Required Metadata Keys (all 7 mandatory)

```python
AUTONOMY_REQUIRED_KEYS = frozenset({
    "autonomy_event_type",       # one of the 7 types above
    "autonomy_schema_version",   # "1.0"
    "idempotency_key",           # canonical key
    "trace_id",                  # from TraceEnvelope
    "correlation_id",            # request correlation
    "action",                    # workspace action name
    "request_kind",              # "autonomous"
})
```

### Optional Metadata Keys

```python
AUTONOMY_OPTIONAL_KEYS = {
    "action_risk",               # "read", "write", "high_risk_write"
    "policy_decision",           # {allowed, reason, escalation, remediation}
    "journal_seq",               # nullable, Body-local sequence number
    "goal_id",                   # autonomous goal identifier
    "step_id",                   # step within goal
    "emitted_at",                # monotonic timestamp for ordering
}
```

### Event Ordering

Events are **not** globally ordered by `journal_seq` (Body-local).
Reactor uses `(trace_id, autonomy_event_type, emitted_at)` with an
out-of-order tolerant reducer. Late `intent_written` arriving after
`committed` is tolerated — the reducer treats the pair as complete.

---

## 2. JARVIS Body — Event Emission

**File:** `backend/neural_mesh/agents/google_workspace_agent.py`

### Emission Points in `execute_task()`

```
Policy check    → emit policy_denied           (before returning error)
Dedup check     → emit deduplicated            (before returning dedup result)
No-lease check  → emit no_journal_lease        (before returning fail-closed error)
Journal write   → emit intent_written          (after fenced_write succeeds)
Action success  → emit committed               (after mark_result("committed"))
Action failure  → emit failed                  (after mark_result("failed"))
On startup      → emit superseded              (for each stale intent reconciled)
```

**Critical:** `no_journal_lease` is emitted **before** the fail-closed return so
Reactor/ops can observe infrastructure reliability impact.

### Private Method

```python
def _emit_autonomy_event(
    self,
    event_type: str,
    action: str,
    idempotency_key: str,
    trace_id: str,
    correlation_id: str,
    *,
    policy_decision: Optional[dict] = None,
    journal_seq: Optional[int] = None,
    extra: Optional[dict] = None,
) -> None:
```

Constructs `ExperienceEvent` with `event_type="METRIC"`, `source="JARVIS_BODY"`,
and the strict autonomy metadata block. Hands to forwarder.

### Trusted Provenance Hardening

Beyond `_request_kind_source` string whitelist, require a caller identity nonce
generated at `execution_budget()` entry and propagated via `ExecutionContext`.
The nonce is validated by `_emit_autonomy_event()` to prevent spoofed emissions
from code paths that bypass the execution budget.

---

## 3. JARVIS Body — Event Forwarding

**File:** `backend/intelligence/cross_repo_experience_forwarder.py`

### Helper Method

```python
def forward_autonomy_event(self, event_type: str, action: str,
                            idempotency_key: str, trace_id: str,
                            correlation_id: str, **extra) -> None:
```

Transport: existing Trinity event bus (primary) + file fallback to
`~/.jarvis/trinity/events/`. At-least-once delivery.

### Replay Storm Mitigation

During startup reconciliation (bulk `superseded` emission), rate-limit
forwarding to `JARVIS_AUTONOMY_EVENT_RATE_LIMIT` events/sec (default: 50).

### Forwarder Event Type

Uses `event_type="METRIC"` on the `ExperienceEvent`. Reactor's ingestion
pipeline already accepts METRIC events via `TelemetryIngestor`. The
`autonomy_event_type` in metadata is the authoritative classifier.

---

## 4. JARVIS Body — Reconciliation Surface

**File:** `backend/neural_mesh/agents/google_workspace_agent.py`

Extend `get_autonomy_doctor_report()`:
- Query journal for `action_filter="workspace:"` with `result="superseded"`
- Count and surface as `reconcile_required` informational check

**File:** `backend/main.py`

Add to `/api/system/status` response:
```json
{
    "autonomy": {
        "mode": "read_only",
        "contract_status": "pass",
        "pending_reconciliation": 0,
        "body_ready": true,
        "prime_compatible": true,
        "reactor_ingesting": true
    }
}
```

---

## 5. Reactor-Core — Autonomy Event Ingestion

**File:** `reactor_core/ingestion/autonomy_event_ingestor.py` (new — implements BaseIngestor protocol)

### Validation

1. Check all 7 required metadata keys present
2. Hard enum validation for `autonomy_event_type`
3. Version check: `autonomy_schema_version` in supported set
4. Malformed events → quarantine (not silently coerced)

### Quarantine Policy

- Location: `~/.jarvis/reactor/quarantine/autonomy/`
- Retention: 7 days (env: `REACTOR_QUARANTINE_RETENTION_DAYS`)
- Max size: 100MB (env: `REACTOR_QUARANTINE_MAX_SIZE_MB`)
- Alert threshold: >10 quarantined/hour triggers warning log

### Classification (Centralized)

**File:** `reactor_core/ingestion/autonomy_classifier.py` (new — single source of truth)

```python
class AutonomyEventClassifier:
    """Centralized training label classifier for autonomy events.

    Every pipeline stage inherits the same exclusion policy.
    """

    TRAINABLE = {"committed", "failed"}
    INFRASTRUCTURE = {"policy_denied", "no_journal_lease"}
    EXCLUDE = {"deduplicated", "intent_written"}
    RECONCILE_ONLY = {"superseded"}

    def classify(self, event_type: str) -> Tuple[InteractionOutcome, bool]:
        """Returns (outcome, should_train)."""
        if event_type in self.TRAINABLE:
            outcome = InteractionOutcome.SUCCESS if event_type == "committed" else InteractionOutcome.FAILURE
            return outcome, True
        if event_type in self.INFRASTRUCTURE:
            return InteractionOutcome.DEFERRED, False  # infrastructure, not model quality
        if event_type in self.RECONCILE_ONLY:
            return InteractionOutcome.DEFERRED, False  # until reconciled
        return InteractionOutcome.UNKNOWN, False  # exclude/unknown
```

### Deduplication

Composite key: `(idempotency_key, autonomy_event_type, trace_id)`.
Uses existing bloom filter in `TrinityExperienceReceiver`.

**File:** `reactor_core/ingestion/base_ingestor.py`

Add to `InteractionOutcome` enum:
```python
INFRASTRUCTURE = "infrastructure"  # Policy/infra events, not model quality
```

**File:** `reactor_core/training/unified_pipeline.py`

Add training exclusion filter:
```python
if outcome in {InteractionOutcome.INFRASTRUCTURE, InteractionOutcome.DEFERRED}:
    skip_training()
```

---

## 6. JARVIS-Prime — Structured Output + Policy Awareness

**File:** `jarvis-prime/jarvis_prime/core/jarvis_bridge.py`

### Policy Input

Accept `autonomy_policy` in command payload:
```python
{
    "autonomy_policy": {
        "allowed_actions": ["send_email", "fetch_unread_emails"],
        "denied_actions": ["delete_email"],
        "write_enabled": true,
        "high_risk_enabled": false,
        "autonomy_schema_version": "1.0",
    }
}
```

### Structured Plan Output

Return structured `action_plan` in response:
```python
{
    "action_plan": [
        {
            "action": "send_email",
            "risk": "write",
            "target": "to=user@example.com",
            "idempotency_key": "goal_1:2:send_email:abc123",
            "verification_plan": "Check sent folder for message",
            "requires_approval": false,
        }
    ],
    "policy_compatible": true,
    "contract_version": "1.0",
    "autonomy_schema_version": "1.0",
}
```

Prime echoes/validates idempotency keys. If Prime generates a key, Body uses it.
If Body supplies one, Prime echoes it.

### GCP Drift Guard

**File:** `jarvis-prime/jarvis_prime/server.py`

Include `autonomy_schema_version` and `contract_version` in `/health` response:
```json
{
    "status": "ready",
    "autonomy_schema_version": "1.0",
    "contract_version": "1.0"
}
```

Also include in every plan response metadata for observability.

---

## 7. Supervisor — Boot Contract Check

**File:** `unified_supervisor.py` or `backend/supervisor/cross_repo_startup_orchestrator.py`

### Contract Compatibility Matrix

```python
AUTONOMY_SCHEMA_COMPATIBILITY = {
    "1.0": {"min_prime": "1.0", "min_reactor": "1.0"},
}
```

### Boot Check

```python
async def _check_autonomy_contracts(self) -> Tuple[bool, str, dict]:
    checks = {}

    # Body
    checks["body_policy"] = hasattr(workspace_agent, "_autonomy_policy")
    checks["body_journal"] = journal is not None and journal.has_lease
    checks["body_schema"] = "1.0"  # from config

    # Prime
    prime_health = await probe_prime_health()
    checks["prime_reachable"] = prime_health is not None
    checks["prime_schema"] = prime_health.get("autonomy_schema_version") if prime_health else None

    # Reactor
    reactor_health = await probe_reactor_health()
    checks["reactor_reachable"] = reactor_health is not None
    checks["reactor_schema"] = reactor_health.get("autonomy_schema_version") if reactor_health else None

    # Compatibility matrix
    compat = AUTONOMY_SCHEMA_COMPATIBILITY.get(checks["body_schema"], {})
    checks["prime_compatible"] = (
        checks["prime_schema"] is not None
        and checks["prime_schema"] >= compat.get("min_prime", "999")
    )
    checks["reactor_compatible"] = (
        checks["reactor_schema"] is not None
        and checks["reactor_schema"] >= compat.get("min_reactor", "999")
    )

    all_pass = all(v for k, v in checks.items() if k.endswith("_compatible") or k.endswith("_reachable"))
    if not all_pass:
        return False, "contract_mismatch", checks
    return True, "autonomy_ready", checks
```

**If mismatch:** run degraded read-only mode, do not enable autonomous writes.

---

## 8. Edge Case Mitigations

| Gap | Mitigation | Location |
|---|---|---|
| Event ordering drift | Out-of-order reducer: `(trace_id, event_type, emitted_at)`, not `journal_seq` | Reactor ingestor |
| Duplicate transport paths | Bloom filter dedup by `(idempotency_key, event_type, trace_id)` triple | Reactor receiver |
| GCP contract drift | `autonomy_schema_version` + `contract_version` in Prime health + every plan response | Prime server |
| Policy version drift | Events include frozen `policy_decision` snapshot at emission time | Body metadata |
| Clock/timezone skew | Monotonic clocks for dedup windows; wall clock only for display | All repos |
| Replay storms at startup | Rate-limit: `JARVIS_AUTONOMY_EVENT_RATE_LIMIT` (default 50/sec) | Body forwarder |
| Provenance spoofing | `_TRUSTED_SOURCES` + execution budget nonce validation | Body execute_task |
| Split-brain supervisor | Journal lease is authoritative; only lease holder emits `intent_written` | Phase 1 |
| Training contamination | Centralized `AutonomyEventClassifier` — single source of truth for all pipeline stages | Reactor |
| Quarantine overflow | Retention 7d, max 100MB, alert >10/hr | Reactor quarantine |

---

## 9. Rollout Plan

1. **Read-only autonomy** (default) — writes gated, events flowing
2. **Canary writes** — `send_email` only via strict allowlist
3. **SLO gate** — stable dedup/reconciliation/error rates for 7 days
4. **Expand actions** — `create_calendar_event`, then docs/sheets
5. **Phase 2-lite** — full contract negotiation (versioned handshake protocol)

---

## 10. Files Modified

### JARVIS-AI-Agent (4 modified + tests)

| File | Change |
|---|---|
| `google_workspace_agent.py` | `_emit_autonomy_event()`, 7 emission points, reconciliation in doctor |
| `cross_repo_experience_forwarder.py` | `forward_autonomy_event()`, rate limiting |
| `main.py` | Autonomy health in `/api/system/status` |
| `unified_supervisor.py` or `cross_repo_startup_orchestrator.py` | `_check_autonomy_contracts()` |

### jarvis-prime (2 modified)

| File | Change |
|---|---|
| `jarvis_bridge.py` | Accept `autonomy_policy`, return structured `action_plan` |
| `server.py` | `autonomy_schema_version` + `contract_version` in health |

### reactor-core (2 new + 2 modified)

| File | Change |
|---|---|
| `ingestion/autonomy_event_ingestor.py` | **New** — BaseIngestor for autonomy events |
| `ingestion/autonomy_classifier.py` | **New** — centralized training label classifier |
| `ingestion/base_ingestor.py` | Add `INFRASTRUCTURE` to InteractionOutcome |
| `training/unified_pipeline.py` | Training exclusion filter |

### Environment Variables (new)

| Variable | Default | Repo | Purpose |
|---|---|---|---|
| `JARVIS_AUTONOMY_EVENT_RATE_LIMIT` | `50` | JARVIS | Max events/sec during startup reconciliation |
| `REACTOR_QUARANTINE_RETENTION_DAYS` | `7` | Reactor | Quarantine file retention |
| `REACTOR_QUARANTINE_MAX_SIZE_MB` | `100` | Reactor | Quarantine max disk usage |
| `REACTOR_QUARANTINE_ALERT_THRESHOLD` | `10` | Reactor | Quarantined events/hr alert trigger |

---

## Definition of Done

### Event Pipeline
- [ ] All 7 autonomy event types emitted from Body at correct lifecycle points
- [ ] `no_journal_lease` emitted before fail-closed return
- [ ] Events forwarded via existing `CrossRepoExperienceForwarder`
- [ ] Rate-limited during startup reconciliation
- [ ] `ExperienceEvent.event_type="METRIC"` with strict autonomy metadata

### Reactor Ingestion
- [ ] Autonomy events ingested with 7 required key validation
- [ ] Hard enum validation for `autonomy_event_type`
- [ ] Malformed events quarantined (not silently coerced)
- [ ] Dedup by `(idempotency_key, autonomy_event_type, trace_id)`
- [ ] Centralized `AutonomyEventClassifier` for all pipeline stages
- [ ] `policy_denied`, `no_journal_lease` never trained as model failure
- [ ] `superseded` treated as RECONCILE_REQUIRED only

### Prime Integration
- [ ] Accepts `autonomy_policy` in command payload
- [ ] Returns structured `action_plan` with idempotency key echo
- [ ] Health response includes `autonomy_schema_version` + `contract_version`
- [ ] Every plan response includes schema version in metadata

### Supervisor
- [ ] Boot contract check: Body + Prime + Reactor schema compatibility
- [ ] Contract mismatch → degraded read-only mode
- [ ] Unified autonomy health state in `/api/system/status`
- [ ] Reconciliation count visible in status

### Edge Cases
- [ ] Out-of-order reducer (not journal_seq sorting)
- [ ] Bloom filter dedup on composite key
- [ ] Provenance nonce validation beyond string whitelist
- [ ] Quarantine ops: retention, max size, alert threshold
- [ ] Replay storm rate limiting
