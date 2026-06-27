# Resource Governor / Anti-Wedge Engine — Design Spec

**Date:** 2026-06-26
**Author:** Derek J. Russell (directing) + O+V
**Branch:** `feature/resource-governor-anti-wedge`
**Status:** Design — pending user review before writing-plans

---

## 1. Problem

The omni soak wedges the GCP node (e2-standard-8, 32GB / 8 vCPU) at "soak-launch."
Symptoms across v4–v7: the phase marker + heartbeat freeze; ~1809s later the 1800s
per-phase stall watchdog fires; SSH times out at autopsy; **no resource evidence
survives**. Seven runs, the swarm has never fanned out — each died on a different
wall before reaching `[MetaGoal]`.

### Corrected diagnosis (from recon — supersedes the "17 sensors ignite simultaneously" model)

1. **Boot is already sequential.** The 6-layer stack boots with sequential `await`s
   (`harness.py:1044-1101`); sensors start in a sequential `for sensor: await
   sensor.start()` loop (`intake_layer_service.py:1030-1031`); the message bus boots
   *before* sensors (`intake_layer_service.py:944-988`). The concurrency is that each
   `start()` spawns a background **poll-loop task** (`asyncio.create_task`), so ~17
   poll loops + `SubagentScheduler` + `WorktreeManager` all go *live* at once —
   several with 120–300s boot delays that **collide at t=0**.

2. **The safety net already exists but is too slow/late.** The harness arms a
   process-memory watchdog **by default** (`JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED=true`,
   cap = `0.75 × total_RAM` ≈ 24GB; `harness.py:1395-1420`, `:6380-6434`): an async
   monitor at 15s (`_monitor_process_memory`, `:6526`), a thread backstop at ~30s
   (`_start_process_memory_hard_deadline_thread`, `:6584`), and a graceful pre-OOM
   cap-fire (`_fire_process_memory_cap`, `:6470`). **It was armed and still produced
   zero artifacts** → it is too slow (15s/30s poll) and too late (0.75 threshold) for
   this wedge. A fast boot-time allocation spike, or swap-thrash that begins well
   before 24GB RSS, blows past it; once the box thrashes, even the thread backstop
   can't get scheduled to write the autopsy.

### Cause is officially UNVERIFIED

It could be a RAM spike, disk fill (worktrees/fastembed cache), or CPU/context-switch
thrash. **We confirm it with a local baseline run (throttle OFF) before trusting the
fix.** The enriched Death Rattle (Piece 3) is the diagnostic that captures the death
curve we keep losing.

---

## 2. Reuse map — what already exists (extend, never duplicate)

| Capability | Existing module | Key API / line |
|---|---|---|
| RAM probe cascade (psutil → /proc/meminfo → vm_stat) | `governance/memory_pressure_gate.py` (866 ln) | `_probe_psutil` `:267`, `_probe_proc_meminfo` `:290`, `_probe_vm_stat` `:329`; `pressure()` `:458`; `can_fanout()` `:540` |
| 4-level pressure enum + per-level fan-out caps | `memory_pressure_gate.py` | `PressureLevel` `:193`; thresholds `:38-47`; `FanoutDecision` `:227` |
| Gate already wired to worker fan-out | `autonomy/subagent_scheduler.py`, `governance/parallel_dispatch.py` | `gate.can_fanout(n)` (live L3 dispatch) |
| Process-tree RSS sum | `governance/process_tree_probe.py` | `probe_process_tree_rss_mb()` `:26` |
| Process-memory watchdog (async + thread backstop + graceful cap-fire) | `battle_test/harness.py` | `:1395-1420`, `_monitor_process_memory` `:6526`, thread backstop `:6584`, `_fire_process_memory_cap` `:6470`, threshold resolver `:6380` |
| `faulthandler` enabled (SIGUSR1 → all-thread dump) | `unified_supervisor.py` | `:253-258` |
| Atomic JSON write (tempfile + `os.replace`) | `sovereign_iac_hypervisor.py` `CheckpointLedger.write` | `:494-515` |
| Signal/atexit partial-summary writers | `harness.py` | `register_signal_handlers` `:5233`, `_atexit_fallback_write` `:611-759` |
| Bash heartbeat death-rattle (atomic mv pattern) | `sovereign_iac_hypervisor.py` | `:1811-1895` |

**Authority invariant (load-bearing):** `MemoryPressureGate` is grep-pinned to never
import any scheduler/sensor/orchestrator/policy module — callers pull from it. All
changes below preserve this: sensors *ask* the gate (Piece 4); the gate never reaches
into sensors.

---

## 3. The five pieces (with the four advanced constraints woven in)

All new behavior is **gated default-OFF** and **byte-identical when off** — the
codebase norm. Master switch: `JARVIS_RESOURCE_GOVERNOR_ENABLED` (default `false`).

### Piece 1 — CPU + context-switch dimension in `MemoryPressureGate`

**Gap:** the gate is RAM-only.
**Change (in `memory_pressure_gate.py`):**
- Add a CPU/ctx-switch sample to the probe. The gate is a singleton → it holds
  `_last_cpu_sample = (cpu_pct, ctx_switches, monotonic_ts)` between calls.
- `psutil.cpu_percent(interval=None)` (non-blocking; first call returns `0.0`, so we
  seed one sample at gate init and always compute against the cached prior).
- `psutil.cpu_stats().ctx_switches` is a **monotonic counter** → compute
  **per-second rate** = `Δctx / Δt`, compared to a **rolling baseline** (EWMA), not a
  hardcoded N (Constraint #3 / no-magic-numbers).
- **Pressure escalation rule (Constraint #3):** the **ctx-switch rate is the PRIMARY
  thrash signal** (verified reliable on macOS: `cpu_stats().ctx_switches` is a clean
  monotonic counter). If `ctx_switch_rate > baseline × CTX_SPIKE_MULT` (default 3.0),
  the gate returns **CRITICAL even if RAM free is at 60%** — swap-thrash signature.
  `cpu_pct` is a **secondary/best-effort** signal only: verification showed
  `psutil.cpu_percent(interval=None)` can return `0.0` even on a second call (macOS
  idle quirk), so it must never be the sole trigger — it only *reinforces* a ctx-rate
  signal or contributes via `cpu_pct >= CPU_CRITICAL_PCT` when it reads non-zero.
- Composition: `effective_level = strictest(ram_level, cpu_level)` — preserves the
  existing "strictest wins" idiom (`:208-211`).
- macOS note: `cpu_stats().ctx_switches` is available and reliable on Darwin;
  `vm_stat` remains the memory ground-truth. Falls back gracefully if a field is
  missing (probe-cascade discipline).

**New env (all default-preserving):**
`JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED` (default `false`),
`JARVIS_RESOURCE_GOVERNOR_CPU_CRITICAL_PCT` (95),
`JARVIS_RESOURCE_GOVERNOR_CPU_HIGH_PCT` (80),
`JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT` (3.0),
`JARVIS_RESOURCE_GOVERNOR_CTX_BASELINE_HALFLIFE_S` (30).

### Piece 2 — Adaptive non-linear polling (harness watchdog)

**Gap:** fixed 15s async / ~30s thread interval — too slow near the event horizon.
**Change (in `harness.py`):**
- The async monitor's sleep becomes a **function of the last observed pressure level**
  (Constraint #2): OK→10s, WARN→3s, HIGH→0.5s, CRITICAL→0.2s (all env-tunable). As
  pressure rises, situational awareness accelerates; at OK it backs off to save CPU.
- The **thread backstop keeps a fixed aggressive floor** (the starvation-immune
  layer must not depend on the same adaptive state it's guarding) — e.g. 1s, env
  `JARVIS_RESOURCE_GOVERNOR_BACKSTOP_INTERVAL_S`. Rationale mirrors the Watchdog
  Isolation Invariant: the immune layer reads only raw clocks/probes, never the
  adaptive ledger.
- Interval lookup reuses the gate's `pressure()` so the watchdog and the gate agree.

**New env:** `JARVIS_RESOURCE_GOVERNOR_POLL_OK_S` (10), `_WARN_S` (3), `_HIGH_S` (0.5),
`_CRITICAL_S` (0.2), `_BACKSTOP_INTERVAL_S` (1.0).

### Piece 3 — Allocation-free Death Rattle (pre-opened FD)

**Gap:** the existing graceful cap-fire calls `_checkpoint_oracle_best_effort()`
(which allocates and can hang under OOM) *before* producing an autopsy → under hard
OOM we get nothing.
**Change (in `harness.py`):**
- **At the very start of boot**, pre-open a raw fd:
  `_autopsy_fd = os.open(autopsy_path, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o644)`,
  stored on the harness. Path: `autopsy_reports/pre_oom_<session>.log` (dir already
  excluded from rsync).
- Pre-encode all static marker strings to **`bytes` at boot** (no f-strings at dump
  time — they allocate and would `MemoryError`).
- **`_fire_death_rattle()` runs the guaranteed, allocation-free dump FIRST:**
  1. `os.write(_autopsy_fd, _RATTLE_HEADER_BYTES)` (pre-encoded; includes a
     monotonic-clock marker built from a small pre-allocated bytearray).
  2. `faulthandler.dump_traceback(file=_autopsy_fd, all_threads=True)` — CPython
     accepts an **int fd** directly; writes via the C fd, allocation-free.
  3. **Best-effort** (may fail under hard OOM, that's acceptable): a per-process RSS
     table via psutil, `os.write`-n line by line.
  4. `os.write(_autopsy_fd, _RATTLE_FOOTER_BYTES)`; `os.fsync(_autopsy_fd)`.
- **Then** the tiered exit: set `stop_reason="resource_governor_pre_oom"`, attempt the
  existing graceful summary path (best-effort), and rely on the **existing bounded
  shutdown watchdog** (`os._exit` floor) so death is deterministic even if graceful
  hangs. This composes with `_fire_process_memory_cap`'s existing termination-hook
  dispatch — we insert the rattle *before* the oracle checkpoint.
- The death rattle is also wired as the redline action for Piece 2's fast tier and is
  invokable from the thread backstop (allocation-free path is thread-safe: only
  `os.write` + `faulthandler`).

**New env:** `JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED` (default `false`),
`JARVIS_RESOURCE_GOVERNOR_REDLINE_FREE_PCT` (e.g. 8 — fire below this, tighter than
the existing 0.75 cap).

### Piece 4 — Pressure-locked, jittered poll-loop activation

**Gap:** L3 worker fan-out is gated, but **sensor poll-loop activation is not** — the
one un-gated path that creates the t=0 herd.
**Change (in `intake_layer_service.py` sensor-start path):**
- Replace the bare `for sensor: await sensor.start()` ignition with a
  **governor-gated, jittered** sequence (Constraint #4): before each sensor's poll
  loop is allowed to ignite, it **awaits the `MemoryPressureGate`**. If the gate reads
  HIGH/CRITICAL, the activation enters a **suspended holding pattern** (await with
  backoff) until pressure subsides to WARN/OK, then ignites with a small jittered
  delay to flatten the curve.
- Direction preserved: **sensors pull from the gate** → authority invariant intact
  (gate never imports sensors).
- Implemented as a thin async helper (e.g. `_gated_stagger_activate(sensors)`) so the
  legacy path is byte-identical when the flag is off (early-return to the old loop).

**New env:** `JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED` (default `false`),
`JARVIS_RESOURCE_GOVERNOR_STAGGER_BASE_MS` (e.g. 250), `_JITTER_MS` (250),
`_HOLD_POLL_S` (0.5), `_HOLD_MAX_S` (60 — escape hatch so a stuck gate can't wedge
boot forever; on timeout, ignite anyway + log loudly).

### Piece 5 — Local high-fidelity diagnostic (macOS-native)

A small, separate watcher (`scripts/resource_blackbox_local.py` or reuse the existing
streaming sink) that, alongside a `--headless` local omni run, streams to the
operator's terminal every ~1s: RSS (process-tree), system free %, `cpu_percent`
(best-effort), `ctx_switches` rate (primary), swap usage, disk free. Tees to a local
log. This is the live "death curve" surface for the baseline.
- **macOS-native sourcing (verified):** `psutil.swap_memory()` **raises `OSError` on
  this macOS** — do NOT use it. Source swap from native `sysctl vm.swapusage` (total/
  used/free) and `vm_stat` (`Swapins`/`Swapouts`/`Pageouts` deltas = the real thrash
  signal). Memory free % from `vm_stat` page math (as `MemoryPressureGate._probe_vm_stat`
  already does). Disk free from `os.statvfs` / `df`. This satisfies the "native macOS
  profiling" directive and avoids the broken psutil path.

**Protocol:**
1. **Baseline (throttle OFF):** governor master OFF; run omni soak `--headless` with a
   short `--max-wall-seconds`; stream telemetry; capture the spike + the Death Rattle
   autopsy (Death Rattle can be ON independently to guarantee capture). **Confirm the
   cause** (RAM vs CPU/ctx vs disk).
2. **Proof (throttle ON):** flip `JARVIS_RESOURCE_GOVERNOR_ENABLED=1` (+ sub-flags);
   re-run; show the spike is flattened and the soak proceeds past the wedge point.

---

## 4. Starvation-immunity invariants (must hold)

- **Allocation-free at redline:** the guaranteed dump uses only pre-opened int fd +
  pre-encoded `bytes` + `os.write` + `faulthandler.dump_traceback(fd)`. No f-strings,
  no `open()`, no `.format`, no new dict/list on the guaranteed path.
- **Immune layer reads raw signals only:** the thread backstop polls raw clocks/probes
  at a fixed floor; it does not depend on the adaptive interval state it guards
  (mirrors the existing Watchdog Isolation Invariant).
- **Authority preserved:** gate never imports scheduler/sensor; sensors pull.
- **No hardcoded magic numbers:** ctx-switch trigger is a rate vs. rolling baseline;
  all thresholds/intervals are env with sane defaults.
- **Default-OFF byte-identical:** every piece early-returns to legacy behavior when its
  flag is off; `git`-HEAD parity test per piece.

---

## 5. Testing strategy

- **Unit (gate):** CPU/ctx-switch dimension — injected fake psutil samples drive
  OK→CRITICAL escalation on ctx-rate spike at 60% RAM; baseline EWMA math; strictest
  composition; OFF byte-identical.
- **Unit (watchdog):** adaptive interval mapping per level; backstop fixed floor;
  redline fires Death Rattle.
- **Unit (death rattle):** allocation-free path writes header+faulthandler+footer to a
  real fd (tmp file), parseable; tiered-exit sets stop_reason; runs without importing
  anything that allocates on the guaranteed path (assert via a constrained call).
- **Unit (stagger):** HIGH/CRITICAL → holding pattern; subside → ignite; hold-timeout
  escape hatch; OFF path identical to legacy loop.
- **Integration:** local `--headless` baseline vs. throttled run (the protocol above)
  — not a CI test, an operator proof.
- Target: per-piece regression files under `tests/governance/` and
  `tests/battle_test/`, mirroring existing arc test spines.

---

## 6. Risks / open questions

- `faulthandler.dump_traceback(file=<int fd>)` int-fd acceptance — **VERIFIED** on
  CPython 3.11.10 (writes header + traceback + footer to a raw fd, allocation-free).
  Re-asserted in a unit test.
- `psutil.swap_memory()` **raises `OSError` on the local macOS** (verified) and
  `cpu_percent()` is noisy/0.0 there — both already handled above (native sysctl/vm_stat
  for swap; ctx-rate primary for thrash). The Linux node path keeps psutil.
- The cause may turn out to be **disk** (worktrees + fastembed), not RAM/CPU — Piece 5
  baseline will reveal it; if so, the governor adds a disk-free dimension (cheap
  follow-on, same pattern) rather than a redesign.
- 16GB local Mac vs 32GB node: the wedge should reproduce *faster* locally if it's
  resource-driven; if the local run is clean, the cause is cloud-specific
  (disk/IAP/spot) — also a valid, narrowing result.

---

## 7. Out of scope

- No new `ResourceGovernor` coordinator class (user directive: strict extend).
- No changes to the cloud IaC path until the local proof lands.
- No swarm/aggregator logic changes — this is purely the runtime footprint layer.
