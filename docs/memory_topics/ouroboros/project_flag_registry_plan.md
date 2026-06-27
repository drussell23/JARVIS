---
title: FlagRegistry + /help Dispatcher — 5-Slice Arc
modules: [backend/core/ouroboros/governance/]
status: merged
source: project_flag_registry_plan.md
---

# FlagRegistry + /help Dispatcher — 5-Slice Arc

**Wave 1 priority #2 for Ouroboros + Venom A-level execution.** First real consumer of Wave 1 #1 (DirectionInferrer posture) for relevance filtering.

## Problem

- **481+ unregistered JARVIS_* env flags** scattered across the codebase. Operators discover them by grepping. No single source of truth.
- **Silent typos** — setting `JARVIS_POSTUR_ENABLED=true` silently falls back to the default because the real flag is `JARVIS_POSTURE_ENABLED`.
- **No cross-verb `/help`** — each REPL (`/posture`, `/recover`, `/session`, `/cost`, `/plan`, `/layout`, `/attach`, `/loop`, `/schedule`) has its own `help`; no top-level discovery surface.
- **No posture-relevance filtering** — operator in HARDEN mode doesn't know which gates tighten under them; operator in EXPLORE mode doesn't know which knobs open up the exploration surface.

## Solution shape

**FlagRegistry** — process-wide typed directory of every `JARVIS_*` env flag the organism reads. Each entry carries name, type, default, description, category, posture-relevance, source-file, example, since-version. Authority-free — purely descriptive. Typed accessors (`get_bool/int/float/str/json`) both read env AND record usage for later audits.

**/help dispatcher** — top-level REPL verb that enumerates all registered REPL verbs + all registered flags + filters by category / posture / substring. Also surfaces unregistered env vars (typo hunter) via Levenshtein similarity.

## Final design rulings (carry over from prior arcs unless superseded)

1. **Authority-free** — registry imports nothing from `orchestrator` / `policy` / `iron_gate` / `risk_tier` / `change_engine` / `candidate_generator` / `gate`. Grep-pinned Slice 4.
2. **Master flag** — `JARVIS_FLAG_REGISTRY_ENABLED` (default `false` Slice 1, graduates `true` Slice 4). Registry data structure stays alive when flag is off (it's just a dict); the **surfaces** go dark (no `/help`, no GET, no SSE, no typo warnings).
3. **Per-slice E2E live-fire** required (Derek's mandate).
4. **Posture-relevance filter** consumes `get_current_posture()` from Wave 1 #1 — first real downstream reader of the DirectionInferrer.
5. **TUI: rich tables** with flat fallback in headless (same pattern as `/posture explain`).

## Categories (fixed vocabulary)

| Category | Examples |
|---|---|
| `safety` | `*_ENABLED` kill switches, `JARVIS_MIN_RISK_TIER`, `JARVIS_PARANOIA_MODE` |
| `timing` | intervals, timeouts (`*_INTERVAL_S`, `*_TIMEOUT_S`, `*_WINDOW_S`) |
| `capacity` | sizes, pools, caps (`*_SIZE`, `*_POOL_SIZE`, `*_MAX_*`) |
| `routing` | provider cascade, model selection, urgency routing |
| `observability` | SSE, GET, logging, audit flags |
| `integration` | external system wiring (GitHub, IDE, MCP, voice) |
| `experimental` | shadow / not-yet-graduated feature flags |
| `tuning` | weights, thresholds, floors (`*_FLOOR`, `*_THRESHOLD`) |

## Posture relevance

Each registered flag carries `posture_relevance: dict[Posture, Relevance]` where `Relevance ∈ {CRITICAL, RELEVANT, IGNORED}`. Default `None` means "no posture-specific relevance" (shown in all posture views). Filter surfaces `/help flags --posture HARDEN` show CRITICAL + RELEVANT flags for that posture.

## Location

```
backend/core/ouroboros/governance/
  flag_registry.py                   # Core registry + FlagSpec (Slice 1)
  flag_registry_seed.py              # ~50 pre-registered flags (Slice 1)
  help_dispatcher.py                 # /help REPL verb + verb registry (Slice 2)
  ide_observability.py               # +GET /observability/flags (Slice 3)
  ide_observability_stream.py        # +SSE flag_typo_detected event (Slice 3)
```

## Slice 1 — `FlagRegistry` primitive + typed accessors + seed registrations

**Goal:** Process-wide typed dict of all known flags. Authority-free. Pure function on registered spec + env read. Graduation kill switch shipped but default off.

**Deliverables:**
- `flag_registry.py`:
  - `FlagType(Enum)` — BOOL / INT / FLOAT / STR / JSON
  - `Relevance(Enum)` — CRITICAL / RELEVANT / IGNORED
  - `FlagSpec(dataclass, frozen)` — name, type, default, description, category, posture_relevance, source_file, example, since
  - `FlagRegistry(class)` — register / get_bool / get_int / get_float / get_str / get_json / list_all / list_by_category / find / relevant_to_posture / unregistered_env / report_typos / to_json
  - Thread-safe via `threading.Lock`
  - `get_default_registry()` / `reset_default_registry()` singletons
- `flag_registry_seed.py`:
  - Pre-registers ~50 flags across all 8 categories — every DirectionInferrer flag, IDE observability, task board, TOOL_MONITOR, PLAN_APPROVAL, L2_REPAIR, AUTO_COMMIT, SUBAGENT, SEMANTIC_INDEX, BG_POOL, ORANGE_PR, EXPLORATION_LEDGER, ASCII_GATE, MIN_RISK_TIER, PARANOIA_MODE, AUTO_APPLY_QUIET_HOURS, SEMANTIC_GUARD_*, STRATEGIC_GIT_HISTORY, MULTI_FILE_GEN, GENERAL_LLM_DRIVER, VISION_SENSOR, VISUAL_VERIFY, etc.
  - `seed_default_registry(registry: FlagRegistry)` function — called once at module-load or boot
- `is_enabled()` master switch (default `false`, Slice 4 graduates)
- Levenshtein typo detection with env-var prefix match (`JARVIS_*` scope)

**Contracts:**
- Zero authority imports (grep-pinned Slice 4)
- Typed accessors on malformed env value → logged + fall back to default
- Duplicate registration → default behavior is override-with-warning (tests pin)
- `FlagSpec` is frozen — once registered, immutable

**Env flags introduced:**
- `JARVIS_FLAG_REGISTRY_ENABLED` (master, default `false`, graduates Slice 4)
- `JARVIS_FLAG_TYPO_WARN_ENABLED` (default `true` once master on)
- `JARVIS_FLAG_TYPO_MAX_DISTANCE` (default `3` — Levenshtein threshold)

**Tests (~50):** register + get shapes per type, type coercion + malformed fallback, duplicate registration, Levenshtein typo detection, category / posture / pattern filters, unregistered env scan, JSON export round-trip, thread-safety stress, schema_version pin, authority-free grep.

**Live-fire:** seed ~50 flags → query each via real env read → inject `JARVIS_POSTUR_ENABLED` typo → expect Levenshtein warning → export JSON → verify all DirectionInferrer flags present → authority-free grep across arc files.

## Slice 2 — `/help` dispatcher + verb registry + flag introspection

**Goal:** Single top-level operator verb that enumerates every REPL + every flag.

**Deliverables:**
- `help_dispatcher.py`:
  - `VerbSpec(dataclass, frozen)` — name, one_line, dispatcher_fn, help_text_fn, category
  - `VerbRegistry(class)` — `register(spec)`, `list_verbs()`, `find_verb(name)`
  - `/help` subcommands:
    - `/help` — top-level index (verbs + flag categories)
    - `/help <verb>` — delegates to verb's own help
    - `/help verbs` — verb-only list
    - `/help flags [--category X] [--posture P] [--search Q]`
    - `/help flag <FLAG_NAME>` — full detail for one flag
    - `/help unregistered` — env vars in environment not matching any spec
    - `/help category <CAT>` — flags in category
    - `/help posture [P]` — flags relevant to current or named posture
    - `/help stats` — registry metrics (count by type, by category, by posture, by source file)
  - Rich TUI tables with flat fallback
- Seed verb registrations: `/posture`, `/recover`, `/session`, `/cost`, `/plan`, `/layout`, `/help` itself
- Each existing REPL module gets a 3-line `register_with_help_dispatcher()` call at module load (opt-in, so no forced migration)

**Env flags:** `JARVIS_HELP_DISPATCHER_ENABLED` (default `true` when master on).

**Tests (~50):** dispatch shape, each subcommand, verb registration + de-dup, flag detail renderer, category filter, posture filter (requires DirectionInferrer master on), unregistered detection, stats rollup, rich-vs-flat fallback, authority-free grep.

**Live-fire:** register 6+ verbs + 50+ flags → exercise all 9 subcommand shapes → inject 2 typo env vars → `/help unregistered` shows them both with Levenshtein suggestions → `/help posture HARDEN` surfaces correct subset.

## Slice 3 — IDE observability: GET /observability/flags + SSE typo events

**Goal:** Extensions can introspect the flag registry + stream typo warnings.

**Deliverables:**
- `ide_observability.py`:
  - `GET /observability/flags` — list with filters (`?category=X`, `?posture=P`, `?search=Q`, `?limit=N`)
  - `GET /observability/flags/{name}` — full FlagSpec projection
  - `GET /observability/flags/unregistered` — typo-hunter output
  - `GET /observability/verbs` — registered REPL verbs
- `ide_observability_stream.py`:
  - `EVENT_TYPE_FLAG_TYPO_DETECTED` — fires when registry first sees an unregistered env var matching a registered flag within Levenshtein distance
  - `EVENT_TYPE_FLAG_REGISTERED` — fires on late-bound registration (post-boot)
- Same disciplines as Gap #6 Slice 1/2: loopback, rate limit, CORS, schema_version, Cache-Control no-store, 403 on flag off.

**Env flags:** `JARVIS_FLAG_OBSERVABILITY_ENABLED` (default `true` when master on).

**Tests (~50):** GET shapes + filters, malformed query params → 400, rate limit, CORS, schema_version, 403 off-path, SSE event whitelist, typo detection fires SSE exactly once per unique typo, authority-free grep.

**Live-fire:** boot EventChannelServer + IDE flag surface → GET endpoints return seed data → subscribe to SSE via raw socket → inject `JARVIS_POSTUR_ENABLED` → receive `flag_typo_detected` frame → register a new flag at runtime → receive `flag_registered` frame.

## Slice 4 — Graduation

**Flip `JARVIS_FLAG_REGISTRY_ENABLED` default `false → true`.**

**~35 graduation pins:**
- Authority (6) — grep-enforced on flag_registry.py, flag_registry_seed.py, help_dispatcher.py + GET handlers
- Behavioral (10) — master off disables all surfaces; typed accessor fallbacks; Levenshtein threshold; duplicate registration; thread safety stress; filter accuracy; JSON export stability
- Graduation-specific (8) — default literal True; seed registers ≥40 flags; all 9 DirectionInferrer flags in registry; 8 categories covered; all 4 postures reachable in relevance; every seeded flag has source_file; since-version ≥ "v1.0"
- Docstring bit-rot (3) — "authority-free" citation, "481+ flags" motivation, "§5 Tier 0" positioning
- Integration (3) — /help posture consumes get_current_posture(), SSE typo fires exactly once, GET double-gated
- Full-revert matrix (2) — one env flip kills all 4 surfaces (REPL + GET + SSE + typo warn), /help help still works master-off
- CLAUDE.md doc guard (3) — entry mentions FlagRegistry, /help, master flag

**Live-fire:** zero-env boot → all surfaces active → GET returns seed data → /help works → typo detected → SSE fires → flip master=false → all surfaces dark → /help help still works → re-default restores.

## Slice 5 — Cross-module migration + consumer API (deferred)

**Goal:** Lands when Wave 1 #3 SensorGovernor ships and actually needs it.
- Add `FlagRegistry.snapshot_for_posture(posture)` returning active-flag values filtered by posture relevance
- Migrate 100+ flags from scattered `os.environ.get()` to `registry.get_bool()` with auto-registration
- Extend seed set to all ~500 flags via an audit script that scans the codebase

Defer until a downstream actually consumes the API — no speculative surface.

## Cross-slice invariants

1. Zero authority — grep-pinned every slice
2. Authority-free in principle: registry is descriptive; `/help override X=Y` does NOT exist (design); there is no write-surface
3. Kill switch: `JARVIS_FLAG_REGISTRY_ENABLED=false` kills all surfaces in lockstep
4. §5 Tier 0 — no LLM in hot paths; pure dict operations
5. §8 Observability — every typo detection is logged + SSE-published once
6. Schema version `"1.0"` on every artifact + GET payload + SSE frame

## Effort

| Slice | LoC | Tests | Sessions |
|---|---|---|---|
| 1 | ~600 | ~50 | 1-2 |
| 2 | ~600 | ~50 | 1-2 |
| 3 | ~400 | ~50 | 1 |
| 4 | ~200 + script | ~35 | 1 |
| 5 | ~500 | ~40 | deferred |
| **Total** | **~1800 + deferred** | **~185 + deferred** | **4-6** |
