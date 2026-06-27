---
title: DirectionInferrer + StrategicPosture — 5-Slice Arc
modules: [tests/rollback, backend/core/ouroboros/governance/, scripts/livefire_direction_inferrer.py]
status: historical
source: project_direction_inferrer_plan.md
---

# DirectionInferrer + StrategicPosture — 5-Slice Arc

**Wave 1 priority #1 for Ouroboros + Venom A-level execution.** Foundation for:
- Wave 1 #2 (FlagRegistry) — tags env flags by posture relevance
- Wave 1 #3 (SensorGovernor) — weights per-sensor op budget by posture

## Shape check

**What it is:** Deterministic signal→posture inference primitive. Reads ~10 ambient telemetry signals (git history, postmortems, open ops, session lessons, cost burn, Iron Gate rejects, etc.) and emits `StrategicPosture` as metacognition — *"what kind of work does the organism most value right now?"*

**What it is not:**
- NOT an authority channel. Posture is **advisory** — appears in prompts and dashboards, never blocks an op.
- NOT a classifier. Pure rule-based vector math, <10ms, no LLM calls. §5 Tier 0.
- NOT an operator toggle. Inferred from signals. Overrides exist but are explicit, time-bound, logged.
- NOT a replacement for `strategic_direction.py` — that carries the *manifesto*; this carries the *current disposition*. They compose.

## Final design rulings (user-ratified)

1. **Posture vocabulary** — strictly 4 values: `EXPLORE`, `CONSOLIDATE`, `HARDEN`, `MAINTAIN`. Severity encoded in confidence score. Acute failure belongs to Tier 3 (Nervous System Reflex) / Iron Gate, not a passive `RECOVER` posture.
2. **Override logging** — new dedicated `.jarvis/posture_audit.jsonl`. §8 isolation: audit trail must be dedicated + immutable so the agentic side can never alter its own posture logs.
3. **Signals** — ship v1 with 10 core signals. Extend to 11-12 post-graduation.
4. **`/posture explain`** — rich TUI table, graceful fallback to flat text in headless (same pattern as `stream_renderer.py`).

## Posture vocabulary

| Posture | Meaning | Driven by |
|---------|---------|-----------|
| `EXPLORE` | Ship new capabilities, take risks, accept churn | High `feat:` commit skew, low postmortem failure rate, recent graduations clean |
| `CONSOLIDATE` | Close open threads, finish in-flight arcs, prefer graduation over new slices | Many stale WIP branches, `refactor:` skew, many deferred/parked items |
| `HARDEN` | Stabilize before new features, tighten gates, favor tests/rollback | Rising postmortem rate, Iron Gate reject spike, L2 repair frequency up, `fix:` skew |
| `MAINTAIN` | No strong directional signal — steady state default | All signals near baseline, OR confidence below floor (fallback) |

## Location

```
backend/core/ouroboros/governance/
  direction_inferrer.py       # Primitive (Slice 1)
  posture.py                  # Dataclass + enum (Slice 1)
  posture_observer.py         # Periodic async task (Slice 2)
  posture_store.py            # Durable state + history (Slice 2)
```

## Slice 1 — `StrategicPosture` dataclass + `DirectionInferrer` primitive

Pure function `DirectionInferrer.infer(SignalBundle) → PostureReading`. Zero side effects, zero authority, fully deterministic.

**Deliverables:**
- `posture.py`: `Posture(Enum)`, `SignalContribution`, `PostureReading` (schema_version="1.0", with signal_bundle_hash for idempotence), `SignalBundle` (with schema_version)
- `direction_inferrer.py`: `DirectionInferrer` class; methods `_normalize`, `_score`, `infer`; `DEFAULT_WEIGHTS` dict; confidence = `(top - second) / top`, clamped; fallback to `MAINTAIN` if `confidence < JARVIS_POSTURE_CONFIDENCE_FLOOR` (default 0.35)
- Deterministic tie-break: alphabetic on posture name (CONSOLIDATE > EXPLORE > HARDEN > MAINTAIN)

**Signal weight table (initial hypothesis — tune post-graduation):**

| Signal | EXPLORE | CONSOLIDATE | HARDEN | MAINTAIN |
|--------|---------|-------------|--------|----------|
| `feat:` commit ratio (last 50) | +1.0 | -0.3 | -0.2 | 0 |
| `fix:` commit ratio | -0.4 | 0 | +1.0 | 0 |
| `refactor:` commit ratio | -0.2 | +0.8 | 0 | 0 |
| `test:` + `docs:` ratio | -0.2 | +0.4 | +0.2 | +0.3 |
| Postmortem failure rate (48h) | -0.8 | 0 | +1.2 | 0 |
| Iron Gate reject rate (24h) | -0.5 | +0.2 | +0.9 | 0 |
| L2 repair invocation rate | -0.3 | +0.3 | +0.6 | 0 |
| Open ops in-flight (normalized) | +0.4 | -0.2 | 0 | 0 |
| Session lessons infra% | -0.2 | +0.2 | +0.7 | 0 |
| Time since last graduation (inverse) | +0.3 | -0.4 | 0 | 0 |
| Cost burn rate (24h, normalized) | +0.2 | -0.3 | -0.2 | 0 |
| Worktree orphan count | -0.2 | +0.5 | +0.2 | 0 |

**Contracts:**
- Zero authority: no imports from `orchestrator`, `policy`, `iron_gate`, `risk_tier`, `gate`, `change_engine`, `candidate_generator`. Grep-pinned Slice 4.
- Pure function: `infer()` stateless beyond weights. Same bundle → same reading.
- No I/O in `infer()`.
- Deterministic tie-break alphabetic.

**Env flags introduced (Slice 1):**
- `JARVIS_DIRECTION_INFERRER_ENABLED` (master, default `false`; Slice 4 graduates)
- `JARVIS_POSTURE_CONFIDENCE_FLOOR` (default `0.35`)
- `JARVIS_POSTURE_WEIGHTS_OVERRIDE` (optional JSON hot-swap for A/B)

**Tests (~35-40):** 4× canonical bundle → expected posture (one per posture), idempotence hash, confidence floor fallback, alphabetic tie-break, weight override flip, every Posture reachable, edge cases (empty commits, zero postmortems, all baseline), schema_version stability.

**DoD:** tests green, grep pin passes, no Slice 2+ wiring. Commit: `feat(governance): add DirectionInferrer primitive (default off)`.

## Slice 2 — PostureObserver + PostureStore + StrategicDirection injection

**Deliverables:**
- `posture_store.py`: `PostureStore` writes `.jarvis/posture_current.json` + ring buffer `.jarvis/posture_history.jsonl`; atomic write (temp+rename); schema_version-gated read.
- `posture_observer.py`: asyncio `Task` periodic, `JARVIS_POSTURE_OBSERVER_INTERVAL_S` default `300`; signal collectors (one method per signal) read-only; structured concurrency via TaskGroup in `governed_loop_service.py`; hysteresis — posture changes require (a) `JARVIS_POSTURE_HYSTERESIS_WINDOW_S` elapsed (default 900s) OR (b) `confidence > 0.75` OR (c) override.
- `strategic_direction.py`: `_compose_posture_section(reading) → str` markdown block gated by `JARVIS_POSTURE_PROMPT_INJECTION_ENABLED`. Rendered ~300 chars, top 3 contributors.

**Contracts:** Observer side-effect-free on organism. Never blocks main loop (`asyncio.wait_for` 30s). Failures opt-in benign (caught, logged, counter++, next cycle). Schema version gates reject mismatched files → cold start.

**Env flags:**
- `JARVIS_POSTURE_OBSERVER_INTERVAL_S` (300)
- `JARVIS_POSTURE_HISTORY_SIZE` (256)
- `JARVIS_POSTURE_HYSTERESIS_WINDOW_S` (900)
- `JARVIS_POSTURE_PROMPT_INJECTION_ENABLED` (true when master on)
- `JARVIS_POSTURE_SIGNAL_COMMIT_WINDOW` (50)
- `JARVIS_POSTURE_SIGNAL_POSTMORTEM_WINDOW_H` (48)

**Tests (~40):** observer cycles produce readings; hysteresis respected; observer timeout clean skip; observer failure stays alive; cold start → MAINTAIN; schema mismatch → cold start; master flag off → observer doesn't start; prompt injection gated by both master + injection flags; block format stable.

**DoD:** ~75-80 tests green; observer emits readings in 10-cycle dry-run; `.jarvis/posture_*.json` human-readable; grep pin. Commit: `feat(governance): add PostureObserver + StrategicDirection prompt integration (default off)`.

## Slice 3 — Operator surface: /posture REPL + IDE observability + SSE

**`/posture` REPL** in `serpent_flow.py` (mirrors `/session`, `/cost`, `/plan`):
- `status` — posture, confidence, since-when, top 3 signals, override banner
- `explain` — full signal table (rich TUI, flat fallback)
- `history [N]` — last N readings (default 20)
- `override <posture> [--until <dur>] [--reason <txt>]` — clamped to `JARVIS_POSTURE_OVERRIDE_MAX_H` (default 24h)
- `clear-override` — drops override
- `signals` — raw signal values (diagnostic)
- `help`

**Override semantics:** `{who=user, at, until, reason}` to `.jarvis/posture_audit.jsonl` (dedicated file, §8); observer continues underneath; current masked by override until expiry; expiry evaluated at read time.

**IDE observability (`ide_observability.py`):**
- `GET /observability/posture` — current reading (loopback, rate-limit 120/min, CORS, schema_version, no-store)
- `GET /observability/posture/history?limit=N`

**SSE (`ide_observability_stream.py`):**
- New event: `posture_changed` — `{posture, previous_posture, confidence, inferred_at, trigger}` where trigger ∈ {inference, override, override_cleared, override_expired}
- Hook via `bridge_posture_to_broker` best-effort (never-raise pattern from Problem #7)

**Contracts:** REPL override is ONLY authority surface; doesn't bypass Iron Gate/SemanticGuardian/risk tiers. GET endpoints read-only (Gap #6 disciplines). SSE on state transitions only. Authority invariant grep-pinned Slice 4.

**Env flags:**
- `JARVIS_POSTURE_REPL_ENABLED` (true when master on)
- `JARVIS_POSTURE_OVERRIDE_MAX_H` (24)
- `JARVIS_POSTURE_OBSERVABILITY_ENABLED` (true when master on)

**Tests (~55):** subcommand shapes; override clamped + warned; override masks natural inference; clear-override resumes; invalid posture name → error; override expiry auto-revert; GET schema + 403 non-loopback + rate limit + CORS; `posture_changed` fires on 4 triggers; multi-subscriber fan-out; broker failure doesn't raise; override persisted across restart.

**DoD:** ~130-140 tests green; `/posture` in `/help`; extensions receive SSE via existing infra. Commit: `feat(governance): add /posture REPL + IDE observability + SSE events (default off)`.

## Slice 4 — Graduation (flip master flag false → true)

**Graduation pins (~20-25):**

*Authority (6):* grep-enforced zero-import tests on `direction_inferrer.py`, `posture.py`, `posture_observer.py`, `posture_store.py`, `/posture` REPL handler, `/observability/posture*` router.

*Behavioral (10):* master off → None + 403 + SSE filter; master on injection off; observer timeout doesn't crash; collector exception doesn't crash; hysteresis respected; override MAX_H cap; override expiry auto-revert; confidence floor → MAINTAIN; schema mismatch → cold start; alphabetic tie-break.

*Graduation-specific (6):* default value literal string `"true"`; `CLAUDE.md` docstring present; full-revert matrix (master=false at runtime fully disables); observer interval 300s; hysteresis 900s; confidence floor 0.35.

**Live-fire (`scripts/livefire_direction_inferrer.py`):** Boot harness master=true → observe initial posture convergence → inject synthetic postmortem failures → observe HARDEN within 2 cycles → `/posture override EXPLORE --until 1h --reason "graduation"` → verify masking → clear → natural resume → assert 3 SSE events received via raw-socket subscriber. Exit 0 = pass. Commit `livefire_direction_inferrer_PASS.log`.

**DoD:** ~175-180 tests green; live-fire passes; graduation PR with flag flip + memory file + `CLAUDE.md` entry. Commit: `feat(governance): graduate DirectionInferrer — flip JARVIS_DIRECTION_INFERRER_ENABLED default false→true`.

## Slice 5 — Anti-oscillation + drift detection + consumer hooks

**Anti-oscillation:** EMA over last N readings (default 5); flip ceiling K=3 / hour W=3600s, beyond → `PostureOverload` log + hold last stable; `flip_count_hour` in `/posture status`.

**Drift detection:** slope of confidence + dominant posture score over rolling window; if slope > threshold for N consecutive cycles without flip → `posture_drifting` SSE + REPL banner. Surfaces slow-moving change hysteresis hides.

**Consumer hooks (public API for #2 FlagRegistry + #3 SensorGovernor):**
- `get_current_posture() → Posture | None`
- `get_current_reading() → PostureReading | None`
- `subscribe(callback) → UnsubscribeHandle` — in-process pub/sub
- `get_posture_weighted_budget(base_budget, sensor_name) → float` — for SensorGovernor
- `is_flag_relevant_to_posture(flag_name) → bool` — for FlagRegistry

**Per-posture sensor weight table** — TestFailure ×1.8 HARDEN, OpportunityMiner ×1.5 EXPLORE, DocStaleness ×1.3 CONSOLIDATE, RuntimeHealth ×1.5 HARDEN, IntentDiscovery ×1.4 EXPLORE, etc.

**Env flags:** `JARVIS_POSTURE_EMA_WINDOW` (5), `JARVIS_POSTURE_FLIP_CEILING` (3/hr), `JARVIS_POSTURE_DRIFT_SLOPE_THRESHOLD` (0.1), `JARVIS_POSTURE_DRIFT_CONSECUTIVE_CYCLES` (3).

**Tests (~30):** EMA convergence; flip ceiling blocks 4th; drift SSE after N cycles; subscribe/unsubscribe; weighted budget per posture×sensor; unsubscribe honored; empty weight table → 1.0.

**DoD:** ~205-210 tests green; public API callable from #2/#3 arcs without further changes. Commit: `feat(governance): anti-oscillation damping + drift detection + consumer API`.

## Cross-slice invariants (pinned in Slice 4 graduation)

1. Zero authority — advisory only
2. §5 Tier 0 — deterministic math, no LLM in hot path
3. §8 Observability — every change logged + SSE + REPL + audit trail (dedicated file)
4. Confidence floor — won't commit without evidence
5. Kill switch — `JARVIS_DIRECTION_INFERRER_ENABLED=false` fully disables in one flag
6. Schema version discipline — every persisted artifact + dataclass + SSE payload + GET response carries `schema_version`

## Effort estimate

| Slice | LoC | Tests | Sessions |
|-------|-----|-------|----------|
| 1 | ~400 | ~35 | 1 |
| 2 | ~500 | ~40 | 1-2 |
| 3 | ~600 | ~55 | 1-2 |
| 4 | ~200 + script | ~25 | 1 |
| 5 | ~400 | ~30 | 1-2 |
| **Total** | **~2100** | **~185** | **5-8** |

Aligns with Gap #6 (7 slices, 216 tests) and Problem #7 (5 slices, 112 tests).
