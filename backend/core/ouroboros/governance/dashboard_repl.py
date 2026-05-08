"""``/dashboard`` REPL — §39 Tier-2 #1 operator surface
(PRD v2.71 to v2.72, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /dashboard                       alias for ``show``
  /dashboard show [pane ...]       full dashboard
  /dashboard compact               one-line summaries
  /dashboard pane <name>           single pane only
  /dashboard list                  enumerate panes
  /dashboard heatmap               focus heatmap pane
  /dashboard status                master flag + counts
  /dashboard help                  this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


DASHBOARD_REPL_SCHEMA_VERSION: str = "dashboard_repl.1"


_HELP = (
    "/dashboard — §39 Tier-2 #1 organism dashboard (PRD)\n"
    "\n"
    "Mission Control multi-pane view composing ALL §38 +\n"
    "§38.11 + §39 Tier-1 render surfaces.\n"
    "\n"
    "Panes (8): alive · activity_radar · fanout · graduation\n"
    "          posture · phase_ribbon · heatmap · constellation\n"
    "\n"
    "Subcommands:\n"
    "  /dashboard                         alias for show\n"
    "  /dashboard show [pane ...]         full dashboard\n"
    "  /dashboard compact                 one-line layout\n"
    "  /dashboard pane <name>             single pane\n"
    "  /dashboard list                    enumerate panes\n"
    "  /dashboard heatmap                 focus heatmap\n"
    "  /dashboard status                  master flag + counts\n"
    "  /dashboard help                    this text\n"
    "\n"
    "Master flag: JARVIS_ORGANISM_DASHBOARD_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class DashboardReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = DASHBOARD_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/dashboard"
        or s == "dashboard"
        or s.startswith("/dashboard ")
        or s.startswith("dashboard ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.organism_dashboard import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_dashboard_command(
    line: str,
) -> DashboardReplDispatchResult:
    if not _matches(line):
        return DashboardReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return DashboardReplDispatchResult(
            ok=False,
            text=f"  /dashboard parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "show")

    if head in ("help", "?"):
        return DashboardReplDispatchResult(
            ok=True, text=_HELP,
        )

    if head == "list":
        return _render_list()

    if not _master_enabled():
        return DashboardReplDispatchResult(
            ok=False,
            text=(
                "  /dashboard: organism dashboard disabled "
                "(default per §33.1). Set "
                "JARVIS_ORGANISM_DASHBOARD_ENABLED=true."
            ),
        )

    try:
        if head == "show":
            return _render_show(
                args[1:], compact=False,
            )
        if head == "compact":
            return _render_show(
                args[1:], compact=True,
            )
        if head == "pane":
            return _render_pane(
                args[1] if len(args) >= 2 else "",
            )
        if head == "heatmap":
            return _render_pane("heatmap")
        if head == "status":
            return _render_status()
        return DashboardReplDispatchResult(
            ok=False,
            text=(
                f"  /dashboard: unknown subcommand "
                f"{head!r}. Try /dashboard help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardReplDispatchResult(
            ok=False,
            text=(
                f"  /dashboard: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_list() -> DashboardReplDispatchResult:
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane,
    )
    parts = ["# /dashboard list — 8 canonical panes"]
    for p in DashboardPane:
        parts.append(f"  - {p.value}")
    return DashboardReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_show(
    pane_args, *, compact: bool,
) -> DashboardReplDispatchResult:
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, format_organism_dashboard,
    )
    panes = None
    if pane_args:
        resolved = []
        unknown = []
        for raw in pane_args:
            p = DashboardPane.coerce(raw)
            if p is None:
                unknown.append(raw)
            else:
                resolved.append(p)
        if unknown:
            return DashboardReplDispatchResult(
                ok=False,
                text=(
                    f"  /dashboard: unknown panes "
                    f"{unknown}. Try /dashboard list."
                ),
            )
        panes = tuple(resolved) if resolved else None

    # Compact mode honors layout via env (caller can also
    # set JARVIS_ORGANISM_DASHBOARD_LAYOUT=compact). For
    # /dashboard compact we override pane-rendering.
    if compact:
        import os as _os
        _saved = _os.environ.get(
            "JARVIS_ORGANISM_DASHBOARD_LAYOUT",
        )
        _os.environ["JARVIS_ORGANISM_DASHBOARD_LAYOUT"] = (
            "compact"
        )
        try:
            out = format_organism_dashboard(panes=panes)
        finally:
            if _saved is None:
                _os.environ.pop(
                    "JARVIS_ORGANISM_DASHBOARD_LAYOUT", None,
                )
            else:
                _os.environ[
                    "JARVIS_ORGANISM_DASHBOARD_LAYOUT"
                ] = _saved
    else:
        out = format_organism_dashboard(panes=panes)

    if not out:
        return DashboardReplDispatchResult(
            ok=True,
            text=(
                "# /dashboard — no panes rendered "
                "(canonical sub-surfaces all empty)"
            ),
        )
    return DashboardReplDispatchResult(ok=True, text=out)


def _render_pane(pane_name: str) -> DashboardReplDispatchResult:
    if not pane_name:
        return DashboardReplDispatchResult(
            ok=False,
            text=(
                "  /dashboard pane: pane name required. "
                "Try /dashboard list."
            ),
        )
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, format_organism_dashboard,
    )
    p = DashboardPane.coerce(pane_name)
    if p is None:
        return DashboardReplDispatchResult(
            ok=False,
            text=(
                f"  /dashboard pane: unknown pane "
                f"{pane_name!r}. Try /dashboard list."
            ),
        )
    out = format_organism_dashboard(panes=(p,))
    if not out:
        return DashboardReplDispatchResult(
            ok=True,
            text=(
                f"# /dashboard pane {p.value} — no content "
                "(canonical sub-surface returned empty)"
            ),
        )
    return DashboardReplDispatchResult(ok=True, text=out)


def _render_status() -> DashboardReplDispatchResult:
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, aggregate_dashboard, master_enabled,
    )
    parts = ["# /dashboard status"]
    parts.append(
        f"  master_enabled : {master_enabled()}"
    )
    snap = aggregate_dashboard()
    parts.append(
        f"  pane count     : {len(DashboardPane)}"
    )
    parts.append(
        f"  rendered now   : "
        f"{len(snap.rendered_panes)}"
    )
    parts.append(
        f"  last elapsed   : {snap.elapsed_s * 1000:.1f}ms"
    )
    parts.append("  pane sizes:")
    for p in DashboardPane:
        text = snap.rendered_panes.get(p.value, "")
        parts.append(
            f"    {p.value:<14} : "
            f"{len(text)} chars"
        )
    return DashboardReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="dashboard",
            description=(
                "Living organism dashboard — Mission "
                "Control multi-pane view of WHOLE state"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "dashboard_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "DASHBOARD_REPL_SCHEMA_VERSION",
    "DashboardReplDispatchResult",
    "dispatch_dashboard_command",
    "register_verbs",
]
