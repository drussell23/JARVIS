"""
UX Polish Pack — One Flag, Cohesive Polish
============================================

Solves the operator-visible UX gap identified in the brutal
review: ~15 §38/§39 polish substrates exist as default-FALSE
graduation-cadence substrates. The substrate code is shipped,
the AST pins are in place, the tests pass — but for a casual
operator comparing JARVIS to Claude Code on first launch, none
of the polish actually surfaces because each substrate gates
on its own env flag.

Per §33.1, individual graduation cadence is correct for
cognitive substrates. But UX polish surfaces are a **different
class** — they're display-layer renderers, not cognitive
substrates affecting decisions. The §33.1 evidence ladder
isn't load-bearing for them; what's load-bearing is
operator-ergonomic discovery.

This module is the canonical composition layer: ONE master
flag (``JARVIS_UX_POLISH_PACK_ENABLED``) activates the entire
polish suite cohesively. Operators retain full per-substrate
control via the individual flags — explicit `_ENABLED=false`
opts a specific substrate OUT even when the pack is on.

Composition contract — additive OR-composition over canonical
substrate master_enabled() functions:

* Each polish substrate's ``master_enabled()`` already reads
  its own env flag (default-FALSE).
* The pack exposes :func:`is_substrate_in_active_pack(name)`.
* Each substrate's ``master_enabled()`` is extended additively:

  .. code-block:: python

     if _flag(_ENV_MASTER, default=False):
         return True                          # explicit on
     return is_substrate_in_active_pack(name) # OR pack-on

* When operator flips ``JARVIS_X_ENABLED=false`` explicitly,
  the pack predicate is NOT consulted (sub-flag wins).
* When operator flips ``JARVIS_X_ENABLED=true`` explicitly,
  the substrate is on regardless of pack state.
* When the operator leaves the sub-flag unset AND flips the
  pack on, the substrate activates.

This preserves §33.1 graduation discipline (each substrate's
own master_enabled body retains its canonical default=False
line — AST pin still fires structurally) while giving casual
operators a single-flag UX.

Closed 15-value :class:`PolishSubstrate` taxonomy — one value
per substrate in the cohesive polish suite. Bytes-pinned via
AST so drift (adding a 16th substrate without updating the pack)
is structurally caught.

§33.1 master flag ``JARVIS_UX_POLISH_PACK_ENABLED`` default-**FALSE**
because this is a NEW substrate per the convention — operator
opts in once and the entire polish suite activates.

Authority asymmetry (AST-pinned): imports stdlib only at module
level; lazy-imports the canonical FlagRegistry on first
``polish_status()`` call. Does NOT import orchestrator /
iron_gate / policy / providers / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor. Pure-function predicate +
read-only operator-introspection surface.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


UX_POLISH_PACK_SCHEMA_VERSION: str = "ux_polish_pack.1"


# ===========================================================================
# Master flag — single operator-friendly switch
# ===========================================================================


_ENV_MASTER = "JARVIS_UX_POLISH_PACK_ENABLED"


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})
_FALSY: FrozenSet[str] = frozenset({"0", "false", "no", "off"})


def _flag(name: str, *, default: bool = False) -> bool:
    """Canonical truthy reader. Mirrors the pattern used in every
    polish substrate so the pack composes consistently."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _explicit_false(name: str) -> bool:
    """Return True iff the env var is set AND its value is
    explicitly in the falsy set. Used to detect 'operator
    explicitly disabled this substrate' (vs. 'operator left
    it unset')."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return False
    return raw in _FALSY


def pack_master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE.

    Operator opts in by flipping ``JARVIS_UX_POLISH_PACK_ENABLED=true``.
    """
    return _flag(_ENV_MASTER, default=False)


# ===========================================================================
# Closed taxonomy — 15-value PolishSubstrate
# ===========================================================================


class PolishSubstrate(str, enum.Enum):
    """Closed 15-value taxonomy — one value per polish substrate
    in the cohesive suite. Bytes-pinned via AST.

    Adding a 16th polish substrate requires:
      1. Extending this enum
      2. Adding an entry to :data:`_SUBSTRATE_REGISTRY`
      3. Wiring the new substrate's ``master_enabled()`` to
         compose :func:`is_substrate_in_active_pack`

    Two of three AST pins fire on drift, surfacing the
    incomplete addition before merge.
    """

    POLISH_BUNDLE = "polish_bundle"
    THINKING_PROGRESS = "thinking_progress_aggregator"
    TASK_PANEL = "task_panel_aggregator"
    POSTURE_MOOD_RING = "posture_palette"
    PIPELINE_PROGRESS = "pipeline_progress"
    ACTIVITY_RADAR = "activity_radar"
    OP_FANOUT_TREE = "op_fanout_tree"
    PHASE_FLOW_RIBBON = "phase_flow_ribbon"
    RISK_TIER_TINT = "risk_tier_tint"
    ORGANISM_DASHBOARD = "organism_dashboard"
    COGNITIVE_HEATMAP = "cognitive_heatmap"
    OP_TRAJECTORY_PREDICTOR = "op_trajectory_predictor"
    RISK_COMMAND_PREVIEW = "risk_command_preview"
    SESSION_STORY = "session_story"
    MEMORY_CRYSTALLIZATION = "memory_crystallization"


# ===========================================================================
# Substrate registry — bytes-pinned mapping of substrate → env var
# ===========================================================================


@dataclass(frozen=True)
class _SubstrateDescriptor:
    """Per-substrate metadata. Frozen so the registry tuple
    is structurally immutable."""

    substrate: PolishSubstrate
    env_var: str
    display_name: str
    """Human-readable label for the operator-introspection
    surface (``polish_status``)."""


# Bytes-pinned registry. AST pin asserts every PolishSubstrate
# enum value has exactly one descriptor here. Drift requires
# both an enum extension AND a registry append (visible diff).
_SUBSTRATE_REGISTRY: Tuple[_SubstrateDescriptor, ...] = (
    _SubstrateDescriptor(
        PolishSubstrate.POLISH_BUNDLE,
        "JARVIS_POLISH_BUNDLE_ENABLED",
        "Polish bundle (heartbeat / mood / sparklines / spinner)",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.THINKING_PROGRESS,
        "JARVIS_THINKING_PROGRESS_ENABLED",
        "Active-thinking timer",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.TASK_PANEL,
        "JARVIS_TASK_PANEL_ENABLED",
        "Persistent task panel",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.POSTURE_MOOD_RING,
        "JARVIS_POSTURE_MOOD_RING_ENABLED",
        "Posture mood ring",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.PIPELINE_PROGRESS,
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED",
        "11-phase pipeline progress bar",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.ACTIVITY_RADAR,
        "JARVIS_ACTIVITY_RADAR_ENABLED",
        "Live activity radar",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.OP_FANOUT_TREE,
        "JARVIS_OP_FANOUT_TREE_ENABLED",
        "Op fan-out tree",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.PHASE_FLOW_RIBBON,
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED",
        "Animated phase-flow ribbon",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.RISK_TIER_TINT,
        "JARVIS_RISK_TIER_TINT_ENABLED",
        "Risk-tier ambient color tint",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.ORGANISM_DASHBOARD,
        "JARVIS_ORGANISM_DASHBOARD_ENABLED",
        "Full organism dashboard",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.COGNITIVE_HEATMAP,
        "JARVIS_COGNITIVE_HEATMAP_ENABLED",
        "Cognitive heatmap",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.OP_TRAJECTORY_PREDICTOR,
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED",
        "Op trajectory predictor",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.RISK_COMMAND_PREVIEW,
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED",
        "Risk-aware command preview",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.SESSION_STORY,
        "JARVIS_SESSION_STORY_ENABLED",
        "Operator's-eye session story",
    ),
    _SubstrateDescriptor(
        PolishSubstrate.MEMORY_CRYSTALLIZATION,
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED",
        "Memory crystallization timeline",
    ),
)


# Reverse lookup: substrate name (string) → descriptor.
# Built once at module load for O(1) predicate calls.
_SUBSTRATE_BY_NAME: Dict[str, _SubstrateDescriptor] = {
    d.substrate.value: d for d in _SUBSTRATE_REGISTRY
}


def composed_substrates() -> Tuple[str, ...]:
    """Public accessor — return the closed tuple of substrate
    names this pack composes. Bounded, deterministic ordering."""
    return tuple(d.substrate.value for d in _SUBSTRATE_REGISTRY)


# ===========================================================================
# Pack predicate — the load-bearing composition surface
# ===========================================================================


def is_substrate_in_active_pack(substrate_name: str) -> bool:
    """Return True iff the polish pack is active AND the named
    substrate has NOT been explicitly disabled by its individual
    env flag.

    Composition semantics (additive OR with explicit-false veto):

    * Pack master off → ``False`` regardless of substrate flag
    * Pack master on + substrate flag explicitly ``false`` →
      ``False`` (operator-veto wins over pack default)
    * Pack master on + substrate flag explicitly ``true`` →
      ``True`` (pack and individual both want it)
    * Pack master on + substrate flag unset → ``True``
      (pack default-on grants the substrate)
    * Unknown substrate_name → ``False`` (defensive — no leak
      of unrecognized substrate names)

    NEVER raises. Pure-function predicate suitable for use
    inside other substrates' ``master_enabled()`` bodies via
    lazy-import — preserves §33.1 graduation discipline because
    the calling substrate's own ``default=False`` line stays
    canonically intact.
    """
    if not pack_master_enabled():
        return False
    try:
        normalized = str(substrate_name or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not normalized:
        return False
    descriptor = _SUBSTRATE_BY_NAME.get(normalized)
    if descriptor is None:
        return False
    # Operator-veto: explicit `=false` on the substrate's own
    # flag wins over the pack's pack-default-on policy.
    if _explicit_false(descriptor.env_var):
        return False
    return True


# ===========================================================================
# §33.5 frozen artifacts — operator-introspection surface
# ===========================================================================


@dataclass(frozen=True)
class PolishSubstrateState:
    """One substrate's pack-relative state — frozen."""

    substrate: str            # PolishSubstrate.value
    env_var: str
    display_name: str
    pack_grants: bool         # would the pack activate this?
    individually_enabled: bool  # explicit `=true` on sub-flag?
    individually_disabled: bool  # explicit `=false` on sub-flag?
    effective: bool           # final composed answer

    def to_dict(self) -> Dict[str, Any]:
        return {
            "substrate": self.substrate,
            "env_var": self.env_var,
            "display_name": self.display_name,
            "pack_grants": bool(self.pack_grants),
            "individually_enabled": bool(self.individually_enabled),
            "individually_disabled": bool(self.individually_disabled),
            "effective": bool(self.effective),
        }


@dataclass(frozen=True)
class PolishPackReport:
    """Aggregate operator-visible pack status — §33.5 artifact."""

    reported_at_unix: float
    pack_master_enabled: bool
    substrates: Tuple[PolishSubstrateState, ...]
    active_count: int
    """Number of substrates effectively on (pack OR individual)."""
    explicitly_vetoed_count: int
    """Number of substrates the pack would activate that the
    operator explicitly disabled via their individual flag."""
    diagnostic: str
    schema_version: str = UX_POLISH_PACK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reported_at_unix": float(self.reported_at_unix),
            "pack_master_enabled": bool(self.pack_master_enabled),
            "substrates": [s.to_dict() for s in self.substrates],
            "active_count": int(self.active_count),
            "explicitly_vetoed_count": int(
                self.explicitly_vetoed_count,
            ),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Operator-facing introspection
# ===========================================================================


def polish_status(
    *,
    now_unix: Optional[float] = None,
) -> PolishPackReport:
    """Build an operator-visible report of every polish
    substrate's state. NEVER raises.

    Useful for ``/polish status`` REPL command + IDE GET
    introspection surface."""
    started = (
        time.time() if now_unix is None else float(now_unix)
    )
    pack_on = pack_master_enabled()
    states: List[PolishSubstrateState] = []
    active = 0
    vetoed = 0
    for descriptor in _SUBSTRATE_REGISTRY:
        # Pack would grant?
        pack_grants = pack_on and not _explicit_false(
            descriptor.env_var,
        )
        individual_on = _flag(
            descriptor.env_var, default=False,
        )
        individual_off = _explicit_false(descriptor.env_var)
        effective = individual_on or (
            pack_on and not individual_off
        )
        if effective:
            active += 1
        if pack_on and individual_off:
            vetoed += 1
        states.append(PolishSubstrateState(
            substrate=descriptor.substrate.value,
            env_var=descriptor.env_var,
            display_name=descriptor.display_name,
            pack_grants=pack_grants,
            individually_enabled=individual_on,
            individually_disabled=individual_off,
            effective=effective,
        ))
    if not pack_on:
        diagnostic = (
            f"pack disabled ({_ENV_MASTER}=false) — "
            f"{active} substrate(s) individually enabled"
        )
    elif vetoed:
        diagnostic = (
            f"pack active — {active} substrate(s) on, "
            f"{vetoed} explicitly vetoed by operator"
        )
    else:
        diagnostic = (
            f"pack active — all {active} substrate(s) on"
        )
    return PolishPackReport(
        reported_at_unix=started,
        pack_master_enabled=pack_on,
        substrates=tuple(states),
        active_count=active,
        explicitly_vetoed_count=vetoed,
        diagnostic=diagnostic,
    )


def format_polish_panel(
    report: Optional[PolishPackReport] = None,
) -> str:
    """Operator-facing rendered panel. NEVER raises."""
    if report is None:
        report = polish_status()
    lines = [
        f"✨ UX Polish Pack  "
        f"({'active' if report.pack_master_enabled else 'disabled'})",
        f"  active substrates    : {report.active_count}"
        f" / {len(report.substrates)}",
    ]
    if report.explicitly_vetoed_count:
        lines.append(
            f"  operator-vetoed      : "
            f"{report.explicitly_vetoed_count}",
        )
    lines.append("  substrate states:")
    for s in report.substrates:
        if s.individually_disabled:
            marker = "✗"
        elif s.effective:
            marker = "✓"
        else:
            marker = "○"
        lines.append(
            f"    {marker} {s.display_name}",
        )
    lines.append(f"  diagnostic           : {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "ux_polish_pack.py"
    )

    _EXPECTED_SUBSTRATES = {
        "polish_bundle", "thinking_progress_aggregator",
        "task_panel_aggregator", "posture_palette",
        "pipeline_progress", "activity_radar",
        "op_fanout_tree", "phase_flow_ribbon",
        "risk_tier_tint", "organism_dashboard",
        "cognitive_heatmap", "op_trajectory_predictor",
        "risk_command_preview", "session_story",
        "memory_crystallization",
    }

    def _validate_substrate_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PolishSubstrate"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_SUBSTRATES - found
                extra = found - _EXPECTED_SUBSTRATES
                if missing:
                    return (
                        f"PolishSubstrate missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"PolishSubstrate drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("PolishSubstrate class not found",)

    def _validate_registry_completeness(
        tree: ast.AST, source: str,
    ) -> tuple:
        """The _SUBSTRATE_REGISTRY tuple MUST contain exactly one
        _SubstrateDescriptor per PolishSubstrate enum value.
        Drift = silently dropping a substrate from the pack."""
        # Heuristic: count occurrences of each enum-value string
        # inside the _SUBSTRATE_REGISTRY assignment.
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_SUBSTRATE_REGISTRY"
            ):
                if not (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "_SUBSTRATE_REGISTRY"
                ):
                    continue
            # Walk the assignment subtree counting attribute
            # references to each PolishSubstrate.*
            assigned_value = (
                node.value if hasattr(node, "value") else None
            )
            if assigned_value is None:
                continue
            referenced: set = set()
            for sub in ast.walk(assigned_value):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "PolishSubstrate"
                ):
                    referenced.add(sub.attr)
            expected_attrs = {
                "POLISH_BUNDLE", "THINKING_PROGRESS",
                "TASK_PANEL", "POSTURE_MOOD_RING",
                "PIPELINE_PROGRESS", "ACTIVITY_RADAR",
                "OP_FANOUT_TREE", "PHASE_FLOW_RIBBON",
                "RISK_TIER_TINT", "ORGANISM_DASHBOARD",
                "COGNITIVE_HEATMAP", "OP_TRAJECTORY_PREDICTOR",
                "RISK_COMMAND_PREVIEW", "SESSION_STORY",
                "MEMORY_CRYSTALLIZATION",
            }
            missing = expected_attrs - referenced
            extra = referenced - expected_attrs
            if missing:
                return (
                    f"_SUBSTRATE_REGISTRY missing: "
                    f"{sorted(missing)}",
                )
            if extra:
                return (
                    f"_SUBSTRATE_REGISTRY unexpected: "
                    f"{sorted(extra)}",
                )
            return ()
        return ("_SUBSTRATE_REGISTRY not found",)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "pack_master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "pack_master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("pack_master_enabled() not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_predicate_returns_bool(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """The load-bearing public predicate
        is_substrate_in_active_pack MUST exist + must short-
        circuit when master flag is off (load-bearing for the
        pack's gate semantics)."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_substrate_in_active_pack"
            ):
                # Look for an early `if not pack_master_enabled()`
                # check.
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.UnaryOp)
                        and isinstance(sub.op, ast.Not)
                        and isinstance(sub.operand, ast.Call)
                        and isinstance(sub.operand.func, ast.Name)
                        and sub.operand.func.id
                        == "pack_master_enabled"
                    ):
                        return ()
                return (
                    "is_substrate_in_active_pack() must "
                    "short-circuit on `if not "
                    "pack_master_enabled():` — load-bearing "
                    "for pack-off-means-no-grant semantics",
                )
        return (
            "is_substrate_in_active_pack() not found",
        )

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "ux_polish_pack_substrate_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "PolishSubstrate 15-value taxonomy bytes-pinned. "
                "Adding/removing requires updating "
                "_SUBSTRATE_REGISTRY + wiring the new substrate's "
                "master_enabled()."
            ),
            validate=_validate_substrate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ux_polish_pack_registry_completeness"
            ),
            target_file=target,
            description=(
                "_SUBSTRATE_REGISTRY tuple MUST contain exactly "
                "one descriptor per PolishSubstrate enum value."
            ),
            validate=_validate_registry_completeness,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ux_polish_pack_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 — pack master default-FALSE per the "
                "new-substrate convention."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ux_polish_pack_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure-function predicate. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ux_polish_pack_predicate_short_circuits"
            ),
            target_file=target,
            description=(
                "is_substrate_in_active_pack() must short-circuit "
                "via `if not pack_master_enabled():` — load-bearing "
                "for pack-off semantics."
            ),
            validate=_validate_predicate_returns_bool,
        ),
    ]


# ===========================================================================
# FlagRegistry seed
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "ux_polish_pack.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "UX polish pack master switch. §33.1 cognitive-"
                "substrate default-FALSE. When on, activates "
                "the entire 15-substrate polish suite cohesively "
                "(heartbeat, mood ring, pipeline progress bar, "
                "active-thinking timer, etc.). Operators retain "
                "full per-substrate control — explicit "
                "JARVIS_X_ENABLED=false on any substrate vetoes "
                "the pack's pack-default-on policy for that "
                "substrate."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "UX_POLISH_PACK_SCHEMA_VERSION",
    "PolishSubstrate",
    "PolishSubstrateState",
    "PolishPackReport",
    "pack_master_enabled",
    "is_substrate_in_active_pack",
    "composed_substrates",
    "polish_status",
    "format_polish_panel",
    "register_shipped_invariants",
    "register_flags",
]
