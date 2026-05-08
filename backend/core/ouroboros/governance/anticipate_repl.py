"""``/anticipate`` REPL — §38.11-C operator surface
(PRD v2.66 to v2.67, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention (file ends ``_repl.py``;
verb derived from basename; dispatcher named
``dispatch_anticipate_command``).

Subcommands:
  /anticipate                alias for ``panel``
  /anticipate panel          full panel: banners + pre-fetch
  /anticipate banners [N]    last N intervention banners
  /anticipate prefetch [N]   last N pre-fetch events
  /anticipate status         master flag + counts
  /anticipate help           this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


ANTICIPATE_REPL_SCHEMA_VERSION: str = "anticipate_repl.1"


_HELP = (
    "/anticipate — §38.11-C anticipation surface (PRD)\n"
    "\n"
    "Surfaces the organism's proactive attention:\n"
    "  - Intervention banners: sensor-fired ops above prompt\n"
    "  - Pre-fetch indicator: tool calls about to fire\n"
    "\n"
    "Subcommands:\n"
    "  /anticipate                  alias for /anticipate panel\n"
    "  /anticipate panel            full panel\n"
    "  /anticipate banners [N]      last N banners\n"
    "  /anticipate prefetch [N]     last N pre-fetches\n"
    "  /anticipate status           master flag + counts\n"
    "  /anticipate help             this text\n"
    "\n"
    "Master flag: JARVIS_ANTICIPATION_SURFACE_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class AnticipateReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = ANTICIPATE_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/anticipate"
        or s == "anticipate"
        or s.startswith("/anticipate ")
        or s.startswith("anticipate ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.anticipation_surface import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_anticipate_command(
    line: str,
) -> AnticipateReplDispatchResult:
    """Parse ``/anticipate`` line. NEVER raises."""
    if not _matches(line):
        return AnticipateReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return AnticipateReplDispatchResult(
            ok=False,
            text=f"  /anticipate parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "panel")

    if head in ("help", "?"):
        return AnticipateReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return AnticipateReplDispatchResult(
            ok=False,
            text=(
                "  /anticipate: anticipation surface "
                "disabled (default per §33.1). Set "
                "JARVIS_ANTICIPATION_SURFACE_ENABLED=true."
            ),
        )

    try:
        if head == "panel":
            return _render_panel()
        if head == "banners":
            return _render_banners(
                _parse_limit(args, default=8),
            )
        if head == "prefetch":
            return _render_prefetch(
                _parse_limit(args, default=8),
            )
        if head == "status":
            return _render_status()
        return AnticipateReplDispatchResult(
            ok=False,
            text=(
                f"  /anticipate: unknown subcommand "
                f"{head!r}. Try /anticipate help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return AnticipateReplDispatchResult(
            ok=False,
            text=(
                f"  /anticipate: internal error: "
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


def _render_panel() -> AnticipateReplDispatchResult:
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_anticipation_panel,
    )
    out = format_anticipation_panel()
    if not out:
        return AnticipateReplDispatchResult(
            ok=True,
            text=(
                "# /anticipate — no anticipation signal yet "
                "(no banners + no pre-fetches)"
            ),
        )
    return AnticipateReplDispatchResult(ok=True, text=out)


def _render_banners(limit: int) -> AnticipateReplDispatchResult:
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_intervention_banner_panel,
        get_default_surface,
    )
    banners = get_default_surface().recent_banners(limit=limit)
    out = format_intervention_banner_panel(
        banners=banners, limit=limit,
    )
    if not out:
        return AnticipateReplDispatchResult(
            ok=True,
            text=(
                f"# /anticipate banners (last {limit}) — "
                f"no intervention banners recorded"
            ),
        )
    return AnticipateReplDispatchResult(ok=True, text=out)


def _render_prefetch(limit: int) -> AnticipateReplDispatchResult:
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_prefetch_indicator,
        get_default_surface,
    )
    prefetches = get_default_surface().recent_prefetches(
        limit=limit,
    )
    out = format_prefetch_indicator(
        prefetches=prefetches, limit=limit,
    )
    if not out:
        return AnticipateReplDispatchResult(
            ok=True,
            text=(
                f"# /anticipate prefetch (last {limit}) — "
                f"no pre-fetch events recorded"
            ),
        )
    return AnticipateReplDispatchResult(ok=True, text=out)


def _render_status() -> AnticipateReplDispatchResult:
    from backend.core.ouroboros.governance.anticipation_surface import (
        banners_enabled, get_default_surface, master_enabled,
        prefetch_enabled,
    )
    surface = get_default_surface()
    banners = surface.recent_banners(limit=64)
    prefetches = surface.recent_prefetches(limit=64)
    parts = [
        "# /anticipate status",
        f"  master_enabled    : {master_enabled()}",
        f"  banners_enabled   : {banners_enabled()}",
        f"  prefetch_enabled  : {prefetch_enabled()}",
        f"  banners ring      : {len(banners)} events",
        f"  prefetch ring     : {len(prefetches)} events",
    ]
    return AnticipateReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="anticipate",
            description=(
                "Anticipation surface — proactive "
                "intervention banners + pre-fetch indicator"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "anticipate_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "ANTICIPATE_REPL_SCHEMA_VERSION",
    "AnticipateReplDispatchResult",
    "dispatch_anticipate_command",
    "register_verbs",
]
