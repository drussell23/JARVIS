---
title: Project Section 37 Tier1 3 Ledger Flock
modules: [backend/core/ouroboros/governance, tests/governance/test_section_37_tier1_3_ledger_flock.py]
status: historical
source: project_section_37_tier1_3_ledger_flock.md
---

May 9 2026: §37 Tier 1 row #3 + §35 row 🔴 #2 + §3.6.3 priority #2 ✅ Shipped.

**Adversarial Cage row sync** (§35 #2 + §3.6.3 #2): substrate already
shipped 2026-04-27 (Phase 9.4) — 36 regression tests green, 0/38 open
vectors, 12/38 documented known gaps in §3.6.2 vector #7. Same Vector
#10 stale-row pattern; ~30 min sync.

**§37 Tier 1 #3 closure scope** — 7 production sites migrated to canonical
`cross_process_jsonl.flock_append_line` (Wave 3 v2.26 substrate):

**Type B (legacy → canonical migrations)**:
1. `observability/decision_trace_ledger.py` — primary path now
   `flock_append_line`; legacy `_append_legacy_fileno_flock` retained
   as substrate-unavailable rollback (mirrors `adaptation/ledger.py`'s
   substrate-unavailable contract per `:752`).
2. `adaptation/graduation_ledger.py` — same migration shape.
3. `observability/post_merge_auditor.py` — same migration shape with
   `_persist_outcome_legacy_fallback`.

**Type A (true gaps — no cross-process flock pre-Wave-3)**:
4. `intake/wal.py::_write_line` — sensor write-ahead log used by 16
   sensors concurrently within a single session.
5. `posture_store.py::append_audit` — §8 immutable audit log;
   within-process `threading.Lock` retained as complementary fence.
6. `mutation_gate.py` — mutation budget ledger; module-level
   `_ledger_lock` retained as complementary fence.
7. `metrics_history.py` — telemetry ledger.

**Already-canonical sites** (verified clean): `auto_action_router.py:1110`
composes `flock_append_line`; `adaptation/ledger.py:717` composes
`flock_critical_section` per Wave 3 v2.26. The §37 row description
naming these files was based on a stale audit.

**Load-bearing AST pin** (`test_every_open_append_is_flock_or_allowlisted`):
walks every `.py` under `backend/core/ouroboros/governance`, detects
`path.open("a", encoding="utf-8")` shape via regex, asserts the file
EITHER composes a flock primitive (`flock_append_line` /
`flock_append_lines` / `flock_critical_section` /
`async_flock_critical_section` / legacy `flock_exclusive`) OR is on a
20-entry bytes-pinned `_OPEN_APPEND_ALLOWLIST` with one-line rationale.
Single-canonical-name discipline at the substrate level — silent drift
becomes reviewer-visible decision.

**Allowlist taxonomy** (20 entries, each with rationale):
- 1 substrate self (`cross_process_jsonl.py` — open('a') is the
  primitive's implementation)
- 1 alternate fcntl pattern (`adaptation/yaml_writer.py` — owns local
  fcntl-via-lock-handle pattern)
- 4 single-process REPL helpers (chat_repl_*, backlog_auto_proposed_repl,
  inline_approval_provider)
- 14 per-session writers / rollup readers whose producers compose flock
  at the original write site (postmortem_recall, curiosity_engine,
  cognitive_metrics, hypothesis_ledger, composite_score, etc.)

Allowlist size pin (`test_allowlist_size_pinned`) forces reviewer
attention on additions — adding a new entry requires updating BOTH
the dict AND the size assertion.

**43 regression tests** in
`tests/governance/test_section_37_tier1_3_ledger_flock.py`:
- 7 migrated-file canonical-substrate-composes pin tests (positive)
- 7 migrated-file substrate-import pin tests
- 20 allowlist anti-stale tests (every allowlisted file MUST still have
  open('a'))
- 1 allowlist-size pin
- 1 load-bearing AST sweep
- 1 true cross-process race via `multiprocessing.get_context("fork").Process`
  — 2 children × 50 lines each → all 100 lines present (no lost writes),
  every line JSON-parseable (no torn bytes from interleaved writes).
  Mirrors Vector #10 v2.79 multiprocess pattern.
- 3 substrate-unavailable rollback contract pins (each migrated site
  has an `ImportError`-handled legacy fallback)
- 3 end-to-end functional smokes (each migrated producer surface writes
  a row AND the sibling `.lock` file is created — load-bearing
  assertion that proves canonical path was taken, not legacy fallback)

**Authority test sync** in `test_item_4_graduation_cadences.py`:
- `test_ledger_only_stdlib_and_adaptation` — allowlist extended to
  permit `cross_process_jsonl` substrate import (previously enforced
  "adaptation"-only). Also added pre-existing graduation/runner_kind +
  graduation/lineage_waiver siblings.
- `test_ledger_uses_flock` — now accepts `flock_append_line` OR
  `flock_exclusive` (canonical OR legacy fallback both satisfy
  cross-process invariant).

**Test results**: 43/43 §37 flock + **649/649 cumulative** across §37
flock + Phase 8 + P9.5 + Vector #5 Part B + 7 consumer files +
graduation_ledger authority tests + canonical sources.

**Architecture preserved**:
- ZERO parallel locking machinery — every site composes Wave 3 v2.26
  canonical substrate
- Within-process `threading.Lock` retained as complementary fence
  where present (intra-process emission still serialized; flock adds
  the missing inter-process serialization)
- Substrate-unavailable rollback at every migrated site (NEVER raises
  on fcntl-unavailable platforms)
- §38.11.5a.5 single-canonical-name discipline: ZERO duplicate flock
  helpers; allowlist exits force reviewer attention

**Operator binding satisfied verbatim**: solved root problem (cross-
process race directly closed at 7 load-bearing sites); no workarounds
(no parallel locking, no within-process-only fences); no shortcuts
(load-bearing AST pin closes future drift); fully leverages existing
architecture (composes Wave 3 v2.26 substrate); no hardcoding
(allowlist with rationales, not site list); strengthened (cross-
process race coverage in regression spine via multiprocessing.Process).

**NEXT** (autonomy arc remaining):
- **§37 Tier 1 #1** Confidence drop SSE producer wiring (~2-3d, near-
  clone of Vector #5 Part B pattern)
- **§37 Tier 1 #2** PostureObserver task-death detection (~3-5d,
  closes worst silent-degradation cascade)
- **§35 row 🟡 #4 / §3.6.3 #4** Cross-runner artifact contract
  schema-versioned (~3-5d)
- **Phase 9 graduation cadence** ~6-9 weeks operator-paced
