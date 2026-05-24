"""Envelope metadata helpers — Slice 12P Phase 1.

Pure classifiers over the IntentEnvelope evidence JSON that the
orchestrator carries via ``op_context.intake_evidence_json``. NEVER
raise. NEVER hardcode instance_ids.

The load-bearing function is :func:`is_wiring_validation_envelope` —
recognises SWE-Bench-Pro fixture envelopes (those with an empty
``gold_patch``) so the Iron Gate exploration discipline can drop
its 2-call minimum to 0 for them. Real benchmark problems (those
with a non-empty ``gold_patch``) are unaffected — they SHOULD
explore, and the gate stays load-bearing for them.

Empirical context: bt-2026-05-23-030130 — the
``jarvis__harness-smoke-001`` fixture was rejected by the
Iron Gate's ``exploration_insufficient`` invariant because the
gate didn't know that a no-op patch is the structurally correct
answer for a fixture with ``gold_patch=""``. Slice 12P closes
that contradiction at the metadata layer, not by special-casing
the fixture's name.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.EnvelopeMetadata")


# Canonical evidence keys (closed set; mirror envelope_builder's
# emission contract). Centralised here so a refactor that renames
# one key catches both the producer (envelope_builder) and the
# consumer (this module + orchestrator) via AST pins.
EVIDENCE_KEY_SWE_BENCH_PRO: str = "swe_bench_pro"
EVIDENCE_KEY_GOLD_PATCH_EMPTY: str = "gold_patch_empty"
EVIDENCE_KEY_REAL_BENCHMARK: str = "real_benchmark"
EVIDENCE_KEY_FIXTURE_PURPOSE: str = "fixture_purpose"


def _parse_intake_evidence(ctx: Any) -> Dict[str, Any]:
    """Decode ``ctx.intake_evidence_json`` defensively. Empty /
    missing / malformed → empty dict. NEVER raises."""
    if ctx is None:
        return {}
    try:
        raw = getattr(ctx, "intake_evidence_json", "") or ""
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


def is_wiring_validation_envelope(ctx: Any) -> bool:
    """True iff the op's IntentEnvelope is a SWE-Bench-Pro
    wiring-validation fixture (not a real benchmark problem).

    Decision is the AND of three signals — all three must be
    present + correct to flip:

      1. ``swe_bench_pro == True`` — the envelope was emitted by
         the SWE-Bench-Pro builder (composes
         :data:`EVIDENCE_KEY_SWE_BENCH_PRO`).
      2. ``gold_patch_empty == True`` — the ProblemSpec carries
         an empty ``gold_patch``, which by SWE-Bench-Pro
         construction means the problem is a no-op-passes
         fixture (real benchmark problems always carry a
         non-empty gold patch — that's the reference fix).
      3. ``real_benchmark == False`` — defensive belt-and-
         suspenders: even if some future fixture somehow had an
         empty gold_patch but was supposed to be a real benchmark,
         this third signal preserves the gate.

    Returns False for:
      * Non-SWE-Bench-Pro envelopes (regular Ouroboros ops)
      * Real SWE-Bench-Pro benchmark problems (non-empty
        gold_patch)
      * Malformed / missing evidence JSON
      * Any case where one of the three signals is missing

    NEVER raises. The default-safe answer is False (preserve
    pre-Slice-12P gate enforcement).
    """
    evidence = _parse_intake_evidence(ctx)
    if not evidence:
        return False
    if evidence.get(EVIDENCE_KEY_SWE_BENCH_PRO) is not True:
        return False
    if evidence.get(EVIDENCE_KEY_GOLD_PATCH_EMPTY) is not True:
        return False
    if evidence.get(EVIDENCE_KEY_REAL_BENCHMARK) is not False:
        return False
    return True


def envelope_fixture_purpose(ctx: Any) -> Optional[str]:
    """Operator-facing telemetry: returns the fixture's declared
    purpose if present in evidence, else None. Useful for
    summary.json attribution. NEVER raises."""
    evidence = _parse_intake_evidence(ctx)
    if not evidence:
        return None
    purpose = evidence.get(EVIDENCE_KEY_FIXTURE_PURPOSE)
    if not isinstance(purpose, str) or not purpose:
        return None
    return purpose


def is_route_wiring_validation_envelope(ctx: Any) -> bool:
    """Slice 12AD — looser sibling of :func:`is_wiring_validation_envelope`
    used for **provider routing** (not for IronGate exploration floor).

    True iff the envelope carries operator's exact two-signal
    wiring-validation criteria:

      1. ``fixture_purpose == "wiring_validation"`` — the operator
         explicitly tagged this envelope's source ProblemSpec with
         ``metadata.purpose = "wiring_validation"``
      2. ``real_benchmark is False`` — defensive belt: real benchmarks
         MUST NEVER take the WIRING_VALIDATION route

    Why a second helper instead of reusing
    :func:`is_wiring_validation_envelope`?

      * The existing 3-signal helper requires ``swe_bench_pro==True``
        AND ``gold_patch_empty==True`` — SWE-Bench-Pro-specific
        structural signals appropriate for the IronGate exploration-
        floor override (which only matters when SWE-Bench-Pro fixtures
        exist).
      * This route-decision helper is the **operator-canonical** test
        for "the operator declared this fixture a wiring-validation
        fixture" — broader so any future non-SWE-Bench-Pro wiring-
        validation fixture (custom harness, internal test substrate)
        also qualifies for the budget-aware route.
      * Defense-in-depth: ``real_benchmark is False`` must be
        *exactly* False (not falsy) — missing key, ``None``, or
        ``"false"`` all read as default-true (assume real benchmark),
        preserving fail-closed posture for any malformed payload.

    Composition (Slice 12AD): consumed by
    :meth:`backend.core.ouroboros.governance.urgency_router
    .UrgencyRouter.classify` at Priority 0.6 (between the existing
    envelope_routing_override at 0.5 and the Priority 1 matrix) when
    ``JARVIS_WIRING_VALIDATION_ROUTE_ENABLED`` is on. Master flag is
    default-FALSE per §33.1 — this helper is a pure classifier and
    NEVER triggers routing without the explicit operator opt-in.

    Returns False for:
      * Non-fixture envelopes (no ``fixture_purpose`` set, or set to
        anything other than ``"wiring_validation"``)
      * Real benchmark envelopes (``real_benchmark`` missing, True,
        ``None``, or any non-False value)
      * Malformed / missing evidence JSON

    NEVER raises. Default-safe answer is False (no WIRING_VALIDATION
    routing → falls through to existing Priority 1-5 matrix).
    """
    evidence = _parse_intake_evidence(ctx)
    if not evidence:
        return False
    purpose = evidence.get(EVIDENCE_KEY_FIXTURE_PURPOSE)
    if purpose != "wiring_validation":
        return False
    # Defense-in-depth: ``is False`` not ``== False`` (excludes None,
    # 0, "", "false" — only the literal boolean False qualifies).
    if evidence.get(EVIDENCE_KEY_REAL_BENCHMARK) is not False:
        return False
    return True


def envelope_is_swe_bench_pro(ctx: Any) -> bool:
    """True iff the op's envelope was emitted by the SWE-Bench-
    Pro builder, regardless of fixture/real-benchmark status.
    Used by telemetry classifiers that need to know about the
    SWE substrate without needing the fixture distinction.
    NEVER raises."""
    evidence = _parse_intake_evidence(ctx)
    return evidence.get(EVIDENCE_KEY_SWE_BENCH_PRO) is True


__all__ = [
    "EVIDENCE_KEY_SWE_BENCH_PRO",
    "EVIDENCE_KEY_GOLD_PATCH_EMPTY",
    "EVIDENCE_KEY_REAL_BENCHMARK",
    "EVIDENCE_KEY_FIXTURE_PURPOSE",
    "envelope_fixture_purpose",
    "envelope_is_swe_bench_pro",
    "is_route_wiring_validation_envelope",
    "is_wiring_validation_envelope",
]
