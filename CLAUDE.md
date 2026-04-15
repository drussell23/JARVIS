# CLAUDE.md -- JARVIS Trinity AI Ecosystem

**Repository**: [github.com/drussell23/JARVIS-AI-Agent](https://github.com/drussell23/JARVIS-AI-Agent)
**Author**: Derek J. Russell -- RSI/AGI Researcher & Trinity Architect

## What This Is

JARVIS is the Body of a tri-partite AGI OS. This repo contains the macOS integration layer (screen capture, voice, keyboard automation), the unified supervisor (102K-line monolithic kernel), and the Ouroboros self-development governance engine. All model references are resolved from `brain_selection_policy.yaml` -- zero hardcoded model names.

## Architecture at a Glance

```
JARVIS (Body) <--HTTP/WS--> J-Prime (Mind) <--sandbox--> Reactor Core (Soul)
     |
     +-- unified_supervisor.py (102K lines, Zones 0-7)
     +-- backend/core/ouroboros/ (autonomous self-development)
     +-- backend/vision/ (VLA pipeline, OCR, frame server)
     +-- backend/ghost_hands/ (focus-preserving UI automation)
     +-- backend/voice/ (voice I/O, wake word, TTS)
     +-- backend/core_contexts/ (5 execution contexts: Executor, Architect, Developer, Communicator, Observer)
     +-- backend/neural_mesh/ (22 legacy agents, Strangler Fig fallback)
```

## Key Conventions

- **Async-first**: All I/O uses `asyncio`. No blocking calls on the event loop.
- **No hardcoded models**: All model names come from `brain_selection_policy.yaml` or env vars.
- **Env-var driven config**: Every tunable parameter reads from environment variables with sensible defaults.
- **Python 3.9+**: No `asyncio.timeout` (3.11+) -- use `asyncio.wait_for` everywhere.
- **`from __future__ import annotations`**: Required in all files for forward reference support.

## Ouroboros Pipeline

The self-development engine runs an 11-phase governance pipeline:

```
CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> [PLAN] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE
```

### Provider Chain (3-Tier Failback)

| Tier | Provider | Cost | Notes |
|------|----------|------|-------|
| 0 | DoubleWord 397B (3-tier: RT SSE + webhook + adaptive poll) | $0.10/$0.40/M | 16384 max_tokens, RT default, preferred |
| 1 | Claude (Anthropic API) | $3/$15/M | Extended thinking + prompt caching, 60s fallback cap |
| 2 | J-Prime (GCP self-hosted) | VM cost only | When available |

### Urgency-Aware Provider Routing (Manifesto §5)

Deterministic routing based on signal urgency + source + task complexity. Stamped at ROUTE phase by `UrgencyRouter` (`urgency_router.py`). No LLM calls — pure code, <1ms.

| Route | Strategy | Cost | When |
|-------|----------|------|------|
| IMMEDIATE | Claude direct, skip DW | ~$0.03/op | Critical urgency, voice commands, test failures, runtime health |
| STANDARD | DW primary → Claude fallback | ~$0.005/op | Normal-priority moderate ops (default cascade) |
| COMPLEX | Claude plans → DW executes | ~$0.015/op | heavy_code, multi-file architectural changes |
| BACKGROUND | DW only, no Claude fallback | ~$0.002/op | OpportunityMiner, DocStaleness, TODOs, backlog |
| SPECULATIVE | DW batch fire-and-forget | ~$0.001/op | IntentDiscovery, DreamEngine pre-computation |

### Timeout Enforcement (Route-Aware)

- **IMMEDIATE generation**: 60s + 5s grace (fast reflex — accounts for Venom tool rounds)
- **STANDARD generation**: 120s + 5s grace
- **COMPLEX/BACKGROUND generation**: 180s + 5s grace
- **Fallback provider**: Hard cap at 60s (`_FALLBACK_MAX_TIMEOUT_S`)
- **Tier 1 reserve**: 25s minimum (not 45s -- reduced to avoid starving Tier 0)
- **DW poll interval**: 5s (not 15s)
- **pytest (TestWatcher)**: 30s timeout
- **IMMEDIATE extended thinking**: Disabled by default (env: `JARVIS_THINKING_BUDGET_IMMEDIATE`, default 0). Route check fires before complexity checks.
- **IMMEDIATE → STANDARD demotion**: After Claude exhausts retries on IMMEDIATE, demotes to STANDARD (DW primary) for one more attempt

### Worker Pool

- **BackgroundAgentPool**: 3 workers (env: `JARVIS_BG_POOL_SIZE`), PriorityQueue (16 slots)
- **Priority ordering**: IMMEDIATE(1) > STANDARD/COMPLEX(3) > BACKGROUND(5) > SPECULATIVE(7)
- **Venom tool loop**: Enabled for IMMEDIATE/STANDARD/COMPLEX routes; skipped for BACKGROUND/SPECULATIVE (cost optimization)

### 16 Autonomous Sensors

TestFailure, VoiceCommand, OpportunityMiner, CapabilityGap, Scheduled, Backlog, RuntimeHealth, WebIntelligence, PerformanceRegression, DocStaleness, GitHubIssue, ProactiveExploration, CrossRepoDrift, TodoScanner, CUExecution, IntentDiscovery.

All flow through `UnifiedIntakeRouter` with priority queuing, deduplication, and WAL persistence.

### Key Subsystems

- **PlanGenerator** (`plan_generator.py`): Model-reasoned implementation planning (PLAN phase) -- structured JSON plan (schema plan.1) with approach, ordered changes, risk factors, test strategy. Injected into GENERATE prompt.
- **SemanticTriage** (`semantic_triage.py`): Pre-generation filter -- classifies NO_OP/REDIRECT/ENRICH/GENERATE before expensive generation
- **CommProtocol** (`comm_protocol.py`): 5-phase observability -- INTENT -> PLAN -> HEARTBEAT -> DECISION -> POSTMORTEM
- **SerpentFlow** (`battle_test/serpent_flow.py`, 1900+ lines): CC-style flowing CLI with `Update(path)` blocks, numbered diffs, per-op reasoning
- **LiveDashboard** (`battle_test/live_dashboard.py`, 1233 lines): Persistent Rich TUI with 3-channel terminal muting
- **Venom** (`tool_executor.py`): Multi-turn agentic tool loop -- 16 built-in tools + MCP external tools. Built-in: read_file, search_code, edit_file, write_file, bash, web_fetch, web_search, run_tests, get_callers, glob_files, list_dir, list_symbols, git_log, git_diff, git_blame, ask_human. MCP tools from external servers discovered at prompt time and forwarded (Gap #7). Live context auto-compaction between rounds (Gap #8).
- **L2 Repair** (`repair_engine.py`): Iterative self-repair FSM (5 iterations, 120s timebox). **Enabled by default** (`JARVIS_L2_ENABLED=true`) — engages when VALIDATE exhausts retries, closes the Ouroboros cycle per Manifesto §6.
- **Iron Gate** (orchestrator.py post-GENERATE): Two deterministic gates flow through the GENERATE retry loop with targeted feedback. (1) Exploration-first (`JARVIS_EXPLORATION_GATE`): min 2 `read_file`/`search_code`/`get_callers` calls before any patch (trivial ops bypass). When `JARVIS_EXPLORATION_LEDGER_ENABLED=true` (default off), the gate switches from the legacy int counter to `ExplorationLedger` — diversity-weighted scoring across categories (comprehension / discovery / call_graph / structure / history) with env-tunable per-complexity floors (`JARVIS_EXPLORATION_MIN_SCORE_<COMPLEXITY>`, `JARVIS_EXPLORATION_MIN_CATEGORIES_<COMPLEXITY>`). Insufficient verdicts raise `ExplorationInsufficientError(verdict, floors)` and the retry path renders a category-aware feedback block via `render_retry_feedback(exc.verdict, exc.floors)` naming the missing categories. Log tags distinguish modes: `ExplorationLedger(decision)` when enforcing, `ExplorationLedger(shadow)` when observing only, `ExplorationLedger(shadow,partial)` from the post-exception handler on generation failures. (2) ASCII-strictness (`JARVIS_ASCII_GATE`): rejects any non-ASCII codepoint in candidate content to prevent Unicode corruption (e.g. `rapidفuzz` → blocked). Manifesto §6 Iron Gate enforcement.
- **Multi-file coordinated generation** (orchestrator.py `_iter_candidate_files`/`_apply_multi_file_candidate`): Candidates may return a `files: [{file_path, full_content, rationale}, ...]` list in addition to the legacy single `file_path`/`full_content` pair. Every file is AST/placeholder-validated at the parser, and the APPLY path composes per-file `ChangeEngine.execute` calls with **batch-level rollback** — if file N fails, files 1..N-1 are restored from pre-apply snapshots (new files are unlinked). Preserves the 8-phase guarantees per file while adding atomic multi-file semantics. Master switch: `JARVIS_MULTI_FILE_GEN_ENABLED` (default `true`).
- **ConsciousnessBridge** (`consciousness_bridge.py`): Injects memory/prediction into pipeline
- **StrategicDirection** (`strategic_direction.py`): Manifesto principles injected into every generation prompt. Additionally infers recent development momentum from the last 50 `git log` commits via Conventional Commit parsing — extracts scope/type histograms and the three freshest subject lines into a "Recent Development Momentum" section of the digest. Manifesto §4 (synthetic soul). Gated by `JARVIS_STRATEGIC_GIT_HISTORY_ENABLED` (default `true`).
- **AutoCommitter** (`auto_committer.py`): Structured git commits with O+V signature after successful APPLY+VERIFY. Conventional commit format, risk-tier metadata, protected-branch push prevention.
- **OrangePRReviewer** (`orange_pr_reviewer.py`): Async-review path for Orange-tier (`APPROVAL_REQUIRED`) changes. Instead of blocking the loop on a synchronous CLI approval, creates a `ouroboros/review/{op-id}` branch, commits the candidate, pushes, and files a GitHub PR via `gh pr create` with evidence + review checklist in the body. The autonomous loop continues immediately; the human reviews asynchronously. Opt-in via `JARVIS_ORANGE_PR_ENABLED` (default `false`). On any failure, falls back to the existing CLI approval provider. Manifesto §7 (absolute observability) — the PR is the auditable artifact.
- **DreamEngine** (`consciousness/dream_engine.py`): Idle GPU speculative improvement blueprints
- **TestRunner** (`test_runner.py`): Multi-strategy async test discovery + pytest execution. Env: `JARVIS_TEST_TIMEOUT_S`, `JARVIS_TEST_RETRY_ENABLED`, `JARVIS_TEST_MAX_FILES`, `JARVIS_TEST_DIR_NAMES`. Known fix: sandbox path resolution via original_paths mapping (commit 22f297d).

### Autonomous Developer Intelligence (O+V)

O+V is **proactive** (self-initiating), not reactive (human-prompted). Key capabilities:

- **4-tier risk escalation**: `SAFE_AUTO` / `NOTIFY_APPLY` / `APPROVAL_REQUIRED` / `BLOCKED` -- Green/Yellow auto-apply, Orange blocks for human
- **Exploration-first**: Generation prompt requires 2+ tool calls before any patch. **Hard-enforced** at the Iron Gate (post-GENERATE, pre-VALIDATE) — violators route through GENERATE_RETRY with targeted "you MUST call read_file/search_code" feedback before any patch reaches disk.
- **Post-apply verification**: Scoped test run after APPLY, routes failures to L2 repair. L2 candidate now applied via change_engine.
- **Session intelligence**: `_session_lessons` buffer (20 max) with infra/code tagging. Convergence metric auto-clears misleading lessons.
- **Cost-aware priority**: `_compute_priority()` factors urgency, file count, confidence, dependency credit (capped at 3).
- **Dependency DAG**: `_active_file_ops` with TTL-based stale lock release (`JARVIS_FILE_LOCK_TTL_S`, default 300s).
- **Signal coalescing**: Same-file signals merged into single operation within `JARVIS_COALESCE_WINDOW_S` (default 30s).
- **Stale exploration guard**: File hashes snapshotted at GENERATE, verified at APPLY. Stale candidates logged.
- **REPL /cancel**: `cancel <op-id>` cooperative cancellation, checked at GENERATE and APPLY phase boundaries.
- **Diff preview for Yellow**: `JARVIS_NOTIFY_APPLY_DELAY_S` (default 5s) delay with diff rendered before auto-apply.
- **Per-op reasoning**: Model rationale captured at GENERATE, displayed in SerpentFlow `Update` blocks.
- **Model-reasoned planning**: PLAN phase between CONTEXT_EXPANSION and GENERATE. Model reasons about implementation strategy (schema plan.1) before writing code. Trivial ops skip planning.
- **Mid-operation clarification**: `ask_human` tool in Venom lets the model ask the human for clarification. Gated to NOTIFY_APPLY+ risk tiers (Green ops don't interrupt).
- **L3 worktree isolation**: Enabled by default (`JARVIS_GOVERNED_L3_ENABLED=true`). Parallel execution graphs use isolated git worktrees to prevent filesystem conflicts.
- **Auto-commit post-APPLY**: AutoCommitter creates structured git commits with O+V signature after VERIFY passes. Conventional commit type/scope inference, risk-tier metadata, protected-branch push prevention. Master switch: `JARVIS_AUTO_COMMIT_ENABLED`.
- **MCP tool forwarding**: External MCP tools discovered from connected servers and injected into generation prompt (Gap #7). Model can call `mcp_{server}_{tool}` during tool loop. Policy engine auto-allows MCP tools; external servers handle their own auth.
- **Live context auto-compaction**: When tool loop prompt exceeds 75% of budget, older tool results are compacted into a deterministic summary (Gap #8). Preserves recent 6 chunks. No model inference. Env: `JARVIS_TOOL_LOOP_COMPACT_THRESHOLD`.
- **UserPreferenceMemory** (`user_preference_memory.py`): Persistent typed memory across O+V sessions, modeled on Claude Code auto-memory (typed `.md` files with YAML frontmatter + `MEMORY.md` index). Six types: `USER` / `FEEDBACK` / `PROJECT` / `REFERENCE` / `FORBIDDEN_PATH` / `STYLE`. Storage lives at `.jarvis/user_preferences/`. Three integration points: (1) StrategicDirection injects a relevance-scored "User Preferences" prompt section at CONTEXT_EXPANSION (scored by path overlap > tag match > type bonus, FORBIDDEN_PATH doubled on matching target). (2) ToolExecutor's `_is_protected_path` consults a global provider hook — every `FORBIDDEN_PATH` memory becomes a hard block on Venom `edit_file`/`write_file`/`delete_file` (same layer as the hardcoded `.git/`, `.env`, `credentials` list). (3) Post-rejection postmortem: when a human rejects an `APPROVAL_REQUIRED` op, `orchestrator.py` auto-extracts the rejection reason into a `FEEDBACK` memory tagged `("rejection", "approval")`, deduped by op-description slug so repeat rejections upsert rather than pile up. Manifesto §4 (synthetic soul, cross-session learning) + §6 (threshold-triggered neuroplasticity).

## Battle Test

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Boots the full 6-layer stack: GovernedLoopService, IntakeLayer (16 sensors), TrinityConsciousness, StrategicDirection, CommProtocol, SerpentFlow CLI.

On startup, the harness auto-reaps any zombie `ouroboros_battle_test.py` processes from earlier crashed sessions (psutil-based, strict path-tail match, SIGTERM → SIGKILL escalation) and removes stale `.jarvis/intake_router.lock` files whose owning PID is dead. Prevents budget competition between sessions. Master switch: `JARVIS_BATTLE_REAP_ZOMBIES` (default `true`).

## File Layout (Key Paths)

```
backend/core/ouroboros/
  governance/
    governed_loop_service.py    # Main loop (Zone 6.8)
    orchestrator.py             # 11-phase FSM
    candidate_generator.py      # 3-tier failback + route-based dispatch
    urgency_router.py           # Deterministic provider routing (§5 Tier 0)
    providers.py                # Claude + Prime providers
    doubleword_provider.py      # DW 397B
    plan_generator.py           # Model-reasoned PLAN phase (schema plan.1)
    semantic_triage.py          # Pre-generation filter
    comm_protocol.py            # 5-phase observability
    tool_executor.py            # Venom tool loop + live context compaction
    auto_committer.py           # Auto-commit with O+V signature (Gap #6)
    mcp_tool_client.py          # MCP external tool client (Gap #7)
    context_compaction.py       # Live context auto-compaction (Gap #8)
    batch_future_registry.py    # Zero-poll webhook batch futures (DW Tier 1)
    event_channel.py            # Webhook receiver (DW + GitHub + CI)
    repair_engine.py            # L2 self-repair
    consciousness_bridge.py     # Consciousness integration
    strategic_direction.py      # Manifesto injection
    user_preference_memory.py   # Persistent typed memory across sessions (Task #195)
    serpent_animation.py        # ASCII animation
    intake/
      sensors/                  # 16 sensors (5,400+ lines)
    intent/
      test_watcher.py           # Pytest polling (30s timeout)
      signals.py                # IntentSignal dataclass
  consciousness/                # Zone 6.11 (7,063 lines)
    consciousness_service.py    # TrinityConsciousness orchestrator
    health_cortex.py            # System health monitoring
    memory_engine.py            # Per-file reputation tracking
    dream_engine.py             # Idle GPU speculative analysis
    prophecy_engine.py          # Regression prediction
  battle_test/
    harness.py                  # 6-layer stack boot
    serpent_flow.py             # SerpentFlow: CC-style CLI (1,900+ lines)
    live_dashboard.py           # Persistent Rich TUI (1,233 lines)
  oracle.py                     # Codebase semantic index
```

## The Governing Philosophy

This is not a software refactor. It is the genesis of an autonomous, self-evolving AI Operating System. The 7 principles:

1. **Unified organism** -- tri-partite microkernel, single entry point
2. **Progressive awakening** -- adaptive lifecycle, no blocking boot chains
3. **Asynchronous tendrils** -- structured concurrency, no event loop starvation
4. **Synthetic soul** -- episodic awareness, cross-session learning
5. **Intelligence-driven routing** -- semantic, not regex; DAGs, not scripts
6. **Threshold-triggered neuroplasticity** -- Ouroboros: detect gaps, synthesize, graduate
7. **Absolute observability** -- every autonomous decision is visible

**Zero-shortcut mandate**: No brute-force retries without diagnosis. No hardcoded routing tables. Structural repair, not bypasses.

## Battle Test Milestones

Full postmortems for sustained battle-test breakthroughs live in `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`. The canonical source of truth for any "did the loop work" question is the session `debug.log` under `.ouroboros/sessions/<session-id>/`, not `summary.json` (which has a known `attempted` counter bug).

**2026-04-11 (`bt-2026-04-11-154947`)** — First sustained full-pipeline completion since the Apr 9–10 Iron Gate tightening. `op-019d7d3e` (requirements.txt upgrade) traversed CLASSIFY → GENERATE → IRON_GATE_REJECT → REGENERATE → APPLY → DECISION(applied) → VERIFY → L2 → POSTMORTEM autonomously. `dependency_file_integrity` Iron Gate caught a hallucinated `anthropic → anthropichttp` rename on attempt 1. Unblocker: captured-client race fix in `providers.py` — `_do_stream`/`_create_with_prefill_fallback`/`_legacy_create`/`_plan_create` now re-acquire `self._client` on every `_call_with_backoff` retry so recycles after hard-pool signals are visible to subsequent attempts.

**2026-04-12 (`bt-2026-04-12-073546`)** — First session with IMMEDIATE thinking cap and DurableJSONL sandbox fix. Validated 5 independent fixes: (1) fallback_concurrency=3 aligned with pool size — zero sem contention across 3 concurrent workers, (2) outer gate grace raised 5s→15s, (3) async sensor scans (OpportunityMiner, TodoScanner, DocStaleness) via run_in_executor, (4) IMMEDIATE route thinking disabled — first_token dropped from 94.5s to 961ms (98x), (5) DurableJSONL routed through sandbox_fallback with error suppression. Furthest progression: INTENT→GENERATE→VALIDATE. TestRunner sandbox path bug fixed in commit 22f297d (original_paths mapping + multi-strategy discovery).

**2026-04-14 (Sessions A–G, reflex + exploration loop)** — `ExplorationLedger` enforcement validated in production. Shadow-mode scorer catches shallow `4× read_file` exploration (`score=3.00 categories=comprehension`) that the legacy int-counter gate waved through. Flipping `JARVIS_EXPLORATION_LEDGER_ENABLED=true` turns the scorer into a hard Iron Gate rejection. Session G proved the full adaptation loop: retry feedback injects category-aware guidance → model diversifies tool selection to include `get_callers` / `git_blame` / `search_code` (first production appearance of these on retry) → second ledger decision `would_pass=True` at `score=25.50 categories=call_graph,comprehension,discovery,history,structure`. Multiple coordinated fixes shipped as commits `614009ec05` (sem trace + tool-round audit), `db13f045ce` (route-aware 900s pool ceiling), `4f60a584f9` (diversity multiplier + `list_dir` → DISCOVERY remap + complex tier 10.0 floor), `ad05fb7c7e` (unconditional sharpened retry feedback + 180s complex fallback cap), `5d169266d6` (env-tunable safety-net thresholds). Full postmortem in OUROBOROS.md breakthrough log.

**2026-04-15 (`bt-2026-04-15-175547`, Session O)** — **First end-to-end autonomous APPLY to disk under full complex-route enforcement.** `tests/governance/intake/sensors/test_test_failure_sensor_dedup.py` (4,986 bytes) written by the ChangeEngine after: sensor detected the backlog task → router classified `complex` → attempt 1 ledger rejected at `score=0.00` → retry injected sharpened feedback → attempt 2 scored `11.00` at 4 categories would_pass=True → Iron Gate ASCII auto-repair on secondary paths → GATE can_write allowed → APPROVE auto-approved (headless bypass) → ChangeEngine `RollbackArtifact.capture()` handled the new-file path (fix from commit `28d52274ec`) → `DECISION outcome=applied reason_code=safe_auto_passed` → VERIFY found test critiques → L2 Repair iteration 1/5 CONVERGED → second DECISION applied → POSTMORTEM root_cause=none. Total wall time ~16m45s, cost $0.55/$0.60. Session O closed an 8-session arc (H → O) that surfaced 6 additional distinct failure modes beyond the Session A–G scoring work, each masked by the one above it: H=`prompt_async [Errno 22]` headless TTY crash (fixed in `d8c1cb4d30`), I=`too_many_files` risk escalation (env override), J=`L3 READ_ONLY_PLANNING` mode switch on ambient probe failures (fixed in `5d169266d6` making thresholds env-tunable), K=`RollbackArtifact [Errno 2]` new-file path (fixed in `28d52274ec`), L=900s pool ceiling crossed by ambient load (`JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S=1800` env), M=180s fallback cap clipped 5-tool-round retry synthesis (`JARVIS_FALLBACK_MAX_TIMEOUT_COMPLEX_S=360` env), N=intake WAL cross-session coalescing merged stale envelopes with new tasks (`rm .jarvis/intake_wal.jsonl` + `rm .jarvis/intake_router.lock` state cleanup before boot). Only 1 of 4 target files landed on disk — by design, since the winning candidate didn't populate a `files: [...]` list, so the `_apply_multi_file_candidate` fan-out path was not taken. Multi-file candidate enforcement is the next architectural item. Commit `a5f36482fa` added an APPLY-time debug log (`[Orchestrator] APPLY mode=(single|multi) candidate_files=N files_list_present=... multi_enabled=... targets=[...]`) so future sessions can grep one line instead of cross-referencing diff_summary against disk state. Full postmortem with quoted terminal log lines in OUROBOROS.md breakthrough log.

**2026-04-15 (Sessions Q–S arc, `bt-2026-04-15-204901`)** — **Multi-file `files: [...]` enforcement proven deterministic through every gate.** Session O's "1-of-4 files landed" gap had two halves: enforcement (prompt + parser + coverage gate) and persistence (APPLY fan-out). This arc closes the enforcement half and isolates the persistence half as a separate reliability track. Shipped: (1) `multi_file_coverage_gate.py` as Iron Gate 5 with 31 unit tests (commit `31504a8f12`), (2) `providers.py _build_multi_file_contract_block` prompt hint injecting the `files: [{file_path, full_content, rationale}, ...]` contract when `len(ctx.target_files) > 1`, (3) `providers.py _parse_generation_response` multi-file-shape detection — `file_path`/`full_content` synthesized from `files[0]` when `files: [...]` is populated so downstream consumers keep working, (4) `provider_exhaustion_watcher.py` per-op dedup (commit `37a371e65d`) with 9 unit tests so one op's retries don't stack on the hibernation threshold. Three-session verification arc: **Q** (`bt-2026-04-15-201035`) — original bug isolated, `schema_invalid:candidate_0_missing_file_path` on multi-file candidates, per-op dedup proven in production with interleaved IMMEDIATE success triggering `counted_ops=1` reset; **R** (`bt-2026-04-15-203724`) — parser fix verified, 4-file candidate passed parser at `cost=$0.1642 117.8s`, died at Iron Gate 1 (exploration) with 0 fresh tool calls, not a multi-file issue; **S** (`bt-2026-04-15-204901`) — `JARVIS_EXPLORATION_GATE=false` to exercise Gate 5, model round-0 fired `3 parallel read_file` calls unprompted, GENERATE `91.3s $0.2085`, ASCII auto-repair healed 2 codepoints, **zero `multi_file_coverage` rejections anywhere in the log** (Gate 5 silently passed → all 4 paths covered), `LSP found 1 type errors in [dedup.py, ttl.py, isolation.py]` (3 of 4 files had LSP errors, marker_refresh clean), `TestRunner Resolved 45 test targets for 4 changed files` — **all 4 target paths visible to the post-gate pipeline**. Persistence not proven: VALIDATE's type error on `dedup.py` routed to `VALIDATE_RETRY → L2 Repair`, which never converged before the 10-minute idle timeout (`pytest timed out after 30.0s`, L2 iteration `49s elapsed, 11s remaining` on the 60s timebox). 0 of 4 files landed. **This is a VALIDATE/L2 timebox issue orthogonal to multi-file enforcement** and is tracked as Follow-up A in the OUROBOROS breakthrough log — falsifiable hypothesis: raise `JARVIS_TEST_TIMEOUT_S` to 120 and verify L2 iteration budget is ≥ N_iters × pytest_timeout + overhead, success criterion is one op (any N ≥ 2) reaching `APPLY mode=multi + DECISION applied + POSTMORTEM root_cause=none` without idle timeout. Full postmortem in OUROBOROS.md breakthrough log.
