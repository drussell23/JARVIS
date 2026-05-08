"""``/ribbon`` REPL — §39 Tier-1 #14 operator surface
(PRD v2.70 to v2.71, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /ribbon                          alias for ``show``
  /ribbon show [active-phase]      compact ribbon
  /ribbon expand [active-phase]    multi-line w/ labels
  /ribbon refresh                  recompute snapshot
  /ribbon status                   master flag + counts
  /ribbon help                     this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


RIBBON_REPL_SCHEMA_VERSION: str = "ribbon_repl.1"


_HELP = (
    "/ribbon — §39 Tier-1 #14 phase-flow ribbon (PRD)\n"
    "\n"
    "Renders the canonical 11-phase forward-flow as a\n"
    "horizontal ribbon with per-phase density markers.\n"
    "\n"
    "Density vocabulary (5 buckets):\n"
    "  · IDLE       0 ops in window\n"
    "  • LIGHT      1 op\n"
    "  ● STEADY     2-3 ops\n"
    "  ◉ HEAVY      4-7 ops\n"
    "  ★ SATURATED  8+ ops\n"
    "\n"
    "Subcommands:\n"
    "  /ribbon                          alias for show\n"
    "  /ribbon show [phase]             compact ribbon\n"
    "  /ribbon expand [phase]           multi-line view\n"
    "  /ribbon refresh                  recompute snapshot\n"
    "  /ribbon status                   master flag + counts\n"
    "  /ribbon help                     this text\n"
    "\n"
    "Master flag: JARVIS_PHASE_FLOW_RIBBON_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class RibbonReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = RIBBON_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/ribbon"
        or s == "ribbon"
        or s.startswith("/ribbon ")
        or s.startswith("ribbon ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.phase_flow_ribbon import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_ribbon_command(
    line: str,
) -> RibbonReplDispatchResult:
    if not _matches(line):
        return RibbonReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return RibbonReplDispatchResult(
            ok=False,
            text=f"  /ribbon parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "show")

    if head in ("help", "?"):
        return RibbonReplDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return RibbonReplDispatchResult(
            ok=False,
            text=(
                "  /ribbon: phase-flow ribbon disabled "
                "(default per §33.1). Set "
                "JARVIS_PHASE_FLOW_RIBBON_ENABLED=true."
            ),
        )

    try:
        if head == "show":
            return _render(
                args[1] if len(args) >= 2 else None,
                compact=True,
            )
        if head == "expand":
            return _render(
                args[1] if len(args) >= 2 else None,
                compact=False,
            )
        if head == "refresh":
            return _refresh()
        if head == "status":
            return _render_status()
        return RibbonReplDispatchResult(
            ok=False,
            text=(
                f"  /ribbon: unknown subcommand "
                f"{head!r}. Try /ribbon help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return RibbonReplDispatchResult(
            ok=False,
            text=(
                f"  /ribbon: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render(
    active_phase, *, compact: bool,
) -> RibbonReplDispatchResult:
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        format_phase_flow_ribbon,
    )
    out = format_phase_flow_ribbon(
        active_phase=active_phase,
        compact=compact,
    )
    if not out:
        return RibbonReplDispatchResult(
            ok=True,
            text=(
                "# /ribbon — canonical forward-flow "
                "unavailable (substrate import failed)"
            ),
        )
    return RibbonReplDispatchResult(ok=True, text=out)


def _refresh() -> RibbonReplDispatchResult:
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        aggregate_phase_flow,
    )
    snap = aggregate_phase_flow()
    parts = [
        "# /ribbon refresh",
        f"  aggregated_at_unix : {snap.aggregated_at_unix:.0f}",
        f"  window_s           : {snap.window_s}",
        f"  cells              : {len(snap.cells)}",
        f"  active_phase_name  : {snap.active_phase_name or '(none)'}",
    ]
    return RibbonReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_status() -> RibbonReplDispatchResult:
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, animation_enabled,
        density_enabled, get_cached_snapshot,
        master_enabled,
    )
    snap = get_cached_snapshot()
    parts = ["# /ribbon status"]
    parts.append(f"  master_enabled      : {master_enabled()}")
    parts.append(f"  density_enabled     : {density_enabled()}")
    parts.append(f"  animation_enabled   : {animation_enabled()}")
    if snap is None:
        parts.append("  cached_snapshot     : (none)")
    else:
        parts.append(
            f"  cached snapshot     : "
            f"{len(snap.cells)} cells, "
            f"window={snap.window_s}s"
        )
        parts.append("  by density:")
        for d in DensityLevel:
            count = snap.by_density.get(d.value, 0)
            parts.append(f"    {d.value:<11} : {count}")
    return RibbonReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="ribbon",
            description=(
                "Phase-flow ribbon — animated 11-phase "
                "forward-flow with per-phase density"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "ribbon_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "RIBBON_REPL_SCHEMA_VERSION",
    "RibbonReplDispatchResult",
    "dispatch_ribbon_command",
    "register_verbs",
]
