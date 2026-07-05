"""TDD spine for the Domain-1 Staging-0 AST structural-delta engine.

Real Oracle ``CodeStructureVisitor`` -- no mocks. Fixtures are inline
before/after Python source strings.
"""
from __future__ import annotations

import json
import re

import pytest

from backend.core.ouroboros.governance.causal.structural_delta import (
    ImportEdge,
    StructuralDelta,
    SymbolRecord,
    SymbolSet,
    compute_file_delta,
    diff_symbol_sets,
    extract_symbol_set,
)

REPO = "jarvis"
FILE = "mod.py"

_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")

BEFORE_TWO = (
    "def alpha(x):\n"
    "    return x + 1\n"
    "\n"
    "def beta(y):\n"
    "    return y - 1\n"
)

AFTER_ADD = (
    "def alpha(x):\n"
    "    return x + 1\n"
    "\n"
    "def beta(y):\n"
    "    return y - 1\n"
    "\n"
    "def gamma(z):\n"
    "    return z * 2\n"
)


def test_add_one_function():
    delta = compute_file_delta(REPO, FILE, BEFORE_TWO, AFTER_ADD)
    assert delta.file_level_churn is False
    assert len(delta.symbols_added) == 1
    added = delta.symbols_added[0]
    assert added.symbol_id == f"{REPO}:{FILE}:gamma"
    assert added.kind == "function"
    assert delta.symbols_removed == ()
    assert delta.symbols_resignatured == ()
    assert delta.import_edges_added == ()
    assert delta.import_edges_removed == ()
    assert delta.churn_counts["added"] == 1


def test_signature_hash_stable_across_identical_source():
    s1 = extract_symbol_set(REPO, FILE, BEFORE_TWO)
    s2 = extract_symbol_set(REPO, FILE, BEFORE_TWO)
    assert s1.parse_ok and s2.parse_ok
    key = f"{REPO}:{FILE}:alpha"
    assert s1.symbols[key].signature_hash == s2.symbols[key].signature_hash
    assert _HEX16.match(s1.symbols[key].signature_hash)


def test_resignature_param_change():
    before = "def alpha(x):\n    return x\n"
    after = "def alpha(x, y):\n    return x\n"
    delta = compute_file_delta(REPO, FILE, before, after)
    assert delta.file_level_churn is False
    assert len(delta.symbols_resignatured) == 1
    sid, old_h, new_h = delta.symbols_resignatured[0]
    assert sid == f"{REPO}:{FILE}:alpha"
    assert old_h != new_h
    assert _HEX16.match(old_h) and _HEX16.match(new_h)
    # not double-counted as add/remove
    assert delta.symbols_added == ()
    assert delta.symbols_removed == ()


def test_remove_class_removes_its_methods():
    before = (
        "class Widget:\n"
        "    def render(self):\n"
        "        return 1\n"
        "    def hide(self):\n"
        "        return 2\n"
        "\n"
        "def standalone():\n"
        "    return 0\n"
    )
    after = "def standalone():\n    return 0\n"
    delta = compute_file_delta(REPO, FILE, before, after)
    assert delta.file_level_churn is False
    removed_ids = {r.symbol_id for r in delta.symbols_removed}
    assert removed_ids == {
        f"{REPO}:{FILE}:Widget",
        f"{REPO}:{FILE}:Widget.render",
        f"{REPO}:{FILE}:Widget.hide",
    }
    assert delta.symbols_added == ()


def test_import_from_added_and_removed():
    before = "import os\n\ndef f():\n    return 1\n"
    after = "import os\nfrom collections import OrderedDict\n\ndef f():\n    return 1\n"

    added_delta = compute_file_delta(REPO, FILE, before, after)
    assert added_delta.file_level_churn is False
    assert len(added_delta.import_edges_added) == 1
    edge = added_delta.import_edges_added[0]
    assert edge.edge_kind == "imports_from"
    assert edge.dst_name == "collections.OrderedDict"
    assert edge.src_id == f"{REPO}:{FILE}:mod"
    assert added_delta.import_edges_removed == ()

    removed_delta = compute_file_delta(REPO, FILE, after, before)
    assert removed_delta.file_level_churn is False
    assert len(removed_delta.import_edges_removed) == 1
    assert removed_delta.import_edges_removed[0].dst_name == "collections.OrderedDict"
    assert removed_delta.import_edges_added == ()


def test_overflow_collapses_to_file_level_churn():
    before = ""  # empty module: parses fine, zero symbols
    after = "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(70))
    delta = compute_file_delta(REPO, FILE, before, after)
    assert delta.file_level_churn is True
    # per-symbol tuples emptied (bound honored)
    assert delta.symbols_added == ()
    assert delta.symbols_removed == ()
    assert delta.symbols_resignatured == ()
    assert delta.import_edges_added == ()
    assert delta.import_edges_removed == ()
    # real counts survive
    assert delta.churn_counts["added"] == 70


def test_unparseable_after_never_raises():
    before = "def ok():\n    return 1\n"
    after = "def broken(:\n    pass\n"  # SyntaxError
    delta = compute_file_delta(REPO, FILE, before, after)  # must not raise
    assert delta.file_level_churn is True
    # extract of the broken revision reports parse_ok=False, empty
    bad = extract_symbol_set(REPO, FILE, after)
    assert bad.parse_ok is False
    assert bad.symbols == {}
    assert bad.import_edges == frozenset()


def test_no_content_invariant():
    magic = "MAGIC_UNIQUE_TOKEN_ZZZ_9182"
    before = ""
    after = (
        "def leaker():\n"
        f'    secret = "{magic}"\n'
        "    return secret\n"
    )
    delta = compute_file_delta(REPO, FILE, before, after)
    blob = json.dumps(delta.to_dict())
    # GREP-ENFORCED Mandate 1: no source body token may leak
    assert magic not in blob
    # every signature field is a 16-hex hash
    for rec in delta.symbols_added:
        assert _HEX16.match(rec.signature_hash)


def test_to_dict_from_dict_round_trip():
    before = (
        "import os\n"
        "\n"
        "def alpha(x):\n"
        "    return x\n"
    )
    after = (
        "import os\n"
        "from collections import OrderedDict\n"
        "\n"
        "def alpha(x, y):\n"
        "    return x\n"
        "\n"
        "def gamma():\n"
        "    return 0\n"
    )
    delta = compute_file_delta(REPO, FILE, before, after)
    # exercise all lanes
    assert delta.symbols_added
    assert delta.symbols_resignatured
    assert delta.import_edges_added

    d = delta.to_dict()
    round_tripped = StructuralDelta.from_dict(json.loads(json.dumps(d)))
    assert round_tripped == delta


def test_diff_symbol_sets_parse_failure_side_is_churn():
    good = extract_symbol_set(REPO, FILE, BEFORE_TWO)
    bad = SymbolSet(REPO, FILE, {}, frozenset(), parse_ok=False)
    delta = diff_symbol_sets(good, bad)
    assert delta.file_level_churn is True


def test_symbol_record_is_frozen_hashable():
    rec = SymbolRecord(symbol_id="a:b:c", kind="function", signature_hash="0" * 16)
    # frozen dataclasses are hashable -> usable in sets
    assert rec in {rec}
    edge = ImportEdge(src_id="a:b:c", dst_name="os", edge_kind="imports")
    assert edge in {edge}
