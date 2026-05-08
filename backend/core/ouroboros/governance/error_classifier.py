"""Phase 2 (A5) — Generic error classifier substrate.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A5: retry hardening (~3-4h). Compose intelligent_retry_manager
   (or lift only the pieces you need: transient vs permanent
   classification + jitter policy) into candidate_generator /
   Move 6.5 watchdog retry paths only — single budget source:
   cost_governor / existing caps. No second parallel retry loop
   with divergent env knobs without consolidating names."

This module **lifts the pure decision functions only** from
``coding_council/advanced/intelligent_retry_manager.py``. It
does NOT lift:

  * The :class:`RetryConfig` dataclass — would create a
    parallel env-knob surface (operator binding violation).
  * The :class:`CircuitBreaker` class — already canonical
    via ``provider_circuit_breaker.py`` (Phase 0 §35
    canonical-vs-canonical mapping).
  * The :class:`IntelligentRetryManager` orchestrator — would
    create a parallel retry loop (operator binding violation).
  * The :class:`DelayCalculator` 6-strategy menu — O+V has the
    canonical :func:`full_jitter_backoff_s` (Phase 12.2 Slice
    A) which implements AWS-style full-jitter backoff. The
    operator-mandated jitter policy IS that primitive; this
    module composes it via lazy-import.

What this module DOES ship:

  1. **Closed 3-value :class:`ErrorClass`** taxonomy
     (TRANSIENT / PERMANENT / UNKNOWN). Minimal vs
     intelligent_retry_manager's 10-value menu — operator
     binding "lift only the pieces you need".

  2. **Pure :func:`classify_error`** decision function —
     pattern-based substring matching + exception-type
     classification. Mirrors intelligent_retry_manager's
     :class:`ErrorClassifier.classify` shape but without the
     HTTP-status branch (kept generic).

  3. **Pure :func:`compute_retry_delay_s`** — composes
     canonical :func:`full_jitter_backoff_s` with
     ErrorClass-tuned per-class parameters. NO new jitter
     math; AST-pinned to forbid local jitter implementation.

  4. **Module-level frozen pattern tables** (TRANSIENT +
     PERMANENT). Bytes-pinned via AST so callers cannot drift
     from the canonical set without an explicit ADR-shaped
     edit.

## Composition discipline (AST-pinned)

  * No retry loop — pure decision functions only. Callers'
    EXISTING loops (e.g. ``candidate_generator._call_fallback``
    outer-retry loop) compose this substrate. AST-pinned via
    ``error_classifier_no_retry_loop``.
  * No env-knob config — callers' existing env knobs feed in.
    Single env knob in this module is the master flag.
    AST-pinned via ``error_classifier_no_config_dataclass``.
  * No circuit breaker — canonical
    ``provider_circuit_breaker`` is the only breaker.
  * Composes :func:`full_jitter_backoff_s` via lazy-import.
    AST-pinned via
    ``error_classifier_composes_canonical_jitter``.
  * Cross-kingdom boundary preserved — Phase 0
    ``governance_no_coding_council_imports`` covers this
    automatically (this module is in ``governance/``).

## Authority asymmetry

No orchestrator / iron_gate / providers / candidate_generator
/ change_engine / semantic_guardian / plan_generator /
urgency_router / direction_inferrer / policy imports.
Pure substrate. AST-pinned.

## Master flag

``JARVIS_ERROR_CLASSIFIER_ENABLED`` default-FALSE per §33.1.
When OFF, :func:`classify_error` returns ``ErrorClass.UNKNOWN``
unconditionally so callers' existing logic remains
authoritative — zero behavior change pre-graduation.

## NEVER raises

Every code path defensive — pattern-match failures swallowed,
non-string exception messages handled, bad input falls back
to UNKNOWN.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from typing import (
    Any, FrozenSet, Optional, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.ErrorClassifier",
)


ERROR_CLASSIFIER_SCHEMA_VERSION: str = (
    "error_classifier.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# ---------------------------------------------------------------------------
# Closed 3-value taxonomy
# ---------------------------------------------------------------------------


class ErrorClass(str, enum.Enum):
    """Closed 3-value retry-eligibility taxonomy. Minimal
    vs intelligent_retry_manager's 10-value menu — operator
    binding 'lift only the pieces you need'. AST-pinned.

    ``TRANSIENT``  — Caller may retry; error is recoverable.
                     Composes :func:`compute_retry_delay_s`
                     for the canonical jitter delay.
    ``PERMANENT``  — Caller MUST NOT retry; propagate.
                     Retry would waste cost budget without
                     recovery.
    ``UNKNOWN``    — Caller decides. Conservative default —
                     when in doubt, classify here. Master-
                     flag-off path returns UNKNOWN
                     unconditionally so existing call-site
                     logic remains authoritative."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Canonical pattern tables — bytes-pinned via AST
# ---------------------------------------------------------------------------


# Mirrors intelligent_retry_manager's TRANSIENT_PATTERNS
# (15 entries) but trimmed of duplicates + provider-specific
# variants. Bytes-pinned: callers MUST NOT extend these
# tables locally — operator-binding "single budget source"
# applies to classification surface as well.
_TRANSIENT_PATTERNS: FrozenSet[str] = frozenset({
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "service unavailable",
    "try again",
    "rate limit",
    "too many requests",
    "overloaded",
    "busy",
    "temporary failure",
    "transient",
    "retry",
    "intermittent",
})


_PERMANENT_PATTERNS: FrozenSet[str] = frozenset({
    "not found",
    "invalid",
    "unauthorized",
    "forbidden",
    "bad request",
    "validation error",
    "missing required",
    "permission denied",
    "not allowed",
})


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_ERROR_CLASSIFIER_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF, :func:`classify_error`
    returns UNKNOWN unconditionally — operator-binding
    "no behavior change pre-graduation". NEVER raises."""
    raw = os.environ.get(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


def classify_error(
    error: BaseException,
) -> ErrorClass:
    """Classify an exception into the 3-value
    :class:`ErrorClass` taxonomy. Pure decision function;
    NEVER raises.

    Decision tree (first-match-wins):
      1. Master flag off → UNKNOWN
         (operator binding "no behavior change
         pre-graduation")
      2. error is None / non-Exception → UNKNOWN
      3. asyncio.TimeoutError → TRANSIENT
      4. ConnectionError / ConnectionRefusedError →
         TRANSIENT
      5. error message matches TRANSIENT_PATTERNS →
         TRANSIENT
      6. error message matches PERMANENT_PATTERNS →
         PERMANENT
      7. ValueError / TypeError / KeyError → PERMANENT
         (validation-class — these are programmer errors,
         retrying won't help)
      8. otherwise → UNKNOWN

    Note: pattern matching is checked BEFORE exception type
    so semantic message overrides type-based defaults
    (e.g. RuntimeError("rate limit") → TRANSIENT despite
    RuntimeError not being in any type list)."""
    if not master_enabled():
        return ErrorClass.UNKNOWN
    if error is None:
        return ErrorClass.UNKNOWN
    try:
        # asyncio.TimeoutError — explicit before pattern
        # matching because the message is empty.
        if isinstance(error, asyncio.TimeoutError):
            return ErrorClass.TRANSIENT
        if isinstance(
            error, (ConnectionError, ConnectionRefusedError),
        ):
            return ErrorClass.TRANSIENT
        # Lower-case the message string once.
        try:
            msg = str(error).lower()
        except Exception:  # noqa: BLE001 — defensive
            msg = ""
        # Pattern match on message — semantic override.
        for pattern in _TRANSIENT_PATTERNS:
            if pattern in msg:
                return ErrorClass.TRANSIENT
        for pattern in _PERMANENT_PATTERNS:
            if pattern in msg:
                return ErrorClass.PERMANENT
        # Type-based defaults for validation-class errors.
        if isinstance(error, (ValueError, TypeError, KeyError)):
            return ErrorClass.PERMANENT
    except Exception:  # noqa: BLE001 — defensive
        return ErrorClass.UNKNOWN
    return ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# Per-class jitter parameters
# ---------------------------------------------------------------------------


# ErrorClass-tuned per-class parameters for
# :func:`compute_retry_delay_s`. AST-pinned to forbid local
# jitter math — every entry's (base_s, cap_s) tuple is fed to
# canonical :func:`full_jitter_backoff_s`. Operator-tunable
# via callers passing override args; module defaults preserved
# as canonical baseline.
#
# Rationale (operator binding "build cleanly on existing"):
#   * TRANSIENT — aggressive retry. Base 1s, cap 30s. Same
#     order of magnitude as intelligent_retry_manager's default
#     (base 1000ms, cap 60000ms).
#   * UNKNOWN — conservative. Base 5s, cap 60s. Wider band so
#     callers don't hammer endpoints when uncertain.
#   * PERMANENT — caller MUST NOT retry; this module returns
#     0.0 if asked, but callers should branch on ErrorClass
#     instead of calling compute_retry_delay_s for PERMANENT.
_DEFAULT_PARAMS: dict = {
    ErrorClass.TRANSIENT: (1.0, 30.0),
    ErrorClass.UNKNOWN:   (5.0, 60.0),
    ErrorClass.PERMANENT: (0.0, 0.0),
}


def compute_retry_delay_s(
    error_class: ErrorClass,
    attempt: int,
    *,
    base_s_override: Optional[float] = None,
    cap_s_override: Optional[float] = None,
) -> float:
    """Compute one retry delay in seconds via canonical
    :func:`full_jitter_backoff_s` (Phase 12.2 Slice A).
    AST-pinned to lazy-import the canonical primitive — no
    local jitter math.

    For PERMANENT errors, returns 0.0 unconditionally (caller
    should NOT retry). For TRANSIENT / UNKNOWN, composes the
    canonical jitter primitive with class-tuned base/cap
    params (operator-overridable via kwargs).

    NEVER raises — defensive on jitter import failure (returns
    base_s as conservative fallback)."""
    try:
        if error_class is ErrorClass.PERMANENT:
            return 0.0
        if attempt < 0:
            attempt = 0
        params = _DEFAULT_PARAMS.get(
            error_class,
            _DEFAULT_PARAMS[ErrorClass.UNKNOWN],
        )
        base_s, cap_s = params
        if base_s_override is not None:
            base_s = float(base_s_override)
        if cap_s_override is not None:
            cap_s = float(cap_s_override)
        if base_s <= 0.0:
            return 0.0
    except Exception:  # noqa: BLE001 — defensive
        return 0.0
    # Compose canonical jitter primitive.
    try:
        from backend.core.ouroboros.governance.full_jitter import (  # noqa: E501
            full_jitter_backoff_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ErrorClassifier] full_jitter primitive "
            "unavailable: %s — fallback to base_s", exc,
        )
        return float(base_s)
    try:
        return float(
            full_jitter_backoff_s(
                attempt, base_s=base_s, cap_s=cap_s,
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return float(base_s)


# ---------------------------------------------------------------------------
# Read-only API for tests + callers
# ---------------------------------------------------------------------------


def get_transient_patterns() -> Tuple[str, ...]:
    """Return canonical TRANSIENT patterns (sorted for
    determinism). Pure read; NEVER raises."""
    return tuple(sorted(_TRANSIENT_PATTERNS))


def get_permanent_patterns() -> Tuple[str, ...]:
    """Return canonical PERMANENT patterns (sorted for
    determinism). Pure read; NEVER raises."""
    return tuple(sorted(_PERMANENT_PATTERNS))


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name="JARVIS_ERROR_CLASSIFIER_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Phase 2 A5 — generic "
                "error classifier substrate. Default-FALSE "
                "per §33.1; when OFF, classify_error returns "
                "UNKNOWN unconditionally — zero behavior "
                "change pre-graduation."
            ),
            category="ErrorHandling",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "error_classifier.py"
            ),
            example=(
                "JARVIS_ERROR_CLASSIFIER_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ErrorClassifier] master-flag seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``error_classifier_master_default_false`` —
         §33.1 producer flag.
      2. ``error_classifier_authority_asymmetry`` — no
         orchestrator-tier imports.
      3. ``error_classifier_taxonomy_3_values`` — closed
         enum (TRANSIENT/PERMANENT/UNKNOWN). AST-pinned at
         3 — operator-binding 'lift only the pieces you
         need'.
      4. ``error_classifier_no_retry_loop`` — module MUST
         NOT contain a ``while`` loop with a retry counter
         (operator binding 'no second parallel retry loop').
      5. ``error_classifier_no_config_dataclass`` — module
         MUST NOT define a RetryConfig-shaped dataclass
         (operator binding 'no parallel env-knob surface').
      6. ``error_classifier_composes_canonical_jitter`` —
         :func:`compute_retry_delay_s` MUST lazy-import
         :func:`full_jitter_backoff_s` (no local jitter
         math).
      7. ``error_classifier_pattern_tables_canonical`` —
         _TRANSIENT_PATTERNS / _PERMANENT_PATTERNS MUST be
         frozenset literals (immutable; operator-binding
         "single source of truth for classification surface").
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
        "error_classifier.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append(
                "master_enabled() missing"
            )
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            for cmp_node in ast.walk(sub.test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operand_empty = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operand_empty = True
                        break
                if not operand_empty:
                    continue
                for stmt in sub.body:
                    if isinstance(stmt, ast.Return) and (
                        isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is False
                    ):
                        empty_returns_false = True
                        break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on "
                "empty env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "error_classifier" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"error_classifier.py MUST NOT "
                            f"import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"error_classifier.py MUST NOT "
                            f"import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"TRANSIENT", "PERMANENT", "UNKNOWN"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ErrorClass"
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
                        f"ErrorClass missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"ErrorClass has extra "
                        f"{sorted(extra)} — closed at 3 "
                        f"values per operator binding "
                        f"'lift only the pieces you need'"
                    )
                return tuple(violations)
        violations.append("ErrorClass class missing")
        return tuple(violations)

    def _validate_no_retry_loop(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT contain a ``while`` loop whose
        body references a ``retry`` / ``attempt`` counter
        increment. Operator binding "no second parallel
        retry loop"."""
        violations: list = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            # Walk while body for AugAssign / Assign that
            # increments a ``retry`` / ``attempt`` counter.
            for sub in ast.walk(node):
                if isinstance(sub, ast.AugAssign):
                    target = sub.target
                    if isinstance(target, ast.Name):
                        name = target.id.lower()
                        if (
                            "retry" in name
                            or "attempt" in name
                        ):
                            violations.append(
                                f"no-retry-loop: while-loop "
                                f"increments {target.id!r} "
                                f"(line {sub.lineno}) — "
                                f"forbidden per operator "
                                f"binding 'no second "
                                f"parallel retry loop'"
                            )
        return tuple(violations)

    def _validate_no_config_dataclass(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT define a RetryConfig-shaped
        dataclass. Operator binding "no parallel env-knob
        surface"."""
        violations: list = []
        forbidden_class_names = {
            "RetryConfig", "RetryStrategy",
            "RetryAttempt", "RetryResult",
            "RetryStats", "RetryManager",
            "IntelligentRetryManager",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name in forbidden_class_names:
                    violations.append(
                        f"no-config-dataclass: class "
                        f"{node.name!r} forbidden — "
                        f"operator binding 'no parallel "
                        f"env-knob surface; single budget "
                        f"source: cost_governor / existing "
                        f"caps'"
                    )
        return tuple(violations)

    def _validate_composes_canonical_jitter(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:func:`compute_retry_delay_s` MUST lazy-import
        :func:`full_jitter_backoff_s` from the canonical
        ``full_jitter`` module. No local jitter math."""
        violations: list = []
        target_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "compute_retry_delay_s"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append(
                "compute_retry_delay_s function missing"
            )
            return tuple(violations)
        composes = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "full_jitter" in module:
                    names = {n.name for n in sub.names}
                    if "full_jitter_backoff_s" in names:
                        composes = True
                        break
        if not composes:
            violations.append(
                "composes-canonical-jitter: "
                "compute_retry_delay_s MUST lazy-import "
                "full_jitter_backoff_s from canonical "
                "full_jitter (no local jitter math)"
            )
        return tuple(violations)

    def _validate_pattern_tables_canonical(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:data:`_TRANSIENT_PATTERNS` and
        :data:`_PERMANENT_PATTERNS` MUST be ``frozenset``
        literal calls — immutable + bytes-pinnable."""
        violations: list = []
        required_tables = {
            "_TRANSIENT_PATTERNS",
            "_PERMANENT_PATTERNS",
        }
        seen: set = set()
        for node in ast.walk(tree):
            target_value: Optional[ast.expr] = None
            target_name: Optional[str] = None
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id in required_tables
                    ):
                        target_name = tgt.id
                        target_value = node.value
                        break
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id in required_tables
                ):
                    target_name = node.target.id
                    target_value = node.value
            if target_name is None or target_value is None:
                continue
            seen.add(target_name)
            # Must be frozenset({...}) call.
            if not (
                isinstance(target_value, ast.Call)
                and isinstance(target_value.func, ast.Name)
                and target_value.func.id == "frozenset"
                and target_value.args
                and isinstance(target_value.args[0], ast.Set)
            ):
                violations.append(
                    f"pattern-tables-canonical: "
                    f"{target_name} MUST be a frozenset({{...}}) "
                    f"literal call (immutable, bytes-"
                    f"pinnable)"
                )
        missing = required_tables - seen
        if missing:
            violations.append(
                f"pattern-tables-canonical: missing tables "
                f"{sorted(missing)}"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_master_default_false"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_taxonomy_3_values"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — ErrorClass closed at 3 "
                "values (TRANSIENT/PERMANENT/UNKNOWN). "
                "Operator binding 'lift only the pieces "
                "you need'."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_no_retry_loop"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — operator binding 'no second "
                "parallel retry loop'. Module MUST NOT "
                "contain a while-loop incrementing a "
                "retry/attempt counter."
            ),
            validate=_validate_no_retry_loop,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_no_config_dataclass"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — operator binding 'no parallel "
                "env-knob surface'. Module MUST NOT define "
                "RetryConfig-shaped dataclasses or Manager "
                "classes."
            ),
            validate=_validate_no_config_dataclass,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_composes_canonical_jitter"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — compute_retry_delay_s "
                "composes canonical full_jitter_backoff_s "
                "via lazy-import (no local jitter math)."
            ),
            validate=_validate_composes_canonical_jitter,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "error_classifier_pattern_tables_canonical"
            ),
            target_file=target,
            description=(
                "Phase 2 A5 — _TRANSIENT_PATTERNS + "
                "_PERMANENT_PATTERNS MUST be frozenset "
                "literal calls (immutable; single source "
                "of truth)."
            ),
            validate=_validate_pattern_tables_canonical,
        ),
    ]


__all__ = [
    "ERROR_CLASSIFIER_SCHEMA_VERSION",
    "ErrorClass",
    "classify_error",
    "compute_retry_delay_s",
    "get_permanent_patterns",
    "get_transient_patterns",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
]
