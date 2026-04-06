# Ouroboros Battle Test Runner — Design Spec

**Date:** 2026-04-06  
**Status:** Approved  
**Author:** Derek J. Russell + Claude  

---

## Purpose

A standalone script that boots the full Ouroboros governance brain — without the unified supervisor, voice, TUI, or vision — and lets it autonomously find and apply real improvements to the JARVIS codebase. This is the living proof test: does the organism actually work?

---

## What This Is

A headless Ouroboros daemon (`scripts/ouroboros_battle_test.py`) that:
1. Boots all components started by `GovernedLoopService` + `IntakeLayerService` for this headless profile (everything except vision, voice output, TUI, and HUD)
2. Creates an accumulation branch for all changes
3. Lets intake sensors find real improvement opportunities
4. Runs operations through the full 10-phase governed pipeline
5. Auto-applies SAFE_AUTO operations, writes APPROVAL_REQUIRED to a review queue file
6. Tracks RSI convergence data (composite scores, transition probabilities)
7. Stops when: session budget exhausted ($0.50) OR SIGINT (Ctrl+C) OR idle (no work found for 10 minutes)
8. Prints terminal summary and generates a Jupyter analysis notebook (or Markdown fallback if jupyter/matplotlib not installed)

## What This Is NOT

- Not the full JARVIS supervisor (no zone 1-9 boot)
- Not interactive (no TUI, no voice, no vision)
- Not multi-repo (JARVIS-only for v1)
- Not a benchmark (no predefined tasks — the organism finds its own work)

---

## Architecture

```
scripts/ouroboros_battle_test.py
    |
    +-- BattleTestHarness (orchestrates everything)
    |       |
    |       +-- GovernanceStack (Phase 0-3: risk, policy, ledger, change engine)
    |       |       +-- RiskEngine (deterministic classification)
    |       |       +-- PolicyEngine (YAML rules)
    |       |       +-- OperationLedger (append-only JSONL)
    |       |       +-- ChangeEngine (filesystem patches)
    |       |
    |       +-- GovernedLoopService (the pipeline)
    |       |       +-- GovernedOrchestrator (10-phase FSM)
    |       |       +-- CandidateGenerator (with failback)
    |       |       +-- BrainSelector (Doubleword 397B primary)
    |       |       +-- Providers (PrimeProvider, ClaudeProvider)
    |       |       +-- ShadowHarness (sandboxed validation)
    |       |       +-- ContextExpander (TheOracle-backed)
    |       |       |
    |       |       +-- Pre-GENERATE injection layers:
    |       |       |       +-- RuntimePromptAdapter (technique #1)
    |       |       |       +-- ModuleLevelMutator (technique #2)
    |       |       |       +-- NegativeConstraintStore (technique #3)
    |       |       |       +-- CodeMetricsAnalyzer (technique #4)
    |       |       |       +-- DynamicRePlanner (technique #5)
    |       |       |       +-- MultiVersionEvolutionTracker (technique #6)
    |       |       |       +-- GenerateVerifyRefine (technique #7)
    |       |       |       +-- HierarchicalMemory (technique #8)
    |       |       |       +-- RepositoryAutoDocumentation (technique #9)
    |       |       |       +-- LearningConsolidator
    |       |       |       +-- SuccessPatternStore
    |       |       |       +-- TestCoverageEnforcer
    |       |       |       +-- HierarchicalFaultLocalizer
    |       |       |       +-- SlowFastThinkingRouter
    |       |       |       +-- DocAugmentedRepair
    |       |       |
    |       |       +-- Post-operation:
    |       |               +-- CompositeScoreFunction (RSI)
    |       |               +-- ConvergenceTracker (RSI)
    |       |               +-- TransitionProbabilityTracker (RSI)
    |       |               +-- VindicationReflector (RSI)
    |       |               +-- OraclePreScorer (RSI)
    |       |
    |       +-- JARVIS-Level Tiers:
    |       |       +-- Tier 1: OperationAdvisor (proactive judgment)
    |       |       +-- Tier 2: EmergencyProtocolEngine (5-level escalation)
    |       |       +-- Tier 3: PredictiveRegressionEngine (4-hour cycle)
    |       |       +-- Tier 5: UnifiedIntelligenceLayer (cross-domain)
    |       |       +-- Tier 6: PersonalityEngine (deterministic state)
    |       |       +-- Tier 7: AutonomousJudgmentFramework (daily review)
    |       |
    |       +-- TheOracle (GraphRAG codebase index)
    |       |
    |       +-- IntakeLayerService (sensors — headless profile)
    |       |       +-- TestFailureSensor            [ENABLED]
    |       |       +-- OpportunityMinerSensor       [ENABLED]
    |       |       +-- CapabilityGapSensor          [ENABLED]
    |       |       +-- BacklogSensor                [ENABLED]
    |       |       +-- ScheduledTriggerSensor       [ENABLED]
    |       |       +-- RuntimeHealthSensor          [ENABLED]
    |       |       +-- WebIntelligenceSensor        [ENABLED]
    |       |       +-- PerformanceRegressionSensor  [ENABLED]
    |       |       +-- DocStalenessSensor           [ENABLED]
    |       |       +-- GitHubIssueSensor            [ENABLED]
    |       |       +-- ProactiveExplorationSensor   [ENABLED]
    |       |       +-- TodoScannerSensor            [ENABLED]
    |       |       +-- VoiceCommandSensor           [DISABLED — no mic input in headless]
    |       |       +-- CUExecutionSensor            [DISABLED — no screen in headless]
    |       |       +-- CrossRepoDriftSensor         [DISABLED — single-repo registry for v1]
    |       |       +-- ReactorEventConsumer         [DISABLED — no Reactor in v1]
    |       |
    |       +-- GraduationOrchestrator (ephemeral -> permanent)
    |       |       +-- EphemeralUsageTracker (adaptive Bayesian threshold)
    |       |
    |       +-- CostTracker (daily budget enforcement)
    |       +-- BranchManager (accumulation branch lifecycle)
    |       +-- SessionRecorder (stats, logs, notebook generation)
    |
    +-- notebooks/ouroboros_battle_test_analysis.ipynb (generated output)
```

---

## Components

### 1. BattleTestHarness

The top-level orchestrator. Single class, single file. Responsibilities:
- Parse CLI args (cost cap, branch name prefix, idle timeout)
- Boot components in dependency order (Oracle first, then governance stack, then loop service, then intake)
- Create accumulation branch via git
- Wire SIGINT handler for graceful shutdown
- Run the main event loop (asyncio)
- On stop: collect stats, print summary, generate notebook

### 2. CostTracker

Lightweight wrapper that monitors cumulative Doubleword API spend during the session.
- Reads cost from BrainSelector.record_cost() or response.cost_usd attributes
- When cumulative cost >= session budget ($0.50 default): sets `budget_exhausted` asyncio.Event that the harness awaits
- **Budget semantics:** Per-session, not calendar-day. Each invocation of `ouroboros_battle_test.py` gets a fresh budget. No timezone ambiguity. Persisted to session JSON so a crash-and-restart within the same session resumes the existing budget.
- **Integration hook:** If `GovernedLoopService` has no pre-dequeue budget check, the harness injects a callback via the `pre_operation_gate` hook (if available) or patches `_can_accept_operation()` at construction time. This is the one allowed minimal core touch — documented in the implementation plan if discovery shows it's needed.

### 3. BranchManager

Manages the accumulation branch lifecycle:
- On start: `git checkout -b ouroboros/battle-test-{YYYY-MM-DD-HHMMSS}`
- On each APPLY: `git add` changed files + `git commit` with structured message
- On stop: print branch summary (total commits, files changed, diff stats)
- Does NOT merge or push — that's the human's decision after review

Commit message format:
```
ouroboros({sensor}): {short description}

Operation: {op_id}
Risk: {risk_tier}
Composite Score: {score}
Technique: {primary_technique}
Auto-applied: true
```

### 4. SessionRecorder

Collects all session data for the terminal summary and notebook:
- Operations attempted, completed, failed, cancelled
- Composite scores over time
- Convergence state at end of session
- Technique success rates
- Cost breakdown
- Sensor activation counts
- Time-to-complete per operation
- Git diff stats

Writes to `~/.jarvis/ouroboros/battle-test/{session_id}/summary.json`

### 5. NotebookGenerator

Generates a pre-populated Jupyter notebook from session data:
- Cell 1: Load session data from summary.json and composite_scores.jsonl
- Cell 2: Composite score trend plot with logarithmic fit overlay (matplotlib)
- Cell 3: Convergence state classification and recommendation
- Cell 4: Transition probability heatmap (technique x domain, seaborn)
- Cell 5: Operations breakdown pie chart (COMPLETE/FAILED/CANCELLED)
- Cell 6: Sensor activation bar chart (which sensors found real work?)
- Cell 7: Cost breakdown (tokens consumed, cost per operation)
- Cell 8: Git diff summary (files changed, insertions, deletions)

Output: `notebooks/ouroboros_battle_test_analysis.ipynb`

**Optional dependencies:** `jupyter`, `matplotlib`, `seaborn`. If not installed, the generator falls back to a Markdown report at `~/.jarvis/ouroboros/battle-test/{session_id}/report.md` with the same data in text/table format.

---

## Git Preconditions

Before creating the accumulation branch:
1. **Working tree must be clean** (`git status --porcelain` is empty). If dirty, abort with message: "Commit or stash your changes before running the battle test."
2. **Must be on `main` branch** (or configurable base branch via `--base-branch`). Accumulation branch is created from HEAD of the base branch.
3. **Branch must not already exist.** If `ouroboros/battle-test-{id}` exists, append a counter suffix.
4. **No rebase/merge assumptions.** The accumulation branch is disposable. Review via `git diff main..branch`, then merge or delete.

## Boot Sequence

```
1. Parse CLI args
2. Validate environment (API keys, repo paths)
3. Validate git preconditions (clean tree, on base branch)
4. Create accumulation branch
5. Initialize TheOracle (index JARVIS codebase) [~10-30s]
6. Create GovernanceStack (risk engine, policy engine, ledger, change engine) [~5s]
7. Create GovernedLoopService (orchestrator, providers, brain selector) [~5s]
8. Initialize JARVIS-level tiers (advisor, emergency, predictive, intelligence, personality, judgment) [~5s]
9. Start IntakeLayerService (headless sensor profile — disable voice/CU/cross-repo/reactor sensors) [~5s]
10. Start GraduationOrchestrator [~2s]
11. Print "Ouroboros is alive. Watching JARVIS repo. Session budget: $0.50."
12. Enter event-driven main loop
```

Total boot: ~30-60 seconds (dominated by Oracle indexing).

## Main Loop

The system is **event-driven**, not polling. Sensors push IntentEnvelopes into the intake router, which dispatches to GovernedLoopService asynchronously. The harness waits for shutdown signals:

```python
# Three asyncio.Events — first one to fire stops the session
shutdown_event = asyncio.Event()      # SIGINT handler sets this
budget_event = asyncio.Event()        # CostTracker sets when budget exhausted
idle_event = asyncio.Event()          # IdleWatchdog sets after idle_timeout

# Register SIGINT
loop.add_signal_handler(signal.SIGINT, shutdown_event.set)

# Sensors feed IntentEnvelopes into the intake router (event-driven)
# IntakeRouter dispatches to GovernedLoopService
# GovernedLoopService runs the 10-phase pipeline
# SAFE_AUTO operations auto-apply to accumulation branch
# APPROVAL_REQUIRED operations written to review queue JSONL

# Wait for any stop condition
done, _ = await asyncio.wait(
    [shutdown_event.wait(), budget_event.wait(), idle_event.wait()],
    return_when=asyncio.FIRST_COMPLETED,
)
```

**IdleWatchdog:** A background task that resets a timer on every `COMPLETE`/`FAILED` operation callback. If no operation completes for `idle_timeout` seconds (default 600), it fires `idle_event`.

## Shutdown Sequence

```
1. Signal intake sensors to stop
2. Wait for any in-flight operations to complete (30s timeout)
3. Collect session stats
4. Print terminal summary
5. Generate Jupyter notebook
6. Print: "Session complete. Review branch: ouroboros/battle-test-{id}"
7. Print: "Run: jupyter notebook notebooks/ouroboros_battle_test_analysis.ipynb"
```

---

## Configuration

### Required Environment Variables
```bash
DOUBLEWORD_API_KEY=dw_...        # Doubleword API (397B primary)
ANTHROPIC_API_KEY=sk-ant-...     # Claude API (fallback)
```

### Optional (with defaults)
```bash
OUROBOROS_BATTLE_COST_CAP=0.50           # Daily cost cap in USD
OUROBOROS_BATTLE_IDLE_TIMEOUT=600        # Seconds before idle shutdown (10 min)
OUROBOROS_BATTLE_BRANCH_PREFIX=ouroboros/battle-test  # Branch name prefix
JARVIS_GOVERNANCE_MODE=governed          # Must be governed for auto-apply
JARVIS_REPO_PATH=.                       # Path to JARVIS repo (default: cwd)
```

### CLI Args
```bash
python3 scripts/ouroboros_battle_test.py \
    --cost-cap 0.50 \
    --idle-timeout 600 \
    --branch-prefix ouroboros/battle-test \
    --repo-path /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
```

---

## Terminal Summary Format

```
============================================================
  OUROBOROS BATTLE TEST — SESSION COMPLETE
============================================================

  Session ID:    bt-2026-04-06-143022
  Duration:      47m 23s
  Stop reason:   Cost cap reached ($0.50)

  OPERATIONS
  ----------
  Attempted:     34
  Completed:     28  (82.4%)
  Failed:        4   (11.8%)
  Cancelled:     2   (5.9%)
  Queued (approval):  6  (see review_queue.jsonl)

  CONVERGENCE
  -----------
  State:         IMPROVING
  Slope:         -0.0142
  R² (log fit):  0.73
  Recommendation: Pipeline is converging. Continue current strategy.

  COST
  ----
  Doubleword:    $0.48 (397B: $0.41, 35B: $0.07)
  Claude:        $0.00 (no fallback needed)
  Total:         $0.48

  TOP TECHNIQUES
  --------------
  1. module_mutation       P(success)=0.82  (14/17)
  2. metrics_feedback      P(success)=0.71  (5/7)
  3. negative_constraints  P(success)=0.67  (4/6)

  TOP SENSORS
  -----------
  1. OpportunityMinerSensor    12 operations
  2. TestFailureSensor          8 operations
  3. DocStalenessSensor         6 operations

  BRANCH
  ------
  Branch:     ouroboros/battle-test-2026-04-06-143022
  Commits:    28
  Files:      42 changed
  Insertions: +1,247
  Deletions:  -389

  Next steps:
    git diff main..ouroboros/battle-test-2026-04-06-143022
    jupyter notebook notebooks/ouroboros_battle_test_analysis.ipynb

============================================================
```

---

## Legitimately Excluded Components

| Component | Why |
|---|---|
| Vision System | No screen to perceive in headless mode |
| Cross-Repo Saga | JARVIS-only for v1 (saga code loaded but no multi-repo ops) |
| Tier 4: Distributed Resilience | Requires GCP secondary instance (PARTIAL status) |
| Voice Narration | Headless — narration events fire but go to log |
| TUI Dashboard | Headless — stats go to summary JSON |
| Swift HUD / IPC Server | No HUD process to talk to |
| Docker lifecycle | Not containerized for battle test |
| GCP VM lifecycle | Local-only test |

All components started by `GovernedLoopService` and `IntakeLayerService` are active for this headless profile. The narration and dashboard events still fire internally — they write to logs/JSON instead of screens/speakers. Sensors that require I/O hardware (voice, vision) or multi-repo infrastructure (cross-repo drift, reactor events) are disabled via config, not removed.

---

## File Structure

### New Files
| File | Purpose |
|---|---|
| `scripts/ouroboros_battle_test.py` | Main entry point — BattleTestHarness + CLI |
| `scripts/battle_test_notebook_generator.py` | Generates pre-populated Jupyter notebook |
| `notebooks/ouroboros_battle_test_analysis.ipynb` | Generated analysis notebook (gitignored template) |

### Modified Files (minimal, if needed)
The battle test script imports existing components and avoids core changes. However, if discovery during implementation reveals that `GovernedLoopService` has no pre-dequeue hook for the budget gate, **one small addition** is allowed: a `pre_operation_gate` callback on GLS that the harness sets at construction. This will be documented explicitly in the implementation plan if needed. No other core modifications.

### Output Files (runtime, not committed)
| File | Purpose |
|---|---|
| `~/.jarvis/ouroboros/battle-test/{session_id}/summary.json` | Session stats |
| `~/.jarvis/ouroboros/battle-test/{session_id}/review_queue.jsonl` | APPROVAL_REQUIRED operations (not invisible — reviewable) |
| `~/.jarvis/ouroboros/evolution/composite_scores.jsonl` | Score history |
| `~/.jarvis/ouroboros/evolution/transition_probabilities.json` | Technique data |
| `~/.jarvis/ouroboros/ledger/` | Operation ledger entries |

---

## Success Criteria

The battle test succeeds if:
1. Ouroboros boots without the full supervisor and reaches "alive" state
2. At least one sensor fires and produces an IntentEnvelope
3. At least one operation completes the full 10-phase pipeline
4. At least one SAFE_AUTO change is auto-applied to the accumulation branch
5. Composite scores are recorded and the convergence tracker produces a report
6. The generated notebook renders and shows real data
7. The accumulation branch passes `pytest` on touched files and introduces no new lint errors (not just "compilable" — verifiably correct for a Python monorepo)

The battle test proves the organism works if:
1. Convergence state is IMPROVING or LOGARITHMIC after 20+ operations
2. Composite scores trend downward (quality improving)
3. At least 2 different sensors produce completed operations
4. At least 2 different techniques have P(success) > 0.5
5. No HOUSE_PARTY or RED emergency triggered
6. The branch diff, when reviewed by a human, contains sensible improvements
