---
title: Project Slice242 Recovery Prior
modules: [backend/core/ouroboros/governance/dw_transport_recovery.py, backend/core/ouroboros/governance/hibernation_prober.py, tests/governance/test_slice242_resurrection_soak.py, tests/governance/test_slice242_recovery_prior.py, tests/governance/test_slice243_stability_gate.py, tests/governance/test_slice243_dw_stream_probe.py]
status: merged
source: project_slice242_recovery_prior.md
---

**Slice 242 — Adaptive Statistical Recovery Matrix & Resurrection Soak. MERGED PR #69484, main `ca4e4a5366`.** Part of the DW-sovereignty arc (see [[project_dw_sovereignty_arc_intent]]): make DoubleWord a reliable/resilient PRIMARY for the O+V loop.

**The gap (T2 leftover):** the `HibernationProber` ("Grid Sentinel") already probes a dark DW grid on exponential backoff and auto-wakes on recovery — but its FIRST probe interval was a hardcoded static 5s. Wasteful when outages last minutes (pings a dark grid for nothing; DW won't return sooner).

**Built:**
- `RecoveryDurationPrior` in `dw_transport_recovery.py`: bounded `collections.deque` ring of observed dark-window durations (`enter_hibernation → wake`) + linear-interpolation `quantile()` + `first_probe_interval(default_s, max_s, ...)` → p25 of history clamped `[floor, max]`, falls back to static `default_s` below `min_samples`. Pure, env-driven, thread-safe, NEVER raises (recovery path). Process-wide singleton `get_recovery_prior()` accumulates across hibernation cycles. Knobs `JARVIS_RECOVERY_PRIOR_{WINDOW=20,QUANTILE=0.25,MIN_SAMPLES=3,FLOOR_S=1.0}`.
- `hibernation_prober.py` wiring: `_first_probe_delay()` derives the first interval from the prior; `_record_outage_duration(elapsed)` banks the dark-window length on successful wake. Master `JARVIS_RECOVERY_PRIOR_ENABLED` default-TRUE (`=0` → byte-identical static-5s path; fully fail-soft).
- Phase 3 resurrection injection soak (`test_slice242_resurrection_soak.py`): deterministic `inject-outage → backoff → recover → autonomous wake → record → adaptive-next-probe` proof using a mock controller+provider with tiny real delays. A live container soak CANNOT force a deterministic DW outage — this is the rigorous controllable proof.

**Discipline upheld (per user's repeated demands):**
- **No ML/NN forecasting** — DW recovery is exogenous (no observable features, sparse data, wrong guess strictly costs). The prior times WHEN to start probing; it NEVER claims to predict WHEN DW returns. Online/training-free quantile only.
- **No fiction** — explicitly did NOT build "mid-stream LLM state serialization" / "zero-lost-compute mid-DAG resume" (impossible against a stateless endpoint). The WAL `_replay_wal` "no op left behind" re-run-from-intent remains the correct, untouched persistence granularity.
- **No duplication** — reused existing `dw_transport_recovery` module + the prober's existing backoff/wake loop. No parallel "Grid Sentinel" or "state-space serializer."

**Tests:** 75 green total (14 new: 10 `test_slice242_recovery_prior.py` + 4 resurrection soak), zero regression across recovery/WAL/existing prober (23) suites. TDD RED→GREEN.

**Pre-existing context — what already existed (so 242 was the ONLY genuine gap):** HibernationProber = the Grid Sentinel (Phases 2+3); WAL `_replay_wal` replays `status="pending"` at-least-once on boot (cross-restart durability); `state_persistence_daemon` backs up `.jarvis/` across host-death; `dw_transport_recovery.dynamic_recovery_window_s` = episode-based exponential. The hibernate→probe→resurrect loop was already operational; 242 only added the statistical-prior intelligence to the first-probe timing.

---

**Slice 243 — Adaptive Grid Stability Matrix & Micro-Streaming Flap Mitigation. MERGED PR #69485, main `c4709d70`.** Builds directly on 242.

**The gap:** HibernationProber woke `wake_from_hibernation` on a single successful `/models` 200-OK ping. But DW's primary failure mode is a *flapping grid* — pings UP while stateless streaming sockets drop mid-flight. Waking the heavy PLAN-EXPLOIT DAG against a flapping grid → immediate `live_transport` ruptures + thrashing.

**Built:**
- `hibernation_prober.py` Stability Confidence Gate: on ping UP → `VERIFYING_STABILITY` → `_verify_grid_stability(provider, name)` runs a Micro-Streaming Load Test requiring `JARVIS_GRID_STABILITY_STREAM_CHECKS` (default 1) consecutive clean streams via `provider.stream_health_probe()`. Mid-flight rupture (raise) / incomplete (falsy) → `FLAPPING_GRID_DETECTED` log, wake ABORTED, falls through to the S242 `RecoveryDurationPrior` backoff loop. WAL intent never disturbed; NO false outage banked (only a stream-stable wake calls `_record_outage_duration`). `_last_healthy_provider` slot set in `_probe_any`. Master `JARVIS_GRID_STABILITY_GATE_ENABLED` default-TRUE (`=0` → byte-identical wake-on-ping). Providers WITHOUT `stream_health_probe` (Prime/Claude) → trust the ping (legacy; keeps the 23 existing prober tests green).
- `DoublewordProvider.stream_health_probe()` (after `health_probe` ~line 5121): lightweight ~$0 micro-stream against `/chat/completions` (`stream=true`, capped `max_tokens=16`), REUSES session/Aegis-auth/lease/SSE-read machinery (no duplicate transport). True iff ≥ `JARVIS_GRID_STABILITY_MIN_TOKENS` (default 2) content tokens + clean close; rupture/non-200/too-few/unavailable → False; NEVER raises.
- TDD: 14 new tests (`test_slice243_stability_gate.py` 8 + `test_slice243_dw_stream_probe.py` 6, incl. injected mid-flight rupture flap simulation + flap-then-recover-wakes-once). 96 green total, zero regression. `wake_from_hibernation` strictly gated behind 100% stream verification → no network thrashing.
- Phase 1 housekeeping: folded the pending 242 `progress.txt` append into the 243 commit. **Process note:** 243 branch was cut from the local 242 branch (pre-squash-merge), causing a `progress.txt` merge conflict on PR merge → fixed by `git rebase origin/main` (skipped the already-merged 242 commit, replayed only 243 which self-contains both progress appends) + `--force-with-lease`.
