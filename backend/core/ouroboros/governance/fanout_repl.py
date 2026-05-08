"""``/fanout`` REPL dispatcher — Op fan-out tree operator
surface (PRD §38 Slice 5, 2026-05-07).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via the §33.3 naming-cage convention (file ends ``_repl.py``;
verb derived from basename; dispatcher named
``dispatch_fanout_command``).

## Subcommands

  * ``/fanout``                alias for ``show``
  * ``/fanout show [op_id]``   render tree (default = all roots)
  * ``/fanout depth <N>``      render with explicit max depth
  * ``/fanout status``         master flag + counts
  * ``/fanout help``           this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


FANOUT_REPL_SCHEMA_VERSION: str = "fanout_repl.1"


_HELP = (
    "/fanout — Op fan-out tree (PRD §38 Slice 5)\n"
    "\n"
    "Visualizes O+V's parent/child op graph (Move 6.5 K-way\n"
    "multi-prior + L3 subagent spawning) as an ASCII tree.\n"
    "\n"
    "Subcommands:\n"
    "  /fanout                  alias for /fanout show\n"
    "  /fanout show [op_id]     render tree (root op_id optional)\n"
    "  /fanout depth <N>        render with explicit max depth\n"
    "  /fanout status           master flag + counts\n"
    "  /fanout help             this text\n"
    "\n"
    "Master flag: JARVIS_OP_FANOUT_TREE_ENABLED (default false).\n"
)


@dataclass(frozen=True)
class FanoutReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = FANOUT_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/fanout"
        or s == "fanout"
        or s.startswith("/fanout ")
        or s.startswith("fanout ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.op_fanout_tree import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_fanout_command(
    line: str,
) -> FanoutReplDispatchResult:
    """Parse a ``/fanout`` line. NEVER raises."""
    if not _matches(line):
        return FanoutReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return FanoutReplDispatchResult(
            ok=False,
            text=f"  /fanout parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "show")

    if head in ("help", "?"):
        return FanoutReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return FanoutReplDispatchResult(
            ok=False,
            text=(
                "  /fanout: op fan-out tree disabled "
                "(default per §33.1). Set "
                "JARVIS_OP_FANOUT_TREE_ENABLED=true."
            ),
        )

    try:
        if head == "show":
            return _render_show(
                _parse_op_filter(args, idx=1),
            )
        if head == "depth":
            return _render_with_depth(
                _parse_int(args, idx=1, default=8),
            )
        if head == "status":
            return _render_status()
        return FanoutReplDispatchResult(
            ok=False,
            text=(
                f"  /fanout: unknown subcommand {head!r}. "
                f"Try /fanout help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return FanoutReplDispatchResult(
            ok=False,
            text=(
                f"  /fanout: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_op_filter(args, *, idx: int) -> Optional[Tuple[str, ...]]:
    if len(args) <= idx:
        return None
    return tuple(args[idx:])


def _parse_int(
    args, *, idx: int, default: int,
) -> int:
    if len(args) <= idx:
        return default
    try:
        n = int(args[idx])
        if n < 1 or n > 64:
            return default
        return n
    except (TypeError, ValueError):
        return default


def _render_show(
    op_filter: Optional[Tuple[str, ...]],
) -> FanoutReplDispatchResult:
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
        format_fanout_tree,
    )
    rows = aggregate_fanout_rows(
        root_op_ids_filter=op_filter,
    )
    rendered = format_fanout_tree(rows)
    if not rendered:
        return FanoutReplDispatchResult(
            ok=True,
            text=(
                "# /fanout — no fan-out structure detected "
                "(every op is a root with no children)"
            ),
        )
    return FanoutReplDispatchResult(
        ok=True, text=rendered,
    )


def _render_with_depth(
    depth: int,
) -> FanoutReplDispatchResult:
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
        format_fanout_tree,
    )
    rows = aggregate_fanout_rows(
        max_depth_override=depth,
    )
    rendered = format_fanout_tree(rows)
    if not rendered:
        return FanoutReplDispatchResult(
            ok=True,
            text=(
                f"# /fanout depth={depth} — no fan-out "
                f"structure detected"
            ),
        )
    return FanoutReplDispatchResult(
        ok=True, text=rendered,
    )


def _render_status() -> FanoutReplDispatchResult:
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
        master_enabled,
        max_depth,
        max_total_lines,
    )
    rows = aggregate_fanout_rows()
    parts = ["# /fanout status"]
    parts.append(f"  master_enabled    : {master_enabled()}")
    parts.append(f"  max_depth         : {max_depth()}")
    parts.append(f"  max_total_lines   : {max_total_lines()}")
    parts.append(f"  rows aggregated   : {len(rows)}")
    roots = [r for r in rows if r.depth == 0]
    children = [r for r in rows if r.depth > 0]
    parts.append(f"  root ops          : {len(roots)}")
    parts.append(f"  child ops         : {len(children)}")
    return FanoutReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="fanout",
            description=(
                "Op fan-out tree — visualize parent/child op "
                "graph (Move 6.5 K-way + L3 subagents)"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "fanout_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "FANOUT_REPL_SCHEMA_VERSION",
    "FanoutReplDispatchResult",
    "dispatch_fanout_command",
    "register_verbs",
]
