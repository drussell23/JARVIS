from __future__ import annotations
import ast
from backend.core.ouroboros.governance.fleet_repair_battery import BATTERY, Defect
from backend.core.ouroboros.governance.fleet_repair_mutator import mutate, seed_from_text


def _defect(name):
    return next(d for d in BATTERY if d.name == name)


def test_mutate_renames_fn_in_both_and_stays_ast_valid():
    d = _defect("arithmetic")
    m = mutate(d, seed=123)
    assert m.fn_name != d.fn_name
    assert m.fn_name in m.buggy_src and m.fn_name in m.test_src
    ast.parse(m.buggy_src)              # variant still parses
    ast.parse(m.test_src)


def test_mutate_preserves_the_bug():
    # arithmetic bug = subtraction; renaming must NOT fix it
    m = mutate(_defect("arithmetic"), seed=7)
    body = m.buggy_src.split("return")[1]
    assert "-" in body and "+" not in body


def test_mutate_deterministic_for_seed():
    d = _defect("comparison")
    assert mutate(d, seed=42).buggy_src == mutate(d, seed=42).buggy_src
    assert mutate(d, seed=1).fn_name != mutate(d, seed=2).fn_name


def test_mutate_renames_locals():
    m = mutate(_defect("missing_return"), seed=9)   # has a local `result`
    assert "result_" in m.buggy_src                 # local was renamed


def test_seed_from_text_deterministic():
    assert seed_from_text("abc") == seed_from_text("abc")
    assert seed_from_text("abc") != seed_from_text("xyz")
    assert seed_from_text("") >= 0


def test_mutate_failsoft_on_garbage():
    bad = Defect("bad", "f", "def (((", "x")
    assert mutate(bad, seed=1) is bad   # unparseable -> original returned
