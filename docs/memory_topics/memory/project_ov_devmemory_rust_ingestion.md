---
title: Project Ov Devmemory Rust Ingestion
modules: [tests/governance/test_strategic_dev_memory.py, tests/governance/test_strategic_rust_map.py, backend/core/ouroboros/oracle.py, orchestrator.py, backend/core/ouroboros/governance/strategic_direction.py, backend/core/ouroboros/governance/flag_registry_seed.py, backend/core/ouroboros/roadmap/source_crawlers.py, backend/core/ouroboros/roadmap/snapshot.py, tests/core/ouroboros/roadmap/test_snapshot.py, backend/core/ouroboros/battle_test/harness.py, backend/core/ouroboros/governance/phase_runners/generate_runner.py, backend/core/ouroboros/governance/providers.py]
status: merged
source: project_ov_devmemory_rust_ingestion.md
---

**Operator priority arc (after the OOM-hardening arc closed).** Goal:
let O+V leverage curated repo knowledge + Rust during battle-test/soak
runs. Discovery established: O+V is structurally Rust-blind (Oracle
double-gated Python-only: hardcoded `SUPPORTED_EXTENSIONS={".py"}`
oracle.py:180 + CPython `ast.parse` CodeStructureVisitor); and no O+V
code reads the git-tracked `./memory/` dir. Natural seam (no
duplication): `StrategicDirectionService` — designed for exactly
"curated durable repo knowledge → every GENERATE prompt", already
booted in soak + injected at orchestrator.py:2137.

Operator-bound priority order: P1 Arc B.1 atomic _save_cache ✅DONE
(see [[project-oracle-cache-oom-hardening]]) · P2 graduation soak
✅CLOSED · **P3 dev-memory ✅CODE-COMPLETE (this file)** · P4 Rust
Option-1 awareness (NOT started — must not begin in P3's commit) ·
P5 Arc C deferred (process-RSS into MemoryPressureGate).

**P3 — Developer-Memory injection (CODE-COMPLETE, NOT yet graduated):**
- `strategic_direction.py`: new instance method
  `_render_dev_memory_section()` composes the EXISTING
  `roadmap.source_crawlers.crawl_memory(self._root)` crawler verbatim
  (NO glob duplication — AST-pinned), recency-ranks fragments by
  `mtime` desc, accumulates title+summary under env budgets, returns
  an advisory authority-free `## Recent Developer Memory` block.
  Wired into `format_for_prompt()` BEFORE the causal-lineage block
  (preserves the existing "causal closest to generation" design).
  Mirrors the codebase-character / failure-mode additive-section
  discipline: fail-silent, ImportError-safe, master-flag-gated.
- 3 flags registered in `flag_registry_seed.py`:
  `JARVIS_STRATEGIC_DEV_MEMORY_ENABLED` (BOOL, **default False**),
  `_MAX_CHARS` (INT 6000), `_MAX_FILES` (INT 8).
- Spine: `tests/governance/test_strategic_dev_memory.py` (10 tests).
  Scoped regression GREEN: **149/149** across test_strategic_dev_memory
  + codebase_character_slice2_strategic_direction +
  strategic_direction_git + flag_registry{,_seed_truth,_graduation}.
  Zero regression.

**P3 GRADUATION GATE (NOT met — code-complete ≠ graduated):** the
master flag stays **default-False per project convention**. It
graduates to default-True only AFTER one soak run with
`JARVIS_STRATEGIC_DEV_MEMORY_ENABLED=true` explicitly set, proving
the section renders into real GENERATE prompts without harm. Do NOT
flip the default before that soak.

**Regression-sweep hazard (recorded):** `pytest tests/ -k "..."`
WEDGES in full-tree collection on this repo (some unrelated module
blocks at import; `--timeout=120` covers test exec only, NOT
collection — observed a 55-min 0% CPU 0-byte hang). Run scoped
regressions by explicit test-file paths, never `tests/ -k`. The
`strateg` substring also false-matches selection_strategies /
saga_apply_strategy / exploration_strategy / tts_strategy_race —
exclude those; the real suites are the 6 strategic_direction +
flag_registry files.

**P4 — Rust awareness, Option-1 (CODE-COMPLETE, NOT graduated):**
- `source_crawlers.py`: new `crawl_rust_subsystems(repo_root)`
  alongside `crawl_memory` (shared `SnapshotFragment` discipline).
  Dynamic Cargo.toml discovery under env
  `JARVIS_STRATEGIC_RUST_SEARCH_ROOT` (default `backend`); skips
  `target/ .git/ worktrees/ .worktrees/ node_modules/`; parses
  `[package].name/.description` via stdlib `tomllib` (regex
  fallback — NO new dep); summary precedence README-first-
  meaningful-line → description → crate path; dedup-by-name; env
  cap `JARVIS_STRATEGIC_RUST_MAX_CRATES` (12). Real-repo smoke: 6
  first-party crates, build artifacts/worktrees excluded.
  `_parse_cargo_package` helper added.
- `snapshot.py`: `VALID_FRAGMENT_TYPES` += `"rust_crate"` (memory
  type NOT overloaded). `test_snapshot.py` extended.
- `strategic_direction.py`: `_render_rust_subsystems_section()`
  mirrors dev-memory discipline (flag-gated, fail-silent,
  ImportError-safe, authority-free), composes
  `crawl_rust_subsystems` only (AST-pinned: no Cargo.toml glob
  call in strategic_direction.py), includes the MANDATED explicit
  line "Oracle structural graph is Python-only; use Venom tools
  for .rs — no blast-radius in graph yet". Wired in
  `format_for_prompt()` AFTER dev-memory, BEFORE causal-lineage
  (recency design preserved).
- 3 flags in `flag_registry_seed.py`:
  `JARVIS_STRATEGIC_RUST_MAP_ENABLED` (BOOL **default False**),
  `_MAX_CHARS` (INT 4000), `_MAX_CRATES` (INT 12).
- Spine: `tests/governance/test_strategic_rust_map.py` (13).
  Scoped regression GREEN: **214/214** across rust_map +
  dev_memory + codebase_character_slice2 + strategic_direction_git
  + flag_registry{,_seed_truth,_graduation} + snapshot +
  source_crawlers. Zero regression.
- Oracle UNCHANGED (SUPPORTED_EXTENSIONS untouched — Option-1
  scope respected; no tree-sitter / no PyO3 runtime wiring).

**P3 GRADUATION SOAK #1 — FAIL (bt-2026-05-18-092457, 2026-05-18).**
`JARVIS_STRATEGIC_DEV_MEMORY_ENABLED=true`, ran ~26min, ended clean
(real GENERATE ops ran: 23 standard + 12 immediate + 5 background).
Proof criterion `grep -c '[Strategic] dev-memory injected'` = **0**.
Root cause (NOT a P3/Slice-0 defect — Slice-0 observability did its
job and revealed a pre-existing gap): the orchestrator
`_run_pipeline` strategic-injection block (~orchestrator.py:2135,
`getattr(gls,"_strategic_direction")` → `format_for_prompt()`)
**never executed** — `[Orchestrator] Strategic direction injected`
count=0, pipeline_markers=1 across the whole soak — so the ENTIRE
StrategicDirection injection (manifesto digest included, not just
dev-memory) is dark in the battle-test generation path. Strategic
Direction *did* load (`[Strategic] Loaded: 7 principles … 4
sources`) and the harness DOES set `gls._strategic_direction`
(harness.py:1903); the smoke test passed because it called
`format_for_prompt()` directly, bypassing the orchestrator gating.
Candidates to confirm: (a) battle-test GENERATE uses
`generate_runner.py` / route-optimized path that bypasses the
`_run_pipeline` strategic region; (b) `JARVIS_LEAN_PROMPT`
default-`true` (providers.py:2002 / `_should_use_lean_prompt`
:4177) skips strategic assembly; (c) a phase/branch short-circuit.
Regression watch PASS: Oracle cache HIT 2.6s / 87,753 nodes (Arc
A/B/B.1 holds), RSS bounded sawtooth peak 2.4GB ≪ 12GB cap, single-
emit 12/11661. **P3 NOT graduated. Step 2 (default-flip) BLOCKED
until the injection-path gap is root-caused + fixed + a re-soak
shows ≥1 proof line.** Task #11 owns the diagnosis. Do NOT flip
`JARVIS_STRATEGIC_DEV_MEMORY_ENABLED` default. P4 soak (Step 3)
also blocked (same injection path).

**Task #11 — StrategicDirection dark-injection root cause: CLOSED
(2026-05-18, Phase C PASS).** Root cause: `GovernedLoopService.
_attach_to_stack()` bound orchestrator/generator/approval_provider
to the stack but NEVER `self._stack.governed_loop_service = self`
(only `hud_governance_boot` wrote it — battle-test doesn't use that
boot). So harness set `gls._strategic_direction` while the
production read path (`getattr(stack,"governed_loop_service")
._strategic_direction`) saw None → the ENTIRE StrategicDirection
injection was dark (soak #1 bt-2026-05-18-092457: injected=0,
failed=0 = skipped not raised). Fix (single source of truth, no
orchestrator-side duplicate holder): GLS self-registers in
`_attach_to_stack` / clears in `_detach_from_stack`. **Critical
lesson:** the strategic-injection block is *pre-duplicated* across
`orchestrator.py:~2137` AND `phase_runners/classify_runner.py:~579`
— the battle-test ops traverse the **classify_runner** path. First
Phase A wrongly grepped `governance/classify_runner.py` (does not
exist) instead of `governance/phase_runners/classify_runner.py`,
concluded "no strategic path / parity fix N/A" — soak #1 caught it
(empty `op_id`, old DEBUG). Both sites now thread
`op_id=getattr(ctx,"op_id",None)` into `format_for_prompt` and log
the success line at INFO (was DEBUG — graduation greps must not
depend on DEBUG, §7). Spine `test_strategic_injection_wiring.py`:
attach/detach unit + AST pin (both assignments) + end-to-end
wired-stack→telemetry + **parametrized both-sites AST pin**
(op_id-threaded + INFO, no old-DEBUG). The 2 GLS-startup
`re-entrancy` fails are PRE-EXISTING (proven identical on clean
HEAD via `git stash` isolation) — NOT this change. Phase C re-run
(soak bt-2026-05-18-185740) PASS: dev-mem injected 3× w/ non-empty
`op=op-…-cau`, INFO strat 3× new format, `old_debug=0`, Oracle
cache-HIT 2.0s/87753 (Arc A/B/B.1 holds), single-emit clean, clean
end. 242/242 light scoped regression green. Infra: heavy GLS test
suites (`test_governed_loop_service`/`_startup`) WEDGE in
collection — run scoped, bound with a watchdog, never `tests/ -k`.

**P3 GRADUATED (Step 2 — DONE 2026-05-18).**
`JARVIS_STRATEGIC_DEV_MEMORY_ENABLED` default flipped false→true:
strategic_direction.py env-read default `"true"` (graduation
comment cites soak bt-2026-05-18-185740) + flag_registry_seed
`default=True`/`example="true"`/desc "GRADUATED" + **default-true
AST persistence pin** (`test_ast_pin_graduated_default_true_persists`
— refactor can't silently revert) + old default-False tests
rewritten to graduation semantics (`test_graduated_default_true_
injects_without_flag`, `test_explicit_disable_hot_reverts_to_empty`).
Hot-revert = `env=false` ONLY, no code-path deleted (mirrors
JARVIS_STRATEGIC_GIT_HISTORY_ENABLED shape). 251/251 light scoped
regression green.

**P4 GRADUATED (Step 4 — DONE 2026-05-18).** Phase D soak
bt-2026-05-18-194040 PASS: `[Strategic] rust-map injected` 2× w/
non-empty `op=op-…-cau crates=6 chars=992`, INFO strat fired, P3
dev-memory also live (2×, graduation confirmed in-soak),
Oracle-Python-only block reached prompt, cache-HIT 5.1s/87753.
`JARVIS_STRATEGIC_RUST_MAP_ENABLED` flipped false→true (mirrors P3
exactly: env-read default `"true"` + flag_registry `default=True`/
`example="true"`/"GRADUATED" + default-true AST persistence pin +
old default-False rust tests → graduation semantics; hot-revert
env=false only). 284/284 light scoped regression green.

**P3 + P4 BOTH GRADUATED.** P5 Arc C shipped (see
[[project-oracle-cache-oom-hardening]]).

**MERGED TO MAIN 2026-05-18.** PR **#39314** squash-merged →
`main` SHA **`d08a48718a`** (local == origin/main). Carries OOM
hardening (Arc A/B/B.1) + Task #11 GLS stack self-registration +
P3/P4 graduated defaults + Slice 0 telemetry + P5 Arc C process-
tree pressure dim. Merge gate: CodeQL Analyze(python)=success +
Run Tests 3.10/3.11 PASS authoritative; operator infra-waiver for
3 non-blocking reds (Vercel account-block / Comprehensive-CI
whole-repo `black` concurrency-cancel + 2697 pre-existing drift /
Database-Connection .env.example) — none in BLOCKING set, none our
16 files. **Post-merge soak `bt-2026-05-18-210054` (main, P3/P4
default-on) PASS:** cache-HIT 3.0s/87753, dev-memory injected 2×,
rust-map injected 2× (`op=op-019e3ca4-…-cau crates=6`), INFO strat
2×, RSS sawtooth peak ~3040MB ≪ 12288MB cap, no process_memory_cap,
single-emit clean, ~41min clean run. 127/127 on-main regression
spot-check green. Operator follow-up (separate post-merge PR, NOT
done here): harden Comprehensive-CI `code-quality` job — scope
black to changed files + concurrency group; don't make whole-repo
black a required signal when diff-scoped black passes.

**Slice 0 — injection telemetry (SHIPPED, unblocks graduation
soaks).** Root finding: the assembled GENERATE prompt is NEVER
persisted to session artifacts (no prompt capture in providers /
candidate_generator / comm_protocol / orchestrator) → a soak alone
could not grep-prove injection. Operator chose Option 1 (observability
micro-slice, §7). Implemented: `_render_dev_memory_section` /
`_render_rust_subsystems_section` now take `op_id`, and on non-empty
injection emit ONE INFO line — `[Strategic] dev-memory injected
op=<id> files=N chars=C` / `[Strategic] rust-map injected op=<id>
crates=N chars=C` — **counts only, NO titles/summaries/URIs**
(operator memory/ may be sensitive). Module-level
`_publish_strategic_injection()` best-effort SSE (mirrors
`publish_failure_mode_recalled`: lazy broker import, never raises);
2 event types `strategic_dev_memory_injected` /
`strategic_rust_map_injected` registered in
`ide_observability_stream._VALID_EVENT_TYPES`. `op_id` threaded:
`format_for_prompt(op_id=...)` (param already existed) ← orchestrator
`:2137` now passes `getattr(ctx,"op_id",None)`. Spine: telemetry
tests added to both files (fires when enabled+fragments / silent off
or empty / **counts-only-no-body** / AST pin forbids logging
block/joined/title/summary/uri). Scoped regression **273/273** green
(incl. test_ide_observability_stream + test_source_crawlers +
test_snapshot). Grep targets for the soaks:
`grep -c '\[Strategic\] dev-memory injected'` (P3) /
`'\[Strategic\] rust-map injected'` (P4) in session debug.log.

Out of scope this arc (unchanged): SWE-bench prepare_failed,
FlagRegistry `name=` bug, full Oracle .rs graph (Option-2
tree-sitter), Option-3 PyO3 runtime wiring. P5 Arc C
(process-RSS into MemoryPressureGate + harness wire) remains the
deferred parallel track.

**Deep capability run — INCONCLUSIVE + #21 drain_buffer finding
(2026-05-18, soak bt-2026-05-19-011003, element-web only, wall
5400s, cost $5, EVAL_TIMEOUT_S=86400 sentinel).** ✅ **Task #21
dynamic clamp EMPIRICALLY PROVEN in production**: `eval timeout
clamped 86400.0s -> 5337.6s (wall_remaining=5397.6 drain=60.0) —
Dynamic Timeout Coherence (Task #21)` — inner eval was 100%
wall-derived, A″ collision now structurally impossible under real
load. ✅ **OOM arc validated at 10× scale**: Oracle cache HIT with
the now-FULL **949,823-node** codebase graph (vs 87,753 prior — the
hardened index finally completed + persisted across soaks), boot
~14s, RSS sawtooth peak ~2.6GB, NO process_memory_cap / NO OOM at
10× graph; dev-mem/rust-map firing. ❌ **NO autoscore verdict at
all** (not RESOLVED, not terminal_timeout): inner eval timed out
~60s pre-wall per #21 design, but bounded-shutdown +
parallel_evaluate-yield + harness_inject verdict-log +
await_autoscore_drain **exceeded the 60s drain_buffer** under the
heavy 950k-node 90-min session → session terminated in the gap, no
summary.json, no stop_reason line (Task #20 wedged-shutdown class).
**#21 mechanism CORRECT; drain_buffer (2×autoscore-grace=60s)
UNDERSIZED vs real production teardown latency** → **Task #22**:
_eval_drain_buffer_s must compose the bounded-shutdown watchdog
deadline + autoscore grace (no hardcode), so the inner eval yields
with margin for the FULL real teardown+verdict-flush. Capability
of O+V on element-web STILL UNMEASURED (no verdict). Fix #22 →
re-run deep capability. #19 NOT closed.

**SWE-Bench-Pro Phase B — PARTIAL (2026-05-18, soak
bt-2026-05-18-235748, main d08a48718a).** Pre-soak gates all green:
HF probe `ScaleAI/SWE-bench_Pro` ok (NOT princeton-nlp — doc-drift,
verified); geometric_sampler structural pick good=element-hq/
element-web (1f/22L), hard=ansible/ansible (4f/2028L), third=
qutebrowser (1f/23L), no psf; staged phase_b_discriminator.jsonl;
prepare-gate 3/3 PROD `git apply --index --check` rc=0, 0
prepare_failed. **Task #21 Dynamic Timeout Coherence SHIPPED+proven**
(harness publishes `OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC` at
WallClockWatchdog arm; `evaluator._resolve_timeout_s`→
`_apply_wall_coherence` = min(configured, wall_remaining−drain);
drain composes existing `AUTOSCORE_SHUTDOWN_GRACE_S`×2 env-tunable;
floor 10s; no-deadline→byte-identical legacy; no battle_test import;
13 spine + 29 evaluator-regression green). Phase B live ($1.50, 3
serial, EVAL_TIMEOUT_S=900): **3 autoscore verdicts, 0
prepare_failed, on-main regression GREEN** (cache-HIT 2.0s/87753,
dev-mem=10/rust-map=10, RSS bounded ~3GB peak, clean
`wall_clock_cap`, session_outcome=complete). #21 `clamp_lines=0` is
CORRECT — 3600s wall had headroom for 3×900s serial so min returned
configured (no-op-when-unneeded by design; clamp path unit-proven).
**CRITERION-#2 FAIL: all 3 = `eval_outcome=terminal_timeout
score_outcome=skipped` — O+V solved NONE of the 3 real problems
within the 900s eval window.** Verdict: **rig/loop GRADUATED
end-to-end on real SWE-Bench-Pro; capability bar NOT cleared this
slice (0 real RESOLVED/UNRESOLVED).** Genuine capability/runtime
baseline (real SWE-bench solves commonly ≫15min), NOT a wiring/
harness defect. **#19 NOT closed as PASS.** Phase D (5–10 / $2.00 /
rubric floor) gated on operator decision re: much larger
per-problem eval window + budget — cost-heavy, operator-bound, NOT
auto-escalated.
