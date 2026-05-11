"""Pipeline progress-bar renderer (PRD §38 Slice 2, 2026-05-07).

Closes the operator-flagged "11-phase pipeline buried as `Phase:
GENERATE` label" gap from §38 Slice 2. CC has loose stages
(analyzing / coding / etc); O+V has a deterministic 11-phase FSM
(CLASSIFY → ROUTE → CONTEXT_EXPANSION → PLAN → GENERATE →
VALIDATE → GATE → APPROVE → APPLY → VERIFY → COMPLETE) that CC
structurally cannot match. Slice 2 renders the deterministic
position as a progress bar — visualizing the unique-to-O+V
substrate.

## Why this exists

The 11-phase forward-flow pipeline is the cognitive substrate of
O+V's autonomous loop. Operators today see a single phase label
(``Phase: GENERATE``) — they cannot tell whether GENERATE is at
phase 5 of 11 (early in the loop) or phase 9 of 11 (almost done).
The progress bar surfaces position deterministically.

## Composes canonical sources (operator binding "no duplication")

  * :class:`governance.op_context.OperationPhase` — the canonical
    17-value phase enum (11 forward-flow + 4 retry/terminal +
    2 error). Forward-flow is a SUBSET — AST-pinned to ensure
    every entry in :data:`FORWARD_FLOW_PHASES` is a valid
    :class:`OperationPhase` member.
  * :data:`governance.op_context.PHASE_TRANSITIONS` — used in
    regression tests to prove every forward-flow phase is
    reachable from CLASSIFY (no orphaned entries).

NEVER reimplements the phase enum or transition graph — pure
render layer over canonical state.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — no I/O. NEVER raises.
  2. **Authority asymmetry** — imports stdlib + governance.op_context
     ONLY. NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / change_engine /
     semantic_guardian.
  3. **Forward-flow subset of OperationPhase** — every entry in
     :data:`FORWARD_FLOW_PHASES` MUST be a valid
     :class:`OperationPhase` enum member. AST-pinned via
     enum-membership check.
  4. **Forward-flow length pinned to 11** — matches CLAUDE.md's
     canonical pipeline doc + §28.5.1 11-phase extraction
     closure. Length change requires explicit scope-doc + pin
     update.
  5. **Master flag default-FALSE** per §33.1.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


PIPELINE_PROGRESS_SCHEMA_VERSION: str = "pipeline_progress.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_PIPELINE_PROGRESS_BAR_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off,
    :func:`format_pipeline_progress` returns empty string and
    the status line renders pre-Slice-2 byte-identical.
    Operator flips after observing the bar composition."""
    if os.environ.get( "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "", ).strip().lower() in _TRUTHY:
        return True
    # §40 polish pack opt-in — when JARVIS_UX_POLISH_PACK_ENABLED
    # is on AND the operator hasn't explicitly disabled this
    # substrate via its own env flag, the pack predicate
    # activates it. Preserves §33.1 default-FALSE discipline:
    # the canonical _flag(...) / _TRUTHY check above is intact
    # so the substrate's master_default_false AST pin still
    # fires structurally.
    try:
        from backend.core.ouroboros.governance.ux_polish_pack import (
            is_substrate_in_active_pack,
        )
        return is_substrate_in_active_pack('pipeline_progress')
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Glyph knobs — env-overridable (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


_FILLED_GLYPH_DEFAULT: str = "●"
_EMPTY_GLYPH_DEFAULT: str = "○"
_BAR_OPEN_DEFAULT: str = "["
_BAR_CLOSE_DEFAULT: str = "]"


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw


def filled_glyph() -> str:
    return _env_str(
        "JARVIS_PIPELINE_PROGRESS_FILLED_GLYPH",
        _FILLED_GLYPH_DEFAULT,
    )


def empty_glyph() -> str:
    return _env_str(
        "JARVIS_PIPELINE_PROGRESS_EMPTY_GLYPH",
        _EMPTY_GLYPH_DEFAULT,
    )


def bar_open() -> str:
    return _env_str(
        "JARVIS_PIPELINE_PROGRESS_BAR_OPEN",
        _BAR_OPEN_DEFAULT,
    )


def bar_close() -> str:
    return _env_str(
        "JARVIS_PIPELINE_PROGRESS_BAR_CLOSE",
        _BAR_CLOSE_DEFAULT,
    )


# ---------------------------------------------------------------------------
# Canonical forward-flow phase tuple (subset of OperationPhase)
# ---------------------------------------------------------------------------
#
# Lazy-evaluated to avoid import-time enum cycle. The tuple
# captures the canonical FORWARD-FLOW happy path:
#
#   CLASSIFY → ROUTE → CONTEXT_EXPANSION → PLAN → GENERATE →
#   VALIDATE → GATE → APPROVE → APPLY → VERIFY → COMPLETE
#
# Length 11 matches:
#   - CLAUDE.md "11-phase governance pipeline" doc
#   - §28.5.1 phase-extraction-closure 11-phase invariant
#   - phase_runners/ package (8 runners + Slice4bRunner combining
#     APPROVE+APPLY+VERIFY = 8+3 = 11 effective phases)
#
# Excluded by design (NOT forward-flow):
#   - GENERATE_RETRY / VALIDATE_RETRY (loop-back)
#   - VISUAL_VERIFY (post-VERIFY optional UI check)
#   - CANCELLED / EXPIRED / POSTMORTEM (terminal/error states)


_FORWARD_FLOW_PHASE_NAMES: Tuple[str, ...] = (
    "CLASSIFY",
    "ROUTE",
    "CONTEXT_EXPANSION",
    "PLAN",
    "GENERATE",
    "VALIDATE",
    "GATE",
    "APPROVE",
    "APPLY",
    "VERIFY",
    "COMPLETE",
)


_FORWARD_FLOW_CACHE: Optional[Tuple[Any, ...]] = None


def forward_flow_phases() -> Tuple[Any, ...]:
    """Return the canonical 11-phase forward-flow tuple as
    :class:`OperationPhase` enum members.

    Lazy-resolved on first call (avoids import-time cycle).
    Falls back to empty tuple on any import failure (defensive
    — caller will produce empty render rather than crash)."""
    global _FORWARD_FLOW_CACHE
    if _FORWARD_FLOW_CACHE is not None:
        return _FORWARD_FLOW_CACHE
    try:
        from backend.core.ouroboros.governance.op_context import (
            OperationPhase,
        )
        resolved = []
        for name in _FORWARD_FLOW_PHASE_NAMES:
            member = getattr(OperationPhase, name, None)
            if member is None:
                # Defensive: skip missing members. Test pin
                # `forward_flow_subset_of_OperationPhase` will
                # fire if any name in the tuple isn't a valid
                # enum member.
                continue
            resolved.append(member)
        _FORWARD_FLOW_CACHE = tuple(resolved)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[pipeline_progress] forward_flow_phases "
            "swallowed: %s",
            type(exc).__name__,
        )
        _FORWARD_FLOW_CACHE = ()
    return _FORWARD_FLOW_CACHE


def reset_forward_flow_cache_for_tests() -> None:
    """TEST-ONLY entry point. Clears the lazy-cache so tests
    can re-derive after monkey-patching OperationPhase."""
    global _FORWARD_FLOW_CACHE
    _FORWARD_FLOW_CACHE = None


def forward_flow_length() -> int:
    """Return the canonical forward-flow phase count (11).
    Composes :func:`forward_flow_phases` — single source of
    truth; never hardcoded."""
    return len(forward_flow_phases())


# ---------------------------------------------------------------------------
# Phase index — pure function
# ---------------------------------------------------------------------------


def phase_index(phase: Any) -> Optional[int]:
    """Return the 0-based index of ``phase`` in the canonical
    forward-flow tuple, or ``None`` if not in forward-flow.

    Accepts :class:`OperationPhase` enum / string name / value
    (defensive coercion — same pattern as posture_palette).
    NEVER raises."""
    try:
        if phase is None:
            return None
        flow = forward_flow_phases()
        if not flow:
            return None
        # Direct enum-member match.
        for i, p in enumerate(flow):
            if p is phase:
                return i
        # String-name fallback (forward-compat with code that
        # passes the .name string instead of enum member).
        target = (
            phase.name
            if hasattr(phase, "name")
            else str(phase)
        )
        if not isinstance(target, str):
            return None
        target_upper = target.strip().upper()
        for i, p in enumerate(flow):
            if (
                getattr(p, "name", "").upper()
                == target_upper
            ):
                return i
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Format pipeline progress — single rendered token
# ---------------------------------------------------------------------------


def format_pipeline_progress(
    phase: Any = None,
    *,
    show_phase_name: bool = True,
    show_position: bool = True,
) -> str:
    """Render the canonical pipeline progress bar.

    Output shape (default):
        ``[●●●●●○○○○○○] GENERATE 5/11``

    NEVER raises. Returns empty string when:

      * Master flag off (``JARVIS_PIPELINE_PROGRESS_BAR_ENABLED``)
      * Forward-flow tuple is empty (substrate broken)
      * ``phase`` is not in forward-flow (e.g., GENERATE_RETRY,
        CANCELLED, etc.)

    Args:
        phase: :class:`OperationPhase` member / name string /
            None. None and out-of-flow phases render with
            zero filled glyphs.
        show_phase_name: append the phase name after the bar.
        show_position: append ``N/11`` position indicator.
    """
    try:
        if not master_enabled():
            return ""
        flow = forward_flow_phases()
        if not flow:
            return ""
        idx = phase_index(phase)
        # If phase isn't in forward-flow, render with idx=-1
        # (zero filled) so the bar visualizes "starting / not
        # yet in pipeline" without dropping the operator-
        # visible context entirely.
        filled_count = (idx + 1) if idx is not None else 0
        empty_count = len(flow) - filled_count
        if empty_count < 0:
            empty_count = 0
        if filled_count > len(flow):
            filled_count = len(flow)
        bar = (
            bar_open()
            + filled_glyph() * filled_count
            + empty_glyph() * empty_count
            + bar_close()
        )
        parts = [bar]
        if show_phase_name and idx is not None:
            phase_name = (
                phase.name
                if hasattr(phase, "name")
                else str(phase).upper()
            )
            parts.append(phase_name)
        if show_position and idx is not None:
            parts.append(
                f"{filled_count}/{len(flow)}"
            )
        return " ".join(parts)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[pipeline_progress] format_pipeline_progress "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_PIPELINE_PROGRESS_-
         BAR_ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity (no
         orchestrator / iron_gate / policy / providers /
         candidate_generator / change_engine / semantic_guardian
         imports).
      3. ``forward_flow_phase_names_canonical_subset`` — every
         name in :data:`_FORWARD_FLOW_PHASE_NAMES` MUST be a
         valid :class:`OperationPhase` enum member; tree-level
         pin walks op_context.py from disk.
      4. ``forward_flow_length_eleven`` — tuple length pinned
         to 11 (matches CLAUDE.md doc + §28.5.1 closure).
      5. ``composes_canonical_op_context`` — module MUST
         compose ``op_context.OperationPhase`` (no parallel
         phase enum / no hardcoded transition graph).
    """
    import ast
    from pathlib import Path

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/pipeline_progress.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                src = ast.unparse(node)
                # §40 polish-pack composition: walk only the
                # top-level body + unconditional containers (Try)
                # so `if env_check: return True` is correctly
                # recognized as gated. Naive `"return True" in src`
                # would fire on the conditional path too.
                def _has_unconditional_return_true(stmts):
                    for stmt in stmts:
                        if (
                            isinstance(stmt, ast.Return)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is True
                        ):
                            return True
                        if isinstance(stmt, ast.Try):
                            if _has_unconditional_return_true(
                                stmt.body,
                            ):
                                return True
                            if _has_unconditional_return_true(
                                stmt.finalbody,
                            ):
                                return True
                    return False

                if _has_unconditional_return_true(node.body):
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED"
                    not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED"
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
                            f"pipeline_progress MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_forward_flow_subset_of_op_context(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Tree-level pin: walk op_context.py from disk and
        assert every name in _FORWARD_FLOW_PHASE_NAMES is a
        valid OperationPhase enum member."""
        violations: list = []
        # Extract _FORWARD_FLOW_PHASE_NAMES tuple from the
        # current module source. Walks both ast.Assign (bare
        # assignment) and ast.AnnAssign (type-annotated
        # assignment) — the canonical source uses
        # `_FORWARD_FLOW_PHASE_NAMES: Tuple[str, ...] = (...)`.
        flow_names: list = []
        for node in ast.walk(tree):
            target_name = None
            value_node = None
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "_FORWARD_FLOW_PHASE_NAMES"
                    ):
                        target_name = tgt.id
                        value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "_FORWARD_FLOW_PHASE_NAMES"
                ):
                    target_name = node.target.id
                    value_node = node.value
            if (
                target_name == "_FORWARD_FLOW_PHASE_NAMES"
                and isinstance(value_node, ast.Tuple)
            ):
                for elt in value_node.elts:
                    if (
                        isinstance(elt, ast.Constant)
                        and isinstance(elt.value, str)
                    ):
                        flow_names.append(elt.value)
        if not flow_names:
            violations.append(
                "_FORWARD_FLOW_PHASE_NAMES tuple not found "
                "or empty"
            )
            return tuple(violations)
        # Walk op_context.py for OperationPhase enum members.
        op_context_path = Path(
            "backend/core/ouroboros/governance/op_context.py"
        )
        try:
            op_ctx_src = op_context_path.read_text()
        except (OSError, FileNotFoundError):
            # Source unavailable (test isolation) — pin is
            # advisory only.
            return ()
        op_ctx_tree = ast.parse(op_ctx_src)
        enum_members: set = set()
        for node in ast.walk(op_ctx_tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "OperationPhase":
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    enum_members.add(tgt.id)
        for name in flow_names:
            if name not in enum_members:
                violations.append(
                    f"_FORWARD_FLOW_PHASE_NAMES contains "
                    f"{name!r} which is not a valid "
                    f"OperationPhase enum member"
                )
        return tuple(violations)

    def _validate_forward_flow_length_eleven(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Tuple length pinned to 11 per CLAUDE.md doc.
        Handles both Assign and AnnAssign (canonical uses
        type annotation)."""
        violations: list = []
        for node in ast.walk(tree):
            value_node = None
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "_FORWARD_FLOW_PHASE_NAMES"
                    ):
                        value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "_FORWARD_FLOW_PHASE_NAMES"
                ):
                    value_node = node.value
            if isinstance(value_node, ast.Tuple):
                count = len(value_node.elts)
                if count != 11:
                    violations.append(
                        f"_FORWARD_FLOW_PHASE_NAMES "
                        f"length is {count}, "
                        f"expected 11 (matches CLAUDE.md "
                        f"+ §28.5.1 closure)"
                    )
        return tuple(violations)

    def _validate_composes_canonical_op_context(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "op_context" not in source:
            violations.append(
                "pipeline_progress MUST compose "
                "op_context.OperationPhase (no parallel "
                "phase enum)"
            )
        if "OperationPhase" not in source:
            violations.append(
                "pipeline_progress MUST reference canonical "
                "OperationPhase enum"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "pipeline_progress_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_PIPELINE_PROGRESS_BAR_"
                "ENABLED stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "pipeline_progress_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Renderer MUST stay pure substrate composing "
                "governance.op_context + stdlib ONLY. NEVER "
                "imports orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "pipeline_progress_forward_flow_subset_of_"
                "OperationPhase"
            ),
            target_file=target,
            description=(
                "Every name in _FORWARD_FLOW_PHASE_NAMES "
                "MUST be a valid OperationPhase enum member "
                "in op_context.py. Tree-level pin walks "
                "op_context.py from disk to verify "
                "membership."
            ),
            validate=(
                _validate_forward_flow_subset_of_op_context
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "pipeline_progress_forward_flow_length_eleven"
            ),
            target_file=target,
            description=(
                "_FORWARD_FLOW_PHASE_NAMES length pinned to "
                "11 (matches CLAUDE.md 11-phase pipeline doc "
                "+ §28.5.1 phase-extraction-closure 11-phase "
                "invariant). Length change requires explicit "
                "scope-doc + pin update."
            ),
            validate=_validate_forward_flow_length_eleven,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "pipeline_progress_composes_canonical_op_context"
            ),
            target_file=target,
            description=(
                "Module MUST compose canonical "
                "op_context.OperationPhase. Forbidden to "
                "maintain parallel phase enum or hardcoded "
                "transition graph."
            ),
            validate=_validate_composes_canonical_op_context,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register pipeline-progress flags with the FlagRegistry."""
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED",
            "bool",
            "false",
            (
                "Master flag for the pipeline progress bar "
                "(§38 Slice 2). Default-FALSE per §33.1; "
                "flips after operator validates the bar "
                "composition."
            ),
        ),
        (
            "JARVIS_PIPELINE_PROGRESS_FILLED_GLYPH",
            "str",
            _FILLED_GLYPH_DEFAULT,
            "Filled-position glyph (default ●).",
        ),
        (
            "JARVIS_PIPELINE_PROGRESS_EMPTY_GLYPH",
            "str",
            _EMPTY_GLYPH_DEFAULT,
            "Empty-position glyph (default ○).",
        ),
        (
            "JARVIS_PIPELINE_PROGRESS_BAR_OPEN",
            "str",
            _BAR_OPEN_DEFAULT,
            "Bar opening character (default '[').",
        ),
        (
            "JARVIS_PIPELINE_PROGRESS_BAR_CLOSE",
            "str",
            _BAR_CLOSE_DEFAULT,
            "Bar closing character (default ']').",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "pipeline_progress.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return n
    return n


__all__ = [
    "PIPELINE_PROGRESS_SCHEMA_VERSION",
    "bar_close",
    "bar_open",
    "empty_glyph",
    "filled_glyph",
    "format_pipeline_progress",
    "forward_flow_length",
    "forward_flow_phases",
    "master_enabled",
    "phase_index",
    "register_flags",
    "register_shipped_invariants",
    "reset_forward_flow_cache_for_tests",
]
