---
title: Anti-Venom Hardening (2026-06-26/27, PR #69733 OPEN — branch `feature/anti-venom-hardening` off main e46a79a, 20 commits, 153 tests green; NOT merged — gated on human line-by-line brain-stem review)
modules: [docs/superpowers/specs/2026-06-26-anti-venom-hardening-verified.md]
status: open
source: project_anti_venom_hardening.md
---

# Anti-Venom Hardening (2026-06-26/27, PR #69733 OPEN — branch `feature/anti-venom-hardening` off main e46a79a, 20 commits, 153 tests green; NOT merged — gated on human line-by-line brain-stem review)

The Phase-3 Priority-#1 pivot after the brutal architectural audit graded O+V a C+ vs a 95% sovereign-dev benchmark. Built via SDD (10 tasks, each implementer→task-review→fix), then Opus whole-branch review, then 2 Criticals fixed+re-reviewed. The user's pact: **Parallel Verification → Sequenced reviewed SDD Implementation → Human PR Review** (the brain stem is never auto-committed unreviewed). An ultracode Workflow (11 agents, 745K tok) adversarially pre-verified 8/9 vulns + found 2 new CRITICALs.

## The 3 locks (all default-behavior-preserving where a kill switch exists)
- **Lock A — fail-closed guardian:** SemanticGuardian per-pattern crash → hard finding (not swallowed); guardian crash → `risk_tier=APPROVAL_REQUIRED`+sentinel (not silent SAFE_AUTO), **propagation verified** (`PhaseResult.artifacts["risk_tier"]`→orchestrator rebind→no demotion path). `change_engine.py` + `gate_runner.py`/`orchestrator.py`.
- **Lock B — universal taint chokepoint:** `ChangeEngine._pre_write_gate` (canonicalize realpath→containment→immutable-governance→protected-path→guardian, fail-closed on ANY internal error) before EVERY write. Covers **create+modify+DELETE** across ChangeEngine single/multi + cross-repo Saga (writes+deletes) + L2 repair. `bash`/`run_tests` → `sandbox_exec.py` (ephemeral **read-only** `--network none` Trinity Docker, fail-closed if Docker absent); bash first-verb allowlist. In-loop `edit_file`/`write_file` content-gate (Task 10) → hard finding returns descriptive `ToolError` to LLM context = self-correction loop.
- **Lock C — immutable governance (anti-quine):** hardcoded NO-env-off `_IMMUTABLE_GOVERNANCE_SENTINELS` frozenset, self-protecting (incl change_engine+sandbox_exec), grep-pinned + anti-drift-pinned (Venom `_PROTECTED_PATH_SUBSTRINGS` ⊇ immutable set).

## REUSABLE LESSONS (the gold — verify these in future immune-system work)
1. **The LIVE path is the extracted phase-runners, NOT orchestrator.py inline.** `JARVIS_PHASE_RUNNER_*` default TRUE → orchestrator inline guardian/apply/noop is the `=false` FALLBACK. Task 7 first hardened ONLY the fallback (theater) → had to port Lock A/baseline/shield to `gate_runner.py`/`generate_runner.py`/`slice4b_runner.py`. ALWAYS grep which path is default-live before claiming a guard is wired.
2. **A chokepoint guardian MUST baseline on the on-disk pre-image, NEVER `old_content=""`.** `old=""` turns every MODIFY into a synthetic creation → delta-gated patterns (shell_exec/dynamic_import/credential) false-positive on PRE-EXISTING legit code → blocks all edits to files using subprocess (the governance dir is full of them). The whole-branch review caught this AFTER per-task reviews passed (their legit-apply test used `x=1`). Fix: `old = target.read_text()` if exists else `""`. Safe because in-loop-mutation is covered upstream at GATE's git-HEAD baseline.
3. **There were 5 governed write surfaces, not 2.** Venom tools / bash / run_tests / ChangeEngine single+multi / **cross-repo Saga (write AND delete) / L2 repair**. "Universal chokepoint" required hunting each. Saga used raw `write_bytes`/`unlink` with zero gating; DELETE of a guard file = disabled guard (phantom-file vector).
4. **Layered review earns its cost:** per-task reviews (sonnet, scoped) + Opus whole-branch (cross-cutting, empirical) caught 2 CRITICALs the per-task pass missed. The whole-branch reviewer ran the guardian directly to prove the false-positive.
5. **bash in the air-gap must mount READ-ONLY** — `--network none` stops exfil but a writable repo mount lets chained `ls && rm -rf governance` destroy the real repo from inside. bash is inspection-only (mutations go through the chokepoint), so mount `:ro`.

## Open follow-up (logged, reviewer-acceptable) + status
- **IMPORTANT-3 partial:** in-loop content-gate covers edit_file/write_file + candidate + noop; a non-candidate in-loop helper touch is still only path-gated until GATE re-inspects. Worth a future task (run guardian inside Venom handlers for all touched paths).
- Cosmetic Minors logged in `.superpowers/sdd/progress.md`.
- **NEXT GATE: human line-by-line review of PR #69733** (change_engine/orchestrator/runners/saga/tool_executor) → merge. Evidence: `docs/superpowers/specs/2026-06-26-anti-venom-hardening-verified.md` + per-task reports. GCP confirmed 0 instances ($0). Related: [[feedback_security_filter_must_be_wired]] (a filter with zero callers is theater — verify the guard is ON the mandatory path).
