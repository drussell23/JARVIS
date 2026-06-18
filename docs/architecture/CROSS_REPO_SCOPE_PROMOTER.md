# Cross-Repo Scope Promoter — Igniting the Dormant Multi-Repo Saga Mesh

> **Status:** IMPLEMENTED (gated default-OFF). 2026-06-18.
> **Goal:** Make O+V able to autonomously develop sibling Trinity repos (reactor, prime) — *without
> rebuilding* the cross-repo orchestration substrate, which already exists.

## 0. The discovery — the mesh was already built, just never ignited

A full cross-repo orchestration substrate already exists and is wired:

| Capability | Where |
|---|---|
| Per-repo path resolution | `OrchestratorConfig.resolve_repo_roots()` — called in `_execute_saga_apply` |
| Unified jarvis\|prime\|reactor code graph | `oracle.py` `self._repos` (reactor registered by default; `NodeID.repo`-keyed) |
| Multi-repo candidate generation | `providers.py` 2c.1 schema → per-repo `patches` map |
| Saga apply + **compensating all-or-nothing rollback** | `saga/saga_apply_strategy.py` (`SagaApplyStrategy`) |
| Cross-repo three-tier verify | `saga/cross_repo_verifier.py` (`CrossRepoVerifier`) |
| Repo registry (name→path) | `multi_repo/registry.py` (`RepoRegistry`) |

The entire path engages **iff** `cross_repo == len(repo_scope) > 1` (`op_context.py:1109`). **No live
caller ever created a multi-repo-scoped op** — so the mesh was a fully-built engine with no ignition
wire. Rebuilding it (as a naive plan would) would duplicate ~3,000 lines. This module is the missing
trigger, not a rebuild.

## 1. Single source of truth — registry ⇆ Oracle key reconciliation

The Oracle keys reactor as `"reactor"` (`NodeID.repo`); the registry registered `"reactor-core"` —
so `resolve_repo_roots("reactor")` would miss. Fixed: the registry now registers under the canonical
`"reactor"` key and honors env precedence `JARVIS_REACTOR_REPO_PATH` > `REACTOR_CORE_REPO_PATH` >
`REACTOR_CORE_PATH` (the var the Oracle itself reads) — setting any one wires *both* the registry and
the Oracle graph.

## 2. The promoter (`governance/cross_repo_scope_promoter.py`)

**Phase 1 — Asynchronous cross-tenant lineage tracing.** Hooked in the orchestrator right after the
`CONTEXT_EXPANSION` advance (before GENERATE, so the elevated scope drives the 2c.1 multi-repo
candidate schema). For the op's target files, it traces the **unified** Oracle graph
(`get_dependencies`/`get_dependents`/`compute_blast_radius`); if a directed dependency/call-chain
crosses from the primary repo into a sibling (`reactor`/`prime`), it **elevates `repo_scope`** to span
both via `OperationContext.with_cross_repo_promotion()` (re-derives `cross_repo=True` → routes APPLY
through `_execute_saga_apply`). The sync graph queries are offloaded via `asyncio.to_thread`.

**Phase 2 — Topological Cascade Shield (Blast-Radius Governor).** Before promoting, it measures how
deep the cross-boundary blast reaches into the sibling. If depth > `JARVIS_MAX_SAGA_CASCADE_DEPTH`
(default 2), it **shards** the op — capping the sibling's mutable surface to the immediate
boundary-interface files and recording the deep internal nodes as *shielded* from rewrite. Prevents
a localized fault from authorizing a runaway rewrite of a sibling repo's internals.

**Phase 3 — Gating, Orange-tier, parity.** Master flag `JARVIS_CROSS_REPO_PROMOTER_ENABLED`
(**default OFF** → single-repo pipeline byte-identical). Every promoted op is forced to **Orange-tier
(`APPROVAL_REQUIRED`)** — autonomous mutation of a second production repo always halts for a human.
The promoter emits a structural-delta visualization (`PromotionReport.render()`: boundary edges,
sibling files in scope, shield status, why scope was elevated) before the saga hands off to
`CrossRepoVerifier`.

## 3. Safety posture

- **Default OFF** + **immutable Orange-tier** on every promotion — the autonomy blast radius across
  repos never auto-applies.
- **Cascade shield** bounds the sibling mutation surface.
- **Fail-soft** everywhere — any promoter/graph error → no promotion → single-repo behavior unchanged.
- No duplication — rides the existing saga apply + compensating rollback + cross-repo verify.

## 4. Tests

`tests/governance/test_cross_repo_scope_promoter.py` (11): cross-boundary detect; intra-repo no-op;
cascade-shield shard vs shallow; elevation derives `cross_repo` + forces Orange; gating default-OFF;
async promote; report render. 91 multi_repo+saga regression tests green.

## 5. Honest bounds / follow-ups

- The structural gate (Repair Context Bridge Slice 3) currently validates the primary file; full
  multi-repo delta simulation across the boundary is a follow-up.
- Cascade-shield enforcement records boundary files + shielded internals in the report and scopes the
  apply plan; hard generator-level restriction to boundary files (vs. advisory + Orange-tier human
  gate) is a follow-up if soak shows over-broad sibling edits.
- Graduating `JARVIS_CROSS_REPO_PROMOTER_ENABLED` default-ON requires a soak that drives a genuine
  cross-boundary op through the saga end-to-end (never exercised before).
