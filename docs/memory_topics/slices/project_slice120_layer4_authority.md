---
title: Project Slice120 Layer4 Authority
modules: [backend/core/ouroboros/governance/layer4_roadmap_authority.py, tests/architecture/test_slice120_layer4_roadmap.py, orchestrator.py]
status: merged
source: project_slice120_layer4_authority.md
---

**Slice 120 — The Sovereign Layer-4 Roadmap Authority (BOUNDED). MERGED PR #69323 (squash `99d1d4d917`), PRD v3.11.** The T4 mechanism: lets the 12–18mo evidence clock run UNATTENDED without dissolving the Zero-Order Doll.

`backend/core/ouroboros/governance/layer4_roadmap_authority.py` (gated `JARVIS_LAYER4_ROADMAP_ENABLED`, §33.1 default-FALSE): ingests operator-signed `.jarvis/roadmap.signed.yaml`, HMAC-verifies the operator's signature by **composing the exact Aegis token codec** (`_encode_token`/`_decode_token`/`TokenVerdictKind` from `aegis/lease.py` — NO reinvented crypto), auto-resolves the approval prompt for SAFE explicitly-authorized scopes only.

**THE LOAD-BEARING REFUSAL (§1):** the operator's brief literally said "fully suppress ALL APPROVAL_REQUIRED prompts" — that REMOVES the recursion bound (one forged/stale-at-month-12 signature could switch off the human gate on the ops the cage protects). REFUSED the "all"; built the bounded form: an **un-signable floor** that `may_suppress_approval` checks BEFORE signature/scope. No signature EVER suppresses approval for SAFETY/BLOCKED tier, Order-2/M10 cognitive self-mod, recursion-bound breach, or governance/cage touch — those always escalate to a live operator. *Delegated autonomy within a signed boundary, never removal of the boundary.*

Fail-closed: missing key / missing-garbled sig / bad HMAC / body-hash mismatch (tamper) / expiry → non-VALID → degrade to per-PR human review; loader never raises. Budget/recursion bounds only TIGHTEN — roadmap asking depth 99 or $10k clamped to Slice-104 cap + hard ceiling (signature can't *buy* more than safety maxima). Orchestrator hook (`orchestrator.py` beside the Slice-119 M10 hook + observe→APPROVAL force) is **escalation-only** (re-asserts APPROVAL_REQUIRED, never relaxes a tier — auto-resolution UX lives in harness), default-off → byte-identical, try/except-isolated. `--layer4-autonomous` soak flag arms it.

Verify-first AGAIN confirmed `immutable_budget_state_machine` is hallucinated (same as [[project_slice101_architecture_audit]] Slice 118) → composed real `risk_tier_floor` + Slice-104 `recursion_depth_gate`. 21 tests (`tests/architecture/test_slice120_layer4_roadmap.py`): fail-closed crypto, un-signable floor, budget clamps. Aegis lease codec regression clean (18/18).

**North-Star re-score (PRD §51.11.C, unchanged ~30–35%):** T1+T2 CLOSED, T3 M10 wired-not-graduated, **T4 now MECHANISM-BUILT+PROVEN** (was decision-pending), T5 evidence clock at ~0. Engineering ~90%; the operator's standing signature + the 12–18mo clock are what remain — earned, not flipped.
