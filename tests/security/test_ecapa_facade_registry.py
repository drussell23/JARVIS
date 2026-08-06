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


# =============================================================================
# LIFECYCLE — construction is not enough; something must START it
# =============================================================================


def test_the_accessor_starts_the_lifecycle():
    """
    `start()` had ZERO call sites.

    It is documented "Non-blocking and idempotent", and every caller does
    `get_ecapa_facade()` then `ensure_ready()`. But `ensure_ready()` only waits
    on an event that a state transition sets, and only `start()` initiates that
    transition. With nothing driving the machine it stayed UNINITIALIZED
    forever, every `ensure_ready()` burned its full timeout, and the operator
    was told "I'm still loading my voice recognition" — for a load never begun.

    Observed 2026-08-06 14:24:14, ninety seconds after boot, on an unlock that
    then refused.

    STRUCTURAL, BECAUSE THE OUTCOME IS NOT OURS TO ASSERT
    -----------------------------------------------------
    An earlier version asserted the resulting STATE. That is undecidable here:
    the facade takes a machine-global `flock`, so on any machine where another
    JARVIS process is running — a battle test, a daemon, a second HUD — the
    correct outcome is DualAuthorityError and an unstarted facade. Measured
    exactly that on 2026-08-06 with `ouroboros_battle_test.py` live.

    A test that fails when the system is behaving correctly trains people to
    ignore it. What IS ours to assert is that the accessor calls `start()` at
    all — the thing that was missing.
    """
    source = (REPO_ROOT / "backend/core/ecapa_facade.py").read_text()
    tree = ast.parse(source)

    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_ecapa_facade"
    )
    starts = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "start"
    ]
    assert starts, (
        "get_ecapa_facade() no longer calls start(); the state machine will "
        "never leave UNINITIALIZED and every ensure_ready() will time out"
    )


def test_start_failure_does_not_break_the_caller():
    """
    The cross-process fence can legitimately refuse — another JARVIS process may
    own ECAPA. That must not raise into a voice command: the facade is returned
    unstarted, ensure_ready() reports not-ready, and the voice layer says so.
    A second process must not break the first one's callers.
    """
    class _Exploding(_Registry):
        pass

    import backend.core.ecapa_facade as mod

    original = mod.EcapaFacade.start

    async def _boom(self):
        raise RuntimeError("fence held elsewhere")

    mod.EcapaFacade.start = _boom
    try:
        facade = asyncio.run(get_ecapa_facade(registry=_Exploding()))
        assert facade is not None, "a failed start must still yield a facade"
    finally:
        mod.EcapaFacade.start = original


def test_lock_error_does_not_present_stale_file_contents_as_the_holder():
    """
    The fence's error used to read the lock FILE and name its pid as the holder.

    The file persists across runs; the flock does not. On 2026-08-06 that
    surfaced a months-dead pid (20415, from May) and produced a confident
    diagnosis of a stale lock that did not exist — flock is advisory and the
    kernel drops it when a process dies, so a stale file blocks nothing.
    """
    source = (REPO_ROOT / "backend/core/ecapa_facade.py").read_text()
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    claims = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
        and "Another process (PID" in n.value
    ]
    assert not claims, (
        "the lock error again asserts a holder identity it cannot know"
    )
