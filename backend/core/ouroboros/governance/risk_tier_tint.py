"""§39 Tier-1 #2 — Risk-tier ambient color tint
(PRD v2.70 to v2.71, 2026-05-08).

Always-visible cage tint. Wraps any text in Rich markup
colored by the current risk-tier-floor stance:

  * GREEN  — SAFE_AUTO  cage (no friction)
  * YELLOW — NOTIFY_APPLY (transient diff overlay)
  * ORANGE — APPROVAL_REQUIRED (human gate)
  * RED    — BLOCKED / governor emergency brake

Authority asymmetry: this module has ZERO authority. It
NEVER mutates risk-tier state, NEVER calls orchestrator,
NEVER changes the cage. It only OBSERVES the canonical
:class:`RiskTierLight` (via §38.11-A
``organism_status.read_current_risk_light_safe``) and
RENDERS the appropriate Rich markup.

§38.11.5a.5 single-canonical-name discipline honored:
ZERO new taxonomy — reuses canonical
:class:`organism_status.RiskTierLight` (4 values) +
canonical :func:`organism_status.rich_color_for_light`
accessor (extension landed alongside this slice).

§33 patterns invoked:

  * §33.1 graduation contract — master flag default-FALSE.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


RISK_TIER_TINT_SCHEMA_VERSION: str = "risk_tier_tint.1"


_ENV_MASTER = "JARVIS_RISK_TIER_TINT_ENABLED"
_ENV_SUB_PROMPT = "JARVIS_RISK_TIER_TINT_PROMPT_ENABLED"
_ENV_SUB_OUTPUT = "JARVIS_RISK_TIER_TINT_OUTPUT_ENABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def prompt_tint_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PROMPT, default=True)


def output_tint_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_OUTPUT, default=True)


# ===========================================================================
# Canonical lookup helpers — NEVER raise
# ===========================================================================


def current_tint_color() -> Optional[str]:
    """Return the canonical Rich color name for the current
    risk-tier light, or ``None`` if substrate unavailable.

    Composes:
      * ``organism_status.read_current_risk_light_safe``
        (canonical risk-tier-floor reader)
      * ``organism_status.rich_color_for_light`` (canonical
        light → Rich color accessor)
    """
    try:
        from backend.core.ouroboros.governance.organism_status import (  # noqa: E501
            read_current_risk_light_safe, rich_color_for_light,
        )
        light = read_current_risk_light_safe()
        return rich_color_for_light(light)
    except Exception:  # noqa: BLE001
        logger.debug(
            "risk_tier_tint: current_tint_color failed",
            exc_info=True,
        )
        return None


# ===========================================================================
# Render helpers — pure
# ===========================================================================


def apply_ambient_tint(
    text: str,
    *,
    color: Optional[str] = None,
    style: str = "",
) -> str:
    """Wrap ``text`` in Rich markup tinted by the current
    risk-tier light.

    Empty or master-off → returns ``text`` unchanged (NEVER
    raises; NEVER strips existing Rich markup).

    ``color`` override: pass an explicit Rich color to skip
    the canonical lookup (used by tests / specific UIs).

    ``style`` extras: e.g., ``"bold"``, ``"italic"``,
    ``"dim"`` — appended to the color in the Rich tag.
    """
    if not master_enabled():
        return text
    if text is None:
        return ""
    try:
        c = color if color else current_tint_color()
        if not c:
            return text
        tag_open = c if not style else f"{c} {style}"
        return f"[{tag_open}]{text}[/]"
    except Exception:  # noqa: BLE001
        return text


def tint_prompt_marker(marker: str = "▸") -> str:
    """Render a small prompt-side marker tinted by current
    risk light. Default glyph is a single right-pointing
    triangle. Empty when sub-flag off.

    Use case: REPL prompt prefix that reflects the cage
    stance at glance.
    """
    if not prompt_tint_enabled():
        return ""
    return apply_ambient_tint(marker)


def tint_output(text: str, *, style: str = "") -> str:
    """Tint operator-facing output. Pass-through when
    output sub-flag off (so output renders cleanly without
    the ambient tint)."""
    if not output_tint_enabled():
        return text
    return apply_ambient_tint(text, style=style)


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-1 #2 risk-tier ambient color tint "
            "master switch (graduation contract per §33.1; "
            "default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_PROMPT, "bool",
            "Enable prompt-side tint marker. Default TRUE "
            "when master on.",
            "true",
        ),
        (
            _ENV_SUB_OUTPUT, "bool",
            "Enable operator-output ambient tint. Default "
            "TRUE when master on.",
            "true",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "risk_tier_tint.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master_default_false(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                ok = False
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
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name="section_39_tier1_2_master_default_false",
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_tier_tint.py"
        ),
        validate=_master_default_false,
    ))

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod} "
                        "(must compose via canonical "
                        "organism_status façade only)"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name="section_39_tier1_2_authority_asymmetry",
        description=(
            "Substrate purity — module must compose canonical "
            "organism_status façade; never reach into "
            "risk_tier_floor or orchestrator directly."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_tier_tint.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _composes_canonical_organism_status(
        tree: ast.AST, src: str,
    ):
        """Bytes-pin: must lazy-import the canonical
        organism_status accessor — no parallel risk-light
        reading."""
        if "organism_status" not in src:
            return [
                "must lazy-import organism_status (canonical "
                "RiskTierLight + rich_color_for_light source)"
            ]
        if "rich_color_for_light" not in src:
            return [
                "must reference rich_color_for_light "
                "(canonical accessor — added in §39 Tier-1 "
                "alongside this slice)"
            ]
        if "read_current_risk_light_safe" not in src:
            return [
                "must reference read_current_risk_light_safe "
                "(canonical risk-light reader)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_2_composes_canonical_"
            "organism_status"
        ),
        description=(
            "Tint substrate composes canonical "
            "organism_status accessors only — no parallel "
            "risk-light → Rich-color mapping."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_tier_tint.py"
        ),
        validate=_composes_canonical_organism_status,
    ))

    return pins


__all__ = [
    "RISK_TIER_TINT_SCHEMA_VERSION",
    "master_enabled",
    "prompt_tint_enabled",
    "output_tint_enabled",
    "current_tint_color",
    "apply_ambient_tint",
    "tint_prompt_marker",
    "tint_output",
    "register_flags",
    "register_shipped_invariants",
]
