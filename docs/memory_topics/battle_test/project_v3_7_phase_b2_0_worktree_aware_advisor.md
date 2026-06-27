---
title: Project V3 7 Phase B2 0 Worktree Aware Advisor
modules: [backend/core/ouroboros/governance/operation_advisor.py, backend/core/ouroboros/governance/orchestrator.py, tests/governance/test_operation_advisor_worktree_aware.py, tests/governance/test_read_only_advisor_bypass.py, docs/architecture/OUROBOROS_VENOM_PRD.md]
status: historical
source: project_v3_7_phase_b2_0_worktree_aware_advisor.md
---

May 12 2026 — closes follow-up arc A from PRD §40.7.10-soak as PR 1 of the §40.7.9 Phase 2 Phase B.2 split. Dedicated branch `ouroboros/swe-bench-pro/b-2-0-worktree-aware-advisor`.

## Why the split

Operator binding 2026-05-12: B.2.0 (worktree-aware OperationAdvisor) is a structural improvement on its own merits. It closes follow-up arc A for L3 worktree-isolated work AND the in-repo L2 exercise corpus AND SWE-Bench-Pro Phase 2 simultaneously — not a SWE-Bench-Pro special case. Shipping it as a standalone PR lets it merge, soak, and graduate on its own ladder without waiting on the envelope_builder + façade + spine work of B.2.1-3.

The user mandate ("solve the root problem directly, no workarounds, no brute force, no shortcuts; strengthen into something advanced, async, dynamic, adaptive, intelligent, robust; no hardcoding; leverage existing files") explicitly rejected Option α (direct factory composition bypassing the orchestrator) and Option γ (per-problem orchestrator) in favor of Option β (full 11-phase pipeline against the cloned worktree). B.2.0 is the enabling layer that makes Option β work end-to-end.

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut would have been: `if envelope.source == "swe_bench_pro": advisor_bypass = True` inside the orchestrator. That's a category special-case and was rejected per PRD §40.7.10 gap-closure direction §3 ("Not a category special-case — must be general policy") AND the user's "fully leverage existing files, avoid duplication" mandate AND operator binding B.2.0 hardening note 4 ("blast computed from the actual mutation root, not because source == swe_bench_pro").

The structural fix: parameterize the advisor on per-op repo_root. The advisor scans the actual tree where the mutation would land — that's structurally correct for every source (L3 worktree, in-repo corpus, SWE-Bench-Pro, future per-problem evaluators that haven't been written yet).

**Canonical evidence key — operator binding hardening note 2**:

`repo_root` (exported as `EVIDENCE_REPO_ROOT_KEY` constant). Documented as the single source of truth so B.2.1's envelope builder cannot fork parallel spellings. `worktree_path` remains sensor-side telemetry (vision_sensor, L2 exercise seed), but the advisor's input is unambiguously `repo_root`.

**Untrusted-input safety — operator binding hardening note 1**:

`resolve_envelope_repo_root` validates the path through a first-failure-wins pipeline. Symlink escapes are defeated by `Path.resolve(strict=False)` BEFORE the allowlist check. The default allowlist is just the orchestrator's `project_root` (covers in-repo `.worktrees/` + `.jarvis/swe_bench_pro/worktrees/` automatically); env-extendable via `JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST` for cross-tmpdir evaluation rigs.

**Source-agnostic by AST enforcement**:

Three AST pins ensure the discipline survives future drift:
1. No `swe_bench_pro` (or similar) string literal under 80 chars in `operation_advisor.py` — short literals are how behavioral branches are typically named
2. No `Compare` node against any `.source` attribute in `operation_advisor.py` — source-conditional advisory is forbidden by construction
3. No `swe_bench_pro` comparison literal in `orchestrator.py` — bypassing the resolver upstream is also forbidden

## Composition discipline — what was deliberately NOT done

- No special case for `envelope.source == "swe_bench_pro"` — root-correct, not category-correct
- No new advisor variant / subclass / parallel module — extend the canonical advisor in place
- No new envelope schema field — `IntentEnvelope.evidence: Dict[str, Any]` already carries arbitrary keys; `OperationContext.intake_evidence_json: str` already snapshots it
- No worktree-creation logic — that's Phase B.1's `per_problem_harness` (already shipped at commit `a5529b0f1a`)
- No reachability into RepairEngine / Iron Gate / SemanticGuardian — pure advisory layer
- No top-level dependency change in `orchestrator.py` — `resolve_envelope_repo_root` joins the existing lazy-import block alongside `OperationAdvisor`, `AdvisoryDecision`, `infer_read_only_intent`
- No graduation flip — master flag stays default-FALSE until soak evidence is collected
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/operation_advisor.py` — substrate (parameterized `advise()` + 4 signal-compute methods + `resolve_envelope_repo_root` + `register_flags`)
- `backend/core/ouroboros/governance/orchestrator.py:~1852` — wiring (resolver call + `repo_root=` kwarg passthrough)
- `tests/governance/test_operation_advisor_worktree_aware.py` — 29-test spine + 6 AST pins
- `tests/governance/test_read_only_advisor_bypass.py` — minor fixture update (`**_` absorbs the new kwarg in `_HighBlastAdvisor` overrides)
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-b20 paragraph + arc-A closure annotation

## Master flags (FlagRegistry auto-seeded via §33.3 walker)

- `JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED` (BOOL/SAFETY, default FALSE) — master switch
- `JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST` (STR/SAFETY, default empty) — colon-separated extra allowed prefixes

## What's next

PR 2 — B.2.1 envelope builder + B.2.2 `evaluate_problem` façade + B.2.3 spine. Three operator bindings hold over:

1. **Naming consistency**: B.2.1's envelope builder MUST use `EVIDENCE_REPO_ROOT_KEY` from `operation_advisor.py` — no parallel spelling
2. **B.2.2 terminal wait must be bounded**: compose `EventChannelServer` SSE broker with `asyncio.wait_for` + cooperative cancel; ReviewCoordinator-style asyncio.Event rendezvous; reject polling-loop as the primary path; on SSE timeout, single-shot ledger fallback at most, never a 1–2s polling loop
3. **AST pin in B.2.3** asserting terminal resolution goes through SSE broker first, documents timeout + optional one-shot ledger fallback, asserts no unbounded `asyncio.wait` anywhere in the façade
