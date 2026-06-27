---
title: Project Slice 40 Gls Boot Wiring
modules: [backend/core/ouroboros/governance/topology_sentinel.py]
status: merged
source: project_slice_40_gls_boot_wiring.md
---

Slice 40 — GLS boot wiring + flag graduation for the [[project_slice_39_multi_surface_health]] substrate. **MERGED to main 2026-05-28** PR #63663 (merge `eefa35aa02`; squash branch tip `81cfcef0d4`). Reviewed on Opus (boot-path = load-bearing).

**What it does:**
- `preflight_probe.run_boot_surface_health_sweep(*, dw_provider)` — boot-eager wrapper: self-gates on `is_surface_health_enabled()`; skips if provider None or `not dw_provider.is_available`; resolves a probe model via `_resolve_surface_probe_model` (`JARVIS_DW_SURFACE_PROBE_MODEL` override → else first `PromotionLedger.promoted_models()` → else None=skip); runs `run_surface_health_sweep`; on a degraded DIRECT_STREAMING surface emits a **SOFT** (`is_terminal=False`) topology-breaker signal via `dw_transport_disambiguator._flip_topology_breaker`. NEVER raises.
- Wired into `GovernedLoopService._build_components` AFTER the Slice 25B preflight block, BEFORE `bg_pool.start()` (AST/source-pinned ordering — `test_gls_wires_boot_surface_sweep_before_pool_start`), inside its own swallow-all try/except. Uses `self._doubleword_ref`.
- **Graduated `JARVIS_DW_SURFACE_HEALTH_ENABLED` default FALSE→TRUE** in BOTH `is_surface_health_enabled` and the FlagSpec seed, after the pre-v35 live sweep validated it (batch_storage healthy real file_id / streaming `done_before_content`→upstream_degraded flush-bypassed / auth healthy).

**Boot-path safety (Opus-reviewed, no Critical/Important):**
- Cannot wedge: concurrent `asyncio.gather` + per-probe `wait_for` (default 10s) → worst-case ~10s added boot latency, not 3×; FIVE never-raises layers between the network call and `_build_components`.
- **No breaker starvation** despite the live blocker tripping every boot: `FailureSource.LIVE_TRANSPORT` weight=**1.0** < CLOSED→OPEN trip threshold=**3.0** (`topology_sentinel.py:310-312, 433/444, 1340-1352`) → one boot signal records the streak WITHOUT tripping DW open; streak decays on success. Edge: a boot signal DOES re-open an already-HALF_OPEN breaker (correct circuit-breaker semantics, self-recovers via OPEN→HALF_OPEN→CLOSED ramp).
- `is_available` guard exempts operators without DW from the graduated default-TRUE 3-call boot cost.

**Side effect to know:** with default-TRUE + boot wiring, every GLS boot with DW configured does 3 live DW calls including a real `/v1/files` upload (Surface A leaves an artifact) + a streaming probe + auth. Intentional (it's the health check); cheap (~$0.001/boot).

**Tests:** 8 new (boot gating / provider-unavailable / no-model / degraded-streaming-trips-breaker / healthy-no-breaker / model-override / GLS-wiring AST pin + default-flip updates → `is_surface_health_enabled` default TRUE, disabled requires explicit `=false`). 46 Slice 39+40 green; regression green (preflight 37 / flag_registry 108 / seed-truth+graduation 44).

**Provenance correction shipped same day (PR #63662, `8102e1b142`):** PRD §49.3.6 reworded — DW Support (Peter) flagged only the SYMPTOM ("invalid multi part files") and offered to verify; he did NOT confirm the trailing-`\n` cause. We diagnosed + empirically verified it. Operator sent Peter the postmortem + the pre-fix malformed sample (`logs/diagnostics/dw_prefix_malformed_sample.jsonl`, 207B ends `}`) for DW's validator corpus → external confirmation path. See [[project_slice_38_jsonl_trailing_newline]].

**Boot-verify CONFIRMED 2026-05-28 ~3:12 PM PDT (session bt-2026-05-28-220956):** a cost-insulated headless boot logged `[GLS] Slice 40 surface-health sweep: batch_storage=healthy direct_streaming=upstream_degraded auth_sync=healthy` + `[Slice40] ... soft topology-breaker signal emitted for model=Qwen/Qwen3.5-35B-A3B-FP8`, and rewrote `.jarvis/dw_surface_health.json` NATIVELY at boot (fresh file_id=03207e19, streaming done_before_content consecutive_failures=2 persisting across runs). stop_reason=wall_clock_cap, cost_total=$0.00 (upload below accrual floor). Note: summary.json showed duration_s≈800 + in_flight despite the 150s cap — known host-suspension/wall-accounting skew (§48.6.6), immaterial. Sweep fires natively at boot = wiring proven end-to-end, not just unit/AST-tested.

**Capability bar UNCHANGED (per [[feedback_no_preresult_euphoria]]):** the pre-v35 live sweep re-confirmed the streaming blocker is upstream `done_before_content` — no client substrate produces an APPLY until DW account capacity recovers. Slice 40's win is fast boot-time detection/classification + zero spurious flush, NOT capability.
