# Stage 4 — Sovereign Brain Resurrection + Priority-Aware WAL Replay: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Body keeps its Brain alive: hierarchical resource lineage (ownership at birth), priority-ordered WAL replay with send-order-cumulative acks, a persistent-token-bucket-governed resurrection keeper, generation fencing that structurally terminates an obsolete Brain's mutation pathways, and the split-brain live drill as acceptance.

**Architecture:** per the approved spec `docs/superpowers/specs/2026-07-04-stage4-sovereign-resurrection.md` + operator answers (driver-first keeper; ~15s fence heartbeat OK; 900s resurrect window; manifest at `.jarvis/manifests/`). Mandate-2 alignment from the PRD review: the fence COMPOSES the existing Async Yield Matrix Sovereign Execution Boundary (force-armed isolation + commit-denial + raw-write guard, PRD §51.11.35 / PR #69638) + `capture_inflight` — no new termination machinery.

**Tech Stack:** existing transport/WAL/lifecycle substrates; `UrgencyRouter` ranks; `cooperative_fs_io.offload`; intake-WAL `_write_line` flock primitives; `fsm_checkpoint`; GCP labels via `gcp_compute_rest`.

## Global Constraints (operator mandates, binding on every task)

- **MANDATE 1:** resurrect rate cap = persistent token bucket / incrementing ledger on disk (flock substrate). Cap hit ⇒ deterministic TERMINAL state (`RESURRECT_CAP_EXHAUSTED`), loud, no retry loop, no sleeps-as-backoff.
- **MANDATE 2:** the fence structurally terminates mutation execution pathways — compose the Async Yield Matrix commit-denial/isolation force-arm + cooperative shutdown of APPLY/AutoCommit surfaces + `capture_inflight(reason="generation_fenced")`. Never a bare `is_fenced` flag consulted by callers.
- **MANDATE 3:** manifest journal/teardown reuse the intake-WAL flock/append primitives exactly; provisioning reuses the extracted Stage-1 core; priority vocabulary imported from `UrgencyRouter`; no duplicate dedup anywhere.
- **MANDATE 4:** the split-brain live drill must prove: obsolete gen-N cannot commit an active payload; captures inflight; gets reaped; FSM ledger untorn. Telemetry streamed to the operator during the drill.
- Standing: dark behind existing masters; `from __future__ import annotations`; py3.9; async-first; fail-soft (except the mandated TERMINAL states, which are loud+deterministic); ASCII; zero hardcoded limits (env-resolved); TDD; named-files commits; ledger updated per task.
- Grep/AST-enforced invariants (add with the code): no list-style discovery in teardown paths; keeper is the only gen-minter; fence transition contains no LLM calls.

---

### Task 1: `brain_lifecycle.py` extraction + lineage substrate (labels + ResourceManifest + family teardown)

**Files:** Create `backend/core/ouroboros/governance/brain_lifecycle.py` (extracted provisioning core: create/brain-env/TLS-metadata composition from `scripts/ignite_brain_vm.py`, byte-equivalent; + `ResourceManifest` class: append-only JSONL at `JARVIS_RESOURCE_MANIFEST_PATH` default `<repo>/.jarvis/manifests/resource_manifest.jsonl`, records create/delete with kind/name/zone/labels/parent/gen/keeper_id, via the intake-WAL `_write_line`/tolerant-parse primitives — import, don't copy; + `teardown_family(node)` walking manifest children-first + label-family aggregatedList drift check, loud on disagreement). Modify `scripts/ignite_brain_vm.py` (consume the core; regression-pin byte-equivalence via existing 21-test suite), `gcp_compute_rest.py` (labels passthrough already exists — thread `jarvis-owner`/`jarvis-parent`/`jarvis-brain-gen`), `failover_lifecycle.py` (pass parent/gen labels from brain-env when present — additive kwargs). Tests: new `tests/governance/test_brain_lifecycle.py` (manifest append/replay/teardown-order/drift-detector; fake create/delete fns) + existing ignite/bake suites green unmodified.
**Produces:** `provision_brain(...)`, `ResourceManifest.record_create/record_delete/live_family(node)`, `teardown_family(node, *, delete_fns)`; label constants `LABEL_OWNER/LABEL_PARENT/LABEL_GEN`.

### Task 2: Priority-aware WAL replay + send-order-cumulative acks (paired — the correctness precondition lands with the feature)

**Files:** Modify `transport/durable_outbound.py` (`pending(order="priority")`: off-loop sort by `(urgency_rank(entry), event_id)`; rank via `UrgencyRouter` vocabulary imported — unknown⇒STANDARD), `transport/bus_bridge_client.py` (per-connection send-sequence list; `_apply_ack` trims by SENT-PREFIX membership up to acked id — replaces id-cumulative; `on_ack` hook now receives the exact trimmed id set… keep `on_ack(eid)` signature, call per prefix element in send order), `durable_outbound.py` `on_ack` becomes exact-id trim (no `<=` sweep — retires the strand class). Tests: extend `test_wal_replay_and_rediscovery.py` + `test_deliberate_partition.py` with a mixed-urgency leg: backlog {SPECULATIVE, IMMEDIATE, STANDARD}×2 replays IMMEDIATE-first; deliberate out-of-order ack cannot over-trim (an unsent lower id survives); strand-class regression: ack racing append no longer strands (exact-set semantics).
**Produces:** `urgency_rank(envelope_dict) -> int`; send-order trim invariant documented in both modules.

### Task 3: `BrainKeeper` (driver-first) + persistent token bucket + gen minting + discovery gen-filter

**Files:** Create `backend/core/ouroboros/governance/brain_keeper.py` (`BrainKeeper(discover_fn, provision_fn, manifest, *, resurrect_after_s=env 900, bucket)`: continuous-absence monotonic window; single-flight; mints gen = manifest max+1; TERMINAL `RESURRECT_CAP_EXHAUSTED` state surfaced as a loud log + keeper property + census line — never retries past cap; `PersistentTokenBucket` in the same file: flock-journaled ledger at `.jarvis/manifests/resurrect_bucket.jsonl`, capacity `JARVIS_BRAIN_RESURRECT_MAX_PER_H` default 2, refill by wall-clock window recorded in the ledger — deterministic across process restarts, no sleeps). Modify `brain_discovery.py` (gen-filter: candidates labeled `jarvis-brain-gen < current_gen` excluded pre-probe; current gen injected via env/param from keeper), `scripts/run_body_mode.py` (arm keeper in live default; census gains `gen=N resurrect=idle|in_flight|cap_exhausted`). Tests: `tests/governance/test_brain_keeper.py` — bucket persistence across instances (cap enforced after restart), terminal state determinism, single-flight, window reset on successful bind, gen monotonicity from manifest; discovery filter test in `test_brain_discovery.py` style.
**Produces:** `BrainKeeper.state`, `PersistentTokenBucket.try_take() -> bool` (journal-backed), gen source of truth = manifest.

### Task 4: Generation fence (Brain-side, structural)

**Files:** Create `backend/core/ouroboros/governance/generation_fence.py` (subscribe the organism trinity bus `console.keeper_heartbeat` topic — the Body's bus heartbeat carries current-gen ~15s cadence via a small addition in `run_body_mode.py`; on observed_gen > own_gen (env `JARVIS_BRAIN_GENERATION` from brain-env): deterministic transition — (1) force-arm the Async Yield Matrix Sovereign Execution Boundary commit-denial + isolation locks (locate its arming API from PR #69638's module; compose, don't reimplement), (2) cooperative-shutdown request for APPLY/AutoCommit surfaces, (3) `capture_inflight(reason="generation_fenced")`, (4) idle-shutdown fast-path touch. Pure code, no LLM — AST-pin it). Modify `organism_bus_host.py` (construct fence when gen env present), `ignite_brain_vm.py`/`brain_lifecycle.py` (fold `JARVIS_BRAIN_GENERATION` into brain-env). Tests: `tests/governance/test_generation_fence.py` — lower-gen heartbeat ⇒ all four arms invoked (recording fakes for boundary/shutdown/capture); equal/higher gen ⇒ no-op; malformed heartbeat ⇒ ignored; AST pin: no LLM imports in the fence path.
**Produces:** `GenerationFence(bus, own_gen, *, boundary_arm_fn, shutdown_fn, capture_fn)`.

### Task 5: Split-brain live drill (acceptance — operator telemetry streamed)

No new code. Runbook (per spec staging 3 + Stage-3 conventions; telemetry streamed to operator during the run):
1. Provision Brain gen-1 via keeper-armed body mode (manifest records family). Verify signals flow.
2. Partition (firewall delete). Keep body mode running with a shortened `JARVIS_BRAIN_RESURRECT_AFTER_S` (e.g. 180 for the drill — env, not code). Keeper resurrects gen-2 (bucket takes 1 token; manifest shows both nodes + gens).
3. Reopen the OLD node's path (recreate firewall). Assert: discovery never rebinds gen-1 (filter); gen-1 observes the keeper heartbeat and FENCES (journal: boundary armed + capture_inflight reason=generation_fenced); keeper reaps gen-1 via manifest family walk (children first — if gen-1 spawned a failover child, it dies too); backlog lands exactly-once on gen-2 (set-equality on dedup ingest lines).
4. Cap drill: force a third resurrect attempt within the hour (kill gen-2) — bucket exhausts ⇒ deterministic `RESURRECT_CAP_EXHAUSTED` terminal, loud, no VM created.
5. Teardown family to $0, gcloud-verified (manifest walk, drift-check clean). Ledger + MEMORY.

## Self-Review

1. **Spec coverage:** 4.1 keeper→T3; 4.2 lineage/manifest/family-teardown→T1; 4.3 priority replay + send-order acks→T2; 4.4 three-layer fencing→T3 (mint+filter) + T4 (self-fence) + T5 (reap, drill); 4.5 extraction→T1. Operator answers folded (900s, 15s heartbeat, `.jarvis/manifests/`, driver-first). All four execution mandates mapped (M1→T3 bucket/terminal; M2→T4 boundary composition; M3→T1 primitives + T2 imported ranks; M4→T5 drill).
2. **Placeholder scan:** contract-style with exact files/envs/signatures; the Async-Yield-Matrix arming API is deliberately "locate from PR #69638's module" — a scouting instruction, not a TBD (the implementer must bind to the real symbol and name it in the report).
3. **Type consistency:** `try_take() -> bool`; `on_ack(eid)` preserved; `urgency_rank(dict)->int` used in T2 only; gen flows manifest→keeper→labels→brain-env→fence as int.
