"""Regression spine for the naming cage — a `*_repl` module IS its verb.

The class of defect under test is not "the dispatcher is wrong". It is
"the dispatcher is correct, tested, documented, and unreachable by a human".
Every assertion here is about REACHABILITY.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from backend.core.ouroboros.governance import repl_verb_cage as cage


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_REPL_VERB_CAGE_ENABLED", raising=False)
    cage.reset_cache()
    yield
    cage.reset_cache()


# -- the contract ----------------------------------------------------------


def test_a_module_claims_the_verb_its_name_spells():
    verbs = {v.verb: v for v in cage.discover()}
    assert "why" in verbs
    assert verbs["why"].module.endswith("why_repl")
    assert verbs["why"].slash_form == "/why"
    assert verbs["why"].dispatch_name == "dispatch_why_command"


def test_both_halves_are_required_to_claim_a_verb(tmp_path):
    """Help alone is a promise; a dispatcher alone is undiscoverable."""
    only_help = tmp_path / "alpha_repl.py"
    only_help.write_text('__verb_help__ = {"alpha": "x"}\n', encoding="utf-8")
    assert cage._read_declaration(only_help, "alpha") is None

    only_disp = tmp_path / "beta_repl.py"
    only_disp.write_text("def dispatch_beta_command(line):\n    return None\n",
                         encoding="utf-8")
    assert cage._read_declaration(only_disp, "beta") is None

    both = tmp_path / "gamma_repl.py"
    both.write_text('__verb_help__ = {"gamma": "does a thing"}\n'
                    "def dispatch_gamma_command(line):\n    return None\n",
                    encoding="utf-8")
    assert cage._read_declaration(both, "gamma") == "does a thing"


def test_a_nested_declaration_is_not_an_export(tmp_path):
    """`getattr` could not find these, so the palette must not offer them."""
    path = tmp_path / "delta_repl.py"
    path.write_text(
        "def _inner():\n"
        '    __verb_help__ = {"delta": "x"}\n'
        "    def dispatch_delta_command(line):\n        return None\n",
        encoding="utf-8")
    assert cage._read_declaration(path, "delta") is None


def test_discovery_never_imports_the_modules_it_scans():
    """A broken verb must not take the whole palette down with it."""
    code = _code_of(cage, "discover", "_read_declaration", "_package_dir")
    assert "import_module" not in code
    assert "__import__" not in code


def test_a_syntactically_broken_module_is_skipped_not_fatal(tmp_path):
    bad = tmp_path / "epsilon_repl.py"
    bad.write_text("def dispatch_epsilon_command(  <<<\n", encoding="utf-8")
    assert cage._read_declaration(bad, "epsilon") is None


# -- reachability: the actual defect ---------------------------------------


def test_why_is_reachable_by_typing_it():
    result = cage.dispatch("/why help")
    assert result is not None, "/why is mounted but unreachable"
    assert result.ok is True
    assert "causal account" in result.text


async def test_reach_is_reachable_by_typing_it():
    result = await cage.dispatch_async("/reach help")
    assert result is not None, "/reach is mounted but unreachable"
    assert "reachability" in result.text


async def test_an_async_verb_is_awaited_by_the_cage():
    """An un-awaited coroutine renders as `<coroutine object …>`.

    The verb would look mounted, print garbage and do nothing — the
    unmounted class wearing a disguise. `/reach` is async because it parses
    a thousand files and must not freeze the loop that runs the organism.
    """
    import inspect

    raw = cage.dispatch("/reach help")
    assert inspect.isawaitable(raw), "/reach stopped being async"
    raw.close()
    assert not inspect.isawaitable(await cage.dispatch_async("/reach help"))


async def test_a_sync_verb_passes_through_the_async_seam_unchanged():
    """A verb author picks def or async def on the merits of the work."""
    assert (await cage.dispatch_async("/why help")).ok is True
    assert await cage.dispatch_async("/no_such_verb_xyz") is None


def test_an_unclaimed_verb_returns_none_so_the_typo_handler_still_runs():
    assert cage.dispatch("/no_such_verb_xyz") is None


def test_a_verb_that_answers_no_is_not_confused_with_an_unclaimed_one():
    """`None` and a falsy result mean different things at the seam."""
    result = cage.dispatch("/why")
    assert result is not None
    assert result.ok is True  # bare /why prints help rather than failing


def test_a_broken_module_refuses_out_loud_rather_than_falling_through(
        monkeypatch):
    """"exists but broken" and "does not exist" call for different actions."""
    import importlib

    def _boom(name):
        raise ImportError("simulated")

    monkeypatch.setattr(importlib, "import_module", _boom)
    result = cage.dispatch("/why help")
    assert result is not None and result.ok is False
    assert "failed to load" in result.text


def test_the_master_switch_disables_the_cage(monkeypatch):
    monkeypatch.setenv("JARVIS_REPL_VERB_CAGE_ENABLED", "false")
    cage.reset_cache()
    assert cage.discover() == ()
    assert cage.dispatch("/why help") is None


def test_a_bare_word_is_not_a_slash_verb():
    assert cage.find("why o-1") is None
    assert cage.find("") is None
    assert cage.find("/") is None


# -- the mount itself ------------------------------------------------------


from tests.source_probe import code_of as _code_of


def test_the_repl_actually_calls_the_cage():
    """Without this the cage is one more thing that is built and inert."""
    from backend.core.ouroboros.battle_test import serpent_flow as sf

    assert "repl_verb_cage" in _code_of(sf), "the cage has no caller"


def test_the_cage_runs_before_the_unknown_verb_handler():
    """After the cage, "did you mean…" would deny a verb that works."""
    from backend.core.ouroboros.battle_test import serpent_flow as sf

    src = inspect.getsource(sf)
    assert src.index("repl_verb_cage") < src.index("suggest_for_typo")


def test_cage_verbs_appear_in_the_palette():
    """A verb that works and is invisible is half a verb."""
    from backend.core.ouroboros.battle_test.repl_completion import (
        unified_registry,
    )

    registry = unified_registry(object())
    assert registry.find("/why") is not None
    assert registry.find("/reach") is not None


async def test_every_contract_honouring_module_is_reachable():
    """The invariant, stated once: declaring the contract IS mounting.

    A future `governance/foo_repl.py` that exports both halves is reachable
    the moment it lands. This test is what makes that true rather than
    aspirational — a regression in the seam fails here, not in production.
    """
    for spec in cage.discover():
        assert await cage.dispatch_async(f"{spec.slash_form} help") is not None, (
            f"{spec.slash_form} declares the contract but is unreachable")


def test_every_declared_description_survives_the_palette_arbiter():
    """A residue description loses the row to a scrape of subcommand names.

    Caught here rather than in a distant palette test, because the module
    that declares a bad description is the one that should fail — otherwise
    the next `*_repl` author learns about it from an unrelated red.
    """
    from backend.core.ouroboros.battle_test.verb_description import (
        Shape, assess,
    )

    for spec in cage.discover():
        verdict = assess(spec.description, spec.verb)
        assert verdict.shape is not Shape.RESIDUE, (
            f"{spec.module} declares a residue description for "
            f"{spec.slash_form}: {verdict.reasons} — {spec.description!r}")
