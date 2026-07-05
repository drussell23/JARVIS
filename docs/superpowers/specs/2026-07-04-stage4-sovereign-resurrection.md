# Stage 4 — Sovereign Brain Resurrection + Priority-Aware WAL Replay

**Date:** 2026-07-04
**Status:** DRAFT — awaiting operator sign-off (spec gate; no implementation until approved)
**Author:** O+V campaign (Claude, for Derek J. Russell)
**Depends on:** Stages 0-3 (all live-proven 2026-07-04); operator mandates of 2026-07-04.
**Promotions:** approved 2026-07-04 from the Stage-3 plan's advancement menu (#1 + #2).

## Problem

Stage 3 proved the link survives failure, but three sovereignty gaps remain, each demonstrated live this campaign:

1. **The Brain has no keeper.** When the Brain node dies (SPOT preemption, idle shutdown, crash), the Body degrades durably and re-races discovery forever — but nothing ever *re-creates* the Brain. Self-healing stops at "wait patiently."
2. **Teardown discovers ownership instead of knowing it.** The orphaned `jarvis-prime-failover` node (2026-07-04, live) was found by an ad-hoc instance listing after the fact: the Brain's failover lifecycle legitimately spawned a child, then died, and nothing recorded that the child belonged to the Brain's family. Loose cleanup greps are the anti-pattern the operator has banned.
3. **WAL replay is FIFO.** After a long partition the Brain receives the backlog in id order — a `SPECULATIVE` dream-signal from hour one replays ahead of an `IMMEDIATE` test-failure from minute-ago. Recovery should be intelligence-ordered (Manifesto §5), not arrival-ordered.

And the keeper itself creates the hardest edge: **split-brain**. If the Body resurrects Brain-B during a partition that Brain-A actually survived, two FSMs hold writer authority when the partition heals.

## Goal

The Body becomes the Brain's **keeper**: it detects sustained Brain absence, resurrects a new Brain by composing the proven Stage-1/2/3 primitives, replays the durable backlog in priority order, and deterministically fences any obsolete Brain generation — with every cloud resource's ownership recorded at birth in a hierarchical lineage model, so teardown is a manifest walk, never a discovery grep. $0-orphan extends from "no orphaned node" to "no orphaned *family tree*."

## Non-Goals (YAGNI)

- Not multi-Brain topologies (one authoritative Brain generation at a time; the mesh-grade origin guard stays a future item).
- Not a new provisioning pathway — resurrection composes `gcp_compute_rest.create_instance` + the Stage-1 brain node spec + golden image + brain-env metadata exactly as `BrainIgnitionDriver` does today (Mandate 3).
- Not Mac-side FSM authority — the Body still never writes the ledger; the keeper manages *lifecycle*, not *governance*.
- Not the Domain 1-4 autonomy specs (sequence-locked behind this stage per operator).

## Architecture

### 4.1 The Keeper (`BrainKeeper`, Body-side)

A single async component owned by the Body-mode driver (and later the Body organism), composing ONLY existing primitives:

- **Detection:** the Stage-3 `url_resolver` seam already re-races `discover_brain_endpoint()` on every reconnect attempt. The keeper observes the same signal stream: when discovery has returned no healthy endpoint for `JARVIS_BRAIN_RESURRECT_AFTER_S` (env, default 900) *continuously* (monotonic-clock window, reset on any successful bind), resurrection arms. Single-flight: one resurrection in progress, ever; a keeper restart re-derives state from the manifest (below), never from memory.
- **Resurrection:** invoke the extracted provisioning core (see 4.5) — golden image family, brain node spec, TLS material via metadata, brain-env fold — the byte-level Stage-1 path. The new node carries the lineage labels and generation stamp (4.2/4.4). Post-create, the keeper simply lets the existing discovery/reconnect/WAL machinery take over: **the Stage-3 replay drains the partition backlog into the resurrected Brain automatically** — resurrection is lifecycle only, delivery is already solved.
- **Cost sovereignty:** resurrection consumes the existing budget authorities (`session_budget_authority` / cost-cap env) — an unfunded keeper refuses to resurrect, loudly. Resurrect attempts are capped per window (`JARVIS_BRAIN_RESURRECT_MAX_PER_H`, default 2) — a crash-looping golden image must not become a VM factory (bounded, loud, manifest-journaled).

### 4.2 Hierarchical Ownership — lineage labels + the Resource Manifest (Mandate 1)

Ownership is recorded **at creation time**, structurally, in two mutually-checking forms:

- **Cascading GCP labels** on every resource the family creates: `jarvis-role` (existing), `jarvis-owner=<keeper-id>` (stable keeper identity), `jarvis-parent=<creator-node-name>`, `jarvis-brain-gen=<N>` (4.4). The Brain's own children inherit: the failover lifecycle's `create_instance` calls (already label-threading since Stage-1 Task 2) gain the parent/gen labels from the Brain's own brain-env — so a `jarvis-prime-failover` spawned by Brain gen-7 is *born* labeled as gen-7's child.
- **The Resource Manifest** — an append-only, flock-journaled JSONL at `.jarvis/resource_manifest.jsonl` (the intake-WAL `_write_line` substrate, reused verbatim): one record per create (`kind, name, zone, labels, parent, created_ts, keeper_id, gen`) and one per confirmed delete. The manifest is the teardown's source of truth; a **single label-family aggregatedList query** (`labels.jarvis-owner=<keeper-id>`) is the drift detector run at teardown and at keeper boot — manifest-vs-labels disagreement is a loud finding, never silently reconciled.
- **Family-tree teardown:** deleting a Brain = walking the manifest for every live record whose parent chain reaches that node (children first, then the node), then the label-query cross-check. This is the structural fix for the orphaned-failover class: the child was *born* owned; teardown *knows*, it doesn't *search*.

### 4.3 Priority-Aware WAL Replay (Mandate 2)

- **Metadata, not queues:** WAL entries already carry the envelope's `urgency` inside `envelope_dict.payload.data`. At reconnect, `_replay_durable` currently sends `pending()` in id order. Stage 4 replaces the ordering with a **dynamically computed sort key** derived per entry at replay time: `(urgency_rank(entry), event_id)` where `urgency_rank` maps through the EXISTING `UrgencyRouter` priority vocabulary (IMMEDIATE=1 … SPECULATIVE=7 — imported, not re-declared). No per-priority queue structures exist anywhere; unknown/missing urgency ranks as STANDARD (fail-neutral). The sort runs **off-loop** (the `cooperative_fs_io.offload` substrate, same as every other WAL touch) on the recovered snapshot; the send loop is the existing sequential awaited path.
- **The ack-semantics consequence (the load-bearing design change):** cumulative trim-by-`id <= acked` is only correct when send order equals id order. Priority replay breaks that. Stage 4 therefore redefines the client-side trim rule from *id-cumulative* to **send-order-cumulative**: the client records its per-connection send sequence; an ack for event X trims exactly the prefix of that sequence up to X (TCP in-order delivery makes the prefix guarantee sound — the same argument the Stage-3 final review validated). The ack frame, the server's ingest-cursor behavior, and the monotonic-in-send-space property are all unchanged — only the client's interpretation moves from id-space to send-space. This also structurally retires the Stage-3 "ack-races-ahead-of-append strand" class: trim eligibility becomes membership in the sent-prefix, never a numeric comparison against unsent ids.

### 4.4 Split-Brain: Generation Fencing (Mandate 4)

Deterministic single-writer authority via a **monotonic Brain generation number**, enforced at three independent layers:

1. **Commissioning:** the keeper is the ONLY minter of generations. Each resurrection increments `gen` (persisted in the manifest — survives keeper restarts) and stamps it into the node's labels AND its brain-env (`JARVIS_BRAIN_GENERATION=N`). Exactly one *authoritative* generation exists by construction: the highest the keeper has commissioned.
2. **Discovery filter (passive fence):** `discover_brain_endpoint()` gains a gen-awareness: candidates whose `jarvis-brain-gen` label is below the keeper's current gen are never probed, never bound. A healed partition cannot reconnect the Body to an obsolete Brain.
3. **Self-fencing (active fence) + reaping (backstop):** the Body's bus hello/heartbeat path carries the current gen (a `console.*`-lane control event — existing topic allowlist). A Brain whose own gen is lower than an observed current-gen executes a deterministic fence transition — pure code, no LLM: halt the governed loop's APPLY/AutoCommit surfaces (the existing cooperative-shutdown + `capture_inflight(reason="generation_fenced")` path, machinery reused unchanged), disable pushes, enter idle-shutdown fast path. Independently, the keeper's manifest walk reaps every node labeled `gen < current` (children first, per 4.2). Fencing is immediate and local; reaping is the eventual backstop — corruption requires BOTH to fail simultaneously AND the git boundary to fail.
4. **Why the ledger cannot tear:** the FSM ledger is node-local disk (it dies with its node — there is no merged-ledger reconciliation surface), and every durable code mutation crosses the world only through the sovereign cross-repo mutator's branch/PR gates (Immutable-Orange). A fenced gen-N Brain's in-flight op suspends to its HMAC checkpoint; the checkpoint's op re-enters intake on the authoritative Brain via the existing hydrate path if its evidence still stands. Two generations can therefore *race* only into separate branches/PRs — annoying at worst, never a torn terminal state.

### 4.5 Refactor-to-reuse (Mandate 3 enabler)

The provisioning core currently lives inside `scripts/ignite_brain_vm.py` (driver-shaped). Stage 4 extracts the create/brain-env/TLS-metadata composition into `backend/core/ouroboros/governance/brain_lifecycle.py` with the CLI driver and the keeper as its two thin consumers — byte-equivalent behavior for the driver (regression-pinned), one provisioning pathway forever. `FailoverLifecycleController` stays untouched except for accepting the pass-through lineage labels it already threads.

## Leverage-Existing Map (DRY)

| Need | Reuse |
|---|---|
| Brain absence signal | Stage-3 `url_resolver` / `discover_brain_endpoint` re-race |
| Provisioning | Stage-1 golden image + brain node spec + `gcp_compute_rest.create_instance` (extracted core, 4.5) |
| Backlog delivery into the new Brain | Stage-3 WAL-seeded replay + server dedup (unchanged) |
| Durable manifest substrate | intake WAL `_write_line` (flock/fsync) |
| Priority vocabulary | `UrgencyRouter` ranks (imported) |
| Off-loop sort/probe | `cooperative_fs_io.offload` |
| Fence suspension | `fsm_checkpoint.capture_inflight` (new reason string only) |
| Child labeling | failover lifecycle's existing label threading |
| Budget refusal | session budget authority / cost-cap envs |

## Staging (each independently shippable)

1. **Lineage substrate:** labels + Resource Manifest + family-tree teardown (retrofit onto ignition driver + failover lifecycle). Live proof: spawn Brain + let it spawn a failover child + teardown reaps both from the manifest, drift-check clean.
2. **Priority replay + send-order acks:** the 4.3 pair (they land together — the ack redefinition is the correctness precondition). Partition-suite leg: mixed-urgency backlog replays IMMEDIATE-first, trims exactly, deliberate out-of-order ack storm cannot strand or over-trim.
3. **The Keeper + generation fencing:** detection, single-flight resurrection, gen minting, discovery filter, self-fence, reap. The live drill: partition the Brain, let the keeper resurrect gen N+1, then *reopen the old node's path* — assert the old Brain self-fences (journal shows the fence transition + checkpoint), discovery never rebinds it, the keeper reaps it, backlog lands exactly-once on gen N+1.

## Testing Strategy

- Every stage: TDD, real-infra localhost pairs (the Stage-3 suite's scaffolding), condition-polled, set-equality exactness on all delivery assertions, flake-immunity runs.
- The split-brain drill (staging 3) is the stage's deliberate-partition analogue: it must be live-fired on real GCP before the stage closes, including the keeper's cost-cap refusal path and the resurrect-rate cap.
- Grep/AST-enforced invariants: no `gcloud ... list`-style discovery in any teardown path (manifest-walk only; the label query is exclusively the drift *detector*); single gen-minter (keeper only); fence transition contains no LLM calls.

## Risks / Open Questions (for operator review)

1. **Keeper placement.** Body-mode driver first (this spec), organism-supervisor later? Recommend: driver-owned in Stage 4, promoted into the Body organism at cutover.
2. **Gen-fence heartbeat cadence** rides the existing bus heartbeat — is ~15s fence latency acceptable given discovery-filter + reap layers? (Recommend yes; tightening is an env knob away.)
3. **Resurrect window default** (`900s`): balances SPOT-preemption blips (self-recovering; golden image reboots) against genuine death. Operator preference?
4. **Manifest location** is Mac-local (`.jarvis/`): the keeper is the Body — acceptable single home? (GCS-mirroring is a YAGNI-deferred hardening.)
