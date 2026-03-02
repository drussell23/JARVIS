# Autonomous Gmail Triage v1 — Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create the implementation plan from this design.

**Goal:** Implement v1 autonomous Gmail triage — score, label, and selectively notify on incoming emails with zero destructive actions.

**Architecture:** Layered package at `backend/autonomy/email_triage/` with pure deterministic scoring, J-Prime structured extraction, Gmail label management, configurable notification policy, and observability. Integrates into `agent_runtime.py` housekeeping loop behind `EMAIL_TRIAGE_ENABLED` feature flag.

**Tech Stack:** Python 3.11, Gmail API v1, PrimeRouter (structured extraction), asyncio, dataclasses (frozen for immutability).

---

## Data Flow

```
agent_runtime.housekeeping_loop (every 5s)
    → _maybe_run_email_triage() (gated by flag + 60s cooldown)
        → EmailTriageRunner.run_cycle()
            → GoogleWorkspaceAgent._fetch_unread_emails(limit=25)
            → For each email:
                → extraction.extract_features(email, router) → EmailFeatures
                → scoring.score_email(features, config) → ScoringResult
                → labels.apply_label(msg_id, tier_label)
                → policy.decide_action(triaged) → notification_action
                → events.emit_triage_event("email_triaged", ...)
            → policy.maybe_flush_summary() (every 30min)
```

## Package Structure

```
backend/autonomy/email_triage/
├── __init__.py          # Public API: EmailTriageRunner, TriageConfig
├── schemas.py           # EmailFeatures, ScoringResult, TriagedEmail, TriageCycleReport
├── config.py            # TriageConfig dataclass, all env vars + feature flags
├── extraction.py        # extract_features() — J-Prime structured extraction
├── scoring.py           # score_email() — pure deterministic scoring (no I/O)
├── policy.py            # NotificationPolicy — quiet hours, dedup, budget, summaries
├── labels.py            # ensure_labels_exist(), apply_label() — Gmail label CRUD
├── runner.py            # EmailTriageRunner — periodic loop, orchestrates pipeline
├── events.py            # emit_triage_event() — 7 structured event types
└── replay.py            # Replay harness for deterministic test verification
```

## Schemas (schemas.py)

```python
@dataclass(frozen=True)
class EmailFeatures:
    message_id: str
    sender: str
    sender_domain: str
    subject: str
    snippet: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    keywords: Tuple[str, ...]         # extracted by J-Prime
    sender_frequency: str             # "first_time" | "occasional" | "frequent"
    urgency_signals: Tuple[str, ...]  # "deadline", "action_required", etc.
    extraction_confidence: float      # 0.0-1.0

@dataclass(frozen=True)
class ScoringResult:
    score: int                # 0-100
    tier: int                 # 1-4
    tier_label: str           # "jarvis/tier1_critical"
    breakdown: Dict[str, float]  # per-factor scores for observability
    idempotency_key: str      # sha256(message_id + scoring_version)

@dataclass
class TriagedEmail:
    features: EmailFeatures
    scoring: ScoringResult
    notification_action: str  # "immediate" | "summary" | "label_only" | "quarantine"
    processed_at: float       # time.time()

@dataclass
class TriageCycleReport:
    cycle_id: str             # uuid4
    started_at: float
    completed_at: float
    emails_fetched: int
    emails_processed: int
    tier_counts: Dict[int, int]  # {1: 2, 2: 5, 3: 8, 4: 10}
    notifications_sent: int
    notifications_suppressed: int
    errors: List[str]
    skipped: bool = False
    skip_reason: Optional[str] = None
```

## Config (config.py)

```python
@dataclass
class TriageConfig:
    # Feature flags
    enabled: bool              # EMAIL_TRIAGE_ENABLED (default: False)
    notify_tier1: bool         # EMAIL_TRIAGE_NOTIFY_TIER1 (default: True)
    notify_tier2: bool         # EMAIL_TRIAGE_NOTIFY_TIER2 (default: True)
    quarantine_tier4: bool     # EMAIL_TRIAGE_QUARANTINE_TIER4 (default: False)
    extraction_enabled: bool   # EMAIL_TRIAGE_EXTRACTION_ENABLED (default: True)
    summaries_enabled: bool    # EMAIL_TRIAGE_SUMMARIES_ENABLED (default: True)

    # Scoring
    scoring_version: str = "v1"

    # Tier thresholds
    tier1_min: int = 85        # 85-100 = critical
    tier2_min: int = 65        # 65-84 = high
    tier3_min: int = 35        # 35-64 = review
    # tier4 = 0-34 = noise

    # Gmail labels
    label_tier1: str = "jarvis/tier1_critical"
    label_tier2: str = "jarvis/tier2_high"
    label_tier3: str = "jarvis/tier3_review"
    label_tier4: str = "jarvis/tier4_noise"

    # Quiet hours (local time)
    quiet_start_hour: int = 23  # EMAIL_TRIAGE_QUIET_START
    quiet_end_hour: int = 8     # EMAIL_TRIAGE_QUIET_END

    # Dedup windows (seconds)
    dedup_tier1_s: int = 900    # 15 min
    dedup_tier2_s: int = 3600   # 60 min

    # Interrupt budget
    max_interrupts_per_hour: int = 3   # EMAIL_TRIAGE_MAX_INTERRUPTS_HOUR
    max_interrupts_per_day: int = 12   # EMAIL_TRIAGE_MAX_INTERRUPTS_DAY

    # Summary
    summary_interval_s: int = 1800     # 30 min

    # Runner
    poll_interval_s: float = 60.0      # EMAIL_TRIAGE_POLL_INTERVAL_S
    max_emails_per_cycle: int = 25     # EMAIL_TRIAGE_MAX_PER_CYCLE
    cycle_timeout_s: float = 30.0      # EMAIL_TRIAGE_CYCLE_TIMEOUT_S
```

## Scoring Engine (scoring.py)

Pure function. No I/O. Deterministic for same inputs.

```python
def score_email(features: EmailFeatures, config: TriageConfig) -> ScoringResult:
    sender_score = _score_sender(features, config)       # 30%
    content_score = _score_content(features, config)      # 35%
    urgency_score = _score_urgency(features, config)      # 25%
    context_score = _score_context(features, config)      # 10%

    raw = (sender_score * 0.30 + content_score * 0.35
           + urgency_score * 0.25 + context_score * 0.10)
    score = int(round(raw * 100))
    score = max(0, min(100, score))

    tier = _score_to_tier(score, config)
    tier_label = _tier_to_label(tier, config)
    idempotency_key = hashlib.sha256(
        f"{features.message_id}:{config.scoring_version}".encode()
    ).hexdigest()[:16]

    return ScoringResult(
        score=score, tier=tier, tier_label=tier_label,
        breakdown={"sender": sender_score, "content": content_score,
                   "urgency": urgency_score, "context": context_score},
        idempotency_key=idempotency_key,
    )
```

Factor scoring:
- **sender** (30%): known contacts=0.9, company domain=0.7, first_time=0.3, frequency boost
- **content** (35%): urgency keywords in subject ("urgent", "deadline", "action required"=0.9), length, has_attachment boost
- **urgency** (25%): urgency_signals from extraction, is_reply boost, time-sensitive keywords
- **context** (10%): label context (INBOX vs CATEGORY_PROMOTIONS), thread depth

Tier mapping:
- Tier 1 (85-100): `jarvis/tier1_critical`
- Tier 2 (65-84): `jarvis/tier2_high`
- Tier 3 (35-64): `jarvis/tier3_review`
- Tier 4 (0-34): `jarvis/tier4_noise`

## Extraction (extraction.py)

J-Prime does structured feature extraction. JARVIS owns the scoring.

```python
async def extract_features(
    email_dict: Dict[str, Any],
    router,  # PrimeRouter instance
    deadline: Optional[float] = None,
) -> EmailFeatures:
    """Extract structured features from raw email dict.

    Falls back to heuristic extraction if J-Prime unavailable.
    """
    # Build heuristic features first (always available)
    heuristic = _heuristic_features(email_dict)

    if not config.extraction_enabled:
        return heuristic

    # Try J-Prime structured extraction
    try:
        prompt = _build_extraction_prompt(email_dict)
        response = await router.generate(
            prompt=prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            max_tokens=512,
            temperature=0.0,  # deterministic
            deadline=deadline,
        )
        parsed = json.loads(response.content)
        return _merge_features(heuristic, parsed)
    except Exception:
        return heuristic  # graceful fallback
```

## Notification Policy (policy.py)

Implements user's 10-section notification spec:

1. **Quiet hours**: 23:00-08:00, suppress tier2+ notifications, tier1 still notifies
2. **Dedup windows**: 15min (tier1), 60min (tier2) — keyed by idempotency_key
3. **Interrupt budget**: 3/hr, 12/day — excess queued for summary
4. **Escalation**: Tier1 during quiet hours → still notifies (overrides quiet)
5. **Summary windows**: Every 30min, batch tier2 into single summary notification
6. **State + idempotency**: Dedup cache in-memory with TTL, cycle report persisted
7. **Observability**: Every decision emits structured event
8. **Safe defaults**: All notifications suppressed if config parse fails
9. **Failure handling**: No retry storm — single attempt, log failure, continue
10. **Tests**: Quiet hours boundary, dedup exact window, budget exhaustion, summary flush

## Labels (labels.py)

Gmail label CRUD via GoogleWorkspaceAgent's Gmail service:

```python
async def ensure_labels_exist(gmail_service, config: TriageConfig) -> Dict[str, str]:
    """Create jarvis/* labels if they don't exist. Returns {label_name: label_id}."""

async def apply_label(gmail_service, message_id: str, label_name: str, label_map: Dict[str, str]):
    """Apply label to message. Idempotent — no error if already applied."""
```

## Events (events.py)

7 structured event types:

1. `triage_cycle_started` — cycle_id, timestamp
2. `email_triaged` — message_id, score, tier, action, breakdown
3. `notification_sent` — message_id, tier, channel
4. `notification_suppressed` — message_id, tier, reason (quiet_hours|dedup|budget)
5. `summary_flushed` — count, tier_counts
6. `triage_cycle_completed` — cycle_id, duration_ms, counts
7. `triage_error` — cycle_id, error_type, message

All events logged as structured JSON via `logging.getLogger("jarvis.email_triage")`.

## Runner (runner.py)

```python
class EmailTriageRunner:
    _instance: ClassVar[Optional["EmailTriageRunner"]] = None

    @classmethod
    def get_instance(cls) -> "EmailTriageRunner":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def run_cycle(self) -> TriageCycleReport:
        """Single triage cycle. Called by agent_runtime housekeeping."""
```

## Integration: agent_runtime.py

Add to `housekeeping_loop()` after `_maybe_generate_proactive_goal()`:

```python
await self._maybe_run_email_triage(context)
```

New method with cooldown + feature flag gate:

```python
async def _maybe_run_email_triage(self, context=None):
    if not _env_bool("EMAIL_TRIAGE_ENABLED", False):
        return
    now = time.time()
    interval = float(os.getenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "60"))
    if now - self._last_email_triage_run < interval:
        return
    self._last_email_triage_run = now
    try:
        from autonomy.email_triage import EmailTriageRunner
        runner = EmailTriageRunner.get_instance()
        await asyncio.wait_for(runner.run_cycle(), timeout=30.0)
    except Exception as e:
        logger.warning("Email triage cycle failed: %s", e)
```

## Integration: google_workspace_agent.py

Add two new sync methods (following existing `_fetch_unread_sync` pattern):

```python
def _modify_labels_sync(self, message_id: str, add_labels: List[str], remove_labels: Optional[List[str]] = None) -> Dict:
    body = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels
    return self._gmail_service.users().messages().modify(
        userId="me", id=message_id, body=body
    ).execute()

def _ensure_label_exists_sync(self, label_name: str) -> str:
    """Create label if it doesn't exist. Returns label ID."""
    labels = self._gmail_service.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    created = self._gmail_service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]
```

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|-----------|
| J-Prime malformed JSON | Bad features | Heuristic fallback (no AI features) |
| Gmail API quota | Label ops fail | Batch ops, exponential backoff, circuit breaker |
| Scoring version drift | Re-score inconsistency | idempotency_key includes scoring_version |
| Timezone mismatch | Notifications at wrong time | datetime.now() local time |
| Runner crash mid-cycle | Partial labeling | Atomic per-email: label + event. Next cycle picks up remaining |
| Config change mid-cycle | Inconsistent | Config frozen at cycle start |
| PrimeRouter unavailable | No extraction | Heuristic-only scoring (headers/subject) |

## Acceptance Criteria

1. `EMAIL_TRIAGE_ENABLED=false` → zero behavior change
2. `EMAIL_TRIAGE_ENABLED=true` → labels applied every 60s
3. Deterministic: same email + config → same score (replay harness)
4. No destructive actions (no archive, delete, mark-read, send)
5. Quiet hours (23:00-08:00) suppress tier2+ notifications
6. Dedup: same email never re-notified within window
7. Budget: max 3/hr, 12/day — excess queued for summary
8. All 7 events emitted with structured payloads
9. Feature flags independently toggleable
10. 100% test coverage on new code

## Rollout Strategy

Phase 0: `EMAIL_TRIAGE_ENABLED=false` (default). Ship all code, zero runtime impact.
Phase 1: Enable with `EMAIL_TRIAGE_NOTIFY_TIER1=false`, `EMAIL_TRIAGE_NOTIFY_TIER2=false`. Labels only.
Phase 2: Enable tier1 notifications. Monitor interrupt budget.
Phase 3: Enable tier2 summaries. Enable tier4 quarantine view.
