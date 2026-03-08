# Autonomous Self-Development Layers — Vertical Slice Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable JARVIS to autonomously detect problems, propose fixes, apply changes across all 3 repos (JARVIS, prime, reactor-core), and communicate in real-time — starting with a thin vertical slice through all 4 layers.

**Architecture:** Vertical slice — thin end-to-end path (detect test failure → generate fix → apply via governed pipeline → narrate via voice/TUI/log) before widening each layer. Staged hybrid triggers: B (test failures) auto-submit first, A (stack traces) observe-only, C (git analysis) deferred.

**Tech Stack:** Python 3.9+, asyncio, existing GovernedLoopService, CommProtocol, Trinity event bus, CAI/UAE/SAI intelligence triad, safe_say voice, Textual TUI.

---

## 1. Delivery Strategy

**Vertical slice first**, then widen:

1. Build a thin path through all 4 layers that demonstrates: "JARVIS detects a test failure, proposes a fix via J-Prime, narrates what it's doing, and applies it after approval."
2. After the slice works end-to-end, widen each layer (more triggers, more repos, more communication channels, more autonomy).

**Staged hybrid triggers:**

| Phase | Trigger | Mode | When |
|-------|---------|------|------|
| Phase 1 | B: Test failures | Auto-submit to governed pipeline | Now |
| Phase 1.5 | A: Stack traces | Observe + narrate only (no submit) | Now |
| Phase 2 | A: Stack traces | Governed submit (after B proves reliability) | After Phase 1 acceptance |
| Phase 3 | C: Git commit analysis | Suggestion-only | After Phase 2 acceptance |

---

## 2. Layer 1: Intent Engine

### Module Layout

```
backend/core/ouroboros/governance/
  intent/
    __init__.py
    engine.py              # IntentEngine — long-running async service
    signals.py             # IntentSignal dataclass + dedup logic
    test_watcher.py        # Pytest result watcher (Phase 1 trigger B)
    error_interceptor.py   # Logger/exception interceptor (Phase 1.5 trigger A)
    rate_limiter.py        # Per-file cooldown + ops/hour/day caps
```

### IntentSignal Protocol

```python
@dataclass(frozen=True)
class IntentSignal:
    signal_id: str                    # UUIDv7 for ordering
    source: str                       # "intent:test_failure" | "intent:stack_trace" | "intent:git_analysis"
    target_files: Tuple[str, ...]     # Files implicated
    repo: str                         # "jarvis" | "prime" | "reactor-core"
    description: str                  # Human-readable: "test_utils.py::test_edge_case failed 2x"
    evidence: Dict[str, Any]          # Raw data: traceback, pytest output, diff
    confidence: float                 # 0.0-1.0, how certain this is a real issue
    timestamp: datetime
    stable: bool                      # True = met stability criteria (e.g., 2 consecutive failures)
```

### IntentEngine Flow

```
INACTIVE → WATCHING → ACTIVE
                        ↓
              signal detected
                        ↓
              dedupe check → (duplicate → skip)
                        ↓
              stability check → (unstable → buffer, wait for next run)
                        ↓
              rate limit check → (over cap → queue)
                        ↓
              autonomy gate (CAI+UAE+SAI) → (defer → narrate reason, skip)
                        ↓
              mode check:
                auto-submit → build OperationContext → GovernedLoopService.submit()
                observe-only → narrate via voice + log → done
```

### Test Watcher (Trigger B — Auto-Submit)

- Polls by running `pytest --tb=short -q` on configurable interval (default 5 min, `JARVIS_INTENT_TEST_INTERVAL_S`)
- Parses pytest output for failures (exit code + stdout parsing)
- Tracks failure history per test: `{test_id: [fail_ts_1, fail_ts_2, ...]}`
- **Stable failure** = same test fails in 2 consecutive runs
- On stable failure: emits `IntentSignal(source="intent:test_failure", stable=True)`
- Ignores known flaky tests (configurable allowlist, or flake-confidence heuristic based on pass/fail ratio)

### Error Interceptor (Trigger A — Observe Only in Phase 1.5)

- Installs custom `logging.Handler` capturing `ERROR` and `CRITICAL` log records
- Captures unhandled exceptions via `sys.excepthook` wrapper
- Extracts traceback, affected file, line number
- Emits `IntentSignal(source="intent:stack_trace", stable=False)` — observe-only, no submit
- Voice narration: "Derek, I'm seeing repeated errors in prime_client.py — connection timeout on line 342. Want me to investigate?"

### Rate Limiter

```python
@dataclass
class RateLimiterConfig:
    max_ops_per_hour: int = 5
    max_ops_per_day: int = 20
    per_file_cooldown_s: float = 600.0   # 10 min between ops on same file
    per_signal_cooldown_s: float = 300.0  # 5 min between same signal
```

- All limits configurable via env vars
- Deterministic rejection with reason code: `rate_limit:hourly_cap`, `rate_limit:file_cooldown`
- Rate limit state resets at midnight UTC

### Cross-Signal Deduplication

- Same file + same error signature within cooldown = one op
- Test failure AND stack trace pointing to same file = one op (test failure wins as trigger source)
- Dedup key: `hash(repo + file_path + error_signature)`

### Hard Guardrails

- Signal dedupe + per-file cooldown + ops/hour cap
- Stable-failure requirement (2 consecutive failures) before submit
- Trigger source in every op (`intent:test_failure`, `intent:stack_trace`, etc.)
- No trigger bypasses governance gate or approval policy
- Explicit `CANCELLED` and `TIMEOUT_EXPIRED` terminal outcomes
- Replay-safe idempotency across triggers (same issue from two sources = one op)
- Side-effect firewall enforced in shadow path with audit field `side_effects_blocked=true`
- Provider routing + reason logged per op for cost and incident forensics

---

## 3. Layer 2: Multi-Repo Coordinator

### Module Layout

```
backend/core/ouroboros/governance/
  multi_repo/
    __init__.py
    registry.py            # RepoRegistry — knows all 3 repos
    context_builder.py     # Cross-repo file reading for prompt context
    blast_radius.py        # Cross-repo impact analysis
    repo_pipeline.py       # Per-repo GovernedLoopService orchestration
```

### RepoRegistry

```python
@dataclass(frozen=True)
class RepoConfig:
    name: str                       # "jarvis" | "prime" | "reactor-core"
    local_path: Path                # Absolute path to repo root
    canary_slices: Tuple[str, ...]  # Initial canary slices for this repo
    default_branch: str = "main"
    enabled: bool = True

class RepoRegistry:
    """Knows about all repos JARVIS operates across.

    Provides unified file search/read across repos.
    Each repo gets its own GovernedLoopService instance.
    """
    def __init__(self, configs: Tuple[RepoConfig, ...]) -> None: ...
    def get(self, name: str) -> RepoConfig: ...
    def list_enabled(self) -> Tuple[RepoConfig, ...]: ...

    # Unified codebase view
    async def read_file(self, repo: str, path: str) -> str: ...
    async def search_files(self, pattern: str, repo: Optional[str] = None) -> List[FileMatch]: ...
    async def grep(self, pattern: str, repo: Optional[str] = None) -> List[GrepMatch]: ...
```

**Configuration via env vars:**
```
JARVIS_REPO_PATH=/Users/djrussell23/Documents/repos/JARVIS-AI-Agent
JARVIS_PRIME_REPO_PATH=/Users/djrussell23/Documents/repos/JARVIS-Prime
JARVIS_REACTOR_REPO_PATH=/Users/djrussell23/Documents/repos/reactor-core
```

### Cross-Repo Context Builder

When J-Prime generates a fix, the prompt needs context from the right repos:

1. Takes the `IntentSignal` (which file, which repo, what broke)
2. Finds related files across all repos:
   - Import graph: "this file imports from prime's `api_client.py`"
   - Contract files: "this repo's `contract_gate.py` references reactor-core schemas"
   - Test companions: "this source file has tests in `tests/test_*.py`"
3. Reads relevant files and includes them in the generation prompt
4. Respects a token budget (don't stuff 50 files — pick the most relevant)

```python
@dataclass(frozen=True)
class CrossRepoContext:
    primary_file: str
    primary_repo: str
    related_files: Tuple[ContextFile, ...]
    total_tokens_estimate: int

@dataclass(frozen=True)
class ContextFile:
    repo: str
    path: str
    content: str
    relevance: str    # "import_dependency" | "contract" | "test" | "caller" | "callee"
```

### Blast Radius Analysis

Before applying a fix, analyze cross-repo impact:

```python
@dataclass(frozen=True)
class BlastRadiusReport:
    target_repo: str
    target_files: Tuple[str, ...]
    affected_repos: Tuple[str, ...]
    affected_files: Tuple[AffectedFile, ...]
    crosses_repo_boundary: bool
    risk_escalation: Optional[str]     # "approval_required" if cross-repo
    contract_impact: Optional[str]     # "api_changed" | "schema_changed" | None

@dataclass(frozen=True)
class AffectedFile:
    repo: str
    path: str
    dependency_type: str   # "imports" | "calls_api" | "implements_contract" | "tests"
```

**Key behavior:**
- `crosses_repo_boundary=True` → risk tier escalates to `APPROVAL_REQUIRED`
- Blast radius report attached to `OperationContext` for approval prompt
- Uses existing `cross_repo.py` `EventType.DEPENDENCY_ANALYSIS_REQUEST`

### Per-Repo Pipeline Orchestration

```python
class RepoPipelineManager:
    """Manages GovernedLoopService instances per repo."""

    def __init__(self, registry: RepoRegistry, governance_stack: Any) -> None:
        self._pipelines: Dict[str, GovernedLoopService] = {}

    async def start_all(self) -> None: ...

    async def submit(self, signal: IntentSignal) -> OperationResult:
        """Route signal to the correct repo's pipeline."""
        pipeline = self._pipelines[signal.repo]
        context = await self._context_builder.build(signal)
        blast = await self._blast_radius.analyze(signal, context)
        return await pipeline.submit(ctx, trigger_source=signal.source)

    async def stop_all(self) -> None: ...
```

### Trinity Event Bus Integration

When an operation completes in any repo:
- Publish `CrossRepoEvent` via existing `EventBridge`
- Other repos' IntentEngines can react (e.g., prime API changed → JARVIS tests might break → auto-run tests)
- Event types already defined: `IMPROVEMENT_COMPLETE`, `API_CONTRACT_CHANGED`, `REFACTORING_COMPLETED`

---

## 4. Layer 3: Real-Time Communication

### Architecture

One emitter, three consumers. All pipeline events flow through existing `CommProtocol`:

```
GovernedLoopService ──→ CommProtocol ──→ VoiceNarrator (safe_say)
IntentEngine        ──→             ──→ TUISelfProgramPanel
BlastRadiusAnalyzer ──→             ──→ OpsLogger
```

### Module Layout

```
backend/core/ouroboros/governance/
  comms/
    __init__.py
    voice_narrator.py      # Voice narration at pipeline milestones
    tui_panel.py           # TUI "Self-Programming" panel data provider
    ops_logger.py          # Human-readable ops log writer
    narrator_script.py     # Message templates for voice narration
```

### Voice Narrator

Subscribes to `CommProtocol` as a transport. Narrates key moments via `safe_say()`:

```python
class VoiceNarrator:
    """CommProtocol transport that narrates pipeline events via speech."""

    NARRATE_PHASES = {
        "signal_detected",   # "I noticed test_utils.py failing..."
        "generating",        # "I'm working on a fix..."
        "approve",           # "I'd like to apply a change. Awaiting approval."
        "applied",           # "Fix applied successfully."
        "postmortem",        # "The fix didn't work. Rolling back."
        "observe",           # Stack trace observe-only
    }
```

**Narration rules:**
- Max 1 narration per 60s globally (hard debounce)
- Respects quiet hours / speech gate
- Suppressed if CLI already responded to approval
- Idempotent via `notification_id = hash(op_id + phase)`
- Priority: `APPROVE` = HIGH, everything else = MEDIUM
- CAI adapts tone/verbosity to cognitive load (`HIGH` = brief, `MINIMAL` = detailed)
- UAE historical context enriches narration ("I've seen this pattern 3 times before...")

**Message templates** (in `narrator_script.py`):

```python
SCRIPTS = {
    "signal_detected": (
        "Derek, I noticed {test_count} test failure{s} in {file}. "
        "Analyzing the issue now."
    ),
    "generating": "I'm generating a fix for {file} via {provider}.",
    "approve": (
        "I'd like to modify {file} to {goal}. "
        "This is approval-required. "
        "Use the CLI to approve or reject op {op_id}."
    ),
    "applied": "Fix applied and verified. {file} — all tests passing now.",
    "postmortem": (
        "The fix for {file} didn't work. I've rolled back the changes. "
        "Reason: {reason}."
    ),
    "observe_error": (
        "I'm seeing repeated errors in {file} — {error_summary}. "
        "Want me to investigate?"
    ),
    "cross_repo_impact": (
        "Heads up — this change to {file} in {repo} affects "
        "{affected_count} file{s} in {other_repos}."
    ),
}
```

### TUI Self-Programming Panel

Data provider for a new section in the Textual TUI dashboard:

```python
@dataclass(frozen=True)
class PipelineStatus:
    op_id: str
    phase: str
    target_file: str
    repo: str
    trigger_source: str
    provider: Optional[str]
    started_at: datetime
    elapsed_s: float
    awaiting_approval: bool

@dataclass(frozen=True)
class SelfProgramPanelState:
    active_ops: Tuple[PipelineStatus, ...]
    pending_approvals: Tuple[PipelineStatus, ...]
    recent_completions: Tuple[CompletionSummary, ...]  # Last 10
    intent_engine_state: str     # "watching" | "active" | "rate_limited"
    ops_today: int
    ops_limit: int
    repos_online: Tuple[str, ...]
```

**TUI renders:**
```
╭─ Self-Programming ──────────────────────────────────────────╮
│ Engine: WATCHING  │  Ops: 3/20 today  │  Repos: 3/3 online │
│                                                              │
│ ● GENERATING  test_utils.py (jarvis)  via gcp-jprime  12s   │
│ ⏳ APPROVE     prime_client.py (prime) op-047  [approve cmd] │
│                                                              │
│ Recent:                                                      │
│ ✓ test_edge_case.py   COMPLETE   2m ago   gcp-jprime        │
│ ✗ api_handler.py      POSTMORTEM 8m ago   rolled back       │
│ ✓ test_validator.py   COMPLETE   1h ago   claude-api        │
╰──────────────────────────────────────────────────────────────╯
```

### Ops Logger

Appends human-readable entries to `~/.jarvis/ops/YYYY-MM-DD-ops.log`:

```python
class OpsLogger:
    """Writes human-readable pipeline narrative to daily log files."""
    LOG_DIR = Path.home() / ".jarvis" / "ops"
    RETENTION_DAYS = 30
```

**Log format:**
```
[2026-03-07 14:23:01] SIGNAL  intent:test_failure  tests/test_utils.py (jarvis)
    2 consecutive failures in test_edge_case_handling
    Evidence: AssertionError: expected 3, got 2

[2026-03-07 14:23:04] SUBMIT  op-048  tests/test_utils.py (jarvis)  trigger=intent:test_failure
    Generating fix via gcp-jprime...

[2026-03-07 14:23:19] COMPLETE  op-048  tests/test_utils.py (jarvis)  duration=15.2s
    Provider: gcp-jprime  Cost: $0.0042
    Fix: Added missing edge case for empty input

[2026-03-07 14:45:12] OBSERVE  intent:stack_trace  prime_client.py (prime)
    ConnectionTimeout at line 342 — repeated 3x in 10 minutes
    Narrated via voice. No auto-submit (observe-only mode).
```

**Features:**
- Daily rotation, auto-cleanup after `RETENTION_DAYS`
- Append-only, structured enough to grep
- Configurable via `JARVIS_OPS_LOG_DIR` and `JARVIS_OPS_LOG_RETENTION_DAYS`

---

## 5. Layer 4: Selective Autonomy

### Intelligence Triad: CAI + UAE + SAI

Three existing intelligence systems feed into the autonomy gate:

| System | Module | What It Knows | Contribution |
|--------|--------|--------------|-------------|
| **CAI** | `backend/context_intelligence/` | User intent, emotional state, cognitive load, work context | "Should JARVIS act now?" — defer during meetings, adapt tone |
| **UAE** | `backend/intelligence/unified_awareness_engine.py` | Historical patterns, learning, confidence fusion | "Has this worked before?" — pattern confidence, graduation |
| **SAI** | `backend/vision/situational_awareness/core_engine.py` | Real-time environment, UI state, system resources | "What's happening right now?" — resource pressure, workspace |

### Autonomy Tiers

```python
class AutonomyTier(Enum):
    OBSERVE = "observe"       # Detect + narrate only. No submit.
    SUGGEST = "suggest"       # Detect + narrate + queue for manual trigger.
    GOVERNED = "governed"     # Auto-submit through full pipeline (approval per risk tier).
    AUTONOMOUS = "autonomous" # Auto-submit + auto-approve SAFE_AUTO ops.
```

### Trust Graduation Model

Each `(trigger_source, repo, canary_slice)` triple has an autonomy tier that graduates:

```
OBSERVE ──→ SUGGEST ──→ GOVERNED ──→ AUTONOMOUS
```

**Graduation criteria (all must pass):**

| Transition | Criteria |
|-----------|----------|
| OBSERVE → SUGGEST | 20 observations with 0 false positives. Human confirmed ≥5 suggestions valid. |
| SUGGEST → GOVERNED | 30 successful governed ops. Rollback rate < 5%. p95 latency < 120s. 72h stability window. |
| GOVERNED → AUTONOMOUS | 50 successful ops with 0 rollbacks. All ops SAFE_AUTO tier. Human audit of last 10 ops. CAI safety agrees with governance ≥95%. |

**Demotion triggers (instant, any one):**
- Rollback on an auto-applied change → demote to GOVERNED
- Two consecutive POSTMORTEM outcomes → demote to SUGGEST
- CAI/SAI detects anomaly → demote to OBSERVE
- Manual break-glass → demote to OBSERVE across all triples

### Per-Signal Autonomy Configuration

```python
@dataclass(frozen=True)
class SignalAutonomyConfig:
    trigger_source: str
    repo: str
    canary_slice: str
    current_tier: AutonomyTier
    graduation_metrics: GraduationMetrics

    # CAI overrides
    defer_during_cognitive_load: CognitiveLoad = CognitiveLoad.HIGH
    defer_during_work_context: Tuple[WorkContext, ...] = (WorkContext.MEETINGS,)
    require_user_active: bool = False
```

**Phase 1 defaults:**

| Signal | Repo | Slice | Initial Tier |
|--------|------|-------|-------------|
| `intent:test_failure` | jarvis | `tests/` | GOVERNED |
| `intent:test_failure` | prime | `tests/` | SUGGEST |
| `intent:test_failure` | reactor-core | `tests/` | OBSERVE |
| `intent:stack_trace` | * | * | OBSERVE |

### Autonomy Gate (CAI + UAE + SAI)

```python
async def should_proceed(
    signal: IntentSignal,
    autonomy_config: SignalAutonomyConfig,
    cai_context: CAIContext,
    uae_decision: UnifiedDecision,
    sai_snapshot: EnvironmentalSnapshot,
) -> Tuple[bool, str]:
    """Decide whether to auto-proceed or defer.

    Returns (proceed, reason_code).
    """
    # 1. Autonomy tier check
    if autonomy_config.current_tier is AutonomyTier.OBSERVE:
        return False, "tier:observe_only"

    # 2. CAI: Cognitive load + work context
    if cai_context.cognitive_load >= autonomy_config.defer_during_cognitive_load:
        return False, "cai:cognitive_load_high"
    if cai_context.work_context in autonomy_config.defer_during_work_context:
        return False, "cai:in_meeting"

    # 3. SAI: Resource pressure + environment
    if sai_snapshot.system_resources.ram_percent > 90:
        return False, "sai:memory_pressure"
    if sai_snapshot.system_state is SystemState.LOCKED:
        return False, "sai:screen_locked"

    # 4. UAE: Historical pattern confidence
    if uae_decision.confidence < 0.6:
        return False, "uae:low_pattern_confidence"

    # 5. Cross-system agreement check
    if sai_snapshot.anomaly_detected and cai_context.safety_level == "SAFE":
        return False, "disagreement:cai_safe_sai_anomaly"

    return True, "proceed"
```

### Persistence

Autonomy tier state persists to `~/.jarvis/autonomy/state.json`:
- Survives process restarts
- Loaded at startup by GovernedLoopService
- Written after each op completion
- Break-glass reset clears the file

---

## 6. Phase 1 Acceptance Checklist

Before promoting trigger A (stack traces) from observe-only to governed submit:

### Functional Requirements

- [ ] Test watcher detects stable failures (2 consecutive runs)
- [ ] IntentSignal created with correct `source`, `target_files`, `repo`, `evidence`
- [ ] Signal dedup works (same failure in cooldown = one op)
- [ ] Rate limiter enforces per-file cooldown, hourly cap, daily cap
- [ ] GovernedLoopService.submit() receives correct OperationContext
- [ ] Pipeline runs: CLASSIFY → ROUTE → GENERATE → VALIDATE → GATE → [APPROVE] → APPLY → VERIFY → COMPLETE
- [ ] Fix generated via J-Prime (or Claude fallback)
- [ ] Fix validated (AST parse, tests pass after apply)
- [ ] Rollback works on failed validation
- [ ] Voice narrates at signal_detected, generating, approve, applied, postmortem
- [ ] TUI panel shows active ops, pending approvals, recent completions
- [ ] Ops log written with correct format
- [ ] Cross-repo context included in generation prompt
- [ ] Blast radius analyzed before apply

### Safety Requirements

- [ ] No trigger bypasses governance gate or approval policy
- [ ] `APPROVAL_REQUIRED` ops always wait for human CLI approval
- [ ] Cross-repo changes always escalate to `APPROVAL_REQUIRED`
- [ ] Rate limiter prevents retry storms
- [ ] Side-effect firewall enforced in shadow path
- [ ] Autonomy tier demotion triggers work (rollback → demote)
- [ ] Break-glass resets all autonomy tiers to OBSERVE
- [ ] CAI cognitive load HIGH defers ops
- [ ] SAI memory pressure > 90% defers ops

### Metrics Requirements (Soak Test: 2 weeks, 20+ ops)

- [ ] ≥ 20 successful auto-submit ops on `tests/` canary slice
- [ ] Rollback rate < 5%
- [ ] Zero false positive triggers (signal fired on non-issue)
- [ ] p95 pipeline latency < 120s
- [ ] Zero governance bypass incidents
- [ ] Cost per op tracked and within budget
- [ ] Provider routing reason logged for every op

---

## 7. Failure Matrix

| Failure | Detection | Response | Terminal State |
|---------|-----------|----------|----------------|
| Test watcher crashes | Task exception handler | Log, restart with backoff | Watcher restarts |
| pytest hangs | Timeout on subprocess | Kill subprocess, skip cycle | Next poll cycle |
| All providers unavailable | FailbackStateMachine → QUEUE_ONLY | Queue signal, retry on recovery | Queued |
| Rate limit exceeded | Rate limiter check | Reject with reason code, narrate | Signal dropped |
| CAI unavailable | Import/call failure | Proceed without CAI gate (fail-open) | Pipeline continues |
| UAE unavailable | Import/call failure | Proceed without UAE confidence | Pipeline continues |
| SAI unavailable | Import/call failure | Proceed without SAI check | Pipeline continues |
| Cross-repo blast radius analysis fails | Exception in analyzer | Escalate to APPROVAL_REQUIRED | Approval gated |
| Voice narration fails | safe_say exception | Log, continue pipeline | Pipeline continues |
| TUI panel update fails | Exception in transport | Log, continue pipeline | Pipeline continues |
| Ops log write fails | IOError | Log to stderr, continue | Pipeline continues |
| Autonomy state file corrupt | JSON parse error | Reset to OBSERVE defaults | Fresh start |

---

## 8. Post-Vertical-Slice Roadmap

### Widening Layer 1 (Intent Engine)
- Phase 2: Promote stack trace trigger to governed submit
- Phase 3: Add git commit analysis trigger (suggestion-only)
- Flaky test detection heuristic (pass/fail ratio)
- Coverage gap analysis trigger

### Widening Layer 2 (Multi-Repo)
- Atomic cross-repo transactions (saga pattern)
- Cross-repo test runner (run prime tests after JARVIS change)
- Shared contract versioning automation

### Widening Layer 3 (Communication)
- TUI approve/reject buttons (not just CLI)
- Voice-based approval with speaker verification
- Slack/webhook notifications
- Real-time conversational interface (JARVIS asks questions mid-pipeline)

### Widening Layer 4 (Selective Autonomy)
- Per-slice trust levels beyond tests/
- Time-of-day autonomy rules (more autonomous during work hours)
- Anomaly-based automatic demotion ML model
- Multi-stakeholder approval for cross-repo AUTONOMOUS ops
