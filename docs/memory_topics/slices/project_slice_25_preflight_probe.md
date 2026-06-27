---
title: Composition discipline (operator-mandated, AST-pinned)
modules: []
status: historical
source: project_slice_25_preflight_probe.md
---

PR #59080 squash-merged 2026-05-26 at `83999fb207`. Branch `ouroboros/slice-25-preflight-health-probe`. Closes v18 observability gap: 33 wall-clock minutes against an upstream DW tier where 1 model was 403-blocked and 3 were 5xx/timeout, with no structural signal.

# Composition discipline (operator-mandated, AST-pinned)

100% leverage of existing primitives:
- `HeavyProber` (existing) — injected via `probe_fn` parameter for testability
- `dw_entitlement_classifier.classify_4xx` (existing) — composed verbatim for 4xx branch; AST pin BANS the classifier marker strings from preflight_probe source (no duplication)
- `PromotionLedger.demote` (existing) — only delta is new origin constant `QUARANTINE_ACCOUNT_NOT_ENTITLED` added to `_VALID_QUARANTINE_ORIGINS`
- `TopologySentinel.report_failure` (existing) — Slice 24's structural fields carry through

# Closed taxonomy (AST-pinned at 5)

`PreflightVerdict`: ACTIVE / DEMOTED_ENTITLEMENT / DEGRADED_5XX / DEGRADED_TIMEOUT / ERROR_OTHER

# Side-effect routing

| Verdict | Action |
|---|---|
| ACTIVE | none |
| DEMOTED_ENTITLEMENT | `ledger.demote(model_id, origin=QUARANTINE_ACCOUNT_NOT_ENTITLED)` |
| DEGRADED_5XX | `sentinel.report_failure(LIVE_HTTP_5XX, status_code, response_body, is_terminal=False)` |
| DEGRADED_TIMEOUT | `sentinel.report_failure(LIVE_TRANSPORT, ...)` |
| ERROR_OTHER | recorded only |

# Fail-fast boundary

`PreflightAllFailedError(report)` raised when every probe failed AND `halt_on_all_fail=True` (default). Concurrency via `asyncio.gather` + per-probe `asyncio.wait_for(timeout_per_model_s)` (default 10s) — worst-case wall is 10s regardless of fleet size.

# Master flag

`JARVIS_PREFLIGHT_PROBE_ENABLED` default-FALSE pending v19 validation + wiring slice.

# Out of scope for Slice 25 (follow-ups)

1. **Wiring slice** — bind `HeavyProber.probe → ProbeOutcome` adapter + integrate at harness boot OR candidate_generator first-sentinel-activation. ~50 LOC.
2. **Graduation** — flip master-on after v19 proves no false-positives. Optionally extend with Slice 23 autonomous-activation pattern (auto-on when Claude disabled + multi-model fleet).

# Verification

12 tests (3 AST pins + 9 spine). 232/232 regression across:
- Slice 18c→25 (102 tests)
- Full `test_topology_sentinel.py` (60, untouched)
- Phase 10 contract (32, preserved)
- Full `test_dw_promotion_ledger.py` (38, new origin integrated)

# v18 forensic that motivated this

bt-2026-05-26-233010: Slice 23 fleet walker iterated all 4 trusted DW models cleanly. Qwen-4B http_403 entitlement / Qwen-35B DoublewordInfraError / Qwen-397B SERVER_ERROR+TIMEOUT / Kimi failed. 33 min × $0.0084 = doomed-attempt spend. With Slice 25 wired, the entire fleet would be probed in 10s parallel, Qwen-4B demoted permanently, sentinel breakers trip on 5xx — and if ALL fail, clean PreflightAllFailedError halt with diagnostic instead of churning the orchestration loop.

Related: [[project_slice_24_sentinel_transition_schema]] (Slice 24's structural fields are what Slice 25 passes to sentinel.report_failure), [[project_slice_23_sentinel_activation]] (the slice that activated the fleet walker, exposing the upstream gap Slice 25 closes), [[feedback_no_preresult_euphoria]] (Slice 25 substrate is methodology; v19 wiring + RESOLVED is capability).
