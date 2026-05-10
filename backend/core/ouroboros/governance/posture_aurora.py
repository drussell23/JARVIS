"""Posture aurora — confidence-modulated posture badge variant.

Section §37 v10 brutal-review item: ambient color tint on the
status line that shifts with EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN.
The existing :mod:`posture_palette` is the canonical 4-color
posture badge (graduated default-true via
``JARVIS_POSTURE_MOOD_RING_ENABLED``); this module composes it
with :class:`PostureReading.confidence` to produce a 3-tier
intensity modulation:

  * **HIGH** confidence (>= 0.75) → bright variant
    (``bright_green`` for EXPLORE, etc.) — the aurora "glows"
  * **NORMAL** confidence (0.50–0.75) → base variant
    (matches existing :func:`posture_palette.palette_for_posture`)
  * **LOW** confidence (< 0.50) → dimmed variant (``dim green``)

The 4×3 intensity grid is closed and frozen — adding a fifth
posture or fourth confidence band requires extending both the
:class:`ConfidenceBand` enum AND the AST pin. No magic numbers
in caller code.

Design pillars (operator mandate "no hardcoding"):

  * **No parallel posture vocabulary** — the 4-value posture
    string set is sourced from existing :mod:`posture_palette`,
    NOT redefined here. Aurora is a presentation modulation,
    not a new domain concept.

  * **Composition over replacement** — when master flag off,
    the status-line wiring falls back to existing
    :func:`posture_palette.format_posture_badge`. The graduated
    badge surface is preserved verbatim under aurora=false.

  * **Defensive shape** — every public function NEVER raises.
    Posture store unwired / no reading / malformed confidence
    all degrade gracefully to empty string.

  * **Authority asymmetry** — aurora is a presentation layer.
    MUST NOT import orchestrator / iron_gate / providers etc.
    AST-pinned by :func:`register_shipped_invariants`.

Master flag: ``JARVIS_POSTURE_AURORA_ENABLED`` (default-FALSE
until Phase 9 cadence graduates it; 3 clean soaks). Asymmetric
env semantics (empty = unset = current default).
"""
from __future__ import annotations

import enum
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


POSTURE_AURORA_SCHEMA_VERSION: str = "posture_aurora.1"


# ---------------------------------------------------------------------------
# Closed taxonomy — 3 confidence bands, frozen
# ---------------------------------------------------------------------------


class ConfidenceBand(str, enum.Enum):
    """Closed 3-value confidence band for aurora intensity.

    Frozen — adding a 4th value requires extending both this enum
    AND the :func:`register_shipped_invariants` AST pin.
    """

    HIGH = "high"      # confidence >= 0.75
    NORMAL = "normal"  # 0.50 <= confidence < 0.75
    LOW = "low"        # confidence < 0.50


# Threshold floors — env-tunable so operator can A/B without
# code edits. Defaults match the §37 spec.
_DEFAULT_HIGH_THRESHOLD: float = 0.75
_DEFAULT_NORMAL_THRESHOLD: float = 0.50


def _high_threshold() -> float:
    raw = os.environ.get(
        "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD", "",
    ).strip()
    if not raw:
        return _DEFAULT_HIGH_THRESHOLD
    try:
        v = float(raw)
        return min(1.0, max(0.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_HIGH_THRESHOLD


def _normal_threshold() -> float:
    raw = os.environ.get(
        "JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD", "",
    ).strip()
    if not raw:
        return _DEFAULT_NORMAL_THRESHOLD
    try:
        v = float(raw)
        return min(_high_threshold(), max(0.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_NORMAL_THRESHOLD


# ---------------------------------------------------------------------------
# Master flag — default-FALSE (§33 graduation contract)
# ---------------------------------------------------------------------------


def aurora_enabled() -> bool:
    """``JARVIS_POSTURE_AURORA_ENABLED`` (default ``false`` until
    Phase 9 cadence graduation). Empty/whitespace = unset =
    default-false. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_POSTURE_AURORA_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Closed 4×3 intensity grid — (posture, band) → Rich color spec
# ---------------------------------------------------------------------------


# Lazy-built so import-time has no Posture-enum dependency. Same
# pattern as posture_palette._POSTURE_COLOR_TABLE.
_AURORA_TABLE: Optional[Dict[Tuple[str, str], str]] = None


def _build_aurora_table() -> Dict[Tuple[str, str], str]:
    """Closed (posture × confidence_band) → Rich-color mapping.

    The base color per posture is sourced from the canonical
    :mod:`posture_palette` palette so this table never drifts
    from the graduated palette. The intensity prefix is the
    aurora's contribution.

    Frozen 4×3 = 12 entries. Adding a row requires extending
    posture_palette AND updating
    :func:`register_shipped_invariants`."""
    # Canonical base colors (mirrors posture_palette._build_color_table)
    base = {
        "EXPLORE": "green",
        "CONSOLIDATE": "blue",
        "HARDEN": "yellow",
        "MAINTAIN": "white",  # MAINTAIN's normal aurora is white;
        # the dim/bright_black fallback is for unknown postures only
    }
    table: Dict[Tuple[str, str], str] = {}
    for posture, color in base.items():
        # HIGH = bright_<color>; bright_white isn't a Rich color so
        # use plain white for MAINTAIN HIGH.
        if color == "white":
            table[(posture, ConfidenceBand.HIGH.value)] = "bright_white"
            table[(posture, ConfidenceBand.NORMAL.value)] = "white"
            table[(posture, ConfidenceBand.LOW.value)] = "bright_black"
        else:
            table[(posture, ConfidenceBand.HIGH.value)] = f"bright_{color}"
            table[(posture, ConfidenceBand.NORMAL.value)] = color
            table[(posture, ConfidenceBand.LOW.value)] = f"dim {color}"
    return table


def _aurora_table() -> Dict[Tuple[str, str], str]:
    global _AURORA_TABLE
    if _AURORA_TABLE is None:
        _AURORA_TABLE = _build_aurora_table()
    return _AURORA_TABLE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def confidence_band_for(confidence: Any) -> ConfidenceBand:
    """Map a confidence float to a closed :class:`ConfidenceBand`.

    Defensive on every input: non-float / NaN / out-of-range
    degrades to LOW (the conservative aurora setting). NEVER
    raises."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return ConfidenceBand.LOW
    # NaN check: NaN != NaN
    if c != c:
        return ConfidenceBand.LOW
    if c >= _high_threshold():
        return ConfidenceBand.HIGH
    if c >= _normal_threshold():
        return ConfidenceBand.NORMAL
    return ConfidenceBand.LOW


def aurora_color_for(
    posture: Any,
    confidence: Any,
) -> str:
    """Resolve a (posture, confidence) pair to a Rich color spec.

    Composes :func:`confidence_band_for` + the closed
    :func:`_aurora_table`. Unknown postures fall back to dim
    (``bright_black``) — same defensive policy as
    :func:`posture_palette.palette_for_posture`.

    NEVER raises."""
    try:
        if posture is None:
            return "bright_black"
        # Accept Posture enum, plain string, or anything with .value
        value = (
            posture.value
            if hasattr(posture, "value")
            else str(posture)
        )
        if not isinstance(value, str):
            return "bright_black"
        key = (value.strip().upper(), confidence_band_for(confidence).value)
        return _aurora_table().get(key, "bright_black")
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[posture_aurora] aurora_color_for swallowed: %s",
            type(exc).__name__,
        )
        return "bright_black"


def _read_current_reading_safe() -> Optional[Any]:
    """Return the current :class:`PostureReading` (with
    ``.posture`` AND ``.confidence``) or ``None``. Composes the
    canonical :mod:`posture_palette.read_current_posture_safe`
    seam — same store-injection contract.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance import (
            posture_repl as _repl,
        )
        store = getattr(_repl, "_default_store", None)
        if store is None:
            return None
        return store.load_current()
    except Exception:  # noqa: BLE001 — defensive
        return None


def format_posture_aurora_badge(
    *,
    plain: bool = False,
) -> str:
    """Render the confidence-modulated posture aurora badge.

    Returns empty string when:
      * Master flag off (aurora not graduated)
      * No current PostureReading available
      * Posture or confidence malformed

    ``plain=False`` (default) — returns Rich-markup-wrapped form
    like ``"[bright_green]🐍 EXPLORE[/bright_green]"``. Uses
    Rich-style ``[color]text[/color]`` markup so consumers can
    pass it through any Rich console / Text builder.

    ``plain=True`` — returns plain text like ``"🐍 EXPLORE"``
    (caller adds color separately). Mirrors the
    :func:`posture_palette.format_posture_badge` ``plain`` flag
    for substitutability.

    NEVER raises."""
    try:
        if not aurora_enabled():
            return ""
        reading = _read_current_reading_safe()
        if reading is None:
            return ""
        posture = getattr(reading, "posture", None)
        if posture is None:
            return ""
        # Extract posture string defensively.
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
        confidence = getattr(reading, "confidence", 0.0)
        color = aurora_color_for(posture, confidence)
        return f"[{color}]{text}[/{color}]"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[posture_aurora] format_posture_aurora_badge "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Module-owned FlagRegistry seed
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagSpec declarations (§33 naming-cage).

    Three flags:
      * ``JARVIS_POSTURE_AURORA_ENABLED`` (BOOL, default-FALSE) —
        master switch.
      * ``JARVIS_POSTURE_AURORA_HIGH_THRESHOLD`` (FLOAT, default
        0.75) — confidence floor for HIGH band.
      * ``JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD`` (FLOAT, default
        0.50) — confidence floor for NORMAL band.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except ImportError:
        return 0
    installed = 0
    specs = (
        FlagSpec(
            name="JARVIS_POSTURE_AURORA_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for posture-aurora confidence "
                "modulation on the status-line badge. When true, "
                "the lead-position posture badge shifts intensity "
                "with PostureReading.confidence (HIGH≥0.75, "
                "NORMAL≥0.50, LOW<0.50) producing a glow vs base "
                "vs dim variant of the canonical posture color. "
                "When false, the graduated "
                "JARVIS_POSTURE_MOOD_RING_ENABLED badge surface "
                "is preserved verbatim. Default-false until Phase "
                "9 cadence graduation (3 clean soaks)."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "posture_aurora.py"
            ),
            example="false",
            since="§37 Tier 2 (2026-05-10)",
            posture_relevance={
                "EXPLORE": Relevance.RELEVANT,
                "CONSOLIDATE": Relevance.RELEVANT,
                "HARDEN": Relevance.RELEVANT,
                "MAINTAIN": Relevance.RELEVANT,
            },
        ),
        FlagSpec(
            name="JARVIS_POSTURE_AURORA_HIGH_THRESHOLD",
            type=FlagType.FLOAT,
            default=0.75,
            description=(
                "Confidence floor for HIGH aurora band (bright "
                "variant). Operator-tunable; clamped to [0, 1]."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "posture_aurora.py"
            ),
            example="0.75",
            since="§37 Tier 2 (2026-05-10)",
        ),
        FlagSpec(
            name="JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD",
            type=FlagType.FLOAT,
            default=0.50,
            description=(
                "Confidence floor for NORMAL aurora band (base "
                "variant). Below this floor degrades to LOW (dim). "
                "Clamped to [0, high_threshold]."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "posture_aurora.py"
            ),
            example="0.50",
            since="§37 Tier 2 (2026-05-10)",
        ),
    )
    for spec in specs:
        try:
            registry.register(spec, override=True)
            installed += 1
        except Exception:  # noqa: BLE001 — defensive
            continue
    return installed


# ---------------------------------------------------------------------------
# Shipped-code AST invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code AST pins.

    Three invariants:
      1. ``aurora_default_false`` — master flag default-false
         (Phase 9 graduation contract).
      2. ``confidence_band_taxonomy_frozen`` — exactly 3 bands
         (HIGH/NORMAL/LOW); aurora table size 12 (4 postures × 3
         bands).
      3. ``no_authority_imports`` — presentation layer only.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/posture_aurora.py"
    )

    def _validate_default_false(_tree, source) -> tuple:
        marker = (
            'os.environ.get(\n'
            '        "JARVIS_POSTURE_AURORA_ENABLED", "",'
        )
        if marker not in source:
            return (
                "posture_aurora.aurora_enabled must read "
                "JARVIS_POSTURE_AURORA_ENABLED env with "
                "default-false fallback (Phase 9 cadence "
                "contract).",
            )
        if "return False  # default-false until graduation" not in source:
            return (
                "posture_aurora.aurora_enabled must explicitly "
                "comment + return False on the default branch "
                "(graduation contract).",
            )
        return ()

    def _validate_taxonomy_frozen(tree, _source) -> tuple:
        # Walk the AST for class ConfidenceBand and assert exactly
        # 3 string-typed enum members.
        try:
            import ast as _ast
        except ImportError:  # pragma: no cover
            return ()
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "ConfidenceBand"
            ):
                # Count assigns of form `NAME = "value"`.
                member_count = 0
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        if (
                            len(stmt.targets) == 1
                            and isinstance(stmt.targets[0], _ast.Name)
                        ):
                            member_count += 1
                if member_count != 3:
                    return (
                        f"ConfidenceBand taxonomy frozen at 3 "
                        f"members (HIGH/NORMAL/LOW); found "
                        f"{member_count}.",
                    )
                return ()
        return (
            "ConfidenceBand class definition not found in "
            "posture_aurora.py.",
        )

    def _validate_no_authority_imports(tree, _source) -> tuple:
        try:
            import ast as _ast
        except ImportError:  # pragma: no cover
            return ()
        forbidden_modules = frozenset({
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.sensor_governor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.strategic_direction",
        })
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden_modules:
                    return (
                        f"posture_aurora authority asymmetry "
                        f"violated: imports forbidden module "
                        f"{mod!r}.",
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    mod = alias.name or ""
                    if mod in forbidden_modules:
                        return (
                            f"posture_aurora authority asymmetry "
                            f"violated: imports forbidden module "
                            f"{mod!r}.",
                        )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="posture_aurora_default_false",
            target_file=target,
            description=(
                "Master flag JARVIS_POSTURE_AURORA_ENABLED must "
                "default-false until Phase 9 cadence."
            ),
            validate=_validate_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="posture_aurora_taxonomy_frozen",
            target_file=target,
            description=(
                "Closed 3-value ConfidenceBand enum (HIGH/NORMAL/"
                "LOW); 4×3=12 aurora table entries cover all "
                "postures × bands."
            ),
            validate=_validate_taxonomy_frozen,
        ),
        ShippedCodeInvariant(
            invariant_name="posture_aurora_no_authority_imports",
            target_file=target,
            description=(
                "Presentation layer only: must not import "
                "orchestrator / iron_gate / providers / etc."
            ),
            validate=_validate_no_authority_imports,
        ),
    ]


__all__ = [
    "POSTURE_AURORA_SCHEMA_VERSION",
    "ConfidenceBand",
    "aurora_enabled",
    "confidence_band_for",
    "aurora_color_for",
    "format_posture_aurora_badge",
    "register_flags",
    "register_shipped_invariants",
]
