---
title: Project Oracle Cache Oom Hardening
modules: [tests/battle_test/test_oracle_cache_symmetry.py, tests/battle_test/test_process_memory_watchdog.py, tests/battle_test/test_oracle_cache_atomic.py, backend/core/ouroboros/oracle.py, backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_oracle_cache_oom_hardening.md
---

**The 52GB soak OOM (2026-05-17/18) — root cause + structural fix.**
Recurring macOS "out of application memory" (Terminal process tree
~52GB) during ~2h ouroboros battle-test soaks.

**Root cause (3 compounding defects, diagnosed via 3 parallel agents):**
1. **Oracle cache load/save path asymmetry** (the actual leak).
   `oracle.py::_load_cache` read the *primary*
   `~/.jarvis/oracle/codebase_graph.pkl` directly; `_save_cache` wrote
   through `sandbox_fallback()`. Under the Iron Gate the primary is
   non-writable → every save landed in
   `.ouroboros/state/sandbox_fallback/oracle/` while every load looked
   at the stale/absent primary → **cold full reindex of 24,735 files
   on EVERY sandboxed boot**, crawling at ~1 file/s (quiescence parks
   each 50-file batch ≤420s behind the always-in-flight provider
   stream), never converging, never saving, unbounded partial
   `nx.DiGraph` accreting for the whole soak → OOM.
2. **MemoryPressureGate is structurally blind**: probes *system-wide
   free %* (host stayed 71% free), only clamps NEW L3 fan-out, never
   wired into the harness. A single-process leak is invisible to it.
3. **Logging double-attach**: `harness.py` legacy FileHandler block
   added a 2nd root FileHandler to the same `debug.log` that
   `silent_boot` already installed (every line emitted 2×; static,
   not the leak, but real).

**Fix shipped (operator-bound plan: Arc A+B before any soak; Arc C
deferred). All on branch ouroboros/battle-test/20260518-052225.**

- **Arc A (surgical, kills repeat cold reindex):**
  `TheOracle._resolved_graph_cache_path()` = single source of truth
  (`sandbox_fallback(GRAPH_CACHE_FILE)`, idempotent/cached). `_load_cache`
  + `_save_cache` + `get_status` all route through it → load/save
  symmetric (primary in dev, same fallback under Iron Gate). Harness
  legacy FileHandler block gated behind `silent_boot._HANDLER_MARKER`
  (its own single-source-of-truth) — runs only as genuine fallback.
  Spine: `tests/battle_test/test_oracle_cache_symmetry.py` (7 tests).
- **Arc B (architectural hardening):** monotonic per-batch
  `_save_cache` checkpoint in `_index_repository` cold build
  (`JARVIS_ORACLE_CHECKPOINT_EVERY_N_BATCHES`, default 1 = every
  batch; quiescence preserved). New **ProcessMemoryWatchdog** in
  harness mirroring WallClockWatchdog: async monitor + thread backstop
  (Oracle indexing is the known event-loop suffocator) probing process
  *tree* RSS (psutil sum self+children → `getrusage` fallback);
  adaptive cap = fraction of total RAM (no hardcoded bytes), env
  knobs `JARVIS_PROCESS_MEMORY_{WARN_MB,CAP_MB,CAP_FRACTION,
  WATCHDOG_INTERVAL_S,WATCHDOG_ENABLED}`. On WARN: proactive Oracle
  checkpoint (hysteresis). On CAP: stamp `stop_reason=
  process_memory_cap`, termination-hook partial summary, bounded
  shutdown watchdog arm, final checkpoint, join 5-way FIRST_COMPLETED
  race. New `TerminationCause.PROCESS_MEMORY_CAP`. Spine:
  `tests/battle_test/test_process_memory_watchdog.py` (13 tests).
- 20 new tests green; 102 pre-existing oracle/harness tests still
  green (zero regression).

**Validation soak (bt-2026-05-18-062703, --max-wall-seconds 2400
--headless --cost-cap 0.50) — CHECKLIST RESOLVED 4/5 PASS,
empirical:**
- RSS trajectory over the *exact cold-index path that previously hit
  52GB*: sawtooth 365MB–2546MB, **peak 2546MB vs 12288MB cap**, no
  unbounded trend across 40+ min. (1) no kernel OOM-kill ✅ (2) RSS
  bounded, WARN/CAP correctly never tripped ✅ (3) single-emit ✅
  (5/2234 incidental adjacent dups vs old ~100% doubling) (5) watchdog
  armed `warn=10445 cap=12288 interval=15s` adaptive = 0.75×16GB host
  RAM (no hardcoding) + 5 heartbeat/alive lines ✅.
- (4) **PARTIAL/FAIL on cache-reload sub-criterion**: Oracle
  checkpoints monotonic 38→136+ (Arc B working, durable progress ✅)
  BUT post-run `_load_cache()` failed `invalid load key '\x00'` on a
  172MB file — **the non-atomic `_save_cache` was torn mid-write by
  the SIGTERM/os._exit(75) kill path**. Checkpoint cadence + bounded-
  shutdown exit makes the torn-write window real and recurring →
  defeats checkpoint durability and BLOCKS graduation proof #6
  (next-boot cache HIT). Index did not complete (quiescence-throttled
  ~2–3 files/s, ~6850/24834 — preserved by mandate; wall-cap was the
  intended stop). Operator ended early via SIGTERM
  (`stop_reason=sigterm+atexit_fallback`, `session_outcome=
  incomplete_kill`, partial summary.json written ✅) to pivot to a new
  priority.
**Arc B.1 — atomic `_save_cache` (SHIPPED, the root completion):**
`oracle._save_cache` now serializes into a same-dir `mkstemp` temp
then `os.replace` (POSIX-atomic) via `_resolved_graph_cache_path()`;
unlink-on-failure; `_tmp_name` guard. Inline bytes variant mirroring
`dw_heavy_probe._atomic_write` / `swe_bench_pro/dataset_loader`
`_write_cache` (NO new shared helper — codebase already has ~8
text-only `_atomic_write` variants; adding a 9th would be the
duplication we were told to avoid). Spine:
`tests/battle_test/test_oracle_cache_atomic.py` (4 tests incl.
kill-at-rename-boundary). 24 cumulative new + 80 regression green.

**OOM ARC FULLY CLOSED — all 6 criteria PASS, empirical:**
- (1) no kernel OOM-kill ✅ (2) RSS peak 2546MB vs 12288MB cap ✅
  (3) single-emit 1/438 boot2, 5/2234 prior vs old ~100% ✅
  (5) watchdog armed adaptive + heartbeats ✅
- (4) **NOW PASS**: graduation boot1 atomic-checkpointed 27×, was
  **SIGKILL'd** (uncatchable, harshest), resolved cache reloaded
  clean at **87,753 nodes / 1,710 hashes, no `\x00`, no `.tmp`
  orphan**. `os.replace` makes a torn cache structurally impossible.
- (6) **GRADUATION PROVEN**: boot2 (same machine, fresh process)
  `[Oracle.boot] initialize complete elapsed_ms=2822.0
  cache_loaded=True graph_nodes=87753 graph_edges=188666` — a
  **2.8s cache HIT** of the SIGKILL'd boot1 checkpoint, NOT a
  24,835-file cold reindex. The cold-reindex-every-boot→OOM loop is
  eliminated; chain compounds: Arc A symmetry + Arc B checkpoint +
  Arc B.1 atomic.

**P5 Arc C — SHIPPED 2026-05-18 (closes the dual blind spot).**
Operator-approved w/ Amendment A (gate SELF-probes — production
correctness must not depend on harness push) + Amendment B
(usage-vs-cap: cap = total_ram × `JARVIS_MEMORY_PRESSURE_PROCESS_
FRACTION` default 0.75; WARN/HIGH/CRITICAL = env fractions OF cap
0.85/0.92/0.98). Sliced: **5a** `governance/process_tree_probe.py`
shared module (probe extracted verbatim; harness
`_probe_process_tree_rss_mb` now a thin delegate — watchdog
behavior byte-stable; parity + no-second-impl AST pins). **5b**
MemoryPressureGate self-probes via the shared fn inside
`_process_tree_dim()`; `_strictest()` strictest-wins compose into
`pressure()` + `can_fanout()`; additive FanoutDecision fields
(`process_level/rss_mb/cap_mb/dominant_dimension`) + `to_dict` +
`snapshot().process_tree`; reason gets `_via_process_tree` suffix
ONLY when process dim escalated (legacy free-% reason byte-
identical). **5c** 5 `JARVIS_MEMORY_PRESSURE_PROCESS_*` FlagSpecs
in the gate's OWN register block (master `_DIM_ENABLED`
default-FALSE §33; no flag_registry_seed dup); reason_code +
GET/governor auto-surface the additive fields (no new wiring/event).
**5d/5e** 28 tests: usage-vs-cap matrix, strictest-wins both
directions, **flag-off byte-identical** regression, fail-open
(probe glitch never clamps — watchdog stays hard-stop), AST pins
(gate composes shared probe / gate NOT merged with watchdog —
import+os._exit check), **graduation proof: process-dim HIGH
clamps fan-out while system free-% is OK, advisory never blocks**.
Watchdog=terminate+summary (authority) and Gate=clamp fan-out
(advisory) remain separate — never merged. 244/244 broad scoped
regression green (incl. gate legacy suite + parallel_dispatch +
sensor_governor consumers). Master flag stays default-FALSE until
operator flips post a real soak (synthetic ramp proven
deterministic at unit level — 5e). Out of P5 scope (noted):
SensorGovernor op/hour weighting (separate arc).

**Observed 2026-05-18 (NOT root-caused — candidate follow-up, do
not over-claim):** post-merge soak `bt-2026-05-18-210054` (main
d08a48718a) banked its PASS verdict normally, but its wall-clock-cap
bounded-shutdown then ran **46+ min past the 2400s cap** without
`os._exit` — the BoundedShutdownWatchdog grace (~30s) was far
exceeded; a second `ouroboros_battle_test` proc also lingered.
Operator-terminated (`pkill -9`) + stale `.jarvis/intake_router.lock`
removed to unblock SWE-Bench prep. Verdict already captured so not
blocking, but the wall-cap shutdown path appears wedge-prone under
some end-state — distinct from Arc B.1; worth a focused repro/
diagnosis (which subprocess/await holds shutdown past grace). Not
investigated this session (scope).

**Status: ARC CLOSED.** Minor non-blocking note for deferred track:
a SIGKILL landing *exactly* mid-`write_bytes` can orphan one `*.tmp`
(uncatchable kill ⇒ no `except` cleanup) — harmless (load only reads
the final resolved path) but a boot-time `*.tmp` sweep would be
tidy; NOT corrupting, NOT in scope. Arc C (process-RSS into
MemoryPressureGate + harness wire) remains the deferred parallel
track per operator priority 5.

**Found follow-ups (NOT in Arc A/B scope — separate tracks):**
- `_save_cache` is **not atomic**: a kill mid-`write_bytes` leaves a
  corrupt pkl (`Error loading cache: invalid load key, '\x00'`). The
  pre-fix OOM corrupted the artifact; first post-fix boot must cold-
  rebuild once (Arc B checkpoint then makes it durable). Real
  hardening gap → atomic write (tmp + os.replace). Candidate Arc C /
  follow-up.
- Pre-existing unrelated bug: `FlagRegistry.register() got an
  unexpected keyword argument 'name'` at
  `verification/multi_prior_runner.py:872` (non-fatal at boot; NOT
  mine, NOT in scope).
- SWE-bench `psf__requests-3362 prepare_failed` (git apply test_patch
  worktree/base mismatch) is the operator's explicitly-deferred
  separate track — orthogonal to the OOM fix.

See [[project-rubric-soak-profile]], [[no-pre-result-euphoria]].
