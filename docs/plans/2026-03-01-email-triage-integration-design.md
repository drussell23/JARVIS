# Email Triage Integration Layer — Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create the implementation plan from this design.

**Goal:** Wire the autonomous Gmail triage system to live infrastructure so it fetches real emails, uses J-Prime extraction, routes notifications through the notification bridge, and enriches "check my email" responses with triage context.

**Prerequisite:** Email triage v1 core (Tasks 1-11) is complete — 77/77 tests, 11 commits on main. The system scores, labels, and decides notifications but currently operates with `workspace_agent=None` and `router=None`.

**Scope boundary:** v1.1 integration layer. No AgenticTaskRunner, no persistent triage DB, no new UI.

---

## Section 1: Runner Dependency Resolution

### Problem

`EmailTriageRunner.get_instance()` creates a runner with no dependencies injected. The runner can score emails (deterministic) but cannot fetch them from Gmail or use J-Prime for extraction.

### Design: Hybrid Lazy Resolution + Injectable Overrides

Three dependencies, each lazy-resolved on first use with injectable override for tests/runtime:

| Dependency | Type | Resolution Path | Degraded Behavior |
|-----------|------|----------------|-------------------|
| `workspace_agent` | **Required** | `get_google_workspace_agent()` | Returns normal report with `emails_fetched=0` (no emails to process) |
| `router` (PrimeRouter) | **Optional** | `get_prime_router()` | Heuristic-only extraction (no J-Prime features) |
| `notifier` | **Optional** | `notification_bridge.notify_user` | Label-only mode (labels applied, no voice/push/macOS notifications) |

### Dependency Health Tracking

```python
@dataclass
class DependencyHealth:
    resolved: bool = False
    instance: Any = None           # The resolved dependency (or None)
    last_resolve_at: float = 0.0   # monotonic timestamp
    last_resolve_error: Optional[str] = None
    consecutive_failures: int = 0
    next_attempt_at: float = 0.0   # monotonic — backoff-controlled
```

One `DependencyHealth` per dependency, stored on the runner singleton.

### Resolution Behavior

- **On first `run_cycle()`**: Attempt resolution for all unresolved deps.
- **On failure**: Record error, increment `consecutive_failures`, compute next attempt via backoff.
- **On success**: Set `resolved=True`, store instance, reset `consecutive_failures` to 0.
- **Injectable override**: `EmailTriageRunner(workspace_agent=agent)` sets `resolved=True` immediately, bypasses lazy path. Tests use this exclusively.

### Backoff Formula

```
interval = min(base_s * 2^consecutive_failures, max_interval_s) * uniform(0.8, 1.2)
```

- `base_s` = 5.0 (env `EMAIL_TRIAGE_DEP_BACKOFF_BASE_S`)
- `max_interval_s` = 300.0 (env `EMAIL_TRIAGE_DEP_BACKOFF_MAX_S`)
- Clock: `time.monotonic()` (not wall clock)
- Jitter: multiplicative uniform [0.8, 1.2]

### Cache Invalidation Rules

- **Resolution failure** (ImportError, RuntimeError, timeout): Invalidate, increment failures, apply backoff.
- **Runtime error during use** (API error, network timeout): Invalidate, increment failures, apply backoff.
- **User-actionable state** (workspace_agent in `NEEDS_REAUTH`): Do NOT invalidate the dependency. Return report with `emails_fetched=0` and emit `EVENT_DEPENDENCY_DEGRADED` (not `UNAVAILABLE`). The dependency is correctly resolved; the operational state requires user action.

---

## Section 2: Resolution Contracts & Degradation Semantics

### Required vs Optional Classification

**Required: `workspace_agent`**
- Without it, `emails_fetched=0` — triage has nothing to process.
- Report is NOT skipped (no `skip_reason`). It's a normal cycle that found zero emails.
- Events emitted: `EVENT_DEPENDENCY_UNAVAILABLE` when resolution fails, `EVENT_DEPENDENCY_DEGRADED` when resolved but auth-limited.

**Optional: `router` (PrimeRouter)**
- Without it, extraction falls back to heuristic-only mode (`extraction_enabled` effectively forced to `False` for J-Prime features).
- Scoring still works (deterministic rules don't need J-Prime).
- Event: `EVENT_DEPENDENCY_UNAVAILABLE` on resolution failure (informational, not blocking).

**Optional: `notifier` (notification_bridge)**
- Without it, labels are still applied, events still emitted, but no voice/push/macOS notifications.
- Label-only mode is a valid production configuration.
- Event: `EVENT_DEPENDENCY_UNAVAILABLE` on resolution failure (informational, not blocking).

### Event Taxonomy

Two distinct events (not overloaded):

- **`EVENT_DEPENDENCY_UNAVAILABLE`**: Resolution failed — import error, singleton not initialized, timeout. Includes: `dependency_name`, `error`, `consecutive_failures`, `next_retry_at`.
- **`EVENT_DEPENDENCY_DEGRADED`**: Resolved but operating in limited state — `NEEDS_REAUTH`, circuit breaker open, rate limited. Includes: `dependency_name`, `degraded_reason`, `capabilities_affected`.

### Degradation Matrix

| workspace_agent | router | notifier | Behavior |
|----------------|--------|----------|----------|
| Resolved + healthy | Resolved | Resolved | Full pipeline: fetch → extract (J-Prime) → score → label → notify |
| Resolved + healthy | Unavailable | Resolved | Heuristic extraction → score → label → notify |
| Resolved + healthy | Resolved | Unavailable | Full pipeline but label-only (no notifications) |
| Resolved + NEEDS_REAUTH | Any | Any | `emails_fetched=0`, emit `EVENT_DEPENDENCY_DEGRADED` |
| Unavailable | Any | Any | `emails_fetched=0`, emit `EVENT_DEPENDENCY_UNAVAILABLE` |

---

## Section 3: Notification Routing & Side-Effect Isolation

### Core Invariant

**Notification delivery failure must NEVER change triage score, tier, or label outcome.**

Pipeline order enforces this:

```
extract → score → label → decide → record event → THEN notify
```

All scoring/labeling decisions are committed before notification delivery begins. If notification fails, the email is still correctly triaged and labeled.

### Notification Routing

The runner delegates to `notification_bridge.notify_user()` through a thin adapter:

```python
async def _deliver_notification(
    self,
    email: TriagedEmail,
    action: NotificationAction,
    deadline: float,
) -> NotificationDeliveryResult:
```

**Urgency mapping** (from triage tier to NotificationUrgency):

| Triage Tier | NotificationUrgency | Delivery |
|-------------|-------------------|----------|
| Tier 1 (critical) | URGENT (4) | Immediate — voice + push + macOS |
| Tier 2 (high) | HIGH (3) | Immediate — push + macOS |
| Cycle summary | NORMAL (2) | Batched at cycle end |
| Tier 3/4 | None | Label-only, no notification |

### Bounded Async Delivery

All notification delivery uses bounded async — no orphan fire-and-forget:

```python
# Immediate notifications (tier 1-2): gather with timeout
results = await asyncio.wait_for(
    asyncio.gather(*immediate_tasks, return_exceptions=True),
    timeout=notification_budget_s,
)

# Summary notification: single wait_for
summary_result = await asyncio.wait_for(
    self._send_summary(buffer),
    timeout=summary_budget_s,
)
```

`notification_budget_s` = remaining cycle budget minus 2s headroom.

### Dedup Key Construction

```python
dedup_key = f"triage:{action.action_type}:{email.message_id}"
```

Includes `message_id` as stable differentiator — prevents collisions between emails with similar subjects.

### Early Flush for High Volume

When processing >N emails (default 10, env `EMAIL_TRIAGE_IMMEDIATE_FLUSH_THRESHOLD`), flush accumulated tier 1-2 immediates every N emails instead of batching all to the end. Prevents notification delay when cycle processes 50+ emails.

### Summary Buffer

`NotificationPolicy._summary_buffer` holds tier 2-3 emails for end-of-cycle summary. Buffer is:
- Cleared after successful summary delivery.
- Preserved on delivery failure (retry next cycle).
- Capped at `max_summary_items` (default 20, env `EMAIL_TRIAGE_MAX_SUMMARY_ITEMS`) — oldest items evicted.

**Restart durability (v1.1 scope):** Buffer is in-memory only. On restart, buffer is lost and next cycle rebuilds from fresh Gmail fetch. Acceptable for v1.1; persistent buffer is a v2 concern. Documented as expected behavior.

### Event Separation

- **`EVENT_NOTIFICATION_SENT`**: Triage decision event — records that a notification was decided (tier, action_type, email_id). Emitted regardless of delivery success.
- **`EVENT_NOTIFICATION_DELIVERY_RESULT`**: Delivery outcome event — records success/failure, channel, latency, error. Separate from the decision event.

---

## Section 4: Command Processor Consistency

### Enforced Rule

**"check my email" must read from the same triage truth model when fresh, and fall back cleanly when stale/unavailable.**

### The Gap

`agent_runtime._maybe_run_email_triage()` runs the triage cycle but discards the `TriageCycleReport`. `_try_workspace_fast_path()` in `unified_command_processor.py` fetches raw emails via `GoogleWorkspaceAgent.execute_email_check()` with no triage awareness. Two independent subsystems talk to Gmail, neither reads from the other.

### Architecture: Single Source of Truth with Freshness Semantics

The `EmailTriageRunner` singleton is the single owner of triage truth:

```python
# On the runner singleton:
_last_report: Optional[TriageCycleReport] = None
_last_report_at: float = 0.0          # monotonic timestamp
_triaged_emails: Dict[str, TriagedEmail] = {}  # {msg_id: TriagedEmail}
_report_lock: asyncio.Lock             # Concurrency guard
_triage_schema_version: str            # e.g. "1.0"
_policy_version: str                   # From TriageConfig
```

**Freshness contract:**
- `staleness_window_s` = `poll_interval_s * 2` (default 120s, env `EMAIL_TRIAGE_STALENESS_WINDOW_S`)
- `get_fresh_results(staleness_window_s=None) -> Optional[TriageCycleReport]`: Returns report only if within window, else `None`.
- `get_triaged_email(msg_id: str) -> Optional[TriagedEmail]`: Per-message lookup for enrichment.
- Clock: `time.monotonic()`.

### Concurrency Guard

`_report_lock` (asyncio.Lock) protects all reads and writes to `_last_report` / `_triaged_emails`:

- **Writer (run_cycle)**: Acquires lock, builds complete new snapshot, atomically swaps `_last_report` and `_triaged_emails`, releases lock.
- **Reader (command processor)**: Acquires lock, reads `_last_report` reference and `_last_report_at`, releases lock. Reads are fast (reference copy, not deep copy).
- **Partial-cycle semantics**: If `run_cycle()` errors mid-run, the previous snapshot remains in place. The new snapshot is only committed when the cycle completes (even with per-email errors). This prevents serving half-built truth.

### Single-Writer Guarantee

`EmailTriageRunner` is a singleton within one Python process. `agent_runtime._maybe_run_email_triage()` is the only caller of `run_cycle()`, gated by its cooldown timer. Multi-process scenarios (multiple runtimes) are not supported in v1.1 — documented as a constraint. If multi-process becomes necessary, the singleton pattern must be replaced with a distributed lock (DLM already exists in codebase).

### Command Processor Integration

Insertion point: `_try_workspace_fast_path()` **after** `execute_email_check()` returns raw emails, **before** J-Prime compose. Enrichment, not replacement.

```
Step 1: WorkspaceIntentDetector → CHECK_EMAIL (unchanged)
Step 2: GoogleWorkspaceAgent.execute_email_check() → raw emails (unchanged)
Step 3: [NEW] Triage enrichment — enrich_with_triage()
Step 4: J-Prime compose (unchanged — richer context when available)
```

### Enrichment Function

```python
def enrich_with_triage(
    emails: List[Dict],
    runner: Optional[EmailTriageRunner],
    staleness_window_s: float = 120.0,
) -> Tuple[List[Dict], bool, Optional[float]]:
    """
    Returns (enriched_emails, was_enriched, triage_age_s).

    - runner is None or results stale: returns (emails, False, None)
    - Fresh results: merges triage_tier, triage_score into each email
      by message_id match. Unmatched emails pass through without fields.
    - triage_age_s: seconds since last triage cycle (for compose context).
    - Checks triage_schema_version compatibility before enriching.

    Invariants:
    - len(output) == len(input) — never removes emails
    - Never reorders emails
    - Never modifies scoring/tier values (read-only)
    - Pure function — no side effects, no network, no exceptions
    """
```

### Compose Context

When `was_enriched=True`:
- `triage_available: True` flag in compose prompt template.
- `triage_age_s` passed into context so responses can reference freshness.
- Tier summaries appended as **structured context** (JSON block), not free text, to prevent compose drift or hallucinated priority claims.

When `was_enriched=False`:
- Compose prompt identical to today's. Zero behavioral change.

### Schema Version Compatibility

`enrich_with_triage()` checks `runner._triage_schema_version` against a known-compatible set. If the runner's schema version is unknown (e.g., after a code upgrade), enrichment is skipped gracefully. This prevents stale enrichment logic from misinterpreting new schema fields.

### Fallback Hierarchy

| Scenario | Behavior |
|----------|----------|
| Triage enabled, fresh results | Full enrichment with tier context |
| Triage enabled, stale results | Raw emails, no enrichment |
| Triage disabled | Runner doesn't exist → `None` → no enrichment |
| Runner exists, last cycle errored | Previous full snapshot serves (partial-cycle semantics) |
| New emails since last cycle | Pass through without triage fields |

### Restart Cold-Start Gap

On restart, `_last_report` is `None` until first `run_cycle()` completes. First "check my email" after boot returns raw emails without triage. This is expected v1.1 behavior — documented, not a bug.

### What We Do NOT Do in v1.1

1. **No on-demand triage from command path** — "check my email" is a reader, not a writer. Triage runs on the housekeeping loop's schedule.
2. **No persistent triage DB** — in-memory singleton only. Restart clears, next cycle rebuilds.
3. **No email re-sorting** — triage tier is metadata, not sort order. Priority-first sorting is a separate future flag (`TRIAGE_SORT_BY_TIER`).
4. **No blocking on triage** — if `get_fresh_results()` returns `None`, proceed immediately.
5. **No multi-process triage** — single-writer within one process. Distributed lock needed for multi-process (documented constraint).

---

## Cross-Cutting Concerns

### Clock Discipline

- **Monotonic (`time.monotonic()`)**: Freshness windows, backoff timers, `_last_report_at`, `_last_resolve_at`, `next_attempt_at`.
- **Wall clock (`time.time()`)**: `TriageCycleReport.started_at` / `completed_at` (for human-readable audit), quiet-hours policy evaluation.

### Privacy Boundary

Triage enrichment fields (`triage_tier`, `triage_score`) are numeric metadata. Email content (subject, body, snippet) is NOT copied into triage cache — it stays in the workspace agent's response. The enrichment function matches by `message_id` only.

### Prompt Hardening

When triage context is included in J-Prime compose:
- Tier/score data is passed as structured JSON, not interpolated into prompt text.
- Compose template uses explicit field references (`{triage.tier_counts}`) not free-form injection.
- Compose output is validated: if it references tiers/priorities not present in the structured context, the deterministic template is used instead.

### Environment Variables (All New)

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMAIL_TRIAGE_DEP_BACKOFF_BASE_S` | 5.0 | Dependency resolution backoff base |
| `EMAIL_TRIAGE_DEP_BACKOFF_MAX_S` | 300.0 | Dependency resolution backoff cap |
| `EMAIL_TRIAGE_STALENESS_WINDOW_S` | 120.0 | Freshness window for command processor |
| `EMAIL_TRIAGE_IMMEDIATE_FLUSH_THRESHOLD` | 10 | Flush tier 1-2 notifications every N emails |
| `EMAIL_TRIAGE_MAX_SUMMARY_ITEMS` | 20 | Max emails in summary notification |
| `EMAIL_TRIAGE_NOTIFICATION_BUDGET_S` | 10.0 | Total notification delivery budget per cycle |
| `EMAIL_TRIAGE_SUMMARY_BUDGET_S` | 5.0 | Summary notification delivery budget |

---

## Files to Create/Modify

### Create
- `backend/autonomy/email_triage/dependencies.py` — DependencyHealth, resolution logic, backoff
- `backend/autonomy/email_triage/notifications.py` — Notification adapter, urgency mapping, delivery
- `backend/autonomy/email_triage/enrichment.py` — `enrich_with_triage()` pure function
- `tests/unit/backend/email_triage/test_dependencies.py`
- `tests/unit/backend/email_triage/test_notifications.py`
- `tests/unit/backend/email_triage/test_enrichment.py`

### Modify
- `backend/autonomy/email_triage/runner.py` — Add dependency resolution, triage cache, report lock, partial-cycle semantics
- `backend/autonomy/email_triage/config.py` — Add new env vars
- `backend/autonomy/email_triage/schemas.py` — Add `triage_schema_version`, `policy_version` to report; add `NotificationDeliveryResult`
- `backend/autonomy/email_triage/__init__.py` — Export new public API
- `backend/api/unified_command_processor.py` — Add triage enrichment call in workspace fast-path
- `backend/autonomy/agent_runtime.py` — Capture `run_cycle()` return value (currently discarded)
