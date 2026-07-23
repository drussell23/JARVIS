"""``/breadcrumbs`` — operator control for the unified live event feed.

The TUI's single event-breadcrumb router surfaces the entire backend event
surface (~149 broker event types) through the descriptor registry. This verb
sets the verbosity floor (off / critical / important / info / all) and shows the
current setting. Read/write of a module-level knob only — no orchestrator /
providers / iron_gate imports (naming-cage authority). Auto-discovered via the
module-level ``dispatch_breadcrumbs_command``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class BreadcrumbsReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_LEVELS = ("off", "critical", "important", "info", "all")

_HELP = (
    f"  {_BOLD}{_CYAN}/breadcrumbs — live event feed verbosity{_RESET}\n"
    f"  {_DIM}One router surfaces the whole backend event surface; this sets how "
    f"much of it prints inline.{_RESET}\n\n"
    f"  {_BOLD}Levels:{_RESET}\n"
    f"    {_CYAN}/breadcrumbs off{_RESET}         {_DIM}silence the live feed{_RESET}\n"
    f"    {_CYAN}/breadcrumbs critical{_RESET}    {_DIM}only trips / exhaustion / anomalies{_RESET}\n"
    f"    {_CYAN}/breadcrumbs important{_RESET}   {_DIM}+ throttles, drift, degradation (default){_RESET}\n"
    f"    {_CYAN}/breadcrumbs info{_RESET}        {_DIM}+ posture, plans, learning{_RESET}\n"
    f"    {_CYAN}/breadcrumbs all{_RESET}         {_DIM}everything (incl. unknown/new events){_RESET}\n"
    f"    {_CYAN}/breadcrumbs{_RESET}             {_DIM}show the current level{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    return s in ("/breadcrumbs", "breadcrumbs") or s.startswith(("/breadcrumbs ", "breadcrumbs "))


def _status_text() -> str:
    try:
        from backend.core.ouroboros.governance.event_breadcrumb_registry import (
            get_min_severity, severity_name,
        )
        lvl = severity_name(get_min_severity())
    except Exception:  # noqa: BLE001
        lvl = "?"
    return (
        f"  {_BOLD}Live event feed:{_RESET} {_CYAN}{lvl}{_RESET}\n"
        f"  {_DIM}one router → the whole broker surface; {' / '.join(_LEVELS)} "
        f"(/breadcrumbs <level>){_RESET}"
    )


def dispatch_breadcrumbs_command(line: str) -> BreadcrumbsReplDispatchResult:
    """Parse ``/breadcrumbs`` and set/show the feed verbosity. NEVER raises."""
    if not _matches(line):
        return BreadcrumbsReplDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return BreadcrumbsReplDispatchResult(ok=False, text=f"  /breadcrumbs parse error: {exc}")
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return BreadcrumbsReplDispatchResult(ok=True, text=_HELP)
    if head == "":
        return BreadcrumbsReplDispatchResult(ok=True, text=_status_text())
    if head in ("status",):
        return BreadcrumbsReplDispatchResult(ok=True, text=_status_text())
    if head in _LEVELS:
        try:
            from backend.core.ouroboros.governance.event_breadcrumb_registry import (
                set_min_severity, severity_name, get_min_severity,
            )
            set_min_severity(head)
            return BreadcrumbsReplDispatchResult(
                ok=True,
                text=f"  live event feed → {_CYAN}{severity_name(get_min_severity())}{_RESET}",
            )
        except Exception:  # noqa: BLE001
            return BreadcrumbsReplDispatchResult(ok=False, text="  /breadcrumbs: registry unavailable")
    return BreadcrumbsReplDispatchResult(
        ok=False,
        text=f"  /breadcrumbs: unknown level {head!r} — one of {', '.join(_LEVELS)}",
    )


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="breadcrumbs",
            description=(
                "Live event-feed verbosity — the single router that surfaces the "
                "whole backend event surface (149 broker event types) inline. "
                "off / critical / important / info / all."
            ),
            posture_relevance="RELEVANT",
            since="Unified event breadcrumb router (2026-07-23)",
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "BreadcrumbsReplDispatchResult",
    "dispatch_breadcrumbs_command",
    "register_verbs",
]
