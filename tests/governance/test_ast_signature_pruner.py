"""Signature-level context: smaller, and still true.

Measured across 4,639 local-lane generations: prompt p50 10,510 tokens, p90
22,770, max 32,756 -- against a transport that negotiates num_ctx=32,768. The
largest prompts leave the model twelve tokens to answer in, which is how a
32k prompt produces a sub-200-token completion.

The failure that costs: of validation failures carrying an identifiable
exception, ~63% are AttributeError / ModuleNotFoundError / ImportError --
code written against APIs that do not exist -- against 6 SyntaxError in the
same corpus. Structure is near-perfect; recall out of 20k tokens is not.

So the tests below care about two properties, in this order: the pruned text
must still be TRUE (every public symbol survives, nothing is invented), and
only then that it is smaller. A prompt that is small and misleading is worse
than the one it replaced.
"""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.governance.ast_signature_pruner import (
    Detail,
    dependency_budget_tokens,
    fit_dependencies,
    names_only,
    prune_to_signatures,
    symbols_of,
)

MODULE = '''
"""Module doc."""
import os

CONSTANT = 3


def public(a, b=2, *args, **kwargs):
    """What it promises."""
    x = a + b
    for _ in range(10):
        x *= 2
    return x


async def async_public(c: int) -> str:
    """Async contract."""
    return str(c)


def _private():
    """Not API."""
    return 1


class Thing:
    """A class."""

    def method(self, q):
        """Method contract."""
        return q * 2

    def _helper(self):
        return 0
'''


# ---------------------------------------------------------------------------
# Still true
# ---------------------------------------------------------------------------


def test_every_public_symbol_survives():
    pruned = prune_to_signatures(MODULE)
    assert pruned is not None
    names = symbols_of(pruned)
    assert "public" in names
    assert "async_public" in names
    assert "Thing" in names
    assert "method" in names


def test_signatures_survive_exactly():
    """Defaults, *args, **kwargs and annotations ARE the contract -- they are
    what a caller must get right, and the reason to show the file at all."""
    pruned = prune_to_signatures(MODULE)
    tree = ast.parse(pruned)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "public")
    assert [a.arg for a in fn.args.args] == ["a", "b"]
    assert fn.args.vararg.arg == "args"
    assert fn.args.kwarg.arg == "kwargs"
    assert ast.unparse(fn.args.defaults[0]) == "2"

    afn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_public")
    assert ast.unparse(afn.returns) == "str"


def test_docstrings_survive():
    """The docstring is the only prose statement of intent a caller gets."""
    pruned = prune_to_signatures(MODULE)
    assert "What it promises." in pruned
    assert "Method contract." in pruned


def test_bodies_do_not_survive():
    pruned = prune_to_signatures(MODULE)
    assert "for _ in range(10)" not in pruned
    assert "x *= 2" not in pruned


def test_private_symbols_are_dropped_by_default():
    pruned = prune_to_signatures(MODULE)
    assert "_private" not in pruned
    assert "_helper" not in pruned


def test_private_symbols_kept_when_asked():
    pruned = prune_to_signatures(MODULE, public_only=False)
    assert "_private" in pruned


def test_pruned_output_always_parses():
    """A slice that cannot compile is worse than no slice: it teaches the
    model a syntax it will reproduce."""
    ast.parse(prune_to_signatures(MODULE))


def test_unparseable_input_yields_no_signatures():
    assert prune_to_signatures("def broken(:\n    pass\n") is None


def test_names_only_is_the_coarsest_true_statement():
    text = names_only(MODULE, "pkg/mod.py")
    assert "pkg/mod.py" in text
    assert "public" in text
    assert "def public" not in text


def test_names_only_survives_unparseable_source():
    text = names_only("def broken(:", "pkg/mod.py")
    assert "pkg/mod.py" in text


# ---------------------------------------------------------------------------
# And smaller
# ---------------------------------------------------------------------------


def test_signatures_are_substantially_smaller():
    pruned = prune_to_signatures(MODULE)
    assert len(pruned) < len(MODULE) * 0.75


def test_real_module_reduction_is_large():
    """The whole point, on a file the size the pipeline actually pastes."""
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/interactive_repair.py"
    )
    source = target.read_text()
    pruned = prune_to_signatures(source)
    assert pruned is not None
    assert len(pruned) < len(source) * 0.25, (
        f"reduction only {100 * (1 - len(pruned) / len(source)):.0f}%"
    )


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_generous_budget_keeps_full_text():
    pruned, _total = fit_dependencies(
        [("m.py", MODULE)], budget_tokens=1_000_000,
    )
    assert pruned[0].detail is Detail.FULL
    assert pruned[0].text == MODULE


def test_tight_budget_degrades_to_signatures():
    pruned, total = fit_dependencies([("m.py", MODULE)], budget_tokens=90)
    assert pruned[0].detail is Detail.SIGNATURES
    assert total <= 90


def test_tightest_budget_degrades_to_names():
    pruned, _total = fit_dependencies([("m.py", MODULE)], budget_tokens=20)
    assert pruned[0].detail is Detail.NAMES


def test_ladder_degrades_all_modules_together():
    """Never truncate the tail. A caller shown three complete modules and
    nothing about the fourth will confidently invent the fourth."""
    pruned, _total = fit_dependencies(
        [("a.py", MODULE), ("b.py", MODULE), ("c.py", MODULE)],
        budget_tokens=260,
    )
    assert len({m.detail for m in pruned}) == 1
    assert {m.path for m in pruned} == {"a.py", "b.py", "c.py"}


def test_every_module_keeps_a_true_statement_at_every_rung():
    for budget in (1_000_000, 200, 20, 1):
        pruned, _ = fit_dependencies(
            [("a.py", MODULE), ("b.py", MODULE)], budget_tokens=budget,
        )
        assert len(pruned) == 2
        assert all(m.text.strip() for m in pruned)


def test_symbols_are_reported_from_the_original_not_the_slice():
    """The symbol list must describe the module, not the rung it was shown
    at -- otherwise a NAMES rung would report having no symbols."""
    pruned, _ = fit_dependencies([("m.py", MODULE)], budget_tokens=1)
    assert "public" in pruned[0].symbols


def test_empty_input_is_empty_output():
    assert fit_dependencies([]) == ([], 0)


def test_junk_never_raises():
    for junk in (None, [], [("", "")], [("a.py", None)]):
        fit_dependencies(junk)


# ---------------------------------------------------------------------------
# Budget derivation
# ---------------------------------------------------------------------------


def test_operator_pin_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_DEPENDENCY_CONTEXT_TOKENS", "4000")
    assert dependency_budget_tokens() == 4000


def test_fraction_scales_the_window(monkeypatch):
    monkeypatch.delenv("JARVIS_DEPENDENCY_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("JARVIS_DEPENDENCY_CONTEXT_FRACTION", "0.5")
    wide = dependency_budget_tokens()
    monkeypatch.setenv("JARVIS_DEPENDENCY_CONTEXT_FRACTION", "0.05")
    narrow = dependency_budget_tokens()
    assert wide > narrow


def test_nonsense_fraction_falls_back(monkeypatch):
    monkeypatch.delenv("JARVIS_DEPENDENCY_CONTEXT_TOKENS", raising=False)
    for bad in ("0", "-1", "9", "banana"):
        monkeypatch.setenv("JARVIS_DEPENDENCY_CONTEXT_FRACTION", bad)
        assert dependency_budget_tokens() > 0


def test_budget_is_never_zero(monkeypatch):
    """A zero budget would degrade every module to names forever."""
    monkeypatch.delenv("JARVIS_DEPENDENCY_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("JARVIS_DEPENDENCY_CONTEXT_FRACTION", raising=False)
    assert dependency_budget_tokens() >= 256
