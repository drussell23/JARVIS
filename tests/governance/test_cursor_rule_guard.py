"""Layer-1 spine — cursor_rule_guard durability pin.

Proves: GREEN on the real shipped `.cursor/rules` rule today, and
RED when the rule is deleted / emptied / deactivated / gutted of
any load-bearing token. A pin that cannot fail is worthless.
"""
from __future__ import annotations

import ast
from pathlib import Path

from backend.core.ouroboros.governance import cursor_rule_guard as g


def test_real_rule_is_green():
    assert g.cursor_rule_path().exists()
    assert g.evaluate_rule() == (), (
        "shipped .cursor/rules ban must be present/active/un-gutted"
    )


def test_registers_one_invariant_and_self_validates():
    invs = g.register_shipped_invariants()
    assert len(invs) == 1
    # validate ignores AST; reads the on-disk rule.
    assert invs[0].validate(ast.parse("x=1"), "x=1") == ()


def _patch_rule(monkeypatch, tmp_path, text):
    p = tmp_path / "no-agent-git-write.mdc"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(g, "cursor_rule_path", lambda: p)
    return p


def test_red_when_missing(monkeypatch, tmp_path):
    _patch_rule(monkeypatch, tmp_path, None)  # never written
    v = g.evaluate_rule()
    assert any("MISSING" in s for s in v), v


def test_red_when_empty(monkeypatch, tmp_path):
    _patch_rule(monkeypatch, tmp_path, "   \n  ")
    v = g.evaluate_rule()
    assert any("EMPTY" in s for s in v), v


def test_red_when_not_active(monkeypatch, tmp_path):
    body = (
        "---\ndescription: x\n---\n"
        "git commit git push git reset git add background agent "
        "worktree commit-authority operator"
    )
    _patch_rule(monkeypatch, tmp_path, body)
    v = g.evaluate_rule()
    assert any("not active" in s for s in v), v


def test_red_when_gutted_missing_tokens(monkeypatch, tmp_path):
    body = (
        "---\nalwaysApply: true\n---\n"
        "Be nice. (all prohibition tokens removed)"
    )
    _patch_rule(monkeypatch, tmp_path, body)
    v = g.evaluate_rule()
    assert any("gutted" in s for s in v), v


def test_green_on_synthetic_complete_rule(monkeypatch, tmp_path):
    body = (
        "---\nalwaysApply: true\n---\n"
        "Background agent: never git commit / git push / git reset "
        "/ git add on the operator checkout. Use a worktree. The "
        "commit-authority daemon is the operator path."
    )
    _patch_rule(monkeypatch, tmp_path, body)
    assert g.evaluate_rule() == ()


def test_alwaysapply_whitespace_tolerant(monkeypatch, tmp_path):
    body = (
        "---\nalwaysApply   :    true\n---\n"
        "git commit git push git reset git add background agent "
        "worktree commit-authority operator"
    )
    _patch_rule(monkeypatch, tmp_path, body)
    assert g.evaluate_rule() == ()


def test_evaluate_never_raises_on_unreadable(monkeypatch):
    monkeypatch.setattr(
        g, "cursor_rule_path", lambda: Path("/nonexistent/zz.mdc"),
    )
    # missing → MISSING violation, not an exception
    v = g.evaluate_rule()
    assert isinstance(v, tuple) and v
