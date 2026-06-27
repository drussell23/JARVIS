---
title: Project Multifile Enforcement Proven
modules: [backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/multi_file_coverage_gate.py, backend/core/ouroboros/governance/provider_exhaustion_watcher.py, tests/governance/intake/sensors/test_test_failure_sensor_dedup.py, dedup.py, orchestrator.py, backend/core/ouroboros/governance/interactive_repair.py]
status: historical
source: project_multifile_enforcement_proven.md
---

**Status (2026-04-15):** Sessions Q/R/S closed the multi-file enforcement arc. Do NOT re-litigate the enforcement path — it is proven.

**What's proven deterministic:**
- Parser accepts `files: [...]` as authoritative, synthesizes `file_path`/`full_content` from `files[0]` for downstream single-file consumers (`providers.py _parse_generation_response` line ~3024)
- Iron Gate 5 (`multi_file_coverage_gate.py`) passes silently on full coverage, rejects on subset; 31 unit tests, zero false rejections across Q/R/S in production
- Prompt hint `_build_multi_file_contract_block` injects `files: [...]` contract when `len(ctx.target_files) > 1` into lean + full-mode prompts
- Per-op exhaustion dedup (`provider_exhaustion_watcher.py record_exhaustion(op_id=...)`, commit 37a371e65d) visible in production as `counted_ops=N` in reset logs
- Post-gate visibility: LSP + TestRunner both walk the full multi-file candidate in Session S; `LSP found 1 type errors in [dedup, ttl, isolation]` and `Resolved 45 test targets for 4 changed files` both confirmed on op-019d92e8-5b11

**What's NOT proven and is a SEPARATE track:**
- `APPLY mode=multi candidate_files=N` + N files landing on disk through `_apply_multi_file_candidate`
- Blocker: Session S stalled in `VALIDATE_RETRY → L2 Repair` loop on a Python type error in `test_test_failure_sensor_dedup.py`. Root cause is timebox misalignment:
  - `pytest timed out after 30.0s — killing process` (the sandbox pytest cap)
  - L2 Repair `Iteration 2/5 starting (49s elapsed, 11s remaining)` — L2's 60s timebox gets cut mid-repair
  - Session idle-timed out at 10 min before L2 could converge

**Follow-up A (reliability track, NOT multi-file enforcement):**
- **Hypothesis:** raising `JARVIS_TEST_TIMEOUT_S=120` + verifying L2 iteration budget ≥ N_iters × pytest_timeout + overhead will allow L2 to converge
- **Success criterion:** one op (any N ≥ 2) reaches `APPLY mode=multi + DECISION applied + POSTMORTEM root_cause=none` without idle timeout
- **Keep `JARVIS_EXPLORATION_GATE=true` (default)** — S disabled it only to exercise Gate 5; Gate 5 is now proven
- **Do NOT** add retries, disable VALIDATE, or raise retry caps without explicit diagnosis. Manifesto §6: structural repair, not blind retry.

**Follow-up A — Session T result (2026-04-15, `bt-2026-04-15-211616`): HYPOTHESIS FALSIFIED.**
- Env tuning applied cleanly (`max_iterations=8, timebox=600.0s` confirmed in boot log).
- Op-019d9301 reached VALIDATE, first critique on `dedup.py` at 14:26:07, `InteractiveRepair disabled → fall through` fired at 14:26:07, then the op **silently exited at 14:27:47** (~1m40s of dead air).
- **L2 Repair NEVER RAN** — string appears exactly 1 time in the session log (boot wiring line only). No `L2 Repair Iteration N/M starting` line anywhere.
- `phase=CLASSIFY` in the final cost_governor.finish log despite the op having advanced through PLAN → GENERATE → VALIDATE → VALIDATE_RETRY — a **ctx-reference mismatch** in the outer `finally` block at `orchestrator.py:748` that points at the retry loop rebinding ctx locally without propagating.
- Multi-file enforcement (parser + Gate 5 + prompt hint + post-gate visibility) worked identically to Sessions Q–S — zero `multi_file_coverage` rejections, model made 3 parallel `read_file` calls in round 0 unprompted.
- **Revised diagnosis:** The real stall is upstream of L2, inside the `VALIDATE_RETRY` loop at `orchestrator.py:3550-3818`. Suspicious surfaces: the hardcoded `asyncio.wait_for(_repair.repair(), timeout=90.0)` at line 3787 with a swallow-all `except Exception` at 3806, AND the `InteractiveRepair` disabled-path early-return at `interactive_repair.py:98-103` which may not populate all fields the retry-loop caller expects. Also noted: `max_validate_retries: int = 2` is non-env-tunable at `orchestrator.py:209`.
- **Next-track candidates** (choose one, not all): (1) instrument VALIDATE_RETRY with INFO entry/exit markers so the next session shows exactly which line exits; (2) make `max_validate_retries` env-tunable and set to `0` to force immediate L2 dispatch, falsifiable; (3) verify `InteractiveRepairResult` disabled-path shape parity. Do NOT raise idle_timeout — that's run-longer-until-luck.

**The enforcement arc remains settled.** Four sessions (Q, R, S, T) all saw the parser accept multi-file shape, Gate 5 pass silently on full coverage, and post-gate visibility for all target files. The blocker is agentic persistence in the VALIDATE_RETRY loop, not multi-file enforcement. Full Session T postmortem in OUROBOROS.md.

**Anti-goals to remember:**
- Don't treat "no APPLY mode=multi in prod" as a multi-file enforcement bug unless new evidence appears.
- Don't conflate "deterministic APPLY fan-out proof" (Option C — feed a hand-crafted 4-file candidate through the pipeline) with "Session S proves multi-file enforcement." Option C is a plumbing regression test, not part of this arc.
- Full postmortem with failure-mode table and quoted log lines: `docs/architecture/OUROBOROS.md` → "Sessions Q–S arc".

**When investigating multi-file issues in the future:**
1. First check `JARVIS_MULTI_FILE_ENFORCEMENT` (default true) and `JARVIS_MULTI_FILE_GEN_ENABLED` (default true) — both need to be on
2. Grep session `debug.log` for `multi_file_coverage` — if present, Gate 5 actually rejected a candidate (read the `missing` paths from retry feedback)
3. If Gate 5 silently passed (no rejection) but only 1 file landed, check for `APPLY mode=single` vs `APPLY mode=multi` in the log — if `single` despite `target_files > 1`, the candidate went through the legacy path somehow (check parser path again)
4. If the op never reached APPLY, it's a VALIDATE/L2 issue — see Follow-up A hypothesis above, don't blame multi-file.
