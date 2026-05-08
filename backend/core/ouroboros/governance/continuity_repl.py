"""``/continuity`` REPL dispatcher — §38.11-B operator surface
(PRD v2.65 to v2.66, 2026-05-07).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via the §33.3 naming-cage convention (file ends ``_repl.py``;
verb derived from basename; dispatcher named
``dispatch_continuity_command``).

## Subcommands

  * ``/continuity``           alias for ``panel``
  * ``/continuity panel``     full panel: diff + ticker
  * ``/continuity diff``      cross-session diff only
  * ``/continuity ticker``    graduation ticker only
  * ``/continuity history``   last N graduation events
  * ``/continuity status``    master flag + counts
  * ``/continuity help``      this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


CONTINUITY_REPL_SCHEMA_VERSION: str = "continuity_repl.1"


_HELP = (
    "/continuity — §38.11-B session continuity (PRD)\n"
    "\n"
    "Surfaces cross-session continuity awareness:\n"
    "  - Graduation ticker: capability flags transitioning\n"
    "    to READY (eligible for default-true flip)\n"
    "  - Cross-session memory diff: what changed since last\n"
    "    session\n"
    "\n"
    "Subcommands:\n"
    "  /continuity                alias for /continuity panel\n"
    "  /continuity panel          full panel (diff + ticker)\n"
    "  /continuity diff           cross-session diff only\n"
    "  /continuity ticker         graduation ticker only\n"
    "  /continuity history [N]    last N graduation events\n"
    "  /continuity status         master flag + counts\n"
    "  /continuity help           this text\n"
    "\n"
    "Master flag: JARVIS_SESSION_CONTINUITY_ENABLED (default false).\n"
)


@dataclass(frozen=True)
class ContinuityReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = CONTINUITY_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/continuity"
        or s == "continuity"
        or s.startswith("/continuity ")
        or s.startswith("continuity ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.session_continuity import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_continuity_command(
    line: str,
) -> ContinuityReplDispatchResult:
    """Parse ``/continuity`` line. NEVER raises."""
    if not _matches(line):
        return ContinuityReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ContinuityReplDispatchResult(
            ok=False,
            text=f"  /continuity parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "panel")

    if head in ("help", "?"):
        return ContinuityReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return ContinuityReplDispatchResult(
            ok=False,
            text=(
                "  /continuity: session continuity disabled "
                "(default per §33.1). Set "
                "JARVIS_SESSION_CONTINUITY_ENABLED=true."
            ),
        )

    try:
        if head == "panel":
            return _render_panel()
        if head == "diff":
            return _render_diff()
        if head == "ticker":
            return _render_ticker()
        if head == "history":
            return _render_history(
                _parse_limit(args, default=10),
            )
        if head == "status":
            return _render_status()
        return ContinuityReplDispatchResult(
            ok=False,
            text=(
                f"  /continuity: unknown subcommand "
                f"{head!r}. Try /continuity help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return ContinuityReplDispatchResult(
            ok=False,
            text=(
                f"  /continuity: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_limit(args, *, default: int) -> int:
    if len(args) <= 1:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > 64:
            return 64
        return n
    except (TypeError, ValueError):
        return default


def _render_panel() -> ContinuityReplDispatchResult:
    from backend.core.ouroboros.governance.session_continuity import (
        format_session_continuity_panel,
    )
    out = format_session_continuity_panel()
    if not out:
        return ContinuityReplDispatchResult(
            ok=True,
            text=(
                "# /continuity — no continuity signal yet "
                "(no prior session + no graduation transitions)"
            ),
        )
    return ContinuityReplDispatchResult(
        ok=True, text=out,
    )


def _render_diff() -> ContinuityReplDispatchResult:
    from backend.core.ouroboros.governance.session_continuity import (
        format_cross_session_diff,
    )
    out = format_cross_session_diff()
    if not out:
        return ContinuityReplDispatchResult(
            ok=True,
            text=(
                "# /continuity diff — no previous session "
                "loadable"
            ),
        )
    return ContinuityReplDispatchResult(
        ok=True, text=out,
    )


def _render_ticker() -> ContinuityReplDispatchResult:
    from backend.core.ouroboros.governance.session_continuity import (
        format_graduation_ticker,
    )
    out = format_graduation_ticker()
    if not out:
        return ContinuityReplDispatchResult(
            ok=True,
            text=(
                "# /continuity ticker — no graduation "
                "transitions in this tick"
            ),
        )
    return ContinuityReplDispatchResult(
        ok=True, text=out,
    )


def _render_history(limit: int) -> ContinuityReplDispatchResult:
    from backend.core.ouroboros.governance.session_continuity import (
        get_default_ticker,
    )
    ticker = get_default_ticker()
    events = ticker.history(limit=limit)
    if not events:
        return ContinuityReplDispatchResult(
            ok=True,
            text=(
                f"# /continuity history (last {limit}) — "
                f"no events recorded yet"
            ),
        )
    parts = [
        f"# /continuity history (last {len(events)})",
    ]
    for ev in events:
        parts.append(
            f"  {ev.transition.value:<14} "
            f"{ev.flag_name} "
            f"({ev.previous_verdict!r} -> "
            f"{ev.current_verdict!r})"
        )
    return ContinuityReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_status() -> ContinuityReplDispatchResult:
    from backend.core.ouroboros.governance.session_continuity import (
        master_enabled, get_default_ticker,
        aggregate_cross_session_diff,
    )
    parts = ["# /continuity status"]
    parts.append(f"  master_enabled    : {master_enabled()}")
    ticker = get_default_ticker()
    history = ticker.history(limit=64)
    parts.append(f"  ticker history    : {len(history)} events")
    diff = aggregate_cross_session_diff()
    parts.append(
        f"  has prev session  : {diff.has_previous}"
    )
    if diff.has_previous:
        sid_short = (
            diff.previous_session_id[-12:]
            if len(diff.previous_session_id) > 12
            else diff.previous_session_id
        )
        parts.append(
            f"  prev session id   : {sid_short}"
        )
    return ContinuityReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="continuity",
            description=(
                "Session continuity — graduation ticker + "
                "cross-session memory diff"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "continuity_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "CONTINUITY_REPL_SCHEMA_VERSION",
    "ContinuityReplDispatchResult",
    "dispatch_continuity_command",
    "register_verbs",
]
