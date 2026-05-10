"""Venom V2 Slice 2 — ``/tool_permissions`` REPL dispatcher.
==============================================================

Operator-facing CLI surface for the
:mod:`permission_decision_archive` ring (v2.89 Slice 1
substrate). Auto-discovered by
:mod:`repl_dispatch_registry` via the §33.3 naming-cage:
filename basename ``tool_permissions_repl.py`` → verb
``tool_permissions`` → ``/tool_permissions`` matches at runtime
zero-edit to ``serpent_flow.py``'s dispatch ladder.

Pattern parallel to :mod:`decisions_repl`,
:mod:`curiosity_repl`, :mod:`outcomes_repl`, etc.

Subcommands
-----------

* ``/tool_permissions``               — alias for
  ``/tool_permissions recent``
* ``/tool_permissions recent [N]``    — most-recent N decisions
  (default 20, max 200)
* ``/tool_permissions tool <name>``   — recent decisions for one
  tool (case-sensitive exact match)
* ``/tool_permissions op <op_id>``    — recent decisions for one
  op_id (case-sensitive exact match)
* ``/tool_permissions stats``         — archive snapshot
  (capacity / size / utilization)
* ``/tool_permissions help``          — usage listing
  (always available; bypasses master-flag gate)

Master gate: :func:`permission_archive_enabled` (default-FALSE
per §33.1 graduation contract). When off, every subcommand
returns a friendly disabled-notice and points at the canonical
env-var to flip — no fake-empty-list output.

Authority invariants (AST-pinned)
---------------------------------

* Imports stdlib + ``permission_decision_archive`` ONLY.
* NEVER imports orchestrator / phase_runners / candidate_generator /
  iron_gate / change_engine / policy / semantic_guardian /
  providers / urgency_router / tool_executor /
  sensor_governor / tool_permission (the substrate's policy
  module — read-only consumer of the archive's projection).
* **READ-ONLY** — no subcommand mutates the archive.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


_HELP = (
    "/tool_permissions — Venom V2 permission decision archive "
    "(v2.89 Slice 2 / PRD §37 Tier 2 #6)\n"
    "\n"
    "Subcommands:\n"
    "  /tool_permissions                  alias for "
    "/tool_permissions recent\n"
    "  /tool_permissions recent [N]       most-recent N "
    "decisions (default 20, max 200)\n"
    "  /tool_permissions tool <name>      recent decisions for "
    "one tool name (exact match)\n"
    "  /tool_permissions op <op_id>       recent decisions for "
    "one op_id (exact match)\n"
    "  /tool_permissions stats            archive snapshot "
    "(capacity / size / utilization)\n"
    "  /tool_permissions help             this text\n"
    "\n"
    "Decision taxonomy (canonical, from tool_permission.py):\n"
    "  allow / deny / ask / defer\n"
    "\n"
    "Master flag: JARVIS_PERMISSION_ARCHIVE_ENABLED (default "
    "FALSE — Phase 9 cadence pending)\n"
    "Capacity env:  JARVIS_PERMISSION_ARCHIVE_SIZE (default 50, "
    "bounds [1, 10000])\n"
    "Cross-substrate: each record carries a ``p-N`` ref usable "
    "with /expand <p-N>\n"
)


_DEFAULT_RECENT_LIMIT: int = 20
_MAX_RECENT_LIMIT: int = 200
_DEFAULT_FILTER_LIMIT: int = 20


# ---------------------------------------------------------------------------
# Frozen result container — mirrors DecisionsReplDispatchResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPermissionsReplDispatchResult:
    """Result of a ``/tool_permissions`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/tool_permissions`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Master-flag gate — defers to canonical permission_archive_enabled
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to the canonical
    :func:`permission_decision_archive.permission_archive_enabled`
    — no parallel flag. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
            permission_archive_enabled,
        )
        return bool(permission_archive_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Dispatch matchers + parsers
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/tool_permissions"
        or s == "tool_permissions"
        or s.startswith("/tool_permissions ")
        or s.startswith("tool_permissions ")
    )


def _parse_limit(
    args: List[str], *, default: int, ceiling: int,
) -> int:
    """Parse limit from the ``args[1]`` slot. Falls through to
    default on parse failure / out-of-bounds."""
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_tool_permissions_command(
    line: str,
) -> ToolPermissionsReplDispatchResult:
    """Parse a ``/tool_permissions`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ToolPermissionsReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=f"  /tool_permissions parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return ToolPermissionsReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=(
                "  /tool_permissions: archive disabled — set "
                "JARVIS_PERMISSION_ARCHIVE_ENABLED=true (Phase 9 "
                "cadence pending; see /tool_permissions help)"
            ),
        )

    if head == "recent":
        return _render_recent(
            _parse_limit(
                args,
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "tool":
        if len(args) < 2:
            return ToolPermissionsReplDispatchResult(
                ok=False,
                text=(
                    "  /tool_permissions tool <name>: missing "
                    "tool name argument."
                ),
            )
        return _render_by_tool(
            args[1],
            _parse_limit(
                args[1:],  # consume <name> so [N] sits at args[2]
                default=_DEFAULT_FILTER_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "op":
        if len(args) < 2:
            return ToolPermissionsReplDispatchResult(
                ok=False,
                text=(
                    "  /tool_permissions op <op_id>: missing "
                    "op_id argument."
                ),
            )
        return _render_by_op(
            args[1],
            _parse_limit(
                args[1:],
                default=_DEFAULT_FILTER_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "stats":
        return _render_stats()
    return ToolPermissionsReplDispatchResult(
        ok=False,
        text=(
            f"  /tool_permissions: unknown subcommand {head!r}. "
            f"Try /tool_permissions help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_record_one_line(rec: object) -> str:
    """One-line rendering for the recent / filter listings. Reads
    via duck-typed attribute access so a foreign object doesn't
    crash the rendering path."""
    ref = getattr(rec, "ref", "") or ""
    op_id = (getattr(rec, "op_id", "") or "")[:18]
    tool_name = (getattr(rec, "tool_name", "") or "")[:24]
    decision = (getattr(rec, "decision_value", "") or "")[:8]
    return (
        f"  {ref:<6}  decision={decision:<6}  "
        f"tool={tool_name:<24}  op={op_id}"
    )


def _render_recent(
    limit: int,
) -> ToolPermissionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
            get_default_archive,
        )
        records = get_default_archive().recent(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=f"  /tool_permissions recent error: {exc}",
        )
    if not records:
        return ToolPermissionsReplDispatchResult(
            ok=True,
            text=(
                "  /tool_permissions: no decisions recorded yet. "
                "Archive is empty (or master flag was just enabled "
                "and no tool dispatch has happened since)."
            ),
        )
    lines = [
        f"  /tool_permissions recent (last {len(records)}):",
    ]
    for rec in records:
        lines.append(_format_record_one_line(rec))
    return ToolPermissionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_tool(
    tool_name: str, limit: int,
) -> ToolPermissionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
            get_default_archive,
        )
        records = get_default_archive().by_tool(
            tool_name, limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=f"  /tool_permissions tool error: {exc}",
        )
    if not records:
        return ToolPermissionsReplDispatchResult(
            ok=True,
            text=(
                f"  /tool_permissions tool {tool_name!r}: "
                f"no decisions recorded for this tool."
            ),
        )
    lines = [
        f"  /tool_permissions tool {tool_name!r} "
        f"(last {len(records)}):",
    ]
    for rec in records:
        lines.append(_format_record_one_line(rec))
    return ToolPermissionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_op(
    op_id: str, limit: int,
) -> ToolPermissionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
            get_default_archive,
        )
        records = get_default_archive().by_op(
            op_id, limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=f"  /tool_permissions op error: {exc}",
        )
    if not records:
        return ToolPermissionsReplDispatchResult(
            ok=True,
            text=(
                f"  /tool_permissions op {op_id!r}: no "
                f"decisions recorded for this op."
            ),
        )
    lines = [
        f"  /tool_permissions op {op_id!r} (last "
        f"{len(records)}):",
    ]
    for rec in records:
        lines.append(_format_record_one_line(rec))
    return ToolPermissionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_stats() -> ToolPermissionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
            get_default_archive,
        )
        snap = get_default_archive().snapshot()
    except Exception as exc:  # noqa: BLE001 — defensive
        return ToolPermissionsReplDispatchResult(
            ok=False,
            text=f"  /tool_permissions stats error: {exc}",
        )
    text = (
        "  /tool_permissions stats:\n"
        f"    capacity:    {snap.capacity}\n"
        f"    size:        {snap.size}\n"
        f"    next_seq:    {snap.next_seq}\n"
        f"    utilization: {snap.utilization:.2%}\n"
        f"    schema:      {snap.schema_version}"
    )
    return ToolPermissionsReplDispatchResult(ok=True, text=text)
