"""§37 Tier 2 #14 — Operation Modes substrate (PRD §32.7 Pattern B).

Operator-facing steering surface: 4-value closed taxonomy
controlling which tool dispatches are permitted in the current
session. Distinct from PostureOverride (which masks the
inferred directional disposition) and RiskTierFloor (which sets
auto-apply gates). Operation Modes gate the **mutation scope**
of the session — orthogonal to both.

**Closed taxonomy** (§33 closed-enum discipline):

  * ``PLAN``    — read-only exploration. Every tool in
    :data:`_MUTATION_TOOLS` (edit_file / write_file /
    delete_file / bash / apply_patch) is denied at dispatch.
    Reads + reporting + tests allowed.
  * ``ANALYZE`` — read-only deep-dive. Same enforcement as
    PLAN today; reserved for future "broader scope but no
    writes" semantics (e.g., commits + push denied while
    PLAN allows certain writes — calibrated post-graduation).
  * ``APPLY``   — status quo. Mutations allowed (subject to
    every other gate: risk tier, V2 permission, Antivenom).
  * ``AUTO``    — alias for APPLY in this slice. Reserved for
    future "fully autonomous" expansion (e.g., pre-approved
    risk tiers per session).

**Composition** (operator binding 2026-05-07):

  * Composes existing ``scoped_tool_access._MUTATION_TOOLS`` —
    no parallel set; AST-pinned.
  * Async-safe via ContextVar (mirrors §37 Tier 2 #13 Slice 2's
    capturer bridge + ``plan_exploit_active_var`` precedent).
  * REPL surface lives in sibling ``mode_repl.py`` (auto-
    discovered via §32.11 Slice 4 naming-cage convention).
  * Master flag default-FALSE per §33.1: when off, no
    mutation gating happens — byte-identical to pre-Slice
    behavior.

**Authority asymmetry** (AST-pinned): no orchestrator / iron_
gate / providers / urgency_router / change_engine / semantic_
guardian / candidate_generator imports.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import contextvars
import enum
import logging
import os
from typing import Any, Optional


logger = logging.getLogger("Ouroboros.OperationMode")


OPERATION_MODE_SCHEMA_VERSION: str = "operation_mode.1"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Closed taxonomy — 4-value enum
# ---------------------------------------------------------------------------


class OperationMode(str, enum.Enum):
    """Closed 4-value taxonomy. AST-pinned."""

    PLAN = "plan"
    """Read-only exploration. All :data:`_MUTATION_TOOLS`
    denied at dispatch. Reads + reporting + tests allowed."""

    ANALYZE = "analyze"
    """Read-only deep-dive. Same as PLAN in this slice;
    reserved for future broader scope semantics."""

    APPLY = "apply"
    """Status quo. Mutations subject to other gates only."""

    AUTO = "auto"
    """Alias for APPLY. Reserved for future fully-autonomous
    expansion."""


_DEFAULT_MODE: OperationMode = OperationMode.AUTO
"""Backward-compatible default: AUTO behaves byte-identically
to today's APPLY (no extra gating)."""


_MUTATION_BLOCKING_MODES: frozenset = frozenset(
    {OperationMode.PLAN, OperationMode.ANALYZE},
)
"""Modes that block mutation tools at dispatch. APPLY/AUTO
pass through. AST-pinned."""


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_OPERATION_MODE_ENABLED`` master switch. Default-
    FALSE per §33.1 — when off, ``is_mutation_blocked`` always
    returns False (byte-identical pre-slice behavior). Operator
    flips to true after observing the substrate via the REPL
    verb (``/mode help``)."""
    raw = os.environ.get(
        "JARVIS_OPERATION_MODE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Env-derived default mode
# ---------------------------------------------------------------------------


def resolve_mode_from_env() -> OperationMode:
    """Read ``JARVIS_OPERATION_MODE`` env knob and normalize to
    an :class:`OperationMode` value. Defaults to
    :data:`_DEFAULT_MODE` when unset, blank, or unrecognized.

    Pure function — NEVER raises."""
    raw = os.environ.get(
        "JARVIS_OPERATION_MODE", "",
    ).strip().lower()
    if not raw:
        return _DEFAULT_MODE
    for mode in OperationMode:
        if raw == mode.value:
            return mode
    # Unrecognized value — defensive fallback to default
    # rather than crash. Operator can correct via REPL verb
    # `/mode set <name>` or fix the env value.
    logger.debug(
        "[OperationMode] unrecognized "
        "JARVIS_OPERATION_MODE=%r — falling back to %s",
        raw, _DEFAULT_MODE.value,
    )
    return _DEFAULT_MODE


# ---------------------------------------------------------------------------
# Async-safe session state via ContextVar
# ---------------------------------------------------------------------------


_ACTIVE_MODE_VAR: "contextvars.ContextVar[Optional[OperationMode]]" = (
    contextvars.ContextVar(
        "operation_mode_active", default=None,
    )
)
"""Async-safe pointer to the operator's current mode. ``None``
means "fall back to env" (resolves via :func:`current_mode`).
Mirrors the §37 Tier 2 #13 Slice 2 capturer-bridge pattern —
inherits across asyncio.Task creation; per-op tasks see the
session-level mode without explicit threading."""


def current_mode() -> OperationMode:
    """Return the currently-active mode for this async context.
    Resolution order:

      1. Operator-set value via :func:`set_mode` (ContextVar)
      2. Env knob ``JARVIS_OPERATION_MODE``
      3. :data:`_DEFAULT_MODE` (AUTO)

    NEVER raises."""
    try:
        active = _ACTIVE_MODE_VAR.get()
    except LookupError:
        active = None
    if active is not None:
        return active
    return resolve_mode_from_env()


def set_mode(
    mode: OperationMode,
) -> "contextvars.Token[Optional[OperationMode]]":
    """Stamp the operator-selected mode. Returns a Token the
    caller MAY pass to :func:`reset_mode` to restore the
    previous value (or simply let task exit clean it up).
    NEVER raises."""
    return _ACTIVE_MODE_VAR.set(mode)


def reset_mode(
    token: "contextvars.Token[Optional[OperationMode]]",
) -> None:
    """Restore the prior mode pointer using the Token returned
    by :func:`set_mode`. Defensive: invalid Token errors are
    swallowed."""
    try:
        _ACTIVE_MODE_VAR.reset(token)
    except (ValueError, LookupError, TypeError):
        logger.debug(
            "[OperationMode] reset_mode received stale/"
            "invalid token (non-fatal)",
        )


def reset_active_mode_for_tests() -> None:
    """Test-only — production code never calls. Pinned via
    naming convention (``_for_tests`` suffix)."""
    try:
        _ACTIVE_MODE_VAR.set(None)
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Mutation-block predicate — load-bearing enforcement primitive
# ---------------------------------------------------------------------------


def is_mutation_blocked(tool_name: str) -> bool:
    """Return True iff tool dispatch should be denied under
    the current mode + master-flag combination.

    Composes the canonical
    ``scoped_tool_access._MUTATION_TOOLS`` set — single source
    of truth for "what counts as a mutation tool." AST-pinned
    via ``operation_mode_composes_canonical_mutation_set``.

    Returns False when:
      * Master flag off (byte-identical pre-slice behavior).
      * Current mode is APPLY or AUTO (mutations pass through;
        Antivenom + risk-tier gates remain authoritative).
      * Tool name is empty / unknown.
      * Tool is not in the canonical mutation set
        (read-only tools always pass).

    NEVER raises.
    """
    if not tool_name:
        return False
    try:
        if not master_enabled():
            return False
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        mode = current_mode()
    except Exception:  # noqa: BLE001 — defensive
        return False
    if mode not in _MUTATION_BLOCKING_MODES:
        return False
    try:
        from backend.core.ouroboros.governance.scoped_tool_access import (
            _MUTATION_TOOLS,
        )
    except ImportError:
        return False
    try:
        return tool_name in _MUTATION_TOOLS
    except Exception:  # noqa: BLE001 — defensive
        return False


def block_reason(tool_name: str) -> str:
    """Human-readable explanation when :func:`is_mutation_blocked`
    returns True. Used by the tool_executor wiring to surface
    a meaningful POLICY_DENIED reason. Returns empty string
    when not blocked."""
    if not is_mutation_blocked(tool_name):
        return ""
    try:
        mode = current_mode()
    except Exception:  # noqa: BLE001 — defensive
        return "operation_mode_blocked"
    return (
        f"OPERATION_MODE={mode.value} blocks mutation tool "
        f"{tool_name!r} (use /mode apply or /mode auto to "
        f"allow mutations)"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered by FlagRegistry. Seeds the 2 knobs this
    module reads. Defensive on registry-shape mismatch."""
    try:
        registry.register(
            name="JARVIS_OPERATION_MODE_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §37 Tier 2 #14 Operation "
                "Modes. Default-FALSE per §33.1; when off, "
                "is_mutation_blocked always returns False "
                "(byte-identical pre-slice behavior)."
            ),
            category="Governance",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "operation_mode.py"
            ),
            example="JARVIS_OPERATION_MODE_ENABLED=true",
        )
        registry.register(
            name="JARVIS_OPERATION_MODE",
            type_="enum",
            default="auto",
            description=(
                "Boot default for the active Operation Mode. "
                "One of: plan / analyze / apply / auto. "
                "Operator can override per-session via "
                "/mode set <name>."
            ),
            category="Governance",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "operation_mode.py"
            ),
            example="JARVIS_OPERATION_MODE=plan",
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[OperationMode] FlagRegistry seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``operation_mode_taxonomy_4_values_closed`` — closed
         enum bytes-pinned (PLAN/ANALYZE/APPLY/AUTO).
      2. ``operation_mode_master_flag_default_false`` — §33.1
         producer flag stays default-FALSE.
      3. ``operation_mode_authority_asymmetry`` — substrate
         purity (no orchestrator / iron_gate / providers /
         change_engine / semantic_guardian / candidate_generator
         / urgency_router imports outside the lazy-import
         compose-points).
      4. ``operation_mode_composes_canonical_mutation_set`` —
         single source of truth for mutation classification:
         is_mutation_blocked MUST lazy-import
         scoped_tool_access._MUTATION_TOOLS (no parallel set).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/operation_mode.py"
    )

    def _validate_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"PLAN", "ANALYZE", "APPLY", "AUTO"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "OperationMode"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                extra = seen - required
                missing = required - seen
                if extra:
                    violations.append(
                        f"OperationMode has extra values "
                        f"{sorted(extra)} — taxonomy is closed"
                    )
                if missing:
                    violations.append(
                        f"OperationMode missing required "
                        f"values {sorted(missing)}"
                    )
                return tuple(violations)
        violations.append("OperationMode class missing")
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        master_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    master_func = node
                    break
        if master_func is None:
            violations.append("master_enabled() helper missing")
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in ast.walk(master_func):
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
                            f"operation_mode.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_mutation_set(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``is_mutation_blocked`` MUST compose the canonical
        ``scoped_tool_access._MUTATION_TOOLS`` set — no parallel
        mutation classification."""
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "is_mutation_blocked":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "is_mutation_blocked() missing"
            )
            return tuple(violations)
        composes_canonical = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "scoped_tool_access" in module:
                    if any(
                        n.name == "_MUTATION_TOOLS"
                        for n in sub.names
                    ):
                        composes_canonical = True
        if not composes_canonical:
            violations.append(
                "is_mutation_blocked MUST compose "
                "scoped_tool_access._MUTATION_TOOLS — no "
                "parallel mutation set"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "operation_mode_taxonomy_4_values_closed"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #14 — OperationMode is 4-value "
                "closed enum (PLAN/ANALYZE/APPLY/AUTO)."
            ),
            validate=_validate_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operation_mode_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #14 — §33.1 producer flag stays "
                "default-FALSE (byte-identical pre-slice "
                "behavior)."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operation_mode_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #14 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operation_mode_composes_canonical_mutation_set"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #14 — is_mutation_blocked composes "
                "scoped_tool_access._MUTATION_TOOLS canonical "
                "set; no parallel classification."
            ),
            validate=_validate_composes_mutation_set,
        ),
    ]


__all__ = [
    "OPERATION_MODE_SCHEMA_VERSION",
    "OperationMode",
    "block_reason",
    "current_mode",
    "is_mutation_blocked",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "reset_active_mode_for_tests",
    "reset_mode",
    "resolve_mode_from_env",
    "set_mode",
]
