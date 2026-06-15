# Slice 256 — O+V Live-Fire Validation Engine (Blueprint)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.
> **BOOTSTRAP PARADOX — MANUAL ONLY:** this validator validates kernel patches and *is* a kernel-adjacent patch. The autonomous loop (which has no live-fire validation yet) must NOT build it — it could ship the validator itself broken. Built + live-validated by Claude.

**Goal:** Give O+V's VALIDATE phase Founding-Engineer instincts — when a candidate mutates `unified_supervisor.py` / `backend/core/`, prove it boots in a real ephemeral subprocess (not just pytest mocks); on any unhandled live exception, FAIL VALIDATE and route the traceback to GENERATE; escalate the genuinely-stuck via async PR; protect against environmental cascade.

**Status:** Skeleton `backend/core/ouroboros/governance/live_kernel_validator.py` is written + syntax-verified; the pure deterministic helpers (`livefire_retry_budget`, `sanitize_state_dump`, `affects_kernel`, breaker counting) are implemented + proven. C.2–C.4 are structured stubs to fill under TDD **after authorization**.

**Locked decisions (this session):**
- Retry budget: **env-tunable bounded + simple per-file scale** (`base 3 + per_file, cap 8` via `JARVIS_LIVEFIRE_RETRY_*`) — deterministic guardrail, NOT an AST/context governor.
- Stuck-after-budget: **risk-tiered async escalation** — kernel/core → `OrangePRReviewer` PR w/ sanitized StateDump + traceback + advisory triage; trivial → needs-attention backlog. Never ship, never halt, never silently drop the core.
- Failure-PR triage: **advisory only** (reuse `request_copilot_review`/repo review; low-cost tier; deterministic citation to StateDump nodes; cascade-override → environmental diagnostic). Never closes/fixes/merges. Human decides.
- Cascade (3 consecutive escalations): **deterministic suspend → non-destructive soft relief (gc + ephemeral cache clear) → re-check `MemoryPressureGate` → resume if recovered, else synthesize `HARD_RESTART` routed through `shadow_guard` for `/endorse`.** NEVER auto-reboots.

**Reuse (zero duplication, all confirmed on origin/main):** `OrangePRReviewer` (async escalation PR), `shadow_guard`/`shadow_guard_async` + `/endorse` (Slice 253/255), `MemoryPressureGate`, `telemetry_sentinel` (CRITICAL alerts), `create_safe_task` / async subprocess utils, the orchestrator GENERATE-retry loop (Iron Gate pattern — route feedback to GENERATE).

---

## Architecture
A focused module `live_kernel_validator.py` (standalone-importable, structurally-injected deps → unit-testable without the kernel) + a thin, flag-guarded hook in the orchestrator's VALIDATE phase. Master `JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED` (default OFF → byte-identical until soak-validated).

```
GENERATE → VALIDATE ─┬─ pytest (existing)
                     └─ LiveKernelValidator (NEW, if patch.affects_kernel):
                          ephemeral subprocess: import unified_supervisor + exercise
                          patched symbols, outbound I/O mocked, TTL+mem bounded
                          ├─ clean boot → VALIDATE proceeds
                          └─ unhandled exception/timeout → VALIDATE FAILS
                               └─ route traceback → GENERATE (retry, budget = base+per-file, capped)
                                    └─ budget exhausted → risk-tiered escalation:
                                         kernel → OrangePRReviewer PR (sanitized StateDump) + CascadeFailureBreaker.record_escalation()
                                         trivial → backlog
CascadeFailureBreaker trips (3 consecutive) → suspend → soft relief → re-eval MemoryPressureGate
   → recovered: resume + ENVIRONMENT_RECOVERED telemetry
   → still critical: synthesize HARD_RESTART → shadow_guard → /endorse [Y/N]
```

**Why subprocess + exercise-the-symbols (not just import):** the Slice-255 `NameError`/`TypeError` lived in *function bodies*, not import-time — a bare `import unified_supervisor` would NOT have caught them. The probe must construct/call the patched symbols. Subprocess isolation + mocked outbound I/O guarantees a failed boot can't corrupt real state/db/fs.

---

## TDD Slice Plan

### C.1 — Deterministic guardrail + sanitizer (DONE — pure, proven) → just needs committed tests
- [ ] Write `tests/governance/test_live_kernel_validator_pure.py`: `livefire_retry_budget` (1→3, 3→5, 50→cap 8; env overrides); `sanitize_state_dump` (redacts by key name, by value shape `sk-/ghp_/hf_/AKIA/JWT`, collapses `$HOME`→`~`, never raises); `affects_kernel` gating; `CascadeFailureBreaker` counting + reset.
- [ ] Run green; commit.

### C.2 — Ephemeral live-fire validator
- [ ] Tests (inject a fake subprocess_runner): clean probe → `LiveFireResult(ok=True)`; probe raising `NameError`/`TypeError` → `ok=False` + traceback + `exception_type`; timeout → `timed_out=True, ok=False`; non-kernel patch → validator skipped.
- [ ] Implement `_build_probe_script` (render: `import unified_supervisor` + construct/call `affected_symbols`, with `unittest.mock.patch` over network/FS/db egress) + `validate_patch` (spawn TTL+mem-bounded subprocess via the injected runner, parse result). Determine `affected_symbols` from the candidate diff (AST of changed defs/classes).
- [ ] **Live-fire self-test (sandbox-off):** feed it a deliberately-broken kernel patch (reintroduce the Slice-255 `NameError`) → assert VALIDATE fails with the traceback; feed a clean patch → passes. Commit.

### C.3 — Cascade breaker + adaptive relief + shadow-gated reboot
- [ ] Tests (fake MemoryPressureGate + fake shadow_guard): 3 consecutive escalations → trip; soft relief (gc + injected cache-clear) called; pressure-recovered → `'recovered'` + ENVIRONMENT_RECOVERED; still-critical → `HARD_RESTART` routed to `shadow_guard` (→ pending `/endorse`), NEVER auto-executed.
- [ ] Implement `on_trip` reusing real `MemoryPressureGate` + `shadow_guard_async`. Commit.

### C.4 — Orchestrator VALIDATE hook + escalation + advisory triage + finalize
- [ ] Flag-guarded hook in the orchestrator VALIDATE phase (default OFF; OFF byte-identical). On budget-exhausted kernel failure → `OrangePRReviewer` PR with `sanitize_state_dump(generation_context)` + traceback; wire advisory `request_copilot_review` (low-cost tier, deterministic-citation prompt, cascade-override → environmental diagnostic).
- [ ] Register all `JARVIS_*` flags in FlagRegistry. Full verification: orchestrator `ast.parse` OK; existing VALIDATE tests green; the live-fire self-test green. Commit + PR (manual).

---

## Constraints & Risks
| Risk | Mitigation |
|---|---|
| Runaway kernel boot hangs the loop | hard TTL (`JARVIS_LIVEFIRE_TIMEOUT_S`, default 90) + mem cap (`_MEM_CAP_MB`, default 4096) in the subprocess; kill on timeout |
| Failed boot corrupts real state | subprocess isolation + `unittest.mock` over all outbound I/O; never touches real db/fs/net |
| Secret leak in StateDump PR | `sanitize_state_dump` (proven) — by-key + by-value-shape + path scrub, fail-safe to REDACTED |
| Validator over-fires on non-kernel patches | `affects_kernel` gate (only `unified_supervisor.py`/`backend/core/`) |
| Cascade breaker non-deterministic | pure counting + deterministic suspend; relief/reboot are explicit, observable steps; reboot only via human `/endorse` |
| The loop ships the validator broken | MANUAL build only (bootstrap paradox) |

## Definition of Done
- Module complete; pure helpers + validator + breaker all under test; **live-fire self-test proves it catches a real reintroduced kernel bug**; orchestrator hook flag-guarded (OFF byte-identical); flags seeded; escalation reuses OrangePRReviewer + shadow_guard; built manually + live-validated by Claude before merge.

## Recommended sequencing (Founding-Engineer)
Build + live-validate **C.1→C.2 first as a vertical slice** (prove the validator actually catches the Slice-255 bug class on a real boot) before C.3/C.4. Don't big-bang the full matrix — validate the validator on reality, exactly as Slice 255 taught us.
