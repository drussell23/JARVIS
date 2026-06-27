---
title: Project Slice 20A Self Fallback
modules: [tests/governance/test_slice20a_self_fallback_elimination.py, backend/core/ouroboros/governance/candidate_generator.py, backend/core/ouroboros/governance/governed_loop_service.py]
status: historical
source: project_slice_20a_self_fallback.md
---

PR #59072 squash-merged 2026-05-26 at `7bf1c12528`. Branch `ouroboros/slice-20-json-healing-and-fallback`.

**The bug**: Slice 19a disabled ClaudeProvider construction (`self._fallback=None` at provider level), but `GovernedLoopService:3576` collapsed it via `effective_fallback = fallback or primary`. When operator set `JARVIS_PROVIDER_CLAUDE_DISABLED=true`: fallback=None, primary=DW → effective_fallback=DW → `CandidateGenerator(primary=DW, fallback=DW)` SAME OBJECT. Slice 19b's `self._fallback is None` guard never fired. v15 empirical proof: `EXHAUSTION cause=fallback_failed primary_name=doubleword-397b fallback_name=doubleword-397b` (same provider).

**Fix mechanism**:
- `candidate_generator.py:833`: `fallback: CandidateProvider` → `fallback: Optional[CandidateProvider] = None`
- `governed_loop_service.py:3573-3590`: when `JARVIS_PROVIDER_CLAUDE_DISABLED=true` AND fallback=None, `effective_fallback` STAYS None. Legacy `fallback or primary` preserved verbatim in else clause (byte-identical for default soaks).

**Verification**: 6 tests (2 AST pins + 4 spine) in `tests/governance/test_slice20a_self_fallback_elimination.py`. Surrounding regression: 23/23 green across Slices 18c/19a/19b/20A.

**Discipline**: No new env knob — `JARVIS_PROVIDER_CLAUDE_DISABLED` (Slice 19a) is the only operator surface; Slice 20A is load-bearing repair to make it work end-to-end. Legacy branch preserved + pinned for zero-regression on default config.

**Deferred follow-ups** (separate PRs, not started):
- **Slice 20B**: Asynchronous JSON healing with DW Qwen3.5-35B repair fallback
- **Slice 20C**: Fleet schema rotation in `UrgencyRouter`

Related: [[project_predictive_provider_resilience]] (broader provider arc), [[project_local_hardware_envelope_16gb_m1]] (same v15 soak surfaced both questions).
