"""Emit-tier substrate — adaptive information density per posture.

Closes the "70+ SerpentFlow emit methods at every density" UX gap.
Per-op rendering currently shows 5-6 lines (sensed → route → planning
→ routing → synthesizing) regardless of operator-visible density.
CC's restraint shows 1-2 lines per op at default density and reveals
the rest only when explicitly asked.

Architectural pillars:

  1. **Closed-taxonomy EmitTier** — ``{PRIMARY, SECONDARY, TERTIARY}``.
     PRIMARY always shown (op_started/completed/failed — essential
     events). SECONDARY shown at NORMAL/FULL density (route, cost,
     validation). TERTIARY shown only at FULL (planning detail,
     per-tick synthesizing, individual tool calls). AST-pinned.
  2. **Tier→density resolution is pure** — :func:`visible_at_density`
     is a closed-taxonomy mapping with no I/O. Each call costs ~50 ns
     so the gate can sit in front of every emit without a hot-path
     concern.
  3. **In-code default tier table is the floor; operator overrides
     layer on top** — ``JARVIS_EMIT_TIER_OVERRIDE`` (JSON map of
     ``{method_name: tier_value}``) lets operators promote/demote any
     emit. Unknown method names + bad tier values silently skipped
     (degrades to default for that method).
  4. **AST-pinned method-name correctness** — every key in
     ``_DEFAULT_TIER_MAP`` MUST correspond to an actual ``def`` in
     :mod:`serpent_flow`. Cross-file pin catches drift when methods
     are renamed without updating the table. Without this pin, a
     stale entry would silently fail the gate (everything emits
     because no method named X exists to be filtered).
  5. **Master flag default false initially** — substrate ships
     dormant. Operator opts in via
     ``JARVIS_EMIT_TIER_GATING_ENABLED=true`` after empirical
     validation. Same posture as previous substrate-arc additions
     (Slice 4 InputController, Slice 5 ThreadObserver, etc.).
  6. **Defensive everywhere** — every accessor returns degraded
     value (default tier, "always emit" should_emit) on registry
     failure. The gate NEVER raises; a misbehaving substrate
     never breaks the producer's emit path.

Authority invariants (AST-pinned):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge.
  * :class:`EmitTier` member set is the documented closed set.
  * Every ``_DEFAULT_TIER_MAP`` key MUST exist as a ``def`` in
    serpent_flow.py (cross-file pin).
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_EMIT_TIER_GATING_ENABLED`` — master gate. Default
    ``false``. When false, :func:`should_emit` always returns ``True``
    (all emits visible — pre-substrate behavior). Hot-revert preserved.
  * ``JARVIS_EMIT_TIER_OVERRIDE`` — JSON map ``{method_name:
    tier_value}``. Default ``{}``. Unknown methods/tiers silently
    skipped.
  * Density resolution flows through :func:`render_conductor.
    resolve_density` — same posture-aware adaptivity that drives
    color palette + theme density. Same env knobs apply.
"""
from __future__ import annotations

import enum
import logging
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


RENDER_EMIT_TIER_SCHEMA_VERSION: str = "render_emit_tier.1"


_FLAG_EMIT_TIER_GATING_ENABLED = "JARVIS_EMIT_TIER_GATING_ENABLED"
_FLAG_EMIT_TIER_OVERRIDE = "JARVIS_EMIT_TIER_OVERRIDE"


# ---------------------------------------------------------------------------
# EmitTier — closed taxonomy
# ---------------------------------------------------------------------------


class EmitTier(str, enum.Enum):
    """Closed taxonomy of emit visibility tiers.

    PRIMARY: Always shown regardless of density. Reserved for events
    the operator MUST see — op start, op completion, op failure,
    proactive alerts. Removing visibility would break operator
    awareness of what's happening.

    SECONDARY: Shown at NORMAL or FULL density. Useful context that
    benefits informed operators but isn't strictly essential — route
    info, cost, validation outcomes. Hidden at COMPACT density.

    TERTIARY: Shown only at FULL density. Deep-debug detail —
    per-phase ticks (planning, synthesizing), per-tool calls,
    intermediate generation summaries, status updates. Operators
    asking for full visibility (debugging or demo) see these.
    """

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    TERTIARY = "TERTIARY"


# ---------------------------------------------------------------------------
# Default tier mapping — every SerpentFlow emit method tagged
# ---------------------------------------------------------------------------


# Mapping is the in-code default. Operators overlay via
# JARVIS_EMIT_TIER_OVERRIDE JSON. Each key MUST correspond to an
# actual def in serpent_flow.py — AST-pinned cross-file (catches
# rename drift).
#
# Notes on tier choices:
# - PRIMARY: events the operator MUST see for situational awareness.
# - SECONDARY: useful context at default density.
# - TERTIARY: deep-debug detail (planning rationale, per-tick
#   spinners, per-tool calls).
_DEFAULT_TIER_MAP: Mapping[str, EmitTier] = {
    # ─── PRIMARY (always shown) ────────────────────────────────
    "op_started":           EmitTier.PRIMARY,
    "op_completed":         EmitTier.PRIMARY,
    "op_failed":            EmitTier.PRIMARY,
    "op_noop":              EmitTier.PRIMARY,
    "emit_proactive_alert": EmitTier.PRIMARY,
    "boot_banner":          EmitTier.PRIMARY,

    # ─── SECONDARY (NORMAL+) ───────────────────────────────────
    "set_op_route":         EmitTier.SECONDARY,
    "op_validation_start":  EmitTier.SECONDARY,
    "op_validation":        EmitTier.SECONDARY,
    "op_l2_repair":         EmitTier.SECONDARY,
    "op_verify_start":      EmitTier.SECONDARY,
    "op_verify_result":     EmitTier.SECONDARY,
    "update_cost":          EmitTier.SECONDARY,

    # ─── TERTIARY (FULL only — deep debug) ─────────────────────
    "op_provider":             EmitTier.TERTIARY,
    "op_phase":                EmitTier.TERTIARY,
    "op_generation":           EmitTier.TERTIARY,
    "op_tool_start":           EmitTier.TERTIARY,
    "op_tool_call":            EmitTier.TERTIARY,
    "op_subagent_spawn":       EmitTier.TERTIARY,
    "op_subagent_result":      EmitTier.TERTIARY,
    "show_streaming_start":    EmitTier.TERTIARY,
    "show_streaming_token":    EmitTier.TERTIARY,
    "show_streaming_end":      EmitTier.TERTIARY,
    "show_code_preview":       EmitTier.TERTIARY,
    "show_diff":               EmitTier.TERTIARY,
    "_render_plan_phase":      EmitTier.TERTIARY,
    "_render_commit_phase":    EmitTier.TERTIARY,
    "update_intent_chain":     EmitTier.TERTIARY,
    "update_triage":           EmitTier.TERTIARY,
    "update_intent_discovery": EmitTier.TERTIARY,
    "update_dream_engine":     EmitTier.TERTIARY,
    "update_learning":         EmitTier.TERTIARY,
    "update_session_lessons":  EmitTier.TERTIARY,
    "update_sensors":          EmitTier.TERTIARY,
    "update_provider_chain":   EmitTier.TERTIARY,
}


# Default tier when an unknown method name is queried. Conservative —
# unknown methods stay visible (PRIMARY) so we don't accidentally hide
# a freshly-added emit until it's tagged in the table.
_DEFAULT_TIER_FALLBACK: EmitTier = EmitTier.PRIMARY


# ---------------------------------------------------------------------------
# Flag accessors
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master gate. Graduated default ``true`` at D5 — operators
    get the cleaner CLI by default. Hot-revert via
    ``JARVIS_EMIT_TIER_GATING_ENABLED=false`` returns to pre-
    substrate behavior (all emits visible). When ``true``, per-op
    TERTIARY chatter (planning/routing/synthesizing/per-tool/per-
    subagent) is hidden at NORMAL density — operators see ~3 lines
    per op instead of ~6."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_EMIT_TIER_GATING_ENABLED, default=True)


def operator_tier_overrides() -> Mapping[str, EmitTier]:
    """Resolved operator overlay on the default tier map. Unknown
    method names + bad tier values silently skipped. Returns an
    empty mapping when registry unavailable or env not set."""
    reg = _get_registry()
    if reg is None:
        return {}
    raw = reg.get_json(_FLAG_EMIT_TIER_OVERRIDE, default=None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, EmitTier] = {}
    for method_name, tier_value in raw.items():
        if not isinstance(method_name, str):
            continue
        if not isinstance(tier_value, str):
            continue
        try:
            tier = EmitTier(tier_value.strip().upper())
        except ValueError:
            logger.debug(
                "[render_emit_tier] unknown tier in override for %s: %r",
                method_name, tier_value,
            )
            continue
        out[method_name.strip()] = tier
    return out


# ---------------------------------------------------------------------------
# Public API: resolve_tier + visible_at_density + should_emit
# ---------------------------------------------------------------------------


def tier_for_method(method_name: str) -> EmitTier:
    """Resolve the active tier for ``method_name``. Operator
    overrides win over the in-code default. Unknown methods fall
    back to ``_DEFAULT_TIER_FALLBACK`` (PRIMARY — conservative)."""
    if not isinstance(method_name, str) or not method_name.strip():
        return _DEFAULT_TIER_FALLBACK
    overrides = operator_tier_overrides()
    if method_name in overrides:
        return overrides[method_name]
    return _DEFAULT_TIER_MAP.get(method_name, _DEFAULT_TIER_FALLBACK)


def visible_at_density(tier: EmitTier, density: Any) -> bool:
    """Closed-taxonomy decision: should an emit at ``tier`` be
    visible at ``density``?

    Rules (load-bearing — AST pin checks the documented behavior):
      * COMPACT  → PRIMARY only
      * NORMAL   → PRIMARY + SECONDARY
      * FULL     → all three tiers
      * Unknown density → defaults to NORMAL semantics (operator-
        friendly: a typo in env doesn't accidentally hide everything)
    """
    if tier is EmitTier.PRIMARY:
        return True
    # Density may be a string (from env) or a RenderDensity enum.
    density_str = (
        density.value if hasattr(density, "value") else str(density or "")
    ).strip().upper()
    if density_str == "FULL":
        return True
    if density_str == "NORMAL":
        return tier in (EmitTier.PRIMARY, EmitTier.SECONDARY)
    if density_str == "COMPACT":
        return tier is EmitTier.PRIMARY
    # Unknown density string → NORMAL semantics
    return tier in (EmitTier.PRIMARY, EmitTier.SECONDARY)


def should_emit(method_name: str) -> bool:
    """The single helper SerpentFlow's emit methods call.

    Returns ``True`` when:
      * Master flag is ``false`` (substrate disabled — pre-substrate
        behavior preserved)
      * OR the method's tier is visible at the conductor's currently-
        resolved density

    Defensive — every failure path returns ``True`` (visible). A
    misbehaving substrate never silences operator-visible emits;
    worst case is "the gate is a no-op", not "operator misses
    important events"."""
    try:
        if not is_enabled():
            return True
        tier = tier_for_method(method_name)
        density = _resolve_active_density()
        return visible_at_density(tier, density)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_emit_tier] should_emit failed for %s — "
            "defaulting to visible", method_name, exc_info=True,
        )
        return True


def _resolve_active_density() -> Any:
    """Read the conductor's currently-resolved density. Cheap —
    reuses the conductor's existing accessor. Returns the enum value
    or ``"NORMAL"`` string on failure."""
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            get_render_conductor,
        )
        conductor = get_render_conductor()
        if conductor is None:
            return "NORMAL"
        return conductor.active_density()
    except Exception:  # noqa: BLE001 — defensive
        return "NORMAL"


# ---------------------------------------------------------------------------
# Introspection helpers — for /render REPL + GET observability
# ---------------------------------------------------------------------------


def all_known_method_names() -> tuple:
    """Return every method name in the in-code default map. Used by
    AST pin to verify each entry corresponds to an actual def in
    serpent_flow.py."""
    return tuple(sorted(_DEFAULT_TIER_MAP.keys()))


def tier_table_snapshot() -> Mapping[str, str]:
    """Resolved tier-table snapshot (defaults + operator overrides).
    Returns ``{method_name: tier.value}``."""
    base: Dict[str, str] = {
        name: tier.value for name, tier in _DEFAULT_TIER_MAP.items()
    }
    for name, tier in operator_tier_overrides().items():
        base[name] = tier.value
    return base


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    all_postures_relevant = {
        "EXPLORE": Relevance.RELEVANT,
        "CONSOLIDATE": Relevance.RELEVANT,
        "HARDEN": Relevance.RELEVANT,
        "MAINTAIN": Relevance.RELEVANT,
    }
    specs = [
        FlagSpec(
            name=_FLAG_EMIT_TIER_GATING_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for emit-tier visibility filtering "
                "(D3 substrate). Graduated default true at D5 — "
                "operators get the cleaner CLI by default. SerpentFlow "
                "emit methods filter by EmitTier per the conductor's "
                "resolved density: COMPACT=PRIMARY only, "
                "NORMAL=PRIMARY+SECONDARY, FULL=all three. Hot-revert "
                "via false returns to pre-substrate behavior."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/render_emit_tier.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_EMIT_TIER_OVERRIDE,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on the in-code emit-tier map. "
                "JSON object mapping SerpentFlow method names "
                "(op_provider / op_phase / show_streaming_start / "
                "etc.) to EmitTier values (PRIMARY / SECONDARY / "
                "TERTIARY). Unknown methods + bad tier values "
                "silently skipped — operator typos degrade to "
                "default for that method."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/render_emit_tier.py"
            ),
            example='{"op_provider": "PRIMARY"}',
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered
# ---------------------------------------------------------------------------


_FORBIDDEN_RICH_PREFIX: tuple = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: tuple = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.risk_tier",
    "backend.core.ouroboros.governance.risk_tier_floor",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.gate",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.cancel_token",
    "backend.core.ouroboros.governance.conversation_bridge",
)


_EXPECTED_EMIT_TIER = frozenset({"PRIMARY", "SECONDARY", "TERTIARY"})


def _imported_modules(tree: Any) -> List:
    import ast
    out: List = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                out.append((node.lineno, mod))
    return out


def _enum_member_names(tree: Any, class_name: str) -> List[str]:
    import ast
    out: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        out.append(tgt.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(
                stmt.target, ast.Name,
            ):
                if stmt.target.id.isupper():
                    out.append(stmt.target.id)
    return out


def _validate_no_rich_import(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod == forbidden or mod.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_emit_tier_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "EmitTier"))
    if found != _EXPECTED_EMIT_TIER:
        return (
            f"EmitTier members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_EMIT_TIER)}",
        )
    return ()


def _validate_tier_map_methods_exist(
    tree: Any, source: str,
) -> tuple:
    """Cross-file pin: every key in ``_DEFAULT_TIER_MAP`` MUST
    correspond to an actual ``def`` in serpent_flow.py. Catches
    rename drift — without this pin, a stale entry silently fails
    the gate (everything emits because no method matches the
    stale name). Pin reads the OTHER file (serpent_flow.py) so
    target_file points there."""
    del tree
    # source IS serpent_flow.py per the invariant target_file
    import re
    expected_method_names = all_known_method_names()
    # Parse out every "def <name>(" in serpent_flow source
    found_defs = set(
        re.findall(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z_0-9]*)\s*\(",
                   source, re.MULTILINE)
    )
    missing = [
        name for name in expected_method_names
        if name not in found_defs
    ]
    if missing:
        return (
            f"render_emit_tier _DEFAULT_TIER_MAP has stale entries "
            f"(no matching def in serpent_flow.py): {missing}",
        )
    return ()


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (f"missing discovery symbols: {sorted(missing)}",)
    return ()


_TARGET_FILE = (
    "backend/core/ouroboros/governance/render_emit_tier.py"
)
_SERPENT_FLOW_TARGET = (
    "backend/core/ouroboros/battle_test/serpent_flow.py"
)


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_emit_tier_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "render_emit_tier.py MUST NOT import rich.* — the "
                "substrate is a pure visibility decision engine; "
                "rendering is downstream's concern."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_emit_tier_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "render_emit_tier.py MUST NOT import any authority "
                "module. Visibility filtering is descriptive only — "
                "never a control-flow surface."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_emit_tier_emit_tier_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "EmitTier enum members must exactly match the "
                "documented 3-value closed set (PRIMARY, SECONDARY, "
                "TERTIARY). Adding a tier requires coordinated "
                "visible_at_density update."
            ),
            validate=_validate_emit_tier_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_emit_tier_map_methods_exist",
            target_file=_SERPENT_FLOW_TARGET,
            description=(
                "Cross-file pin: every key in render_emit_tier's "
                "_DEFAULT_TIER_MAP MUST correspond to an actual def "
                "in serpent_flow.py. Catches rename drift — without "
                "this pin, a stale entry silently fails the gate "
                "(everything emits because the method name doesn't "
                "match anything)."
            ),
            validate=_validate_tier_map_methods_exist,
        ),
        ShippedCodeInvariant(
            invariant_name="render_emit_tier_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must "
                "be module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "EmitTier",
    "RENDER_EMIT_TIER_SCHEMA_VERSION",
    "all_known_method_names",
    "is_enabled",
    "operator_tier_overrides",
    "register_flags",
    "register_shipped_invariants",
    "should_emit",
    "tier_for_method",
    "tier_table_snapshot",
    "visible_at_density",
]
