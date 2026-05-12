"""Treefinement Phase 4 — ``/repair_tree`` REPL dispatcher.
==============================================================

Operator-facing CLI surface for the
:mod:`repair_tree_archive` ring (Phase 4 substrate).
Auto-discovered by :mod:`repl_dispatch_registry` via the §33.3
naming-cage: filename basename ``repair_tree_repl.py`` → verb
``repair_tree`` → ``/repair_tree`` matches at runtime zero-edit
to ``serpent_flow.py``'s dispatch ladder.

Pattern parallel to :mod:`tool_permissions_repl`,
:mod:`fast_path_qa_repl`, :mod:`decisions_repl`.

Subcommands
-----------

* ``/repair_tree``                    — alias for
  ``/repair_tree recent``
* ``/repair_tree recent [N]``         — most-recent N branches
  (default 20, max 200)
* ``/repair_tree branches [N]``       — alias for ``recent``
* ``/repair_tree op <op_id>``         — branches for one op_id
  (case-sensitive exact match)
* ``/repair_tree layers <op_id>``     — per-layer breakdown for
  one op_id (verdict + branch count + wall_ms)
* ``/repair_tree stats``              — archive snapshot
  (capacity / size / utilization / next_seq)
* ``/repair_tree help``               — usage listing
  (always available; bypasses master-flag gate)

Master gate: :func:`repair_tree_archive.archive_enabled` (default-
FALSE per §33.1 graduation contract). When off, every subcommand
returns a friendly disabled-notice.

Authority invariants (AST-pinned in Phase 5)
--------------------------------------------

* Imports stdlib + ``repair_tree`` + ``repair_tree_archive`` ONLY.
* NEVER imports orchestrator / phase_runners / candidate_generator /
  iron_gate / change_engine / policy / providers / urgency_router /
  tool_executor / sensor_governor / repair_engine.
* **READ-ONLY** — no subcommand mutates the archive.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


_DEFAULT_RECENT_LIMIT: int = 20
_DEFAULT_FILTER_LIMIT: int = 50
_MAX_RECENT_LIMIT: int = 200


_HELP = (
    "/repair_tree — Treefinement L2 tree-search archive (Phase 4)\n"
    "\n"
    "Subcommands:\n"
    "  /repair_tree                       alias for /repair_tree recent\n"
    "  /repair_tree recent [N]            most-recent N branches "
    "(default 20, max 200)\n"
    "  /repair_tree branches [N]          alias for recent\n"
    "  /repair_tree op <op_id>            branches for one op_id "
    "(exact match)\n"
    "  /repair_tree layers <op_id>        per-layer breakdown for "
    "one op_id\n"
    "  /repair_tree stats                 archive snapshot "
    "(capacity / size / utilization)\n"
    "  /repair_tree help                  this text\n"
    "\n"
    "Branch outcome taxonomy (canonical, from repair_tree.BranchOutcome):\n"
    "  promoted / pruned_validator / pruned_duplicate / pruned_budget / won\n"
    "\n"
    "Layer verdict taxonomy (canonical, from repair_tree.LayerVerdict):\n"
    "  expanded / exhausted / won_terminal / budget_terminal\n"
    "\n"
    "Master flag: JARVIS_L2_TREE_ARCHIVE_ENABLED (default FALSE — "
    "Phase 9 cadence pending)\n"
    "Capacity env: JARVIS_L2_TREE_ARCHIVE_SIZE (default 30, "
    "bounds [1, 10000])\n"
    "Cross-substrate ref: branches archived as b-N — use /expand b-N "
    "for full diff.\n"
)


@dataclass(frozen=True)
class RepairTreeReplDispatchResult:
    """Result of a ``/repair_tree`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/repair_tree`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "matched": self.matched,
        }


# ---------------------------------------------------------------------------
# Master-flag gate — defers to canonical archive_enabled
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to the canonical
    :func:`repair_tree_archive.archive_enabled` — no parallel flag.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.repair_tree_archive import (
            archive_enabled,
        )
        return bool(archive_enabled())
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
        s == "/repair_tree"
        or s == "repair_tree"
        or s.startswith("/repair_tree ")
        or s.startswith("repair_tree ")
    )


def _parse_limit(
    args: List[str], *, default: int, ceiling: int,
) -> int:
    """Find first int-shaped token in args; clamp to [1, ceiling]."""
    for tok in args:
        try:
            n = int(tok)
        except (ValueError, TypeError):
            continue
        return max(1, min(ceiling, n))
    return default


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_repair_tree_command(
    line: str,
) -> RepairTreeReplDispatchResult:
    """Parse a ``/repair_tree`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return RepairTreeReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return RepairTreeReplDispatchResult(
            ok=False,
            text=f"  /repair_tree parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return RepairTreeReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return RepairTreeReplDispatchResult(
            ok=False,
            text=(
                "  /repair_tree: archive disabled — set "
                "JARVIS_L2_TREE_ARCHIVE_ENABLED=true (Phase 9 cadence "
                "pending; see /repair_tree help)"
            ),
        )

    if head in ("recent", "branches"):
        return _render_recent(
            _parse_limit(
                args[1:],  # skip head
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "op":
        if len(args) < 2:
            return RepairTreeReplDispatchResult(
                ok=False,
                text=(
                    "  /repair_tree op <op_id>: missing op_id argument."
                ),
            )
        return _render_by_op(
            args[1],
            _parse_limit(
                args[2:],
                default=_DEFAULT_FILTER_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "layers":
        if len(args) < 2:
            return RepairTreeReplDispatchResult(
                ok=False,
                text=(
                    "  /repair_tree layers <op_id>: missing op_id argument."
                ),
            )
        return _render_layers(args[1])
    if head == "stats":
        return _render_stats()
    return RepairTreeReplDispatchResult(
        ok=False,
        text=(
            f"  /repair_tree: unknown subcommand {head!r}. "
            f"Try /repair_tree help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers — read-only projections, defensive against archive shape drift
# ---------------------------------------------------------------------------


def _format_branch_one_line(entry: Any) -> str:
    """One-line rendering. ``entry`` is an
    :class:`repair_tree_archive.ArchivedBranch`. Defensive against
    duck-typed projections (e.g., test stubs)."""
    try:
        ref = getattr(entry, "ref", "?")
        op_id = getattr(entry, "op_id", "?")
        branch = getattr(entry, "branch", None)
        if branch is None:
            return f"  {ref} op={op_id} (malformed entry)"
        outcome = getattr(branch.outcome, "value", str(branch.outcome))
        score = float(getattr(branch, "validator_score", 0.0))
        layer = int(getattr(branch, "layer_index", 0))
        bid = str(getattr(branch, "branch_id", "?"))[:12]
        prune_reason = getattr(branch, "prune_reason", None)
        prune_str = (
            f"/{prune_reason.value}"
            if prune_reason is not None else ""
        )
        hyp = (getattr(branch, "fix_hypothesis", "") or "").strip()
        if len(hyp) > 60:
            hyp = hyp[:57] + "..."
        return (
            f"  {ref} L{layer} {outcome}{prune_str} "
            f"score={score:.2f} bid={bid} op={op_id}"
            + (f" — {hyp}" if hyp else "")
        )
    except Exception:  # noqa: BLE001
        return "  (malformed archive entry)"


def _render_recent(limit: int) -> RepairTreeReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.repair_tree_archive import (
            get_default_archive,
        )
        entries = get_default_archive().recent(limit)
    except Exception as exc:  # noqa: BLE001
        return RepairTreeReplDispatchResult(
            ok=False,
            text=f"  /repair_tree recent error: {exc}",
        )
    if not entries:
        return RepairTreeReplDispatchResult(
            ok=True,
            text=(
                "  /repair_tree recent: archive is empty "
                "(no tree-search ops have completed yet)."
            ),
        )
    lines = [
        f"/repair_tree recent (newest first, {len(entries)} of "
        f"≤{limit}):",
    ]
    lines.extend(_format_branch_one_line(e) for e in entries)
    return RepairTreeReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_op(
    op_id: str, limit: int,
) -> RepairTreeReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.repair_tree_archive import (
            get_default_archive,
        )
        entries = get_default_archive().by_op(op_id)
    except Exception as exc:  # noqa: BLE001
        return RepairTreeReplDispatchResult(
            ok=False,
            text=f"  /repair_tree op error: {exc}",
        )
    if not entries:
        return RepairTreeReplDispatchResult(
            ok=True,
            text=f"  /repair_tree op {op_id}: no archived branches.",
        )
    truncated = entries[-limit:] if len(entries) > limit else entries
    lines = [
        f"/repair_tree op {op_id} ({len(truncated)} of "
        f"{len(entries)} archived):",
    ]
    lines.extend(_format_branch_one_line(e) for e in truncated)
    return RepairTreeReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_layers(op_id: str) -> RepairTreeReplDispatchResult:
    """Aggregate by layer_index. Per-layer summary: branch count
    + outcomes + verdict (inferred from branch outcomes since the
    archive is branch-flat — operators get the same picture without
    needing the full RepairTreeLayer object)."""
    try:
        from backend.core.ouroboros.governance.repair_tree_archive import (
            get_default_archive,
        )
        entries = get_default_archive().by_op(op_id)
    except Exception as exc:  # noqa: BLE001
        return RepairTreeReplDispatchResult(
            ok=False,
            text=f"  /repair_tree layers error: {exc}",
        )
    if not entries:
        return RepairTreeReplDispatchResult(
            ok=True,
            text=f"  /repair_tree layers {op_id}: no archived branches.",
        )
    # Bucket by layer_index
    by_layer: Dict[int, List[Any]] = {}
    for e in entries:
        try:
            li = int(e.branch.layer_index)
        except Exception:  # noqa: BLE001
            li = -1
        by_layer.setdefault(li, []).append(e)
    lines = [f"/repair_tree layers {op_id}:"]
    for li in sorted(by_layer.keys()):
        layer_entries = by_layer[li]
        outcomes: Dict[str, int] = {}
        for e in layer_entries:
            o = getattr(e.branch.outcome, "value", str(e.branch.outcome))
            outcomes[o] = outcomes.get(o, 0) + 1
        outcome_str = " ".join(
            f"{k}={v}" for k, v in sorted(outcomes.items())
        )
        lines.append(
            f"  L{li}: {len(layer_entries)} branches  ({outcome_str})"
        )
    return RepairTreeReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_stats() -> RepairTreeReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.repair_tree_archive import (
            get_default_archive,
        )
        snap = get_default_archive().snapshot()
    except Exception as exc:  # noqa: BLE001
        return RepairTreeReplDispatchResult(
            ok=False,
            text=f"  /repair_tree stats error: {exc}",
        )
    util = snap.size / snap.capacity if snap.capacity else 0.0
    text = (
        "/repair_tree stats:\n"
        f"  capacity      = {snap.capacity}\n"
        f"  size          = {snap.size}\n"
        f"  utilization   = {util:.1%}\n"
        f"  next_b_seq    = {snap.next_seq}"
    )
    return RepairTreeReplDispatchResult(ok=True, text=text)


# Re-exports for the registry walker
__all__ = [
    "RepairTreeReplDispatchResult",
    "dispatch_repair_tree_command",
]
