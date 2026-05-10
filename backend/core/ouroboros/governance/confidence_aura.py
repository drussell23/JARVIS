"""§39 Tier-5 #15 — Color-coded confidence aura
(PRD v2.74 to v2.75, 2026-05-09).

Renders per-token confidence as a Rich background tint.
Composes canonical
:class:`verification.confidence_capture.ConfidenceTrace`
+ :meth:`ConfidenceToken.margin_top1_top2`. ZERO parallel
logprob computation; ZERO new probability math.

Authority asymmetry: ZERO. Read-only renderer.

§38.11.5a.5 single-canonical-name: reuses canonical
ConfidenceToken + ConfidenceTrace shapes; the only NEW
closed taxonomy is :class:`ConfidenceTier` (4 values
mapped via bytes-pinned margin thresholds).
"""
from __future__ import annotations

import enum
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


CONFIDENCE_AURA_SCHEMA_VERSION: str = "confidence_aura.1"


_ENV_MASTER = "JARVIS_CONFIDENCE_AURA_ENABLED"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


# ===========================================================================
# Closed taxonomy — 4-value ConfidenceTier
# ===========================================================================


class ConfidenceTier(str, enum.Enum):
    """Closed 4-value confidence vocabulary mapped via
    bytes-pinned :data:`_MARGIN_THRESHOLDS` (units: natural-
    log probability margin between top-1 and top-2 token).

    CERTAIN     — margin >= 4.0  (top-1 ≥ 50× top-2)
    CONFIDENT   — margin >= 2.0  (top-1 ≥ 7× top-2)
    UNCERTAIN   — margin >= 0.5  (top-1 ≥ 1.6× top-2)
    SCATTERED   — margin < 0.5   (model genuinely conflicted)
    """

    CERTAIN = "certain"
    CONFIDENT = "confident"
    UNCERTAIN = "uncertain"
    SCATTERED = "scattered"


# Bytes-pinned threshold table.
_MARGIN_THRESHOLDS: Tuple[Tuple[float, ConfidenceTier], ...] = (
    (4.0, ConfidenceTier.CERTAIN),
    (2.0, ConfidenceTier.CONFIDENT),
    (0.5, ConfidenceTier.UNCERTAIN),
)


def _tier_for_margin(margin: Optional[float]) -> ConfidenceTier:
    """Pure-function bucketing. NEVER raises.

    None / NaN / non-finite → SCATTERED (no signal).
    """
    if margin is None:
        return ConfidenceTier.SCATTERED
    try:
        m = float(margin)
        if not math.isfinite(m):
            return ConfidenceTier.SCATTERED
    except (TypeError, ValueError):
        return ConfidenceTier.SCATTERED
    for threshold, tier in _MARGIN_THRESHOLDS:
        if m >= threshold:
            return tier
    return ConfidenceTier.SCATTERED


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class AuraToken:
    """One token + tier projection."""

    text: str
    tier: ConfidenceTier
    margin: Optional[float] = None
    logprob: float = 0.0
    schema_version: str = CONFIDENCE_AURA_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tier": self.tier.value,
            "margin": self.margin,
            "logprob": self.logprob,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AuraSnapshot:
    """Aggregated aura projection of a ConfidenceTrace."""

    provider: str = ""
    model_id: str = ""
    tokens: Tuple[AuraToken, ...] = field(default_factory=tuple)
    by_tier: dict = field(default_factory=dict)
    schema_version: str = CONFIDENCE_AURA_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model_id": self.model_id,
            "tokens": [t.to_dict() for t in self.tokens],
            "by_tier": dict(self.by_tier),
        }


# ===========================================================================
# Aggregator — composes canonical ConfidenceTrace
# ===========================================================================


def aggregate_aura(trace: Any) -> AuraSnapshot:
    """Project a canonical
    :class:`verification.confidence_capture.ConfidenceTrace`
    onto AuraTokens. NEVER raises; returns empty snapshot
    on master flag off OR malformed trace.
    """
    if not master_enabled():
        return AuraSnapshot()
    if trace is None:
        return AuraSnapshot()

    provider = getattr(trace, "provider", "") or ""
    model_id = getattr(trace, "model_id", "") or ""
    raw_tokens = getattr(trace, "tokens", None) or ()

    tokens: list = []
    by_tier: dict = {t.value: 0 for t in ConfidenceTier}
    for tok in raw_tokens:
        try:
            margin: Optional[float]
            if hasattr(tok, "margin_top1_top2"):
                margin = tok.margin_top1_top2()
            else:
                margin = None
            tier = _tier_for_margin(margin)
            text = str(getattr(tok, "token", "") or "")
            try:
                lp = float(
                    getattr(tok, "logprob", 0.0) or 0.0,
                )
            except (TypeError, ValueError):
                lp = 0.0
            tokens.append(AuraToken(
                text=text,
                tier=tier,
                margin=margin,
                logprob=lp,
            ))
            by_tier[tier.value] = (
                by_tier.get(tier.value, 0) + 1
            )
        except Exception:  # noqa: BLE001
            continue

    snap = AuraSnapshot(
        provider=str(provider),
        model_id=str(model_id),
        tokens=tuple(tokens),
        by_tier=by_tier,
    )
    _publish_event(snap)
    return snap


def _publish_event(snap: AuraSnapshot) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_CONFIDENCE_AURA_RENDERED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            # Bounded payload — by_tier summary only (not
            # token text, which can be large).
            broker.publish(
                EVENT_TYPE_CONFIDENCE_AURA_RENDERED,
                "confidence_aura",
                {
                    "schema_version": (
                        CONFIDENCE_AURA_SCHEMA_VERSION
                    ),
                    "provider": snap.provider,
                    "model_id": snap.model_id,
                    "by_tier": dict(snap.by_tier),
                    "token_count": len(snap.tokens),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "confidence_aura: SSE failed", exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_TIER_TINTS = {
    ConfidenceTier.CERTAIN: "on green",
    ConfidenceTier.CONFIDENT: "on cyan",
    ConfidenceTier.UNCERTAIN: "on yellow",
    ConfidenceTier.SCATTERED: "on red",
}


_TIER_GLYPHS = {
    ConfidenceTier.CERTAIN: "█",
    ConfidenceTier.CONFIDENT: "▓",
    ConfidenceTier.UNCERTAIN: "▒",
    ConfidenceTier.SCATTERED: "░",
}


def format_aura_summary(snap: Optional[AuraSnapshot]) -> str:
    """Render confidence aura summary line + per-tier
    glyph bar. Empty when master off OR no tokens."""
    if not master_enabled():
        return ""
    if snap is None or not snap.tokens:
        return ""

    header_parts = [
        "[bright_yellow]🌈 Confidence aura:[/]"
    ]
    if snap.provider:
        header_parts.append(
            f"  [dim]provider: {snap.provider}"
            + (
                f" / model: {snap.model_id}"
                if snap.model_id else ""
            )
            + f"  ·  {len(snap.tokens)} tokens[/]"
        )
    by_tier_parts = []
    for tier in ConfidenceTier:
        n = snap.by_tier.get(tier.value, 0)
        if n > 0:
            tint = _TIER_TINTS.get(tier, "white")
            glyph = _TIER_GLYPHS.get(tier, "·")
            by_tier_parts.append(
                f"[{tint}]{glyph}[/] {tier.value}={n}"
            )
    if by_tier_parts:
        header_parts.append(
            "  " + " · ".join(by_tier_parts)
        )

    # Per-token aura strip — first 80 tokens, glyph per
    # token tinted by tier.
    glyphs = []
    for tok in snap.tokens[:80]:
        tint = _TIER_TINTS.get(tok.tier, "white")
        g = _TIER_GLYPHS.get(tok.tier, "·")
        glyphs.append(f"[{tint}]{g}[/]")
    if glyphs:
        header_parts.append("  " + "".join(glyphs))
    return "\n".join(header_parts)


# ===========================================================================
# FlagRegistry + AST pins
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            name=_ENV_MASTER, type="bool", category="ux",
            description=(
                "§39 Tier-5 #15 confidence aura master "
                "switch (default FALSE per §33.1)."
            ),
            example="false",
            source_file=(
                "backend/core/ouroboros/governance/"
                "confidence_aura.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
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
                                return []
                return ["master_enabled must default False"]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_15_master_default_false"
        ),
        description="§33.1 graduation contract.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "confidence_aura.py"
        ),
        validate=_master,
    ))

    def _tier_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ConfidenceTier"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "CERTAIN", "CONFIDENT",
                    "UNCERTAIN", "SCATTERED",
                }
                missing = expected - names
                if missing:
                    return [f"missing: {sorted(missing)}"]
                return []
        return ["ConfidenceTier not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_15_tier_taxonomy_4_values"
        ),
        description="Closed 4-value ConfidenceTier.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "confidence_aura.py"
        ),
        validate=_tier_taxonomy,
    ))

    def _thresholds_pinned(tree, src):
        if "_MARGIN_THRESHOLDS" not in src:
            return ["_MARGIN_THRESHOLDS missing"]
        if "4.0" not in src or "2.0" not in src or "0.5" not in src:
            return [
                "canonical thresholds 4.0/2.0/0.5 must "
                "appear in source — drift requires "
                "explicit pin update"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_15_thresholds_canonical"
        ),
        description=(
            "Bytes-pin canonical 4.0/2.0/0.5 logprob "
            "margin thresholds."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "confidence_aura.py"
        ),
        validate=_thresholds_pinned,
    ))

    def _no_parallel_logprob_math(tree, src):
        # Bytes-pin: must compose canonical
        # ConfidenceToken.margin_top1_top2 — substring
        # check for the canonical method name.
        if "margin_top1_top2" not in src:
            return [
                "must call canonical "
                "ConfidenceToken.margin_top1_top2() — NO "
                "parallel logprob math"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_15_composes_canonical_margin"
        ),
        description=(
            "Aggregator MUST call canonical "
            "margin_top1_top2() — no parallel logprob math."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "confidence_aura.py"
        ),
        validate=_no_parallel_logprob_math,
    ))

    return pins


__all__ = [
    "CONFIDENCE_AURA_SCHEMA_VERSION",
    "ConfidenceTier",
    "AuraToken",
    "AuraSnapshot",
    "master_enabled",
    "aggregate_aura",
    "format_aura_summary",
    "register_flags",
    "register_shipped_invariants",
]
