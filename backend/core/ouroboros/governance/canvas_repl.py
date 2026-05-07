"""§37 Tier 2 #12 Slice 2 — `/canvas` REPL verb (op dependency
canvas + parallel fan-out).

Operator-facing surface for the parent/child + fan-out tracking
shipped in Slice 1 (``op_block_buffer.OpBlock`` carries
``parent_op_id`` / ``candidate_index`` / ``subagent_kind`` /
``child_op_ids``). Auto-discovered via §32.11 Slice 4 naming-
cage: file ``canvas_repl.py`` → verb ``/canvas`` → dispatcher
``dispatch_canvas_command(line)``.

**Why a NEW verb (not extending `/graph`)**: the existing
``/graph`` REPL (Path D.1, shipped earlier) surfaces the L3
execution-graph tracker (work-unit DAGs from
``ExecutionGraphProgressTracker``). The new ``/canvas``
operates at the OP level (parent→child + Move 6 K-way + L3
dispatch fan-out). Different scope, different substrate;
keeping them as separate verbs honors the §32 naming-cage
discipline (one file → one verb, single source of truth).

Renders the op dependency DAG so operator can see:
  * Move 6 K-way candidate fan-out (siblings rooted at one
    parent op, ordered by ``candidate_index``).
  * L3 subagent dispatch fan-out (children grouped by
    ``subagent_kind`` ∈ explore / review / plan / general).
  * RecursiveExplorationAgent recursive spawning depth.

**Subcommands**:

  * ``/canvas`` (bare) — show all root ops with fan-out preview.
  * ``/canvas tree`` — full dependency tree (ASCII art).
  * ``/canvas op <op-id>`` — focused subtree rooted at <op-id>.
  * ``/canvas json [<op-id>]`` — JSON projection (pipe to jq /
    external dashboards).
  * ``/canvas dot [<op-id>]`` — Graphviz DOT format
    (``/canvas dot | dot -Tpng > /tmp/canvas.png``).
  * ``/canvas fanout`` — list ops with ≥2 children (Move 6
    K-way + L3 dispatch).
  * ``/canvas help`` — usage.

**Read-only browser** (mirrors ``replay_repl`` /
``history_repl`` / ``mode_repl`` authority asymmetry): operator
queries the buffer but never mutates orchestrator state.

**Composition** (operator binding 2026-05-07):
  * Single source of truth — :func:`get_default_buffer` from
    Slice 1; no parallel buffer construction.
  * Master flag composes Slice 1's
    ``JARVIS_OP_DEPENDENCY_GRAPH_ENABLED`` (no separate REPL
    flag — single source of truth).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any, List, Optional


logger = logging.getLogger("Ouroboros.CanvasREPL")


_VERBS = ("/canvas",)
_VALID_SUBCOMMANDS = {
    "tree", "op", "json", "dot", "fanout", "help",
    # Move 6.5 Slice 5 — multi-prior fan-out renderers
    "multi_prior", "multi_prior_diff",
}


_TREE_BRANCH = "├─ "
_TREE_LAST = "└─ "
_TREE_VERT = "│  "
_TREE_BLANK = "   "


@dataclass
class CanvasDispatchResult:
    """Mirrors sibling REPL dispatch shape — auto-discovery
    convention requires ``ok``, ``text``, ``matched``."""
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _VERBS


def dispatch_canvas_command(line: str) -> CanvasDispatchResult:
    """Parse a ``/canvas`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return CanvasDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CanvasDispatchResult(
            ok=False, text=f"/canvas: parse error — {exc}",
        )
    args = tokens[1:] if len(tokens) > 1 else []
    if not args:
        return _render_overview()
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return CanvasDispatchResult(
            ok=False,
            text=(
                f"/canvas: unknown subcommand {sub!r}. "
                f"Try /canvas help."
            ),
        )
    if sub == "help":
        return _render_help()
    if sub == "tree":
        return _render_tree(focus_op=None)
    if sub == "op":
        if len(args) < 2:
            return CanvasDispatchResult(
                ok=False,
                text=(
                    "/canvas op: missing op-id. Usage: "
                    "/canvas op <op-id>"
                ),
            )
        return _render_tree(focus_op=args[1])
    if sub == "json":
        focus = args[1] if len(args) >= 2 else None
        return _render_json(focus_op=focus)
    if sub == "dot":
        focus = args[1] if len(args) >= 2 else None
        return _render_dot(focus_op=focus)
    if sub == "fanout":
        return _render_fanout()
    if sub == "multi_prior":
        if len(args) < 2:
            return CanvasDispatchResult(
                ok=False,
                text=(
                    "/canvas multi_prior: missing op-id. "
                    "Usage: /canvas multi_prior <op-id>"
                ),
            )
        return _render_multi_prior(
            op_id=args[1], with_diffs=False,
        )
    if sub == "multi_prior_diff":
        if len(args) < 2:
            return CanvasDispatchResult(
                ok=False,
                text=(
                    "/canvas multi_prior_diff: missing "
                    "op-id. Usage: /canvas multi_prior_diff "
                    "<op-id>"
                ),
            )
        return _render_multi_prior(
            op_id=args[1], with_diffs=True,
        )
    return CanvasDispatchResult(
        ok=False,
        text=f"/canvas: unhandled subcommand {sub!r}",
    )


def _render_multi_prior(
    *,
    op_id: str,
    with_diffs: bool,
) -> CanvasDispatchResult:
    """Slice 5 multi-prior fan-out renderer. Composes Slice
    5's :func:`render_fan_out_overview` /
    :func:`render_diff_fan_out`. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
            find_recent,
            master_enabled,
            render_diff_fan_out,
            render_fan_out_overview,
        )
    except ImportError:
        return CanvasDispatchResult(
            ok=False,
            text=(
                "/canvas multi_prior — Slice 5 substrate "
                "unavailable"
            ),
        )
    if not master_enabled():
        return CanvasDispatchResult(
            ok=True,
            text=(
                "/canvas multi_prior — disabled "
                "(JARVIS_MULTI_PRIOR_CANVAS_ENABLED=false)"
            ),
        )
    name = (op_id or "").strip()
    if not name:
        return CanvasDispatchResult(
            ok=False,
            text="/canvas multi_prior: blank op-id",
        )
    verdict = find_recent(name)
    if verdict is None:
        return CanvasDispatchResult(
            ok=True,
            text=(
                f"/canvas multi_prior {name!r} — not in "
                f"the in-memory ring (evicted or never "
                f"recorded). Try /multi_prior op {name!r} "
                f"for ledger summary."
            ),
        )
    text = (
        render_diff_fan_out(verdict)
        if with_diffs
        else render_fan_out_overview(verdict)
    )
    if not text:
        return CanvasDispatchResult(
            ok=True,
            text=(
                f"/canvas multi_prior {name!r} — verdict "
                f"present but missing fan-out detail "
                f"(generator returned no diffs?)"
            ),
        )
    return CanvasDispatchResult(ok=True, text=text)


def _render_help() -> CanvasDispatchResult:
    text = (
        "/canvas — Op dependency graph + parallel fan-out "
        "canvas (§37 Tier 2 #12)\n"
        "\n"
        "  /canvas                show all root ops with "
        "fan-out preview\n"
        "  /canvas tree           full dependency tree "
        "(ASCII art)\n"
        "  /canvas op <op-id>     focused subtree rooted at "
        "<op-id>\n"
        "  /canvas json [<op-id>] JSON projection (pipe to "
        "jq)\n"
        "  /canvas dot [<op-id>]  Graphviz DOT format\n"
        "  /canvas fanout         list ops with ≥2 children\n"
        "  /canvas multi_prior <op-id>      Move 6.5 K-prior "
        "fan-out overview (consensus + per-prior signatures)\n"
        "  /canvas multi_prior_diff <op-id> Move 6.5 K-prior "
        "fan-out + per-prior diff comparison\n"
        "  /canvas help           this message\n"
        "\n"
        "Master flag: JARVIS_OP_DEPENDENCY_GRAPH_ENABLED "
        "(default-FALSE per §33.1)\n"
        "Tracks: Move 6 K-way candidates + L3 subagent "
        "dispatch + RecursiveExplorationAgent spawning.\n"
        "Distinct from /graph (L3 execution-graph tracker — "
        "different scope)."
    )
    return CanvasDispatchResult(ok=True, text=text)


def _buffer_or_disabled() -> Optional[Any]:
    """Resolve the canonical buffer if master flag is on.
    Returns ``None`` (caller renders disabled message) when off
    or import fails."""
    try:
        from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
            get_default_buffer,
            op_dependency_graph_enabled,
        )
    except ImportError:
        return None
    try:
        if not op_dependency_graph_enabled():
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        return get_default_buffer()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _disabled_result() -> CanvasDispatchResult:
    return CanvasDispatchResult(
        ok=True,
        text=(
            "/canvas: op-dependency tracking disabled. Set "
            "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED=true to "
            "enable. Note: the buffer always carries fan-out "
            "fields (default neutral); only the WRITE surface "
            "(register_parent) is gated."
        ),
    )


def _render_overview() -> CanvasDispatchResult:
    buf = _buffer_or_disabled()
    if buf is None:
        return _disabled_result()
    try:
        roots = buf.find_root_ops()
    except Exception:  # noqa: BLE001 — defensive
        return CanvasDispatchResult(
            ok=False,
            text="/canvas: buffer read failed (non-fatal)",
        )
    if not roots:
        return CanvasDispatchResult(
            ok=True,
            text=(
                "/canvas: no root ops in current window. Run "
                "/canvas tree once ops have been recorded, "
                "or /canvas help for usage."
            ),
        )
    lines = [f"/canvas overview ({len(roots)} root ops):"]
    for root in roots:
        lines.append(_format_op_summary(root))
        if root.fan_out_size:
            lines.append(
                f"   {_TREE_LAST}{root.fan_out_size} child(ren) "
                f"— `/canvas op {root.op_id}` for full subtree"
            )
    return CanvasDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_tree(
    *, focus_op: Optional[str],
) -> CanvasDispatchResult:
    buf = _buffer_or_disabled()
    if buf is None:
        return _disabled_result()
    try:
        if focus_op:
            subtree = buf.walk_subtree(focus_op)
            if not subtree:
                return CanvasDispatchResult(
                    ok=False,
                    text=(
                        f"/canvas op: no op found for "
                        f"{focus_op!r}"
                    ),
                )
            roots = (subtree[0],)
        else:
            roots = buf.find_root_ops()
    except Exception:  # noqa: BLE001 — defensive
        return CanvasDispatchResult(
            ok=False,
            text=(
                "/canvas tree: buffer read failed "
                "(non-fatal)"
            ),
        )
    if not roots:
        return CanvasDispatchResult(
            ok=True,
            text=(
                "/canvas tree: no root ops in current window."
            ),
        )
    out_lines: List[str] = ["/canvas tree:"]
    for root in roots:
        out_lines.extend(_format_subtree(buf, root, prefix=""))
    return CanvasDispatchResult(
        ok=True, text="\n".join(out_lines),
    )


def _format_subtree(
    buf: Any, op: Any, *, prefix: str,
    depth: int = 0, max_depth: int = 16,
) -> List[str]:
    """Recursive ASCII-art tree rendering. Composes
    OpBlockBuffer.get_child_op_ids — single source of truth
    for child resolution (no parallel walker)."""
    lines = [
        prefix + _format_op_summary(op, indent_for_tree=True),
    ]
    if depth >= max_depth:
        lines.append(prefix + _TREE_BLANK + "(max depth)")
        return lines
    try:
        child_ids = list(op.child_op_ids)
    except Exception:  # noqa: BLE001 — defensive
        return lines
    last_idx = len(child_ids) - 1
    for idx, child_id in enumerate(child_ids):
        try:
            child = buf._find_block_by_op_id(  # noqa: SLF001
                child_id,
            )
        except Exception:  # noqa: BLE001 — defensive
            child = None
        is_last = idx == last_idx
        branch = _TREE_LAST if is_last else _TREE_BRANCH
        descent = _TREE_BLANK if is_last else _TREE_VERT
        child_prefix = prefix + descent
        if child is None:
            lines.append(
                prefix + branch
                + f"{child_id} (evicted from buffer window)"
            )
            continue
        sub_lines = _format_subtree(
            buf, child, prefix=child_prefix,
            depth=depth + 1, max_depth=max_depth,
        )
        if sub_lines:
            sub_lines[0] = (
                prefix + branch
                + sub_lines[0][len(child_prefix):]
            )
        lines.extend(sub_lines)
    return lines


def _format_op_summary(
    op: Any, *, indent_for_tree: bool = False,
) -> str:
    """Single-line op summary for tree/overview rendering."""
    parts = [str(op.op_id)]
    state_str = (
        op.state.value if hasattr(op.state, "value")
        else str(op.state)
    )
    parts.append(f"[{state_str}]")
    if op.subagent_kind:
        parts.append(f"kind={op.subagent_kind}")
    if op.candidate_index > 0:
        parts.append(f"candidate=#{op.candidate_index}")
    if op.fan_out_size:
        parts.append(f"children={op.fan_out_size}")
    if op.summary_line:
        clip = (
            op.summary_line[:60] + "…"
            if len(op.summary_line) > 60
            else op.summary_line
        )
        parts.append(f"— {clip}")
    if indent_for_tree:
        return " ".join(parts)
    return "  " + " ".join(parts)


def _collect_ops(
    buf: Any, focus_op: Optional[str],
) -> Optional[List[Any]]:
    """Resolve the op corpus for json/dot rendering. Returns
    ``None`` on lookup miss (caller renders error)."""
    try:
        if focus_op:
            subtree = buf.walk_subtree(focus_op)
            if not subtree:
                return None
            return list(subtree)
        roots = buf.find_root_ops()
        ops: List[Any] = []
        visited: set = set()
        for r in roots:
            for op in buf.walk_subtree(r.op_id):
                if op.op_id not in visited:
                    visited.add(op.op_id)
                    ops.append(op)
        return ops
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_json(
    *, focus_op: Optional[str],
) -> CanvasDispatchResult:
    buf = _buffer_or_disabled()
    if buf is None:
        return _disabled_result()
    ops = _collect_ops(buf, focus_op)
    if ops is None:
        return CanvasDispatchResult(
            ok=False,
            text=(
                f"/canvas json: no op found for "
                f"{focus_op!r}"
                if focus_op
                else "/canvas json: buffer read failed"
            ),
        )
    payload = {
        "schema_version": "canvas_repl.1",
        "ops": [op.to_dict() for op in ops],
    }
    try:
        text = json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        text = "{}"
    return CanvasDispatchResult(ok=True, text=text)


def _render_dot(
    *, focus_op: Optional[str],
) -> CanvasDispatchResult:
    """Emit Graphviz DOT format. Operator pipes:
    ``/canvas dot | dot -Tpng > /tmp/c.png``."""
    buf = _buffer_or_disabled()
    if buf is None:
        return _disabled_result()
    ops = _collect_ops(buf, focus_op)
    if ops is None:
        return CanvasDispatchResult(
            ok=False,
            text=(
                f"/canvas dot: no op found for "
                f"{focus_op!r}"
                if focus_op
                else "/canvas dot: buffer read failed"
            ),
        )
    lines = [
        "digraph OpDependencyCanvas {",
        "  rankdir=LR;",
        '  node [shape=box, fontname="monospace"];',
    ]
    seen_ids = {op.op_id for op in ops}
    for op in ops:
        label_parts = [str(op.op_id)]
        if op.subagent_kind:
            label_parts.append(f"kind={op.subagent_kind}")
        if op.candidate_index > 0:
            label_parts.append(f"#{op.candidate_index}")
        label = "\\n".join(label_parts)
        node_id = _dot_safe_id(op.op_id)
        lines.append(f'  {node_id} [label="{label}"];')
    for op in ops:
        for child_id in op.child_op_ids:
            if child_id not in seen_ids:
                continue
            parent_node = _dot_safe_id(op.op_id)
            child_node = _dot_safe_id(child_id)
            lines.append(
                f"  {parent_node} -> {child_node};"
            )
    lines.append("}")
    return CanvasDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _dot_safe_id(op_id: str) -> str:
    """Convert an op_id to a DOT-safe node identifier (alphanum
    + underscore only)."""
    return "op_" + "".join(
        c if c.isalnum() else "_" for c in str(op_id)
    )


def _render_fanout() -> CanvasDispatchResult:
    """List ops with ≥2 direct children (load-bearing surface
    for Move 6 K-way Quorum + L3 subagent dispatch)."""
    buf = _buffer_or_disabled()
    if buf is None:
        return _disabled_result()
    ops = _collect_ops(buf, focus_op=None)
    if ops is None:
        return CanvasDispatchResult(
            ok=False,
            text=(
                "/canvas fanout: buffer read failed "
                "(non-fatal)"
            ),
        )
    fanouts = [op for op in ops if op.fan_out_size >= 2]
    if not fanouts:
        return CanvasDispatchResult(
            ok=True,
            text=(
                "/canvas fanout: no fan-out ops in current "
                "window. Move 6 K-way + L3 subagent dispatch "
                "produce these — register_parent must be "
                "called by the spawn site for them to appear."
            ),
        )
    lines = [
        f"/canvas fanout ({len(fanouts)} fan-out ops):",
    ]
    for op in fanouts:
        lines.append(_format_op_summary(op))
        kinds: list = []
        try:
            children = [
                buf._find_block_by_op_id(  # noqa: SLF001
                    cid,
                )
                for cid in op.child_op_ids
            ]
            kinds = sorted(
                {
                    c.subagent_kind for c in children
                    if c is not None and c.subagent_kind
                }
            )
        except Exception:  # noqa: BLE001 — defensive
            kinds = []
        if kinds:
            lines.append(
                f"   children kinds: {', '.join(kinds)}"
            )
    return CanvasDispatchResult(
        ok=True, text="\n".join(lines),
    )


__all__ = [
    "CanvasDispatchResult",
    "dispatch_canvas_command",
]
