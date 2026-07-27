"""The plan, ticked off as the work lands.

    ⏺ Plan(4 changes)
      ⎿ ☒ thin_client.py · extract the socket-path helper
         ☒ harness.py · call the helper at bind time
         ☐ attach_probe.py · call the helper at probe time
         ☐ test_thin_client.py · pin one resolver

The PLAN phase already produces exactly this. `plan_generator` emits schema
`plan.1` with `ordered_changes` — a list of `{file_path, change_type,
description}` — and `/show_plan` renders it on demand. What was missing is
that an operator had to ASK, mid-flight, for the shape of work already
decided.

Completion is DERIVED, not tracked
-----------------------------------
Nothing reports "step 2 finished". But a plan item names a file, and a
successful `edit_file` / `write_file` names the file it touched — the same
events the diff renderer already intercepts. An item is done when its file has
been written.

That is a real inference rather than a guarantee, and it is stated as one: the
checklist shows what has been TOUCHED, which is evidence of progress, not
proof of correctness. VERIFY decides whether the work was right. A checklist
that claimed otherwise would be the confident-and-wrong failure this codebase
keeps finding.

Deriving also means there is nothing to keep in sync. An alternative design —
the orchestrator reporting step completion — would need a second source of
truth about what happened, and the two would eventually disagree about an op
that partially applied.

Path matching
-------------
A plan says `backend/core/x.py`; a tool may report an absolute path, a
repo-relative one, or a bare filename. Matching is by path SUFFIX on whole
segments, so `core/x.py` matches `/repo/backend/core/x.py` and `x.py` matches
both — while `ax.py` matches neither, which a plain `endswith` would get
wrong.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.PlanChecklist")

__all__ = [
    "PlanChecklist", "checklist_enabled", "paths_match",
    "register_plan", "note_file_touched",
]

#: Beyond this, a checklist stops being a glance and becomes a wall. The plan
#: itself is never truncated — only what is drawn inline.
_MAX_SHOWN = 12


def checklist_enabled() -> bool:
    """Default ON: the plan is already computed; hiding it is the anomaly."""
    return os.environ.get(
        "JARVIS_PLAN_CHECKLIST_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def paths_match(plan_path: str, touched_path: str) -> bool:
    """Do these two path spellings name the same file?

    Compared on whole SEGMENTS from the right. A plain `endswith` would call
    `ax.py` a match for `x.py`, and a plain basename comparison would conflate
    `cli/utils.py` with `governance/utils.py` — both wrong in the direction of
    ticking something that did not happen.
    """
    try:
        a = [p for p in str(plan_path or "").replace("\\\\", "/").split("/") if p]
        b = [p for p in str(touched_path or "").replace("\\\\", "/").split("/") if p]
        if not a or not b:
            return False
        shared = min(len(a), len(b))
        return a[-shared:] == b[-shared:]
    except Exception:  # noqa: BLE001
        return False


class PlanChecklist:
    """One op's plan, and which of its files have been written."""

    def __init__(self, ordered_changes: Sequence[Any] = ()) -> None:
        self._items: List[Dict[str, Any]] = []
        try:
            for change in ordered_changes or ():
                if isinstance(change, dict):
                    path = str(change.get("file_path", "") or "")
                    desc = str(change.get("description", "") or "")
                    kind = str(change.get("change_type", "") or "")
                else:
                    path = str(getattr(change, "file_path", "") or "")
                    desc = str(getattr(change, "description", "") or "")
                    kind = str(getattr(change, "change_type", "") or "")
                if path or desc:
                    self._items.append(
                        {"path": path, "desc": desc, "kind": kind,
                         "done": False},
                    )
        except Exception:  # noqa: BLE001
            logger.debug("[PlanChecklist] parse degraded", exc_info=True)

    # -- state -------------------------------------------------------------

    def mark_touched(self, path: str) -> bool:
        """Tick the item naming *path*. True if this changed something.

        Returns False for an already-ticked item so a re-edit does not
        re-announce a step the operator has already seen completed — the
        model revising the same file twice is normal, and narrating it twice
        would read as two steps.
        """
        try:
            for item in self._items:
                if item["done"] or not item["path"]:
                    continue
                if paths_match(item["path"], path):
                    item["done"] = True
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @property
    def total(self) -> int:
        return len(self._items)

    @property
    def done(self) -> int:
        return sum(1 for i in self._items if i["done"])

    @property
    def complete(self) -> bool:
        return bool(self._items) and self.done == self.total

    # -- render ------------------------------------------------------------

    def render(self, max_shown: int = _MAX_SHOWN) -> List[str]:
        """Chrome lines for the checklist, or [] when there is nothing to say.

        A single-item plan renders nothing: "1 change" with one tick is a
        checklist that never had a decision in it, and it would push the real
        work off screen for no information.
        """
        if not checklist_enabled() or len(self._items) < 2:
            return []
        lines = [f"⏺ Plan({self.done}/{self.total} changes)"]
        # UNFINISHED work first among the hidden: if a plan is longer than the
        # window, the steps still to come are what an operator needs.
        shown = self._items[:max_shown]
        for idx, item in enumerate(shown):
            box = "☒" if item["done"] else "☐"
            label = item["path"] or item["desc"]
            detail = ""
            if item["path"] and item["desc"]:
                # Separator rebuilt AFTER clipping: `_short` collapses
                # whitespace, so composing " · desc" before clipping loses the
                # leading space and yields "file.py· desc".
                detail = f" · {_short(item['desc'], 44)}"
            # ⎿ opens the subordinate block once; continuation rows align
            # under it. Repeating the glyph per row makes a four-item plan
            # read as four separate results rather than one list.
            marker = "⎿" if idx == 0 else " "
            lines.append(f"  {marker} {box} {_short(label)}{detail}")
        hidden = len(self._items) - len(shown)
        if hidden > 0:
            remaining = sum(1 for i in self._items[max_shown:] if not i["done"])
            lines.append(f"    … {hidden} more ({remaining} outstanding)")
        return lines


def _short(text: str, limit: int = 44) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    # Paths are identified by their tail; the leading directories are the
    # part that repeats across every entry in a checklist.
    if "/" in flat:
        return "…" + flat[-(limit - 1):]
    return flat[: limit - 1].rstrip() + "…"


class ChecklistRegistry:
    """Per-op checklists. Bounded — a cockpit is not a ledger."""

    def __init__(self, max_ops: int = 32) -> None:
        self._by_op: Dict[str, PlanChecklist] = {}
        self._order: List[str] = []
        self._max = max(1, int(max_ops))

    def register(self, op_id: str, ordered_changes: Sequence[Any]) -> Optional[PlanChecklist]:
        try:
            checklist = PlanChecklist(ordered_changes)
            if checklist.total < 2:
                return None
            key = str(op_id or "")
            if key in self._by_op:
                self._order.remove(key)
            self._by_op[key] = checklist
            self._order.append(key)
            while len(self._order) > self._max:
                self._by_op.pop(self._order.pop(0), None)
            return checklist
        except Exception:  # noqa: BLE001
            return None

    def get(self, op_id: str) -> Optional[PlanChecklist]:
        return self._by_op.get(str(op_id or ""))

    def clear(self, op_id: str) -> None:
        key = str(op_id or "")
        if self._by_op.pop(key, None) is not None:
            try:
                self._order.remove(key)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# The process-wide registry
# ---------------------------------------------------------------------------
#
# A module singleton for the same reason `set_operator_dispatcher` is one: the
# producer (the orchestrator, at PLAN) and the consumer (SerpentFlow, at each
# edit) are in different layers with no handle to each other, and threading
# one through every intermediate would give each a parameter it does not use.

_REGISTRY = ChecklistRegistry()


def register_plan(op_id: str, ordered_changes: Sequence[Any]) -> Optional[PlanChecklist]:
    """Called at PLAN completion. NEVER raises."""
    try:
        return _REGISTRY.register(op_id, ordered_changes)
    except Exception:  # noqa: BLE001
        return None


def note_file_touched(op_id: str, path: str) -> List[str]:
    """Called on a successful edit/write. Returns lines to render, or [].

    Empty when nothing changed — a re-edit of an already-ticked file, an op
    with no plan, or a single-item plan. Rendering only on a REAL transition
    is what keeps the checklist a progress signal rather than a repeated
    banner.
    """
    try:
        checklist = _REGISTRY.get(op_id)
        if checklist is None:
            return []
        if not checklist.mark_touched(path):
            return []
        return checklist.render()
    except Exception:  # noqa: BLE001
        return []


def reset_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = ChecklistRegistry()
