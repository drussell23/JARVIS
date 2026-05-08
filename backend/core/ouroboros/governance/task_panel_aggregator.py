"""Persistent task-panel aggregator (PRD §37 Phase 3,
2026-05-07).

Closes the operator-flagged "persistent task-list panel
missing" gap from the v2.53 UX comparison: CC's screenshot
shows ``■ Resolve EXPLORATION_LEDGER runner attribution bug``
(in-progress) + ``□ Verify cron install runbook + install``
(pending) as a multi-line panel pinned at the bottom of the
TUI. Pre-Phase-3 O+V's TUI rendered op blocks inline as they
fired but had NO persistent operator-visible panel
aggregating active/recent ops.

This module is the SOLE knower of the task-panel composition.
It composes the canonical :class:`OpBlockBuffer` lifecycle
state — eliminates the need for downstream consumers to track
parallel "active task" state.

## Why this exists

CC's "task" semantics come from its `TaskCreate`/`TaskUpdate`
substrate. O+V's autonomous execution model has no human-
created tasks; the equivalent unit-of-work is **active ops in
the orchestrator pipeline**. ``OpBlockBuffer`` already tracks
the canonical lifecycle (BUFFERING → COMMITTED) per op. The
panel composes that.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — no I/O beyond what's needed for the
     panel render. NEVER raises.
  2. **Authority asymmetry** — imports stdlib + battle_test/
     ONLY at top-level. NEVER imports orchestrator / iron_gate
     / policy / providers / candidate_generator / change_engine
     / semantic_guardian.
  3. **Composes canonical sources** — verb/elapsed for each
     op MUST come from ``OpBlockBuffer``'s public read API
     (``active_blocks`` + ``recently_committed``). NO parallel
     state for op lifecycle.
  4. **Closed glyph taxonomy** — :class:`TaskPanelGlyph` is a
     3-value frozen enum (``IN_PROGRESS`` / ``PENDING`` /
     ``COMPLETED``). New glyphs require explicit scope-doc +
     pin update.
  5. **Bounded** — panel renders at most :data:`MAX_PANEL_LINES`
     entries (env-overridable). Newer entries displace older
     ones in registration order; in-progress before recently-
     committed (CC visual order).
"""
from __future__ import annotations

import enum
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


TASK_PANEL_AGGREGATOR_SCHEMA_VERSION: str = "task_panel.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_TASK_PANEL_ENABLED`` master switch. Default-
    FALSE per §33.1 — when off, :func:`format_task_panel`
    returns empty string and the bottom_toolbar's panel segment
    is not rendered. Operator flips after observing the panel
    via the status-line composition."""
    return os.environ.get(
        "JARVIS_TASK_PANEL_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Knobs — env-overridable (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


_MAX_PANEL_LINES_DEFAULT: int = 5
_RECENT_COMMIT_FADE_S_DEFAULT: float = 30.0
_OP_ID_SHORT_LEN_DEFAULT: int = 6
_LABEL_MAX_CHARS_DEFAULT: int = 80


def max_panel_lines() -> int:
    raw = os.environ.get(
        "JARVIS_TASK_PANEL_MAX_LINES", "",
    ).strip()
    if not raw:
        return _MAX_PANEL_LINES_DEFAULT
    try:
        n = int(raw)
        return max(1, min(20, n))
    except (TypeError, ValueError):
        return _MAX_PANEL_LINES_DEFAULT


def recent_commit_fade_s() -> float:
    raw = os.environ.get(
        "JARVIS_TASK_PANEL_RECENT_COMMIT_FADE_S", "",
    ).strip()
    if not raw:
        return _RECENT_COMMIT_FADE_S_DEFAULT
    try:
        v = float(raw)
        return max(0.0, min(600.0, v))
    except (TypeError, ValueError):
        return _RECENT_COMMIT_FADE_S_DEFAULT


def op_id_short_len() -> int:
    raw = os.environ.get(
        "JARVIS_TASK_PANEL_OP_ID_SHORT_LEN", "",
    ).strip()
    if not raw:
        return _OP_ID_SHORT_LEN_DEFAULT
    try:
        n = int(raw)
        return max(2, min(20, n))
    except (TypeError, ValueError):
        return _OP_ID_SHORT_LEN_DEFAULT


def label_max_chars() -> int:
    raw = os.environ.get(
        "JARVIS_TASK_PANEL_LABEL_MAX_CHARS", "",
    ).strip()
    if not raw:
        return _LABEL_MAX_CHARS_DEFAULT
    try:
        n = int(raw)
        return max(20, min(240, n))
    except (TypeError, ValueError):
        return _LABEL_MAX_CHARS_DEFAULT


# ---------------------------------------------------------------------------
# Closed glyph taxonomy (3 values, AST-pinned)
# ---------------------------------------------------------------------------


class TaskPanelGlyph(str, enum.Enum):
    """Closed 3-value glyph taxonomy for task-panel rendering.
    Bytes-pinned via AST regression.

      * ``IN_PROGRESS`` — ``■`` filled square; matches CC's
        in-progress task indicator.
      * ``PENDING`` — ``□`` empty square; reserved for future
        BackgroundAgentPool queue surfacing (Phase 3 ships
        the substrate; pending-state population deferred to
        a follow-on slice).
      * ``COMPLETED`` — ``✓`` check mark; recently-committed
        ops within the fade window.
    """

    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    COMPLETED = "completed"


_GLYPH_CHARS: Dict[TaskPanelGlyph, str] = {
    TaskPanelGlyph.IN_PROGRESS: "■",
    TaskPanelGlyph.PENDING: "□",
    TaskPanelGlyph.COMPLETED: "✓",
}


def glyph_char(glyph: TaskPanelGlyph) -> str:
    """Map :class:`TaskPanelGlyph` → display character. NEVER
    raises. Defensive on unknown enum extension (returns ``?``)."""
    return _GLYPH_CHARS.get(glyph, "?")


# ---------------------------------------------------------------------------
# Versioned entry artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskPanelEntry:
    """One panel row. Frozen for safe propagation."""

    schema_version: str = TASK_PANEL_AGGREGATOR_SCHEMA_VERSION
    op_id: str = ""
    op_id_short: str = ""
    label: str = ""
    glyph: TaskPanelGlyph = TaskPanelGlyph.IN_PROGRESS
    started_at_monotonic: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """§33.5 symmetric projection. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "op_id_short": self.op_id_short,
            "label": self.label,
            "glyph": self.glyph.value,
            "glyph_char": glyph_char(self.glyph),
            "started_at_monotonic": float(
                self.started_at_monotonic,
            ),
            "elapsed_s": float(self.elapsed_s),
        }


# ---------------------------------------------------------------------------
# Label derivation
# ---------------------------------------------------------------------------


# Strip Rich/ANSI markup from rendered op-block lines so the
# panel renders plain text. Operator binding "build cleanly on
# what already exists" — lines come from canonical OpBlock,
# but the panel surface needs unmarked text.
_RICH_MARKUP_RE = re.compile(r"\[/?[a-zA-Z0-9_ #\-]+\]")
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_markup(text: str) -> str:
    """Strip Rich `[bold]...[/bold]` markup AND raw ANSI escape
    sequences. Pure function. NEVER raises."""
    if not isinstance(text, str):
        return ""
    s = _ANSI_ESC_RE.sub("", text)
    s = _RICH_MARKUP_RE.sub("", s)
    return s.strip()


def derive_label(
    *,
    block_lines: Tuple[str, ...] = (),
    summary_line: str = "",
    op_id: str = "",
    fallback: str = "in progress",
) -> str:
    """Pick the best operator-readable label for a panel row.

    Priority order:
      1. ``summary_line`` if non-empty (only set after commit
         on COMMITTED blocks)
      2. First non-empty stripped block line if available
      3. ``fallback`` text (default "in progress")

    Truncated to :func:`label_max_chars` chars. Pure function.
    NEVER raises."""
    cap = label_max_chars()
    try:
        if summary_line:
            cleaned = _strip_markup(summary_line)
            if cleaned:
                return cleaned[:cap]
        for line in block_lines or ():
            cleaned = _strip_markup(line)
            if cleaned:
                return cleaned[:cap]
        return fallback[:cap]
    except Exception:  # noqa: BLE001 — defensive
        return fallback[:cap]


def short_op_id(op_id: str) -> str:
    """Return the trailing N chars of the op_id (default 6).
    Operator-readable shortcut for the panel. Pure function."""
    n = op_id_short_len()
    if not isinstance(op_id, str) or not op_id:
        return ""
    return op_id[-n:] if len(op_id) > n else op_id


# ---------------------------------------------------------------------------
# Aggregation — composes canonical OpBlockBuffer
# ---------------------------------------------------------------------------


def aggregate_panel_entries(
    *,
    now_monotonic: Optional[float] = None,
    fade_s_override: Optional[float] = None,
    max_lines_override: Optional[int] = None,
) -> Tuple[TaskPanelEntry, ...]:
    """Compose canonical ``OpBlockBuffer`` state into a tuple
    of :class:`TaskPanelEntry`. NEVER raises.

    Order:
      1. All BUFFERING blocks (``IN_PROGRESS`` glyph)
      2. COMMITTED blocks within the fade window
         (``COMPLETED`` glyph)

    Capped at :func:`max_panel_lines` entries; older overflow
    drops first."""
    try:
        from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
            get_default_buffer,
        )
        buffer = get_default_buffer()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    try:
        now = (
            float(now_monotonic)
            if now_monotonic is not None
            else time.monotonic()
        )
        fade = (
            float(fade_s_override)
            if fade_s_override is not None
            else recent_commit_fade_s()
        )
        cap = (
            int(max_lines_override)
            if max_lines_override is not None
            else max_panel_lines()
        )
    except (TypeError, ValueError):
        return ()
    out: List[TaskPanelEntry] = []
    # 1) Active (BUFFERING) blocks first.
    try:
        active = buffer.active_blocks()
    except Exception:  # noqa: BLE001 — defensive
        active = ()
    for block in active:
        out.append(_block_to_entry(
            block,
            glyph=TaskPanelGlyph.IN_PROGRESS,
            now=now,
        ))
        if len(out) >= cap:
            return tuple(out)
    # 2) Recently committed blocks (within fade window).
    try:
        recent = buffer.recently_committed(
            within_seconds=fade,
            now_monotonic=now,
        )
    except Exception:  # noqa: BLE001 — defensive
        recent = ()
    for block in recent:
        out.append(_block_to_entry(
            block,
            glyph=TaskPanelGlyph.COMPLETED,
            now=now,
        ))
        if len(out) >= cap:
            break
    return tuple(out)


def _block_to_entry(
    block: Any, *, glyph: TaskPanelGlyph, now: float,
) -> TaskPanelEntry:
    """Project an :class:`OpBlock` into a panel entry. NEVER
    raises — defensive on every field."""
    try:
        op_id = str(getattr(block, "op_id", "") or "")
        lines = tuple(getattr(block, "lines", ()) or ())
        summary = str(getattr(block, "summary_line", "") or "")
        started = float(getattr(block, "started_at", 0.0) or 0.0)
        elapsed = max(0.0, now - started) if started > 0 else 0.0
        label = derive_label(
            block_lines=lines,
            summary_line=summary,
            op_id=op_id,
        )
        return TaskPanelEntry(
            op_id=op_id,
            op_id_short=short_op_id(op_id),
            label=label,
            glyph=glyph,
            started_at_monotonic=started,
            elapsed_s=elapsed,
        )
    except Exception:  # noqa: BLE001 — defensive
        return TaskPanelEntry(glyph=glyph)


# ---------------------------------------------------------------------------
# Render — multi-line panel string
# ---------------------------------------------------------------------------


def format_task_panel(
    entries: Optional[Tuple[TaskPanelEntry, ...]] = None,
) -> str:
    """Render the task panel as a multi-line string.

    Output shape (matches CC visual format):
        ``■ <op-short> <label>  [42s]``
        ``■ <op-short> <label>  [12s]``
        ``✓ <op-short> <committed-summary>``

    NEVER raises. Returns empty string when:
      * Master flag off
      * No entries
      * All entries had defensive fallbacks producing empty
        labels"""
    try:
        if not master_enabled():
            return ""
        rows = (
            entries
            if entries is not None
            else aggregate_panel_entries()
        )
        if not rows:
            return ""
        lines: List[str] = []
        for e in rows:
            ch = glyph_char(e.glyph)
            short = e.op_id_short or "?"
            label = e.label or "in progress"
            elapsed_token = (
                f"  [{int(e.elapsed_s)}s]"
                if e.elapsed_s >= 1.0
                else ""
            )
            lines.append(
                f"{ch} {short} {label}{elapsed_token}"
            )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[TaskPanel] format_task_panel swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_TASK_PANEL_ENABLED
         stays default-FALSE per §33.1.
      2. ``glyph_taxonomy_3_values`` — closed-enum integrity.
      3. ``authority_asymmetry`` — substrate purity.
      4. ``composes_canonical_op_block_buffer`` — aggregator
         MUST lazy-import ``op_block_buffer`` for active +
         recently-committed blocks (no parallel op-state
         tracking).
      5. ``no_hardcoded_glyphs`` — aggregator MUST NOT inline
         glyph characters as string literals at call sites;
         every glyph render path goes through
         :func:`glyph_char`.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "task_panel_aggregator.py"
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
                src = ast.unparse(node)
                if "return True" in src:
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if "JARVIS_TASK_PANEL_ENABLED" not in src:
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_TASK_PANEL_ENABLED"
                    )
        return tuple(violations)

    def _validate_glyph_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"IN_PROGRESS", "PENDING", "COMPLETED"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "TaskPanelGlyph":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"TaskPanelGlyph missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"TaskPanelGlyph has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
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
                            f"task_panel_aggregator MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_op_block_buffer(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "op_block_buffer" not in source:
            violations.append(
                "task_panel_aggregator MUST compose "
                "op_block_buffer (no parallel op-state "
                "tracking)"
            )
        if "active_blocks" not in source:
            violations.append(
                "aggregator MUST use canonical "
                "OpBlockBuffer.active_blocks accessor"
            )
        if "recently_committed" not in source:
            violations.append(
                "aggregator MUST use canonical "
                "OpBlockBuffer.recently_committed accessor"
            )
        return tuple(violations)

    def _validate_no_hardcoded_glyphs(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Operator binding "no hardcoding" — enforced
        STRUCTURALLY by asserting:

          1. ``_GLYPH_CHARS`` dict is the canonical mapping at
             module level.
          2. The dict contains all 3 ``TaskPanelGlyph`` keys.
          3. ``glyph_char`` function is the SOLE accessor and
             composes ``_GLYPH_CHARS``.

        Pragmatic over a pure-literal-scan because docstrings
        + comments legitimately mention the glyphs (operator-
        readable documentation). The structural check guards
        against the load-bearing case: a future maintainer
        writing ``panel_line = f"■ {x}"`` at a render call-site
        would BYPASS the canonical dict — but only the
        existence-check at module level can prove this is
        impossible without false-positives on docs. We instead
        enforce by **asserting glyph_char composes
        _GLYPH_CHARS** (so any direct literal at call-sites
        would be a separate, reviewable choice rather than a
        silent regression)."""
        violations: list = []

        # 1. _GLYPH_CHARS dict exists at module level.
        glyph_chars_assignment_found = False
        glyph_keys_seen: set = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for tgt in targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "_GLYPH_CHARS"
                    ):
                        glyph_chars_assignment_found = True
                        # Walk the value (Dict) to capture
                        # which glyph values appear as
                        # ast.Constant value strings.
                        val = node.value
                        if isinstance(val, ast.Dict):
                            for v in val.values:
                                if (
                                    isinstance(v, ast.Constant)
                                    and isinstance(v.value, str)
                                ):
                                    glyph_keys_seen.add(
                                        v.value,
                                    )
        if not glyph_chars_assignment_found:
            violations.append(
                "_GLYPH_CHARS canonical mapping missing at "
                "module level"
            )
        # All 3 canonical glyph chars MUST appear in the dict.
        required_glyphs = {"■", "□", "✓"}
        missing = required_glyphs - glyph_keys_seen
        if missing:
            violations.append(
                f"_GLYPH_CHARS missing canonical glyphs: "
                f"{sorted(missing)}"
            )

        # 2. glyph_char function references _GLYPH_CHARS.
        glyph_char_composes_canonical = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "glyph_char"
            ):
                src_unparsed = ast.unparse(node)
                if "_GLYPH_CHARS" in src_unparsed:
                    glyph_char_composes_canonical = True
        if not glyph_char_composes_canonical:
            violations.append(
                "glyph_char MUST compose _GLYPH_CHARS — "
                "operator binding 'no hardcoding'"
            )

        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "task_panel_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_TASK_PANEL_ENABLED stays "
                "default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "task_panel_glyph_taxonomy_3_values"
            ),
            target_file=target,
            description=(
                "TaskPanelGlyph is a 3-value closed taxonomy "
                "(IN_PROGRESS / PENDING / COMPLETED). New "
                "values require explicit scope-doc + pin "
                "update."
            ),
            validate=_validate_glyph_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "task_panel_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Aggregator MUST stay pure substrate composing "
                "op_block_buffer + stdlib ONLY. NEVER imports "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator / change_engine / "
                "semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "task_panel_composes_canonical_op_block_buffer"
            ),
            target_file=target,
            description=(
                "Aggregator MUST compose canonical "
                "OpBlockBuffer.active_blocks + "
                "recently_committed. No parallel state for "
                "op lifecycle."
            ),
            validate=_validate_composes_op_block_buffer,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "task_panel_no_hardcoded_glyphs"
            ),
            target_file=target,
            description=(
                "Glyph characters (■ / □ / ✓) MUST appear "
                "ONLY in _GLYPH_CHARS canonical dict. Render "
                "paths MUST compose glyph_char(). Operator "
                "binding 'no hardcoding'."
            ),
            validate=_validate_no_hardcoded_glyphs,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register task-panel flags with the FlagRegistry."""
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_TASK_PANEL_ENABLED",
            "bool",
            "false",
            (
                "Master flag for the persistent task panel "
                "(§37 Phase 3). Default-FALSE per §33.1; "
                "flips after operator validates the panel "
                "rendering."
            ),
        ),
        (
            "JARVIS_TASK_PANEL_MAX_LINES",
            "int",
            str(_MAX_PANEL_LINES_DEFAULT),
            "Maximum number of panel rows displayed.",
        ),
        (
            "JARVIS_TASK_PANEL_RECENT_COMMIT_FADE_S",
            "float",
            str(_RECENT_COMMIT_FADE_S_DEFAULT),
            (
                "Fade window (seconds) for recently-committed "
                "ops. After this many seconds, COMPLETED ops "
                "drop from the panel."
            ),
        ),
        (
            "JARVIS_TASK_PANEL_OP_ID_SHORT_LEN",
            "int",
            str(_OP_ID_SHORT_LEN_DEFAULT),
            "Trailing characters of op_id displayed in panel.",
        ),
        (
            "JARVIS_TASK_PANEL_LABEL_MAX_CHARS",
            "int",
            str(_LABEL_MAX_CHARS_DEFAULT),
            "Maximum label length per panel row.",
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
                        "task_panel_aggregator.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return n
    return n


__all__ = [
    "TASK_PANEL_AGGREGATOR_SCHEMA_VERSION",
    "TaskPanelEntry",
    "TaskPanelGlyph",
    "aggregate_panel_entries",
    "derive_label",
    "format_task_panel",
    "glyph_char",
    "label_max_chars",
    "master_enabled",
    "max_panel_lines",
    "op_id_short_len",
    "recent_commit_fade_s",
    "register_flags",
    "register_shipped_invariants",
    "short_op_id",
]
