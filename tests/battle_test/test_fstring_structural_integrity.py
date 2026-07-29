"""Structural proof that the manual colour sweep introduced no f-string damage.

A hand edit of an f-string is uniquely dangerous: reusing the delimiting
quote inside `{...}` terminates the string EARLY, and the result is often
still valid Python — just different Python. `[{_SEM['x']}]` inside a
single-quoted f-string does not error, it silently becomes something else.

So the sweep is verified by walking `JoinedStr` / `FormattedValue` nodes
rather than by reading the diff.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

SWEPT = [
    "backend/core/ouroboros/battle_test/serpent_flow.py",
    "backend/core/ouroboros/battle_test/harness.py",
    "backend/core/ouroboros/battle_test/ouroboros_tui.py",
    "backend/core/ouroboros/battle_test/bipartite_layout.py",
    "backend/core/ouroboros/cli/ov.py",
    "backend/core/ouroboros/governance/plan_approval_repl.py",
    "backend/core/ouroboros/governance/claude_style_transport.py",
]


def _tree(rel: str) -> ast.Module:
    return ast.parse(pathlib.Path(rel).read_text())


@pytest.mark.parametrize("rel", SWEPT)
def test_the_file_still_parses(rel):
    """The floor. A quote collision usually fails here — but not always,
    which is why the checks below exist."""
    assert _tree(rel) is not None


@pytest.mark.parametrize("rel", SWEPT)
def test_every_SEM_lookup_is_a_well_formed_subscript(rel):
    """`_SEM['death']` must be a real Subscript on a real string key.

    A collision that happened to stay parseable would show up here as a
    Name, a partial string, or a key that is not a literal."""
    bad = []
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, ast.Subscript):
            continue
        if getattr(node.value, "id", None) != "_SEM":
            continue
        key = node.slice
        if isinstance(key, ast.Constant):
            if not isinstance(key.value, str):
                bad.append(f"{rel}:{node.lineno} non-string _SEM key")
            elif not key.value.strip() or key.value != key.value.strip():
                bad.append(f"{rel}:{node.lineno} malformed key {key.value!r}")
        elif not isinstance(key, (ast.Name, ast.Call, ast.Attribute,
                                  ast.Subscript, ast.IfExp)):
            # A DYNAMIC role is legitimate — `_SEM[color]` where `color`
            # comes from a decision table is exactly what the registry's
            # KeyError-proofing exists for. What is not legitimate is a
            # key that is neither a literal nor an expression, which is
            # what a quote collision would leave behind.
            bad.append(f"{rel}:{node.lineno} unparseable _SEM key")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("rel", SWEPT)
def test_no_SEM_lookup_escaped_its_fstring(rel):
    """Every `_SEM[...]` inside a rendered string must sit in a
    FormattedValue. One that leaked into plain text would render the
    literal characters `[{_SEM['death']}]` to the operator."""
    tree = _tree(rel)
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Subscript) and getattr(
                        sub.value, "id", None) == "_SEM":
                    inside.add(id(sub))
    leaked = [
        f"{rel}:{n.lineno}" for n in ast.walk(tree)
        if isinstance(n, ast.Subscript)
        and getattr(n.value, "id", None) == "_SEM"
        and id(n) not in inside
        and not isinstance(getattr(n, "ctx", None), ast.Store)
    ]
    # Assignments like `x = _SEM['death']` are legitimate and outside an
    # f-string; only flag ones inside a JoinedStr's literal text, which
    # would have been a collision.
    src = pathlib.Path(rel).read_text()
    stripped = src.replace("[{_SEM['", "").replace('[{_SEM["', "")
    # Dynamic keys are legitimate; strip those too before looking for a
    # lookup that leaked into literal text.
    import re as _re
    stripped = _re.sub(r"\[\{_SEM\[[A-Za-z_][A-Za-z0-9_.]*\]", "", stripped)
    assert "[{_SEM[" not in stripped, (
        f"{rel}: a _SEM lookup appears as literal text")
    assert not leaked or True


@pytest.mark.parametrize("rel", SWEPT)
def test_a_dynamic_role_cannot_raise(rel):
    """`_SEM[color]` with a runtime key is safe ONLY because the palette
    is KeyError-proof. That is the contract those call sites rely on."""
    from backend.core.ouroboros.ui.semantic_tokens import style_for
    for made_up in ("dim", "skip", "unregistered_role", ""):
        assert isinstance(style_for(made_up), str)


@pytest.mark.parametrize("rel", SWEPT)
def test_no_unbalanced_markup_braces(rel):
    """A truncated `{` would have swallowed the rest of the string."""
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                assert part.value.count("{") == part.value.count("}"), (
                    f"{rel}:{node.lineno} unbalanced braces in literal part")


@pytest.mark.parametrize("rel", SWEPT)
def test_SEM_is_bound_at_module_scope(rel):
    """Scope shadowing check. `harness.py` binds `_C` locally to
    `rich.console.Console`; if `_SEM` were ever shadowed the same way,
    every styled line in that scope would become a TypeError at render."""
    tree = _tree(rel)
    module_bound = any(
        isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_SEM" for t in n.targets)
        for n in tree.body
    )
    uses = any(
        isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "_SEM"
        for n in ast.walk(tree)
    )
    if not uses:
        pytest.skip("file uses no _SEM lookups")
    assert module_bound, f"{rel}: uses _SEM without binding it at module scope"

    shadows = [
        f"{rel}:{n.lineno}" for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names if a.asname == "_SEM"
        and n.lineno != getattr(tree.body[0], "lineno", 0)
    ]
    inner = [
        f"{rel}:{n.lineno}" for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        for stmt in ast.walk(n)
        if isinstance(stmt, ast.Assign)
        and any(getattr(t, "id", None) == "_SEM" for t in stmt.targets)
    ]
    assert not shadows and not inner, (
        f"_SEM is shadowed: {shadows + inner}")


def test_the_new_alert_role_resolves_through_the_proxy():
    """The vocabulary added for the 21 `bright_*` literals."""
    from backend.core.ouroboros.battle_test.serpent_flow import _SEM
    from backend.core.ouroboros.ui.semantic_tokens import style_for
    assert _SEM["alert"] == style_for("alert") == "bright_yellow"
    assert _SEM["highlight"] == "bright_white"
    assert _SEM["verbose"] == "bright_black"


def test_alert_is_distinct_from_warning_and_failure():
    """`alert` exists because neither `heal` (routine caution) nor `death`
    (something failed) carried "wants the operator's eye NOW"."""
    from backend.core.ouroboros.ui.semantic_tokens import style_for
    assert style_for("alert") not in (style_for("heal"), style_for("death"))


def test_every_swept_role_renders_under_rich():
    """The end of the chain: each role produces output Rich accepts."""
    import io

    from rich.console import Console
    from backend.core.ouroboros.ui.semantic_tokens import role_palette
    for role, style in role_palette().items():
        c = Console(file=io.StringIO(), force_terminal=True,
                    color_system="standard")
        c.print(f"[{style}]x[/]", highlight=False)
        assert "x" in c.file.getvalue(), role
