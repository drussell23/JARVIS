# Ouroboros Daemon: Proactive Self-Evolution Engine

**Date:** 2026-03-28
**Status:** Design approved, pending implementation
**Zone:** 7.0 (capstone boot phase, after GLS 6.8 + IntakeLayer 6.9)
**Approach:** B — Unified OuroborosDaemon lifecycle

---

## Preamble

Ouroboros is the most important feature in the Trinity Ecosystem. Without it, Trinity is a sophisticated assistant that does what you ask. With it, Trinity is a self-evolving organism that wakes up, surveys its own body, identifies what's broken or missing, and starts fixing itself.

Today, the infrastructure is 80-90% built but dormant. ExplorationFleet, ExplorationSubagent, UnlimitedFleetOrchestrator, BackgroundAgentPool, ScheduledAgentRunner, TheOracle's analysis methods (find_dead_code, find_circular_dependencies, compute_blast_radius), ProactiveDrive idle detection, GraduationOrchestrator, TrustGraduator — all exist as isolated modules, wired at boot but never activated. The organs exist. The nervous system connecting them doesn't fire.

This spec defines the `OuroborosDaemon` — a single class that breathes life into every dormant organ and transforms Ouroboros from a reactive intake loop into a proactive, self-executing autonomous daemon.

### Governing Philosophy

**The Symbiotic AI-Native Manifesto v2 — Boundary Mandate:**

Deterministic code is the skeleton — fast, reliable, testable, predictable. Agentic intelligence is the nervous system — adaptive, creative, contextual, resilient. The skeleton does not think; the nervous system does not hold weight.

Applied to this design:
- **Phase 1 (Vital Scan):** Deterministic. Known checks with known pass/fail criteria. Zero model inference.
- **Phase 2 (Spinal Cord):** Deterministic. Subscription wiring, handshake confirmation. Zero model inference.
- **Phase 3 (REM Sleep):** Agentic. ExplorationSubagents reason about code, Doubleword 397B synthesizes patches, the governance pipeline validates and applies. Intelligence deployed where ignorance exists.

### Trust Boundary

**GOVERNED-from-boot.** Ouroboros is a fully autonomous daemon, not an observer that waits for human approval. The existing governance pipeline's safety rails (RiskEngine, SecurityReviewer, canary checks, sandbox execution) serve as the immune system. The TrustGraduator evolves per-signal autonomy over time (OBSERVE → SUGGEST → GOVERNED → AUTONOMOUS).

OBSERVE-first was rejected because it keeps the human as a bottleneck. The organism must be capable of autonomous maintenance and evolution from its first breath.

---

## Architecture Overview

```
unified_supervisor.py boots zones 1-6.9 (existing, unchanged)
                    |
                    v
           +--- ZONE 7.0: OuroborosDaemon.awaken() ---+
           |                                            |
           |  Phase 1: VITAL SCAN (blocking, <=30s)     |
           |  +- Oracle.initialize() (load cached graph) |
           |  +- find_circular_dependencies()            |
           |  +- CrossRepoDriftSensor.scan_once()        |
           |  +- RuntimeHealthSensor.scan_once()         |
           |  +- Result: VitalReport (pass/warn/fail)    |
           |     +- FAIL -> degraded mode, voice alert   |
           |     +- WARN -> findings queued for REM      |
           |     +- PASS -> clean organism               |
           |                                            |
           |  Phase 2: SPINAL CORD (async, <=10s)        |
           |  +- Wire governance channel -> EventStream  |
           |  +- Wire approval events -> EventStream     |
           |  +- Wire findings (up) + patches (down)     |
           |  +- SpinalGate + SpinalLiveness established  |
           |  +- Bidirectional: Body <-> Mind <-> Soul   |
           |                                            |
           |  Phase 3: REM SLEEP (background daemon)     |
           |  +- Activate ExplorationFleet               |
           |  +- Activate BackgroundAgentPool             |
           |  +- Connect ProactiveDrive.ELIGIBLE -> fleet |
           |  +- Oracle: dead code, complexity, gaps      |
           |  +- Findings -> IntakeRouter -> pipeline     |
           |  +- Doubleword 397B for heavy analysis       |
           |  +- Patches -> GATE -> APPLY -> Git PR       |
           |                                            |
           +--------------------------------------------+
```

---

## Phase 1: Vital Scan (Boot Invariant Gate)

### Purpose

Verify structural integrity of the organism before any agentic work begins. This is the narrow blocking exception to Progressive Awakening — safety and contract invariants only. Zones 1-6.9 remain progressively ready; only Zone 7.0 Phase 1 blocks, and only for invariants.

### Contract

```
Inputs:
  - TheOracle cached graph (from ~/.jarvis/oracle/codebase_graph.pkl)
  - RepoRegistry (JARVIS, J-Prime, Reactor paths)
  - RuntimeHealthSensor (from IntakeLayerService)

Outputs:
  - VitalReport(status: PASS|WARN|FAIL, findings: List[VitalFinding])
  - FAIL findings -> organism runs degraded (zones 1-6.9 healthy, self-evolution offline)
  - WARN findings -> queued as IntentEnvelopes for REM Sleep
  - PASS -> clean organism, no findings

Hard Timeout: 30 seconds (env: OUROBOROS_VITAL_SCAN_TIMEOUT_S)
  - If timeout exceeded -> VitalReport(WARN) with partial results
  - Never blocks boot indefinitely

Model Calls: ZERO
  - All checks are deterministic: graph queries, file hashes, version comparisons

Idempotency:
  - Safe to re-run on supervisor restart
  - Oracle cache loaded, not rebuilt (rebuild belongs in Phase 3)
  - No side effects — pure read-only analysis
```

### Checks and Criteria

| Check | Method | FAIL | WARN | PASS |
|-------|--------|------|------|------|
| Circular imports | `Oracle.find_circular_dependencies()` | Cycle includes `unified_supervisor.py` or `governed_loop_service.py` (kernel integrity) | Any other cycles detected | No cycles |
| Contract drift | File hash comparison across repos | Protocol version mismatch (J-Prime schema != JARVIS schema) | File content drift without version mismatch | Hashes match |
| Dependency health | `RuntimeHealthSensor.scan_once()` | Critical CVE in active dependency | Stale packages, minor advisories | All healthy |
| Oracle cache freshness | `_last_indexed_monotonic_ns` age check | No cache exists AND repos have >500 files (cold boot too slow) | Cache >24h stale | Cache <24h |

### What Does NOT Belong in Phase 1

Pushed to Phase 3 (agentic tier):
- Dead code detection — requires graph traversal interpretation
- Complexity hotspots — cyclomatic complexity is numeric but prioritization is agentic
- Architecture gaps — requires semantic reasoning about intent vs implementation
- Test coverage — requires mapping between production and test files

### Integration Point

```python
# unified_supervisor.py Zone 7.0
vital_report = await daemon.vital_scan(timeout_s=30.0)
if vital_report.status == VitalStatus.FAIL:
    logger.critical("[Zone 7.0] Vital scan FAILED: %s", vital_report)
    await safe_say("Ouroboros vital scan failed. Review required.")
    # Don't raise — organism runs degraded, not dead
    # Kernel (zones 1-6.9) is healthy; only self-evolution is offline
elif vital_report.status == VitalStatus.WARN:
    daemon.queue_for_rem_sleep(vital_report.findings)
    await safe_say(f"Ouroboros online with {len(vital_report.warnings)} warnings.")
else:
    await safe_say("Ouroboros online. Organism fully awakened.")
```

---

## Phase 2: Spinal Cord (Nervous System Connectivity)

### Purpose

Wire bidirectional event channels between Body (MacBook), Mind (GCP), and Soul (governance pipeline) so Phase 3 has a real-time communication backbone. No intelligence deployed — this is plumbing.

### Contract

```
Inputs:
  - EventStreamProtocol (already active, 6 channels, 3-layer protocol)
  - GovernedLoopService (Zone 6.8, already running)
  - IntakeLayerService (Zone 6.9, already running)

Outputs:
  - SpinalGate (one-shot asyncio.Event: "safe to start first fleet deploy")
  - SpinalLiveness (dynamic flag: stream vs local-buffer mode)
  - 3 new subscriptions on governance channel
  - Bidirectional flow confirmed via handshake

Hard Timeout: 10 seconds (env: OUROBOROS_SPINAL_TIMEOUT_S)
  - Outer cap encompasses ALL inner waits (subscribe + handshake)
  - If timeout -> SpinalStatus.DEGRADED (Phase 3 runs local-only)

Model Calls: ZERO

Idempotency:
  - All subscriptions use idempotent subscribe() (duplicate = no-op)
  - SpinalGate is monotonic — once set, stays set
  - Safe to re-run on reconnect
```

### Stream Definitions

```
STREAM UP (Body -> Mind):                    STREAM DOWN (Mind -> Body):
+---------------------------+               +---------------------------+
| exploration.finding        |               | generation.candidate      |
|  - ExplorationFinding      |               |  - Patch diff from        |
|  - Source repo + scope     |               |    Doubleword/J-Prime     |
|  - Category + confidence   |               |                           |
|                            |               | governance.decision       |
| governance.progress        |               |  - GATE verdict           |
|  - Pipeline phase changes  |               |  - APPROVE result         |
|  - Operation status        |               |  - APPLY outcome          |
|                            |               |                           |
| oracle.insight             |               | governance.patch_applied  |
|  - Dead code found         |               |  - Git commit SHA         |
|  - Blast radius computed   |               |  - PR URL                 |
|  - Structural anomaly      |               |  - Files changed          |
+---------------------------+               +---------------------------+
```

### Ordering Guarantees

```python
async def wire_spinal_cord(self) -> SpinalStatus:
    """Phase 2: Establish bidirectional governance streams.

    Ordering contract:
    1. Subscribe to all channels FIRST (no events lost)
    2. Verify subscriptions via echo handshake
    3. Emit spinal_ready ONLY after handshake confirmed
    4. Phase 3 MUST await SpinalGate before first fleet deploy
    """
```

**Protocol Guarantee — No Events Lost:**

The EventStreamProtocol's ReplayBuffer (monotonic seq, bounded 500-entry deque) provides the buffering guarantee. Producers on the governance channel buffer from the subscription cursor. Consumers registered via `subscribe()` receive all events from their subscription point forward. No events are dropped for `NEVER`-drop-policy channels.

**Echo Scope:**

The echo handshake is **transport-level** — it proves reachability and basic framing on the governance channel. Service attachment (GLS and IntakeLayerService are consuming these event types) is a separate invariant guaranteed by Zones 6.8/6.9 being ACTIVE before Zone 7.0 starts.

### Two-Flag State Machine

SpinalGate and SpinalLiveness serve different purposes:

- **SpinalGate** (`asyncio.Event`, one-shot): "Phase 2 succeeded at least once. Safe to start REM." Set once, never cleared. Prevents Phase 3 from racing Phase 2.
- **SpinalLiveness** (`bool`, dynamic): Flips on disconnect/reconnect. When False, REM writes to `~/.jarvis/ouroboros/pending_findings.jsonl` instead of streaming. When connectivity resumes, ReplayBuffer replays the buffer. TUI shows "SPINAL: CONNECTED" vs "SPINAL: LOCAL-ONLY".

### Degraded Fallback

If the Spinal Cord cannot establish (GCP unreachable, WebSocket down), Phase 3 still runs in local-only mode:
- Exploration findings stored in `~/.jarvis/ouroboros/pending_findings.jsonl`
- Governance pipeline runs locally (J-Prime fallback to Claude API)
- When connectivity resumes, spinal cord replays the buffer

---

## Phase 3: REM Sleep (Autonomous Maintenance Daemon)

### Purpose

The organism's autonomous nervous system. After boot, Ouroboros continuously explores itself, identifies defects and opportunities, synthesizes patches via the 397B cognitive engine, and applies them through the governance pipeline. The organism physically grows while idle.

### Contract

```
Inputs:
  - SpinalGate (one-shot: safe to start) <- Phase 2
  - SpinalLiveness (dynamic: stream vs buffer) <- Phase 2
  - ExplorationFleet (wired in GLS, dormant)
  - BackgroundAgentPool (started, awaits submissions)
  - TheOracle (initialized, cached graph)
  - ProactiveDrive (idle verifiers, state machine)
  - DoublewordProvider (Tier 0, Qwen3.5-397B batch API)
  - UnifiedIntakeRouter (Zone 6.9)
  - GovernedLoopService (Zone 6.8, full pipeline)

Outputs:
  - Continuous stream of IntentEnvelopes (source: "exploration")
  - Governance pipeline operations (CLASSIFY -> ... -> COMPLETE)
  - Git PRs for graduated patches
  - TUI dashboard + voice narration of autonomous activity

Hard Timeouts:
  - Single exploration cycle: 300s (env: OUROBOROS_REM_CYCLE_TIMEOUT_S)
  - Single governance operation: 150s (existing JARVIS_PIPELINE_TIMEOUT_S)
  - Doubleword batch poll: min(epoch_remaining, DOUBLEWORD_MAX_WAIT_S)
  - Full REM epoch (explore+analyze+patch): 1800s (env: OUROBOROS_REM_EPOCH_TIMEOUT_S)

Model Calls: YES — this is the agentic tier
  - ExplorationSubagent: AST-only by default. Model gated behind
    OUROBOROS_EXPLORATION_MODEL_ENABLED=false with RPM budget.
  - Doubleword 397B: heavy analysis + patch synthesis (primary)
  - J-Prime 7B: fallback for generation if Doubleword unavailable
  - Claude API: final fallback

Idempotency:
  - All IntentEnvelopes carry dedup_key (SHA256)
  - IntakeRouter dedup window prevents duplicate submissions
  - Governance operations carry idempotency_key
  - epoch_id (monotonic) correlates all artifacts within an epoch
  - Safe to restart mid-cycle — WAL recovers in-flight ops
```

### Internal State Machine

```
                    +------------------------------+
                    |         REM SLEEP             |
                    |                               |
  Phase 2 done --> |  IDLE_WATCH                   |
                    |  | ProactiveDrive ticks (10s) |
                    |  | All 3 repos idle?           |
                    |  | 60s eligibility timer       |
                    |  v                             |
                    |  EXPLORING                     |
                    |  | ExplorationFleet.deploy()   |
                    |  | Oracle.find_dead_code()     |
                    |  | Oracle.find_circular_deps() |
                    |  | Oracle.compute_blast_radius |
                    |  | All 9 checks run parallel   |
                    |  | Findings streamed UP        |
                    |  v                             |
                    |  ANALYZING                     |
                    |  | Rank findings by impact      |
                    |  | Doubleword 397B: deep analysis|
                    |  | (batch API, async)           |
                    |  | Results streamed DOWN        |
                    |  v                             |
                    |  PATCHING                      |
                    |  | Convert findings -> envelopes|
                    |  | IntakeRouter.ingest()        |
                    |  | Pipeline: CLASSIFY -> ... -> |
                    |  |   GATE -> APPLY -> VERIFY    |
                    |  | BackgroundAgentPool executes  |
                    |  | Git PRs created              |
                    |  v                             |
                    |  COOLDOWN                      |
                    |  | 3600s (env configurable)     |
                    |  | Then -> IDLE_WATCH           |
                    |                               |
                    |  ---- INTERRUPT ----           |
                    |  User activity detected         |
                    |  -> Cooperative pause via token  |
                    |  -> Resume when idle returns     |
                    +------------------------------+
```

### The Ignition Wire

The single missing connection that activates everything:

```python
self._proactive_drive.on_eligible(self._begin_rem_epoch)
```

When ProactiveDrive detects all 3 repos idle for 60s continuously, `_begin_rem_epoch()` fires. This is the wire that connects idle detection to exploration.

**Idle scope:** Eligibility = ProactiveDrive's Little's Law verifiers (queue idle across repos). REM's own I/O (fleet reads, Oracle queries) does NOT count as repo activity — these are read-only operations, not enqueued governance operations.

### EXPLORING Substate — All 9 Checks in Parallel

```python
async def _begin_rem_epoch(self) -> None:
    """Full organism self-scan. All checks run concurrently."""
    self._current_epoch_id = next(self._epoch_counter)  # monotonic
    token = CancellationToken(epoch_id=self._current_epoch_id)

    async with asyncio.TaskGroup() as tg:
        # Oracle graph analysis (deterministic, cached graph)
        dead_code    = tg.create_task(self._oracle_dead_code(token))
        circular     = tg.create_task(self._oracle_circular_deps(token))
        complexity   = tg.create_task(self._oracle_complexity_hotspots(token))

        # ExplorationFleet (agentic, AST + import chain analysis)
        fleet_report = tg.create_task(self._fleet.deploy(
            goal="Identify unwired components, architecture gaps, "
                 "and dormant agents across Trinity ecosystem",
            repos=("jarvis", "jarvis-prime", "reactor"),
            max_agents=self._config.rem_max_agents,
        ))

        # Existing sensors (one-shot scans)
        test_gaps    = tg.create_task(self._scan_test_coverage(token))
        todos        = tg.create_task(self._scan_todos(token))
        doc_stale    = tg.create_task(self._scan_doc_staleness(token))
        perf_regress = tg.create_task(self._scan_perf_regressions(token))
        github_issues= tg.create_task(self._scan_github_issues(token))

    all_findings = self._merge_and_rank(...)
    for finding in all_findings:
        await self._stream_up("exploration.finding", finding)

    await self._transition(RemState.ANALYZING, findings=all_findings)
```

### ANALYZING Substate — Doubleword 397B

```python
async def _analyze_findings(self, findings: List[RankedFinding]) -> None:
    """Send top findings to Doubleword 397B for deep analysis.

    Boundary Principle: ranking is deterministic (impact score).
    Analysis and patch synthesis are agentic (397B model).
    """
    top_findings = findings[:self._config.rem_max_findings_per_epoch]

    # Timeout: min(epoch_remaining, doubleword_budget)
    remaining = self._epoch_deadline - time.monotonic()
    timeout = min(remaining, self._config.doubleword_max_wait_s)

    batch_result = await asyncio.wait_for(
        self._doubleword.submit_and_retrieve(
            findings=top_findings,
            context_fn=self._build_finding_context,
        ),
        timeout=timeout,
    )

    envelopes = self._findings_to_envelopes(batch_result)
    await self._transition(RemState.PATCHING, envelopes=envelopes)
```

### PATCHING Substate — Governance Pipeline

```python
async def _execute_patches(self, envelopes: List[IntentEnvelope]) -> None:
    """Route findings through full governance pipeline.

    Each envelope goes through:
    CLASSIFY -> ROUTE -> CONTEXT_EXPANSION -> GENERATE ->
    VALIDATE -> GATE -> [APPROVE if risky] -> APPLY -> VERIFY -> COMPLETE

    Safety rails (all existing, no new code needed):
    - RiskEngine: BLOCKED for supervisor/security surface changes
    - GATE: canary check + security review
    - APPROVE: auto for SAFE_AUTO, 10-min timeout for APPROVAL_REQUIRED
    - Sandbox execution for all APPLY operations
    """
    for envelope in envelopes:
        result = await self._intake_router.ingest(envelope)
        if result == "backpressure":
            break  # organism is busy, stop feeding

        await self._stream_down("governance.progress", {
            "envelope_id": envelope.signal_id,
            "epoch_id": self._current_epoch_id,
            "status": result,
        })

    await self._transition(RemState.COOLDOWN)
```

### COOLDOWN Semantics

COOLDOWN runs after ANY epoch completion — success, fail, or cancel. This prevents retry storms after failures and gives the organism time to observe the effects of patches before the next cycle.

- Default: 3600s (env: `OUROBOROS_REM_COOLDOWN_S`)
- Failed epochs: log to WAL for post-mortem analysis
- On COOLDOWN expiry: transition to IDLE_WATCH, wait for next idle eligibility

### Epoch Identity and Correlation

Every artifact within a REM epoch carries `epoch_id` (monotonic int):
- ExplorationFindings: `finding.epoch_id`
- Doubleword batch jobs: `batch.metadata.epoch_id`
- IntentEnvelopes: `envelope.evidence["epoch_id"]`
- Governance operations: `op_context.metadata["epoch_id"]`

On interrupt and resume: if a new epoch starts (new `epoch_id`), stale Doubleword batch results from a previous epoch are ignored — the `epoch_id` on the batch result won't match `self._current_epoch_id`.

### Timeout Hierarchy

```
OUROBOROS_REM_EPOCH_TIMEOUT_S = 1800  (outer cap)
  |
  +-- EXPLORING: OUROBOROS_REM_CYCLE_TIMEOUT_S = 300
  |
  +-- ANALYZING: min(epoch_remaining, DOUBLEWORD_MAX_WAIT_S)
  |     - epoch_remaining = 1800 - elapsed
  |     - DOUBLEWORD_MAX_WAIT_S = 3600 (but capped by epoch)
  |     - If epoch timeout fires: detach batch with WAL record
  |
  +-- PATCHING: governed by per-operation JARVIS_PIPELINE_TIMEOUT_S = 150
        - Each governance op has its own timeout
        - PATCHING itself limited by epoch_remaining
```

Epoch timeout cancels or detaches all in-flight work. WAL records preserve state for post-mortem.

### Cooperative Pause/Cancel Semantics

`_paused = True` does not stop a `TaskGroup`. Pause is cooperative:

- **CancellationToken**: Passed to every task in the epoch. Contains `epoch_id` and `cancelled: asyncio.Event`.
- **ExplorationSubagent**: Checks `token.cancelled` between file reads. Contract: `finish_current_file_then_yield()` is a real API — the agent completes its current file analysis, then returns partial results.
- **Oracle queries**: Synchronous, sub-second — no cancellation needed.
- **Doubleword batch**: Fire-and-forget. If paused during ANALYZING, the batch continues on Doubleword's side. On resume, check if batch completed and epoch_id matches.

Resume behavior: when idle returns, daemon checks current substate and resumes from where it paused. If epoch_id changed (stale), starts fresh.

### ExplorationSubagent Model Policy

Default path is **AST-only** — no model inference during exploration. Model reasoning is gated behind:
- `OUROBOROS_EXPLORATION_MODEL_ENABLED=false` (default)
- When enabled: lightweight model only (J-Prime 7B), with RPM budget (`OUROBOROS_EXPLORATION_MODEL_RPM=10`)
- Model used for: "is this dead code intentional?" and "what is the purpose of this orphaned import?" — questions AST cannot answer

This is not a contradiction with "Model Calls: YES" — the model calls in Phase 3 are primarily Doubleword 397B in ANALYZING and J-Prime/Claude in GENERATE (governance pipeline). Exploration model use is optional and budget-constrained.

### Deterministic Ranking Formula

```python
def impact_score(finding: Finding) -> float:
    """Deterministic, testable, versioned (v1.0).

    blast_radius: normalized 0-1 (from Oracle.compute_blast_radius)
    confidence:   0-1 (from sensor/fleet)
    urgency:      critical=1.0, high=0.75, normal=0.5, low=0.25
    recency:      1.0 if file modified in last 7 days, decays linearly to 0.0 at 90 days
    """
    return (
        blast_radius * 0.4 +
        confidence   * 0.3 +
        urgency      * 0.2 +
        recency      * 0.1
    )
    # Tie-breaker: alphabetical file path (deterministic, reproducible)
```

### RiskEngine Rules for Exploration Source

New rule added to `risk_engine.py` for `source == "exploration"`:

```python
# Exploration-sourced intents have STRICTER rules:
if source == "exploration":
    if touches_supervisor:     -> BLOCKED  (cannot self-modify kernel)
    if touches_ouroboros_code:  -> BLOCKED  (cannot self-modify self-evolution)
    if touches_security_surface: -> BLOCKED (auth, secrets, credentials)
    if touches_config_files:   -> APPROVAL_REQUIRED (env, settings)
    if blast_radius > 3:       -> APPROVAL_REQUIRED (lower than default 5)
    # All other rules from base risk engine also apply
```

The organism cannot modify its own kernel or governance code autonomously. It can patch application code, agents, tests, and documentation.

### New IntentEnvelope Source

Add `"exploration"` to `_VALID_SOURCES` in `intent_envelope.py`:

```python
_VALID_SOURCES = frozenset({
    "backlog", "test_failure", "voice_human", "ai_miner",
    "capability_gap", "runtime_health", "exploration",  # NEW
})
```

Priority in `_PRIORITY_MAP`: 4 (between `ai_miner` at 3 and `runtime_health` at 5).
`requires_human_ack`: False (GOVERNED tier, risk engine handles safety).

### Interrupt Handling

```python
async def _on_activity_detected(self) -> None:
    """User activity breaks idle state. Cooperative pause."""
    if self._rem_state in (RemState.EXPLORING, RemState.ANALYZING):
        self._cancellation_token.cancel()
        # Fleet agents finish current file, then yield
        # Doubleword batch continues in background (fire-and-forget)
        self._paused = True
    elif self._rem_state == RemState.PATCHING:
        # Pipeline ops already submitted -> they complete independently
        # Just stop feeding new envelopes
        self._paused = True
    # When idle returns -> resume from current substate if epoch_id matches
```

### Memory Budget

Local (16GB MacBook M1):
- ExplorationSubagent: read-only AST parsing, bounded by `max_files` per agent
- Oracle queries: in-memory NetworkX graph (already loaded)
- Fleet agent state: ~1KB per agent, 30 agents max = ~30KB
- Findings buffer: bounded by `rem_max_findings_per_epoch` (default 10)
- MemoryBudgetGuard: existing 85% cap, checked before each agent spawn

Remote (GCP hybrid cloud):
- Doubleword 397B: runs on Doubleword infrastructure (API call, zero local GPU)
- J-Prime 7B: runs on g2-standard-4+L4 (separate machine)
- Only local cost: HTTP request overhead

---

## Zone 7.0 Integration

### Boot Sequence

```
Zone 1-5:   Hardware, audio, voice, TUI, API       (existing, unchanged)
Zone 6.0-6.7: Intelligence, routing, vision         (existing, unchanged)
Zone 6.8:  GovernedLoopService.start()              (existing, unchanged)
Zone 6.9:  IntakeLayerService.start()               (existing, unchanged)
Zone 7.0:  OuroborosDaemon.awaken()                 (NEW)
            +- Phase 1: vital_scan()          [blocking, <=30s]
            |   +- FAIL -> degraded mode
            |   +- WARN -> findings queued
            |   +- PASS -> continue
            +- Phase 2: wire_spinal_cord()    [async, <=10s]
            |   +- SpinalGate set (one-shot)
            |   +- SpinalLiveness tracking begins
            +- Phase 3: start_rem_daemon()    [background task]
                +- Returns immediately
                +- Waits for SpinalGate before first epoch

Total added boot time: <=40s worst case (30s vital + 10s spinal)
Typical: <5s (Oracle cache hit + local echo handshake)
```

### Wiring in unified_supervisor.py

```python
# ---- Zone 7.0: Ouroboros Daemon ----
if self._config.ouroboros_daemon_enabled:  # env: OUROBOROS_DAEMON_ENABLED
    try:
        from backend.core.ouroboros.daemon import OuroborosDaemon

        daemon = OuroborosDaemon(
            oracle=self._gls._oracle,
            fleet=self._gls._exploration_fleet,
            bg_pool=self._gls._bg_pool,
            intake_router=self._intake_layer._router,
            event_stream=self._event_stream,
            proactive_drive=self._proactive_drive,
            doubleword=self._gls._doubleword_provider,
            gls=self._gls,
            config=OuroborosDaemonConfig.from_env(),
        )

        report = await daemon.awaken()
        self._ouroboros_daemon = daemon

        if report.vital_status == VitalStatus.FAIL:
            logger.critical("[Zone 7.0] Vital scan FAILED: %s", report)
            await safe_say("Ouroboros vital scan failed. Review required.")
        else:
            logger.info("[Zone 7.0] OuroborosDaemon online: %s", report)

    except Exception as exc:
        logger.warning("[Zone 7.0] OuroborosDaemon failed: %s", exc)
        # Graceful degradation — organism runs without self-evolution
```

### Shutdown Integration

```python
# In supervisor shutdown sequence (reverse order)
if self._ouroboros_daemon is not None:
    await self._ouroboros_daemon.shutdown()
    # Drain in-flight governance ops
    # Persist REM epoch state to WAL
    # Cancel fleet agents gracefully via CancellationToken
```

### OuroborosDaemon Public API

```python
class OuroborosDaemon:
    """Zone 7.0 -- Proactive self-evolution daemon.

    Lifecycle: awaken() at boot, shutdown() at supervisor teardown.
    Three phases run in sequence; Phase 3 runs as background daemon.

    Dependencies (injected, not constructed):
        oracle: TheOracle
        fleet: ExplorationFleet
        bg_pool: BackgroundAgentPool
        intake_router: UnifiedIntakeRouter
        event_stream: EventStreamProtocol
        proactive_drive: ProactiveDrive
        doubleword: DoublewordProvider
        gls: GovernedLoopService
        config: OuroborosDaemonConfig
    """

    # --- Lifecycle ---
    async def awaken(self) -> AwakeningReport
    async def shutdown(self) -> None

    # --- Phase 1 ---
    async def vital_scan(self, timeout_s: float = 30.0) -> VitalReport

    # --- Phase 2 ---
    async def wire_spinal_cord(self) -> SpinalStatus

    # --- Phase 3 ---
    async def start_rem_daemon(self) -> None

    # --- Observability ---
    def health(self) -> DaemonHealth
    def metrics(self) -> DaemonMetrics
```

---

## File Structure

```
backend/core/ouroboros/
+-- daemon.py                          # OuroborosDaemon (Zone 7.0 entry)
+-- daemon_config.py                   # OuroborosDaemonConfig (env-driven)
+-- vital_scan.py                      # Phase 1: VitalScan, VitalReport, VitalFinding
+-- spinal_cord.py                     # Phase 2: SpinalCord, SpinalGate, SpinalLiveness
+-- rem_sleep.py                       # Phase 3: RemSleepDaemon, RemState machine
+-- rem_epoch.py                       # Single epoch: explore -> analyze -> patch
+-- finding_ranker.py                  # Deterministic merge_and_rank (impact_score v1.0)
+-- exploration_envelope_factory.py    # Finding -> IntentEnvelope conversion
+-- cancellation_token.py             # CancellationToken (epoch-scoped cooperative cancel)
```

Modifications to existing files:
- `intent_envelope.py`: Add `"exploration"` to `_VALID_SOURCES`
- `unified_intake_router.py`: Add `"exploration"` to `_PRIORITY_MAP` (priority 4)
- `risk_engine.py`: Add exploration-source stricter rules
- `unified_supervisor.py`: Add Zone 7.0 wiring block
- `exploration_subagent.py`: Add `finish_current_file_then_yield()` API
- `governed_loop_service.py`: Expose `_oracle`, `_exploration_fleet`, `_bg_pool`, `_doubleword_provider` for daemon injection (or add getter methods)

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_DAEMON_ENABLED` | `true` | Master toggle for Zone 7.0 |
| `OUROBOROS_VITAL_SCAN_TIMEOUT_S` | `30` | Phase 1 blocking timeout |
| `OUROBOROS_SPINAL_TIMEOUT_S` | `10` | Phase 2 wiring timeout |
| `OUROBOROS_REM_ENABLED` | `true` | Phase 3 toggle (daemon can boot without REM) |
| `OUROBOROS_REM_CYCLE_TIMEOUT_S` | `300` | Single exploration cycle cap |
| `OUROBOROS_REM_EPOCH_TIMEOUT_S` | `1800` | Full epoch (explore+analyze+patch) |
| `OUROBOROS_REM_MAX_AGENTS` | `30` | ExplorationFleet agent cap |
| `OUROBOROS_REM_MAX_FINDINGS` | `10` | Findings sent to Doubleword per epoch |
| `OUROBOROS_REM_COOLDOWN_S` | `3600` | Between epochs |
| `OUROBOROS_REM_IDLE_ELIGIBLE_S` | `60` | Idle time before exploration starts |
| `OUROBOROS_EXPLORATION_MODEL_ENABLED` | `false` | Model reasoning during exploration |
| `OUROBOROS_EXPLORATION_MODEL_RPM` | `10` | RPM budget if model enabled |

---

## End-to-End Data Flow (One Complete REM Cycle)

```
ProactiveDrive: all 3 repos idle for 60s
  -> on_eligible callback fires
    -> RemSleepDaemon._begin_rem_epoch(epoch_id=42)
      -> EXPLORING: 9 checks in parallel via TaskGroup
        -> Oracle: 3 dead functions found
        -> Fleet: 2 unwired agents, 1 orphaned import
        -> TodoScanner: 5 FIXME markers
        -> Total: 11 findings
      -> Stream UP: 11 exploration.finding events -> TUI dashboard
      -> ANALYZING: top 10 findings -> Doubleword 397B batch
        -> Doubleword returns: 6 actionable patches, 4 info-only
      -> Stream DOWN: 6 generation.candidate events
      -> PATCHING: 6 IntentEnvelopes (source="exploration")
        -> IntakeRouter.ingest() x 6
        -> Pipeline runs for each:
          -> CLASSIFY (structural) -> ROUTE (to GLS)
          -> GENERATE (patch from Doubleword, or J-Prime refines)
          -> VALIDATE (ShadowHarness tests)
          -> GATE (RiskEngine: 4 SAFE_AUTO, 2 APPROVAL_REQUIRED)
            -> 4 auto-applied -> Git commits
            -> 2 queued for approval (10-min timeout)
          -> APPLY (sandbox execution)
          -> VERIFY (test re-run)
          -> COMPLETE
        -> Stream DOWN: 4 governance.patch_applied events
        -> 2 Git PRs created for approved patches
      -> COOLDOWN: 3600s
        -> Organism grew 4 new capabilities, 2 pending review
```

---

## Day 1 Capabilities

What Ouroboros can do autonomously once this is implemented:

| Capability | Sensor/Tool | Example |
|-----------|-------------|---------|
| Find and fix bugs | Oracle dead code + fleet + pipeline | "Removing unreferenced `_legacy_voice_handler`" |
| Wire dormant components | Fleet detects "imported but never instantiated" | "Wiring PredictivePlanningAgent into voice pipeline" |
| Resolve TODOs | TodoScanner -> Doubleword synthesizes fix | "Implementing retry logic at `mind_client.py:234`" |
| Fix test gaps | Oracle test_counterparts -> generate tests | "Creating `test_spinal_cord.py`" |
| Fix contract drift | CrossRepoDrift -> patches both repos | "Aligning J-Prime schema 2c.1 with JARVIS" |
| Fix dependencies | RuntimeHealth -> CVE/stale -> version bumps | "Upgrading numpy for ARM64 GEMM fix" |
| Graduate tools | Usage tracking -> GraduationOrchestrator | "Graduating YouTube browser tool to `YouTubeAgent`" |
| Resolve GitHub issues | GitHubIssueSensor -> Doubleword -> patches | "Fixing Ghost Hands Retina display issue #47" |

## Future Extensions (Not in This Spec)

The daemon's architecture supports future sensors that transform it from maintenance daemon to autonomous developer:

- **Roadmap Sensor**: Reads docs, memory files, and conversation history to understand WHERE the system is going. Generates IntentEnvelopes for missing capabilities.
- **Feature Synthesis Engine**: Reasons about desired vs actual capabilities. "The Manifesto says we need app-specific agents — none exist yet."
- **Architecture Reasoning Agent**: Designs multi-file, multi-repo features requiring structural decisions.

Each is "just another sensor" plugged into IntakeLayerService. The execution pipeline (analyze -> generate -> validate -> apply -> verify) already exists.

---

## Testing Strategy

- **Phase 1**: Unit tests for each vital check. Mock Oracle graph with known cycles/drift.
- **Phase 2**: Integration test with in-process EventStream. Verify subscribe -> echo -> SpinalGate sequence.
- **Phase 3**: Unit tests for RemState machine transitions. Mock ProactiveDrive, Fleet, Doubleword. Integration test for full epoch cycle with in-memory providers.
- **Ranking**: Property-based tests for impact_score determinism and tie-breaking.
- **RiskEngine**: Unit tests for exploration-source rules (supervisor/auth/secrets -> BLOCKED).
- **Epoch correlation**: Test that stale batch results (mismatched epoch_id) are ignored on resume.
