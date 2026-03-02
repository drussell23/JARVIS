# Autonomy Wiring Design — P0+P1

**Date:** 2026-03-01
**Scope:** P0 (4 blockers) + P1 (verification + runtime escalation)
**Approach:** In-place hardening (Approach A) — no new files, no new abstractions
**Files Modified:** 4 — `unified_command_processor.py`, `google_workspace_agent.py`, `integration.py`, `agi_os_coordinator.py`

---

## Root Cause Analysis

"Check my email" fails due to 4 compounding gaps:

1. **Split singleton** — Command processor calls `integration.py:get_neural_mesh_coordinator()` which returns `_neural_mesh_coordinator`. But AGI OS startup uses `neural_mesh_coordinator.py:start_neural_mesh()` which sets a *different* module-level `_coordinator`. The lookup always returns None.
2. **One-shot lookup** — After the first miss, `_neural_mesh_lookup_attempted=True` prevents retries forever.
3. **Auth dead-end** — `NEEDS_REAUTH` is terminal. No autonomous recovery, no visual fallback (disabled by default).
4. **No output verification** — Success is returned without validating the response schema or semantics.

---

## Cross-Cutting Invariants

### Error/Reason Code Taxonomy

Single taxonomy reused across all 4 sections:

```
# Coordinator (Section 1)
coordinator_unresolved          # Lookup hasn't succeeded yet
coordinator_backing_off         # In retry backoff window
coordinator_cooldown            # Max retries hit, in cooldown
coordinator_stale               # Object exists but _running==False
coordinator_resolved            # Successfully resolved

# Auth (Section 2)
auth_healthy                    # Token valid
auth_refreshing                 # Attempting token refresh
auth_refresh_transient_fail     # Transient refresh failure (network, 5xx)
auth_refresh_permanent_fail     # Permanent failure (invalid_grant, revoked)
auth_degraded_visual            # Operating via visual fallback
auth_guided_recovery            # Waiting for user re-auth
auth_auto_healed                # Token file replaced externally

# Verification (Section 4)
verify_passed                   # All checks passed
verify_schema_fail              # Required keys missing or wrong type
verify_semantic_fail            # Values present but nonsensical
verify_empty_valid              # Empty result that's semantically valid (0 unread)
verify_transport_fail           # Network/timeout during fetch

# Recovery (Section 4)
recovery_same_tier_retry        # Retried same tier
recovery_tier_fallback          # Fell back to next tier
recovery_runtime_escalated      # Submitted to AgentRuntime
recovery_deadline_exhausted     # No budget remaining
recovery_idempotency_blocked    # Write retry blocked (no idempotency key)
```

### Idempotency Invariant

**Write actions (`send_email`, `draft_email`, `create_event`, `delete_*`) are NEVER retried without a stable idempotency key.** If no idempotency mechanism exists for the action, same-tier retry is skipped and the flow moves directly to tier fallback or guided recovery.

### Deadline Floor for Runtime Escalation

Runtime goal escalation requires minimum `JARVIS_RUNTIME_ESCALATION_FLOOR` seconds remaining (default 5.0s, env-configurable). If remaining budget is below this floor, escalation is skipped and structured failure is returned immediately. Prevents guaranteed timeout churn.

### Degraded-Mode User Message Policy

All degraded/guided responses use deterministic, concise wording:

- **DEGRADED_VISUAL:** `"Using visual fallback — Google API auth is being refreshed. Results may be slower than usual."`
- **NEEDS_REAUTH_GUIDED (read):** `"Your Google auth needs renewal. I fetched your email visually, but say 'fix my Google auth' or re-run the setup script for full API access."`
- **NEEDS_REAUTH_GUIDED (write):** `"I can't send emails right now — Google auth needs renewal. Say 'fix my Google auth' or re-run the setup script."`
- **Verification failed:** `"I tried to check your email but the response was incomplete. Retrying with a different method."`
- **All attempts exhausted:** `"I wasn't able to complete that workspace action after multiple attempts. Here's what I tried: {attempt_summary}"`

### Rollback Clause

Section 2 auth state machine is gated behind `JARVIS_AUTH_STATE_MACHINE_V2` env var (default `true`). If regressions are detected, setting to `false` reverts to the previous 3-state auth path (`UNAUTHENTICATED → AUTHENTICATED → NEEDS_REAUTH`) with no visual fallback. All other sections (1, 3, 4) are independent and unaffected.

---

## Section 1: Coordinator Lookup Retry

**File:** `backend/api/unified_command_processor.py`

### State Machine

```
UNRESOLVED ──lookup success──> RESOLVED
     |                             |
     | lookup fail                 | coordinator._running==False
     v                             | (stale invalidation)
BACKING_OFF <──────────────────────┘
     |
     | max_retries hit
     v
COOLDOWN (5min, env JARVIS_COORDINATOR_COOLDOWN_SECONDS)
     |
     | cooldown expires OR readiness event
     v
UNRESOLVED (new window, reset counters)
```

### Fields

```python
# Replace _neural_mesh_lookup_attempted with:
_coordinator_state: str = "UNRESOLVED"  # UNRESOLVED|BACKING_OFF|RESOLVED|COOLDOWN
_coordinator_last_lookup: float = 0.0
_coordinator_lookup_failures: int = 0
_coordinator_max_retries: int  # env JARVIS_COORDINATOR_LOOKUP_MAX_RETRIES, default 5
_coordinator_cooldown_seconds: float  # env JARVIS_COORDINATOR_COOLDOWN_SECONDS, default 300.0
_coordinator_lock: asyncio.Lock  # guards all state transitions
```

### Backoff Schedule

Exponential: 5s, 10s, 20s, 40s, 60s (cap). Total window ~135s before cooldown.

### Stale Coordinator Invalidation

On every access in RESOLVED state: check `getattr(coordinator, '_running', True)`. If False, transition to UNRESOLVED, clear cached reference, emit `coordinator_stale` metric.

### Readiness Event

Public method `notify_coordinator_ready()` that any subsystem can call to clear BACKING_OFF or COOLDOWN immediately. AGI OS calls this after successful mesh init.

### Concurrency Safety

`asyncio.Lock` acquired for every state read+write in `_get_neural_mesh_coordinator()`.

### Metrics

`workspace.coordinator_lookup{result=resolved|miss|cached|stale, attempt=N, source=integration|coordinator_module}`

---

## Section 2: Auth Recovery State Machine

**File:** `backend/neural_mesh/agents/google_workspace_agent.py`
**Feature flag:** `JARVIS_AUTH_STATE_MACHINE_V2` (default `true`)

### State Machine

```
AUTHENTICATED
     |
     | token expired / refresh error
     v
REFRESHING ──refresh success──> AUTHENTICATED
     |
     | permanent failure (invalid_grant, revoked)
     v
DEGRADED_VISUAL
     |  \
     |   \ write action --> NEEDS_REAUTH_GUIDED
     |    \
     | read action: execute via Computer Use
     | periodic API probe (every 120s, env JARVIS_AUTH_PROBE_INTERVAL)
     |   success --> AUTHENTICATED
     v
NEEDS_REAUTH_GUIDED
     |
     | token file mtime change + valid
     | OR explicit re-auth success
     v
UNAUTHENTICATED --> (normal auth flow)
```

### Transition Map (constant table)

```python
_AUTH_TRANSITIONS: List[AuthTransition] = [
    AuthTransition("AUTHENTICATED", "token_expired", "REFRESHING", "auth_refreshing"),
    AuthTransition("REFRESHING", "refresh_success", "AUTHENTICATED", "auth_healthy"),
    AuthTransition("REFRESHING", "transient_failure", "REFRESHING", "auth_refresh_transient_fail"),  # up to 3x
    AuthTransition("REFRESHING", "permanent_failure", "DEGRADED_VISUAL", "auth_refresh_permanent_fail"),
    AuthTransition("DEGRADED_VISUAL", "write_action", "NEEDS_REAUTH_GUIDED", "auth_guided_recovery"),
    AuthTransition("DEGRADED_VISUAL", "api_probe_success", "AUTHENTICATED", "auth_auto_healed"),
    AuthTransition("NEEDS_REAUTH_GUIDED", "token_healed", "UNAUTHENTICATED", "auth_auto_healed"),
]
```

### Auth State Lock

Single `asyncio.Lock` (`_auth_transition_lock`) guards all writes to `_auth_state`, counters, and timestamps.

### Visual Fallback Policy

| Action | DEGRADED_VISUAL | NEEDS_REAUTH_GUIDED |
|--------|----------------|---------------------|
| `fetch_unread` / `check_email` | Visual fallback | Visual fallback + guided prompt |
| `list_events` / `check_calendar` | Visual fallback | Visual fallback + guided prompt |
| `search_email` | Visual fallback | Visual fallback + guided prompt |
| `send_email` | Refuse → NEEDS_REAUTH_GUIDED | Refuse + guided prompt |
| `draft_email` | Refuse → NEEDS_REAUTH_GUIDED | Refuse + guided prompt |
| `create_event` | Refuse → NEEDS_REAUTH_GUIDED | Refuse + guided prompt |
| `delete_*` | Refuse → NEEDS_REAUTH_GUIDED | Refuse + guided prompt |

Default config change: `email_visual_fallback_enabled` default → `true` (for read-only actions only).
New field: `write_visual_fallback_enabled` default → `false`.

### Action Risk Classification

```python
_ACTION_RISK: Dict[str, Literal["read", "write", "high_risk_write"]] = {
    "fetch_unread": "read",
    "list_events": "read",
    "search_email": "read",
    "get_email": "read",
    "send_email": "write",
    "draft_email": "write",
    "create_event": "write",
    "update_event": "write",
    "delete_email": "high_risk_write",
    "delete_event": "high_risk_write",
}
```

### Periodic API Probe from DEGRADED_VISUAL

Background probe every 120s (env `JARVIS_AUTH_PROBE_INTERVAL`). Attempts lightweight API call (Gmail `users.getProfile`). On success → AUTHENTICATED. On failure → stay in DEGRADED_VISUAL. Bounded: max 30 probes (1 hour), then stop probing.

### Visual Fallback Bounds

Max 1 visual fallback attempt per command. No retry loop within visual path.

### Guided Recovery

Non-blocking voice prompt via `safe_say()`. Cooldown-enforced (reuse `_REAUTH_NOTICE_COOLDOWN`, default 30s). Message follows degraded-mode user message policy above.

### Structured Response Fields

Every degraded/guided response includes:
```python
{
    "auth_state": "degraded_visual",
    "tier_used": "visual_fallback",
    "recovery_action_required": True/False,
    "recovery_instructions": "Say 'fix my Google auth' or re-run...",
    "verification_passed": True/False,
}
```

### Metrics

- `workspace.auth_transition{from_state, to_state, reason_code}` — counter
- `workspace.visual_fallback{action, success, duration_ms}` — counter + histogram
- `workspace.guided_recovery{prompted, recovered}` — counter
- `workspace.auth_probe{result=success|fail, attempt}` — counter

### Acceptance Tests

1. Valid token: AUTHENTICATED, API success
2. Expired token, refresh succeeds: AUTHENTICATED → REFRESHING → AUTHENTICATED
3. Transient refresh failures then success within 3-retry budget
4. Permanent failure (invalid_grant): REFRESHING → DEGRADED_VISUAL
5. Read action in DEGRADED_VISUAL succeeds via visual fallback
6. Write action in DEGRADED_VISUAL → NEEDS_REAUTH_GUIDED with clear response
7. Token file replaced externally → auto-heal → UNAUTHENTICATED → AUTHENTICATED
8. Guided prompt cooldown works (no repeated voice spam)
9. Concurrent requests don't produce contradictory states

---

## Section 3: Startup Unification

**Files:** `backend/neural_mesh/integration.py`, `backend/agi_os/agi_os_coordinator.py`

### Root Cause

Two module-level singletons:
- `neural_mesh_coordinator.py:_coordinator` — set by `start_neural_mesh()` (AGI OS path)
- `integration.py:_neural_mesh_coordinator` — set by `initialize_neural_mesh()` (standalone path)

Command processor calls `integration.py:get_neural_mesh_coordinator()` → always returns None when AGI OS path was used.

### Fix: Public API in integration.py

```python
def set_neural_mesh_coordinator(coordinator) -> None:
    """Set the canonical coordinator reference.

    Called by AGI OS after start_neural_mesh() to cross-register.
    """
    global _neural_mesh_coordinator
    _neural_mesh_coordinator = coordinator

def mark_neural_mesh_initialized(initialized: bool = True) -> None:
    """Mark integration module's initialized flag."""
    global _initialized
    _initialized = initialized
```

### Fix: Canonical Accessor

```python
def get_neural_mesh_coordinator():
    """Get the global coordinator — checks all sources."""
    if _neural_mesh_coordinator is not None:
        return _neural_mesh_coordinator
    # Fallback: coordinator module's singleton
    try:
        from neural_mesh.neural_mesh_coordinator import _coordinator as _cm_coordinator
        if _cm_coordinator is not None and getattr(_cm_coordinator, '_running', False):
            return _cm_coordinator
    except (ImportError, Exception):
        pass
    return None
```

### Fix: AGI OS Cross-Registration

After `start_neural_mesh()` in `_init_neural_mesh()`:

```python
# Cross-register with integration module
try:
    from neural_mesh.integration import set_neural_mesh_coordinator, mark_neural_mesh_initialized
    set_neural_mesh_coordinator(self._neural_mesh)
    mark_neural_mesh_initialized(True)
except ImportError:
    pass
```

### Production Agent Registration Parity

Add to `initialize_neural_mesh()` (standalone path):

```python
if not _is_agent_set_registered(coordinator_instance_id, expected_agent_names):
    registered = await initialize_production_agents(coordinator)
    _mark_agent_set_registered(coordinator_instance_id, registered_agent_names)
```

Idempotency keyed by `(coordinator_instance_id, frozenset(agent_names))` — not just a boolean flag.

### Metrics

- `workspace.coordinator_registration{source=agi_os|integration, agents_count, coordinator_id}`
- `workspace.coordinator_cross_register{success, source}`

### Validation Scenarios

1. Startup via `unified_supervisor.py` → command processor resolves coordinator immediately
2. Startup via `initialize_neural_mesh()` direct path → same resolution
3. GoogleWorkspaceAgent visible in both paths
4. No duplicate registration after repeated startup calls
5. `is_neural_mesh_initialized()` consistent with running coordinator
6. Late mesh startup discovered via Section 1 retry logic
7. Coordinator restart invalidates stale reference, re-resolves

---

## Section 4: Post-Action Verification + Runtime Escalation

**File:** `backend/api/unified_command_processor.py`

### Normalization Contract (versioned)

```python
WORKSPACE_RESULT_CONTRACT_VERSION = "v1"

_WORKSPACE_RESULT_NORMALIZERS: Dict[str, Callable] = {
    "fetch_unread": _normalize_email_result,   # ensures "emails" key, canonical field names
    "list_events": _normalize_calendar_result,  # ensures "events" key, "summary"/"title" → "title"
    "search_email": _normalize_search_result,   # ensures "emails" key (not "results")
    "send_email": _normalize_send_result,       # ensures "message_id" key
    "draft_email": _normalize_draft_result,     # ensures "draft_id" key
    "create_event": _normalize_event_result,    # ensures "event_id" key
}
```

### Verification Contract Table

```python
_WORKSPACE_VERIFICATION_CONTRACTS: Dict[str, VerificationContract] = {
    "fetch_unread": VerificationContract(
        required_keys=["emails"],
        type_checks={"emails": list},
        item_required_keys=["subject", "from"],
        allow_empty=True,
    ),
    "list_events": VerificationContract(
        required_keys=["events"],
        type_checks={"events": list},
        item_required_keys=["title", "start"],
        allow_empty=True,
    ),
    "send_email": VerificationContract(
        required_keys=["message_id"],
        type_checks={"message_id": str},
        semantic_check=lambda v: bool(v.get("message_id")),
    ),
    ...
}
```

### Verification Failure Taxonomy

- `verify_passed` — all checks passed
- `verify_schema_fail` — required keys missing or wrong type
- `verify_semantic_fail` — values present but nonsensical
- `verify_empty_valid` — empty result that's semantically valid
- `verify_transport_fail` — network/timeout during fetch

### Replan Strategy (bounded)

```
Action result received
    |
    v
Normalize (workspace_result_contract_v1)
    |
    v
Verify (contract table)
    |
    ├── passed --> annotate + return
    |
    ├── failed (read action)
    |     |
    |     v
    |   Attempt 1: Same-tier retry (1x max)
    |     |
    |     ├── passed --> annotate + return
    |     v
    |   Attempt 2: Tier fallback (API→visual, visual→none)
    |     |
    |     ├── passed --> annotate + return
    |     v
    |   Attempt 3: Runtime escalation (if budget >= floor)
    |     |
    |     ├── completed --> annotate + return
    |     v
    |   Structured failure with attempt audit trail
    |
    ├── failed (write action, has idempotency key)
    |     |
    |     v
    |   Attempt 1: Same-tier retry (1x max, idempotent)
    |     |
    |     ├── passed --> annotate + return
    |     v
    |   Attempt 2: Runtime escalation (if budget >= floor)
    |
    └── failed (write action, NO idempotency key)
          |
          v
        Attempt 1: Tier fallback only (no same-tier retry)
          |
          v
        Structured failure (recovery_idempotency_blocked)
```

### Deadline Partitioning

Total remaining budget split into slices:
- Same-tier retry: 40% of remaining
- Tier fallback: 40% of remaining after retry
- Runtime escalation: remaining, minimum `JARVIS_RUNTIME_ESCALATION_FLOOR` (default 5.0s)

Each slice has hard minimum. If a slice would be < 2.0s, it's skipped.

### Runtime Escalation Binding

```python
runtime = get_agent_runtime()
if runtime and remaining_budget >= escalation_floor:
    goal_id = await runtime.submit_goal(
        description=f"Complete workspace action: {command_text}",
        priority=GoalPriority.NORMAL,
        source="workspace_replan",
        context={
            "action": action,
            "failed_tiers": failed_tiers,
            "auth_state": auth_state.value,
            "original_command_id": command_id,
            "attempt_history": attempts,
        },
    )
    # Poll with deadline
    result = await _await_runtime_goal(goal_id, timeout=remaining_budget)
```

`_await_runtime_goal()` polls `get_goal_status()` every 1s until COMPLETED/FAILED/timeout.

### Compose Path Invariant

When `compose_with_jprime=True`: verify action result FIRST, then compose. Never verify composed output (composition is presentation, not data).

### Attempt Audit Trail

Every response includes:
```python
result["_attempts"] = [
    {"strategy": "primary", "tier": "api", "outcome": "verify_schema_fail", "reason": "missing 'emails' key", "duration_ms": 234},
    {"strategy": "same_tier_retry", "tier": "api", "outcome": "verify_passed", "reason": None, "duration_ms": 189},
]
result["_verification"] = {
    "passed": True,
    "contract_version": "v1",
    "tier_used": "api",
    "auth_state": "authenticated",
    "recovery_path": "same_tier_retry",
}
```

### Metrics

- `workspace.verification{action, outcome, contract_version}` — counter
- `workspace.replan{action, strategy, from_tier, to_tier, success}` — counter
- `workspace.escalation{action, submitted, completed, duration_ms}` — counter + histogram
- `workspace.e2e{action, final_success, total_attempts, total_duration_ms}` — summary

Low-cardinality labels only. No command text in labels.

---

## Done Criteria

"Check my email" succeeds in all 4 conditions:
1. **Valid auth** → API path, verified output
2. **Expired token, refresh succeeds** → REFRESHING → AUTHENTICATED, verified
3. **Revoked token** → DEGRADED_VISUAL, visual fallback, verified
4. **Mesh late startup** → coordinator resolves via retry, verified

Additional invariants:
- No silent success without verified output
- No unbounded retries
- No duplicate write execution
- Structured failure with attempt audit trail on all failure paths
- Metrics emitted on every significant event
- Feature flag rollback for Section 2 auth state machine
