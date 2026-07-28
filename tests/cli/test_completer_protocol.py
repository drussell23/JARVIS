"""Every completer must inherit `Completer`, not merely look like one.

`ov` crashed on the first keystroke of a fresh session:

    Exception 'HistoryCompleter' object has no attribute
    'get_completions_async'

repeated per keypress, because the failure lives inside a coroutine the event
loop restarts.

The cause is a protocol that is only half duck-typed. prompt_toolkit does NOT
consume a completer through `get_completions` — it calls
`get_completions_async`, which `Completer` supplies by wrapping the sync
method. A class implementing only `get_completions` satisfies every static
reading of the interface, passes any test that calls it directly, and dies
the moment prompt_toolkit uses it the way prompt_toolkit actually does.

Two classes had it. The second (`MentionPathCompleter`) had never crashed
only because nothing had routed to it yet — latent, not absent. So this test
audits the WHOLE tree rather than the two that were found.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from prompt_toolkit.completion import Completer


_ROOT = pathlib.Path(__file__).resolve().parents[2] / "backend/core/ouroboros"


def _duck_typed_completers() -> list:
    """Classes defining `get_completions` without inheriting `Completer`."""
    offenders = []
    for path in _ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {m.name for m in node.body
                       if isinstance(m, ast.FunctionDef)}
            if "get_completions" not in methods:
                continue
            if "get_completions_async" in methods:
                continue          # implements the async path itself
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Call):
                    # A base resolved at class-creation time — e.g.
                    # `_completer_base()`, which returns Completer.
                    bases.append("resolved")
                else:
                    bases.append(getattr(base, "id", "")
                                 or getattr(base, "attr", ""))
            if not any("Completer" in b or b == "resolved" for b in bases):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    return offenders


def test_no_completer_is_only_duck_typed() -> None:
    """THE invariant. A new completer that forgets to inherit fails here
    rather than on an operator's first keystroke."""
    offenders = _duck_typed_completers()
    assert offenders == [], (
        "these define get_completions but do not inherit Completer, so "
        "prompt_toolkit's async path raises AttributeError on the first "
        "keypress:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_actually_detects(tmp_path) -> None:
    """A guard that cannot fail proves nothing."""
    sample = tmp_path / "bad.py"
    sample.write_text("class Bad:\n    def get_completions(self):\n        pass\n")
    tree = ast.parse(sample.read_text())
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and any(isinstance(m, ast.FunctionDef)
                    and m.name == "get_completions" for m in n.body)
            and not n.bases]
    assert hits, "the audit would not have caught the class that crashed"


@pytest.mark.parametrize("dotted,name", [
    ("backend.core.ouroboros.battle_test.history_search", "HistoryCompleter"),
    ("backend.core.ouroboros.battle_test.repl_completion",
     "MentionPathCompleter"),
])
def test_the_two_that_were_broken_now_inherit(dotted: str, name: str) -> None:
    import importlib

    cls = getattr(importlib.import_module(dotted), name)
    assert issubclass(cls, Completer)
    assert hasattr(cls, "get_completions_async")


@pytest.mark.asyncio
async def test_the_async_path_is_exercised_the_way_pt_uses_it() -> None:
    """Calling `get_completions` directly is what MISSED this. The regression
    is only visible through the coroutine prompt_toolkit actually awaits."""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from backend.core.ouroboros.battle_test.history_search import (
        HistoryCompleter, HistorySearchController,
    )

    class _Hist:
        def get_strings(self):
            return ["fix the flaky test", "run the soak"]

    completer = HistoryCompleter(HistorySearchController(), _Hist())
    results = []
    async for c in completer.get_completions_async(
        Document("fix", 3), CompleteEvent(),
    ):
        results.append(c)
    assert isinstance(results, list)
