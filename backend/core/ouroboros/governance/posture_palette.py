"""Posture mood-ring palette (PRD §38 Slice 1, 2026-05-07).

Closes the operator-flagged "posture is buried in the UI" gap
from the §38 brutal review: posture is O+V's most unique signal
(EXPLORE / CONSOLIDATE / HARDEN / MAINTAIN — autonomous strategic
stance), and CC structurally cannot replicate it. Pre-Slice-1
posture surfaced only via `/posture` REPL and SSE; the status
line had no posture badge at all.

Slice 1 ships:

  * Pure-function ``palette_for_posture(posture)`` → Rich color
    name. Closed mapping: green=EXPLORE, blue=CONSOLIDATE,
    yellow=HARDEN, dim=MAINTAIN, dim=None (unknown).
  * ``read_current_posture_safe()`` — defensive accessor that
    composes ``posture_repl._default_store`` (the canonical
    store-injection seam). NEVER raises. Returns
    ``Optional[Posture]``.
  * ``format_posture_badge()`` — composes both above into a
    single styled token suitable for prepending to the
    status-line plain output.

Composes existing canonical sources (operator binding
"fully leverage existing files"):

  * :mod:`governance.posture` — ``Posture`` 4-value enum
  * :mod:`governance.posture_store` — ``PostureStore.load_current``
  * :mod:`governance.posture_repl` — ``_default_store`` injection
    seam

NEVER reimplements posture state or readings — pure render +
canonical color mapping.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — no I/O beyond the lazy-import of the
     canonical store. NEVER raises.
  2. **Authority asymmetry** — imports stdlib + governance.posture
     family ONLY. NEVER imports orchestrator / iron_gate / policy
     / providers / candidate_generator / change_engine /
     semantic_guardian.
  3. **Closed color mapping** — ``_POSTURE_COLOR_TABLE`` is an
     exhaustive 4-key + None mapping. AST-pinned: every
     :class:`Posture` enum value MUST appear as a key.
  4. **Composes canonical store** — accessor MUST lazy-import
     ``posture_repl._default_store``. AST-pinned: forbidden
     to maintain parallel posture state.
  5. **Master flag default-FALSE** per §33.1.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


POSTURE_PALETTE_SCHEMA_VERSION: str = "posture_palette.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_POSTURE_MOOD_RING_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off,
    :func:`format_posture_badge` returns empty string and the
    status-line lead-position badge is not rendered. Operator
    flips after observing the badge composition."""
    if os.environ.get( "JARVIS_POSTURE_MOOD_RING_ENABLED", "", ).strip().lower() in _TRUTHY:
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
        return is_substrate_in_active_pack('posture_palette')
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Canonical color mapping — closed table, AST-pinned exhaustive
# ---------------------------------------------------------------------------
#
# Mapping rationale (operator binding "no hardcoding" — table is
# the single source of truth; every consumer composes via
# :func:`palette_for_posture`, never inlines color strings):
#
#   * EXPLORE     → green   (intrinsic motivation; growth posture)
#   * CONSOLIDATE → blue    (refining substrate; depth posture)
#   * HARDEN      → yellow  (caution; throttling activity)
#   * MAINTAIN    → dim     (steady-state; minimum-noise)
#   * None / unknown → dim  (defensive — unknown posture should
#                            not call attention to itself)


# Lazy-evaluated to avoid import-time enum dependency.
_POSTURE_COLOR_TABLE: Optional[Dict[Optional[str], str]] = None


def _build_color_table() -> Dict[Optional[str], str]:
    """Build the canonical posture-value → Rich color mapping.
    Lazy because importing :class:`Posture` at module load
    creates a startup dependency cycle in some test paths."""
    return {
        "EXPLORE": "green",
        "CONSOLIDATE": "blue",
        "HARDEN": "yellow",
        "MAINTAIN": "bright_black",  # Rich's "dim" equivalent
        None: "bright_black",  # unknown / None → dim
    }


def _color_table() -> Dict[Optional[str], str]:
    global _POSTURE_COLOR_TABLE
    if _POSTURE_COLOR_TABLE is None:
        _POSTURE_COLOR_TABLE = _build_color_table()
    return _POSTURE_COLOR_TABLE


def palette_for_posture(posture: Any) -> str:
    """Map a :class:`Posture` (or its string value, or None) to
    a Rich color name. Pure function. NEVER raises.

    Defensive on every input shape — non-Posture / non-string /
    None all degrade to the canonical ``"bright_black"`` (dim)
    color. Caller composes the result into Rich markup like
    ``f"[{color}]🐍 {label}[/{color}]"``."""
    try:
        if posture is None:
            return _color_table()[None]
        # Accept Posture enum, plain string, or anything with .value
        value = (
            posture.value
            if hasattr(posture, "value")
            else str(posture)
        )
        if not isinstance(value, str):
            return _color_table()[None]
        return _color_table().get(
            value.strip().upper(),
            _color_table()[None],
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[posture_palette] palette_for_posture "
            "swallowed: %s",
            type(exc).__name__,
        )
        return "bright_black"


# ---------------------------------------------------------------------------
# Defensive store accessor — composes canonical posture_store
# via posture_repl._default_store injection seam
# ---------------------------------------------------------------------------


def read_current_posture_safe() -> Optional[Any]:
    """Return the current :class:`Posture` if the store is
    wired and a reading exists, else ``None``.

    Composes canonical sources via lazy-import:

      1. ``posture_repl._default_store`` (boot-wired singleton —
         the canonical store-injection seam)
      2. ``PostureStore.load_current()`` → ``PostureReading``
      3. ``PostureReading.posture`` → ``Posture`` enum

    NEVER raises. Returns ``None`` on:

      * Store not wired (boot incomplete / test harness)
      * No current reading on disk
      * Malformed / schema-mismatched reading
      * Any other failure path
    """
    try:
        from backend.core.ouroboros.governance import (
            posture_repl as _repl,
        )
        store = getattr(_repl, "_default_store", None)
        if store is None:
            return None
        reading = store.load_current()
        if reading is None:
            return None
        return getattr(reading, "posture", None)
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Format posture badge — single rendered token for status line
# ---------------------------------------------------------------------------


def format_posture_badge(
    *,
    plain: bool = True,
) -> str:
    """Render the posture badge as a single token suitable for
    status-line lead-position composition.

    NEVER raises. Returns empty string when:

      * Master flag off (``JARVIS_POSTURE_MOOD_RING_ENABLED``)
      * No current posture reading available

    ``plain=True`` (default) — returns plain text like
    ``"🐍 EXPLORE"`` (no Rich markup). Caller adds color via
    Rich pipeline.

    ``plain=False`` — returns Rich-markup-wrapped form like
    ``"[green]🐍 EXPLORE[/green]"`` ready for direct emit into
    a Rich console.
    """
    try:
        if not master_enabled():
            return ""
        posture = read_current_posture_safe()
        if posture is None:
            return ""
        # Extract posture string value defensively.
        value = (
            posture.value
            if hasattr(posture, "value")
            else str(posture)
        )
        if not isinstance(value, str) or not value.strip():
            return ""
        label = value.strip().upper()
        text = f"🐍 {label}"
        if plain:
            return text
        color = palette_for_posture(posture)
        return f"[{color}]{text}[/{color}]"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[posture_palette] format_posture_badge "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 4 pins:

      1. ``master_default_false`` — JARVIS_POSTURE_MOOD_RING_-
         ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity (no
         orchestrator / iron_gate / policy / providers /
         candidate_generator / change_engine / semantic_guardian
         imports).
      3. ``composes_canonical_posture_store`` — accessor MUST
         lazy-import ``posture_repl._default_store`` (canonical
         injection seam); forbidden to construct PostureStore
         directly or maintain parallel posture state.
      4. ``color_table_exhaustive`` — ``_build_color_table()``
         MUST contain entries for all 4 :class:`Posture` enum
         values (EXPLORE / CONSOLIDATE / HARDEN / MAINTAIN) +
         ``None`` for unknown — operator binding "no hardcoding"
         enforced via exhaustive-mapping check.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/posture_palette.py"
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
                    "JARVIS_POSTURE_MOOD_RING_ENABLED"
                    not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_POSTURE_MOOD_RING_ENABLED"
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
                            f"posture_palette MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_store(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "posture_repl" not in source:
            violations.append(
                "posture_palette MUST compose canonical "
                "posture_repl._default_store (no parallel "
                "posture state)"
            )
        if "_default_store" not in source:
            violations.append(
                "accessor MUST reference _default_store "
                "(canonical injection seam)"
            )
        # Forbid direct PostureStore() construction.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "PostureStore"
                ):
                    violations.append(
                        "posture_palette MUST NOT construct "
                        "PostureStore directly — compose "
                        "posture_repl._default_store instead"
                    )
        return tuple(violations)

    def _validate_color_table_exhaustive(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Walk ``_build_color_table()`` and assert it contains
        keys for all 4 Posture enum values + None."""
        violations: list = []
        required_keys = {
            "EXPLORE",
            "CONSOLIDATE",
            "HARDEN",
            "MAINTAIN",
        }  # plus None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_build_color_table"
            ):
                seen_string_keys: set = set()
                seen_none_key = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        for k in sub.keys:
                            if isinstance(k, ast.Constant):
                                if k.value is None:
                                    seen_none_key = True
                                elif isinstance(k.value, str):
                                    seen_string_keys.add(
                                        k.value,
                                    )
                missing_strings = required_keys - seen_string_keys
                if missing_strings:
                    violations.append(
                        f"_build_color_table missing keys: "
                        f"{sorted(missing_strings)}"
                    )
                if not seen_none_key:
                    violations.append(
                        "_build_color_table MUST include "
                        "None key for unknown posture"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "posture_palette_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_POSTURE_MOOD_RING_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "posture_palette_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Palette MUST stay pure substrate composing "
                "governance.posture family + stdlib ONLY. "
                "NEVER imports orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "posture_palette_composes_canonical_store"
            ),
            target_file=target,
            description=(
                "Accessor MUST compose canonical "
                "posture_repl._default_store injection seam. "
                "Forbidden to construct PostureStore directly "
                "or maintain parallel posture state."
            ),
            validate=_validate_composes_canonical_store,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "posture_palette_color_table_exhaustive"
            ),
            target_file=target,
            description=(
                "_build_color_table MUST contain keys for "
                "all 4 Posture enum values (EXPLORE / "
                "CONSOLIDATE / HARDEN / MAINTAIN) + None for "
                "unknown — operator binding 'no hardcoding' "
                "enforced via exhaustive-mapping check."
            ),
            validate=_validate_color_table_exhaustive,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register posture-palette flags with the FlagRegistry."""
    if registry is None:
        return 0
    try:
        registry.register(
            name="JARVIS_POSTURE_MOOD_RING_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master flag for the posture mood-ring badge "
                "(§38 Slice 1). Default-FALSE per §33.1; "
                "flips after operator validates the badge "
                "composition."
            ),
            category="ux",
            posture_relevance="CRITICAL",
            source_file=(
                "backend/core/ouroboros/governance/"
                "posture_palette.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


__all__ = [
    "POSTURE_PALETTE_SCHEMA_VERSION",
    "format_posture_badge",
    "master_enabled",
    "palette_for_posture",
    "read_current_posture_safe",
    "register_flags",
    "register_shipped_invariants",
]
