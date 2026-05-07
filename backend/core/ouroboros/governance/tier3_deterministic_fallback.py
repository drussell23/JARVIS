"""§3.6.2 vector #12 — Tier 3 deterministic fallback closure.

When Tier 0 (DoubleWord) + Tier 1 (Claude) + Tier 2 (J-Prime
when available) ALL exhaust, the canonical exception path
raises ``RuntimeError("all_providers_exhausted")`` and the
organism freezes (CuriosityEngine idle-signal cannot fire,
the loop aborts, no recovery). This is the structural SPOF
identified in the brutal review §3.6.2 vector #12.

This module closes the freeze vector via a **deterministic
fallback** that is NOT a fourth model — it's a graceful-
degradation result substitute. When master flag on, the
candidate_generator's exhaustion handler intercepts the
exception and substitutes a structured empty
:class:`GenerationResult` instead of re-raising.

**Composition** (operator binding 2026-05-07):

  * Reuses canonical :class:`op_context.GenerationResult`
    shape — no parallel result type. Empty ``candidates``
    tuple signals "no real candidate"; orchestrator's
    existing empty-candidates handling routes the op
    through APPROVAL_REQUIRED gate (or completes the op
    as deferred).
  * NO model call. NO API spend. Tier 3 returns immediately
    with ``generation_duration_s=0.0`` + ``cost_usd=0.0``.
    The deterministic name is structurally accurate: same
    inputs → same output → no nondeterminism leaks into
    governance state.
  * NO claim of generated code. ``provider_name`` is the
    explicit ``"tier3_deterministic_fallback"`` so every
    downstream observer (postmortem, audit ledger, SerpentFlow
    rendering) can identify the deferral.

**This is NOT a fourth model** — it's the cage's
last-mile graceful-degradation. The structurally correct
"real fix" remains M12 (J-Prime LoRA as a real Tier 3
model); Tier 3 deterministic fallback is the band-aid that
keeps the organism alive while M12 infrastructure is
scoped + built.

**Master flag** ``JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED``
default-FALSE per §33.1: when off, ``should_intercept`` returns
False and the canonical exhaustion path remains unchanged
(byte-identical pre-slice behavior). Operator opts in once
empirical evidence shows the deferred-result path doesn't
mask real provider problems (Phase 9 cadence helps here —
soak runs without the master flag verify the cage's
authentic behavior; soak runs WITH the master flag verify
graceful degradation).

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / change_engine /
semantic_guardian / candidate_generator imports outside the
lazy-import bridge to ``op_context.GenerationResult``.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


logger = logging.getLogger(
    "Ouroboros.Tier3DeterministicFallback",
)


TIER3_DETERMINISTIC_FALLBACK_SCHEMA_VERSION: str = (
    "tier3_deterministic_fallback.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Closed taxonomy — 2-value outcome
# ---------------------------------------------------------------------------


class Tier3FallbackOutcome(str, enum.Enum):
    """Closed 2-value taxonomy for the fallback dispatch
    decision. AST-pinned."""

    SUBSTITUTED = "substituted"
    """Master flag on + exhaustion encountered → deterministic
    deferred result substituted; original RuntimeError
    suppressed."""

    DISABLED = "disabled"
    """Master flag off (or substrate unavailable) →
    canonical exhaustion path runs unchanged
    (byte-identical pre-slice behavior)."""


# ---------------------------------------------------------------------------
# Frozen artifact — Tier3FallbackReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tier3FallbackReport:
    """Structured outcome record. Frozen for safe propagation
    across observability surfaces. §33.5 versioned artifact."""

    outcome: Tier3FallbackOutcome
    op_id: str
    cause: str
    """The exhaustion cause string (e.g.,
    ``all_providers_exhausted:queue_only_dispatch``)."""

    schema_version: str = field(
        default=TIER3_DETERMINISTIC_FALLBACK_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "op_id": str(self.op_id),
            "cause": str(self.cause)[:256],
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED`` master
    switch. Default-FALSE per §33.1: when off, the canonical
    ``all_providers_exhausted`` exception path remains
    unchanged. Operator binding 2026-05-07: graceful-
    degradation Tier 3 is opt-in until empirical evidence
    (Phase 9 cadence) shows it doesn't mask real provider
    problems."""
    raw = os.environ.get(
        "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Should-intercept predicate
# ---------------------------------------------------------------------------


def should_intercept_exhaustion() -> bool:
    """Return True iff the candidate_generator's exhaustion
    handler should substitute a deterministic deferred result
    instead of re-raising. Equals ``master_enabled()`` today;
    factored as a separate predicate so future refinements
    (e.g., per-route or per-cause gating) compose cleanly.
    NEVER raises."""
    try:
        return master_enabled()
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Deterministic deferred GenerationResult builder
# ---------------------------------------------------------------------------


def build_deferred_generation_result(
    *,
    op_id: str = "",
    cause: str = "all_providers_exhausted",
) -> Any:
    """Build a structured deferred :class:`GenerationResult`
    that the orchestrator's empty-candidates handling can route
    through APPROVAL_REQUIRED (or operator-deferred completion)
    instead of crashing the op.

    The returned shape is the canonical ``GenerationResult``
    from ``op_context`` — NOT a parallel type. Lazy-imported
    here to keep this module's import surface clean.

    Field semantics (load-bearing for downstream observers):

      * ``candidates = ()`` — empty tuple signals "no real
        candidate." Orchestrator must NOT treat this as a
        successful generation.
      * ``provider_name = "tier3_deterministic_fallback"`` —
        explicit signal in audit/postmortem ledgers.
      * ``generation_duration_s = 0.0`` — zero wall-clock
        cost; the fallback is instant.
      * ``cost_usd = 0.0`` — zero financial cost; no model
        call was made.

    Returns ``None`` if the canonical GenerationResult is
    unavailable (rollback branch); caller falls back to the
    original raise path. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.op_context import (
            GenerationResult,
        )
    except ImportError:
        return None
    try:
        return GenerationResult(
            candidates=(),
            provider_name="tier3_deterministic_fallback",
            generation_duration_s=0.0,
            model_id="",
            is_noop=False,
            tool_execution_records=(),
            venom_edit_history=(),
            prompt_preloaded_files=(),
            total_input_tokens=0,
            total_output_tokens=0,
            cost_usd=0.0,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Tier3DeterministicFallback] GenerationResult "
            "construction failed (non-fatal)", exc_info=True,
        )
        return None


def emit_substitution_telemetry(
    *,
    op_id: str,
    cause: str,
) -> Tier3FallbackReport:
    """Emit a structured operator-visible log line + return
    a frozen :class:`Tier3FallbackReport`. The log line is
    distinct from the canonical exhaustion-report logger so
    observers can filter on ``[Tier3DeterministicFallback]``
    prefix to count fallback fires.

    NEVER raises."""
    op_safe = str(op_id or "")
    cause_safe = str(cause or "all_providers_exhausted")
    try:
        logger.warning(
            "[Tier3DeterministicFallback] all_providers_"
            "exhausted intercepted — substituting deterministic "
            "deferred GenerationResult op_id=%s cause=%s "
            "(operator-binding: graceful degradation prevents "
            "organism freeze; APPROVAL_REQUIRED routing "
            "expected downstream)",
            op_safe, cause_safe,
        )
    except Exception:  # noqa: BLE001 — defensive
        pass
    return Tier3FallbackReport(
        outcome=Tier3FallbackOutcome.SUBSTITUTED,
        op_id=op_safe,
        cause=cause_safe,
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name=(
                "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED"
            ),
            type_="bool",
            default="false",
            description=(
                "Master switch for §3.6.2 vector #12 Tier 3 "
                "deterministic fallback. Default-FALSE per "
                "§33.1; when off, the canonical "
                "all_providers_exhausted exception path runs "
                "unchanged (byte-identical pre-slice). When "
                "on, the candidate_generator's exhaustion "
                "handler substitutes a structured deferred "
                "GenerationResult instead of re-raising — "
                "prevents the organism freeze when both "
                "Tier 0 + Tier 1 are simultaneously out."
            ),
            category="Resilience",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tier3_deterministic_fallback.py"
            ),
            example=(
                "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED"
                "=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Tier3DeterministicFallback] FlagRegistry "
            "seeding failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tier3_fallback_outcome_taxonomy_2_values`` —
         closed 2-value enum (SUBSTITUTED / DISABLED).
      2. ``tier3_fallback_master_flag_default_false`` —
         §33.1 producer flag stays default-FALSE.
      3. ``tier3_fallback_authority_asymmetry`` — substrate
         purity (no orchestrator-tier imports outside the
         lazy-import bridge).
      4. ``tier3_fallback_composes_canonical_result`` —
         build_deferred_generation_result MUST lazy-import
         ``op_context.GenerationResult`` (no parallel result
         type).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "tier3_deterministic_fallback.py"
    )

    def _validate_outcome_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"SUBSTITUTED", "DISABLED"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "Tier3FallbackOutcome"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"Tier3FallbackOutcome missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"Tier3FallbackOutcome extra "
                        f"{sorted(extra)} — taxonomy is closed"
                    )
                return tuple(violations)
        violations.append("Tier3FallbackOutcome class missing")
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            compares: list = []
            for st in ast.walk(test):
                if isinstance(st, ast.Compare):
                    compares.append(st)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                # Allow self-references.
                if any(
                    "tier3_deterministic_fallback" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"tier3_deterministic_fallback.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"tier3_deterministic_fallback.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_canonical_result(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """build_deferred_generation_result MUST lazy-import
        op_context.GenerationResult — no parallel result
        type."""
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "build_deferred_generation_result":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "build_deferred_generation_result() missing"
            )
            return tuple(violations)
        composes_canonical = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "op_context" in module:
                    if any(
                        n.name == "GenerationResult"
                        for n in sub.names
                    ):
                        composes_canonical = True
        if not composes_canonical:
            violations.append(
                "build_deferred_generation_result MUST "
                "lazy-import GenerationResult from "
                "op_context — no parallel result type"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "tier3_fallback_outcome_taxonomy_2_values"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #12 — Tier3FallbackOutcome is "
                "2-value closed enum (SUBSTITUTED / DISABLED)."
            ),
            validate=_validate_outcome_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tier3_fallback_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #12 — §33.1 producer flag "
                "stays default-FALSE; byte-identical pre-"
                "slice behavior when off."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tier3_fallback_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #12 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tier3_fallback_composes_canonical_result"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #12 — build_deferred_"
                "generation_result composes "
                "op_context.GenerationResult; no parallel "
                "result type."
            ),
            validate=_validate_composes_canonical_result,
        ),
    ]


__all__ = [
    "TIER3_DETERMINISTIC_FALLBACK_SCHEMA_VERSION",
    "Tier3FallbackOutcome",
    "Tier3FallbackReport",
    "build_deferred_generation_result",
    "emit_substitution_telemetry",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "should_intercept_exhaustion",
]
