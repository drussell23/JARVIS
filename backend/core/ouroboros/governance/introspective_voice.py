"""§38.11-D — Introspective Voice (PRD v2.67 to v2.68, 2026-05-08).

Merged with §39 #9 per §38.11.5a reconciliation. ONE substrate
exposes the model's introspective voice across four
NarrativeKind frames:

  * ``INTENT``           — proactive "I'm going to do X"
  * ``THINKING``         — extended-thinking reasoning tokens
  * ``L2_REPAIR_PROSE``  — self-correction during repair
  * ``DREAM``            — DreamEngine speculative blueprint prose
                          (NEW kind, extended canonical taxonomy
                          6→7 in this slice)

This module is a READ-ONLY aggregator. It composes the
existing canonical :class:`NarrativeChannel` —
authority-asymmetry: it NEVER produces parallel prose, NEVER
calls the model, NEVER mutates frames. It only:

  1. Reads frames matching the 4 introspection kinds.
  2. Renders an aggregated panel.
  3. Exposes a producer-bridge ``emit_dream_prose(...)`` for
     DreamEngine (writes into the canonical channel as a
     :data:`NarrativeKind.DREAM` frame).
  4. Publishes a single ``dream_emitted`` SSE event when a
     DREAM frame commits.

§33 patterns invoked:

  * §33.1 graduation contract — master flag default-FALSE.
  * §33.2 producer-bridge — ``emit_dream_prose`` is the
    DreamEngine-side hook (lazy-importable; NEVER raises).
  * §33.3 naming-cage — ``introspect_repl.py`` (sibling
    module) auto-discovers via §32.11 Slice 4 dispatch
    registry.
  * §33.5 versioned artifact — frozen
    :class:`IntrospectionFrame` (projection wrapping a
    canonical NarrativeFrame; carries schema_version).
"""
from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


INTROSPECTIVE_VOICE_SCHEMA_VERSION: str = "introspective_voice.1"


_ENV_MASTER = "JARVIS_INTROSPECTIVE_VOICE_ENABLED"
_ENV_SUB_DREAM_BRIDGE = "JARVIS_INTROSPECTIVE_DREAM_BRIDGE_ENABLED"
_ENV_SUB_PANEL = "JARVIS_INTROSPECTIVE_PANEL_ENABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def dream_bridge_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_DREAM_BRIDGE, default=True)


def panel_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PANEL, default=True)


# ===========================================================================
# Closed taxonomy — voice axes
# ===========================================================================


class IntrospectionAxis(str, enum.Enum):
    """Closed 4-value vocabulary for the introspective voice
    panel's logical axes. Each axis maps to one canonical
    NarrativeKind from the (now 7-value) taxonomy.

    The 3 NarrativeKind values NOT mapped (PLAN_PROSE,
    TOOL_PREAMBLE, POSTMORTEM_PROSE) are excluded from the
    introspection panel by design — they're either covered
    by other §38.11 surfaces (PLAN_PROSE in §38.11-E) or
    out-of-scope for "introspective voice".
    """

    INTENT = "intent"                     # proactive intent
    THINKING = "thinking"                 # active reasoning
    SELF_CORRECTION = "self_correction"   # → L2_REPAIR_PROSE
    DREAM = "dream"                       # idle speculation

    @classmethod
    def coerce(cls, raw: object) -> "IntrospectionAxis":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.THINKING


# Bytes-pinned mapping IntrospectionAxis → NarrativeKind.
# Values resolved lazily inside _axis_to_narrative_kind to
# keep imports light and the module testable in isolation.
_AXIS_KIND_NAMES: Tuple[Tuple[IntrospectionAxis, str], ...] = (
    (IntrospectionAxis.INTENT, "INTENT"),
    (IntrospectionAxis.THINKING, "THINKING"),
    (IntrospectionAxis.SELF_CORRECTION, "L2_REPAIR_PROSE"),
    (IntrospectionAxis.DREAM, "DREAM"),
)


def _axis_to_narrative_kind(axis: IntrospectionAxis):
    """Resolve canonical NarrativeKind member from axis. Lazy
    import; NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            NarrativeKind,
        )
    except Exception:  # noqa: BLE001
        return None
    for ax, name in _AXIS_KIND_NAMES:
        if ax is axis:
            return getattr(NarrativeKind, name, None)
    return None


# ===========================================================================
# Frozen §33.5 versioned projection
# ===========================================================================


@dataclass(frozen=True)
class IntrospectionFrame:
    """Read-only projection of one canonical NarrativeFrame
    grouped under an :class:`IntrospectionAxis`.

    Carries schema_version per §33.5 versioned-artifact contract.
    """

    axis: IntrospectionAxis
    op_id: str
    phase: str
    prose: str
    started_at: float
    terminal_at: float
    schema_version: str = INTROSPECTIVE_VOICE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "axis": self.axis.value,
            "op_id": self.op_id,
            "phase": self.phase,
            "prose": self.prose,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Aggregator — composes canonical NarrativeChannel only
# ===========================================================================


_MAX_PER_AXIS = 8


def aggregate_introspection_frames(
    *,
    op_id: Optional[str] = None,
    limit_per_axis: int = 3,
) -> Tuple[IntrospectionFrame, ...]:
    """Read recent COMMITTED frames across the 4 axes.

    Composes canonical
    :meth:`NarrativeChannel.frames_by_op_kind`. NEVER raises;
    returns empty tuple on any composition failure.

    ``op_id`` filter — if non-empty, only frames for that op
    are returned. If None or empty, walks ALL refs (system-
    level introspection — bounded by NarrativeChannel
    capacity).
    """
    if not panel_enabled():
        return ()
    try:
        n = max(1, min(int(limit_per_axis), _MAX_PER_AXIS))
    except (TypeError, ValueError):
        n = 3

    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            FrameState, NarrativeKind, get_default_channel,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        ch = get_default_channel()
    except Exception:  # noqa: BLE001
        return ()

    out: list = []
    for axis, _kind_name in _AXIS_KIND_NAMES:
        kind = _axis_to_narrative_kind(axis)
        if kind is None:
            continue
        try:
            if op_id:
                frames = ch.frames_by_op_kind(
                    op_id=str(op_id),
                    kind=kind,
                    states=(FrameState.COMMITTED,),
                )
            else:
                frames = _walk_all_frames_for_kind(
                    ch, kind=kind,
                )
        except Exception:  # noqa: BLE001
            continue
        for fr in frames[-n:]:
            try:
                out.append(IntrospectionFrame(
                    axis=axis,
                    op_id=fr.op_id,
                    phase=fr.phase,
                    prose=_truncate(fr.prose, max_chars=240),
                    started_at=fr.started_at,
                    terminal_at=fr.terminal_at,
                ))
            except Exception:  # noqa: BLE001
                continue
    return tuple(out)


def _walk_all_frames_for_kind(channel, *, kind):
    """Compose canonical
    :meth:`NarrativeChannel.find_by_kind` then filter to
    COMMITTED state. NEVER raises — returns empty list on
    any failure."""
    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            FrameState,
        )
    except Exception:  # noqa: BLE001
        return []
    try:
        all_of_kind = channel.find_by_kind(kind)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for fr in all_of_kind:
        try:
            if fr.state is FrameState.COMMITTED:
                out.append(fr)
        except Exception:  # noqa: BLE001
            continue
    return out


def _truncate(s: object, *, max_chars: int) -> str:
    try:
        text = str(s or "")
    except Exception:  # noqa: BLE001
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


# ===========================================================================
# Producer-bridge §33.2 — DreamEngine emits DREAM prose
# ===========================================================================


def emit_dream_prose(
    *,
    op_id: str,
    prose: str,
    phase: str = "DREAM",
    provider: str = "dream_engine",
) -> bool:
    """Producer-bridge for DreamEngine.

    Writes a single :data:`NarrativeKind.DREAM` frame into
    the canonical NarrativeChannel:

      * start_frame → append_token (one shot) → commit

    NEVER raises. Returns ``True`` if the frame was committed
    cleanly, ``False`` on any failure (master/sub-flag off,
    canonical channel unavailable, etc.).
    """
    if not dream_bridge_enabled():
        return False
    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            NarrativeKind, get_default_channel,
        )
    except Exception:  # noqa: BLE001
        return False
    try:
        ch = get_default_channel()
        frame = ch.start_frame(
            op_id=str(op_id or ""),
            phase=str(phase or "DREAM"),
            kind=NarrativeKind.DREAM,
            provider=str(provider or ""),
        )
        if frame is None:
            return False
        text = _truncate(prose, max_chars=2000)
        if text:
            ch.append_token(
                op_id=frame.op_id,
                phase=frame.phase,
                kind=NarrativeKind.DREAM,
                token=text,
            )
        committed = ch.commit(
            op_id=frame.op_id,
            phase=frame.phase,
            kind=NarrativeKind.DREAM,
        )
        if committed is not None:
            _publish_dream_event(committed)
            return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "introspective: emit_dream_prose failed",
            exc_info=True,
        )
    return False


def _publish_dream_event(frame) -> None:
    """Publish dream_emitted SSE via canonical broker.
    Best-effort; NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_DREAM_EMITTED, get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_DREAM_EMITTED,
            getattr(frame, "op_id", "") or "",
            {
                "op_id": getattr(frame, "op_id", ""),
                "phase": getattr(frame, "phase", ""),
                "ref": getattr(frame, "ref", ""),
                "char_count": getattr(frame, "char_count", 0),
                "schema_version": (
                    INTROSPECTIVE_VOICE_SCHEMA_VERSION
                ),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "introspective: SSE dream publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer — pure, NEVER raises
# ===========================================================================


_AXIS_GLYPHS = {
    IntrospectionAxis.INTENT: "💭",
    IntrospectionAxis.THINKING: "🤔",
    IntrospectionAxis.SELF_CORRECTION: "🔧",
    IntrospectionAxis.DREAM: "🌙",
}


_AXIS_LABELS = {
    IntrospectionAxis.INTENT: "intent",
    IntrospectionAxis.THINKING: "thinking",
    IntrospectionAxis.SELF_CORRECTION: "self-correction",
    IntrospectionAxis.DREAM: "dream",
}


def format_introspective_voice_panel(
    *,
    frames: Optional[Tuple[IntrospectionFrame, ...]] = None,
    op_id: Optional[str] = None,
    limit_per_axis: int = 2,
) -> str:
    """Render the introspective voice panel.

    Groups frames by axis. Empty when master off OR no frames.
    """
    if not panel_enabled():
        return ""
    if frames is None:
        frames = aggregate_introspection_frames(
            op_id=op_id,
            limit_per_axis=limit_per_axis,
        )
    if not frames:
        return ""

    grouped: dict = {a: [] for a, _ in _AXIS_KIND_NAMES}
    for f in frames:
        grouped.setdefault(f.axis, []).append(f)

    parts = ["[bright_magenta]🌙 Introspective voice:[/]"]
    for axis, _name in _AXIS_KIND_NAMES:
        items = grouped.get(axis, [])
        if not items:
            continue
        glyph = _AXIS_GLYPHS.get(axis, "•")
        label = _AXIS_LABELS.get(axis, axis.value)
        parts.append(f"  {glyph} [italic]{label}[/]")
        for item in items[-limit_per_axis:]:
            prose = item.prose or "(silent)"
            op_tag = (
                f" ({item.op_id[:12]})"
                if item.op_id else ""
            )
            parts.append(f"      › {prose}{op_tag}")
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
            "§38.11-D introspective voice master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_DREAM_BRIDGE, "bool",
            "Enable DreamEngine producer-bridge "
            "(emit_dream_prose). Default TRUE when master on.",
            "true",
        ),
        (
            _ENV_SUB_PANEL, "bool",
            "Enable introspective voice panel renderer. "
            "Default TRUE when master on.",
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
                    "introspective_voice.py"
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

    # ---- Pin 1: master_default_false -------------------------------------

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
            "section_38_11d_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "introspective_voice.py"
        ),
        validate=_master_default_false,
    ))

    # ---- Pin 2: authority_asymmetry --------------------------------------

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
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11d_authority_asymmetry"
        ),
        description=(
            "Substrate purity — module composes canonical "
            "NarrativeChannel read API only; no orchestrator "
            "/risk-tier imports."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "introspective_voice.py"
        ),
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: axis_taxonomy_4_values -----------------------------------

    def _axis_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "IntrospectionAxis"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "INTENT", "THINKING",
                    "SELF_CORRECTION", "DREAM",
                }
                missing = expected - names
                if missing:
                    return [
                        f"IntrospectionAxis missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["IntrospectionAxis class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11d_axis_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value IntrospectionAxis taxonomy — "
            "DREAM is the §38.11-D-added axis tied to the "
            "canonical NarrativeKind.DREAM extension."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "introspective_voice.py"
        ),
        validate=_axis_taxonomy,
    ))

    # ---- Pin 4: composes_canonical_narrative_channel ---------------------

    def _composes_narrative(tree: ast.AST, src: str):
        if "battle_test.narrative_channel" not in src:
            return [
                "must lazy-import battle_test.narrative_channel "
                "(canonical substrate)"
            ]
        # Must reference DREAM kind (the new value this slice adds)
        if "NarrativeKind.DREAM" not in src:
            return [
                "must reference NarrativeKind.DREAM (the kind "
                "this slice extends the canonical taxonomy with)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11d_composes_canonical_narrative_channel"
        ),
        description=(
            "Aggregator + producer-bridge compose canonical "
            "NarrativeChannel; references the DREAM kind that "
            "this slice extends the taxonomy with (no parallel "
            "prose substrate)."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "introspective_voice.py"
        ),
        validate=_composes_narrative,
    ))

    # ---- Pin 5: dream_kind_is_extended_canonical -------------------------

    def _dream_extends_canonical(_tree: ast.AST, _src: str):
        """The canonical NarrativeKind enum MUST contain
        DREAM. Without this, the §38.11-D extension regressed
        and the DreamEngine producer-bridge silently fails."""
        try:
            from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
                NarrativeKind,
            )
        except Exception:  # noqa: BLE001
            return [
                "could not import canonical NarrativeKind"
            ]
        if "DREAM" not in {m.name for m in NarrativeKind}:
            return [
                "canonical NarrativeKind missing DREAM — "
                "§38.11-D extension regressed"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11d_dream_kind_is_extended_canonical"
        ),
        description=(
            "The §38.11-D NarrativeKind 6→7 extension is "
            "load-bearing for emit_dream_prose. This pin "
            "fires if the canonical taxonomy regresses."
        ),
        target_file=(
            "backend/core/ouroboros/battle_test/"
            "narrative_channel.py"
        ),
        validate=_dream_extends_canonical,
    ))

    return pins


__all__ = [
    "INTROSPECTIVE_VOICE_SCHEMA_VERSION",
    "IntrospectionAxis",
    "IntrospectionFrame",
    "master_enabled",
    "dream_bridge_enabled",
    "panel_enabled",
    "aggregate_introspection_frames",
    "emit_dream_prose",
    "format_introspective_voice_panel",
    "register_flags",
    "register_shipped_invariants",
]
