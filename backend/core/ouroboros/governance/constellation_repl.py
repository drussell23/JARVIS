"""``/constellation`` REPL — §38.11-F operator surface
(PRD v2.69 to v2.70, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /constellation                       alias for ``panel``
  /constellation panel [N]             panel grouped by axis
  /constellation refresh               recompute snapshot
  /constellation show <flag-name>      detailed star view
  /constellation only <brightness>     filter by brightness
  /constellation status                master flag + counts
  /constellation help                  this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


CONSTELLATION_REPL_SCHEMA_VERSION: str = "constellation_repl.1"


_HELP = (
    "/constellation — §38.11-F capability constellation (PRD)\n"
    "\n"
    "Renders the system's flag landscape as a star-map:\n"
    "each flag is a star, brightness encodes graduation\n"
    "readiness, axes group flags by canonical Category.\n"
    "\n"
    "Brightness vocabulary (1:1 with UnifiedGraduationVerdict):\n"
    "  ⭐ RADIANT   ready (eligible for default-true flip)\n"
    "  ✦ GLOWING   evidence_gathering\n"
    "  · DIM       evidence_insufficient\n"
    "  ⚠ FAULTING  evidence_failed\n"
    "  ○ DARK      disabled\n"
    "\n"
    "Subcommands:\n"
    "  /constellation                  alias for /panel\n"
    "  /constellation panel [N]        panel grouped by axis\n"
    "  /constellation refresh          recompute snapshot\n"
    "  /constellation show <flag>      detailed star view\n"
    "  /constellation only <bright>    filter by brightness\n"
    "  /constellation status           master flag + counts\n"
    "  /constellation help             this text\n"
    "\n"
    "Master flag: JARVIS_CAPABILITY_CONSTELLATION_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class ConstellationReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = CONSTELLATION_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/constellation"
        or s == "constellation"
        or s.startswith("/constellation ")
        or s.startswith("constellation ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.capability_constellation import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_constellation_command(
    line: str,
) -> ConstellationReplDispatchResult:
    if not _matches(line):
        return ConstellationReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                f"  /constellation parse error: {exc}"
            ),
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "panel")

    if head in ("help", "?"):
        return ConstellationReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                "  /constellation: capability constellation "
                "disabled (default per §33.1). Set "
                "JARVIS_CAPABILITY_CONSTELLATION_ENABLED=true."
            ),
        )

    try:
        if head == "panel":
            return _render_panel(_parse_int(args, default=5))
        if head == "refresh":
            return _refresh()
        if head == "show":
            return _render_show(
                args[1] if len(args) >= 2 else "",
            )
        if head == "only":
            return _render_only(
                args[1] if len(args) >= 2 else "",
            )
        if head == "status":
            return _render_status()
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                f"  /constellation: unknown subcommand "
                f"{head!r}. Try /constellation help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                f"  /constellation: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_int(args, *, default: int) -> int:
    if len(args) <= 1:
        return default
    try:
        n = int(args[1])
        return max(1, min(n, 50))
    except (TypeError, ValueError):
        return default


def _render_panel(
    limit_per_axis: int,
) -> ConstellationReplDispatchResult:
    from backend.core.ouroboros.governance.capability_constellation import (
        format_constellation_panel,
    )
    out = format_constellation_panel(
        limit_per_axis=limit_per_axis,
    )
    if not out:
        return ConstellationReplDispatchResult(
            ok=True,
            text=(
                "# /constellation — no stars yet "
                "(canonical sources unavailable or empty)"
            ),
        )
    return ConstellationReplDispatchResult(
        ok=True, text=out,
    )


def _refresh() -> ConstellationReplDispatchResult:
    from backend.core.ouroboros.governance.capability_constellation import (
        aggregate_constellation,
    )
    snap = aggregate_constellation()
    parts = [
        "# /constellation refresh",
        f"  aggregated_at : {snap.aggregated_at_unix:.0f}",
        f"  elapsed_s     : {snap.elapsed_s:.3f}",
        f"  total stars   : {len(snap.stars)}",
    ]
    return ConstellationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_show(flag_name: str) -> ConstellationReplDispatchResult:
    if not flag_name:
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                "  /constellation show: flag-name required"
            ),
        )
    from backend.core.ouroboros.governance.capability_constellation import (
        aggregate_constellation, get_cached_snapshot,
    )
    snap = get_cached_snapshot() or aggregate_constellation()
    star = next(
        (s for s in snap.stars if s.flag_name == flag_name),
        None,
    )
    if star is None:
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                f"  /constellation show: no star with name "
                f"{flag_name!r}"
            ),
        )
    parts = [
        f"# Star {star.flag_name}",
        f"  brightness     : {star.brightness.value}",
        f"  graduation     : {star.graduation_verdict}",
        f"  category       : {star.category}",
        f"  diagnostic     : {star.diagnostic}",
    ]
    if star.linked_principles:
        parts.append("  linked_principles:")
        for p in star.linked_principles:
            parts.append(f"    - {p}")
    if star.posture_relevance:
        parts.append("  posture_relevance:")
        for p in star.posture_relevance:
            parts.append(f"    - {p}")
    return ConstellationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_only(
    brightness_str: str,
) -> ConstellationReplDispatchResult:
    if not brightness_str:
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                "  /constellation only: brightness arg "
                "required (radiant|glowing|dim|faulting|dark)"
            ),
        )
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, format_constellation_panel,
    )
    s = brightness_str.strip().lower()
    target = next(
        (b for b in ConstellationBrightness if b.value == s),
        None,
    )
    if target is None:
        return ConstellationReplDispatchResult(
            ok=False,
            text=(
                f"  /constellation only: invalid brightness "
                f"{brightness_str!r}. Use one of: radiant, "
                "glowing, dim, faulting, dark"
            ),
        )
    out = format_constellation_panel(only_brightness=target)
    if not out:
        return ConstellationReplDispatchResult(
            ok=True,
            text=(
                f"# /constellation only {target.value} — "
                f"no stars match"
            ),
        )
    return ConstellationReplDispatchResult(
        ok=True, text=out,
    )


def _render_status() -> ConstellationReplDispatchResult:
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, auto_refresh_enabled,
        get_cached_snapshot, master_enabled, panel_enabled,
    )
    snap = get_cached_snapshot()
    parts = ["# /constellation status"]
    parts.append(f"  master_enabled       : {master_enabled()}")
    parts.append(f"  panel_enabled        : {panel_enabled()}")
    parts.append(
        f"  auto_refresh_enabled : {auto_refresh_enabled()}"
    )
    if snap is None:
        parts.append("  cached_snapshot      : (none)")
    else:
        parts.append(
            f"  cached snapshot      : "
            f"{len(snap.stars)} stars, "
            f"aggregated_at={snap.aggregated_at_unix:.0f}"
        )
        parts.append("  by brightness:")
        for b in ConstellationBrightness:
            count = snap.by_brightness.get(b.value, 0)
            parts.append(f"    {b.value:<14} : {count}")
    return ConstellationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="constellation",
            description=(
                "Capability constellation — flag star-map "
                "grouped by axis, brightness encodes "
                "graduation readiness"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "constellation_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "CONSTELLATION_REPL_SCHEMA_VERSION",
    "ConstellationReplDispatchResult",
    "dispatch_constellation_command",
    "register_verbs",
]
