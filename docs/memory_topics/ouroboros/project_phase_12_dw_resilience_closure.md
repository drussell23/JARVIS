---
title: Project Phase 12 Dw Resilience Closure
modules: [backend/core/ouroboros/governance/pricing_oracle.py, tests/governance/test_sentinel_pacemaker_handshake.py]
status: merged
source: project_phase_12_dw_resilience_closure.md
---

Phase 12 / 12.2 DoubleWord Resilience arc CLOSED 2026-04-29 by soak #7 (`bt-2026-04-29-074851`).

**Why:** The arc started with soaks #1–#6 hitting `idle_timeout` because DW endpoint flakiness (403, transport timeouts) cascaded into a catalog-deadlock loop (catalog purged → BG ops topology-blocked → no DW calls → no breaker transitions → catalog stays purged → blocked ops accumulate). Three structural fixes were prescribed and all three are now graduated and proven on a hostile-network soak.

**How to apply:** Treat this as the canonical closure record for any future "DW reliability"-style triage. The remaining BG-route blocks observed in soak #7 are NOT a regression — they are the `project_bg_spec_sealed.md` cost contract holding (BG never cascades to Claude on full DW failure). Don't reopen this arc just because BG ops queue under DW outage.

Soak #7 final tokens:
  * stop_reason=idle_timeout, duration_s=853, session_outcome=complete (clean)
  * 22 catalog models, 4 routes assigned (background+complex+speculative+standard)
  * 16 Pricing Oracle resolutions — incl. soak #6 root case `Qwen/Qwen3.5-397B-A17B-FP8-dottxt → qwen_3_5_397b ($0.10/$0.40)`
  * 3 Handshake firings — `force_refresh requested ... force_refresh wake — bypassing 1800s cadence sleep`
  * 14 BG topology blocks (intentional skip_and_queue per project_bg_spec_sealed.md)
  * 8 postmortem captures + 8 default-claim batches (Priority A + F end-to-end loop closure)
  * cost_total=$0.0316 (1 IMMEDIATE Claude op), strategic_drift status=ok (10 ops, 1 drifted)
  * 0 unhandled exceptions in runner frames; 2 infra-noise errors (DeepSeek 403, V4-Flash transport timeout) — non-blocking by waiver policy

Components closed by this arc:
  * **Pricing Oracle (Option α)** — `pricing_oracle.py` + hook in `dw_catalog_client.ModelCard.from_api_dict`. JARVIS_PRICING_ORACLE_ENABLED default true. 70 regression tests + integration tests. Closes Static Pricing Blindspot.
  * **Sentinel-Pacemaker Handshake (Option β)** — `dw_discovery_runner.request_force_refresh` + asyncio.wait FIRST_COMPLETED race in `_discovery_refresh_loop` + late-import trigger in `candidate_generator`. JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED default true. Rate-limited at 30s min interval (floored at 1s). 16 §-numbered regression tests in `test_sentinel_pacemaker_handshake.py`. Closes catalog-deadlock loop.
  * **Universal Terminal Postmortem (Option E)** — already graduated in earlier slice; soak #7 confirms 8 captures with mandatory claim density + insufficient-evidence accounting working as designed.

Don't re-litigate:
  * BG topology block reason text "Static list purged; ranking authority is dw_catalog_classifier" is the policy YAML's static explanation, NOT a runtime "catalog is empty" signal
  * `attempted=0` in summary.json is the known counter bug (use strategic_drift.total_ops or debug.log grep for ground truth)
  * ShutdownWatchdog `os._exit(75)` at 30s post-teardown is by design (post_asyncio_teardown eager-bail), not a soak failure — summary/replay/notebook all write before fire
