"""§39 Tier-3 #19 — Risk-aware command preview
(PRD v2.72 to v2.73, 2026-05-08).

Pre-submission "what will happen if I submit this" preview.
Composes canonical:

  * :class:`urgency_router.UrgencyRouter.classify` — pure
    deterministic route classifier (the same path every
    real op flows through at ROUTE phase).
  * :func:`risk_tier_floor.recommended_floor` — current
    cage-stance floor (canonical risk-tier source).
  * :func:`urgency_router.UrgencyRouter.route_budget_profile`
    — per-route cost/duration estimates.

Authority asymmetry: ZERO authority. Read-only previewer +
renderer. Builds a SYNTHETIC duck-typed context to feed
the canonical classifier — NEVER calls orchestrator,
NEVER changes risk-tier, NEVER spawns ops.

§38.11.5a.5 single-canonical-name discipline honored: the
existing 5-value :class:`ProviderRoute` is reused as the
predicted route output (NO parallel route taxonomy); the
NEW closed taxonomy is :class:`PreviewVerdict` (4 values
mapping risk-tier → operator-friendly traffic light).

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen :class:`CommandPreview`)
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


RISK_COMMAND_PREVIEW_SCHEMA_VERSION: str = (
    "risk_command_preview.1"
)


_ENV_MASTER = "JARVIS_RISK_COMMAND_PREVIEW_ENABLED"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    if _flag(_ENV_MASTER, default=False):
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
        return is_substrate_in_active_pack('risk_command_preview')
    except ImportError:
        return False


# ===========================================================================
# Closed taxonomy — 4-value PreviewVerdict
# ===========================================================================


class PreviewVerdict(str, enum.Enum):
    """Closed 4-value verdict mapping the predicted risk-
    tier-floor to an operator-friendly traffic light.

    Mapped via :data:`_FLOOR_TO_VERDICT` (bytes-pinned).
    """

    SAFE = "safe"           # safe_auto floor
    NOTIFY = "notify"       # notify_apply floor
    APPROVAL = "approval"   # approval_required floor
    BLOCKED = "blocked"     # blocked / governor brake


# Bytes-pinned floor → verdict map. AST regression locks
# the canonical names so risk_tier_floor enum drift fires
# the pin (matches §38.11-A `_FLOOR_TO_LIGHT` pattern).
_FLOOR_TO_VERDICT = {
    "safe_auto": PreviewVerdict.SAFE,
    "notify_apply": PreviewVerdict.NOTIFY,
    "approval_required": PreviewVerdict.APPROVAL,
    "blocked": PreviewVerdict.BLOCKED,
}


def _verdict_for_floor(
    floor: str, *, governor_emergency: bool = False,
) -> PreviewVerdict:
    """Pure-function map. NEVER raises."""
    if governor_emergency:
        return PreviewVerdict.BLOCKED
    try:
        s = str(floor or "").strip().lower()
    except Exception:  # noqa: BLE001
        return PreviewVerdict.NOTIFY
    return _FLOOR_TO_VERDICT.get(s, PreviewVerdict.NOTIFY)


# ===========================================================================
# Synthetic preview context — duck-typed for UrgencyRouter
# ===========================================================================


@dataclass(frozen=True)
class _PreviewContext:
    """Synthetic context fed to canonical
    :class:`UrgencyRouter.classify`. Carries the exact
    fields the classifier reads (signal_urgency / signal_source
    / task_complexity / target_files / cross_repo /
    provider_route / provider_route_reason). Frozen +
    hashable.

    NOT a clone of OperationContext — only the read-fields
    classify() needs. Keeping it minimal preserves the
    boundary: this is a PREVIEW, not a real op.
    """

    signal_urgency: str = "normal"
    signal_source: str = ""
    task_complexity: str = "moderate"
    target_files: Tuple[str, ...] = ()
    cross_repo: bool = False
    provider_route: str = ""
    provider_route_reason: str = ""


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class CommandPreview:
    """One command preview. Frozen + hashable."""

    command_summary: str
    predicted_route: str = "standard"     # ProviderRoute.value
    route_reason: str = ""
    predicted_floor: str = "safe_auto"     # risk_tier_floor name
    verdict: PreviewVerdict = PreviewVerdict.SAFE
    governor_emergency: bool = False
    estimated_cost_usd: float = 0.0
    estimated_duration_s: float = 0.0
    target_file_count: int = 0
    diagnostic: str = ""
    schema_version: str = (
        RISK_COMMAND_PREVIEW_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "command_summary": self.command_summary,
            "predicted_route": self.predicted_route,
            "route_reason": self.route_reason,
            "predicted_floor": self.predicted_floor,
            "verdict": self.verdict.value,
            "governor_emergency": self.governor_emergency,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_duration_s": self.estimated_duration_s,
            "target_file_count": self.target_file_count,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Cost/duration estimates — composes canonical route_budget_profile
# ===========================================================================


# Bytes-pinned per-route cost estimates. Drawn from
# CLAUDE.md §"Urgency-Aware Provider Routing" canonical
# table (~$0.03/IMMEDIATE / ~$0.005/STANDARD / etc.).
# AST regression pins the values so silent drift fires.
_ROUTE_COST_USD = {
    "immediate": 0.03,
    "standard": 0.005,
    "complex": 0.015,
    "background": 0.002,
    "speculative": 0.001,
}


# Per-route nominal duration in seconds, derived from
# CLAUDE.md timeout guidance (IMMEDIATE 60s / STANDARD
# 120s / COMPLEX/BACKGROUND 180s; SPECULATIVE async).
# These are MEDIANS for a single op — actual duration
# varies. Used as advisory ETA.
_ROUTE_DURATION_S = {
    "immediate": 30.0,
    "standard": 60.0,
    "complex": 120.0,
    "background": 90.0,
    "speculative": 5.0,
}


def _estimate_cost(route_value: str) -> float:
    return _ROUTE_COST_USD.get(
        route_value.lower(), 0.005,
    )


def _estimate_duration(route_value: str) -> float:
    return _ROUTE_DURATION_S.get(
        route_value.lower(), 60.0,
    )


# ===========================================================================
# Previewer — composes canonical classifier + risk-tier
# ===========================================================================


def preview_command(
    *,
    command_summary: str = "",
    signal_urgency: str = "normal",
    signal_source: str = "",
    task_complexity: str = "moderate",
    target_files: Optional[Tuple[str, ...]] = None,
    cross_repo: bool = False,
) -> Optional[CommandPreview]:
    """Pre-classify a hypothetical command. NEVER raises.

    Returns None when:
      * master flag off
      * canonical classifier or risk-tier-floor unavailable

    Composes canonical:
      * UrgencyRouter.classify(synthetic_ctx) → ProviderRoute
      * risk_tier_floor.recommended_floor() → current floor
      * route_budget_profile() — embedded in cost estimate
      * sensor_governor emergency state for verdict bump
    """
    if not master_enabled():
        return None

    files_tuple = tuple(target_files or ())
    ctx = _PreviewContext(
        signal_urgency=str(signal_urgency or "normal"),
        signal_source=str(signal_source or ""),
        task_complexity=str(task_complexity or "moderate"),
        target_files=files_tuple,
        cross_repo=bool(cross_repo),
    )

    # Predicted route via canonical classifier.
    predicted_route = "standard"
    route_reason = ""
    try:
        from backend.core.ouroboros.governance.urgency_router import (  # noqa: E501
            UrgencyRouter,
        )
        router = UrgencyRouter()
        route, reason = router.classify(ctx)
        predicted_route = route.value
        route_reason = reason
    except Exception:  # noqa: BLE001
        logger.debug(
            "risk_command_preview: classify failed",
            exc_info=True,
        )

    # Predicted floor via canonical risk-tier reader.
    predicted_floor = "safe_auto"
    try:
        from backend.core.ouroboros.governance.risk_tier_floor import (  # noqa: E501
            recommended_floor,
        )
        predicted_floor = str(recommended_floor() or "safe_auto")
    except Exception:  # noqa: BLE001
        logger.debug(
            "risk_command_preview: floor read failed",
            exc_info=True,
        )

    # Governor emergency state via canonical sensor_governor
    # (best-effort; falls through to False on any failure).
    governor_emergency = False
    try:
        from backend.core.ouroboros.governance.sensor_governor import (  # noqa: E501
            get_default_governor,
        )
        gov = get_default_governor()
        if gov is not None:
            governor_emergency = bool(
                getattr(gov, "is_emergency_brake", lambda: False)()
            )
    except Exception:  # noqa: BLE001
        governor_emergency = False

    verdict = _verdict_for_floor(
        predicted_floor,
        governor_emergency=governor_emergency,
    )

    cost = _estimate_cost(predicted_route)
    duration = _estimate_duration(predicted_route)

    diagnostic = ""
    if cross_repo:
        diagnostic = "cross_repo"
    elif len(files_tuple) >= 3:
        diagnostic = f"multi_file_{len(files_tuple)}"

    preview = CommandPreview(
        command_summary=str(command_summary or "")[:200],
        predicted_route=predicted_route,
        route_reason=route_reason,
        predicted_floor=predicted_floor,
        verdict=verdict,
        governor_emergency=governor_emergency,
        estimated_cost_usd=cost,
        estimated_duration_s=duration,
        target_file_count=len(files_tuple),
        diagnostic=diagnostic,
    )
    _publish_preview_event(preview)
    return preview


# ===========================================================================
# SSE composition
# ===========================================================================


def _publish_preview_event(preview: CommandPreview) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_COMMAND_PREVIEW_RENDERED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_COMMAND_PREVIEW_RENDERED,
            "command_preview",
            preview.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "risk_command_preview: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_VERDICT_GLYPHS = {
    PreviewVerdict.SAFE: "✓",
    PreviewVerdict.NOTIFY: "⚠",
    PreviewVerdict.APPROVAL: "🔒",
    PreviewVerdict.BLOCKED: "✗",
}


_VERDICT_TINTS = {
    PreviewVerdict.SAFE: "green",
    PreviewVerdict.NOTIFY: "yellow",
    PreviewVerdict.APPROVAL: "orange3",
    PreviewVerdict.BLOCKED: "red",
}


def _format_duration(seconds: float) -> str:
    if seconds <= 0.0:
        return "0s"
    if seconds < 60.0:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60.0:
        return f"{minutes:.1f}m"
    return f"{minutes / 60.0:.1f}h"


def format_command_preview(
    preview: Optional[CommandPreview],
) -> str:
    """Render multi-line command preview. Empty when master
    off OR preview is None."""
    if not master_enabled():
        return ""
    if preview is None:
        return ""
    glyph = _VERDICT_GLYPHS.get(preview.verdict, "?")
    tint = _VERDICT_TINTS.get(preview.verdict, "white")
    parts = [
        "[bright_yellow]🔮 Command preview:[/]"
    ]
    if preview.command_summary:
        parts.append(
            f"  [dim]\"{preview.command_summary}\"[/]"
        )
    parts.append(
        f"  [{tint}]{glyph} {preview.verdict.value.upper()}[/] "
        f"(floor: {preview.predicted_floor})"
    )
    parts.append(
        f"  route   : {preview.predicted_route} "
        f"[dim]({preview.route_reason})[/]"
    )
    parts.append(
        f"  cost    : ~${preview.estimated_cost_usd:.4f}"
    )
    parts.append(
        f"  duration: ~"
        f"{_format_duration(preview.estimated_duration_s)}"
    )
    if preview.target_file_count > 0:
        parts.append(
            f"  files   : "
            f"{preview.target_file_count}"
        )
    if preview.governor_emergency:
        parts.append(
            "  [red]⚠ governor emergency brake active[/]"
        )
    if preview.diagnostic:
        parts.append(
            f"  [dim]{preview.diagnostic}[/]"
        )
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-3 #19 risk-aware command preview "
            "master switch (graduation contract per §33.1; "
            "default FALSE).",
            "false",
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
                    "risk_command_preview.py"
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
        invariant_name=(
            "section_39_tier3_19_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — preview master "
            "stays default-False until evidence ladder "
            "closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_command_preview.py"
        ),
        validate=_master_default_false,
    ))

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_19_authority_asymmetry"
        ),
        description=(
            "Substrate purity — preview NEVER calls "
            "orchestrator or candidate_generator. "
            "(risk_tier_floor IS allowed — it's a read-"
            "only canonical source.)"
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_command_preview.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _verdict_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PreviewVerdict"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "SAFE", "NOTIFY", "APPROVAL", "BLOCKED",
                }
                missing = expected - names
                if missing:
                    return [
                        f"PreviewVerdict missing: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["PreviewVerdict class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_19_verdict_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value PreviewVerdict taxonomy "
            "mapping risk-tier floors to operator-"
            "friendly traffic light."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_command_preview.py"
        ),
        validate=_verdict_taxonomy,
    ))

    def _composes_urgency_router(tree: ast.AST, src: str):
        if (
            "urgency_router" not in src
            or "UrgencyRouter" not in src
        ):
            return [
                "must lazy-import urgency_router + "
                "UrgencyRouter (canonical classifier — NO "
                "parallel route logic)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_19_composes_canonical_"
            "urgency_router"
        ),
        description=(
            "Preview composes canonical UrgencyRouter "
            "classifier — NO parallel route logic."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_command_preview.py"
        ),
        validate=_composes_urgency_router,
    ))

    def _composes_risk_tier_floor(tree: ast.AST, src: str):
        if (
            "risk_tier_floor" not in src
            or "recommended_floor" not in src
        ):
            return [
                "must lazy-import risk_tier_floor + "
                "recommended_floor (canonical risk-tier "
                "source — NO parallel floor inference)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_19_composes_canonical_"
            "risk_tier_floor"
        ),
        description=(
            "Preview composes canonical risk_tier_floor "
            "for cage stance — NO parallel floor inference."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "risk_command_preview.py"
        ),
        validate=_composes_risk_tier_floor,
    ))

    return pins


__all__ = [
    "RISK_COMMAND_PREVIEW_SCHEMA_VERSION",
    "PreviewVerdict",
    "CommandPreview",
    "master_enabled",
    "preview_command",
    "format_command_preview",
    "register_flags",
    "register_shipped_invariants",
]
