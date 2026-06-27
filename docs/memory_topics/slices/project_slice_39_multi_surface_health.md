---
title: Project Slice 39 Multi Surface Health
modules: [docs/architecture/OUROBOROS_VENOM_PRD.md, tests/governance/test_dw_heavy_probe.py]
status: historical
source: project_slice_39_multi_surface_health.md
---

Slice 39 — Multi-Surface DW Transport-Health Substrate. **MERGED to main 2026-05-28** via PR #63661 (merge commit `b5c2169375`). Built subagent-driven (8 TDD tasks, each spec+code-quality reviewed; load-bearing tasks 5/6 + final review on Opus). 38 tests, zero regressions.

**Why it exists:** v25→v34 showed the "moving-blocker" anti-pattern — each fix (bearer → loop-health → 397B-timeout → `/v1/files`-500 → streaming `done_before_content`) was discovered only by burning a full ~60-min capability soak. Slice 39 replaces that with an up-front concurrent multi-surface health sweep that classifies failures by protocol semantics in seconds.

**Shipped (composes existing code, zero new transport path):**
- `dw_surface_health.py` — `SurfaceKind`(batch_storage/direct_streaming/auth_sync) + `SurfaceVerdict`(healthy/transport_degraded/upstream_degraded/auth_failed/error_other) + `SurfaceHealthLedger` (per-*surface*; mirrors `ModalityLedger` atomic-write pattern, NOT a copy; `.jarvis/dw_surface_health.json`).
- `dw_transport_disambiguator.py` — `classify_surface_failure` (pure; UPSTREAM markers checked BEFORE transport so `done_before_content` always wins; markers keyed to REAL `dw_heavy_probe` emitter strings incl. `transport:` prefix + `probe_raised`/`prober_raised`) + `raw_http_bypass_probe` (fresh one-shot TCPConnector) + `disambiguate_and_recover` + `_flip_topology_breaker` (uses `FailureSource.LIVE_TRANSPORT`).
- `dw_client_lifecycle.py` — `ClientLifecycleManager.flush_transport_pool` (env-gated + cooldown).
- `dw_surface_probes.py` — probe A (`/v1/files` via `_compose_jsonl_batch_entry`+`_upload_file`), B (streaming via `build_heavyprobe_adapter`), C (Aegis `dw_session_auth_header`) + `run_surface_sweep` (concurrent `asyncio.gather`, per-probe timeout isolation, records-only — never flushes).
- `doubleword_provider.force_session_reset()` (composes existing `_get_session` rebuild — nulls `self._session`; note `_session` is a property over `self._state.session`).
- `preflight_probe.run_surface_health_sweep` + `is_surface_health_enabled` (lazy imports → no cycle) + 6 FlagSpec seeds in `flag_registry_seed.py` (positional `FlagSpec(...)` — avoided the [[project_flag_registry_api_drift]] kwargs landmine).

**THE load-bearing invariant (AST-pinned):** `done_before_content` (HTTP 200, clean SSE, `[DONE]` with zero content deltas) = **upstream** → flush is BYPASSED (flushing a healthy socket + re-probing the same empty stream is a forbidden brute-force loop). Only transport-class faults (disconnect/reset/`transport:`/`stream_closed_early`/`ttft_timeout`/…) run the raw-bypass probe → hard-flush iff fresh socket succeeds while pooled failed. `test_ast_pin_flush_bypass_on_upstream` walks `disambiguate_and_recover`'s AST and fails if `flush_transport_pool` ever appears in the UPSTREAM branch.

**Honest scope (per [[feedback_no_preresult_euphoria]]):** master `JARVIS_DW_SURFACE_HEALTH_ENABLED` defaults **FALSE** (dormant until v35 graduation). Substrate DETECTS/CLASSIFIES the blocker fast + refuses to mask an upstream fault with client churn — it does NOT manufacture upstream DW capacity, so makes NO APPLY-bar claim. v34's blocker (all 3 models `status=0 done_before_content`) is upstream/account-side (§48.5 hypothesis (a)). Full arc narrative: PRD `docs/architecture/OUROBOROS_VENOM_PRD.md` §49 (also MERGED in this PR). See [[project_v33_capability_soak_postmortem]].

**Next (operator-gated, NOT started):** v35 health-telemetry soak `JARVIS_DW_SURFACE_HEALTH_ENABLED=true` ($1.00/600s) → flip default→TRUE only if it classifies the live blocker with zero spurious flushes. Two informational notes from final review: probe A leaves an uploaded `/v1/files` artifact + upload cost per sweep; `run_surface_health_sweep` builds a fresh ledger per call (fine for one-shot; a cadence-wiring slice should pass a long-lived ledger).

**Unrelated pre-existing failures (NOT Slice 39):** `tests/governance/test_dw_heavy_probe.py::test_scheduler_picks_first_eligible` + `::test_scheduler_skips_within_cooldown` fail on the pre-Slice-39 base commit `62057ec5df` too; `dw_heavy_probe.py` was untouched. Candidate for a separate fix.
