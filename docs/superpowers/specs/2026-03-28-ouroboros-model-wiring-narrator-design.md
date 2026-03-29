# Ouroboros Model Wiring + DaemonNarrator: Activating the Cognitive Layer

**Date:** 2026-03-28
**Status:** Design approved, pending implementation
**Depends on:** Ouroboros Daemon (Zone 7.0) + Cognitive Extensions + Architecture Reasoning Agent
**Scope:** Two components — 397B model wiring (cognition) + DaemonNarrator (voice transparency)

---

## Preamble

The Ouroboros infrastructure is complete: REM Sleep daemon, Roadmap Sensor, Feature Synthesis Engine, Architecture Reasoning Agent — all built, tested, merged. But the cognitive layer is dormant: the Synthesis Engine runs Tier 0 deterministic hints only, and the Architect returns None. The organism can see but cannot think. The DaemonNarrator doesn't exist — the organism is silent about its autonomous activity.

This spec activates both: 397B model calls give the organism intelligence, and the DaemonNarrator gives it a voice.

### Governing Philosophy

**The Symbiotic AI-Native Manifesto v2 — Boundary Mandate:**

- `prompt_only()` is **deterministic infrastructure** — HTTP, auth, polling, retries, cost tracking. Same boundary as any other provider method.
- Prompt construction and JSON parsing are the **agentic boundary** — this is where model intelligence creates leverage.
- DaemonNarrator's event subscription, rate limiting, and message templates are **deterministic skeleton**.
- No model inference in the narrator — all speech templates are explicit, all filtering is rule-based.

---

## Component 1: 397B Model Wiring

### prompt_only() on DoublewordProvider

**File:** `backend/core/ouroboros/governance/doubleword_provider.py`

New method on the existing DoublewordProvider class:

```python
async def prompt_only(
    self,
    prompt: str,
    model: Optional[str] = None,
    caller_id: str = "ouroboros_cognition",
    response_format: Optional[Dict] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Execute inference bypassing governance OperationContext.

    Reuses: auth, rate limiting, retry/backoff, polling, cost tracking.
    Bypasses: OperationContext, governance ledger, TelemetryBus.

    Uses batch API internally (submit JSONL -> poll -> retrieve).
    caller_id tagged on cost metrics for attribution.
    response_format passed to API for structured output enforcement.
    Returns raw response string (caller parses).
    Raises: DoublewordTimeoutError on poll timeout.
    Raises: DoublewordAPIError on HTTP/parse failure.
    """
```

**Implementation details:**
- Internally constructs a minimal JSONL batch request (same HTTP flow as `submit_batch`)
- Polls with existing backoff logic (reuse `_poll_interval_s` and `_max_wait_s`)
- Returns the response content string — caller is responsible for JSON parsing
- Cost tracked under `caller_id` bucket in `self._stats`, not governance metrics
- `response_format` passed as `response_format` field in the API request body for structured output enforcement
- `model` defaults to `self._model` (Qwen3.5-397B) if not specified
- No OperationContext created, no ledger entries, no TelemetryBus events

**Error handling:**
- HTTP errors → `DoublewordAPIError` (new exception class, or reuse existing)
- Poll timeout → `DoublewordTimeoutError`
- Empty response → `DoublewordAPIError("Empty response")`
- Caller catches and falls back to Claude API

### Synthesis Engine Wiring

**File:** `backend/core/ouroboros/roadmap/synthesis_engine.py`

Replace the v2 placeholder in `_run_synthesis()` with actual Doubleword call:

```python
async def _run_doubleword(
    self,
    snapshot: RoadmapSnapshot,
    tier0_hints: List[FeatureHypothesis],
) -> List[FeatureHypothesis]:
    """Call Doubleword 397B for deep gap analysis.

    Context shedding: only P0 fragments, truncated to budget.
    Structured output: JSON array of hypothesis objects.
    Fallback: Claude API on timeout/parse failure.
    """
```

**Prompt structure:**

```
You are analyzing a software roadmap for capability gaps.

ROADMAP EVIDENCE (specs, plans, backlog):
{P0 fragment summaries, truncated to budget}

EXISTING GAPS ALREADY DETECTED (deterministic):
{Tier 0 hints as context — prevents model from rediscovering known gaps}

CODEBASE STRUCTURE:
{Oracle graph summary — top 20 modules by node count}

Identify capability gaps between stated intent and current implementation.
Return a JSON array where each object has:
- description: string (what is missing)
- evidence_fragments: array of source_ids from the roadmap evidence
- gap_type: one of "missing_capability", "incomplete_wiring", "stale_implementation", "manifesto_violation"
- confidence: float 0-1
- urgency: one of "critical", "high", "normal", "low"
- suggested_scope: string (directory or file path where the fix belongs)
- suggested_repos: array of strings ("jarvis", "jarvis-prime", "reactor")
```

**Context shedding rules (deterministic, in order):**
1. Include all P0 fragment summaries (first 500 chars each)
2. If over 6000 tokens: drop P0 memory fragments, keep specs + plans + backlog
3. If still over: truncate spec summaries to 200 chars each
4. If still over: raise `ContextBudgetExceededError` — do NOT silently send over-budget prompt

**JSON parsing:**
- Parse response as JSON array
- For each object: validate required fields, construct FeatureHypothesis via `.new()` factory
- Invalid entries: skip with warning (log, don't crash)
- provenance: `"model:doubleword-397b"` (or `"model:claude-api"` for fallback)
- `confidence_rule_id`: `"model_inference"`

**Fallback chain:** Doubleword 397B → Claude API. Claude uses same prompt, synchronous `anthropic.messages.create()`. Same parsing logic.

### Architecture Reasoning Agent Wiring

**File:** `backend/core/ouroboros/architect/reasoning_agent.py`

Replace the v1 `return None` in `design()` with actual Doubleword call:

```python
async def _generate_plan(
    self,
    hypothesis: FeatureHypothesis,
    snapshot: RoadmapSnapshot,
    oracle: Any,
) -> Optional[ArchitecturalPlan]:
    """Call Doubleword 397B to design an architectural plan.

    Structured output: ArchitecturalPlan JSON with steps, contracts, acceptance.
    Context: hypothesis + Oracle file neighborhood + snapshot P0.
    Fallback: Claude API on timeout/parse failure.
    """
```

**Prompt structure:**

```
You are designing a multi-file feature for the JARVIS Trinity ecosystem.

CAPABILITY GAP:
Description: {hypothesis.description}
Evidence: {hypothesis.evidence_fragments}
Gap type: {hypothesis.gap_type}
Suggested scope: {hypothesis.suggested_scope}

CODEBASE CONTEXT:
{Oracle.get_file_neighborhood(suggested_scope) — imports, callers, tests}
{Existing interfaces and class signatures in scope}

CONSTRAINTS:
- Maximum {config.max_steps} implementation steps
- Each step targets one file (create, modify, or delete)
- Include test files for each new module
- Include acceptance checks (shell commands that verify correctness)
- Explicitly list non-goals (what is out of scope)
- All paths must be repo-relative (no ".." escape)
- Steps must form an acyclic dependency graph

Return a JSON object with:
- title: string
- description: string (design rationale)
- repos_affected: array of strings
- non_goals: array of strings
- steps: array of step objects, each with:
  - step_index: int (0-based)
  - description: string
  - intent_kind: "create_file" | "modify_file" | "delete_file"
  - target_paths: array of strings
  - ancillary_paths: array of strings (registry, __init__, config)
  - tests_required: array of strings
  - interface_contracts: array of strings (signatures)
  - repo: string
  - depends_on: array of int (step indices)
- acceptance_checks: array of check objects, each with:
  - check_id: string
  - check_kind: "exit_code" | "regex_stdout" | "import_check"
  - command: string
  - expected: string
```

**Context shedding rules (deterministic):**
1. Oracle file neighborhood for `hypothesis.suggested_scope` (depth 1)
2. P0 spec fragments related to the hypothesis (by evidence_fragments)
3. If over 8000 tokens: drop Oracle callers/inheritors, keep imports + tests only
4. If still over: raise `ContextBudgetExceededError`

**JSON parsing:**
- Parse response as JSON object
- Validate required fields
- Construct PlanStep objects with enum conversion (StepIntentKind)
- Construct AcceptanceCheck objects with enum conversion (CheckKind)
- Call `ArchitecturalPlan.create()` which computes plan_hash and file_allowlist
- Run `PlanValidator.validate()` on the result
- If validation fails: log warnings, return None (don't submit invalid plans)

---

## Component 2: DaemonNarrator

### Purpose

Give the Ouroboros organism a voice for its autonomous activity. Listens to SpinalCord events and speaks significant state changes via `safe_say()`. Deterministic filtering — no model inference for speech generation.

### File: `backend/core/ouroboros/daemon_narrator.py`

```python
class DaemonNarrator:
    """Voices salient Ouroboros events via the SpinalCord event bus.

    Deterministic: all speech templates are explicit strings.
    Rate-limited: max 1 announcement per category per rate_limit_s.
    Subscribes to: SpinalCord governance channel (confirmed state changes).
    """

    def __init__(
        self,
        spinal_cord: SpinalCord,
        say_fn: Callable = safe_say,
        rate_limit_s: float = 60.0,
        enabled: bool = True,
    ) -> None:
        ...

    async def start(self) -> None:
        """Subscribe to SpinalCord event streams."""

    async def stop(self) -> None:
        """Unsubscribe and drain pending speech."""
```

### Event → Speech Mapping

All events flow through the SpinalCord governance channel. The DaemonNarrator subscribes to the downward and progress streams.

| Event Type | Stream | Speech Template | Category (rate key) |
|-----------|--------|----------------|-------------------|
| `rem.epoch_start` | progress | "Entering REM Sleep. Scanning the organism." | `rem` |
| `rem.epoch_complete` | progress | "REM complete. Found {n} issues. {m} patches applied." | `rem` |
| `synthesis.complete` | progress | "Roadmap analysis complete. {n} capability gaps identified." | `synthesis` |
| `saga.started` | progress | "Designing {title}. {n} implementation steps." | `saga` |
| `saga.complete` | decision | "Feature implemented: {title}. PR ready for review." | `saga` |
| `saga.aborted` | decision | "Saga aborted at step {n}: {reason}. Earlier PRs blocked." | `saga` |
| `governance.patch_applied` | decision | "Patch applied: {description}" | `patch` |
| `vital.warn` | progress | "Boot scan: {n} warnings. REM will address them." | `vital` |

### Rate Limiting (Not Debounce)

Per-category rate limiting, not debounce:

```python
async def _speak(self, category: str, message: str) -> None:
    """Rate-limited speech. Max 1 per category per rate_limit_s."""
    now = time.time()
    last = self._last_spoken_at.get(category, 0.0)
    if (now - last) < self._rate_limit_s:
        return  # rate limited — drop silently
    self._last_spoken_at[category] = now
    await self._say_fn(
        message,
        source="ouroboros_narrator",
        skip_dedup=True,  # don't let safe_say dedup valid repeated events
    )
```

A burst of 5 `saga.complete` events within 60s → only the first speaks. The rest are dropped (not queued). This is correct: the TUI dashboard shows all events in real-time; voice is for the highlight reel.

### Event Aggregation

For `rem.epoch_complete`, the narrator aggregates findings rather than speaking per-finding:
- "REM complete. Found 5 issues: 3 dead functions, 2 unwired agents. 3 patches applied, 2 pending review."
- Single sentence, summary-level. Never per-finding.

### Subscription Model

DaemonNarrator subscribes to SpinalCord events. All lifecycle events (rem.epoch_start, synthesis.complete, saga events) flow through the SpinalCord governance channel — the same bus the TUI dashboard reads. This ensures narrator and TUI are always in sync.

Events that must be emitted by the respective components through SpinalCord:
- `RemSleepDaemon` emits `rem.epoch_start` and `rem.epoch_complete` via `spinal_cord.stream_up()`
- `FeatureSynthesisEngine` emits `synthesis.complete` via spinal cord callback
- `SagaOrchestrator` emits `saga.started`, `saga.complete`, `saga.aborted` via spinal cord

### Wiring

DaemonNarrator is created by OuroborosDaemon during Phase 3 (after SpinalCord is wired):

```python
# In daemon.py awaken(), after spinal cord and before REM:
self._narrator = DaemonNarrator(
    spinal_cord=self._spinal,
    rate_limit_s=self._config.narrator_rate_limit_s,
    enabled=self._config.narrator_enabled,
)
await self._narrator.start()
```

---

## File Structure

### New Files

```
backend/core/ouroboros/daemon_narrator.py           # DaemonNarrator (voice transparency)
backend/core/ouroboros/roadmap/synthesis_prompt.py   # Prompt builder + context shedding for synthesis
backend/core/ouroboros/architect/design_prompt.py    # Prompt builder + context shedding for architect

tests/core/ouroboros/test_daemon_narrator.py
tests/core/ouroboros/roadmap/test_synthesis_prompt.py
tests/core/ouroboros/architect/test_design_prompt.py
tests/core/ouroboros/test_prompt_only.py             # Tests for DoublewordProvider.prompt_only()
tests/core/ouroboros/test_model_wiring_integration.py # E2E: prompt -> parse -> hypothesis/plan
```

### Modified Files

| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/doubleword_provider.py` | Add `prompt_only()` method |
| `backend/core/ouroboros/roadmap/synthesis_engine.py` | Wire `_run_doubleword()` into `_run_synthesis()` |
| `backend/core/ouroboros/architect/reasoning_agent.py` | Wire `_generate_plan()` into `design()` |
| `backend/core/ouroboros/daemon.py` | Create + wire DaemonNarrator |
| `backend/core/ouroboros/daemon_config.py` | Add narrator env vars |
| `backend/core/ouroboros/rem_sleep.py` | Emit rem.epoch_start/complete events to SpinalCord |
| `backend/core/ouroboros/roadmap/synthesis_engine.py` | Emit synthesis.complete to SpinalCord (callback) |
| `backend/core/ouroboros/architect/saga_orchestrator.py` | Emit saga.started/complete/aborted to SpinalCord |

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_NARRATOR_ENABLED` | `true` | Master toggle for DaemonNarrator |
| `OUROBOROS_NARRATOR_RATE_LIMIT_S` | `60` | Max 1 speech per category per N seconds |
| `OUROBOROS_SYNTHESIS_MAX_TOKENS` | `6000` | Context budget for synthesis prompt |
| `OUROBOROS_ARCHITECT_MAX_TOKENS` | `8000` | Context budget for architect prompt |
| `OUROBOROS_PROMPT_ONLY_TIMEOUT_S` | `300` | Timeout for prompt_only batch poll |

---

## Testing Strategy

- **prompt_only():** Unit tests with mock HTTP (verify JSONL construction, poll loop, cost tracking under caller_id, structured output format pass-through)
- **Synthesis prompt:** Unit tests for context shedding rules (each rule in order, verify truncation, verify ContextBudgetExceededError on overflow)
- **Architect prompt:** Unit tests for prompt construction with mock Oracle neighborhoods
- **JSON parsing:** Unit tests with valid/invalid/partial model responses (verify graceful handling of malformed JSON, missing fields, extra fields)
- **DaemonNarrator:** Unit tests with mock SpinalCord + mock safe_say (verify rate limiting per category, verify event → speech mapping, verify aggregation for epoch_complete)
- **Integration:** E2E test with mock Doubleword (returns structured JSON) → verify FeatureHypothesis/ArchitecturalPlan correctly constructed and validated
- **SpinalCord event emission:** Verify REM, Synthesis, Saga all emit correct events that DaemonNarrator receives

---

## Day 1 Capabilities After Implementation

| What changes | Before | After |
|-------------|--------|-------|
| Feature Synthesis | Tier 0 deterministic hints only | 397B reasons about roadmap gaps + Tier 0 |
| Architecture Agent | Returns None (threshold filter only) | 397B designs multi-file plans with steps, contracts, acceptance |
| Voice transparency | Silent during autonomous work | "Entering REM Sleep..." / "3 capability gaps found" / "PR ready" |
| Cost attribution | N/A | Per-caller tracking (synthesis vs architect vs governance) |
