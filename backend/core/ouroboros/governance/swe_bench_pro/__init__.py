"""SWE-Bench-Pro evaluation arc — PRD §40.7.9 Phase 2.

Package layout (one phase per module; all default-FALSE per §33.1):

  * ``dataset_loader``   — Phase A: ProblemSpec + cache + load.
  * (future) ``per_problem_harness`` — Phase B
  * (future) ``scorer``               — Phase C
  * (future) ``result_substrate``     — Phase D
  * (future) ``parallel_eval``        — Phase E
  * (future) ``report_card``          — Phase F

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


__all__ = [
    "LoadOutcome",
    "ProblemSpec",
    "SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION",
    "cache_dir",
    "clear_cache",
    "list_cached_problems",
    "load_problem",
    "swe_bench_pro_enabled",
]
