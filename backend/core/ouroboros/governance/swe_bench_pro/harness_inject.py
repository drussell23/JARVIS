"""SWE-Bench-Pro battle-test harness boot hook.

Mirrors the L2 exercise corpus precedent at
``backend/core/ouroboros/governance/l2_exercise_seed.maybe_inject_exercise_at_boot``
- master-flag-gated boot-time injection that lifts cached
ProblemSpec records into per-problem worktrees + envelopes + the
canonical IntakeLayerService.ingest_envelope surface.

Composition discipline
----------------------

  * Composes ONLY canonical surfaces:
      - Phase A ``load_problem`` / ``list_cached_problems``
      - Phase B.1 ``prepare_problem`` (returns PreparedProblem)
      - Phase B.2.1 ``build_evaluation_envelope`` (canonical
        IntentEnvelope shape)
      - Canonical ``IntakeLayerService.ingest_envelope`` (the
        same surface Phase 9 cadence synthetic + L2 exercise
        corpus inject through)

  * NO parallel worktree manager, NO parallel envelope construction,
    NO direct UnifiedIntakeRouter access - everything routes
    through Phase B.1 / B.2.1 / IntakeLayerService.

  * Master flag JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED
    (default-FALSE per section 33.1) is ORTHOGONAL to Phase A's
    JARVIS_SWE_BENCH_PRO_ENABLED. Operators can have the loader
    enabled (e.g., unit tests / offline scoring) without auto-
    injecting at every harness boot.

  * Two-tier instance-id selection:
      1. ``JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS`` (CSV)
         takes priority - explicit operator-chosen problem set
      2. ``JARVIS_SWE_BENCH_PRO_INJECT_COUNT`` (INT, default 1)
         takes first-N from ``list_cached_problems()``

  * Section 7 fail-closed: NEVER raises into harness. Boot MUST
    NEVER fail. asyncio.CancelledError propagates per orchestrator
    POSTMORTEM convention.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from typing import Any, List, Optional

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
    list_cached_problems,
    load_problem,
)
from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
    build_evaluation_envelope,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    HarnessOutcome,
    prepare_problem,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.HarnessInject")


# ===========================================================================
# Env vocabulary
# ===========================================================================


HARNESS_INJECT_ENABLED_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED"
)
INJECT_COUNT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_INJECT_COUNT"
INJECT_INSTANCE_IDS_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS"
)


_DEFAULT_INJECT_COUNT: int = 1


# ===========================================================================
# Closed 5-value taxonomy (AST-pinned; mirrors L2 exercise verdict shape)
# ===========================================================================


class SWEBenchProInjectionVerdict(str, enum.Enum):
    """Five canonical outcomes for maybe_inject_swe_bench_at_boot."""

    INJECTED = "injected"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_NO_PROBLEMS = "skipped_no_problems"
    FAILED_LOAD = "failed_load"
    FAILED_INJECT = "failed_inject"


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def harness_inject_enabled() -> bool:
    raw = os.environ.get(
        HARNESS_INJECT_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in ("true", "1", "yes", "on")


def inject_count() -> int:
    raw = os.environ.get(INJECT_COUNT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_INJECT_COUNT
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be > 0")
        return value
    except (ValueError, TypeError):
        logger.warning(
            "[SWEBenchPro.HarnessInject] invalid %s=%r - using default %d",
            INJECT_COUNT_ENV_VAR, raw, _DEFAULT_INJECT_COUNT,
        )
        return _DEFAULT_INJECT_COUNT


def inject_instance_ids() -> List[str]:
    raw = os.environ.get(INJECT_INSTANCE_IDS_ENV_VAR, "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# ===========================================================================
# Internal helpers
# ===========================================================================


def _resolve_instance_ids() -> List[str]:
    """Two-tier resolution: CSV override > first-N from cache.
    Pure function over env state; NEVER raises."""
    explicit = inject_instance_ids()
    if explicit:
        return explicit
    cached = list_cached_problems()
    if not cached:
        return []
    return list(cached)[: inject_count()]


# ===========================================================================
# Public API - maybe_inject_swe_bench_at_boot
# ===========================================================================


async def maybe_inject_swe_bench_at_boot(
    intake_service: Any,
) -> SWEBenchProInjectionVerdict:
    """Battle-test harness boot hook for SWE-Bench-Pro injection.

    Orchestrates the four-stage injection pipeline:

      1. Master-flag check  -> SKIPPED_DISABLED if False
      2. Instance-id resolution (CSV > count) -> SKIPPED_NO_PROBLEMS
         if neither tier yielded any
      3. Per-problem: Phase A load_problem + Phase B.1 prepare_problem
         + Phase B.2.1 build_evaluation_envelope
      4. Canonical IntakeLayerService.ingest_envelope submission

    Returns one of five SWEBenchProInjectionVerdict outcomes.
    NEVER raises into the caller; asyncio.CancelledError propagates
    (orchestrator POSTMORTEM contract).

    The boot hook is called once per battle-test session AFTER the
    IntakeLayerService has booted. Composes the canonical
    intake_service.ingest_envelope surface - no parallel router /
    no parallel worktree manager / no parallel envelope shape.
    """
    if not harness_inject_enabled():
        return SWEBenchProInjectionVerdict.SKIPPED_DISABLED
    if intake_service is None:
        logger.debug(
            "[SWEBenchPro.HarnessInject] intake_service is None - "
            "cannot inject"
        )
        return SWEBenchProInjectionVerdict.FAILED_INJECT
    try:
        instance_ids = _resolve_instance_ids()
        if not instance_ids:
            logger.info(
                "[SWEBenchPro.HarnessInject] master flag ON but "
                "no problems available (cache empty + no CSV "
                "override) - nothing to inject"
            )
            return SWEBenchProInjectionVerdict.SKIPPED_NO_PROBLEMS

        loaded_count = 0
        injected_count = 0
        for instance_id in instance_ids:
            problem, load_outcome = load_problem(instance_id)  # Phase A loader is sync
            if problem is None:
                logger.info(
                    "[SWEBenchPro.HarnessInject] could not load "
                    "problem=%r (outcome=%s) - skipping",
                    instance_id,
                    getattr(load_outcome, "value", "?"),
                )
                continue
            loaded_count += 1

            prepared, harness_outcome = await prepare_problem(problem)
            if prepared is None or harness_outcome != HarnessOutcome.READY:
                logger.warning(
                    "[SWEBenchPro.HarnessInject] prepare_problem "
                    "failed for instance=%r outcome=%s - skipping",
                    instance_id,
                    getattr(harness_outcome, "value", "?"),
                )
                continue

            envelope = build_evaluation_envelope(problem, prepared)

            try:
                ingest_result = await intake_service.ingest_envelope(
                    envelope,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - fail-open per contract
                logger.warning(
                    "[SWEBenchPro.HarnessInject] ingest_envelope "
                    "raised for instance=%r - skipping",
                    instance_id, exc_info=True,
                )
                continue

            if not ingest_result:
                logger.warning(
                    "[SWEBenchPro.HarnessInject] ingest_envelope "
                    "returned False for instance=%r - skipping",
                    instance_id,
                )
                continue

            injected_count += 1
            logger.info(
                "[SWEBenchPro.HarnessInject] injected instance=%r "
                "worktree=%r causal_id=%r",
                instance_id, str(prepared.worktree_path),
                envelope.causal_id,
            )

        if loaded_count == 0:
            return SWEBenchProInjectionVerdict.FAILED_LOAD
        if injected_count == 0:
            return SWEBenchProInjectionVerdict.FAILED_INJECT
        return SWEBenchProInjectionVerdict.INJECTED
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - fail-open contract
        logger.warning(
            "[SWEBenchPro.HarnessInject] maybe_inject_swe_bench_at_boot "
            "raised", exc_info=True,
        )
        return SWEBenchProInjectionVerdict.FAILED_LOAD


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by section 33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count
    successfully registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=HARNESS_INJECT_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "SWE-Bench-Pro harness boot-hook master switch "
                "(section 33.1 default-FALSE). When ON, the "
                "battle-test harness lifts cached ProblemSpec "
                "records into per-problem worktrees + envelopes + "
                "canonical IntakeLayerService.ingest_envelope at "
                "boot time. Orthogonal to JARVIS_SWE_BENCH_PRO_ENABLED "
                "(operators can have loader enabled without auto-"
                "injecting at every boot)."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "harness_inject.py"
            ),
            example="false",
            since="v3.7 Phase 2 harness-inject (2026-05-12)",
        ),
        FlagSpec(
            name=INJECT_COUNT_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_INJECT_COUNT,
            description=(
                "When HARNESS_INJECT_ENABLED is ON and "
                "INJECT_INSTANCE_IDS is empty, the boot hook lifts "
                "the first N problems from list_cached_problems(). "
                "Default 1 keeps initial runs small. Operators "
                "scaling to soaks flip higher within their cost/wall "
                "cap budgets."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "harness_inject.py"
            ),
            example=str(_DEFAULT_INJECT_COUNT),
            since="v3.7 Phase 2 harness-inject (2026-05-12)",
        ),
        FlagSpec(
            name=INJECT_INSTANCE_IDS_ENV_VAR,
            type=FlagType.STR,
            default="",
            description=(
                "Explicit comma-separated instance_id list for the "
                "boot hook. Takes priority over INJECT_COUNT. Useful "
                "when reproducing a specific failure / soak: set "
                "this to the failing instance_ids and the harness "
                "will inject exactly those problems."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "harness_inject.py"
            ),
            example="octocat__hello-001,foo__bar-003",
            since="v3.7 Phase 2 harness-inject (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.HarnessInject] flag registration "
                "failed for %s", getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "HARNESS_INJECT_ENABLED_ENV_VAR",
    "INJECT_COUNT_ENV_VAR",
    "INJECT_INSTANCE_IDS_ENV_VAR",
    "SWEBenchProInjectionVerdict",
    "harness_inject_enabled",
    "inject_count",
    "inject_instance_ids",
    "maybe_inject_swe_bench_at_boot",
    "register_flags",
]
