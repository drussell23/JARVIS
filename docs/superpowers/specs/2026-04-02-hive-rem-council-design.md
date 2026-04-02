# Hive REM Council — Design Spec

**Date:** 2026-04-02
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved
**Depends on:** Phase 1+2 Hive backend (261 tests), `hive_service.py` orchestrator

## Overview

The REM Council is a structured review session that runs during the Cognitive FSM's REM state (every 6+ hours during idle periods). It executes three review modules sequentially — Health Scanner, Graduation Auditor, Manifesto Compliance Reviewer — each producing Hive threads with findings that the Trinity Personas can triage using the 35B model. Critical findings escalate to FLOW for deep 397B reasoning.

The council is **triage, not solution**. It identifies problems; FLOW solves them.

## 1. Session Lifecycle

```
FSM enters REM (T1: REM_TRIGGER)
    ↓
HiveService._run_rem_council()
    ↓
Module 1: Health Scanner (~15 LLM calls)
    ↓
Module 2: Graduation Auditor (~15 LLM calls)
    ↓
Module 3: Manifesto Compliance (~15 LLM calls)
    ↓
Evaluate findings:
    ↓ critical finding → T2b: COUNCIL_ESCALATION → FLOW
    ↓ all informational → T3b: COUNCIL_COMPLETE → BASELINE
```

**Budget enforcement:** Total session capped at `JARVIS_HIVE_REM_MAX_CALLS` (default 50). Each module gets `max_calls // 3` with remainder as reserve. If budget exhausts mid-module, remaining modules are skipped and session completes.

**Model:** All REM reasoning uses `Qwen/Qwen3.5-35B-A3B-FP8` (cheap triage, 3B active). The 397B is reserved for FLOW.

## 2. Module 1: Health Scanner

**Purpose:** Detect system degradation patterns from metrics and error history.

**Inputs (deterministic — no LLM needed to gather):**
- Current system metrics via `psutil`: RAM percent, CPU percent, disk usage
- Recent error count from Python logging (last 6h)
- Active process count and resource hogs
- Hive-specific: thread count, tokens consumed, debate timeout rate

**LLM analysis (35B):**
- Persona: JARVIS (observe) — synthesizes raw metrics into a health narrative
- If degradation detected: J-Prime (propose) — suggests investigation or mitigation
- If proposal: Reactor (validate) — risk assessment

**Thread creation:**
- One thread per significant finding (e.g., "RAM trending upward", "Error spike in vision loop")
- Minor findings (all green) → single summary thread, no debate needed
- Severity mapping: metric within normal range = info, approaching threshold = warning, breached = error

**Escalation rule:** Any finding with severity >= "error" triggers `COUNCIL_ESCALATION` → FLOW.

## 3. Module 2: Graduation Auditor

**Purpose:** Review the Ouroboros ephemeral tool graduation pipeline per Manifesto §6 (Neuroplasticity).

**Inputs (deterministic):**
- Ouroboros ledger at `~/.jarvis/ouroboros/ledger/` — scan for tool usage records
- Graduation threshold: `count >= 3`
- Staleness threshold: not used in 30+ days (configurable via `JARVIS_HIVE_TOOL_STALE_DAYS`, default 30)

**LLM analysis (35B):**
- JARVIS (observe): summarizes graduation candidates and stale tools
- J-Prime (propose): recommends which candidates should graduate, which stale tools to prune
- Reactor (validate): checks if graduation/pruning is safe

**Thread creation:**
- One thread for graduation candidates (if any exist)
- One thread for stale tool pruning (if any exist)
- If no candidates and no stale tools: no threads created, module completes silently

**Escalation rule:** Graduation candidates with `count >= 5` (strong signal) escalate to FLOW for actual code generation via Ouroboros.

## 4. Module 3: Manifesto Compliance Reviewer

**Purpose:** Review recent code changes against the Symbiotic AI-Native Manifesto principles.

**Inputs (deterministic):**
- `git log --since={last_rem_timestamp} --name-only` — list of changed files
- `git diff {last_rem_hash}..HEAD --stat` — change summary
- Last REM timestamp stored in `~/.jarvis/hive/last_rem_at` (ISO format)

**LLM analysis (35B):**
- For each changed file (up to 10 files, prioritized by diff size):
  - Read first 200 lines of the file
  - JARVIS (observe): "Does this file follow the Boundary Principle?"
  - Checks for: hardcoded patterns that should be agentic, agentic patterns that should be deterministic, missing observability, security boundary violations
- J-Prime (propose): aggregated recommendations across all reviewed files
- Reactor (validate): risk assessment of proposed changes

**Thread creation:**
- One thread per file with findings (if compliance issues detected)
- Summary thread for "all reviewed files compliant" (if clean)

**Caps:**
- Max 10 files reviewed per session (prioritized by largest diff)
- Max 200 lines read per file
- Files matching secret denylist skipped: `.env`, `*credentials*`, `*secret*`, `*.key`, `*.pem`

**Escalation rule:** Files with multiple Manifesto violations escalate to FLOW for deeper analysis.

## 5. REM Council Runner

**New class: `RemCouncil`**

```python
class RemCouncil:
    def __init__(
        self,
        persona_engine: PersonaEngine,
        thread_manager: ThreadManager,
        relay: HudRelayAgent,
        max_calls: int = 50,
    ) -> None

    async def run_session(self) -> RemSessionResult:
        """Execute all three modules within budget. Returns summary."""

    async def _run_health_scanner(self, budget: int) -> list[str]:
        """Returns list of thread_ids created."""

    async def _run_graduation_auditor(self, budget: int) -> list[str]:
        """Returns list of thread_ids created."""

    async def _run_manifesto_reviewer(self, budget: int) -> list[str]:
        """Returns list of thread_ids created."""
```

**RemSessionResult dataclass:**
```python
@dataclass
class RemSessionResult:
    threads_created: list[str]
    calls_used: int
    calls_budget: int
    should_escalate: bool          # True if any critical finding
    escalation_thread_id: str | None
    modules_completed: list[str]   # ["health", "graduation", "manifesto"]
    modules_skipped: list[str]     # Budget-exhausted modules
```

## 6. Integration with HiveService

The existing `HiveService._rem_poll_loop()` currently only checks FSM eligibility. After this spec, when REM triggers:

```python
# In HiveService, after FSM transitions to REM:
council = RemCouncil(self._persona_engine, self.thread_manager, self._relay)
result = await council.run_session()

if result.should_escalate:
    # Escalate to FLOW for the critical thread
    decision = self.fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
    self.fsm.apply_last_decision()
    # Start debate on the escalation thread
    self._flow_thread_ids.add(result.escalation_thread_id)
    asyncio.create_task(self._run_debate_round(result.escalation_thread_id))
else:
    # Council complete, back to BASELINE
    decision = self.fsm.decide(CognitiveEvent.COUNCIL_COMPLETE)
    self.fsm.apply_last_decision()
```

## 7. New Files

| File | Responsibility |
|------|----------------|
| `backend/hive/rem_council.py` | RemCouncil runner + RemSessionResult |
| `backend/hive/rem_health_scanner.py` | Module 1: system health metrics collection + analysis |
| `backend/hive/rem_graduation_auditor.py` | Module 2: Ouroboros ledger scanning + graduation logic |
| `backend/hive/rem_manifesto_reviewer.py` | Module 3: git diff analysis + compliance checking |
| `tests/test_hive_rem_council.py` | Council runner tests (session lifecycle, budget, escalation) |
| `tests/test_hive_rem_health.py` | Health scanner tests |
| `tests/test_hive_rem_graduation.py` | Graduation auditor tests |
| `tests/test_hive_rem_manifesto.py` | Manifesto reviewer tests |

## 8. Modified Files

| File | Change |
|------|--------|
| `backend/hive/hive_service.py` | Wire `RemCouncil` into `_rem_poll_loop()` when FSM enters REM |

## 9. Environment Variables (new)

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_HIVE_TOOL_STALE_DAYS` | `30` | Days before an unused ephemeral tool is considered stale |
| `JARVIS_HIVE_REM_MAX_FILES` | `10` | Max files reviewed per Manifesto compliance session |
| `JARVIS_HIVE_REM_MAX_LINES_PER_FILE` | `200` | Max lines read per file during compliance review |

## 10. Testing Strategy

- **RemCouncil:** Mock all three modules. Verify sequential execution, budget splitting, escalation detection, module skip on budget exhaustion.
- **Health Scanner:** Mock `psutil` metrics. Verify thread creation for degradation, no threads for healthy system, severity mapping.
- **Graduation Auditor:** Mock ledger directory with sample files. Verify candidate detection (count >= 3), stale detection (>30 days), no threads when clean.
- **Manifesto Reviewer:** Mock `git log`/`git diff` output and file reads. Verify per-file analysis, cap enforcement (max 10 files, 200 lines), secret denylist, thread creation for violations.
- **Integration:** Wire RemCouncil into HiveService, mock Doubleword, verify REM→council→BASELINE or REM→council→FLOW escalation.

## 11. Out of Scope

- Automated fix generation during REM (that's FLOW's job)
- Historical trend analysis across REM sessions
- Custom review modules (plugin architecture for council)
- Notification push to phone/watch for REM findings
