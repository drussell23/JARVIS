"""Behavioural contracts for tests that currently pin source strings.

The problem these replace
------------------------

2,323 assertions across 281 test files have the shape
``assert "<literal>" in src``. Three of them were found holding defects in
place during the micro-fix audit:

* ``assert err.line_number > 0  # Must be > 0 to defeat the hard-guard`` --
  on input stating no line. The cascade satisfied it by returning 1, a
  placeholder shaped like a location, and the safety guard was defeated by
  a test written to defeat it.
* ``assert "_repair_abs = _repair_root / _repair_target" in src`` -- pinning
  the disk read that WAS the defect, so the pin had to be removed before the
  bug could be.
* ``assert "_test_argv.extend(_fail_to_pass)" in src`` -- whose docstring
  named the real invariant ("without this the model still sees the full
  noise of an unscoped pytest run") while the assertion matched a spelling.

A string pin fails on safe refactors and passes on comments containing the
magic substring. It tests neither behaviour nor structure -- it tests
spelling, and spelling is the one property no one intends to freeze.

What replaces them
------------------

Contracts over the module's AST. They answer the questions a string pin was
reaching for -- does this call happen, with this argument, in this order,
inside this function -- against the parsed program rather than its text, so
renaming a local or rewrapping a line cannot break them and deleting the
call cannot pass them.

Prefer a real behavioural test (import the function, call it, assert the
outcome) over any contract here. These exist for invariants with no
addressable seam: construction-site arguments, call ordering, the absence of
a forbidden call. That is a narrow set, and narrower than 2,323.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Sequence


def parse_module(path: Path) -> ast.Module:
    """Parse *path*, raising with the file named if it will not parse."""
    try:
        return ast.parse(path.read_text(errors="replace"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a broken tree fails loudly
        raise AssertionError(f"{path} does not parse: {exc}") from exc


def _calls(tree: ast.AST) -> List[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def function_named(tree: ast.AST, name: str) -> Optional[ast.AST]:
    """The function or method *name*, at any nesting depth."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def calls_to(tree: ast.AST, callee: str) -> List[ast.Call]:
    """Every call to *callee*, by bare name or attribute."""
    return [c for c in _calls(tree) if _call_name(c) == callee]


def keyword_argument(call: ast.Call, name: str) -> Optional[str]:
    """The source of keyword *name* on *call*, unparsed back to text.

    Unparsed from the AST rather than sliced from the file, so ``foo(x=a.b)``
    and ``foo(\\n    x=a.b,\\n)`` are the same answer.
    """
    for kw in call.keywords:
        if kw.arg == name:
            return ast.unparse(kw.value)
    return None


def assert_constructed_with(
    tree: ast.AST, *, callee: str, keyword: str, expected: str,
) -> None:
    """*callee* is constructed with ``keyword=expected`` somewhere.

    Replaces ``assert "callee(keyword=expected" in src``: immune to
    reformatting and argument reordering, and it cannot be satisfied by a
    comment.
    """
    found = []
    for call in calls_to(tree, callee):
        value = keyword_argument(call, keyword)
        if value is not None:
            found.append(value)
            if value == expected:
                return
    raise AssertionError(
        f"no call to {callee}(...) passes {keyword}={expected!r}; "
        f"observed {found or 'no such keyword on any call'}"
    )


def assert_never_constructed_with(
    tree: ast.AST, *, callee: str, keyword: str, forbidden: str,
) -> None:
    """No call to *callee* passes ``keyword=forbidden``."""
    for call in calls_to(tree, callee):
        if keyword_argument(call, keyword) == forbidden:
            raise AssertionError(
                f"{callee}(...) is constructed with {keyword}={forbidden!r} "
                f"at line {call.lineno}"
            )


def assert_calls_in_order(
    tree: ast.AST, *, first: str, then: str,
) -> None:
    """*first* is called before *then*.

    Ordering invariants ("resolve the root before constructing the loop")
    were pinned by comparing ``str.find`` offsets, which a comment mentioning
    either name silently breaks.
    """
    firsts = [c.lineno for c in calls_to(tree, first)]
    thens = [c.lineno for c in calls_to(tree, then)]
    if not firsts:
        raise AssertionError(f"{first}(...) is never called")
    if not thens:
        raise AssertionError(f"{then}(...) is never called")
    if min(firsts) > min(thens):
        raise AssertionError(
            f"{first}(...) first called at line {min(firsts)}, after "
            f"{then}(...) at line {min(thens)}"
        )


def assert_assigned_from(
    tree: ast.AST, *, target: str, expected: str,
) -> None:
    """*target* is assigned from *expected* somewhere in the module."""
    seen = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == target and node.value is not None:
                value = ast.unparse(node.value)
                seen.append(value)
                if value == expected:
                    return
    raise AssertionError(
        f"{target} is never assigned from {expected!r}; observed {seen or 'nothing'}"
    )


def assert_no_call_to(tree: ast.AST, callee: str) -> None:
    """*callee* is never called. For invariants stated as prohibitions."""
    hits = [c.lineno for c in calls_to(tree, callee)]
    if hits:
        raise AssertionError(f"{callee}(...) is called at line(s) {hits}")


def literal_defaults(tree: ast.AST, *, callee: str = "get") -> dict:
    """``{env_name: default}`` for two-argument ``os.environ.get`` calls.

    Lets a test assert what a switch defaults to without importing the module
    and inheriting the ambient environment.
    """
    out = {}
    for call in calls_to(tree, callee):
        if len(call.args) != 2:
            continue
        name, default = call.args
        if isinstance(name, ast.Constant) and isinstance(default, ast.Constant):
            out[str(name.value)] = default.value
    return out


__all__ = [
    "assert_assigned_from",
    "assert_calls_in_order",
    "assert_constructed_with",
    "assert_never_constructed_with",
    "assert_no_call_to",
    "calls_to",
    "function_named",
    "keyword_argument",
    "literal_defaults",
    "parse_module",
]
