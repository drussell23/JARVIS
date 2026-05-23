"""SWE-Bench-Pro envelope builder — Phase 2 Phase B.2.1
(PRD §40.7.9 / §40.7.10-b21).

Pure-data composition layer: takes a ``ProblemSpec`` + ``PreparedProblem``
(both produced upstream by Phases A + B.1) and produces an
``IntentEnvelope`` ready for ``IntakeLayerService.ingest_envelope``.

Composition discipline
----------------------

  * **Composes existing canonical surfaces only**:
      - ``make_envelope`` (intake) — single source of truth for envelope
        construction; this module NEVER reaches IntentEnvelope's
        constructor directly.
      - ``EVIDENCE_REPO_ROOT_KEY`` (operation_advisor / B.2.0) — the
        canonical evidence key the B.2.0 worktree-aware advisor
        consumes. This module imports the constant rather than
        hardcoding the string "repo_root" so drift across the
        producer/consumer pair is structurally impossible.
      - ``_VALID_SOURCES`` (intent_envelope) — already extended in
        the same commit to include "swe_bench_pro". An AST pin in
        the B.2.1 spine asserts ``ENVELOPE_SOURCE`` is a member of
        ``_VALID_SOURCES`` so renames stay in sync.

  * **No master-flag gate inside the builder**: this layer is pure
    data composition with no side effects. The master flag
    (``swe_bench_pro_enabled()``) gates the side-effect-producing
    surfaces — the B.2.2 evaluator façade checks it before invoking
    ``ingest_envelope``. Separating responsibility this way keeps
    the builder unit-testable without env juggling and prevents the
    "flag drift across multiple layers" anti-pattern.

  * **Source-agnostic by design** (mirrors B.2.0's hardening note 4):
    every envelope from this builder carries ``source="swe_bench_pro"``.
    Downstream consumers MUST NOT branch on this source value to
    achieve correctness — they branch on observable envelope/context
    fields (target_files, evidence.repo_root, urgency, etc.). The
    source token exists for observability + dedup + WAL replay only.

  * **Honest urgency derivation**: ``_derive_urgency()`` is a
    deterministic helper backed by an env override
    (``JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY``, default "normal" →
    routes STANDARD via UrgencyRouter). Trace-2 (soak
    bt-2026-05-17-225244): the prior "low" default gave the injected
    op the lowest priority-queue rank with deadline=inf, so it was
    structurally starved by the background-sensor flood and never
    dequeued. "normal" earns a finite deadline + starvation-guard
    protection. Operators wanting the old DW-only bulk economics can
    set the env to "low" explicitly (accepting the starvation risk for
    non-interactive bulk runs); "high"/"critical" → IMMEDIATE.

§7 fail-closed contract
-----------------------

Every public surface NEVER raises (``asyncio.CancelledError`` is the
sole exception that would propagate; this is a sync function so that's
moot in practice). Malformed inputs produce a degraded but valid
envelope rather than a partial construction that would fail downstream
validation.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
    _VALID_URGENCIES,
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.operation_advisor import (
    EVIDENCE_REPO_ROOT_KEY,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    PreparedProblem,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.EnvelopeBuilder")


# ===========================================================================
# Constants — single source of truth (AST-pinned subset of _VALID_SOURCES)
# ===========================================================================


# Canonical envelope source token for B.2.1 evaluator envelopes. AST
# pin in the B.2.1 spine asserts this constant is a member of
# :data:`_VALID_SOURCES`; drift would be caught at test time, not at
# the constructor's runtime ``EnvelopeValidationError``.
ENVELOPE_SOURCE: str = "swe_bench_pro"


ENVELOPE_URGENCY_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY"


# Per PRD §5 Urgency-Aware Provider Routing:
#   * critical / high → IMMEDIATE (Claude direct, $0.03/op)
#   * normal          → STANDARD (DW primary → Claude fallback)
#   * low             → BACKGROUND (DW-only, $0.002/op)
#
# Trace-2 fix (soak bt-2026-05-17-225244): "low" gives the injected
# benchmark op the LOWEST intake_priority_queue rank (urgency_rank=3)
# with deadline=inf — it NEVER force-dequeues and is structurally
# starved by higher-urgency background-sensor ops (django saw 0 BG
# submissions while 46 sensor ops flooded the dispatch loop). The
# queue's own starvation guard protects urgency>=normal *from* low
# floods — the opposite of what a benchmark op needs. So the default
# is "normal": a finite per-urgency deadline + starvation-guard
# protection guarantee the op actually dequeues. Operators who want
# the old DW-only bulk-cost economics can still set
# JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY=low explicitly (accepting the
# starvation risk for non-interactive bulk runs).
_DEFAULT_URGENCY: str = "normal"


# ===========================================================================
# Urgency derivation — env override with deterministic fallback
# ===========================================================================


def _derive_urgency() -> str:
    """Return a deterministic envelope urgency value.

    Reads the ``JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY`` env override.
    When unset or invalid, returns ``_DEFAULT_URGENCY`` ("normal" —
    Trace-2 anti-starvation default; see the constant's rationale).
    NEVER raises.

    The env value is normalized to lowercase + stripped. Invalid
    values produce a WARN log and fall back to the default rather
    than failing the build — keeps benchmark runs robust to operator
    typos.
    """
    raw = os.environ.get(ENVELOPE_URGENCY_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_URGENCY
    if raw not in _VALID_URGENCIES:
        logger.warning(
            "[SWEBenchPro] %s=%r invalid (allowed: %s); using default %r",
            ENVELOPE_URGENCY_ENV_VAR, raw,
            sorted(_VALID_URGENCIES), _DEFAULT_URGENCY,
        )
        return _DEFAULT_URGENCY
    return raw


# ===========================================================================
# Public API — build_evaluation_envelope
# ===========================================================================


def _safe_str(value: Any, *, default: str = "") -> str:
    """Coerce ``value`` to a non-None string. NEVER raises."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return default


def _build_evidence(
    problem: ProblemSpec,
    prepared: PreparedProblem,
) -> Dict[str, Any]:
    """Assemble the evidence dict the envelope carries downstream.

    The composition is the load-bearing piece of B.2.1: this is where
    the B.2.0 worktree-aware advisor learns it must scan the cloned
    worktree (via ``EVIDENCE_REPO_ROOT_KEY``), and where future
    Phase C scorer learns which problem produced which captured
    patch (via ``problem_instance_id``).

    Keys (closed set; documented for B.2.2 evaluator + Phase C scorer):
      * ``EVIDENCE_REPO_ROOT_KEY`` → str(prepared.worktree_path)
      * ``problem_instance_id``   → ProblemSpec.instance_id
      * ``base_commit``            → ProblemSpec.base_commit
      * ``branch_name``            → PreparedProblem.branch_name
      * ``repo_url``               → ProblemSpec.repo_url
      * ``signature``              → ProblemSpec.instance_id (drives
                                     ``_dedup_key`` so the same problem
                                     reaching intake twice within the
                                     idempotency window is deduped at
                                     the router level)

    Slice 12P Phase 1 additions — metadata signals that let the
    orchestrator's Iron Gate distinguish wiring-validation
    fixtures from real benchmark problems WITHOUT hardcoding
    instance_ids. Composed by ``envelope_metadata.
    is_wiring_validation_envelope`` downstream.
      * ``swe_bench_pro``      → True (always — this is THE
                                  SWE-Bench-Pro builder, so any
                                  envelope it produces is by
                                  definition a SWE-Bench-Pro
                                  envelope)
      * ``gold_patch_empty``   → True iff problem.gold_patch is
                                  empty (no reference fix → the
                                  problem is structurally a no-op-
                                  passes fixture)
      * ``real_benchmark``     → False when problem.metadata
                                  explicitly declares
                                  real_benchmark=False (fixture
                                  signal); True otherwise (default
                                  assumption: real benchmark)
      * ``fixture_purpose``    → problem.metadata["purpose"] if
                                  present (operator-facing
                                  telemetry — propagates to
                                  summary.json attribution)
    """
    # Slice 12P — read fixture metadata defensively (legacy
    # ProblemSpecs may not carry the metadata dict at all).
    _meta = getattr(problem, "metadata", None) or {}
    _gold_patch = _safe_str(getattr(problem, "gold_patch", ""))
    _real_benchmark_flag = _meta.get("real_benchmark")
    # Default: assume real benchmark unless metadata explicitly
    # says otherwise. Only the literal False value flips the gate.
    _is_real_benchmark = _real_benchmark_flag is not False
    _fixture_purpose = _meta.get("purpose")
    if not isinstance(_fixture_purpose, str):
        _fixture_purpose = ""
    return {
        EVIDENCE_REPO_ROOT_KEY: str(prepared.worktree_path),
        "problem_instance_id": _safe_str(problem.instance_id),
        "base_commit": _safe_str(problem.base_commit),
        "branch_name": _safe_str(prepared.branch_name),
        "repo_url": _safe_str(problem.repo_url),
        "signature": _safe_str(problem.instance_id),
        # Slice 12P Phase 1 metadata signals
        "swe_bench_pro": True,
        "gold_patch_empty": (_gold_patch == ""),
        "real_benchmark": _is_real_benchmark,
        "fixture_purpose": _fixture_purpose,
    }


def build_evaluation_envelope(
    problem: ProblemSpec,
    prepared: PreparedProblem,
) -> IntentEnvelope:
    """Compose a SWE-Bench-Pro evaluation :class:`IntentEnvelope`.

    Parameters
    ----------
    problem:
        The :class:`ProblemSpec` produced by Phase A's dataset
        loader. Carries the problem statement, base commit, repo URL,
        and (separately) the failing-tests patch + gold patch (which
        the builder does NOT thread into the envelope — patches are
        worktree state, not envelope payload).
    prepared:
        The :class:`PreparedProblem` produced by Phase B.1's per-
        problem harness. Carries the worktree path, branch name, and
        the target_paths parsed from the test_patch's ``+++ b/<path>``
        headers.

    Returns
    -------
    IntentEnvelope
        A frozen envelope with ``source="swe_bench_pro"``, urgency
        derived deterministically, evidence dict assembled by
        :func:`_build_evidence`, and a fresh causal_id allocated by
        :func:`make_envelope`. The envelope is ready for the B.2.2
        evaluator façade to hand to
        ``IntakeLayerService.ingest_envelope``.

    Notes
    -----
    * The builder does NOT check the SWE-Bench-Pro master flag —
      it's pure data composition. The B.2.2 façade owns the master-
      flag gate before any side-effect-producing call.
    * The envelope's ``causal_id`` becomes the downstream
      ``OperationContext.op_id`` (see
      ``unified_intake_router.py:1159``). The B.2.2 façade reads
      this back AFTER construction to subscribe to the canonical
      ``operation_terminal`` SSE stream (B.2.0.5) filtered by
      ``op_id=envelope.causal_id``.
    * Every (problem × build) pair gets a fresh ``causal_id`` /
      ``idempotency_key``. Retries are distinct ops at the ledger
      level. The ``signature`` field (set to ``problem.instance_id``)
      drives router-side dedup so two near-simultaneous builds for
      the same problem don't both fire — only the first lands.
    """
    # Cognition-feed fix (soak bt-2026-05-17-194855: psf__requests-3362
    # terminated as a CLASSIFY no-op because this builder handed the
    # agent the test file as its target).
    # Authentic SWE-bench protocol: the agent must LOCALIZE the bug
    # from the issue text alone (exploration-first Iron Gate), NOT be
    # handed a target. ``prepared.target_paths`` are the *test_patch*
    # paths — surfacing them inverts the task (the agent is forbidden
    # to edit tests; Phase C scorer rejects test edits as cheating),
    # and surfacing gold_patch paths would leak the solution. So a
    # SWE-bench envelope carries NO target_files. This is honoured by
    # intent_envelope's ``_EMPTY_TARGET_FILES_EXEMPT_SOURCES`` (same
    # epistemic class as vision_sensor). Dedup still works: _dedup_key
    # composes evidence["signature"] (== problem.instance_id), so two
    # near-simultaneous builds for the same problem still collapse.
    # The test_patch remains worktree state for scoring only — never
    # the agent's stated target.
    target_files: Tuple[str, ...] = ()
    evidence = _build_evidence(problem, prepared)
    description = _safe_str(problem.problem_statement)
    repo = _safe_str(problem.repo) or _safe_str(problem.repo_url)
    urgency = _derive_urgency()
    return make_envelope(
        source=ENVELOPE_SOURCE,
        description=description,
        target_files=target_files,
        repo=repo,
        confidence=1.0,
        urgency=urgency,
        evidence=evidence,
        requires_human_ack=False,
    )


# ===========================================================================
# FlagRegistry self-registration
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
            name=ENVELOPE_URGENCY_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_URGENCY,
            description=(
                "Override the urgency stamped on SWE-Bench-Pro Phase "
                "B.2.1 evaluator envelopes. Allowed values: critical "
                "/ high / normal / low. Defaults to 'normal' (→ "
                "STANDARD) — Trace-2 anti-starvation: 'low' gave the "
                "injected op the lowest priority-queue rank with no "
                "deadline so it was starved by background-sensor ops "
                "and never dequeued (soak bt-2026-05-17-225244). Set "
                "'low' for old DW-only bulk economics (accepts "
                "starvation risk on non-interactive bulk runs); "
                "'high'/'critical' route IMMEDIATE (Claude direct). "
                "Invalid values log a WARN and fall back to default."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "envelope_builder.py"
            ),
            example=_DEFAULT_URGENCY,
            since="v3.7 Phase 2 Phase B.2.1 (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro] envelope_builder flag registration "
                "failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "ENVELOPE_SOURCE",
    "ENVELOPE_URGENCY_ENV_VAR",
    "build_evaluation_envelope",
    "register_flags",
]
