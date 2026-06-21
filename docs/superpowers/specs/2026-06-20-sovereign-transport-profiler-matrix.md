# Sovereign Transport Profiler & Batch Exemption Matrix — Design Spec

**Date:** 2026-06-20
**Author:** Derek J. Russell (O+V) / Claude Opus 4.8
**Status:** Approved (Option 1 — Learn-then-detach, immortal profile)

## Problem

DW diff-capable codegen resolves to `-dottxt` model variants that only serve via
the **batch API** (RT streaming returns `done_before_content` — empty). Two layers
disagree on transport:

- **Budget layer** (`candidate_generator._compute_primary_budget`, *pre-call*): the
  transport-hedge (default-on) makes `_slice36_should_force_batch` return False →
  op gets the **autarky RT budget (180s)**.
- **Dispatch layer** (`doubleword_provider`, *during call*): hedge races RT∥batch;
  RT yields nothing on `-dottxt` → only batch can win → strangled by the 180s budget.

Result: every STANDARD/COMPLEX codegen op times out at 180s; the Zero-Shot
quarantine then (correctly-but-uselessly) bans healthy models for batch latency →
fleet exhaustion → no `state=applied` → no clean soak.

## Reuse-first inventory (already built — DO NOT rebuild)

- `op_park_store.py` (`ParkedOpStore`), `park_signal.py` (`ParkRequested`/`ParkDescriptor`)
- `generate_park_wrapper.py::maybe_park_or_resume` — wired into `generate_runner.py:839`;
  out-of-pool continuation with widened timeout; PARK-EMIT/RESUME/LEGACY paths
- `BatchFutureRegistry` + `event_channel.py:/webhook/doubleword` + 3-tier race
  (`_await_batch_result`: webhook ∥ adaptive-poll, FIRST_COMPLETED)
- `background_agent_pool.submit_for_resume` / `is_resumed_dispatch` / `BackgroundOp.resumed`
- `dw_transport_disambiguator` — already classifies `done_before_content`
- `state_persistence_daemon` — native GCS push/pull of all of `.jarvis/`

## The 10% gap (this build)

### Slice 1 — Immortal Transport Profile (`dw_transport_profile.py`)
Persistent, fail-soft, gated module mirroring `TtftObserver`'s proven shape.
- `record_batch_only(model_id)` — tag a model RT-incapable (serializes to
  `.jarvis/dw_transport_profile.json`, GCS-backed via existing daemon).
- `is_batch_only(model_id)` — read (rehydrates per fork).
- `clear(model_id)`, load/save additive, `SCHEMA_VERSION = "transport_profile.1"`.
- Master `JARVIS_DW_TRANSPORT_PROFILE_ENABLED` (default true, failure-path-only).
- Optional TTL re-probe (`JARVIS_DW_TRANSPORT_PROFILE_TTL_S`, default 0 = immortal)
  so a model that gains RT capability can be re-learned — defaults immortal per spec.

### Slice 2 — Detection wiring
When an RT arm yields `done_before_content` for `model_id`, call
`record_batch_only(model_id)`. Hook at the existing `done_before_content`
classification seam in `doubleword_provider` (reuse `dw_transport_disambiguator`).
Fail-soft, gated, never perturbs the dispatch.

### Slice 3 — Upfront tagging (`ASYNC_BATCH_PAYLOAD`)
Before the budget layer, if `is_batch_only(resolved_model)`, stamp the op
`ctx.async_batch_payload = True` (new optional OperationContext attribute,
default False → byte-identical when unset).

### Slice 4 — Budget + ban exemption
- `_compute_primary_budget`: when the op is `async_batch_payload`, return the batch
  budget (`force_batch_gen_timeout_floor_s()` ≈ 330s, capped by remaining), even
  though the hedge made `_force_batch=False`.
- Zero-Shot quarantine: `record_timeout` is a no-op for `async_batch_payload` ops
  (batch latency ≠ model death). Pass the tag through to the seam.

### Slice 5 — Active detachment (park trigger)
Extend `should_park_for_route` (or the wrapper's PARK-EMIT gate) so an
`async_batch_payload` op triggers `ParkRequested` regardless of `queue_pressure`.
Graduate the park master for this path. The existing continuation + registry +
resume machinery carries it the rest of the way.

### Slice 6 — Re-ignition
Fresh Spot node, `JARVIS_AEGIS_ENABLED=true`, soak to 3/3 clean + first
`[SOVEREIGN GRADUATION]` PR.

## Invariants
- All knobs default-on but failure-path-only; OFF = byte-identical legacy.
- Fail-soft everywhere (never raise into dispatch).
- No hardcoded model lists — the profile is learned.
- Immortal: profile survives the subprocess fork (disk) and node preemption (GCS).
- Reuse-first: zero duplication of park/registry/webhook/disambiguator machinery.
