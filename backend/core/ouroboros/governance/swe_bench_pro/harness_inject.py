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
AUTOSCORE_ENABLED_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED"
)


_DEFAULT_INJECT_COUNT: int = 1


# ===========================================================================
# Closed 5-value taxonomy (AST-pinned; mirrors L2 exercise verdict shape)
# ===========================================================================


class SWEBenchProInjectionVerdict(str, enum.Enum):
    """Seven canonical outcomes for maybe_inject_swe_bench_at_boot.

    ``INJECTED_AUTOSCORE`` is the closed-loop outcome: when the
    autoscore flag is ON the boot hook hands the loaded ProblemSpec
    set to the existing ``parallel_evaluate`` rig (Phase E → B.2.2 →
    Phase C → Phase D) as a background task, so each solve op is
    auto-scored against its gold patch on its terminal event. The
    legacy ``INJECTED`` outcome is the open-loop path (ingest only,
    no scoring) — preserved byte-identical when the flag is OFF.

    ``MISCONFIGURED_PHASE_A_DISABLED`` is the Slice 12N config-
    hardening outcome: when the operator has set
    ``JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true`` but the
    Phase A master flag ``JARVIS_SWE_BENCH_PRO_ENABLED`` is OFF,
    every ``load_problem()`` call would return ``MISSING`` (the
    Phase A loader short-circuits on the master gate). The prior
    behavior surfaced this as an ambiguous ``FAILED_LOAD`` per
    candidate instance — operators couldn't tell "config error"
    from "real missing problem". Slice 12N halts cleanly with a
    clear distinct verdict BEFORE any load/ingest attempt, so
    no budget is burned and the operator sees an unambiguous
    actionable signal.
    """

    INJECTED = "injected"
    INJECTED_AUTOSCORE = "injected_autoscore"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_NO_PROBLEMS = "skipped_no_problems"
    FAILED_LOAD = "failed_load"
    FAILED_INJECT = "failed_inject"
    MISCONFIGURED_PHASE_A_DISABLED = "misconfigured_phase_a_disabled"


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


def autoscore_enabled() -> bool:
    """Closed-loop autoscore master switch (§33.1 default-FALSE).

    When ON, the boot hook composes the existing ``parallel_evaluate``
    rig (Phase E) so injected solve ops are scored against their gold
    patch on terminal — closing the open-loop. When OFF (default) the
    legacy open-loop ingest path is byte-identical. NEVER raises."""
    raw = os.environ.get(
        AUTOSCORE_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in ("true", "1", "yes", "on")


# ===========================================================================
# Internal helpers
# ===========================================================================


def _resolve_instance_ids() -> List[str]:
    """Three-tier resolution (strict precedence; NEVER raises):

      1. ``INJECT_INSTANCE_IDS`` CSV override — explicit operator
         control always wins.
      2. **GeometricInstanceSampler** (when
         ``JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED`` is ON):
         a deterministic (known-good single-file, known-hard
         multi-file) discriminator pair curated from the dataset's
         own gold-patch geometry — zero hardcoded IDs.  This is the
         Stage-2 rubric path.  If the sampler cannot form a valid
         pair it returns ``None`` and resolution falls through.
      3. First-N from ``list_cached_problems()`` (legacy default).

    Pure function over env + dataset state."""
    explicit = inject_instance_ids()
    if explicit:
        return explicit

    # Tier 2 — geometric self-curation (opt-in). Compose the
    # sampler; never let an import/scan failure break the legacy
    # fallthrough (fail-open per §7).
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.geometric_sampler import (  # noqa: E501
            geometric_sampler_enabled,
            sample_discriminator_pair,
        )

        if geometric_sampler_enabled():
            sample = sample_discriminator_pair()
            if sample is not None:
                logger.info(
                    "[SWEBenchPro.HarnessInject] geometric sampler "
                    "curated discriminator pair: known_good=%r "
                    "known_hard=%r (scanned %d records)",
                    sample.known_good_id, sample.known_hard_id,
                    sample.scanned_count,
                )
                return sample.instance_ids
            logger.warning(
                "[SWEBenchPro.HarnessInject] geometric sampler ON "
                "but yielded no valid pair — falling through to "
                "cache first-N"
            )
    except Exception:  # noqa: BLE001 — fail-open (legacy path intact)
        logger.warning(
            "[SWEBenchPro.HarnessInject] geometric sampler tier "
            "raised — falling through to cache first-N",
            exc_info=True,
        )

    cached = list_cached_problems()
    if not cached:
        return []
    return list(cached)[: inject_count()]


# Strong refs to fire-and-forget autoscore driver tasks — without
# this the event loop may GC a pending task ("Task was destroyed but
# it is pending"). Discarded in the task's own done-callback.
_AUTOSCORE_DRIVER_TASKS: "set" = set()


async def _drive_parallel_evaluate(
    specs: "List[ProblemSpec]", intake_service: Any,
) -> None:
    """Consume the EXISTING ``parallel_evaluate`` async generator
    (Phase E → B.2.2 evaluate_problem → Phase C score → Phase D
    record). Pure composition — ZERO net-new evaluation logic here;
    this only drains the iterator and logs each verdict as it lands.

    Runs as a fire-and-forget background task so the soak loop keeps
    running (solve ops must reach their terminal event for the
    broker to wake the scorer). NEVER raises into the loop;
    asyncio.CancelledError propagates."""
    from backend.core.ouroboros.governance.swe_bench_pro.parallel_eval import (  # noqa: E501
        parallel_evaluate,
    )

    # Hold an explicit reference to the async generator so we can
    # close it in our OWN coroutine context on cancellation. The v16
    # `aclose(): asynchronous generator is already running` crash came
    # from the harness force-cancelling this task while the bare
    # `async for` was suspended at the generator's yield and a
    # concurrent aclose ran. Owning the agen + closing it in `finally`
    # (suppressing the benign races) keeps cancellation clean.
    agen = parallel_evaluate(specs, intake_service=intake_service)
    try:
        async for record in agen:
            ev = getattr(record, "evaluation", None)
            sc = getattr(record, "scoring", None)
            logger.info(
                "[SWEBenchPro.HarnessInject] autoscore verdict: "
                "instance=%r eval_outcome=%s score_outcome=%s "
                "diagnostic=%r",
                getattr(ev, "instance_id", None)
                or getattr(sc, "problem_instance_id", "?"),
                getattr(getattr(ev, "outcome", None), "value", "?"),
                getattr(getattr(sc, "outcome", None), "value", "?"),
                getattr(sc, "diagnostic", "")[:160]
                if getattr(sc, "diagnostic", "") else "",
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail-open (soak must not die)
        logger.warning(
            "[SWEBenchPro.HarnessInject] autoscore driver raised — "
            "closed loop aborted, soak continues", exc_info=True,
        )
    finally:
        # Close the generator in THIS coroutine's context. Suppress
        # the two benign races: (1) RuntimeError 'aclose(): ... already
        # running' if cancellation is already unwinding it; (2)
        # CancelledError re-raised by aclose during shutdown. Either
        # way the generator's own finally (broker unsubscribe, worktree
        # cleanup) has run or will run within parallel_evaluate.
        try:
            await agen.aclose()
        except (RuntimeError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[SWEBenchPro.HarnessInject] autoscore agen.aclose() "
                "raised (benign at shutdown)", exc_info=True,
            )


def autoscore_work_in_flight() -> bool:
    """Session-liveness probe: True while any fire-and-forget
    autoscore driver task is still running.

    Registered with the harness ActivityMonitor at boot so a
    backgrounded ``parallel_evaluate`` counts as "the organism is
    busy" — closing the v16 ``bt-2026-05-16-085224`` failure where
    the session idle-reaped a still-running discriminator because
    the fire-and-forget task was invisible to the idle counter.
    NEVER raises (a probe must never break the ActivityMonitor)."""
    try:
        return any(not t.done() for t in _AUTOSCORE_DRIVER_TASKS)
    except Exception:  # noqa: BLE001 — probe must be total
        return False


async def await_autoscore_drain(grace_s: float = 30.0) -> None:
    """Shutdown helper: give in-flight autoscore tasks a bounded
    grace to finish (so a near-complete Phase C/D verdict can land
    and the ``parallel_evaluate`` generator closes in its OWN
    coroutine context), then cancel + await any stragglers.

    Mirrors the harness's existing cancel→await component-shutdown
    shape. Bounded — never blocks shutdown unboundedly. NEVER raises
    except to propagate an outer ``CancelledError`` after still
    cancelling children (clean teardown contract)."""
    tasks = [t for t in _AUTOSCORE_DRIVER_TASKS if not t.done()]
    if not tasks:
        return
    try:
        _done, pending = await asyncio.wait(
            tasks, timeout=max(0.0, grace_s),
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
    except Exception:  # noqa: BLE001 — fail-open (shutdown must finish)
        logger.debug(
            "[SWEBenchPro.HarnessInject] await_autoscore_drain raised",
            exc_info=True,
        )


async def _inject_autoscore(
    instance_ids: "List[str]", intake_service: Any,
) -> SWEBenchProInjectionVerdict:
    """Closed-loop injection: load each ProblemSpec (its ``gold_patch``
    rides in-memory — the operator's "contextual state passing",
    satisfied by the spec itself, no re-fetch) and hand the set to
    the existing ``parallel_evaluate`` rig as a background task.

    ``parallel_evaluate`` internally does subscribe-before-ingest
    (race-free), prepare, build_envelope, ingest, await the
    ``operation_terminal`` event, capture the produced patch, score
    (Phase C) and record (Phase D) — so this function adds NO
    evaluation/scoring/ingest logic of its own. NEVER raises."""
    specs: "List[ProblemSpec]" = []
    for instance_id in instance_ids:
        problem, load_outcome = load_problem(instance_id)
        if problem is None:
            logger.info(
                "[SWEBenchPro.HarnessInject] autoscore: could not "
                "load problem=%r (outcome=%s) — skipping",
                instance_id, getattr(load_outcome, "value", "?"),
            )
            continue
        specs.append(problem)

    if not specs:
        return SWEBenchProInjectionVerdict.FAILED_LOAD

    # Slice 2 naming convention — the evaluator_trace_observer (Slice 1)
    # filters tasks by ``swe_bench_pro:`` prefix and derives the
    # EvaluatorPhase from the colon-suffixed name. Driver task carries
    # the first instance id so the trace frame can surface which batch
    # it owns. AST-pinned: every asyncio.create_task in evaluator path
    # MUST carry ``name=swe_bench_pro:<phase>:<id>``.
    _first_id = next(iter(instance_ids), "")
    _task_name = (
        f"swe_bench_pro:harness_inject:{_first_id}"
        if _first_id else "swe_bench_pro:harness_inject"
    )
    task = asyncio.create_task(
        _drive_parallel_evaluate(specs, intake_service),
        name=_task_name,
    )
    _AUTOSCORE_DRIVER_TASKS.add(task)
    task.add_done_callback(_AUTOSCORE_DRIVER_TASKS.discard)

    logger.info(
        "[SWEBenchPro.HarnessInject] autoscore: %d ProblemSpec(s) "
        "handed to parallel_evaluate (background) — closed loop "
        "armed, verdicts land on each solve op's terminal event",
        len(specs),
    )
    return SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE


# ===========================================================================
# Public API - maybe_inject_swe_bench_at_boot
# ===========================================================================


async def maybe_inject_swe_bench_at_boot(
    intake_service: Any,
) -> SWEBenchProInjectionVerdict:
    """Battle-test harness boot hook for SWE-Bench-Pro injection.

    Orchestrates the four-stage injection pipeline:

      1. Master-flag check  -> SKIPPED_DISABLED if False
      2. Instance-id resolution (CSV > geometric sampler > count)
         -> SKIPPED_NO_PROBLEMS if no tier yielded any
      3. Closed-loop branch (autoscore flag ON): hand the loaded
         ProblemSpec set to the existing parallel_evaluate rig as a
         background task → INJECTED_AUTOSCORE. Each solve op is
         scored (Phase C) + recorded (Phase D) on its terminal event.
      3'. Legacy open-loop (flag OFF, default — byte-identical):
         per-problem Phase B.1 prepare_problem + Phase B.2.1
         build_evaluation_envelope + canonical
         IntakeLayerService.ingest_envelope submission.

    Returns one of six SWEBenchProInjectionVerdict outcomes.
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

    # Slice 12N — config-hardening preflight. When operator has
    # turned the inject hook ON but the Phase A loader master flag
    # is OFF, every ``load_problem()`` call returns ``MISSING`` (the
    # Phase A loader short-circuits on the master gate). The prior
    # behavior surfaced this as ambiguous ``FAILED_LOAD`` per
    # candidate instance — operators could not tell "config error"
    # from "real missing problem". Halt cleanly with a distinct
    # verdict + clear actionable log line BEFORE any load/ingest
    # attempt, so no budget is burned and no worktree is created.
    # Composes the canonical ``swe_bench_pro_enabled()`` predicate
    # rather than re-reading the env directly (single source of
    # truth — same gate that ``load_problem`` itself uses).
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (  # noqa: E501
            swe_bench_pro_enabled,
        )
        if not swe_bench_pro_enabled():
            logger.warning(
                "[SWEBenchPro.HarnessInject] MISCONFIGURED: "
                "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true "
                "but JARVIS_SWE_BENCH_PRO_ENABLED is OFF — every "
                "load_problem() call would return MISSING. Set "
                "JARVIS_SWE_BENCH_PRO_ENABLED=true to proceed. "
                "No budget burned. No worktree created."
            )
            return SWEBenchProInjectionVerdict.MISCONFIGURED_PHASE_A_DISABLED
    except Exception:  # noqa: BLE001 — boot-must-never-fail
        # Defensive: if the predicate import itself fails for some
        # reason, fall through to the legacy resolution path. The
        # subsequent FAILED_LOAD verdict will at least surface a
        # downstream signal even if the new distinct verdict is
        # unreachable.
        logger.debug(
            "[SWEBenchPro.HarnessInject] swe_bench_pro_enabled "
            "predicate raised — falling through to legacy path",
            exc_info=True,
        )

    try:
        instance_ids = _resolve_instance_ids()
        if not instance_ids:
            logger.info(
                "[SWEBenchPro.HarnessInject] master flag ON but "
                "no problems available (cache empty + no CSV "
                "override) - nothing to inject"
            )
            return SWEBenchProInjectionVerdict.SKIPPED_NO_PROBLEMS

        # ── Closed-loop autoscore (§33.1 opt-in) ──────────────────
        # When ON, compose the EXISTING parallel_evaluate rig so each
        # solve op is scored against its gold patch on terminal.
        # Flag-gated; the legacy open-loop path below is byte-
        # identical when the flag is OFF (default).
        if autoscore_enabled():
            return await _inject_autoscore(instance_ids, intake_service)

        # ── Legacy open-loop path (autoscore OFF — byte-identical) ─
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
        FlagSpec(
            name=AUTOSCORE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Closed-loop autoscore (section 33.1 default-FALSE). "
                "When ON, the boot hook hands the loaded ProblemSpec "
                "set to the existing parallel_evaluate rig (Phase E "
                "→ B.2.2 evaluate_problem → Phase C score → Phase D "
                "record) as a background task — each injected solve "
                "op is scored against its gold patch on its terminal "
                "event, closing the open loop. When OFF the legacy "
                "open-loop ingest path is byte-identical. Required "
                "for any Stage-2 discriminator rubric run."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "harness_inject.py"
            ),
            example="false",
            since="v3.7 Stage 2 autoscore wiring (2026-05-16)",
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
    "AUTOSCORE_ENABLED_ENV_VAR",
    "SWEBenchProInjectionVerdict",
    "harness_inject_enabled",
    "inject_count",
    "inject_instance_ids",
    "autoscore_enabled",
    "autoscore_work_in_flight",
    "await_autoscore_drain",
    "maybe_inject_swe_bench_at_boot",
    "register_flags",
]
