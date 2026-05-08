"""``/radar`` REPL dispatcher — Activity radar operator surface
(PRD §38 Slice 4, 2026-05-07).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via the §33.3 naming-cage convention:

  * file ends ``_repl.py`` → verb derived from basename
  * exposes module-level ``dispatch_radar_command(line)``
  * SerpentREPL routes any line matching ``/radar`` /
    ``radar`` / ``/radar …`` / ``radar …`` here zero-edit.

## Subcommands

  * ``/radar``                 alias for ``show``
  * ``/radar show [N]``        render radar over last N seconds
                               (default = JARVIS_ACTIVITY_RADAR_WINDOW_S)
  * ``/radar categories``      one-line summary by category
  * ``/radar status``          master flag + window + counts
  * ``/radar help``            this text

## Architectural locks

  1. **Composes canonical aggregator** — invokes
     :func:`aggregate_activity` + :func:`format_activity_radar`.
     NO parallel rendering / NO direct broker access.
  2. **Read-only** — radar is observe-only; no mutation.
  3. **Master-flag-bypass for /help** — discoverability path
     always works.
  4. **Authority asymmetry** — imports stdlib +
     activity_radar ONLY.
  5. **NEVER raises** — defensive on every code path.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


RADAR_REPL_SCHEMA_VERSION: str = "radar_repl.1"


_HELP = (
    "/radar — Live activity radar (PRD §38 Slice 4)\n"
    "\n"
    "Surfaces 60-second sliding-window activity across O+V's\n"
    "16 sensors, 5 contexts, autonomy bridges, governance,\n"
    "and generation pipeline — one ASCII radar panel.\n"
    "\n"
    "Subcommands:\n"
    "  /radar                  alias for /radar show\n"
    "  /radar show [N]         render over last N seconds\n"
    "  /radar categories       one-line per-category summary\n"
    "  /radar status           master flag + window + counts\n"
    "  /radar help             this text\n"
    "\n"
    "Master flag: JARVIS_ACTIVITY_RADAR_ENABLED (default false).\n"
)


@dataclass(frozen=True)
class RadarReplDispatchResult:
    """Result of a ``/radar`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/radar`` invocation."""

    ok: bool
    text: str
    matched: bool = True
    schema_version: str = RADAR_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/radar"
        or s == "radar"
        or s.startswith("/radar ")
        or s.startswith("radar ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.activity_radar import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_radar_command(
    line: str,
) -> RadarReplDispatchResult:
    """Parse a ``/radar`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return RadarReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return RadarReplDispatchResult(
            ok=False,
            text=f"  /radar parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "show")

    if head in ("help", "?"):
        return RadarReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return RadarReplDispatchResult(
            ok=False,
            text=(
                "  /radar: activity radar disabled "
                "(default per §33.1). Set "
                "JARVIS_ACTIVITY_RADAR_ENABLED=true."
            ),
        )

    try:
        if head == "show":
            return _render_show(
                _parse_window(args, idx=1),
            )
        if head == "categories":
            return _render_categories()
        if head == "status":
            return _render_status()
        return RadarReplDispatchResult(
            ok=False,
            text=(
                f"  /radar: unknown subcommand {head!r}. "
                f"Try /radar help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return RadarReplDispatchResult(
            ok=False,
            text=(
                f"  /radar: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_window(args, *, idx: int) -> Optional[float]:
    if len(args) <= idx:
        return None
    try:
        n = float(args[idx])
        if n < 1.0 or n > 3600.0:
            return None
        return n
    except (TypeError, ValueError):
        return None


def _render_show(
    window_override: Optional[float],
) -> RadarReplDispatchResult:
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
        format_activity_radar,
    )
    snap = aggregate_activity(
        window_s_override=window_override,
    )
    rendered = format_activity_radar(snap)
    if not rendered:
        return RadarReplDispatchResult(
            ok=True,
            text=(
                "# /radar — no activity in window "
                f"(events_in_window={snap.events_in_window})"
            ),
        )
    return RadarReplDispatchResult(
        ok=True, text=rendered,
    )


def _render_categories() -> RadarReplDispatchResult:
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
    )
    snap = aggregate_activity()
    parts = [
        f"# /radar categories (last {int(snap.window_s)}s)"
    ]
    for c in snap.by_category:
        parts.append(
            f"  {c.category.value:<12} {c.event_count:>4} "
            f"events  ({c.distinct_event_types} types)"
        )
    return RadarReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_status() -> RadarReplDispatchResult:
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
        master_enabled,
        window_seconds,
    )
    snap = aggregate_activity()
    parts = ["# /radar status"]
    parts.append(
        f"  master_enabled         : {master_enabled()}"
    )
    parts.append(
        f"  window_seconds         : "
        f"{int(window_seconds())}"
    )
    parts.append(
        f"  events_in_window       : "
        f"{snap.events_in_window}"
    )
    parts.append(
        f"  distinct_event_types   : "
        f"{snap.distinct_event_types}"
    )
    parts.append(
        f"  distinct_op_ids        : "
        f"{snap.distinct_op_ids_in_window}"
    )
    parts.append(
        f"  sensor_fire_rate/min   : "
        f"{snap.sensor_fire_rate_per_min:.2f}"
    )
    return RadarReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="radar",
            description=(
                "Live activity radar — 60s sliding window "
                "by category"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "radar_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "RADAR_REPL_SCHEMA_VERSION",
    "RadarReplDispatchResult",
    "dispatch_radar_command",
    "register_verbs",
]
