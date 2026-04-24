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
- **Phase B subagents — COMPLETE 2026-04-20** (all 4 graduated, 138 regression tests green): EXPLORE, REVIEW (`JARVIS_REVIEW_SUBAGENT_SHADOW=true` default), PLAN (shadow + parallel-edge default-on, 4× wall-clock on multi-file), GENERAL (infrastructure-only with Semantic Firewall §5). Phase C Slices 1a+1b + Epochs 1+2 landed same day — cage absolute. Details: `memory/project_phase_b_subagent_roadmap.md`, `project_phase_b_step2_deferred.md`, `project_phase_c_general_llm_driver.md`.
- **GENERAL Semantic Firewall + mutation cage** (`semantic_firewall.py` + `scoped_tool_backend.py` + `dispatch_general`): 11 injection detectors, 5 credential shapes, recursion ban, output quarantine fence, hard-kill wrapper. `ScopedToolBackend` carries per-instance mutation counter + budget as structural COUNT gate (second-rejection after type gate, `POLICY_DENIED reason=mutation_budget_exhausted`). `state_mirror` dict preserves exec_trace across hard-kill. LLM driver gated by `JARVIS_GENERAL_LLM_DRIVER_ENABLED` (default **true** post 2026-04-20).
- **Gap #4 campaign — CLOSED 2026-04-20** (9 sensors event-primary, 4 polling-by-design): webhook transport for GitHub/CI sensors; `TrinityEventBus` `fs.changed.*` for FS sensors (TestFailure/TodoScanner/Backlog/OpportunityMiner w/ layered storm-guard); conversation-bus for IntentDiscovery. Retained-on-polling (architectural): RuntimeHealth/WebIntelligence/ProactiveExploration/Scheduled. `EventChannelServer` multi-surface (`/webhook/github`, `/webhook/ci`); `/channel/health` per-sensor telemetry. ~120 tests. Details: `memory/project_gap_4_*.md`.
- **SerpentFlow** (`battle_test/serpent_flow.py`, 1900+ lines): CC-style flowing CLI with `Update(path)` blocks, numbered diffs, per-op reasoning
- **LiveDashboard** (`battle_test/live_dashboard.py`, 1233 lines): Persistent Rich TUI with 3-channel terminal muting
- **Venom** (`tool_executor.py`): Multi-turn agentic tool loop -- 16 built-in tools + MCP external tools. Built-in: read_file, search_code, edit_file, write_file, bash, web_fetch, web_search, run_tests, get_callers, glob_files, list_dir, list_symbols, git_log, git_diff, git_blame, ask_human. MCP tools from external servers discovered at prompt time and forwarded (Gap #7). Live context auto-compaction between rounds (Gap #8).
- **L2 Repair** (`repair_engine.py`): Iterative self-repair FSM (5 iterations, 120s timebox). **Enabled by default** (`JARVIS_L2_ENABLED=true`) — engages when VALIDATE exhausts retries, closes the Ouroboros cycle per Manifesto §6.
- **Iron Gate** (orchestrator post-GENERATE): Two deterministic gates routed through the GENERATE retry loop. (1) Exploration-first (`JARVIS_EXPLORATION_GATE`): min 2 `read_file`/`search_code`/`get_callers` before any patch. `JARVIS_EXPLORATION_LEDGER_ENABLED=true` switches to diversity-weighted scoring across 5 categories with env-tunable per-complexity floors; insufficient → `ExplorationInsufficientError` + category-aware retry feedback. (2) ASCII-strictness (`JARVIS_ASCII_GATE`): rejects non-ASCII codepoints. Manifesto §6.
- **SemanticGuardian** (`semantic_guardian.py`, post-VALIDATE / pre-GATE): Deterministic pre-APPLY pattern detector, 10 AST/regex patterns, ~10ms, zero LLM. Hard findings (removed_import_still_referenced, function_body_collapsed, credential_shape_introduced, test_assertion_inverted, permission_loosened) → APPROVAL_REQUIRED; soft findings → NOTIFY_APPLY. Per-pattern + master kill switches (`JARVIS_SEMGUARD_<PATTERN>_ENABLED` / `JARVIS_SEMANTIC_GUARD_ENABLED`). Structured telemetry `[SemanticGuard] op=X findings=N ...`. Raises friction, doesn't replace proof — VALIDATE + Iron Gate + tests remain authoritative. 47 regression cases.
- **Risk-tier floor** (`risk_tier_floor.py`): Three composing env knobs — strictest wins. `JARVIS_MIN_RISK_TIER={safe_auto|notify_apply|approval_required}`, `JARVIS_PARANOIA_MODE=1` (shortcut for notify_apply), `JARVIS_AUTO_APPLY_QUIET_HOURS=<start>-<end>` in `JARVIS_AUTO_APPLY_QUIET_HOURS_TZ` (IANA, defaults UTC; wrap-around supported). 43 regression cases incl. DST-correct TZ math.
- **Multi-file coordinated generation** (orchestrator.py `_iter_candidate_files`/`_apply_multi_file_candidate`): Candidates may return a `files: [{file_path, full_content, rationale}, ...]` list in addition to the legacy single `file_path`/`full_content` pair. Every file is AST/placeholder-validated at the parser, and the APPLY path composes per-file `ChangeEngine.execute` calls with **batch-level rollback** — if file N fails, files 1..N-1 are restored from pre-apply snapshots (new files are unlinked). Preserves the 8-phase guarantees per file while adding atomic multi-file semantics. Master switch: `JARVIS_MULTI_FILE_GEN_ENABLED` (default `true`).
- **ConsciousnessBridge** (`consciousness_bridge.py`): Injects memory/prediction into pipeline
- **StrategicDirection** (`strategic_direction.py`): Manifesto principles injected into every generation prompt. Infers recent momentum from last 50 `git log` commits (scope/type histograms + 3 latest subjects) into a "Recent Development Momentum" digest section. Gated by `JARVIS_STRATEGIC_GIT_HISTORY_ENABLED` (default `true`). Additively appends `## Current Strategic Posture` block from DirectionInferrer reading. Manifesto §4.
- **DirectionInferrer + StrategicPosture** (`direction_inferrer.py`, `posture*.py`): Deterministic signal→posture metacognition. 4-value vocabulary (`EXPLORE`/`CONSOLIDATE`/`HARDEN`/`MAINTAIN`) from 12 ambient signals, §5 Tier 0, zero LLM. `PostureObserver` 300s cadence + 900s hysteresis + 0.75 confidence bypass + overrides clamped to `JARVIS_POSTURE_OVERRIDE_MAX_H` (24h). Durable triplet at `.jarvis/posture_{current,history,audit}.jsonl` — audit file is append-only per §8. Four surfaces: CONTEXT_EXPANSION injection, `/posture` REPL (override is only write-surface, doesn't bypass Iron Gate/SemanticGuardian/risk-tier), `GET /observability/posture{,/history}`, SSE `posture_changed`. **Graduated 2026-04-21** — `JARVIS_DIRECTION_INFERRER_ENABLED` default `true`. Wave 1 #1. Details: `memory/project_direction_inferrer_graduation.md`.
- **SensorGovernor + MemoryPressureGate** (`sensor_governor.py` + `memory_pressure_gate.py`): Global op-emission cap across 16 sensors weighted by posture + advisory memory-pressure gate for L3 fan-out. Rolling window, 200 global ops/hour, weighted cap = `base × posture_weight × urgency_multiplier`. Emergency brake at `cost_burn>0.9` OR `postmortem_rate>0.6` → 20% caps. `MemoryPressureGate` stdlib probe cascade (psutil → meminfo → vm_stat) with 4-level enum + per-level fanout caps. Four surfaces: `/governor` REPL, `GET /observability/governor{,/history,/memory-pressure}`, SSE `governor_throttle_applied`/`governor_emergency_brake`/`memory_pressure_changed`. **Graduated 2026-04-21** — both `JARVIS_SENSOR_GOVERNOR_ENABLED` + `JARVIS_MEMORY_PRESSURE_GATE_ENABLED` default `true`. Consumed posture (Wave 1 #1) and auto-registers 13 flags in FlagRegistry (Wave 1 #2). Wave 1 #3. Slice 5 Arc A wired intake shadow-consult; Arc B wired L3 subagent_scheduler direct-enforce (both landed 2026-04-21). Details: `memory/project_sensor_governor_graduation.md`, `project_slice5_arc_{a,b}.md`.
- **FlagRegistry + /help dispatcher** (`flag_registry.py` + `help_dispatcher.py`): Typed registry for 481+ `JARVIS_*` env flags (52 curated seed), each with type + category (8 slots) + source_file + example + posture-relevance (CRITICAL/RELEVANT/IGNORED). First Wave 1 #1 consumer via relevance filtering. Bounded Levenshtein typo detection (`JARVIS_POSTUR_ENABLED` → suggests `JARVIS_POSTURE_ENABLED`). Surfaces: `/help {verbs,flags,flag,unregistered,category,posture,stats}` REPL, `GET /observability/{flags,verbs}`, SSE `flag_typo_detected`/`flag_registered`. **Graduated 2026-04-21** — `JARVIS_FLAG_REGISTRY_ENABLED` default `true`; registry data structure stays alive master-off (descriptive, not authoritative). `/help help` works master-off (discoverability). Wave 1 #2. Details: `memory/project_flag_registry_graduation.md`.
- **AutoCommitter** (`auto_committer.py`): Structured git commits with O+V signature after APPLY+VERIFY. Conventional commit format, risk-tier metadata, protected-branch push prevention.
- **OrangePRReviewer** (`orange_pr_reviewer.py`): Async-review path for Orange-tier (`APPROVAL_REQUIRED`). Creates a `ouroboros/review/{op-id}` branch, commits candidate, pushes, files PR via `gh pr create` with evidence + checklist. Loop continues; human reviews async. Opt-in via `JARVIS_ORANGE_PR_ENABLED` (default `false`); falls back to CLI approval on any failure. Manifesto §7.
- **IDEObservabilityRouter** (`ide_observability.py`, Gap #6 Slice 1): Read-only GETs on `EventChannelServer`: `/observability/{health,tasks,tasks/{op_id}}` (bounded projection). Loopback-only + 120/min/IP rate limit + CORS allowlist + `schema_version: "1.0"` + `Cache-Control: no-store`. Authority invariant grep-enforced (no orchestrator/policy/iron_gate imports). `JARVIS_IDE_OBSERVABILITY_ENABLED` graduated default `true` (2026-04-20). 41 tests.
- **IDEStreamRouter + StreamEventBroker** (`ide_observability_stream.py`, Gap #6 Slice 2): SSE at `GET /observability/stream`, unidirectional. 10 event types. Bounded: 8 subs × 64 queue × 512 history × 15s heartbeat. Drop-oldest → single `stream_lag` per window. `Last-Event-ID` replay. task_tool publish hooks best-effort. `JARVIS_IDE_STREAM_ENABLED` graduated default `true` (2026-04-20). 49 tests. Details: `memory/project_gap_6_*.md`.
- **VS Code/Cursor/Sublime/JetBrains extensions** (`extensions/{vscode,sublime,jetbrains}-jarvis/`, Gap #6 Slices 3/5/6): Read-only consumers of Slice 1 GET + Slice 2 SSE. Tree view + detail webview (CSP-hard) + status bar + command palette. Cursor zero-change (VS Code fork, `vscode-webview://` CORS matches). Native fetch SSE parser, exp-backoff+jitter reconnect, bounded LRU, poll fallback. 35 TS + 27 Sublime + 28 Kotlin tests.
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
- **L3 worktree isolation** (`subagent_scheduler.py` + `worktree_manager.py`): Enabled by default (`JARVIS_GOVERNED_L3_ENABLED=true`). Parallel execution graphs use isolated git worktrees (COW via `git worktree add -b`, no copy/venv warmup) to prevent filesystem conflicts between parallel units. **Manifesto §1 Boundary + §6 Iron Gate**: if isolation was promised and `create()` fails (branch collision, disk full, permission denied), the unit returns `WorkUnitResult(FAILED, failure_class="infra", error="worktree_create_failed:<type>:<msg>")` — **no silent fallback to the shared tree**. Generator is never reached. **§2 Progressive Awakening**: `WorktreeManager.reap_orphans()` sweeps on boot (`JARVIS_WORKTREE_REAP_ORPHANS=true`, default `true`) — registered `unit-*` worktrees, unregistered on-disk `unit-*` dirs under `worktree_base`, and dangling `unit-*` branches (prevents "branch already exists" on next submit), followed by `git worktree prune`. Recovers from SIGKILL/OOM/power-loss leftovers; the `finally`-block cleanup covers normal exits. Regression spine: `tests/governance/test_worktree_isolation.py` (10 tests), `tests/governance/autonomy/test_subagent_executor_worktree.py` (2 tests).
- **Auto-commit post-APPLY**: AutoCommitter creates commits with O+V signature after VERIFY passes. Conventional type/scope inference, risk-tier metadata, protected-branch push prevention. Master switch: `JARVIS_AUTO_COMMIT_ENABLED`.
- **MCP tool forwarding** (Gap #7): External MCP tools injected into generation prompt; model calls `mcp_{server}_{tool}` via tool loop. Policy auto-allows MCP; external servers own auth.
- **Live context auto-compaction**: When tool loop prompt exceeds 75% of budget, older tool results are compacted into a deterministic summary (Gap #8). Preserves recent 6 chunks. No model inference. Env: `JARVIS_TOOL_LOOP_COMPACT_THRESHOLD`.
- **UserPreferenceMemory** (`user_preference_memory.py`): Persistent typed memory (6 kinds: `USER`/`FEEDBACK`/`PROJECT`/`REFERENCE`/`FORBIDDEN_PATH`/`STYLE`) at `.jarvis/user_preferences/`. Three integration points: StrategicDirection prompt injection, ToolExecutor FORBIDDEN_PATH hook, post-rejection auto-extraction.
- **LastSessionSummary** (`last_session_summary.py`, v1.1a): Read-only parse of most recent `summary.json` → one-liner digest with `apply=MODE/N verify=P/T commit=HASH[:10]` tokens. Authority-free. Env: `JARVIS_LAST_SESSION_SUMMARY_ENABLED` (default `false`).
- **SemanticIndex** (`semantic_index.py`, v0.1 + v1.0 Slices 3a+3c): Recency-weighted centroid over commits + goals + conversation (3d halflife conversation, 14d commits/goals). POSTMORTEM excluded from centroid (failure-gravity avoidance). Local fastembed + bge-small-en-v1.5. Two consumers: intake priority bias + CONTEXT_EXPANSION prompt — authority-free. v1.0 adds hand-rolled NumPy k-means + auto-K silhouette + cluster-kind classifier + themed prompt rendering (`cluster_mode=kmeans` default `centroid`). Cache: `.jarvis/semantic_index.npz`. Env: `JARVIS_SEMANTIC_INFERENCE_ENABLED` (default `false`) + cluster knobs per `memory/project_phase_c_semantic_index_v1.md`.
- **ConversationBridge** (`conversation_bridge.py`, v1.1): Sanitized bounded channel from agentic dialogue into CONTEXT_EXPANSION. In-process ring buffer, Tier -1 sanitizer, 5 signal sources (`tui_user`, `ask_human_q`+`_a`, `postmortem`, `voice`-reserved), authority-free. Master switch `JARVIS_CONVERSATION_BRIDGE_ENABLED` (default `false`).
- **VisionSensor** (`intake/sensors/vision_sensor.py`): Read-only Ferrari frame consumer. Tier 1 regex + Tier 2 VLM (Qwen3-VL-235B). Hot path: dhash dedup → app denylist → OCR → credential-regex → Tier 1 → cooldown → Tier 2 → sanitize → schema v1 envelope. Policy: 20-op FP budget auto-pause, 120s finding cooldown, chain cap 1→3. Cost ledger: $1 daily cap, 3-step cascade. Structural invariants: no-capture-authority (AST-enforced), export-ban on `ctx.attachments`, NOTIFY_APPLY risk floor.
- **Multi-modal ingest** (`ctx.attachments` → Claude/DW GENERATE): Two paths (VisionSensor autonomous + SerpentFlow `/attach` human-initiated) converge at `unified_intake_router` hoist → `Attachment(kind=...)`. `providers.py::_serialize_attachments()` emits native Claude image/document blocks or OpenAI-compat `image_url` blocks for DW. Validates path + extension + mime + 10MiB cap + sha256[:8] hash. BG/SPEC routes strip attachments. Master: `JARVIS_GENERATE_ATTACHMENTS_ENABLED`.
- **Visual VERIFY** (`visual_verify.py`, Slices 3-4): Post-APPLY pre-COMPLETE UI check. 3-tier trigger (target_files glob / plan ui_affected / risk-based fallback). Deterministic battery (first-miss-wins: app_crashed / blank_screen / hash_unchanged / hash_scrambled). TestRunner-red clamps a pass to fail (asymmetric). Model-assisted advisory via injectable VLM + AdvisoryLedger (verdict + reasoning_hash only). Auto-demotion at ≥50% post-graduation FP.

## Battle Test

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Boots the full 6-layer stack: GovernedLoopService, IntakeLayer (16 sensors), TrinityConsciousness, StrategicDirection, CommProtocol, SerpentFlow CLI.

**`--max-wall-seconds SEC`** (env `OUROBOROS_BATTLE_MAX_WALL_SECONDS`, Ticket A1 Guard 2, 2026-04-23): hard wall-clock ceiling on total session duration — fires `stop_reason=wall_clock_cap` when exceeded. Opaque to any activity signal so it cannot be defeated by provider retry storms that reset `--idle-timeout`'s per-op liveness counter (the failure mode that hung #7 GENERATE S2 for 67+ min). `0` or unset disables the cap (legacy 3-way race: shutdown / budget / idle). **Graduation soaks MUST set this** — typical value `2400` (40 min) for ~2× safety margin over the 850–1300s happy-path. Treated equivalent to `idle_timeout` for clean-bar purposes in the Wave 2 (5) graduation matrix harness-class footnote.

**`--headless`** (env `OUROBOROS_BATTLE_HEADLESS`, Ticket C, 2026-04-23): skips the `SerpentREPL` input task. Agent-conducted soaks + CI + daemons should set this (or rely on auto-detect via `not sys.stdin.isatty()`). Replaces the deprecated `tail -f /dev/null | python3 ...` stdin-guard idiom. `--no-headless` is the escape hatch to force interactive REPL when stdin isn't a TTY (rare).

**Signal handling** (Ticket B, 2026-04-23): harness installs SIGHUP / SIGINT / SIGTERM handlers + ignores SIGPIPE. On any of those signals it writes a partial `summary.json` with `session_outcome=incomplete_kill` + signal-specific `stop_reason` (sighup / sigterm / sigint) before the shutdown event fires — so externally-killed sessions leave auditable artifacts, not just `debug.log`. The clean `_generate_report` path stamps `session_outcome=complete` on the full summary. Schema is v1.1b (additive, `schema_version` stays 2).

On startup, the harness auto-reaps any zombie `ouroboros_battle_test.py` processes from earlier crashed sessions (psutil-based, strict path-tail match, SIGTERM → SIGKILL escalation) and removes stale `.jarvis/intake_router.lock` files whose owning PID is dead. Prevents budget competition between sessions. Master switch: `JARVIS_BATTLE_REAP_ZOMBIES` (default `true`).

Partial-shutdown insurance: the harness registers an `atexit` fallback **and** a sync signal-handler write so every session dir ends up with a v1.1a-parseable `summary.json` — even when SIGTERM arrives mid-cleanup or the async finally can't complete. `SIGKILL` remains unrecoverable by design (OS-level, uncatchable in Python). Regression spine for "session continuity + aborted runs": `tests/governance/test_last_session_summary_composition.py` (proves production injection path wires LSS tokens into the composed CONTEXT_EXPANSION prompt) + `tests/battle_test/test_harness_partial_shutdown.py` (proves partial summaries land on every reachable exit path and are LSS-parseable on the next boot).

Operator-visible UX (Rich): the GENERATE token stream (`stream_renderer.py`) and the NOTIFY_APPLY rich diff preview (`diff_preview.py`) both require a real interactive TTY — headless / sandbox / CI runs always fall through to the plain (spinner-and-sleep) paths, so visual verification of these features is a local interactive battle test, not a background run.

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
    event_channel.py            # Webhook receiver (DW + GitHub + CI) + /observability/* (Gap #6)
    ide_observability.py        # Read-only GET /observability/{health,tasks,tasks/{op_id}} — loopback-only, rate-limited, authority-free (Gap #6 Slice 1)
    ide_observability_stream.py # SSE /observability/stream — broker + history replay + drop-oldest backpressure + heartbeat (Gap #6 Slice 2)
    repair_engine.py            # L2 self-repair
    consciousness_bridge.py     # Consciousness integration
    strategic_direction.py      # Manifesto injection
    user_preference_memory.py   # Persistent typed memory across sessions (Task #195)
    conversation_bridge.py      # TUI dialogue → CONTEXT_EXPANSION (v1.1, Tier -1 sanitized)
    semantic_index.py           # Recency-weighted semantic centroid + cosine scoring (v0.1)
    last_session_summary.py     # Read-only session-to-session continuity from summary.json (v1.1a)
    ops_digest_observer.py      # Stable observer protocol for APPLY/VERIFY/commit telemetry (v1.1a)
    visual_verify.py            # Post-APPLY deterministic + advisory VERIFY (Slices 3-4)
    vision_repl.py              # /vision status|resume|boost handlers + dashboard + origin tag (Task 21)
    serpent_animation.py        # ASCII animation
    intake/
      sensors/                  # 17 sensors (VisionSensor added — Slices 1-2)
        vision_sensor.py        # Read-only Ferrari consumer with Tier 0/1/2 cascade + policy layer
    intent/
      test_watcher.py           # Pytest polling (30s timeout)
      signals.py                # IntentSignal dataclass + SignalSource enum + VisionSignalEvidence schema v1
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

- **2026-04-11 (`bt-2026-04-11-154947`)** — First sustained full-pipeline completion post-Iron Gate tightening. `op-019d7d3e` traversed CLASSIFY → IRON_GATE_REJECT → REGENERATE → APPLY → VERIFY → L2 → POSTMORTEM autonomously. `dependency_file_integrity` gate caught hallucinated `anthropic → anthropichttp` rename. Unblocker: captured-client race fix in `providers.py` re-acquires `self._client` on every `_call_with_backoff` retry.
- **2026-04-12 (`bt-2026-04-12-073546`)** — IMMEDIATE thinking cap + DurableJSONL sandbox fix. 5 validated fixes: fallback_concurrency=3, outer-gate grace 5s→15s, async sensor scans, IMMEDIATE thinking disabled (first_token 94.5s→961ms, 98x), DurableJSONL sandbox_fallback. TestRunner sandbox path bug fixed (commit `22f297d`).
- **2026-04-14 (Sessions A–G)** — `ExplorationLedger` enforcement validated. Session G: retry feedback → model diversifies to `get_callers`/`git_blame`/`search_code` → `would_pass=True score=25.50` across 5 categories. Commits `614009ec05`/`db13f045ce`/`4f60a584f9`/`ad05fb7c7e`/`5d169266d6`.
- **2026-04-15 (Session O, `bt-2026-04-15-175547`)** — **First end-to-end autonomous APPLY to disk under full complex-route enforcement.** `test_test_failure_sensor_dedup.py` (4 986 bytes) written after attempt 2 scored `11.00`/4 categories → ChangeEngine → L2 iter 1 CONVERGED → POSTMORTEM root_cause=none. ~16m45s, $0.55/$0.60. Closed an 8-session arc (H–O) surfacing 6 additional failure modes: headless TTY crash (`d8c1cb4d30`), risk escalation, L3 planning-mode switch (`5d169266d6`), RollbackArtifact new-file path (`28d52274ec`), pool ceiling (`JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S=1800`), fallback cap (`…_FALLBACK_MAX_TIMEOUT_COMPLEX_S=360`), intake WAL cross-session coalescing (state cleanup). 1-of-4 files landed — multi-file fan-out gap.
- **2026-04-15 (Sessions Q–S, `bt-2026-04-15-204901`)** — Multi-file `files: [...]` enforcement proven through every gate. Shipped: `multi_file_coverage_gate.py` as Iron Gate 5 (commit `31504a8f12`, 31 tests), `_build_multi_file_contract_block` prompt hint, `_parse_generation_response` multi-file shape detection, `provider_exhaustion_watcher.py` per-op dedup (`37a371e65d`). Session S: Gate 5 silently passed all 4 paths, LSP+TestRunner saw all 4. Persistence blocked by L2 timebox → tracked as Follow-up A.
- **2026-04-15 (Sessions U–W, `bt-2026-04-15-230849`)** — **First end-to-end autonomous multi-file APPLY to disk.** `op-019d9368-654b` generated 4 test modules → L2 iter 1 (50s) → `APPLY mode=multi candidate_files=4` → 4 `::NN` sub-op DECISIONs applied → 4 POSTMORTEMs root_cause=none → AutoCommitter commit `0890a7b6f0`. Post-hoc pytest: 20/20 pass in 2.28s. Key fixes: FSM instrumentation (`d6aa78c8ba`) exposed re-validation flakiness (iter=0 'test' vs iter=1 'infra' flake), `JARVIS_MAX_VALIDATE_RETRIES` env factory (`58709f27de`) bypasses it, L2 deadline reconciliation (`53e6bd9f76`) — fresh `now + JARVIS_L2_TIMEBOX_S` at dispatch, `ctx.pipeline_deadline` reconciled upward. Not yet graduated (§6 needs 3 consecutive successes); 3 deferred latent bugs as workarounds (re-validation infra flake, cost_governor ctx staleness, hardcoded 90s wait_for).
- **2026-04-19→20 (Multi-Modal Ingest arc, 12 commits)** — CC-parity for "see a screenshot/PDF of a bug and describe it" closed end-to-end with live Anthropic API proof. VisionSensor autonomous path graduated (4 schema contract fixes, OCR adapter, event-loop-starvation fix); `/attach <path>` REPL command + envelope schema relaxation for user_attachments. Live-fire proof: 701-byte PDF → Claude `2b.1-noop` with verbatim quote of embedded string. Details: `memory/project_vision_sensor_verify_arc.md`.
- **2026-04-20 (Phase C — GENERAL cage sealed, 4 slices)** — Slice 1a+1b: LLM driver graduated (`JARVIS_GENERAL_LLM_DRIVER_ENABLED=true` default), live 3-test matrix vs real Claude API proved allowlist + scope + mutation cap. Epoch 1: `max_mutations` structural COUNT gate (not cooperative) via `ScopedToolBackend._mutations_count` — 2nd rejection layer returns `POLICY_DENIED reason=mutation_budget_exhausted`. Epoch 2: hard-kill records preservation via shared `state_mirror` dict — `tool_calls_made` / `mutations_count` / `mutation_records` / `call_records` / `tool_names` all survive cancellation. Uniform `_build_partial_trace` → exec_trace shape identical across 8 exit statuses. **111/111 GENERAL tests green**. Details: `memory/project_phase_c_general_llm_driver.md`, `memory/project_phase_b_step2_deferred.md`.
- **2026-04-20 (Gap #5 — CLOSED, 4-slice arc)** — Structured to-do lists. Slice 4 flipped `JARVIS_TOOL_TASK_BOARD_ENABLED` false→true. 17 graduation pins. Live-fire proof `livefire-gap5-1776743088` (6 Venom tool calls ALLOWED+SUCCESS with zero env overrides, 8 audit lines captured). Manifest caps still empty; tools still NOT in `_MUTATION_TOOLS`. **107/107 tests green**. Details: `memory/project_gap_5_*.md`.
- **2026-04-20 (Gap #4 — CLOSED per Reading A)** — CC-parity stdout event streaming. 4-slice arc: BackgroundMonitor primitive + Venom `monitor` tool (read-only, binary allowlist) + TestRunner streaming + Slice 4 graduation (both `JARVIS_TOOL_MONITOR_ENABLED` + `JARVIS_TEST_RUNNER_STREAMING_ENABLED` flipped default `false`→`true`). Graduation does NOT escalate authority. Reading B (full subprocess sweep) OUT OF SCOPE. **111/111 tests green**. Details: `memory/project_ticket_4_*.md`.
- **2026-04-20 (Phase C Epoch 3 — Semantic Index v1.0 GRADUATED)** — Slices 3a+3c+3b+3d. Hand-rolled NumPy k-means + auto-K silhouette + cluster-kind classifier (`goal`/`conversation`/`postmortem`/`mixed`). Themed prompt sections. `CLUSTER_SCORING_POLICY={"centroid"|"max_cluster"}` with zero-boost-with-evidence for postmortem winners. Slice 3d graduated both defaults: `CLUSTER_MODE=kmeans`, `CLUSTER_SCORING_POLICY=max_cluster`. Clustering + policy remain advisory. **126/126 tests green**. Details: `memory/project_phase_c_semantic_index_v1.md`.
- **2026-04-20 → 2026-04-21 (Gap #6 — CLOSED, 7-slice arc)** — IDE integration. Server (Slices 1/2/4): `/observability/{health,tasks,tasks/{op_id}}` + SSE `/observability/stream` (10-event vocab, 8×64×512 broker, drop-oldest, `Last-Event-ID` replay, heartbeat, loopback-only, rate/CORS, grep-pinned authority). Both env flags graduated default `true` with 18 pins. Three clients (Slices 3/5/6): `extensions/vscode-jarvis/` TypeScript (35 tests, Cursor zero-change), `extensions/sublime-jarvis/` stdlib Python 3.8 (27 tests), `extensions/jetbrains-jarvis/` Kotlin + Gradle + IntelliJ SDK (28 tests). Slice 7 live-fire (`scripts/livefire_gap6.py`) boots server + raw-socket SSE + 4 TaskBoard transitions observed. **216 checks total**. Details: `memory/project_gap_6_*.md`.
- **2026-04-21 (Problem #7 — CLOSED, 5-slice arc)** — Plan mode as distinct operator modality. `PlanGenerator` was already a phase; orchestrator already had a plan-approval hard gate for complex ops; the gap was "operator can't say halt every op for my review." 5-slice arc: Slice 1 `PlanApprovalController` (per-op pending registry, Future-backed resolution, state machine pending→approved/rejected/expired, bounded timeouts/capacity/reason-len, listener hooks). Slice 2 `PlanApprovalProviderAdapter` (implements orchestrator's existing `ApprovalProvider` protocol — `request_plan`/`approve`/`reject`/`await_decision` — so it's drop-in) + `should_force_plan_review()` OR-in for the existing `_should_gate` predicate. Slice 3 `/plan` REPL dispatcher (`mode [on|off]`/`pending`/`show`/`approve`/`reject`/`history`/`help`) with `render_plan_detail` exported for reuse. Slice 4 IDE observability — `GET /observability/plans{,/<op_id>}` + 4 new SSE event types (`plan_pending`/`plan_approved`/`plan_rejected`/`plan_expired`) + `bridge_plan_approval_to_broker` one-call adapter (whitelist-guarded, summary-only SSE payload, full JSON at the GET endpoint). Slice 5 graduation keeps default `JARVIS_PLAN_APPROVAL_MODE` **false** deliberately — turning it on halts every op, so it's an operator choice not a default posture; 12 graduation pins (docstring bit-rot guard, authority invariants, 4 event-type constants, 6 REPL subcommands, 2 GET routes, full revert matrix, per-op override). **112/112 tests green**. Details: `memory/project_problem_7_plan_approval.md`.
