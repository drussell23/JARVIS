"""§37 Tier 2 #16 Slice 1 — Per-component tool scope (Pattern C
from PRD §32.7.3).

Closes the structural gap identified in the 2026-05-07 brutal
review: each "component" (sensor / subagent kind / explicit
operator-tagged op) can register a tighter tool allowlist than
the global default. When the component fires an op,
:func:`is_tool_allowed` denies tools outside its scope BEFORE
the V2 permission registry evaluates — defense-in-depth via
strictest-first composition (session-wide OperationMode first,
then per-component scope, then operator callbacks via V2).

**Composition** (operator binding 2026-05-07):

  * Composes Venom V4's ``tool_name_pattern`` matcher — no
    parallel regex math; AST-pinned. Component allowlists
    accept regex patterns (e.g., ``"mcp_github_*"``,
    ``"read_*"``) so operators can scope by tool family.
  * Composes Venom V2 PermissionRegistry implicitly via
    fire-order: component scope is the structural gate;
    V2 evaluates operator callbacks. Component-level DENY
    short-circuits V2 (PRD §32.7.3).
  * ContextVar bridge for async-safe component identity
    propagation (mirrors §37 Tier 2 #13 Slice 2's capturer
    bridge + ``plan_exploit_active_var`` precedent).
  * Mirrors :func:`risk_tier_floor.recommended_floor`
    ``signal_source`` per-source pattern at the tool-scope
    layer (per-source floor → per-component allowlist).

**Closed semantics** (§33 closed-taxonomy discipline):

  * ``ComponentScopeDecision`` is a 4-value closed enum
    (ALLOW / DENY / NO_SCOPE / DISABLED). NO_SCOPE means no
    scope was registered for this component — caller treats
    as ALLOW (no restriction). DISABLED means master flag
    is off.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / change_engine /
semantic_guardian / candidate_generator imports.

**Master flag** ``JARVIS_COMPONENT_TOOL_SCOPE_ENABLED`` default-
FALSE per §33.1: when off, every check returns DISABLED →
caller treats as ALLOW (byte-identical pre-slice behavior).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import contextvars
import enum
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional


logger = logging.getLogger("Ouroboros.ComponentToolScope")


COMPONENT_TOOL_SCOPE_SCHEMA_VERSION: str = (
    "component_tool_scope.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Closed taxonomy — 4-value decision enum
# ---------------------------------------------------------------------------


class ComponentScopeDecision(str, enum.Enum):
    """Closed 4-value taxonomy for component-scope evaluation
    outcomes. AST-pinned."""

    ALLOW = "allow"
    """Tool matches the component's allowed_tools set (or no
    denied_tools set restricts it). Caller proceeds to V2
    permission evaluation."""

    DENY = "deny"
    """Tool is explicitly in denied_tools OR not in
    allowed_tools (when allowlist is non-empty). Caller
    short-circuits dispatch with POLICY_DENIED."""

    NO_SCOPE = "no_scope"
    """No scope registered for this component_id. Caller
    treats as ALLOW — components without registered scopes
    operate under the global default (V2 + risk tier alone)."""

    DISABLED = "disabled"
    """Master flag is off (``JARVIS_COMPONENT_TOOL_SCOPE_
    ENABLED=false``). Caller treats as ALLOW (byte-identical
    pre-slice behavior)."""


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_COMPONENT_TOOL_SCOPE_ENABLED`` master switch.
    Default-FALSE per §33.1: when off,
    :func:`evaluate_component_scope` returns DISABLED →
    callers treat as ALLOW (byte-identical pre-slice
    behavior)."""
    raw = os.environ.get(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Frozen artifact — ComponentToolScope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentToolScope:
    """One component's tool-allowlist policy. Frozen for safe
    propagation across async contexts. Adopts §33.5 versioned-
    artifact contract.

    Semantics:
      * When ``allowed_tools`` is non-empty, ONLY tools matching
        any pattern in the set are allowed (allowlist mode).
      * When ``allowed_tools`` is empty, all tools EXCEPT those
        matching ``denied_tools`` are allowed (denylist mode).
      * ``denied_tools`` always wins over ``allowed_tools``
        (defense-in-depth: explicit deny beats allow).
      * Patterns are V4 ``tool_name_pattern`` regex shapes
        (compile-once at register, ``re.fullmatch`` at dispatch).
      * ``inherits_from`` references another registered
        component_id; matched patterns from the parent compose
        UNDER the child (child's denied_tools still wins).
        Reserved for future hierarchical scoping; v1 ignores
        the field structurally.
    """

    component_id: str
    allowed_tools: FrozenSet[str] = field(
        default_factory=frozenset,
    )
    denied_tools: FrozenSet[str] = field(
        default_factory=frozenset,
    )
    inherits_from: str = ""
    schema_version: str = field(
        default=COMPONENT_TOOL_SCOPE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": str(self.component_id),
            "allowed_tools": sorted(self.allowed_tools),
            "denied_tools": sorted(self.denied_tools),
            "inherits_from": str(self.inherits_from),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Module-level registry — single source of truth
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, ComponentToolScope] = {}
_REGISTRY_LOCK = threading.RLock()


def register_scope(scope: ComponentToolScope) -> bool:
    """Register a component scope. Idempotent — re-registering
    the same component_id REPLACES the prior scope (operators
    can update without unregistering first). Returns ``True``
    on success, ``False`` when:

      * Master flag is off (caller wired prematurely).
      * ``scope.component_id`` is empty.
      * ``scope`` is malformed (defensive).

    NEVER raises.
    """
    if not master_enabled():
        return False
    if not isinstance(scope, ComponentToolScope):
        return False
    cid = str(scope.component_id).strip()
    if not cid:
        return False
    try:
        with _REGISTRY_LOCK:
            _REGISTRY[cid] = scope
        return True
    except Exception:  # noqa: BLE001 — defensive
        return False


def unregister_scope(component_id: str) -> bool:
    """Drop a component's scope. Idempotent — unregistering an
    unknown id is a silent no-op. NEVER raises."""
    cid = str(component_id or "").strip()
    if not cid:
        return False
    try:
        with _REGISTRY_LOCK:
            return _REGISTRY.pop(cid, None) is not None
    except Exception:  # noqa: BLE001 — defensive
        return False


def get_scope(component_id: str) -> Optional[ComponentToolScope]:
    """Read-only lookup. Returns ``None`` when unregistered.
    NEVER raises."""
    cid = str(component_id or "").strip()
    if not cid:
        return None
    try:
        with _REGISTRY_LOCK:
            return _REGISTRY.get(cid)
    except Exception:  # noqa: BLE001 — defensive
        return None


def list_components() -> Dict[str, ComponentToolScope]:
    """Snapshot of all registered scopes. Returns a NEW dict
    (caller-safe). NEVER raises."""
    try:
        with _REGISTRY_LOCK:
            return dict(_REGISTRY)
    except Exception:  # noqa: BLE001 — defensive
        return {}


def reset_registry_for_tests() -> None:
    """Test-only — pinned via naming convention."""
    try:
        with _REGISTRY_LOCK:
            _REGISTRY.clear()
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Async-safe component identity propagation
# ---------------------------------------------------------------------------


_ACTIVE_COMPONENT_VAR: (
    "contextvars.ContextVar[Optional[str]]"
) = contextvars.ContextVar(
    "component_tool_scope_active", default=None,
)


def set_active_component(
    component_id: Optional[str],
) -> "contextvars.Token[Optional[str]]":
    """Stamp the active component_id for the current async
    context. Returns a Token the caller MAY pass to
    :func:`reset_active_component` to restore the prior value
    (or simply let task exit clean it up). NEVER raises."""
    cid = (
        str(component_id).strip()
        if component_id is not None
        else None
    )
    return _ACTIVE_COMPONENT_VAR.set(cid)


def reset_active_component(
    token: "contextvars.Token[Optional[str]]",
) -> None:
    """Restore the prior component pointer. Defensive: invalid
    Token errors swallowed (mirrors §37 Tier 2 #13 Slice 2
    discipline)."""
    try:
        _ACTIVE_COMPONENT_VAR.reset(token)
    except (ValueError, LookupError, TypeError):
        logger.debug(
            "[ComponentToolScope] reset_active_component "
            "received stale/invalid token (non-fatal)",
        )


def get_active_component() -> str:
    """Read the active component_id (empty string when none).
    NEVER raises."""
    try:
        cid = _ACTIVE_COMPONENT_VAR.get()
    except LookupError:
        return ""
    return cid or ""


def reset_active_component_for_tests() -> None:
    """Test-only — pinned via naming convention."""
    try:
        _ACTIVE_COMPONENT_VAR.set(None)
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Tool-name pattern matcher — composes V4 substrate
# ---------------------------------------------------------------------------


_COMPILED_PATTERN_CACHE: Dict[str, Any] = {}
_COMPILED_PATTERN_CACHE_LOCK = threading.Lock()


def _compile_pattern_cached(pattern: str) -> Optional[Any]:
    """Memoize V4 :func:`compile_tool_name_pattern` results so
    pattern compilation happens once per (component, pattern)
    combination, not on every dispatch. Returns ``None`` on
    invalid pattern (caller treats as no-match)."""
    if not pattern:
        return None
    with _COMPILED_PATTERN_CACHE_LOCK:
        cached = _COMPILED_PATTERN_CACHE.get(pattern)
    if cached is not None:
        return cached
    try:
        from backend.core.ouroboros.governance.tool_name_pattern import (  # noqa: E501
            compile_tool_name_pattern,
        )
    except ImportError:
        return None
    try:
        compiled = compile_tool_name_pattern(pattern)
    except Exception:  # noqa: BLE001 — defensive
        # Invalid pattern → don't cache (let operator fix).
        return None
    with _COMPILED_PATTERN_CACHE_LOCK:
        _COMPILED_PATTERN_CACHE[pattern] = compiled
    return compiled


def _matches_any_pattern(
    tool_name: str, patterns: FrozenSet[str],
) -> bool:
    """Return True iff ``tool_name`` matches any pattern in the
    set. Composes V4's :func:`matches_tool_name` for regex
    semantics — no parallel matching logic.

    Patterns are V4 ``re.fullmatch`` regex shapes. For glob-
    style intent (e.g., "match any read tool"), use
    ``"read_.*"``. Compilation is memoized across calls.

    AST-pinned: this is the ONLY place component scope
    evaluates pattern membership; callers MUST go through
    this helper. NEVER raises.
    """
    if not tool_name or not patterns:
        return False
    try:
        from backend.core.ouroboros.governance.tool_name_pattern import (  # noqa: E501
            matches_tool_name,
        )
    except ImportError:
        # Fallback: exact match only when V4 substrate
        # unavailable (defensive — should never happen in
        # production, but covers edge cases like circular
        # imports during module load).
        return tool_name in patterns
    for pattern in patterns:
        try:
            compiled = _compile_pattern_cached(pattern)
            if compiled is None:
                # Invalid pattern → defensive exact match
                # fallback (operator likely meant the literal
                # tool name).
                if pattern == tool_name:
                    return True
                continue
            if matches_tool_name(compiled, tool_name):
                return True
        except Exception:  # noqa: BLE001 — defensive
            continue
    return False


def _reset_pattern_cache_for_tests() -> None:
    """Test-only — pinned via naming convention."""
    with _COMPILED_PATTERN_CACHE_LOCK:
        _COMPILED_PATTERN_CACHE.clear()


# ---------------------------------------------------------------------------
# Decision API
# ---------------------------------------------------------------------------


def evaluate_component_scope(
    *,
    component_id: str,
    tool_name: str,
) -> ComponentScopeDecision:
    """Decide whether ``tool_name`` is allowed under the
    registered scope for ``component_id``. NEVER raises.

    Resolution order (composing V4 pattern matching + V2-
    style first-DENY-wins):

      1. Master flag off → DISABLED.
      2. ``tool_name`` empty → ALLOW (defensive — caller
         likely has a bug, not a policy violation).
      3. ``component_id`` empty → NO_SCOPE (caller is not a
         component-tagged op; global gates handle it).
      4. No scope registered for ``component_id`` → NO_SCOPE.
      5. Tool matches ``denied_tools`` → DENY (deny wins).
      6. Allowlist non-empty AND tool does NOT match it →
         DENY.
      7. Otherwise → ALLOW.
    """
    if not master_enabled():
        return ComponentScopeDecision.DISABLED
    if not tool_name:
        return ComponentScopeDecision.ALLOW
    cid = str(component_id or "").strip()
    if not cid:
        return ComponentScopeDecision.NO_SCOPE
    scope = get_scope(cid)
    if scope is None:
        return ComponentScopeDecision.NO_SCOPE
    try:
        # Gate 5: explicit deny wins.
        if _matches_any_pattern(tool_name, scope.denied_tools):
            return ComponentScopeDecision.DENY
        # Gate 6: allowlist non-empty + tool not in it.
        if scope.allowed_tools and not _matches_any_pattern(
            tool_name, scope.allowed_tools,
        ):
            return ComponentScopeDecision.DENY
        return ComponentScopeDecision.ALLOW
    except Exception:  # noqa: BLE001 — defensive
        # Defensive ALLOW on evaluation failure — never block
        # tool dispatch on a substrate bug.
        return ComponentScopeDecision.ALLOW


def is_tool_allowed(
    *,
    component_id: str,
    tool_name: str,
) -> bool:
    """Convenience wrapper — returns False ONLY when
    :func:`evaluate_component_scope` returns DENY. NO_SCOPE /
    DISABLED / ALLOW all return True (caller defaults to
    permissive when no scope applies)."""
    decision = evaluate_component_scope(
        component_id=component_id, tool_name=tool_name,
    )
    return decision is not ComponentScopeDecision.DENY


def block_reason(
    *,
    component_id: str,
    tool_name: str,
) -> str:
    """Human-readable explanation when
    :func:`is_tool_allowed` returns False. Returns empty
    string when allowed."""
    decision = evaluate_component_scope(
        component_id=component_id, tool_name=tool_name,
    )
    if decision is not ComponentScopeDecision.DENY:
        return ""
    cid = str(component_id or "").strip()
    return (
        f"COMPONENT_SCOPE={cid!r} blocks tool {tool_name!r} "
        f"(use /scope show {cid} to inspect allowed/denied; "
        f"register a wider scope to permit)"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module reads."""
    try:
        registry.register(
            name="JARVIS_COMPONENT_TOOL_SCOPE_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §37 Tier 2 #16 (Pattern C) "
                "per-component tool scope. Default-FALSE per "
                "§33.1; when off, evaluate_component_scope "
                "returns DISABLED and callers treat as ALLOW "
                "(byte-identical pre-slice behavior)."
            ),
            category="Governance",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "component_tool_scope.py"
            ),
            example=(
                "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ComponentToolScope] FlagRegistry seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``component_scope_decision_taxonomy_4_values`` —
         closed enum bytes-pinned (ALLOW / DENY / NO_SCOPE /
         DISABLED).
      2. ``component_scope_master_flag_default_false`` —
         §33.1 producer flag stays default-FALSE.
      3. ``component_scope_authority_asymmetry`` — substrate
         purity (no orchestrator-tier imports).
      4. ``component_scope_composes_tool_name_pattern`` —
         pattern matching MUST go through V4 substrate; no
         parallel regex math.
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
        "component_tool_scope.py"
    )

    def _validate_decision_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "ALLOW", "DENY", "NO_SCOPE", "DISABLED",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ComponentScopeDecision"
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
                        f"ComponentScopeDecision has extra "
                        f"values {sorted(extra)} — taxonomy "
                        f"is closed"
                    )
                if missing:
                    violations.append(
                        f"ComponentScopeDecision missing "
                        f"required values {sorted(missing)}"
                    )
                return tuple(violations)
        violations.append(
            "ComponentScopeDecision class missing"
        )
        return tuple(violations)

    def _validate_master_default_false(
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
            violations.append("master_enabled() missing")
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
                            f"component_tool_scope.py MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_tool_name_pattern(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Pattern matching helper MUST lazy-import V4's
        :func:`matches_tool_name` (composition discipline; no
        parallel regex)."""
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_matches_any_pattern":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "_matches_any_pattern helper missing"
            )
            return tuple(violations)
        composes_v4 = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "tool_name_pattern" in module:
                    if any(
                        n.name == "matches_tool_name"
                        for n in sub.names
                    ):
                        composes_v4 = True
        # Also forbid direct re/regex usage in this function.
        uses_re_module = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.Attribute):
                if (
                    isinstance(sub.value, ast.Name)
                    and sub.value.id == "re"
                ):
                    uses_re_module = True
        if not composes_v4:
            violations.append(
                "_matches_any_pattern MUST lazy-import "
                "matches_tool_name from "
                "tool_name_pattern (composition discipline)"
            )
        if uses_re_module:
            violations.append(
                "_matches_any_pattern MUST NOT use 're' "
                "module directly — compose V4 substrate "
                "(no parallel regex)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "component_scope_decision_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #16 — ComponentScopeDecision is "
                "4-value closed enum (ALLOW/DENY/NO_SCOPE/"
                "DISABLED)."
            ),
            validate=_validate_decision_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "component_scope_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #16 — §33.1 producer flag stays "
                "default-FALSE; byte-identical pre-slice "
                "behavior when off."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "component_scope_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #16 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "component_scope_composes_tool_name_pattern"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #16 — pattern matching MUST "
                "compose V4's matches_tool_name; no parallel "
                "regex math."
            ),
            validate=_validate_composes_tool_name_pattern,
        ),
    ]


__all__ = [
    "COMPONENT_TOOL_SCOPE_SCHEMA_VERSION",
    "ComponentScopeDecision",
    "ComponentToolScope",
    "block_reason",
    "evaluate_component_scope",
    "get_active_component",
    "get_scope",
    "is_tool_allowed",
    "list_components",
    "master_enabled",
    "register_flags",
    "register_scope",
    "register_shipped_invariants",
    "reset_active_component",
    "reset_active_component_for_tests",
    "reset_registry_for_tests",
    "set_active_component",
    "unregister_scope",
]
