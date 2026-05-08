"""``/introspect`` REPL — §38.11-D operator surface
(PRD v2.67 to v2.68, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /introspect                 alias for ``panel``
  /introspect panel [op-id]   show 4-axis introspection panel
  /introspect dream <text>    emit a test DREAM frame
                              (debugging only; bypasses
                              DreamEngine — useful for
                              wiring verification)
  /introspect status          master flag + counts
  /introspect help            this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


INTROSPECT_REPL_SCHEMA_VERSION: str = "introspect_repl.1"


_HELP = (
    "/introspect — §38.11-D introspective voice (PRD)\n"
    "\n"
    "Surfaces the model's introspective voice across 4 axes:\n"
    "  - INTENT             (proactive 'I'm going to do X')\n"
    "  - THINKING           (reasoning tokens)\n"
    "  - SELF_CORRECTION    (L2 repair prose)\n"
    "  - DREAM              (DreamEngine speculative prose)\n"
    "\n"
    "Subcommands:\n"
    "  /introspect                      panel for any op\n"
    "  /introspect panel [op-id]        panel scoped to op\n"
    "  /introspect dream <text>         emit test DREAM frame\n"
    "  /introspect status               master flag + counts\n"
    "  /introspect help                 this text\n"
    "\n"
    "Master flag: JARVIS_INTROSPECTIVE_VOICE_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class IntrospectReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = INTROSPECT_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/introspect"
        or s == "introspect"
        or s.startswith("/introspect ")
        or s.startswith("introspect ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.introspective_voice import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_introspect_command(
    line: str,
) -> IntrospectReplDispatchResult:
    if not _matches(line):
        return IntrospectReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return IntrospectReplDispatchResult(
            ok=False,
            text=f"  /introspect parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "panel")

    if head in ("help", "?"):
        return IntrospectReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return IntrospectReplDispatchResult(
            ok=False,
            text=(
                "  /introspect: introspective voice disabled "
                "(default per §33.1). Set "
                "JARVIS_INTROSPECTIVE_VOICE_ENABLED=true."
            ),
        )

    try:
        if head == "panel":
            op_id = args[1] if len(args) >= 2 else None
            return _render_panel(op_id=op_id)
        if head == "dream":
            text = " ".join(args[1:]).strip()
            return _emit_test_dream(text)
        if head == "status":
            return _render_status()
        return IntrospectReplDispatchResult(
            ok=False,
            text=(
                f"  /introspect: unknown subcommand "
                f"{head!r}. Try /introspect help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return IntrospectReplDispatchResult(
            ok=False,
            text=(
                f"  /introspect: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_panel(*, op_id) -> IntrospectReplDispatchResult:
    from backend.core.ouroboros.governance.introspective_voice import (
        format_introspective_voice_panel,
    )
    out = format_introspective_voice_panel(op_id=op_id)
    if not out:
        scope = f" for op {op_id}" if op_id else ""
        return IntrospectReplDispatchResult(
            ok=True,
            text=(
                f"# /introspect panel{scope} — no committed "
                f"introspection frames yet"
            ),
        )
    return IntrospectReplDispatchResult(ok=True, text=out)


def _emit_test_dream(text: str) -> IntrospectReplDispatchResult:
    if not text:
        return IntrospectReplDispatchResult(
            ok=False,
            text="  /introspect dream: text required",
        )
    from backend.core.ouroboros.governance.introspective_voice import (
        emit_dream_prose,
    )
    op_id = "test-dream"
    ok = emit_dream_prose(
        op_id=op_id,
        prose=text,
        provider="repl",
    )
    if ok:
        return IntrospectReplDispatchResult(
            ok=True,
            text=(
                f"# /introspect dream — emitted DREAM frame "
                f"for op '{op_id}' ({len(text)} chars)"
            ),
        )
    return IntrospectReplDispatchResult(
        ok=False,
        text=(
            "  /introspect dream: emit failed (sub-flag off "
            "or canonical channel unavailable)"
        ),
    )


def _render_status() -> IntrospectReplDispatchResult:
    from backend.core.ouroboros.governance.introspective_voice import (
        aggregate_introspection_frames,
        dream_bridge_enabled, master_enabled,
        panel_enabled,
    )
    parts = ["# /introspect status"]
    parts.append(f"  master_enabled       : {master_enabled()}")
    parts.append(f"  dream_bridge_enabled : {dream_bridge_enabled()}")
    parts.append(f"  panel_enabled        : {panel_enabled()}")
    frames = aggregate_introspection_frames(limit_per_axis=8)
    parts.append(
        f"  total introspection  : {len(frames)} committed frames"
    )
    by_axis = {}
    for f in frames:
        by_axis[f.axis.value] = by_axis.get(f.axis.value, 0) + 1
    for axis_value, n in sorted(by_axis.items()):
        parts.append(f"    {axis_value:<18} : {n}")
    return IntrospectReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="introspect",
            description=(
                "Introspective voice — INTENT + THINKING + "
                "SELF_CORRECTION + DREAM panel"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "introspect_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "INTROSPECT_REPL_SCHEMA_VERSION",
    "IntrospectReplDispatchResult",
    "dispatch_introspect_command",
    "register_verbs",
]
