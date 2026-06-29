# RCA — The Phantom Write (split-brain `written=True`)

**Date:** 2026-06-28
**Severity:** Critical (silent data-integrity / telemetry-truth violation)
**Status:** RESOLVED — 2PC refactor + Cryptographic Terminal Gate, bi-directionally proven
**Author:** Derek J. Russell + O+V (Claude Opus 4.8)
**Components:** `backend/core/ouroboros/governance/change_engine.py`

---

## 1. Summary

`ChangeEngine.execute()` could report a terminal success state — ledger
`state=APPLIED`, decision `outcome=applied`, `written=True` — for an operation
whose file mutation was **rolled back and never durably persisted**. The ledger
("the truth") and the filesystem ("reality") diverged: a **split-brain phantom
write**. This is the precise mechanism behind the long-standing "unit-green-
fails-live" / C+ grade: every green claim could be a lie about the disk.

## 2. Impact

- O+V (and any consumer of the operation ledger) could believe a change was
  durably applied when the working tree was unchanged.
- Across 15 live cloud runs the symptom presented as inconclusive `written=True`
  with no durable PR — costing ~50 minutes each and never root-caused live.

## 3. Root Cause

A **transactional-boundary ordering violation**. In the pre-fix sequence:

```
1. target.write_text(signed_content)        # write
2. ledger.append(state=APPLIED)             # ← TERMINAL SUCCESS recorded
3. emit_decision(outcome="applied")         # ← SUCCESS published
4. VERIFY (post-apply verification)          # ← runs AFTER success was claimed
5. if not verify_passed: rollback(target)   # ← file reverted, success already stands
```

The success state (steps 2–3) was committed to the telemetry ledger **before**
VERIFY (step 4) and its possible rollback (step 5). A verify failure reverted the
bytes on disk but left `APPLIED`/`written=True` standing — split-brain.

## 4. Detection

Isolated via an **8-second strict-isolation integration harness** that drove the
deterministic fixture through the real 11-phase orchestrator to `state=applied
written=True`, then found `_A1_FIXTURE_SENTINEL` on **no file on disk**. An
Absolute I/O Verification Gate (re-read immediately after `write_text`) did **not**
fire — proving the write itself was real, and the divergence was a *post-write
rollback after success was recorded*. This replaced 50-minute blind cloud runs
with a deterministic, repeatable reproduction.

## 5. Fix

**Two-Phase Commit (2PC):**
- **Phase 1 PREPARE** — stage `write_text()` only. No ledger entry, no decision.
- **Phase 2 VERIFY** — run post-apply verification first.
- **Phase 3 COMMIT** — record `APPLIED` + emit decision **only after** VERIFY
  passes. On verify-fail, the existing rollback + terminal `ROLLED_BACK` stands,
  with no spurious `APPLIED`.

**Cryptographic Terminal Gate** (`PhantomWriteException`, fail-closed, env
kill-switch `JARVIS_CHANGE_ENGINE_IO_VERIFY` default-on): immediately before
claiming `APPLIED`, SHA-256 the in-memory expected bytes against the physical
file; any mismatch raises rather than committing a false success.

## 6. Verification (bi-directional cryptographic proof)

`tests/governance/integration/test_change_engine_2pc_proof.py` (concrete
`FixtureVerifier`, real disk, no mocks):
- **Alpha (verify pass):** `success=True` is returned only past Phase-3 COMMIT
  (crypto-gated) → the exact deterministic mutation is byte-verified durable.
- **Omega (verify fail):** `success=False`, `rolled_back=True`, and the file's
  SHA-256 **after rollback is bit-identical to the original**.

Non-regression: `test_validate_pipeline_acceptance` shows identical 8-fail/5-pass
baseline with and without the change (those failures pre-date and are unrelated).

## 7. The Immutable Architectural Law (machine-readable directive)

```yaml
law: two_phase_commit_io_verification
version: 1
status: immutable
directive: >-
  No subsystem shall append a terminal success state to a telemetry ledger
  without prior cryptographic OR OS-level physical verification of the mutation.
invariants:
  - terminal_success_state_requires: verify_passed == true
  - terminal_success_state_requires: sha256(in_memory_expected) == sha256(on_disk_actual)
  - ordering: PREPARE -> VERIFY -> COMMIT   # success recorded ONLY in COMMIT
  - on_verify_fail: rollback THEN terminal=ROLLED_BACK   # never a residual APPLIED
  - on_io_mismatch: raise PhantomWriteException          # fail-closed, never silent
enforced_by:
  - backend/core/ouroboros/governance/change_engine.py  # execute() 2PC + terminal gate
  - tests/governance/integration/test_change_engine_2pc_proof.py  # bi-directional proof
rationale: >-
  The ledger is the system's source of truth. A success state that is not
  physically verified on the medium it claims to have written is a split-brain
  lie. This law forbids the transactional-boundary ordering that produced the
  Phantom Write, permanently.
```

## 8. Guards deliberately NOT modified

The orchestrator's post-apply VERIFY-rollback and the AutoCommitter's
commit-sovereignty refusal (`ledger_sovereignty_refused`, the Immutable-Orange
boundary) are **working as designed** — they correctly refuse to fabricate a
commit in a non-sovereign / verify-failing environment. They were left untouched;
the fix targets only the premature success-state recording, not the guards.
