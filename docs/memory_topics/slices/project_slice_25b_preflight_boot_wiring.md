---
title: 4 Phases
modules: [backend/core/ouroboros/governance/preflight_probe.py, backend/core/ouroboros/governance/governed_loop_service.py]
status: historical
source: project_slice_25b_preflight_boot_wiring.md
---

PR #59081 squash-merged 2026-05-26 at `b56d6ae94d`. Branch `ouroboros/slice-25b-preflight-boot-wiring`. Wires the Slice 25 substrate into `GovernedLoopService._build_components` BEFORE `BackgroundAgentPool.start` — closes the v18 (`bt-2026-05-26-233010`) 33-min wall-clock burn.

# 4 Phases

**Phase 1 — Adapter** (in `preflight_probe.py`):
- `_parse_heavyprobe_error(error_str) -> (status_code, error_body, timeout, msg)` — pure-fn parser for HeavyProbeResult.error string (Task #86 structured `entitlement_blocked:<marker>:status_<N>`, `status_<N>:<body>`, `ttft_timeout`, freeform).
- `_heavyresult_to_outcome(result) -> ProbeOutcome` — frozen-record adapter.
- `build_heavyprobe_adapter(dw_provider, *, prober_factory=None)` — async closure binding session/base_url/api_key; `prober_factory` injectable for tests.
- AST pin BANS classifier marker strings from adapter (forward marker as error_body; classifier composes at receiver side).

**Phase 2 — Boot gate**:
- New `async def run_boot_preflight(*, dw_provider, prober_factory=None) -> Optional[PreflightReport]`.
- Integration site in `governed_loop_service.py _build_components` INLINE BEFORE `self._bg_pool.start()`. AST pin asserts source-position ordering.
- `PreflightAllFailedError` propagates to `start()`'s outer `try/except` → `ServiceState.FAILED` clean exit. Other exceptions swallowed (preflight must not block boot).

**Phase 3 — Dynamic eviction**:
- 403 entitlement → `ledger.demote(origin=QUARANTINE_ACCOUNT_NOT_ENTITLED)` PERSISTED to disk (future boots inherit pre-filtered fleet — end-to-end test verifies via fresh PromotionLedger().load()).
- 5xx/timeout → `sentinel.report_failure` with Slice 24 structural fields.
- All-fail → structured per-model diagnostic logged + clean halt.

**Phase 4 — Autonomous activation**:
- `is_preflight_enabled()` now consults `JARVIS_PROVIDER_CLAUDE_DISABLED` as branch-3 autonomous trigger (mirrors Slice 23 decision-matrix pattern).
- Operator-explicit OFF still wins (rollback contract preserved).

# Composition discipline

- No new env knobs (existing `JARVIS_PREFLIGHT_PROBE_ENABLED` + `JARVIS_PROVIDER_CLAUDE_DISABLED`)
- No new state — composes existing `PromotionLedger.demote` + `get_default_sentinel` + `HeavyProber`
- Acyclic — lazy imports
- 3 AST pins prevent regression

# Verification

13 tests (3 AST + 10 spine). 245/245 regression (operator's 232 target exceeded). Phase 10 contract preserved.

# v19 expected behavior

Boot post-Slice-25B with Claude disabled:
- `_build_components()` constructs DW provider
- Slice 25B boot gate fires (claude_disabled branch auto-on)
- HeavyProber probes 4 trusted models in parallel (max 10s)
- 403/5xx/timeout → side-effects routed
- All-fail → 10s clean halt with diagnostic (vs v18's 33-min burn)
- ONLY IF ≥1 ACTIVE → `bg_pool.start()`

Related: [[project_slice_25_preflight_probe]] (substrate this wires), [[project_slice_24_sentinel_transition_schema]] (provides structural fields preflight uses), [[project_slice_23_sentinel_activation]] (sibling autonomous-activation pattern that Slice 25B Phase 4 mirrors), [[feedback_no_preresult_euphoria]] (Slice 25B is methodology — v19 RESOLVED is the capability bar).
