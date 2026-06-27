---
title: Project Goal Relevance Scoring
modules: []
status: historical
source: project_goal_relevance_scoring.md
---

Item 3 (Apr 10, 2026): upgraded intake goal-alignment from a silent int-returning helper to a rich, fault-visible, evidence-surfacing path.

**Why:** The old `alignment_boost()` at strategic_direction.py returned a bare int and was wrapped in a silent `try/except: pass` inside `_compute_priority`, throwing away both the raw relevance score AND any exception. Signals that hit three high-confidence goals got the same priority bump as signals that hit one low-confidence goal, and a broken GoalTracker looked identical to a healthy one with no matches — a textbook Manifesto §7 observability violation.

**How to apply:** When touching intake prioritization or goal-alignment, use `GoalTracker.alignment_context()` (returns a `GoalAlignment` dataclass with `boost`, `raw_score`, `top_goal_id`, `matched_count`, `score_multiplier`). `alignment_boost()` still exists as a thin compat wrapper for the pre-existing pytest suite and v1 callers. `_compute_priority` now returns `Tuple[int, Optional[GoalAlignment]]` — all three callsites in `unified_intake_router.py` (ingest 298, retry 637, WAL replay 668) unpack it; the ingest path mutates `envelope.evidence` in place via `alignment.as_evidence()` (safe: the evidence dict is mutable even on frozen envelopes per `intent_envelope.py:166`). Broken scorers log warn-once at WARNING, debug-after, counted in `_goal_alignment_failures`; `goal_alignment_failure_stats()` exposes the counter for health dashboards.

**Tuning knobs:** `JARVIS_GOAL_SCORE_DIVISOR` (default 10.0), `JARVIS_GOAL_SCORE_MULT_MIN` (1.0), `JARVIS_GOAL_SCORE_MULT_MAX` (3.0) control how raw relevance score translates into a priority multiplier. A signal scoring 30+ (path + tag + keyword with high priority_weight) caps at 3× the base boost so one monster-score goal can't starve the queue.

**Smoke test:** `/tmp/claude/test_goal_relevance_smoke.py` — 12 tests covering dataclass shape, no-match, single-keyword, multi-signal scaled boost, clamping, v1 compat, tuple contract, no-tracker branch, broken-tracker visibility, and end-to-end ingest evidence stashing. Uses `created_at = time.time() + 3600` to pin staleness at 1.0 for deterministic raw-score asserts.
