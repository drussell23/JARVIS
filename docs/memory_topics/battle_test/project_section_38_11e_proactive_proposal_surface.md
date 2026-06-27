---
title: Project Section 38 11E Proactive Proposal Surface
modules: []
status: historical
source: project_section_38_11e_proactive_proposal_surface.md
---

May 8 2026: §38.11-E closed end-to-end same-day. Single substrate composing 4 canonical autonomy producers per §38.11.5a row 5 reconciliation contract.

**Substrate**: `governance/proactive_proposal_surface.py` (~810 LOC) + `governance/proposals_repl.py` (~340 LOC §33.3 auto-discovered).

**Closed taxonomies**:
- 4-value `ProposalKind` enum (CURIOSITY 🔭 / CAPABILITY_GAP 🧩 / OPPORTUNITY 💡 / ARCHITECTURE 🏛) — one slot per canonical producer
- 4-value `ProposalDecision` enum (PENDING ○ / ACCEPTED ✓ / REJECTED ✗ / EXPIRED ⌛) with `is_terminal` property — PENDING is the only mutable state

**Frozen §33.5 versioned artifact** `ProactiveProposal`: deterministic 12-char `proposal_id` (sha256[:12] of `kind|signal_source|summary|emitted_at_unix`); symmetric `to_dict` / `from_dict` round-trips full schema (returns None on defensive parse failure).

**Composes canonical sources** (5 AST pins enforce):
- `intent.signals.SignalSource` envelope shape (canonical `signal_source` field — same field IntentSignal uses; required by §38.11.5a row 5 reconciliation)
- `cross_process_jsonl.flock_append_line` for §33.4 optional persistence at `.jarvis/proactive_proposals.jsonl`
- `ide_observability_stream` broker for SSE — single event ring (no parallel publication)

**Producer-bridge §33.2** `emit_proposal(kind=, signal_source=, summary=, rationale=, priority_hint=)` returns `proposal_id` or `None`. Lazy-importable from each of the 4 canonical producers; NEVER raises.

**Operator-decision API**: `accept_proposal(pid, note=)` / `reject_proposal(pid, note=)` / `ProactiveProposalLedger.expire_stale()` sweep. Terminal-idempotency guarantee: second `accept` on already-ACCEPTED is no-op `True`; cross-state transitions return `False` (no resurrection from terminal).

**Bounded ring** (env: `_RING_SIZE`, default 64, clamped 8..512) with drop-oldest eviction. Idempotent on duplicate `proposal_id`.

**New SSE event**: `EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED = "proactive_proposal_emitted"` registered in canonical `_VALID_EVENT_TYPES` frozenset.

**Sub-flag granularity**: master `JARVIS_PROACTIVE_PROPOSAL_ENABLED` default-FALSE per §33.1 + 2 sub-flags (PANEL_ENABLED default TRUE, PERSISTENCE_ENABLED default FALSE — opt-in JSONL writes) + 2 ring-size knobs.

**`/proposals` REPL** (§33.3 auto-discovered): 8 subcommands (panel [N] / all [N] / show <id> / accept <id> [note] / reject <id> [note] / expire / status / help). Help bypasses master gate.

**5 AST pins**: master_default_false / authority_asymmetry / proposal_kind_taxonomy_4_values (one slot per canonical producer) / proposal_decision_taxonomy_4_values / **composes_canonical_signal_source** (bytes-pin: `ProactiveProposal` MUST carry `signal_source` field — required by §38.11.5a row 5 reconciliation envelope shape).

**Regression**: 52 new tests + 495/495 cumulative across §38.11-A + B + C + D + E + canonical sources + Gap #6 narrative-channel arc.

**§38.11.5a.5 single-canonical-name discipline honored**: ONE substrate, ONE envelope shape (same `signal_source` field that IntentSignal carries), ONE ledger; 4 producers compose via `emit_proposal` lazy-import; no parallel proposal store, no parallel decision recorder.

**§38.11.5a.2 row 5 closes §39 #11 "Capability gap proactive proposals"** structurally — capability-gap proposals are now ONE of 4 ProposalKind slots in the unified surface.

**§33 patterns invoked** (5 of 5 catalog):
- §33.1 graduation contract (master default-FALSE)
- §33.2 producer-bridge (`emit_proposal` lazy-importable from 4 producers)
- §33.3 naming-cage (`/proposals` auto-discovered)
- §33.4 per-cluster flock'd JSONL (optional persistence layer)
- §33.5 versioned artifact (`ProactiveProposal` with symmetric to_dict/from_dict)

**NEXT** (§38.11.5a.2 sequence row 6 — final): §38.11-F — Capability Constellation (~6h merged with §39 #8; composes `flag_registry` + `unified_graduation_dashboard` + `strategic_direction` manifest; new `capability_constellation_updated` SSE event). After §38.11-F, §38.11 closes; §39 Tier 1+ becomes the next-tier UX foundation.
