# Slice 127 Phase 3 — DW Transport Exponential Self-Healing (LOCKED BLUEPRINT)

> **For the fresh instance:** This is a locked, verify-first spec. Work TDD, tests-first, on THIS branch (`worktree-slice-127-dw-transport-healing`, already off the merged main with P1+P2 + ownership-marked for commits). Compose existing code — do NOT rebuild. PR + squash-merge when green.

**Goal:** Replace the **hardcoded 120s static** DW transport recovery window with a **dynamic full-jitter exponential backoff** keyed to consecutive rupture *episodes*, so a transient DW blip recovers fast while a chronically-rupturing lane is probed with progressively wider (capped, jittered) windows — eliminating thundering-herd re-probe collisions. A successful DW completion instantly resets the episode counter to 0.

---

## Verify-first context (READ — the scope is narrow by design)

P3's *core self-healing already exists on main* — confirmed against the live tree this session:

- **DW lane recovery window EXISTS but is FIXED.** `candidate_generator.py::dw_transport_degraded_preflight()` (≈L997) severs the DW lane to Claude **only while** the `DIRECT_STREAMING → TRANSPORT_DEGRADED` verdict is **fresh** (age ≤ `_dw_preflight_freshness_s()`, a **hardcoded-default 120s**, env `JARVIS_DW_PREFLIGHT_FRESHNESS_S`, ≈L985). After 120s the verdict is stale → gate returns False → DW lane re-enabled → next op **auto-probes** DW. Recovery is automatic; ruptures re-stamp via `_note_dw_live_transport_degraded()` (≈L945). **The static 120s is the only thing to replace.**
- **EMPTY_RESPONSE is ALREADY recoverable** — do NOT add an `EMPTY_RESPONSE` FailureMode. A 0-token empty raises `DoublewordInfraError(status_code=0)` (`doubleword_provider.py:2949`) → `FailbackStateMachine.classify_exception` status-0 falls through to `FailureMode.TIMEOUT`/`CONTENT_FAILURE` (`candidate_generator.py:1764-1771`) → both map to `RetryDecision.RETRY_TRANSIENT` in `provider_retry_classifier._FAILURE_MODE_DEFAULT`. Already transient/recoverable. (An explicit `EMPTY_RESPONSE` enum member would be cosmetic telemetry only AND would force the closed-taxonomy AST-pin + FailureMode-coverage-pin updates — not worth it. Skip unless an operator specifically wants the telemetry distinction.)
- **P1 + P2 are merged** (PR #69333 → main `79977dd0ba`): economic reclassification + per-lane economic self-healing `ClaudeCircuitBreaker`. This branch is based on that.

So Phase 3 = **one focused change**: make the recovery window dynamic/exponential. Everything else self-heals already.

---

## Design (compose, don't rebuild)

### New module: `backend/core/ouroboros/governance/dw_transport_recovery.py`
A thread-safe process singleton (model it on `dual_lane_breaker.py` — same shape, same `get_*()` accessor + `reset()` test hook). It tracks DW rupture **episodes** and computes the dynamic window by **composing** the EXISTING AWS full-jitter function — do NOT write new backoff math:

```python
from backend.core.ouroboros.governance.circuit_breaker import full_jitter_delay
```

API:
- `note_degraded() -> None` — register a rupture episode. **Debounce by window**: increment `_episode_count` only when the previous degraded stamp is OLDER than the current dynamic window (so a burst of ruptures inside one outage = ONE episode, not N). Stamp `_last_degraded_monotonic`.
- `note_recovered() -> None` — a DW completion succeeded → reset `_episode_count = 0` instantly.
- `dynamic_recovery_window_s() -> float` — return `full_jitter_delay(attempt=max(0, _episode_count - 1), base_s=_base(), cap_s=_cap())`, floored so episode 1 ≥ base (full-jitter can return 0; clamp to a sane floor, e.g. `max(base*0.5, full_jitter)` or just re-roll-free `min(cap, base*2^(n-1))` then jitter — match `full_jitter_delay`'s contract; see circuit_breaker.py:396).
- `episode_count` / `snapshot()` — read-only observability (§7).
- Master `dw_dynamic_recovery_enabled()` → `JARVIS_DW_DYNAMIC_RECOVERY_ENABLED` **default-FALSE** (§33.1). Knobs: `JARVIS_DW_RECOVERY_BASE_S` (default 30.0), `JARVIS_DW_RECOVERY_CAP_S` (default 600.0).

### Integration (3 surgical, gated edits in `candidate_generator.py`)
1. **`dw_transport_degraded_preflight()` (≈L997, the freshness comparison ≈L1026-1027):** when `dw_dynamic_recovery_enabled()`, compare `age_s` against `get_dw_transport_recovery().dynamic_recovery_window_s()` instead of the fixed `_dw_preflight_freshness_s()`. OFF → unchanged (byte-identical fixed 120s).
2. **`_note_dw_live_transport_degraded()` (≈L945):** also call `get_dw_transport_recovery().note_degraded()` (gated; best-effort, never raises — it sits on the dispatch error path).
3. **DW success site:** call `get_dw_transport_recovery().note_recovered()` where a DW candidate succeeds — co-locate with the existing `get_dual_lane_breaker().record_success()` call (grep `record_success` in candidate_generator.py, ≈L940). Gated.

### Tests-first (`tests/governance/test_dw_transport_recovery.py` + integration pin)
- episode 1 → window ≈ base (within jitter bounds); episode N → window grows ~`2^(n-1)*base`, capped at cap.
- `note_recovered()` → episode_count resets to 0 → next window back to base.
- burst debounce: many `note_degraded()` within one window = 1 episode.
- `dw_dynamic_recovery_enabled()` default-FALSE; OFF → `dw_transport_degraded_preflight` uses the fixed 120s (byte-identical legacy — add a pin).
- thread-safety (concurrent note_degraded/note_recovered → counter never corrupts), NEVER raises.
- bytes-pin the 3 integration sites (the hot path is too large to unit-test directly — same convention as `test_dispatcher_consults_breaker_before_primary`).

### Invariants
- Composes `circuit_breaker.full_jitter_delay` (no new/duplicate backoff math — anti-duplication mandate).
- Gated default-FALSE → OFF byte-identical to the fixed-120s legacy path.
- DW success resets episodes instantly (transient blip recovers fast).
- Thread-safe singleton; NEVER raises (best-effort on the dispatch error path).
- No hardcoded models; no secrets logged.

---

## Commit/PR mechanics for the fresh instance (learned this session)
- This branch's worktree is **already ownership-marked** (`ledger_sovereignty.mark_owned`) so autonomous-channel commits pass the Iron Gate `denied_sovereignty` check. If a new worktree is made, re-stamp: `python3 -c "from pathlib import Path; from backend.core.ouroboros.governance import ledger_sovereignty as ls; ls.mark_owned(Path('.').resolve(), session_id='slice-127-p3', branch_name='<branch>')"`.
- **Edit worktree paths, NOT the main tree** (the autonomous loop mutates the main checkout).
- `gh` needs `dangerouslyDisableSandbox` for TLS; plain `git push` works sandboxed. Merge: `gh pr merge <#> --squash --admin --delete-branch`.
- Classifier purity AST pins are strict (no `os.environ` in `provider_retry_classifier.py`).
- **Verify-first ALWAYS** — both the §51.11.D blueprint AND the P3 brief turned out to assume self-healing that already existed. Re-confirm anchors against the live tree before editing.
