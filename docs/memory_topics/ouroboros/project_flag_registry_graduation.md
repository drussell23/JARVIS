---
title: FlagRegistry + /help Arc — CLOSED
modules: [scripts/audit_env_flags.py]
status: merged
source: project_flag_registry_graduation.md
---

# FlagRegistry + /help Arc — CLOSED

**Graduated 2026-04-21.** Wave 1 #2 of the A-level-execution roadmap is live.

## What graduated

`JARVIS_FLAG_REGISTRY_ENABLED` default flipped **`false` → `true`** in `flag_registry.py::is_enabled()`. All surfaces now active with zero env setup:

1. **`/help` REPL** — 9 subcommands (top-index, verbs, `<verb>` delegation, flags with `--category|--posture|--search|--limit`, flag `<NAME>`, category/posture aliases, unregistered typo-hunter, stats, help)
2. **IDE GET** — `/observability/flags{,/{name},/unregistered}` + `/observability/verbs` (loopback + rate-limit + CORS + schema_version=1.0 + `Cache-Control: no-store`, double-gated)
3. **SSE** — `flag_typo_detected` + `flag_registered` event types; `publish_flag_typo_event` + `publish_flag_registered_event` best-effort helpers; `bridge_flag_registry_to_broker` monkey-patches `registry.register` to publish net-new specs only (override-in-place does NOT fire)
4. **Typo warnings** — `FlagRegistry.report_typos()` logs + publishes SSE per unique typo per process

Explicit `JARVIS_FLAG_REGISTRY_ENABLED=false` reverts all four surfaces in lockstep (proven in live-fire revert matrix). **Registry data structure stays alive** when flag is off — it's descriptive, not authoritative; only operator-facing surfaces are gated. `/help help` still works master-off for discoverability.

## Commits

```
4524e72bc6 Slice 1 — FlagRegistry primitive + 52-flag seed (64 tests, 26 live-fire)
c8b87d1427 Slice 2 — /help dispatcher + VerbRegistry (44 tests, 36 live-fire)
14c02530b3 Slice 3 — GET /observability/flags + SSE flag events (27 tests, 35 live-fire)
<pending>   Slice 4 — graduation + 38 pins (32 live-fire)
```

## Final numbers

| Dimension | Count |
|---|---|
| Python test files | 4 (flag_registry / help_dispatcher / flag_observability / graduation) |
| Tests green | **173/173** combined |
| Live-fire scripts | 4, all PASS |
| Live-fire checks | 26 + 36 + 35 + 32 = **129 total** |
| Graduation pins | 38 (6 authority + 10 behavioral + 9 graduation-specific + 3 schema + 3 integration + 2 full-revert + 3 docstring + 2 CLAUDE.md = 38) |
| LoC new | ~2200 Python (primitive + seed + dispatcher) |
| LoC integration | ~240 (ide_observability + ide_observability_stream extensions) |
| Authority files grep-pinned | 3 arc files + 4 GET handler methods + 3 SSE helpers |
| Seeded flags | **52** across 8 categories |
| Commits | 4 |

## First real downstream consumer of Wave 1 #1

Live-fire on real repo: `/help flags --posture HARDEN` returns 10 HARDEN-critical flags:

```
JARVIS_ASCII_GATE, JARVIS_DIRECTION_INFERRER_ENABLED, JARVIS_EXPLORATION_GATE,
JARVIS_FLAG_REGISTRY_ENABLED, JARVIS_L2_ENABLED, JARVIS_MIN_RISK_TIER,
JARVIS_PARANOIA_MODE, JARVIS_PLAN_APPROVAL_MODE, JARVIS_SEMANTIC_GUARD_ENABLED,
JARVIS_THINKING_BUDGET_IMMEDIATE
```

Every knob an operator needs when the organism is HARDENing — discoverable from one command. When DirectionInferrer infers HARDEN based on rising postmortem rate, `/help flags --posture` surfaces the exact gates to tighten.

## 52-flag seed breakdown

| Category | Count | Representative |
|---|---|---|
| safety | 24 | JARVIS_DIRECTION_INFERRER_ENABLED, JARVIS_L2_ENABLED, JARVIS_ASCII_GATE |
| capacity | 6 | JARVIS_POSTURE_HISTORY_SIZE, JARVIS_BG_POOL_SIZE |
| experimental | 6 | JARVIS_SEMANTIC_INFERENCE_ENABLED, JARVIS_VISION_SENSOR_ENABLED |
| timing | 5 | JARVIS_POSTURE_OBSERVER_INTERVAL_S, JARVIS_L2_TIMEBOX_S |
| observability | 4 | JARVIS_STRATEGIC_GIT_HISTORY_ENABLED, JARVIS_FLAG_TYPO_WARN_ENABLED |
| tuning | 3 | JARVIS_POSTURE_CONFIDENCE_FLOOR, JARVIS_FLAG_TYPO_MAX_DISTANCE |
| integration | 2 | JARVIS_IDE_OBSERVABILITY_CORS_ORIGINS, JARVIS_GENERATE_ATTACHMENTS_ENABLED |
| routing | 2 | JARVIS_THINKING_BUDGET_IMMEDIATE, JARVIS_TIER1_RESERVE_S |

## Full-revert matrix proof

```
[graduated defaults]
  → is_enabled()=True, dispatcher_enabled()=True, typo_warn_enabled()=True
  → /help works, GET /observability/flags 200, SSE publishes emit

[master=false single env flip]
  → is_enabled()=False (✓)
  → dispatcher_enabled()=False (✓)
  → typo_warn_enabled()=False (✓)
  → /help flags rejected, cites "JARVIS_FLAG_REGISTRY_ENABLED" (✓)
  → /help help STILL works (discoverability exception) (✓)
  → GET /observability/flags 403 (✓)
  → GET /observability/flags/{name} 403 (✓)
  → GET /observability/flags/unregistered 403 (✓)
  → GET /observability/verbs 403 (✓)

[master env var removed again]
  → is_enabled() back to True (✓)
  → GET /flags back to 200 (✓)
  → /help flags back to ok (✓)
```

Bidirectional. No restart required.

## 38 graduation pins

| Group | Pins | What |
|---|---|---|
| A. Authority | 6 | grep-enforced zero-import on 3 arc files + 4 GET handlers + 3 SSE helpers |
| B. Behavioral | 10 | master off/on, help-still-works, duplicate registration (override + strict), malformed value fallback, Levenshtein threshold + symmetry, filter exclusivity, thread-safety stress, JSON export stability |
| C. Graduation-specific | 9 | default literal `True`, is_enabled True, seed ≥50, 9 DirectionInferrer flags, 8 categories, 4 postures, source_file non-empty, description ≥10 chars, typo max_distance=3 |
| C'. Docstring bit-rot | 3 | "authority-free" citation, "Tier 0" positioning, "read-only" in help_dispatcher |
| D. Schema version | 3 | registry schema `"1.0"`, JSON export, SSE frames |
| E. Integration | 3 | report_typos → SSE typo fires, bridge → SSE registered fires, GET double-gated |
| F. Full-revert matrix | 2 | one flip kills REPL + 4 GETs + typo warn; /help help still works |
| G. CLAUDE.md doc | 3 | mentions FlagRegistry + /help + master flag name |

## What's next

**Wave 1 #3 — SensorGovernor.** Global op-emission cap across 16 sensors, weighted by current posture (TestFailure ×1.8 HARDEN, OpportunityMiner ×1.5 EXPLORE, etc.) + MemoryPressureGate that refuses worktree fan-out under threshold.

SensorGovernor can now consume:
- `get_current_posture()` from Wave 1 #1
- `registry.relevant_to_posture(posture)` from Wave 1 #2 for per-sensor flag discovery
- `registry.get_int/bool(...)` typed accessors with auto-registration

**Slice 5 (deferred) for FlagRegistry:**
- `FlagRegistry.snapshot_for_posture(posture)` returning active-flag values filtered by posture relevance
- Migration script: `scripts/audit_env_flags.py` to discover and register the ~430 remaining flags
- VS Code / Cursor extension integration for `flag_typo_detected` SSE frames (live operator warnings in IDE)

Lands when a downstream actually consumes the API — no speculative surface.

## Per-slice E2E mandate proof

Per Derek's durable guidance, every slice ran real-repo live-fire. Bugs caught by live-fire across this arc:

1. **Slice 2** — text-case mismatch in test assertion (trivial)
2. **Slice 2** — my posture-filter counting regex had wrong indent pattern; real stdout showed the right data, test counted wrong rows
3. **Slice 3** — FlagSpec unhashable (dict field); `set()` comparison crashed server-side → fixed with name-comparison filter
4. **Slice 3** — `ide_observability.py` default for `JARVIS_IDE_OBSERVABILITY_ENABLED` is `true` (Gap #6 graduated); tests that wanted 403 needed explicit `=false`

Each fix landed in-slice; zero bugs deferred forward.

## Operator reference — flag kill-switch cascade

| Flag | Default | Effect when `false` |
|---|---|---|
| `JARVIS_FLAG_REGISTRY_ENABLED` | `true` | All 4 surfaces revert in lockstep (master kill) |
| `JARVIS_FLAG_TYPO_WARN_ENABLED` | `true` | Typo warnings silent (logs + SSE drops) |
| `JARVIS_FLAG_TYPO_MAX_DISTANCE` | `3` | Tuning knob — lower = stricter |
| `JARVIS_HELP_DISPATCHER_ENABLED` | `true` | Sub-gate for /help operational verbs |
| `JARVIS_IDE_OBSERVABILITY_ENABLED` | `true` (Gap #6) | Kills GET + first gate on /observability/flags* |
| `JARVIS_IDE_STREAM_ENABLED` | `true` (Gap #6) | Kills SSE (publishes drop silently) |
