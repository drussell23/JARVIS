"""§39 Tier-5 #17 — Procedural ASCII portrait
(PRD v2.74 to v2.75, 2026-05-09).

Generative ASCII art representing organism's mood +
posture + activity. Different "face" each moment —
deterministic given inputs (same mood + posture +
heartbeat → same face).

Composes canonical:
  * polish_bundle.compute_mood (4-value MoodGlyph)
  * polish_bundle.format_heartbeat
  * direction_inferrer current posture (via posture_palette)

Authority asymmetry: ZERO. Pure composer + renderer.

§38.11.5a.5 single-canonical-name: ZERO new mood/posture/
heartbeat substrate; the only NEW closed taxonomy is
:class:`PortraitMode` (3 values for face style: AT_REST /
WORKING / ALERT).

§33 patterns:
- §33.1 graduation contract
- §33.5 versioned artifact
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


PROCEDURAL_PORTRAIT_SCHEMA_VERSION: str = (
    "procedural_portrait.1"
)


_ENV_MASTER = "JARVIS_PROCEDURAL_PORTRAIT_ENABLED"


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
# Closed taxonomy — 3-value PortraitMode
# ===========================================================================


class PortraitMode(str, enum.Enum):
    """Closed 3-value face mode vocabulary.

    AT_REST   — idle / IDLE posture, low activity
    WORKING   — normal ops, moderate activity
    ALERT     — high activity / EMERGENCY mood / HARDEN posture
    """

    AT_REST = "at_rest"
    WORKING = "working"
    ALERT = "alert"


# Bytes-pinned face element catalog. Each is a tuple of
# (mode, glyph_set) — face composes one element per slot
# deterministically. AST regression locks the canonical
# 3 mode-keys + slot count.
_EYE_GLYPHS_AT_REST: Tuple[str, ...] = ("·", "-", "˘", "•")
_EYE_GLYPHS_WORKING: Tuple[str, ...] = ("○", "◔", "◑", "◕")
_EYE_GLYPHS_ALERT: Tuple[str, ...] = ("◉", "⊙", "⦿", "⊚")

_MOUTH_GLYPHS_AT_REST: Tuple[str, ...] = ("‿", "_", "—", "‾")
_MOUTH_GLYPHS_WORKING: Tuple[str, ...] = ("◡", "◌", "◯", "◐")
_MOUTH_GLYPHS_ALERT: Tuple[str, ...] = ("◑", "◒", "▽", "○")


_EYES_BY_MODE = {
    PortraitMode.AT_REST: _EYE_GLYPHS_AT_REST,
    PortraitMode.WORKING: _EYE_GLYPHS_WORKING,
    PortraitMode.ALERT: _EYE_GLYPHS_ALERT,
}


_MOUTH_BY_MODE = {
    PortraitMode.AT_REST: _MOUTH_GLYPHS_AT_REST,
    PortraitMode.WORKING: _MOUTH_GLYPHS_WORKING,
    PortraitMode.ALERT: _MOUTH_GLYPHS_ALERT,
}


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class PortraitState:
    """Composed portrait state."""

    mode: PortraitMode
    mood_label: str = ""           # canonical MoodGlyph value
    posture_label: str = ""        # canonical Posture value
    heartbeat_glyph: str = ""      # canonical heartbeat
    seed: str = ""                 # determinism seed
    face: Tuple[str, ...] = field(default_factory=tuple)
    aggregated_at_unix: float = 0.0
    schema_version: str = PROCEDURAL_PORTRAIT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "mood_label": self.mood_label,
            "posture_label": self.posture_label,
            "heartbeat_glyph": self.heartbeat_glyph,
            "seed": self.seed,
            "face": list(self.face),
            "aggregated_at_unix": self.aggregated_at_unix,
        }


# ===========================================================================
# Composer — composes canonical mood + posture + heartbeat
# ===========================================================================


def _read_canonical_mood() -> str:
    """Pull canonical MoodGlyph value via polish_bundle.
    Returns "" on any failure."""
    try:
        from backend.core.ouroboros.governance.polish_bundle import (  # noqa: E501
            MoodGlyph, compute_mood,
        )
        # Use neutral inputs as fallback when callers don't
        # supply data — mood will land in NEUTRAL slot.
        m = compute_mood(
            convergence_score=0.5,
            error_rate=0.0,
            cost_burn_pct=0.0,
            governor_emergency=False,
        )
        if hasattr(m, "value"):
            return m.value
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _read_canonical_posture() -> str:
    try:
        from backend.core.ouroboros.governance.posture_palette import (  # noqa: E501
            read_current_posture_safe,
        )
        p = read_current_posture_safe()
        if p is None:
            return ""
        if hasattr(p, "value"):
            return str(p.value)
        return str(p)
    except Exception:  # noqa: BLE001
        return ""


def _read_canonical_heartbeat() -> str:
    """Compose canonical heartbeat glyph via polish_bundle."""
    try:
        from backend.core.ouroboros.governance.polish_bundle import (  # noqa: E501
            format_heartbeat,
        )
        # Caller-provided dummy ops_per_min; the rendered
        # glyph is the artifact (♥ or ♡).
        h = format_heartbeat(ops_per_min=1.0, tick_index=0)
        return str(h or "♡")
    except Exception:  # noqa: BLE001
        return "♡"


def _mode_for_inputs(
    *,
    mood_label: str, posture_label: str,
) -> PortraitMode:
    """Pure-function mode classification. NEVER raises."""
    m = (mood_label or "").lower()
    p = (posture_label or "").lower()
    # ALERT first — emergency / harden / struggling.
    if (
        m in ("emergency", "struggling")
        or p == "harden"
    ):
        return PortraitMode.ALERT
    # AT_REST — neutral mood + maintain posture.
    if (
        m in ("", "neutral") and p in ("", "maintain")
    ):
        return PortraitMode.AT_REST
    return PortraitMode.WORKING


def _seed(
    mode: PortraitMode,
    mood_label: str, posture_label: str,
) -> str:
    """Deterministic seed for face element selection."""
    raw = (
        f"{mode.value}|{mood_label}|{posture_label}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


def _pick_glyph(
    glyphs: Tuple[str, ...], *, seed: str, slot: int,
) -> str:
    """Deterministic glyph pick from a bytes-pinned glyph
    tuple. NEVER raises."""
    if not glyphs:
        return "?"
    try:
        h = hashlib.sha256(
            f"{seed}|{slot}".encode("utf-8"),
        ).hexdigest()
        idx = int(h[:8], 16) % len(glyphs)
        return glyphs[idx]
    except Exception:  # noqa: BLE001
        return glyphs[0]


def aggregate_portrait(
    *,
    mood_label: Optional[str] = None,
    posture_label: Optional[str] = None,
    heartbeat_glyph: Optional[str] = None,
) -> PortraitState:
    """Compose canonical mood + posture + heartbeat into
    portrait state. NEVER raises."""
    if not master_enabled():
        return PortraitState(mode=PortraitMode.AT_REST)

    mood = (
        mood_label
        if mood_label is not None
        else _read_canonical_mood()
    )
    posture = (
        posture_label
        if posture_label is not None
        else _read_canonical_posture()
    )
    heart = (
        heartbeat_glyph
        if heartbeat_glyph is not None
        else _read_canonical_heartbeat()
    )

    mode = _mode_for_inputs(
        mood_label=mood, posture_label=posture,
    )
    seed = _seed(mode, mood, posture)

    eyes_pool = _EYES_BY_MODE.get(
        mode, _EYE_GLYPHS_AT_REST,
    )
    mouth_pool = _MOUTH_BY_MODE.get(
        mode, _MOUTH_GLYPHS_AT_REST,
    )
    left_eye = _pick_glyph(eyes_pool, seed=seed, slot=0)
    right_eye = _pick_glyph(eyes_pool, seed=seed, slot=1)
    mouth = _pick_glyph(mouth_pool, seed=seed, slot=2)

    # 3-line face frame, 9 chars wide.
    face_lines: List[str] = [
        "  ┌─────┐",
        f"  │ {left_eye} {right_eye} │  {heart}",
        f"  │  {mouth}  │",
        "  └─────┘",
    ]

    state = PortraitState(
        mode=mode,
        mood_label=mood,
        posture_label=posture,
        heartbeat_glyph=heart,
        seed=seed,
        face=tuple(face_lines),
        aggregated_at_unix=time.time(),
    )
    _publish_event(state)
    return state


def _publish_event(state: PortraitState) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PORTRAIT_RENDERED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            broker.publish(
                EVENT_TYPE_PORTRAIT_RENDERED,
                "procedural_portrait",
                {
                    "schema_version": (
                        PROCEDURAL_PORTRAIT_SCHEMA_VERSION
                    ),
                    "mode": state.mode.value,
                    "mood_label": state.mood_label,
                    "posture_label": state.posture_label,
                    "seed": state.seed,
                    "aggregated_at_unix": (
                        state.aggregated_at_unix
                    ),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "procedural_portrait: SSE failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


def format_portrait(
    state: Optional[PortraitState] = None,
) -> str:
    """Render the procedural portrait. Empty when master
    off."""
    if not master_enabled():
        return ""
    if state is None:
        state = aggregate_portrait()
    if not state.face:
        return ""
    label_parts = []
    if state.mood_label:
        label_parts.append(f"mood={state.mood_label}")
    if state.posture_label:
        label_parts.append(f"posture={state.posture_label}")
    label = (
        " · ".join(label_parts) if label_parts else "(no labels)"
    )
    parts = [
        f"[bright_yellow]🎭 Procedural portrait:[/] "
        f"[dim]{state.mode.value} · {label}[/]",
    ]
    parts.extend(state.face)
    parts.append(
        f"  [dim]seed={state.seed}[/]"
    )
    return "\n".join(parts)


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
                "§39 Tier-5 #17 procedural portrait "
                "master switch (default FALSE per §33.1)."
            ),
            example="false",
            source_file=(
                "backend/core/ouroboros/governance/"
                "procedural_portrait.py"
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
                return ["master must default False"]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_17_master_default_false"
        ),
        description="§33.1.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "procedural_portrait.py"
        ),
        validate=_master,
    ))

    def _mode_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PortraitMode"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "AT_REST", "WORKING", "ALERT",
                }
                missing = expected - names
                if missing:
                    return [f"missing: {sorted(missing)}"]
                return []
        return ["PortraitMode not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_17_mode_taxonomy_3_values"
        ),
        description="Closed 3-value PortraitMode.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "procedural_portrait.py"
        ),
        validate=_mode_taxonomy,
    ))

    def _composes_canonical(tree, src):
        required = ("polish_bundle", "posture_palette")
        missing = [r for r in required if r not in src]
        if missing:
            return [
                f"must compose canonical: {missing}"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_17_composes_canonical_sources"
        ),
        description=(
            "Composes canonical polish_bundle + "
            "posture_palette — NO parallel mood/posture/"
            "heartbeat substrate."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "procedural_portrait.py"
        ),
        validate=_composes_canonical,
    ))

    def _authority(tree, src):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if any(m.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden: {m}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_17_authority_asymmetry"
        ),
        description="Substrate purity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "procedural_portrait.py"
        ),
        validate=_authority,
    ))

    return pins


__all__ = [
    "PROCEDURAL_PORTRAIT_SCHEMA_VERSION",
    "PortraitMode",
    "PortraitState",
    "master_enabled",
    "aggregate_portrait",
    "format_portrait",
    "register_flags",
    "register_shipped_invariants",
]
