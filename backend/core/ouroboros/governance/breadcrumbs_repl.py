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
import time
from dataclasses import dataclass

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_RED = "\033[31m"
_YELLOW = "\033[33m"

_DEFAULT_TAIL = 20
_MAX_TAIL = 200
_SEV_WORDS = {"critical": 3, "important": 2, "info": 1, "verbose": 0, "all": 0}
_ANSI_BY_SEV = {3: _RED + _BOLD, 2: _YELLOW, 1: _CYAN, 0: _DIM}
_GLYPH_BY_SEV = {3: "✖", 2: "▲", 1: "·", 0: "·"}


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
    f"    {_CYAN}/breadcrumbs{_RESET}             {_DIM}show the current level{_RESET}\n\n"
    f"  {_BOLD}Review:{_RESET}\n"
    f"    {_CYAN}/breadcrumbs tail [N]{_RESET}   {_DIM}scroll back the last N events (max 200){_RESET}\n"
    f"    {_CYAN}/breadcrumbs tail critical{_RESET}  {_DIM}filter the tail by severity{_RESET}\n"
    f"    {_CYAN}/breadcrumbs tail <category>{_RESET} {_DIM}provider/governance/memory/cost/model/…{_RESET}\n"
)


def _fmt_age(ts: float, now: float) -> str:
    d = max(0.0, now - ts)
    if d < 90:
        return f"{int(d)}s"
    if d < 5400:
        return f"{int(d / 60)}m"
    return f"{d / 3600:.1f}h"


def _render_tail(args) -> str:
    """Scroll back the unified event history the live router feeds. Never raises."""
    try:
        from backend.core.ouroboros.governance.event_history_buffer import (
            get_default_history,
        )
        from backend.core.ouroboros.governance.event_breadcrumb_registry import (
            build_default_registry,
        )
    except Exception:  # noqa: BLE001
        return f"  {_DIM}event history unavailable{_RESET}"

    n, min_sev, category = _DEFAULT_TAIL, None, None
    for a in args:
        al = a.lower()
        if al.isdigit():
            n = min(_MAX_TAIL, max(1, int(al)))
        elif al in _SEV_WORDS:
            min_sev = _SEV_WORDS[al] if al != "all" else None
        else:
            category = al

    hist = get_default_history()
    recs = hist.recent(n, min_severity=min_sev, category=category)
    if not recs:
        filt = " matching filter" if (min_sev is not None or category) else ""
        return f"  {_DIM}no events yet{filt} (history size {hist.size()}){_RESET}"

    reg = build_default_registry()
    now = time.time()
    out = [
        f"  {_BOLD}{_CYAN}recent events{_RESET} "
        f"{_DIM}(newest first, {len(recs)} of {hist.size()} buffered){_RESET}"
    ]
    for rec in recs:
        try:
            _sev, text = reg.render(rec.event_type, rec.payload)
            col = _ANSI_BY_SEV.get(rec.severity, _CYAN)
            glyph = _GLYPH_BY_SEV.get(rec.severity, "·")
            out.append(
                f"    {_DIM}{_fmt_age(rec.ts, now):>4}{_RESET}  {col}{glyph}{_RESET} {text}"
            )
        except Exception:  # noqa: BLE001
            out.append(f"    {_DIM}?  {rec.event_type}{_RESET}")
    return "\n".join(out)


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
    if head in ("tail", "history", "log"):
        return BreadcrumbsReplDispatchResult(ok=True, text=_render_tail(args[1:]))
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
