"""Pytest assertions for ``merge``.

Behavior contract (inferred from input/output pairs)
----------------------------------------------------

* Flat keys: override wins on conflict; base-only keys preserved.
* Nested dicts: when BOTH base and override have a dict at the
  same key, MERGE recursively (do not replace wholesale).
* Lists: when both sides have a list at the same key, the
  override's list REPLACES the base's list (no element merging).
* Mixed types: if override has a non-dict at a key where base
  has a dict (or vice-versa), override's value REPLACES base's
  value at that key.
* Neither input is mutated.

These rules are not documented in the function docstring — they
must be inferred from the test examples below.
"""
from __future__ import annotations

from before import merge


# ---- Flat (passes naive shallow merge too) ----


def test_flat_disjoint_keys():
    assert merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_flat_override_replaces():
    assert merge({"a": 1, "b": 2}, {"a": 99}) == {"a": 99, "b": 2}


def test_empty_base():
    assert merge({}, {"a": 1}) == {"a": 1}


def test_empty_override():
    assert merge({"a": 1}, {}) == {"a": 1}


# ---- Depth 2 — the canonical shallow-merge canary ----


def test_two_level_preserves_base_keys():
    """Override at depth 2 must MERGE INTO base's nested dict, not
    REPLACE it.  Catches the most-naive shallow `dict.update`
    fix."""
    assert merge(
        {"db": {"host": "localhost", "port": 5432}},
        {"db": {"port": 6543}},
    ) == {"db": {"host": "localhost", "port": 6543}}


def test_two_level_adds_new_nested_key():
    assert merge(
        {"db": {"host": "localhost"}},
        {"db": {"port": 5432}},
    ) == {"db": {"host": "localhost", "port": 5432}}


def test_two_level_disjoint_sibling_keys():
    assert merge(
        {"app": {"version": "1.0"}, "db": {"host": "a"}},
        {"db": {"port": 1}},
    ) == {"app": {"version": "1.0"}, "db": {"host": "a", "port": 1}}


# ---- Depth 3 — the level-1-only naive fix canary ----


def test_three_level_preserves_base_keys():
    """A naive fix that handles nested dicts but doesn't RECURSE
    (level-1 deep, level-2 shallow) ceiling-fails here: at
    {app}{db}{port} → {app}{db}{port}, the level-1 fix sees that
    both sides have a dict at 'app' and creates a new merged dict;
    but inside, when it sees both sides have a dict at 'db', it
    just REPLACES (no recursion) — losing {app}{db}{host}."""
    assert merge(
        {"app": {"db": {"host": "a", "port": 1}, "cache": {"ttl": 60}}},
        {"app": {"db": {"port": 2}}},
    ) == {
        "app": {
            "db": {"host": "a", "port": 2},
            "cache": {"ttl": 60},
        },
    }


def test_four_level_with_partial_overlap():
    """Stress: 4-level nesting with override at the deepest level.
    Only a fully-recursive merge passes."""
    assert merge(
        {"a": {"b": {"c": {"d": 1, "e": 2}}}},
        {"a": {"b": {"c": {"d": 99}}}},
    ) == {"a": {"b": {"c": {"d": 99, "e": 2}}}}


# ---- Semantic edges ----


def test_list_value_replaces_not_merges():
    """Lists fully REPLACE (no element merging) when both sides
    have a list at the same key.  Catches over-aggressive deep
    merges that try to recurse into lists."""
    assert merge(
        {"servers": ["a", "b"]},
        {"servers": ["c"]},
    ) == {"servers": ["c"]}


def test_list_in_nested_dict_replaces():
    assert merge(
        {"app": {"servers": ["a", "b"], "name": "x"}},
        {"app": {"servers": ["c"]}},
    ) == {"app": {"servers": ["c"], "name": "x"}}


def test_scalar_override_replaces_dict():
    """Override has a scalar at a key where base has a dict —
    override wins, base's dict is discarded."""
    assert merge(
        {"x": {"nested": 1}},
        {"x": "scalar"},
    ) == {"x": "scalar"}


def test_dict_override_replaces_scalar():
    """Symmetric: override has a dict at a key where base has a
    scalar — override wins, base's scalar is discarded."""
    assert merge(
        {"x": "scalar"},
        {"x": {"nested": 1}},
    ) == {"x": {"nested": 1}}


# ---- Immutability ----


def test_does_not_mutate_base():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    merge(base, override)
    assert base == {"a": {"b": 1}}


def test_does_not_mutate_override():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    merge(base, override)
    assert override == {"a": {"c": 2}}
