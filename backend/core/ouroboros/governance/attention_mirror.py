"""§39 Tier-5 #16 — Operator-AI attention mirror
(PRD v2.74 to v2.75, 2026-05-09).

Shows what O+V is "looking at" right now — composes
canonical recent SSE events (broker recent_history) +
narrative active frames (THINKING + TOOL_PREAMBLE) into
a focus-snapshot.

Authority asymmetry: ZERO. Read-only aggregator + renderer.

§38.11.5a.5 single-canonical-name: ZERO new aggregator —
composes canonical broker.recent_history and (when
available) canonical narrative_channel; the only NEW
closed taxonomy is :class:`AttentionFocus` (4 values).

§33 patterns:
- §33.1 graduation contract
- §33.5 versioned artifact
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


ATTENTION_MIRROR_SCHEMA_VERSION: str = "attention_mirror.1"


_ENV_MASTER = "JARVIS_ATTENTION_MIRROR_ENABLED"
_ENV_WINDOW_S = "JARVIS_ATTENTION_MIRROR_WINDOW_S"

_DEFAULT_WINDOW_S = 30
_MIN_WINDOW_S = 5
_MAX_WINDOW_S = 300


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def _read_window_s() -> int:
    raw = os.environ.get(_ENV_WINDOW_S, "").strip()
    if not raw:
        return _DEFAULT_WINDOW_S
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_S
    return max(_MIN_WINDOW_S, min(_MAX_WINDOW_S, n))


# ===========================================================================
# Closed taxonomy — 4-value AttentionFocus
# ===========================================================================


class AttentionFocus(str, enum.Enum):
    """Closed 4-value attention vocabulary.

    READING    — actively reading/exploring source
    SEARCHING  — actively searching code or globbing
    THINKING   — extended-thinking active (no tool calls)
    IDLE       — no recent attention signals in window
    """

    READING = "reading"
    SEARCHING = "searching"
    THINKING = "thinking"
    IDLE = "idle"


# Bytes-pinned event-name → focus map. Drift would silently
# misroute attention signals → AST regression.
_EVENT_TO_FOCUS = {
    # tool-call events surface what file is being read.
    "tool_call_started": AttentionFocus.READING,
    # MCP tool calls + read_file/glob_files patterns
    "mcp_tool_call": AttentionFocus.READING,
}


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class AttentionItem:
    """One observed attention signal."""

    focus: AttentionFocus
    summary: str
    op_id: str = ""
    observed_at_unix: float = 0.0
    schema_version: str = ATTENTION_MIRROR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "focus": self.focus.value,
            "summary": self.summary,
            "op_id": self.op_id,
            "observed_at_unix": self.observed_at_unix,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AttentionSnapshot:
    """Current attention state."""

    primary_focus: AttentionFocus = AttentionFocus.IDLE
    items: Tuple[AttentionItem, ...] = field(default_factory=tuple)
    aggregated_at_unix: float = 0.0
    window_s: int = _DEFAULT_WINDOW_S
    schema_version: str = ATTENTION_MIRROR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "primary_focus": self.primary_focus.value,
            "aggregated_at_unix": self.aggregated_at_unix,
            "window_s": self.window_s,
            "items": [i.to_dict() for i in self.items],
        }


# ===========================================================================
# Aggregator — composes canonical broker + narrative channel
# ===========================================================================


def aggregate_attention() -> AttentionSnapshot:
    """Compose recent canonical signals into a focus
    snapshot. NEVER raises."""
    if not master_enabled():
        return AttentionSnapshot()

    window = _read_window_s()
    now = time.time()
    cutoff = now - window
    items: List[AttentionItem] = []

    # 1) SSE broker recent_history → tool-call attention.
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            history = broker.recent_history()
            for ev in history:
                try:
                    ts = float(getattr(ev, "timestamp", 0) or 0)
                    if ts > 0 and ts < cutoff:
                        continue
                    et = str(
                        getattr(ev, "event_type", "") or "",
                    )
                    payload = (
                        getattr(ev, "payload", {}) or {}
                    )
                    if not isinstance(payload, dict):
                        continue
                    focus = _EVENT_TO_FOCUS.get(et)
                    if focus is None:
                        # Heuristic: search/glob events
                        # often have "tool" or "search" in
                        # their payload.
                        tool_name = str(
                            payload.get("tool_name", "")
                            or payload.get("tool", "")
                            or "",
                        ).lower()
                        if "search" in tool_name:
                            focus = AttentionFocus.SEARCHING
                        elif (
                            "read" in tool_name
                            or "glob" in tool_name
                        ):
                            focus = AttentionFocus.READING
                    if focus is None:
                        continue
                    summary_parts = []
                    for key in (
                        "tool_name", "tool",
                        "arg_summary", "summary",
                        "phase",
                    ):
                        v = payload.get(key)
                        if v:
                            summary_parts.append(str(v))
                    summary = " ".join(summary_parts)[:120]
                    items.append(AttentionItem(
                        focus=focus,
                        summary=summary or et,
                        op_id=str(
                            getattr(ev, "op_id", "") or "",
                        ),
                        observed_at_unix=ts,
                    ))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        logger.debug(
            "attention_mirror: broker unavailable",
            exc_info=True,
        )

    # 2) Active THINKING frames in narrative_channel.
    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            FrameState, NarrativeKind, get_default_channel,
        )
        ch = get_default_channel()
        for kind in (
            NarrativeKind.THINKING,
            NarrativeKind.TOOL_PREAMBLE,
        ):
            try:
                frames = ch.find_by_kind(kind)
                for fr in frames:
                    if fr.state is not FrameState.BUFFERING:
                        continue
                    items.append(AttentionItem(
                        focus=AttentionFocus.THINKING,
                        summary=(
                            str(fr.prose or "")[:120]
                            or kind.value
                        ),
                        op_id=str(fr.op_id or ""),
                        observed_at_unix=now,
                    ))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass

    # Determine primary focus by recency.
    if not items:
        primary = AttentionFocus.IDLE
    else:
        most_recent = max(
            items, key=lambda i: i.observed_at_unix,
        )
        primary = most_recent.focus

    snap = AttentionSnapshot(
        primary_focus=primary,
        items=tuple(
            sorted(
                items,
                key=lambda i: i.observed_at_unix,
                reverse=True,
            )[:20]
        ),
        aggregated_at_unix=now,
        window_s=window,
    )
    _publish_event(snap)
    return snap


def _publish_event(snap: AttentionSnapshot) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ATTENTION_MIRROR_UPDATED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            broker.publish(
                EVENT_TYPE_ATTENTION_MIRROR_UPDATED,
                "attention_mirror",
                {
                    "schema_version": (
                        ATTENTION_MIRROR_SCHEMA_VERSION
                    ),
                    "primary_focus": (
                        snap.primary_focus.value
                    ),
                    "item_count": len(snap.items),
                    "window_s": snap.window_s,
                    "aggregated_at_unix": (
                        snap.aggregated_at_unix
                    ),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "attention_mirror: SSE failed", exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_FOCUS_GLYPHS = {
    AttentionFocus.READING: "📖",
    AttentionFocus.SEARCHING: "🔍",
    AttentionFocus.THINKING: "🤔",
    AttentionFocus.IDLE: "⋯",
}


def format_attention_mirror(
    *, snapshot: Optional[AttentionSnapshot] = None,
    limit: int = 6,
) -> str:
    """Render the attention mirror. Empty when master off."""
    if not master_enabled():
        return ""
    if snapshot is None:
        snapshot = aggregate_attention()
    if not snapshot.items and snapshot.primary_focus is AttentionFocus.IDLE:
        if not master_enabled():
            return ""
        return (
            "[bright_yellow]🪞 Attention mirror:[/]\n"
            "  [dim]idle (no recent attention signals)[/]"
        )
    primary_glyph = _FOCUS_GLYPHS.get(
        snapshot.primary_focus, "⋯",
    )
    parts = [
        f"[bright_yellow]🪞 Attention mirror:[/] "
        f"{primary_glyph} {snapshot.primary_focus.value}",
        f"  [dim](window {snapshot.window_s}s · "
        f"{len(snapshot.items)} signals)[/]",
    ]
    for item in snapshot.items[:limit]:
        glyph = _FOCUS_GLYPHS.get(item.focus, "•")
        op_tag = (
            f" [dim]({item.op_id[:12]})[/]"
            if item.op_id else ""
        )
        parts.append(
            f"  {glyph} {item.summary}{op_tag}"
        )
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry + AST pins
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-5 #16 attention mirror master switch "
            "(default FALSE per §33.1).",
            "false",
        ),
        (
            _ENV_WINDOW_S, "int",
            "Attention window seconds (default 30; "
            "clamped 5..300).",
            "30",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name, type=typ, category="ux",
                description=desc, example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "attention_mirror.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


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
            "section_39_tier5_16_master_default_false"
        ),
        description="§33.1.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "attention_mirror.py"
        ),
        validate=_master,
    ))

    def _focus_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "AttentionFocus"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "READING", "SEARCHING",
                    "THINKING", "IDLE",
                }
                missing = expected - names
                if missing:
                    return [f"missing: {sorted(missing)}"]
                return []
        return ["AttentionFocus not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_16_focus_taxonomy_4_values"
        ),
        description="Closed 4-value AttentionFocus.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "attention_mirror.py"
        ),
        validate=_focus_taxonomy,
    ))

    def _composes_broker(tree, src):
        if (
            "ide_observability_stream" not in src
            or "recent_history" not in src
        ):
            return [
                "must compose canonical broker "
                "recent_history"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_16_composes_broker"
        ),
        description=(
            "Composes canonical SSE broker "
            "recent_history — NO parallel event ring."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "attention_mirror.py"
        ),
        validate=_composes_broker,
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
            "section_39_tier5_16_authority_asymmetry"
        ),
        description="Substrate purity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "attention_mirror.py"
        ),
        validate=_authority,
    ))

    return pins


__all__ = [
    "ATTENTION_MIRROR_SCHEMA_VERSION",
    "AttentionFocus",
    "AttentionItem",
    "AttentionSnapshot",
    "master_enabled",
    "aggregate_attention",
    "format_attention_mirror",
    "register_flags",
    "register_shipped_invariants",
]
