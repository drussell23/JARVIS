---
title: Project Phase10 Audit And Graduation Contract
modules: [backend/core/ouroboros/governance/phase10_graduation_contract.py, tests/governance/test_phase10_graduation_contract.py, backend/core/ouroboros/governance/topology_sentinel.py, backend/core/ouroboros/governance/candidate_generator.py]
status: historical
source: project_phase10_audit_and_graduation_contract.md
---

**Status (2026-05-05)**: Phase 10 substrate audit complete + graduation-contract harness landed at 32/32 tests + 263/263 across the full consolidation + Phase 10 spine. Master flag flip remains operator-paced empirical (3 forced-clean once-proofs).

## Audit findings (corrected stale PRD)

PRD §32.8.1 v4 supplement (added 2026-05-04) was authored before realizing Phase 10 was further along than tracked. Audit on 2026-05-05 found:

- ✅ **Slice 1** (P10.1) — `topology_sentinel.py` ~2,170 LOC shipped via PR #25504 (already known)
- ✅ **Slice 2** (P10.2) yaml v2 + dual-reader — `provider_topology.SCHEMA_VERSION_V2 = "topology.2"` + frozen `RouteEntryV2` + `Topology.from_v2()` classmethod; `brain_selection_policy.yaml:343` is on `topology.2`. Landed earlier under Phase 12 Slice E coordination.
- ✅ **Slice 3** (P10.3) consumer wiring — `candidate_generator.py:1703-1762` AsyncTopologySentinel gate behind `JARVIS_TOPOLOGY_SENTINEL_ENABLED` with `preflight_check()` raising `SentinelInitializationError` at the gate + `_dispatch_via_sentinel(context, deadline, route)` walking ranked `dw_models` list + `fallback_tolerance` enforcement
- ✅ **Slice 4** (P10.4) live-exception ingest — `candidate_generator.py:2482/2494/2514/2524` wires `sentinel.report_failure(model_id, FailureSource.LIVE_STREAM_STALL, detail)` at FOUR DW failure sites (PRD said 3 — exceeded)

**What's actually pending**:
- Slice 5 deletion-side (~50 LOC of yaml + reader migration)
- 3 forced-clean once-proofs (operator-paced empirical)
- Master flag flip
- Slice 6 24h soak

## Graduation-contract harness landed

`backend/core/ouroboros/governance/phase10_graduation_contract.py` (~430 LOC, pure substrate composing topology_sentinel state ledger + session debug.log artifacts):

- **`is_ready_for_purge() -> ContractReport`** — single-call predicate gating the master flag flip
- **5-value `ContractVerdict` closed enum**: `READY_FOR_PURGE` / `INSUFFICIENT_SESSIONS` / `MISSING_QUEUE_EVIDENCE` / `MISSING_RECOVERY_EVIDENCE` / `DISABLED`
- **Frozen `SessionEvidence`** — per-session projection: queue_event_count + recovery_transition_count + excerpts + diagnostics + `is_clean` derived (both criteria within same session window)
- **Frozen `ContractReport`** — aggregated 3-session rolling window verdict + `to_dict()` projection for telemetry
- **Per-session evidence extraction**:
  - Queue evidence: scan `<session>/debug.log` for `dw_severed_queued:` / `fallback_tolerance:queue:severed` tokens (raised by `candidate_generator._dispatch_via_sentinel` when `fallback_tolerance="queue"` fires under SEVERED)
  - Recovery evidence: scan `<session>/topology_sentinel_history.jsonl` for OPEN→HALF_OPEN→CLOSED transition chains per `model_id` (exact 3-row sequence detection)
- **3-session rolling window** — most-recent sessions by mtime; older sessions ignored (regression on session 4 invalidates contract until 3 fresh clean sessions stack)
- **Master flag** `JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED` default-true; off → `DISABLED` verdict
- **Required clean sessions** `JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS` default 3 (clamped [1, 10])
- **NEVER raises** — every fault → diagnostics list, never exception

## 2 new AST pins (auto-discovered)

1. `topology_sentinel_master_flag_stays_default_false` — bytes-pin `_env_bool("JARVIS_TOPOLOGY_SENTINEL_ENABLED", default=False)` literal in topology_sentinel.py; forbids `default=True` until graduation contract reports READY_FOR_PURGE; mirrors M10's operator-binding pattern
2. `phase10_graduation_contract_authority_asymmetry` — phase10_graduation_contract.py imports stdlib + topology_sentinel ONLY (no orchestrator/iron_gate/policy/providers/candidate_generator/etc.)

Both auto-discovered via `register_shipped_invariants` on the module's exposure of the function.

## 2 new FlagRegistry seeds

- `JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED` (BOOL, default true)
- `JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS` (INT, default 3, clamped [1, 10])

## Test spine

`tests/governance/test_phase10_graduation_contract.py` — 32 tests:
- Closed-enum 5-value taxonomy
- Master flag asymmetric env semantics (truthy/falsy/default-true/disabled-yields-disabled)
- `required_clean_sessions` env clamp
- Per-session evidence extraction: clean / no_queue / no_recovery / missing_dir / partial_recovery_chain (no match)
- Verdict ladder: insufficient / missing_queue / missing_recovery / ready_for_purge
- Rolling window uses most-recent 3 (older session ignored)
- Frozen `SessionEvidence` + `ContractReport` + `to_dict` projections
- AST pins auto-registered + pass `validate_all`
- Synthetic check: pin DOES fire if a future PR flips the master flag default to True prematurely
- Authority asymmetry walk
- Public API stability

## PRD updates

- v2.22 → v2.23
- §1565+ P10.2 / §1578+ P10.3 / §1591+ P10.4 flipped to ✅ MERGED with audit-2026-05-05 stamps
- §32.8 v4 sequencing row 13 + §32.8.1 supplement updated
- Recommended-sequencing block: Phase 10 collapsed from "2-3 weeks" to "3-7 days operator-paced empirical"

## Architectural significance

The graduation contract is the **structural enforcement** of an operator binding that was previously documentation only. Pre-Slice-5 the binding lived in a markdown checklist. Post-Slice-5 the binding lives in:
- An AST pin asserting the master flag default stays False
- A predicate that reports READY_FOR_PURGE only when 3 sessions show both criteria
- Test that proves the pin DOES fire on premature flip

Mirrors M10's `m10_master_flag_stays_default_false` pattern — substrate enforces the binding, doesn't just document it. Operator paces the empirical evidence; the cage validates it.

## Reverse Russian Doll alignment

This is the immune system catching up to the spawning core: Phase 10 substrate (Slices 1-4) shipped without a binding harness because the cage pattern hadn't been formalized yet. Slice 5 retroactively pins the binding. Future ARC graduation arcs (Phase 9 12+ flag flips, M10 30+ proposal-acceptance audit, M12 LoRA gate) inherit the pattern.

## What's next

**Operator-paced empirical work** (cannot be done in-session):
1. Run forced-clean soak session #1 with `JARVIS_TOPOLOGY_SENTINEL_ENABLED=true` and `JARVIS_TOPOLOGY_FORCE_SEVERED=true` → wait for breaker self-heal → observe both criteria
2. Repeat sessions #2 and #3
3. After 3 clean sessions: `python -c "from backend.core.ouroboros.governance.phase10_graduation_contract import is_ready_for_purge; print(is_ready_for_purge().to_dict())"` should report `verdict=ready_for_purge`
4. Operator commits the Slice 5 deletions (yaml `dw_allowed: false` lines + `block_mode:` lines + Nervous System Reflex carve-out + env shortcuts) + flips `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default to True
5. Slice 6 — 24h soak post-purge to validate ≥30% DW cost share + ≤50% $/op median reduction

After Phase 10 fully closes, Phase 9 (CRITICAL BLOCKER for A-level RSI) becomes affordable to graduate.
