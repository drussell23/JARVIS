---
title: Project Prd Hygiene Pass V224
modules: []
status: merged
source: project_prd_hygiene_pass_v224.md
---

**Status (2026-05-05)**: PRD v2.24 hygiene pass LANDED. Coverage audit gaps closed; PRD now navigable top-down without conflicting checkbox state.

## What was stale (before pass)

1. §32.5 cleanup checklist (lines 346-352) — 6 items rendered as `[ ]` but §32.5.2 closure markers confirmed all closed 2026-05-04
2. M9 / M10 / M11 + U1 / U2 / U3 in §30.5 / §31.4 main bodies — closures documented in version banners but checkboxes not flipped
3. P10.4 — `[ ]` but audit confirmed shipped at 4 sites (exceeded PRD's 3)
4. §32.8.1 v4 supplement (line 5037) — flatly stated Phase 9 was "**NOT STARTED — CRITICAL BLOCKER**" but Phase 9 substrate landed 2026-04-27 (7 days BEFORE the supplement was authored)

## What was missing (before pass)

5. **No reusable-meta-pattern section** for crystallized architectural disciplines (graduation contract / producer bridge / Slice 5b naming cage / flock'd JSONL)
6. **VisionSensor + Multi-modal ingest + Visual VERIFY** — major capabilities described in CLAUDE.md but unanchored in PRD
7. **Open Strategic Moves** — §28.6.3 + §29.4 + §3.6.2 vectors scattered across multiple brutal reviews with no consolidated registry

## What landed in v2.24

### Checkbox refreshes (~13 items flipped to `[x]`)

- §32.5 cleanup: 6 items + bonus §32.11 row added
- §30.5 ASCO: M9 / M10 / M11
- §31.4 Critical Path: U1 / U2 / U3
- Phase 10: P10.4
- §32.8.1 v4 supplement Phase 9 status corrected with explicit audit-refresh annotation

### §33 Reusable Meta-Patterns (~370 lines)

Documents 4 crystallized architectural disciplines with reference implementations + future-arc inheritance map:

1. **Graduation Contract Pattern** — `phase10_graduation_contract.py`, `graduation/graduation_contract.py`, M10 master flag pin, `topology_sentinel_master_flag_stays_default_false`. Pattern: master flag stays default-false until `is_ready_for_<verb>() -> ContractVerdict` predicate green; AST pin enforces flag default; tests verify pin fires on premature flip.
2. **Producer-Bridge Pattern** — `curiosity_producer_bridge.py`, `phase8_producers.py`, `confidence_probe_bridge.py`. Lazy-import + NEVER-raises wrapper modules isolate producer/consumer arcs.
3. **Slice 5b Naming-Convention Cage** — `*_observability.py` exposes `register_routes`; `*_repl.py` exposes `dispatch_<basename>_command`. AST pin + signature validation make wiring zero-edit.
4. **Per-Cluster `flock`'d JSONL Persistence** — `cross_process_jsonl.py` canonical primitive used by 8+ stores. Closes §28.5.1 v9 brutal review's "concrete data-loss path" finding.

Includes composition table showing how M9/M10/Phase 10/Phase 9 inherit each pattern.

### §34 VisionSensor + Multi-modal Subsystem (~75 lines)

Anchors three subsystems described in CLAUDE.md but unanchored in PRD:
- **VisionSensor** — 17th sensor, Tier 0/1/2 cascade, no-capture-authority AST-enforced, NOTIFY_APPLY risk floor
- **Multi-modal ingest path** — `_serialize_attachments` Claude/DW dispatch, BG/SPEC strip-attachments structurally
- **Visual VERIFY** — 3-tier trigger ladder, deterministic battery first-miss-wins, asymmetric TestRunner clamp, ≥50% FP auto-demotion

### §35 Open Strategic Moves Registry (~120 lines)

Consolidates §28.6.3 Moves 6-10 + §29.4 Move 7-8 + §3.6.2 vectors #6-#12 + §28.5.1 race conditions into a single severity-tagged registry with current status + action recommendations:

- 🔴 Critical: vector #6 Default-False Flag Problem (closes via Phase 9 cadence)
- 🟠 High: vector #7 Quine-shape (Move 6 default-FALSE empirically unproven), Move 8 GENERAL LLM driver (status conflict)
- 🟡 Medium: 5 items closeable via Wave 3 hygiene arc (≤6-8 hours total via `cross_process_jsonl` migration class) — vectors #8/#10/§28.5.1 invariant_drift_store baseline race + Move 7 Cross-op Semantic Budget + 4-phases-not-extracted
- 🔵 Low: vectors #9, #11, #12

Identifies highest-leverage cluster: Wave 3 hygiene arc closes 5 medium-severity items in ≤1 week.

## Operator decision points exposed

The audit + hygiene pass surfaced 3 distinct next-step options:
1. **Wave 3 hygiene arc** (~6-8 hours, closes 5 🟡 medium items)
2. **Phase 9 empirical cadence** (operator-paced soak runs, structurally ready)
3. **Move 8 GENERAL LLM driver status conflict resolution** (source-grep `agentic_general_subagent.py:39` + reconcile CLAUDE.md vs §28.6.3)

## Test impact

263/263 across consolidation + Phase 10 spine still green post-PRD edits. Pure documentation pass; no code touched.

## What's NOT yet in the PRD (residual)

- **CC capability gaps** mentioned in earlier session (per-tool hooks V1-V4 + GitHub Patterns B+C) — these ARE listed in §32.6 + §32.7 with `[ ]` markers; NOT covered in §35 because they're already-scoped pending arcs, not "open strategic moves" requiring scoping
- **Phase B subagents** (REVIEW/EXPLORE/PLAN/GENERAL all graduated per CLAUDE.md) — minor anchor gap; only in `memory/project_phase_b_*.md`. Could absorb into §32 in a future hygiene pass; deferred as low-leverage
- **Gap #4 / #6 / #7 / #8 closures** (event-bus webhook / IDE Observability / MCP forwarding / live context auto-compaction) — referenced as substrate in §32 but no §X-anchored arc rows. Deferred as low-leverage

## Architectural significance

The hygiene pass is itself a meta-Reverse-Russian-Doll move: the PRD is the immune system's documentation; bringing the PRD to actual codebase parity is the immune system catching up to the spawning core. The 3 new sections (§33-§35) make crystallized practice **referenceable by name** so future arcs inherit the discipline by anchored-citation, not by re-derivation from scattered brutal-review prose.
