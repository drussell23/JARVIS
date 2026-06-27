---
title: Project Exploration Ledger Validated
modules: [backend/core/ouroboros/governance/background_agent_pool.py, backend/core/ouroboros/governance/complexity_classifier.py, backend/core/ouroboros/governance/candidate_generator.py, backend/core/ouroboros/governance/tool_executor.py]
status: historical
source: project_exploration_ledger_validated.md
---

**Supersedes earlier diagnosis:** Session B's `fallback_semaphore_wait=121.53s` reading was a red herring. The `sem_wait_total_s` field in the exhaustion report is a misnomer — it measures time from sem acquire start to release, not just queue-wait time, so it looked like contention but was really the full op lifetime counting down toward the pool ceiling.

**Status as of 2026-04-14 (sessions `bt-2026-04-15-040118` / `bt-2026-04-15-041413` / `bt-2026-04-15-044627`):**

**What's proven:**

1. **ExplorationLedger shadow scoring works.** Catches `4× read_file` as `score=3.00 categories=comprehension would_pass=False` while the legacy int-counter gate passes `4 ≥ 2`.
2. **Ledger enforcement works.** `JARVIS_EXPLORATION_LEDGER_ENABLED=true` flips it to a hard Iron Gate rejection — `ExplorationLedger(decision) insufficient` is the production log line.
3. **Retry mechanism fires and injects episodic feedback.** `Injecting N episodic failure(s) into retry context` is the cue.
4. **The model adapts to retry feedback with categorically diverse tools.** Session C proved Class A: attempt 1 made 0 tool calls (`score=0.00 unique=0`), retry round 0 called `read_file, read_file, list_dir, list_symbols` — 3 distinct tools across ≥2 categories. **The retry feedback loop is more effective than initial prompting at producing diverse exploration.**
5. **The true retry killer was `BackgroundAgentPool` per-op wall-time ceiling** (`JARVIS_BG_WORKER_OP_TIMEOUT_S`, default 360s). Session C under `BG_POOL_SIZE=1` isolation (zero sem contention, `sem_wait=0.0s` on both acquires) STILL hit `exceeded pool ceiling (360s) — freeing slot` at 370s wall time, cancelling the retry synthesis mid-stream with 131s of nominal budget nominally remaining. The `CancelledError` on `_call_fallback`'s `wait_for` was propagated from above.

**Fix landed (this session, commit pending review):** `background_agent_pool.py` around line 648 now reads a route-aware ceiling. Ops where `len(op.context.target_files) >= 4` (file count being the CLASSIFY-deterministic predictor of `complex` complexity → COMPLEX route) get `JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S` (default `900s`, 2.5× base). Everything else keeps the 360s anti-hang watchdog unchanged. Uses file count instead of `task_complexity` because CLASSIFY hasn't run at worker pickup — but the classifier (`complexity_classifier.py:161-165`) is deterministic on file count, so the prediction is equivalent without the phase-ordering hazard.

**Instrumentation that cracked the case (commit `614009ec05`):**
- `candidate_generator.py` Fallback sem acquire/release INFO with `phase=GENERATE` vs `phase=GENERATE_RETRY` labeling
- `candidate_generator.py` Primary sem acquire/release (symmetry)
- `tool_executor.py` `tool_round_complete` INFO logged BEFORE the next synthesis call, so killed streams still leave an audit trail of which tools ran

Without these four log lines, the Session B misdiagnosis would have persisted and the first fix attempt would have tuned a semaphore that wasn't broken.

**How to apply:**

- **Next battle test of exploration loop should use `JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S=900` by default** (already the code default, just informational for operators setting env).
- **For future retry-reliability bugs, read the instrumentation first.** The `phase=` label on sem acquires + `tool_round_complete` names are the debugging truth. `sem_wait_total_s` in the exhaustion report can lie about the cause.
- **Ship instrumentation before fix for concurrency issues.** This session's lesson is that additive logging is the cheapest root-cause analysis tool, and should be the first move when a bug straddles multiple async layers.
- **Defer still:** `_DEFAULT_FLOORS` missing `complex` entry (complex ops silently map to moderate floors — minor calibration gap); semaphore reserved-slot / per-route fairness work (not the root cause); PLAN phase empty-error bug (separate ticket, reproducible).

**What it means:** The `ExplorationLedger(decision) → retry with episodic feedback → diversified tools` control loop is a complete, measurable, working behavioral steering mechanism. It is the first thing in O+V that *makes the model smarter under feedback* rather than just routing/gating it. The pool-ceiling fix gives that loop the wall-clock room to complete. Session D (next) should see a real retry land with a second `ExplorationLedger(decision)` evaluation — the first empirical measurement of whether the adapted retry actually clears the gate.
