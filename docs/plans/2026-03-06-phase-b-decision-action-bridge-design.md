# Phase B: Wire the Decision-Action Bridge

**Date**: 2026-03-06
**Status**: Approved
**Prerequisite**: Phase A (formal contracts) -- complete
**Goal**: Wire Phase A contracts into the email triage pipeline so every
autonomous action flows through: envelope -> policy gate -> ledger reserve ->
pre-exec check -> execute -> commit/abort -> health record.

## Approach

Modified in-place wiring in `runner.py`. No new orchestrator abstraction.
The runner is already the orchestrator.

## Critical Rules (from review)

1. **Mandatory ledger for write actions** -- when autonomy is enabled and the
   action has side effects (label, notify), the ledger is not optional.
   If `reserve()` fails, execution is DENIED, not deferred.
2. **Fail-closed on invariant failure** -- if `check_pre_exec_invariants()`
   returns `(False, reason)`, the action is aborted. No fallthrough.
3. **Typed contracts everywhere** -- `PolicyContext` dataclass (not dict),
   all enum fields (not strings).
4. **Protocols in `core/contracts/`** -- triage-specific adapters in
   `backend/autonomy/email_triage/`.
5. **Explicit replay semantics** -- on startup, scan for RESERVED records
   that were never committed. Expire them and log for forensics.
6. **Health monitor recommends, runtime enforces** -- the monitor returns
   `ThrottleRecommendation`; the runner's `run_cycle()` acts on it.
7. **Backpressure is deterministic** -- `REDUCE_BATCH` uses
   `recommended_max_emails` directly, clamped to `[1, max_emails_per_cycle]`.

## File Changes

### New Files

| File | Purpose |
|---|---|
| `backend/core/contracts/policy_context.py` | Typed `PolicyContext` dataclass |
| `backend/autonomy/email_triage/triage_policy_gate.py` | `TriagePolicyGate` implementing `PolicyGate`, wraps `NotificationPolicy` |

### Modified Files

| File | Changes |
|---|---|
| `backend/autonomy/email_triage/runner.py` | Add contract lifecycle to `__init__`/`warm_up`/`run_cycle`, wrap stages in envelopes, add ledger + gate + health |
| `backend/autonomy/email_triage/config.py` | Add `ledger_lease_duration_s` config field |

### Files NOT Changed
- `extraction.py` (unchanged)
- `scoring.py` (unchanged)
- `policy.py` (unchanged -- wrapped, not modified)
- `labels.py` (unchanged)
- `notifications.py` (unchanged)
- `state_store.py` (unchanged)

## Contract 0: PolicyContext (typed, not dict)

```python
# backend/core/contracts/policy_context.py

@dataclass(frozen=True)
class PolicyContext:
    """Typed context for PolicyGate evaluation."""
    tier: int
    score: int
    message_id: str
    sender_domain: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    cycle_id: str
    fencing_token: int
    config_version: str
```

## TriagePolicyGate

```python
# backend/autonomy/email_triage/triage_policy_gate.py

class TriagePolicyGate:
    """Wraps NotificationPolicy behind the PolicyGate protocol."""

    def __init__(self, policy: NotificationPolicy):
        self._policy = policy

    async def evaluate(
        self, envelope: DecisionEnvelope, context: PolicyContext
    ) -> PolicyVerdict:
        # Build TriagedEmail from envelope payload + context
        # Call self._policy.decide_action(triaged)
        # Wrap result in PolicyVerdict
        ...
```

Signature note: the `PolicyGate` protocol specifies
`context: Dict[str, Any]` but `TriagePolicyGate` narrows to `PolicyContext`.
This is valid under Liskov (accepting a broader type is covariant on input).
We pass a `PolicyContext` which is also a valid dict-like frozen dataclass.
The protocol remains generic; the triage implementation is typed.

## Wiring in runner.py

### 1. New Imports and Instance Variables

```python
# New imports
from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
    EnvelopeFactory, IdempotencyKey,
)
from core.contracts.action_commit_ledger import ActionCommitLedger
from core.contracts.policy_context import PolicyContext
from autonomy.contracts.behavioral_health import (
    BehavioralHealthMonitor, ThrottleRecommendation,
)
from autonomy.email_triage.triage_policy_gate import TriagePolicyGate

# In trace_envelope (existing file)
from core.trace_envelope import LamportClock

# New instance vars in __init__:
self._envelope_factory = EnvelopeFactory(clock=LamportClock())
self._health_monitor = BehavioralHealthMonitor()
self._commit_ledger: Optional[ActionCommitLedger] = None
self._policy_gate = TriagePolicyGate(self._policy)
self._runner_id = f"runner-{uuid4().hex[:8]}"
```

### 2. warm_up() -- Initialize Ledger

```python
# After state store init:
if self._config.state_persistence_enabled:
    from pathlib import Path
    parent = Path(self._config.state_db_path).parent if self._config.state_db_path else Path.home() / ".jarvis"
    ledger_path = parent / "action_commits.db"
    self._commit_ledger = ActionCommitLedger(ledger_path)
    await self._commit_ledger.start()
    # Replay semantics: expire any stale RESERVED records from prior crash
    expired = await self._commit_ledger.expire_stale()
    if expired > 0:
        logger.info("Expired %d stale ledger reservations from prior session", expired)
```

### 3. run_cycle() -- Throttle Check (before cycle starts)

```python
# After enabled check, before fetch:
rec, throttle_reason = self._health_monitor.should_throttle()
if rec == ThrottleRecommendation.CIRCUIT_BREAK:
    return TriageCycleReport(
        ..., skipped=True, skip_reason=f"circuit_break:{throttle_reason}",
    )
if rec == ThrottleRecommendation.PAUSE_CYCLE:
    return TriageCycleReport(
        ..., skipped=True, skip_reason=f"pause:{throttle_reason}",
    )
# REDUCE_BATCH: applied during admission gate
health_report = self._health_monitor.check_health()
health_max_emails = health_report.recommended_max_emails
```

### 4. Admission Gate -- Apply REDUCE_BATCH

```python
# Existing admission code, modified:
effective_max = self._config.max_emails_per_cycle
if health_max_emails is not None:
    effective_max = max(1, min(effective_max, health_max_emails))
# Use effective_max instead of max_emails_per_cycle in _compute_budget
```

### 5. Per-Email Processing -- Envelope + Gate + Ledger

For each email after extraction and scoring:

```python
cycle_envelopes: List[DecisionEnvelope] = []

for result in extraction_results:
    # ... existing extraction handling ...

    # === ENVELOPE: Extraction ===
    source_enum = _map_extraction_source(features.extraction_source)
    extraction_envelope = self._envelope_factory.create(
        trace_id=cycle_id,
        decision_type=DecisionType.EXTRACTION,
        source=source_enum,
        origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
        payload={
            "message_id": features.message_id,
            "extraction_source": features.extraction_source,
            "extraction_confidence": features.extraction_confidence,
        },
        confidence=features.extraction_confidence,
        config_version=self._triage_schema_version,
    )
    cycle_envelopes.append(extraction_envelope)

    # ... existing scoring ...

    # === ENVELOPE: Scoring ===
    scoring_envelope = self._envelope_factory.create(
        trace_id=cycle_id,
        decision_type=DecisionType.SCORING,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
        payload={
            "message_id": features.message_id,
            "score": scoring.score,
            "tier": scoring.tier,
        },
        confidence=1.0,
        config_version=self._config.scoring_version,
        parent_envelope_id=extraction_envelope.envelope_id,
    )
    cycle_envelopes.append(scoring_envelope)

    # === POLICY GATE (replaces direct decide_action) ===
    policy_context = PolicyContext(
        tier=scoring.tier, score=scoring.score,
        message_id=features.message_id,
        sender_domain=features.sender_domain,
        is_reply=features.is_reply,
        has_attachment=features.has_attachment,
        label_ids=features.label_ids,
        cycle_id=cycle_id,
        fencing_token=self._current_fencing_token,
        config_version=self._config.scoring_version,
    )
    verdict = await self._policy_gate.evaluate(scoring_envelope, policy_context)

    # Derive action from verdict (backwards compat with existing flow)
    if verdict.allowed:
        action = verdict.reason  # TriagePolicyGate puts the action name in metadata
    else:
        action = "label_only" if verdict.action == VerdictAction.DENY else "summary"

    # === LEDGER: Reserve -> Pre-exec -> Execute -> Commit/Abort ===
    idem_key = IdempotencyKey.build(
        DecisionType.ACTION, features.message_id,
        "triage", self._config.scoring_version,
    )

    commit_id = None
    if self._commit_ledger:
        # Duplicate check
        if await self._commit_ledger.is_duplicate(idem_key):
            errors.append(f"duplicate:{features.message_id}")
            emails_processed += 1
            continue

        # Reserve (MANDATORY for write actions)
        try:
            commit_id = await self._commit_ledger.reserve(
                envelope=scoring_envelope,
                action="triage",
                target_id=features.message_id,
                fencing_token=self._current_fencing_token,
                lock_owner=self._runner_id,
                session_id=cycle_id,
                idempotency_key=idem_key,
                lease_duration_s=self._config.ledger_lease_duration_s,
            )
        except Exception as e:
            # FAIL CLOSED: deny action on reserve failure
            errors.append(f"ledger_reserve:{features.message_id}:{e}")
            emails_processed += 1
            continue

        # Pre-exec invariant check
        ok, inv_reason = await self._commit_ledger.check_pre_exec_invariants(
            commit_id, self._current_fencing_token,
        )
        if not ok:
            await self._commit_ledger.abort(commit_id, inv_reason)
            errors.append(f"pre_exec:{features.message_id}:{inv_reason}")
            emails_processed += 1
            continue

    # === EXECUTE: Label + Notify (existing logic, unchanged) ===
    action_succeeded = True
    try:
        await self._apply_label(features.message_id, scoring.tier_label)
    except Exception as label_err:
        action_succeeded = False
        errors.append(f"label:{features.message_id}:{label_err}")

    # ... notification logic (immediate_emails.append etc.) ...

    # === LEDGER: Commit or Abort ===
    if commit_id and self._commit_ledger:
        try:
            if action_succeeded:
                await self._commit_ledger.commit(
                    commit_id, outcome="success",
                    metadata={"tier": scoring.tier, "action": action},
                )
            else:
                await self._commit_ledger.abort(commit_id, "action_failed")
        except Exception as e:
            logger.warning("Ledger commit/abort failed for %s: %s",
                          features.message_id, e)

    emails_processed += 1
```

### 6. After Cycle -- Health Recording + Stale Expiry

```python
# After report is built, before snapshot commit:
self._health_monitor.record_cycle(report, cycle_envelopes)

# Expire stale leases (periodic cleanup)
if self._commit_ledger:
    try:
        await self._commit_ledger.expire_stale()
    except Exception:
        pass
```

### 7. Helper: Map extraction_source to DecisionSource enum

```python
def _map_extraction_source(source_str: str) -> DecisionSource:
    _SOURCE_MAP = {
        "heuristic": DecisionSource.HEURISTIC,
        "jprime_v1": DecisionSource.JPRIME_V1,
        "jprime_degraded_fallback": DecisionSource.JPRIME_DEGRADED,
    }
    return _SOURCE_MAP.get(source_str, DecisionSource.HEURISTIC)
```

## Replay Semantics for Reserved-But-Uncommitted

On startup (`warm_up()`), the ledger calls `expire_stale()` which transitions
all RESERVED records past their `expires_at_monotonic` to EXPIRED.

**Why expire (not replay):** A reserved-but-uncommitted record means the
action was either never executed or executed without confirmation. Since
Gmail labels and notifications are idempotent at the application level
(re-applying a label is a no-op, re-sending a notification is low-harm),
expiring is the safe default. The next triage cycle will re-process
the same emails and create fresh reservations.

**Forensics:** Expired records remain in the ledger for audit. They can be
queried via `ledger.query(state=CommitState.EXPIRED)`.

## Partial Commit Window Analysis

| Crash Point | State | Recovery |
|---|---|---|
| After `reserve()`, before action | RESERVED in DB | `expire_stale()` on next startup |
| After action, before `commit()` | RESERVED in DB, label applied | `expire_stale()` on next startup, label re-applied (idempotent) |
| After `commit()` | COMMITTED in DB | Clean -- no recovery needed |
| After `abort()` | ABORTED in DB | Clean -- action was not executed |

## Config Additions

```python
# In TriageConfig:
ledger_lease_duration_s: float = 60.0  # generous: cycle_timeout + buffer

# In from_env():
ledger_lease_duration_s=_env_float("EMAIL_TRIAGE_LEDGER_LEASE_S", 60.0),
```

## Backpressure Policy (deterministic, bounded)

When `BehavioralHealthMonitor` recommends `REDUCE_BATCH`:
- `recommended_max_emails` is computed as `max(1, int(rolling_mean * 0.5))`
- Applied via `effective_max = max(1, min(config.max_emails_per_cycle, recommended_max_emails))`
- Clamped to `[1, max_emails_per_cycle]` -- never oscillates below 1 or above config max
- Resets when health returns to NONE (window clears)

## Done Criteria (10 gates)

1. Every extraction result wrapped in `DecisionEnvelope(EXTRACTION)`
2. Every scoring result wrapped in `DecisionEnvelope(SCORING, parent=extraction)`
3. `TriagePolicyGate.evaluate()` called for every scored email
4. `ActionCommitLedger.reserve()` called before every write action
5. `check_pre_exec_invariants()` called between reserve and execute
6. `commit()` on success, `abort()` on failure -- no silent drops
7. `BehavioralHealthMonitor.record_cycle()` called after every cycle
8. `should_throttle()` checked before every cycle (CIRCUIT_BREAK/PAUSE skip)
9. `expire_stale()` called on startup and after each cycle
10. All existing email triage tests still pass (zero regressions)

## Test Plan

- Unit tests for `TriagePolicyGate` (wraps NotificationPolicy correctly)
- Unit tests for `PolicyContext` (frozen, typed)
- Integration test: mock full cycle, verify envelope chain is created
- Integration test: verify ledger reserve/commit lifecycle in cycle
- Integration test: verify health throttle skips cycle
- Integration test: verify fail-closed on reserve failure
- Regression: all 223 existing email triage tests pass
