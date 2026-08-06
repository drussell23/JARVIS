"""
A precondition no caller can satisfy is not a precondition.

`get_ecapa_facade()` raised ``ValueError("registry required for first facade
creation")`` when called without a registry. Every call site in the codebase
calls it without one, so the singleton was uncreatable by construction and the
facade was dead code that announced itself as a configuration error::

    [INIT] EcapaFacade error: registry required for first facade creation
        - falling back to local engine

Observed on every boot, and downstream on 2026-08-06 at 13:57:04 an unlock
refused because the fallback engine held no voiceprints.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from backend.core.ecapa_facade import _reset_facade, get_ecapa_facade

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Registry:
    """Minimum contract the facade documents: ``get_wrapper(name)``."""

    def get_wrapper(self, name):  # noqa: D102
        return object()


@pytest.fixture(autouse=True)
def _clean_singleton():
    _reset_facade()
    yield
    _reset_facade()


def test_the_no_argument_call_every_caller_makes_now_works():
    """
    The regression, stated exactly as the callers state it.

    Six sites call `get_ecapa_facade()` with no arguments. If this raises, the
    facade is unreachable in production no matter how correct the rest of it is.
    """
    facade = asyncio.run(get_ecapa_facade())
    assert facade is not None


def test_injected_registry_still_wins():
    """Explicit injection must keep working — tests and any holder rely on it."""
    reg = _Registry()
    facade = asyncio.run(get_ecapa_facade(registry=reg))
    assert facade is not None


def test_singleton_is_preserved():
    """Second call returns the same instance; no second ECAPA model is loaded."""
    first = asyncio.run(get_ecapa_facade(registry=_Registry()))
    second = asyncio.run(get_ecapa_facade())
    assert first is second


def test_a_registry_that_cannot_answer_is_rejected_at_construction():
    """
    Fail where it is diagnosable.

    A registry with no ``get_wrapper`` cannot supply 'ecapa_tdnn'. Discovering
    that later, inside a verification, makes a broken registry look like a voice
    that did not match — which is the failure this whole arc has been about.
    """
    class _Useless:
        pass

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(get_ecapa_facade(registry=_Useless()))

    assert "get_wrapper" in str(excinfo.value)


def test_failure_message_does_not_blame_the_caller():
    """
    The old message said "registry required for first facade creation", which
    reads as "you forgot an argument" — so six call sites were each read as
    correct and the accessor was never suspected. Any future failure must name
    registry availability instead.
    """
    source = (REPO_ROOT / "backend/core/ecapa_facade.py").read_text()
    tree = ast.parse(source)

    # Docstrings are exempt: the fix's own documentation quotes the old message
    # to explain what it replaced, and a guard that cannot tell an explanation
    # from an executable string would forbid describing the bug it prevents.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    live = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
        and "registry required for first facade creation" in n.value
    ]
    assert not live, (
        "the caller-blaming message is back as a runtime string; "
        "it hid this defect for months"
    )


def test_no_call_site_passes_a_registry():
    """
    Pins WHY the fix belongs in the accessor.

    If a call site ever starts passing one, that is fine — but if ALL of them
    did, knowledge of which registry to use would be spread across six modules
    for a singleton that has exactly one owner. This test documents the shape
    the fix assumes, and fails loudly if that shape changes.
    """
    sites = []
    for path in REPO_ROOT.joinpath("backend").rglob("*.py"):
        if "venv" in path.parts or path.name == "ecapa_facade.py":
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "get_ecapa_facade(" not in text:
            continue
        for call in re.findall(r"get_ecapa_facade\(([^)]*)\)", text):
            sites.append((path.name, call.strip()))

    assert sites, "no call sites found — has the facade been removed?"
    with_registry = [s for s in sites if s[1]]
    assert not with_registry, (
        f"a call site now passes arguments: {with_registry}. Re-read this test; "
        f"the accessor-side resolution may no longer be the right shape."
    )
