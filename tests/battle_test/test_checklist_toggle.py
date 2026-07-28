"""Ctrl+T — the plan checklist is orientation, then it is clutter.

A four-item plan is exactly what you want while work runs and exactly what
you do not want while reading a diff, and which of those it is changes minute
to minute. That makes it a keystroke rather than a setting.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.plan_checklist import (
    PlanChecklist, checklist_enabled, checklist_visible, toggle_checklist,
)


@pytest.fixture(autouse=True)
def _restore():
    yield
    toggle_checklist(True)


def _plan() -> PlanChecklist:
    return PlanChecklist([
        {"file_path": "test_runner.py", "description": "prove containment"},
        {"file_path": "gate_runner.py", "description": "route the floor"},
    ])


def test_it_collapses_and_reopens() -> None:
    checklist = _plan()
    assert checklist.render()
    toggle_checklist()
    assert checklist.render() == []
    toggle_checklist()
    assert checklist.render()


def test_tracking_CONTINUES_while_collapsed() -> None:
    """Re-opening must show the plan's real state, not the state it had when
    it was hidden. A checklist that resumed stale would be worse than one
    that was never shown."""
    checklist = _plan()
    toggle_checklist(False)
    assert checklist.mark_touched("test_runner.py") is True
    toggle_checklist(True)
    assert checklist.done == 1
    assert any("☒" in line for line in checklist.render())


def test_the_toggle_does_not_rewrite_CONFIGURATION(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env master says "this cockpit never shows checklists"; the toggle
    says "not while I am reading something else". Collapsing them would let a
    keystroke silently edit configuration, and the next session would inherit
    a decision made about one screenful."""
    toggle_checklist(False)
    assert checklist_enabled() is True, "the keystroke changed the env master"
    assert checklist_visible() is False


def test_the_env_master_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PLAN_CHECKLIST_ENABLED", "0")
    toggle_checklist(True)
    assert checklist_visible() is False
    assert _plan().render() == []


def test_explicit_state_is_idempotent() -> None:
    toggle_checklist(False)
    toggle_checklist(False)
    assert checklist_visible() is False


def test_both_surfaces_bind_it() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        src = (repo / path).read_text()
        names = {a.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "toggle_checklist" in names, f"{path} cannot collapse it"


def test_the_binding_is_registered_and_toggles() -> None:
    """Registered AND invoked — a binding that exists in source but does
    nothing is the most repeated defect here."""
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()
    kb.add("c-t")(lambda event: toggle_checklist())
    assert ("Keys.ControlT",) in [
        tuple(str(k) for k in b.keys) for b in kb.bindings
    ]
    before = checklist_visible()
    kb.bindings[0].handler(object())
    assert checklist_visible() is not before
