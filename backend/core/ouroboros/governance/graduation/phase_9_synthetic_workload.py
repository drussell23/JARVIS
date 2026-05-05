"""Phase 9 synthetic workload factory — closes the headless-cadence
zero-ops gap WITHOUT diluting the P9.2 graduation contract.

PRD §36.5 priority #1 / §3.6.2 vector #6 / 2026-05-05 first-soak
diagnosis: the Phase 9.2 contract correctly refuses to graduate
flags on absence of evidence (``predicate_requires_decision_trace_rows``
demands ``ops_count >= 1``). Production O+V fires ops because real
intent signals arrive (TestFailure, GitHub issues, voice commands,
etc.). Headless cadence soaks have no signal source → zero ops →
contract correctly refuses graduation forever.

This module builds **transparent synthetic IntentEnvelopes** that
flow through the canonical ``UnifiedIntakeRouter.ingest()`` pipeline
— the SAME ingestion surface and FSM as production — but carry an
honest ``source="cadence_synthetic"`` token + ``evidence.category =
"cadence_synthetic"`` marker so downstream observability + audits
NEVER confuse them with real production traffic.

Operator binding (verbatim, 2026-05-05):

  * **Single pipeline** — envelopes built with existing
    ``make_envelope`` + routed via ``UnifiedIntakeRouter.ingest()``
    only. No second router, no direct ledger writes.
  * **Observability / honesty** — every envelope tags ``source``
    AND ``evidence.category`` AND ``evidence.sensor`` as synthetic.
    Operators MUST be able to filter cadence load from real load.
  * **Defaults and safety** — caller passes ``n``; the harness CLI
    flag ``--seed-intents`` defaults to 0; only the cadence wrapper
    sets ``n >= 1``. Hard cap N at ``JARVIS_PHASE9_SEED_INTENTS_MAX``
    (default 16, clamped [1, 64]) so a misconfigured cron CANNOT
    spam ops or spend budget.
  * **Proof not vibes** — envelopes route through Iron Gate / risk
    tier ladder / SemanticGuardian like any other envelope. The
    contract still enforces ``ops_count >= 1`` from real FSM
    execution, not from the synthesis call returning successfully.
  * **Reuse before inventing** — composes ``make_envelope`` (single
    canonical builder); no parallel envelope construction path.

What this module is NOT:

  * NOT a workaround for the contract. The contract still applies
    fully; synthetic envelopes still route through Iron Gate and
    can fail just like production envelopes.
  * NOT a replacement for real production cadence. Operator-driven
    real-workload soaks (Option B from the 2026-05-05 review) are
    additive, not superseded.
  * NOT a bypass of cost discipline. ``urgency="low"`` routes via
    BACKGROUND ProviderRoute (DW-only cascade, never Claude); env
    cap on N protects against runaway misconfigure.

Architectural locks (AST-pinned at Slice 1):

  * Module composes ``make_envelope`` ONLY (no parallel
    ``IntentEnvelope(...)`` construction outside the canonical
    builder).
  * No imports of orchestrator / iron_gate / policy / providers /
    candidate_generator (substrate-purity authority asymmetry).
  * Hard cap on N (env-knob, clamped); exceeding it returns the
    capped tuple, never raises.
  * NEVER raises — caller-injected misconfigure returns empty tuple
    with one debug log line, never breaks the parent harness boot.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION: str = (
    "phase_9_synthetic_workload.1"
)


# Honest-source-token convention — must match the whitelist entry in
# ``intake/intent_envelope.py`` (added 2026-05-05). Pinned via AST
# regression (the test suite asserts both this constant and the
# whitelist entry agree, so they cannot drift).
CADENCE_SYNTHETIC_SOURCE: str = "cadence_synthetic"

# Evidence-category marker. Same string is used in
# ``evidence["category"]`` and in operator-facing observability
# filters. AST-pinned (test suite asserts factory output carries
# this value) so a future refactor cannot silently drop the
# transparency marker.
CADENCE_SYNTHETIC_CATEGORY: str = "cadence_synthetic"

# Sensor-identity marker. Distinct from ``ProactiveExplorationSensor``
# (which is a real sensor); identifies Phase 9 cadence-only synthesis.
CADENCE_SYNTHETIC_SENSOR_NAME: str = "Phase9SyntheticSeeder"


# ---------------------------------------------------------------------------
# Env knobs — all clamped, all default-conservative
# ---------------------------------------------------------------------------


def seed_intents_max() -> int:
    """``JARVIS_PHASE9_SEED_INTENTS_MAX`` — hard ceiling on the
    number of synthetic envelopes one harness invocation may
    inject. Default 16. Clamped [1, 64] so a misconfigured cron
    cannot spam ops or spend budget. The cadence wrapper passes
    a much smaller N (typically 3); this is a defense-in-depth
    cap, not the operating value."""
    raw = os.environ.get(
        "JARVIS_PHASE9_SEED_INTENTS_MAX", "",
    ).strip()
    try:
        n = int(raw) if raw else 16
        if n < 1:
            return 1
        if n > 64:
            return 64
        return n
    except (TypeError, ValueError):
        return 16


# ---------------------------------------------------------------------------
# Pure-function factory — single pipeline via make_envelope
# ---------------------------------------------------------------------------


def build_synthetic_envelopes(
    *,
    n: int,
    repo: str,
    project_root: Optional[Path] = None,
    seq_offset: int = 0,
) -> Tuple:
    """Build ``min(n, seed_intents_max())`` synthetic IntentEnvelopes
    for Phase 9 cadence injection.

    Each envelope is structurally distinct (unique ``signal_id`` /
    ``causal_id`` / ``idempotency_key``) but carries identical
    transparency markers so downstream filters can isolate them
    cleanly.

    Composes the canonical :func:`make_envelope` builder — no
    parallel ``IntentEnvelope(...)`` construction. AST-pinned.

    Returns an empty tuple (NEVER raises) when:

      * ``n <= 0`` — caller passed nothing to inject (production
        non-cadence path).
      * ``make_envelope`` import fails — substrate not present in
        rollback environment; harness boot must not break.
      * Any envelope-build raises — single bad envelope does not
        poison the rest; debug-log + skip.

    Args:
        n: requested number of envelopes (clamped to
           :func:`seed_intents_max`).
        repo: repo identifier for the envelope (matches harness
              context).
        project_root: optional; reserved for future per-repo
                      framing. Not used today.
        seq_offset: starting index for the per-envelope ``seq``
                    evidence field (audit-only; lets a caller that
                    invokes the factory multiple times keep the
                    seq monotonic).

    Returns:
        Tuple of ``IntentEnvelope`` instances; empty on misconfigure.
    """
    if n <= 0:
        return ()

    capped_n = min(int(n), seed_intents_max())

    try:
        from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501
            make_envelope,
        )
    except ImportError:
        logger.debug(
            "[Phase9SyntheticSeeder] make_envelope import failed "
            "— substrate unavailable, returning empty tuple",
            exc_info=True,
        )
        return ()

    out = []
    for idx in range(capped_n):
        seq = int(seq_offset) + idx
        try:
            envelope = make_envelope(
                source=CADENCE_SYNTHETIC_SOURCE,
                description=(
                    f"Phase 9 cadence synthetic exploration "
                    f"probe (seq={seq}). Transparent test "
                    f"workload — NOT a real production signal. "
                    f"Routes through canonical FSM to satisfy "
                    f"P9.2 graduation contract evidence "
                    f"requirement (ops_count >= 1) without "
                    f"diluting predicate semantics."
                ),
                # Project-root sentinel; the model uses its tool
                # loop (read_file / search_code) to discover
                # representative files. Matches the existing
                # cluster-coverage / curiosity-driven sentinel
                # convention in ProactiveExplorationSensor.
                target_files=(".",),
                repo=str(repo or ""),
                # Low confidence (0.50) — synthetic load should
                # NOT crowd out real signals on priority queue.
                confidence=0.50,
                # Low urgency — routes BACKGROUND via
                # UrgencyRouter (DW-only cascade, no Claude
                # fallback unless DW unavailable). Cost contract
                # preserved by composition; no special-case
                # routing for cadence_synthetic.
                urgency="low",
                evidence={
                    # Mandatory transparency markers — pinned by
                    # AST regression. Any drift here breaks the
                    # operator-facing filter.
                    "category": CADENCE_SYNTHETIC_CATEGORY,
                    "sensor": CADENCE_SYNTHETIC_SENSOR_NAME,
                    "phase_9_seq": seq,
                    "schema_version": (
                        PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION
                    ),
                    # Operator-audit hint: this envelope was
                    # synthesized in a cadence soak, NOT in
                    # response to a real signal.
                    "is_synthetic_cadence_load": True,
                },
                # Cadence runs unattended; never block on operator.
                requires_human_ack=False,
            )
            out.append(envelope)
        except Exception:  # noqa: BLE001 -- defensive
            # One bad envelope does not poison the rest.
            logger.debug(
                "[Phase9SyntheticSeeder] envelope build failed "
                "for seq=%d — skipping",
                seq,
                exc_info=True,
            )
            continue

    if out:
        logger.info(
            "[Phase9SyntheticSeeder] built n=%d synthetic "
            "envelopes (requested=%d, capped=%d, source=%s)",
            len(out), int(n), capped_n,
            CADENCE_SYNTHETIC_SOURCE,
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# AST pins (§32.11 Slice 2 / shipped_code_invariants auto-discovery)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``phase_9_synthetic_workload_authority_asymmetry`` —
         substrate purity (no orchestrator / iron_gate / policy /
         providers imports).
      2. ``phase_9_synthetic_workload_composes_make_envelope`` —
         factory MUST call ``make_envelope`` only; no parallel
         ``IntentEnvelope(...)`` construction.
      3. ``phase_9_synthetic_workload_source_token_constant`` —
         ``CADENCE_SYNTHETIC_SOURCE`` constant present + matches
         ``"cadence_synthetic"`` literal (matches whitelist entry).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graduation/"
        "phase_9_synthetic_workload.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"phase_9_synthetic_workload.py "
                            f"MUST NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_make_envelope(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Factory MUST call ``make_envelope`` only — no parallel
        ``IntentEnvelope(...)`` construction. Single-pipeline
        guardrail."""
        violations: list = []
        has_make_envelope_call = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "make_envelope"
                ):
                    has_make_envelope_call = True
                # Forbid direct ``IntentEnvelope(...)`` call.
                if (
                    isinstance(func, ast.Name)
                    and func.id == "IntentEnvelope"
                ):
                    violations.append(
                        "phase_9_synthetic_workload.py MUST "
                        "NOT call IntentEnvelope(...) directly "
                        "— compose make_envelope() (single-"
                        "pipeline guardrail)"
                    )
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "IntentEnvelope"
                ):
                    violations.append(
                        "phase_9_synthetic_workload.py MUST "
                        "NOT call IntentEnvelope(...) directly"
                    )
        if not has_make_envelope_call:
            violations.append(
                "phase_9_synthetic_workload.py MUST call "
                "make_envelope (single-pipeline guardrail "
                "— factory composes canonical builder)"
            )
        return tuple(violations)

    def _validate_source_token_constant(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``CADENCE_SYNTHETIC_SOURCE`` MUST be a module-level
        constant equal to literal ``"cadence_synthetic"``. AST-
        pinned because a drift between the factory's source token
        and the whitelist entry would silently break envelope
        validation."""
        violations: list = []
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id == "CADENCE_SYNTHETIC_SOURCE"
                ):
                    if (
                        isinstance(node.value, ast.Constant)
                        and node.value.value
                        == "cadence_synthetic"
                    ):
                        return ()
                    violations.append(
                        "CADENCE_SYNTHETIC_SOURCE must equal "
                        "literal 'cadence_synthetic' (matches "
                        "whitelist entry in intent_envelope.py)"
                    )
                    return tuple(violations)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "CADENCE_SYNTHETIC_SOURCE"
                    ):
                        if (
                            isinstance(node.value, ast.Constant)
                            and node.value.value
                            == "cadence_synthetic"
                        ):
                            return ()
                        violations.append(
                            "CADENCE_SYNTHETIC_SOURCE must "
                            "equal literal 'cadence_synthetic'"
                        )
                        return tuple(violations)
        violations.append(
            "CADENCE_SYNTHETIC_SOURCE module-level constant "
            "missing"
        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "phase_9_synthetic_workload_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 9 Slice 1 — substrate purity: factory "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase_9_synthetic_workload_composes_make_envelope"
            ),
            target_file=target,
            description=(
                "Phase 9 Slice 1 — single-pipeline guardrail: "
                "factory composes canonical make_envelope; no "
                "parallel IntentEnvelope(...) construction."
            ),
            validate=_validate_composes_make_envelope,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase_9_synthetic_workload_source_token_constant"
            ),
            target_file=target,
            description=(
                "Phase 9 Slice 1 — honest source token: "
                "CADENCE_SYNTHETIC_SOURCE MUST equal literal "
                "'cadence_synthetic' (matches whitelist entry "
                "in intent_envelope.py)."
            ),
            validate=_validate_source_token_constant,
        ),
    ]


__all__ = [
    "CADENCE_SYNTHETIC_CATEGORY",
    "CADENCE_SYNTHETIC_SENSOR_NAME",
    "CADENCE_SYNTHETIC_SOURCE",
    "PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION",
    "build_synthetic_envelopes",
    "register_shipped_invariants",
    "seed_intents_max",
]
