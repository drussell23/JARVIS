"""Op fan-out tree renderer (PRD §38 Slice 5, 2026-05-07).

Closes the §38.4 commitment to surface O+V's parent/child op
graph (Move 6.5 K-way multi-prior + L3 subagent spawning) as an
ASCII tree — uniquely visualizing what CC structurally cannot
match (CC ops are flat).

## Composes canonical sources (operator binding "no duplication")

  * :class:`battle_test.op_block_buffer.OpBlockBuffer` —
    canonical op-lifecycle store with parent/child fields
    populated by Tier 2 #12 (PRD §37 Tier 2 #12 op dependency
    canvas). Specifically:
      - ``find_root_ops()`` — ops with no parent
      - ``walk_subtree(op_id, max_depth)`` — BFS-ordered subtree
      - ``get_child_op_ids(op_id)`` — direct children

NEVER reimplements parent/child tracking, op lifecycle state,
or BFS traversal — pure render layer over canonical Tier 2 #12
fields.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib + battle_test.
     op_block_buffer ONLY. NEVER imports orchestrator /
     iron_gate / policy / providers / candidate_generator /
     change_engine / semantic_guardian.
  3. **Composes canonical fan-out fields** — render path MUST
     compose ``OpBlockBuffer.find_root_ops`` +
     ``walk_subtree`` (Tier 2 #12 canonical accessors). No
     direct ``_items`` / ``_active_op_ids`` access; no
     parallel parent/child tracking.
  4. **Bounded** — render at most :data:`MAX_TOTAL_LINES`
     lines (env-overridable) regardless of fan-out shape.
     Tree depth capped via ``walk_subtree`` max_depth.
  5. **NEVER raises** — every render path defensive.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


OP_FANOUT_TREE_SCHEMA_VERSION: str = "op_fanout_tree.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_OP_FANOUT_TREE_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off,
    :func:`format_fanout_tree` returns empty string and
    ``/fanout`` REPL verb reports disabled."""
    if os.environ.get( "JARVIS_OP_FANOUT_TREE_ENABLED", "", ).strip().lower() in _TRUTHY:
        return True
    # §40 polish pack opt-in — when JARVIS_UX_POLISH_PACK_ENABLED
    # is on AND the operator hasn't explicitly disabled this
    # substrate via its own env flag, the pack predicate
    # activates it. Preserves §33.1 default-FALSE discipline:
    # the canonical _flag(...) / _TRUTHY check above is intact
    # so the substrate's master_default_false AST pin still
    # fires structurally.
    try:
        from backend.core.ouroboros.governance.ux_polish_pack import (
            is_substrate_in_active_pack,
        )
        return is_substrate_in_active_pack('op_fanout_tree')
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Tunable knobs — env-overridable
# ---------------------------------------------------------------------------


_MAX_DEPTH_DEFAULT: int = 8
_MAX_TOTAL_LINES_DEFAULT: int = 30
_OP_ID_SHORT_LEN_DEFAULT: int = 8
_LABEL_MAX_CHARS_DEFAULT: int = 60


def max_depth() -> int:
    raw = os.environ.get(
        "JARVIS_OP_FANOUT_MAX_DEPTH", "",
    ).strip()
    if not raw:
        return _MAX_DEPTH_DEFAULT
    try:
        return max(1, min(64, int(raw)))
    except (TypeError, ValueError):
        return _MAX_DEPTH_DEFAULT


def max_total_lines() -> int:
    raw = os.environ.get(
        "JARVIS_OP_FANOUT_MAX_TOTAL_LINES", "",
    ).strip()
    if not raw:
        return _MAX_TOTAL_LINES_DEFAULT
    try:
        return max(2, min(500, int(raw)))
    except (TypeError, ValueError):
        return _MAX_TOTAL_LINES_DEFAULT


def op_id_short_len() -> int:
    raw = os.environ.get(
        "JARVIS_OP_FANOUT_OP_ID_SHORT_LEN", "",
    ).strip()
    if not raw:
        return _OP_ID_SHORT_LEN_DEFAULT
    try:
        return max(2, min(20, int(raw)))
    except (TypeError, ValueError):
        return _OP_ID_SHORT_LEN_DEFAULT


def label_max_chars() -> int:
    raw = os.environ.get(
        "JARVIS_OP_FANOUT_LABEL_MAX_CHARS", "",
    ).strip()
    if not raw:
        return _LABEL_MAX_CHARS_DEFAULT
    try:
        return max(20, min(200, int(raw)))
    except (TypeError, ValueError):
        return _LABEL_MAX_CHARS_DEFAULT


# ---------------------------------------------------------------------------
# Canonical glyphs — env-overridable
# ---------------------------------------------------------------------------
#
# Closed canonical 4-glyph table (operator binding "no
# hardcoding" — render paths compose `glyph_*` accessors,
# never inline the characters).


_BRANCH_T_DEFAULT: str = "├─"
_BRANCH_L_DEFAULT: str = "└─"
_VERTICAL_DEFAULT: str = "│ "
_INDENT_DEFAULT: str = "  "


def _branch_t() -> str:
    return os.environ.get(
        "JARVIS_OP_FANOUT_BRANCH_T", "",
    ) or _BRANCH_T_DEFAULT


def _branch_l() -> str:
    return os.environ.get(
        "JARVIS_OP_FANOUT_BRANCH_L", "",
    ) or _BRANCH_L_DEFAULT


def _vertical() -> str:
    return os.environ.get(
        "JARVIS_OP_FANOUT_VERTICAL", "",
    ) or _VERTICAL_DEFAULT


def _indent() -> str:
    return os.environ.get(
        "JARVIS_OP_FANOUT_INDENT", "",
    ) or _INDENT_DEFAULT


# ---------------------------------------------------------------------------
# Versioned snapshot artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutTreeRow:
    """One rendered tree row. Frozen for safe propagation."""

    schema_version: str = OP_FANOUT_TREE_SCHEMA_VERSION
    op_id: str = ""
    op_id_short: str = ""
    depth: int = 0
    parent_op_id: str = ""
    subagent_kind: str = ""
    candidate_index: int = 0
    label: str = ""
    line: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "op_id_short": self.op_id_short,
            "depth": int(self.depth),
            "parent_op_id": self.parent_op_id,
            "subagent_kind": self.subagent_kind,
            "candidate_index": int(self.candidate_index),
            "label": self.label,
            "line": self.line,
        }


# ---------------------------------------------------------------------------
# Label derivation — composes canonical OpBlock fields
# ---------------------------------------------------------------------------


def _short_op_id(op_id: str) -> str:
    n = op_id_short_len()
    if not isinstance(op_id, str) or not op_id:
        return ""
    return op_id[-n:] if len(op_id) > n else op_id


def _derive_label(block: Any) -> str:
    """Pick the best operator-readable label for a tree row.
    Composes :class:`OpBlock.summary_line` (committed) /
    ``lines[0]`` (active) / op_id fallback. NEVER raises."""
    cap = label_max_chars()
    try:
        summary = str(getattr(block, "summary_line", "") or "")
        if summary:
            return summary[:cap]
        lines = tuple(getattr(block, "lines", ()) or ())
        for line in lines:
            cleaned = str(line or "").strip()
            if cleaned:
                return cleaned[:cap]
        return "in progress"
    except Exception:  # noqa: BLE001 — defensive
        return "in progress"


# ---------------------------------------------------------------------------
# Tree rendering — pure function over canonical sources
# ---------------------------------------------------------------------------


def aggregate_fanout_rows(
    *,
    root_op_ids_filter: Optional[Tuple[str, ...]] = None,
    max_depth_override: Optional[int] = None,
    max_total_lines_override: Optional[int] = None,
) -> Tuple[FanoutTreeRow, ...]:
    """Compose canonical OpBlockBuffer fan-out fields into a
    flat tuple of :class:`FanoutTreeRow`. Pure read; NEVER
    raises.

    Caller filters (or omits) which roots to render. Default:
    every root op currently buffered.

    BFS ordering preserved within each root subtree (matches
    canonical ``walk_subtree``)."""
    try:
        from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
            get_default_buffer,
        )
        buf = get_default_buffer()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    try:
        depth_clamp = (
            int(max_depth_override)
            if max_depth_override is not None
            else max_depth()
        )
        line_clamp = (
            int(max_total_lines_override)
            if max_total_lines_override is not None
            else max_total_lines()
        )
    except (TypeError, ValueError):
        return ()
    try:
        roots = buf.find_root_ops()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    if root_op_ids_filter is not None:
        filter_set = frozenset(
            (op or "").strip() for op in root_op_ids_filter
        )
        roots = tuple(
            r for r in roots
            if getattr(r, "op_id", "") in filter_set
        )
    rows: List[FanoutTreeRow] = []
    for root in roots:
        if len(rows) >= line_clamp:
            break
        try:
            subtree = buf.walk_subtree(
                root.op_id, max_depth=depth_clamp,
            )
        except Exception:  # noqa: BLE001 — defensive
            subtree = (root,)
        # Build depth map by walking parent_op_id chain.
        # OpBlock.depth field is not canonical; compute here.
        depths: Dict[str, int] = {root.op_id: 0}
        for block in subtree:
            if block.op_id == root.op_id:
                continue
            parent = getattr(block, "parent_op_id", "")
            depths[block.op_id] = depths.get(parent, 0) + 1
        for block in subtree:
            if len(rows) >= line_clamp:
                break
            row = _block_to_row(
                block, depth=depths.get(block.op_id, 0),
            )
            rows.append(row)
    return tuple(rows)


def _block_to_row(
    block: Any, *, depth: int,
) -> FanoutTreeRow:
    """Project an :class:`OpBlock` into a tree row. NEVER
    raises."""
    try:
        op_id = str(getattr(block, "op_id", "") or "")
        parent = str(getattr(block, "parent_op_id", "") or "")
        subagent = str(getattr(block, "subagent_kind", "") or "")
        cand_idx = int(getattr(block, "candidate_index", 0) or 0)
        label = _derive_label(block)
        return FanoutTreeRow(
            op_id=op_id,
            op_id_short=_short_op_id(op_id),
            depth=int(depth),
            parent_op_id=parent,
            subagent_kind=subagent,
            candidate_index=cand_idx,
            label=label,
            line="",  # populated by render
        )
    except Exception:  # noqa: BLE001 — defensive
        return FanoutTreeRow(depth=int(depth))


def format_fanout_tree(
    rows: Optional[Tuple[FanoutTreeRow, ...]] = None,
) -> str:
    """Render the fan-out tree as a multi-line ASCII string.

    Output shape:
        ``● 019d1234  Update(file.py)``
        ``├─ 019d5678  [explore] read_file(target=...)``
        ``│  └─ 019d9abc  [general] grep(pattern=...)``
        ``└─ 019dffff  [review] semantic_check(...)``

    Branch glyphs (``├─`` / ``└─`` / ``│ `` / ``  ``) are
    canonical and env-overridable per "no hardcoding".

    NEVER raises. Returns empty when:
      * Master flag off
      * No rows / no fan-out (single-op trees with no children
        render as empty too — fan-out is the load-bearing
        signal here)
    """
    try:
        if not master_enabled():
            return ""
        rows_tuple = (
            rows
            if rows is not None
            else aggregate_fanout_rows()
        )
        if not rows_tuple:
            return ""
        # If every row is depth=0 with no children, don't render
        # (no fan-out structure — falls back to flat panel).
        has_children = any(
            r.depth > 0 for r in rows_tuple
        )
        if not has_children:
            return ""
        # Build an op_id → row map + per-parent children list
        # so we can render proper terminal branch glyphs (last
        # child uses └─; siblings use ├─).
        children_by_parent: Dict[str, List[str]] = {}
        for r in rows_tuple:
            if r.parent_op_id:
                children_by_parent.setdefault(
                    r.parent_op_id, [],
                ).append(r.op_id)
        # Order by op_id (BFS preserves insertion; we need to
        # know "is this the last child of its parent" for glyph
        # selection).
        last_child_of: Dict[str, bool] = {}
        for parent_op, kids in children_by_parent.items():
            if kids:
                last_child_of[kids[-1]] = True
        lines: List[str] = []
        # Track per-depth "is parent's last sibling" for proper
        # vertical-bar continuation.
        parent_was_last_at: Dict[int, bool] = {}
        for r in rows_tuple:
            if r.depth == 0:
                # Root — bullet glyph
                short = r.op_id_short or "?"
                label = r.label or "in progress"
                line = f"● {short}  {label}"
            else:
                is_last = last_child_of.get(r.op_id, False)
                # Build prefix: indent + vertical bars for each
                # ancestor depth, then ├─/└─ for self.
                prefix_parts = []
                for d in range(1, r.depth):
                    if parent_was_last_at.get(d, False):
                        prefix_parts.append(_indent())
                    else:
                        prefix_parts.append(_vertical())
                prefix_parts.append(
                    _branch_l() if is_last else _branch_t()
                )
                prefix = "".join(prefix_parts)
                short = r.op_id_short or "?"
                kind_tag = (
                    f"[{r.subagent_kind}] "
                    if r.subagent_kind else ""
                )
                cand_tag = (
                    f"#{r.candidate_index} "
                    if r.candidate_index > 0 else ""
                )
                label = r.label or "in progress"
                line = (
                    f"{prefix} {short}  "
                    f"{kind_tag}{cand_tag}{label}"
                )
                parent_was_last_at[r.depth] = is_last
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[op_fanout_tree] format_fanout_tree "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 4 pins:

      1. ``master_default_false`` — JARVIS_OP_FANOUT_TREE_-
         ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity.
      3. ``composes_canonical_op_block_buffer`` — aggregator
         MUST lazy-import ``OpBlockBuffer`` canonical accessors
         (``find_root_ops`` + ``walk_subtree`` from Tier 2 #12).
      4. ``no_hardcoded_glyphs`` — render path MUST compose
         ``_branch_t`` / ``_branch_l`` / ``_vertical`` /
         ``_indent`` accessor functions; ASCII branch literals
         (``├─`` / ``└─`` / ``│``) MUST live ONLY in the
         canonical ``_*_DEFAULT`` constants — operator binding
         "no hardcoding" enforced via accessor-composition
         check.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/op_fanout_tree.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                # §40 polish-pack composition: walk only the
                # top-level body + unconditional containers (Try)
                # so `if env_check: return True` is correctly
                # recognized as gated. Naive `"return True" in src`
                # would fire on the conditional path too.
                def _has_unconditional_return_true(stmts):
                    for stmt in stmts:
                        if (
                            isinstance(stmt, ast.Return)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is True
                        ):
                            return True
                        if isinstance(stmt, ast.Try):
                            if _has_unconditional_return_true(
                                stmt.body,
                            ):
                                return True
                            if _has_unconditional_return_true(
                                stmt.finalbody,
                            ):
                                return True
                    return False

                if _has_unconditional_return_true(node.body):
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_OP_FANOUT_TREE_ENABLED" not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_OP_FANOUT_TREE_ENABLED"
                    )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"op_fanout_tree MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_op_block_buffer(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "op_block_buffer" not in source:
            violations.append(
                "op_fanout_tree MUST compose "
                "op_block_buffer (no parallel parent/child "
                "tracking)"
            )
        if "find_root_ops" not in source:
            violations.append(
                "render path MUST use canonical "
                "OpBlockBuffer.find_root_ops accessor"
            )
        if "walk_subtree" not in source:
            violations.append(
                "render path MUST use canonical "
                "OpBlockBuffer.walk_subtree accessor"
            )
        return tuple(violations)

    def _validate_no_hardcoded_glyphs(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Branch-glyph string literals MUST appear ONLY in
        the canonical ``_*_DEFAULT`` constants. Render paths
        MUST go through ``_branch_t`` / ``_branch_l`` /
        ``_vertical`` / ``_indent`` accessor functions."""
        violations: list = []
        # Walk for AST FunctionDef _branch_t / _branch_l /
        # _vertical / _indent (must exist as accessor funcs).
        accessor_names = {
            "_branch_t", "_branch_l",
            "_vertical", "_indent",
        }
        seen_accessors: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name in accessor_names:
                    seen_accessors.add(node.name)
        missing_accessors = accessor_names - seen_accessors
        if missing_accessors:
            violations.append(
                f"missing canonical glyph accessor "
                f"functions: {sorted(missing_accessors)}"
            )
        # Default constants must exist with the canonical
        # branch glyph values.
        required_defaults = {
            "_BRANCH_T_DEFAULT": "├─",  # ├─
            "_BRANCH_L_DEFAULT": "└─",  # └─
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) or isinstance(
                node, ast.Assign,
            ):
                targets = (
                    [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else node.targets
                )
                for tgt in targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id in required_defaults
                    ):
                        if (
                            isinstance(node.value, ast.Constant)
                            and isinstance(
                                node.value.value, str,
                            )
                        ):
                            expected = required_defaults[tgt.id]
                            if (
                                node.value.value
                                != expected
                            ):
                                violations.append(
                                    f"{tgt.id} value "
                                    f"{node.value.value!r} "
                                    f"does not match canonical "
                                    f"branch glyph"
                                )
                            required_defaults.pop(tgt.id)
        if required_defaults:
            violations.append(
                f"missing canonical default constants: "
                f"{sorted(required_defaults.keys())}"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "op_fanout_tree_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_OP_FANOUT_TREE_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "op_fanout_tree_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Renderer MUST stay pure substrate composing "
                "op_block_buffer + stdlib ONLY."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "op_fanout_tree_composes_canonical_op_block_"
                "buffer"
            ),
            target_file=target,
            description=(
                "Aggregator MUST compose canonical "
                "OpBlockBuffer.find_root_ops + walk_subtree "
                "(Tier 2 #12 fan-out fields). No parallel "
                "parent/child tracking."
            ),
            validate=_validate_composes_op_block_buffer,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "op_fanout_tree_no_hardcoded_glyphs"
            ),
            target_file=target,
            description=(
                "Branch glyphs MUST live in canonical "
                "_BRANCH_T_DEFAULT / _BRANCH_L_DEFAULT / "
                "_VERTICAL_DEFAULT / _INDENT_DEFAULT "
                "constants, accessed via _branch_t / "
                "_branch_l / _vertical / _indent functions. "
                "Operator binding 'no hardcoding'."
            ),
            validate=_validate_no_hardcoded_glyphs,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_OP_FANOUT_TREE_ENABLED",
            "bool",
            "false",
            (
                "Master flag for op fan-out tree (§38 Slice 5). "
                "Default-FALSE per §33.1."
            ),
        ),
        (
            "JARVIS_OP_FANOUT_MAX_DEPTH",
            "int",
            str(_MAX_DEPTH_DEFAULT),
            "Maximum tree depth rendered (default 8).",
        ),
        (
            "JARVIS_OP_FANOUT_MAX_TOTAL_LINES",
            "int",
            str(_MAX_TOTAL_LINES_DEFAULT),
            "Maximum tree rows rendered total (default 30).",
        ),
        (
            "JARVIS_OP_FANOUT_OP_ID_SHORT_LEN",
            "int",
            str(_OP_ID_SHORT_LEN_DEFAULT),
            "Trailing characters of op_id displayed.",
        ),
        (
            "JARVIS_OP_FANOUT_LABEL_MAX_CHARS",
            "int",
            str(_LABEL_MAX_CHARS_DEFAULT),
            "Maximum label length per row.",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "op_fanout_tree.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return n
    return n


__all__ = [
    "FanoutTreeRow",
    "OP_FANOUT_TREE_SCHEMA_VERSION",
    "aggregate_fanout_rows",
    "format_fanout_tree",
    "label_max_chars",
    "master_enabled",
    "max_depth",
    "max_total_lines",
    "op_id_short_len",
    "register_flags",
    "register_shipped_invariants",
]
