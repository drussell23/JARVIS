"""The plan, ticked off as the work lands.

The PLAN phase already produces exactly this — `plan_generator` emits schema
`plan.1` with `ordered_changes`, and `/show_plan` renders it on demand. What
was missing is that an operator had to ASK, mid-flight, for the shape of work
already decided.

Completion is DERIVED, not reported: a plan item names a file, and a
successful edit names the file it touched. That is an inference about
PROGRESS, not a claim of correctness — VERIFY decides whether the work was
right, and a checklist implying otherwise would be the confident-and-wrong
failure this codebase keeps finding.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test.plan_checklist import (
    PlanChecklist,
    note_file_touched,
    paths_match,
    register_plan,
    reset_registry_for_tests,
)

_REPO = Path(__file__).resolve().parents[2]

_PLAN = [
    {"file_path": "backend/core/cli/thin_client.py",
     "description": "extract the socket-path helper", "change_type": "modify"},
    {"file_path": "backend/core/battle_test/harness.py",
     "description": "call it at bind time", "change_type": "modify"},
    {"file_path": "tests/cli/test_thin_client.py",
     "description": "pin one resolver", "change_type": "create"},
]


@pytest.fixture(autouse=True)
def _clean():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# --------------------------------------------------------------------------
# 1. path matching cannot tick the wrong thing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("plan,touched,expected", [
    ("core/x.py", "/repo/backend/core/x.py", True),
    ("x.py", "/repo/backend/core/x.py", True),
    ("backend/core/x.py", "core/x.py", True),
    # A plain endswith would call this a match.
    ("x.py", "ax.py", False),
    # A basename comparison would conflate these.
    ("cli/utils.py", "governance/utils.py", False),
    ("", "x.py", False),
    ("x.py", "", False),
])
def test_paths_match_on_whole_segments(
    plan: str, touched: str, expected: bool,
) -> None:
    """Both failure directions tick something that did not happen."""
    assert paths_match(plan, touched) is expected


@pytest.mark.parametrize("junk", [None, 42, object()])
def test_matching_never_raises(junk: Any) -> None:
    assert isinstance(paths_match(junk, junk), bool)


# --------------------------------------------------------------------------
# 2. the checklist tracks real progress
# --------------------------------------------------------------------------

def test_items_start_unticked() -> None:
    c = PlanChecklist(_PLAN)
    assert c.total == 3 and c.done == 0
    assert "☐" in "\n".join(c.render())


def test_touching_a_file_ticks_its_item() -> None:
    c = PlanChecklist(_PLAN)
    assert c.mark_touched("/repo/backend/core/cli/thin_client.py") is True
    assert c.done == 1
    assert "☒ backend/core/cli/thin_client.py" in "\n".join(c.render())


def test_a_re_edit_does_not_re_announce() -> None:
    """The model revising the same file twice is normal; narrating it twice
    would read as two steps."""
    c = PlanChecklist(_PLAN)
    assert c.mark_touched("harness.py") is True
    assert c.mark_touched("harness.py") is False
    assert c.done == 1


def test_an_unplanned_file_ticks_nothing() -> None:
    """Touching a file the plan never mentioned is not progress against the
    plan — it may be the model going somewhere unplanned, which is exactly
    what an operator should notice."""
    c = PlanChecklist(_PLAN)
    assert c.mark_touched("some/other/file.py") is False
    assert c.done == 0


def test_completion_is_reported() -> None:
    c = PlanChecklist(_PLAN)
    for item in _PLAN:
        c.mark_touched(item["file_path"])
    assert c.complete is True
    assert "3/3" in c.render()[0]


# --------------------------------------------------------------------------
# 3. it stays a glance
# --------------------------------------------------------------------------

def test_a_single_item_plan_renders_nothing() -> None:
    """One change with one tick is a checklist that never had a decision in
    it, and it would push real work off screen for no information."""
    assert PlanChecklist([{"file_path": "a.py", "description": "x"}]).render() == []


def test_an_empty_plan_renders_nothing() -> None:
    assert PlanChecklist([]).render() == []


def test_a_long_plan_is_windowed_and_says_what_remains() -> None:
    """A silent truncation reads as "that is the whole plan"."""
    big = [{"file_path": f"f{i}.py", "description": f"step {i}"}
           for i in range(40)]
    out = "\n".join(PlanChecklist(big).render())
    assert "more" in out and "outstanding" in out
    assert len(PlanChecklist(big).render()) < 20


def test_the_subordinate_glyph_opens_the_block_once() -> None:
    """Repeating ⎿ per row makes one list read as several separate results."""
    lines = PlanChecklist(_PLAN).render()
    assert lines[1].strip().startswith("⎿")
    assert not lines[2].strip().startswith("⎿")


def test_the_separator_survives_clipping() -> None:
    """`_short` collapses whitespace, so composing " · desc" BEFORE clipping
    yields "file.py· desc"."""
    out = "\n".join(PlanChecklist(_PLAN).render())
    assert "py · " in out
    assert "py· " not in out


def test_a_malformed_change_is_skipped_not_fatal() -> None:
    c = PlanChecklist([{"nonsense": 1}, {"file_path": "a.py", "description": "x"},
                       {"file_path": "b.py", "description": "y"}])
    assert c.total == 2


def test_dataclass_style_changes_are_accepted() -> None:
    """`ordered_changes` may arrive as objects rather than dicts."""
    class _Change:
        file_path = "a/b.py"
        description = "do the thing"
        change_type = "modify"

    assert PlanChecklist([_Change(), _Change()]).total == 2


# --------------------------------------------------------------------------
# 4. the registry, and both wiring seams
# --------------------------------------------------------------------------

def test_a_tick_renders_only_on_a_REAL_transition() -> None:
    """Rendering on every edit would make the checklist a repeated banner
    rather than a progress signal."""
    register_plan("op-1", _PLAN)
    assert note_file_touched("op-1", "thin_client.py")
    assert note_file_touched("op-1", "thin_client.py") == []


def test_an_op_with_no_plan_renders_nothing() -> None:
    """Trivial ops skip planning — silence is correct."""
    assert note_file_touched("op-never-planned", "x.py") == []


def test_the_registry_is_bounded() -> None:
    for i in range(200):
        register_plan(f"op-{i}", _PLAN)
    # The oldest are evicted; the newest still resolve.
    assert note_file_touched("op-199", "thin_client.py")
    assert note_file_touched("op-0", "thin_client.py") == []


def test_plan_registration_happens_at_PLAN_completion() -> None:
    src = (_REPO / "backend/core/ouroboros/governance/orchestrator.py").read_text()
    assert "register_plan(" in src


def test_the_tick_happens_where_the_diff_is_rendered() -> None:
    """One seam: the edit event already carries the path, and reusing it
    means there is no second source of truth about what was touched."""
    src = (_REPO / "backend/core/ouroboros/battle_test/serpent_flow.py").read_text()
    body = ""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "op_tool_call":
            body = ast.unparse(node)
            break
    assert "note_file_touched" in body
    assert "show_diff" in body


def test_checklist_lines_go_through_the_MIRRORED_path() -> None:
    """`_op_line` reaches an attached cockpit; a console print does not."""
    src = (_REPO / "backend/core/ouroboros/battle_test/serpent_flow.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "op_tool_call":
            body = ast.unparse(node)
            idx = body.index("note_file_touched")
            assert "_op_line" in body[idx:idx + 400]
            return
    pytest.fail("op_tool_call is gone")


def test_the_switch_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.ouroboros.battle_test.plan_checklist import (
        checklist_enabled,
    )
    assert checklist_enabled() is True
    monkeypatch.setenv("JARVIS_PLAN_CHECKLIST_ENABLED", "0")
    assert checklist_enabled() is False
    assert PlanChecklist(_PLAN).render() == []
