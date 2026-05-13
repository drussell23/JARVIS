"""SWE-Bench-Pro evaluation arc — PRD §40.7.9 Phase 2.

Package layout (one phase per module; all default-FALSE per §33.1):

  * ``dataset_loader``     — Phase A: ProblemSpec + cache + load.
  * ``per_problem_harness`` — Phase B.1: PreparedProblem + worktree.
  * ``envelope_builder``    — Phase B.2.1: PreparedProblem → IntentEnvelope.
  * ``evaluator``           — Phase B.2.2: evaluate_problem async façade.
  * ``scorer``              — Phase C: score_evaluation pass/partial/fail.
  * ``result_store``        — Phase D: EvaluationResultStore + JSONL audit.
  * (future) ``parallel_eval``    — Phase E
  * (future) ``report_card``      — Phase F

Composition discipline (mirrors :mod:`l2_exercise_seed` pattern):

  * Authority asymmetry: the package consumes canonical surfaces
    (``WorktreeManager``, ``RepairEngine``, ``TestRunner``,
    ``subagent_scheduler``) but never imports policy substrates
    (``orchestrator``, ``iron_gate``, ``change_engine``,
    ``policy_engine``, ``risk_tier``, ``candidate_generator``).
    Phase A is read-only data; Phase B+ compose execution surfaces.
  * §33.1 default-FALSE master flags: every operator-facing knob
    starts off; production behavior byte-identical when unset.
  * §33.5 symmetric ``to_dict``/``from_dict`` on every frozen
    dataclass.
  * Closed taxonomies (AST bytes-pinned by spine).
  * Fail-open contract on every public surface (NEVER raises;
    ``asyncio.CancelledError`` is the sole exception that
    propagates per orchestrator POSTMORTEM convention).
"""
from __future__ import annotations

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    LoadOutcome,
    ProblemSpec,
    SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION,
    cache_dir,
    clear_cache,
    list_cached_problems,
    load_problem,
    swe_bench_pro_enabled,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    DiffCaptureOutcome,
    HarnessOutcome,
    PER_PROBLEM_HARNESS_SCHEMA_VERSION,
    PreparedProblem,
    capture_produced_patch,
    cleanup_prepared,
    prepare_problem,
    repo_cache_path,
    worktree_base_path,
)
from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
    ENVELOPE_SOURCE,
    ENVELOPE_URGENCY_ENV_VAR,
    build_evaluation_envelope,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EVAL_TIMEOUT_ENV_VAR,
    EVALUATION_RESULT_SCHEMA_VERSION,
    EvaluationOutcome,
    EvaluationResult,
    evaluate_problem,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    SCORE_GIT_OP_TIMEOUT_ENV_VAR,
    SCORE_REJECT_TEST_MODS_ENV_VAR,
    SCORE_TEST_TIMEOUT_ENV_VAR,
    SCORING_RESULT_SCHEMA_VERSION,
    ScoreOutcome,
    ScoringResult,
    score_evaluation,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    RESULT_PATH_ENV_VAR,
    RESULT_PERSISTENCE_ENABLED_ENV_VAR,
    RESULT_RECORD_SCHEMA_VERSION,
    EvaluationRecord,
    EvaluationResultStore,
    get_default_store,
    record_evaluation,
    replay_default_store_from_disk,
    reset_default_store,
)


__all__ = [
    # Phase A — dataset loader
    "LoadOutcome",
    "ProblemSpec",
    "SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION",
    "cache_dir",
    "clear_cache",
    "list_cached_problems",
    "load_problem",
    "swe_bench_pro_enabled",
    # Phase B.1 — per-problem harness substrate
    "DiffCaptureOutcome",
    "HarnessOutcome",
    "PER_PROBLEM_HARNESS_SCHEMA_VERSION",
    "PreparedProblem",
    "capture_produced_patch",
    "cleanup_prepared",
    "prepare_problem",
    "repo_cache_path",
    "worktree_base_path",
    # Phase B.2.1 — envelope builder
    "ENVELOPE_SOURCE",
    "ENVELOPE_URGENCY_ENV_VAR",
    "build_evaluation_envelope",
    # Phase B.2.2 — evaluator façade
    "EVAL_TIMEOUT_ENV_VAR",
    "EVALUATION_RESULT_SCHEMA_VERSION",
    "EvaluationOutcome",
    "EvaluationResult",
    "evaluate_problem",
    # Phase C — scorer
    "SCORE_GIT_OP_TIMEOUT_ENV_VAR",
    "SCORE_REJECT_TEST_MODS_ENV_VAR",
    "SCORE_TEST_TIMEOUT_ENV_VAR",
    "SCORING_RESULT_SCHEMA_VERSION",
    "ScoreOutcome",
    "ScoringResult",
    "score_evaluation",
    # Phase D — result substrate
    "RESULT_PATH_ENV_VAR",
    "RESULT_PERSISTENCE_ENABLED_ENV_VAR",
    "RESULT_RECORD_SCHEMA_VERSION",
    "EvaluationRecord",
    "EvaluationResultStore",
    "get_default_store",
    "record_evaluation",
    "replay_default_store_from_disk",
    "reset_default_store",
]
