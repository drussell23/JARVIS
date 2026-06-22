"""Tests for Task B4: semantic DAG de-dup — bounded attempt ledger + active-plan cross-check."""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance import recursion_dedup as d


# ---------------------------------------------------------------------------
# subgoal_hash: stable + scope-sensitive
# ---------------------------------------------------------------------------

def test_hash_stable_and_scope_sensitive():
    h1 = d.subgoal_hash(("a.py::F",), "do x")
    assert h1 == d.subgoal_hash(("a.py::F",), "do x"), "same inputs must produce same hash"
    assert h1 != d.subgoal_hash(("a.py::G",), "do x"), "different scope must produce different hash"


def test_hash_stable_with_different_target_order():
    """Sorting targets makes hash order-independent."""
    h1 = d.subgoal_hash(("b.py", "a.py"), "refactor")
    h2 = d.subgoal_hash(("a.py", "b.py"), "refactor")
    assert h1 == h2, "target order must not affect hash"


def test_hash_description_case_insensitive():
    """Description is lowercased+stripped before hashing."""
    h1 = d.subgoal_hash(("a.py",), "  Do X  ")
    h2 = d.subgoal_hash(("a.py",), "do x")
    assert h1 == h2, "description whitespace/case must be normalized"


def test_hash_different_descriptions_differ():
    h1 = d.subgoal_hash(("a.py",), "do x")
    h2 = d.subgoal_hash(("a.py",), "do y")
    assert h1 != h2


def test_hash_empty_targets_does_not_raise():
    """fail-soft: bad input must not raise."""
    h = d.subgoal_hash((), "")
    assert isinstance(h, str) and len(h) == 64  # sha256 hex digest


def test_hash_returns_hex_string():
    h = d.subgoal_hash(("x.py",), "task")
    assert isinstance(h, str)
    assert len(h) == 64
    int(h, 16)  # must be valid hex


def test_hash_fail_soft_on_bad_input():
    """If something weird is passed, hash of repr rather than raising."""
    # Pass non-tuple — mypy would catch this at static analysis time,
    # but runtime must not raise.
    h = d.subgoal_hash(None, None)  # type: ignore[arg-type]
    assert isinstance(h, str) and len(h) == 64


# ---------------------------------------------------------------------------
# AttemptLedger: bounded FIFO dedup
# ---------------------------------------------------------------------------

def test_ledger_dedup():
    led = d.AttemptLedger()
    h = d.subgoal_hash(("a.py::F",), "x")
    assert not d.is_duplicate(h, led, frozenset())
    led.mark(h)
    assert d.is_duplicate(h, led, frozenset())


def test_ledger_seen_false_before_mark():
    led = d.AttemptLedger()
    assert not led.seen("abc123")


def test_ledger_seen_true_after_mark():
    led = d.AttemptLedger()
    led.mark("abc123")
    assert led.seen("abc123")


def test_ledger_bounded_evicts_oldest(monkeypatch):
    """When ledger is full, oldest entry is evicted (FIFO)."""
    monkeypatch.setenv("JARVIS_RECURSION_LEDGER_SIZE", "3")
    led = d.AttemptLedger()
    led.mark("h1")
    led.mark("h2")
    led.mark("h3")
    # Adding h4 evicts h1
    led.mark("h4")
    assert not led.seen("h1"), "oldest entry must be evicted when bound exceeded"
    assert led.seen("h2")
    assert led.seen("h3")
    assert led.seen("h4")


def test_ledger_default_size_is_512():
    """Default bound is 512 (env not set)."""
    led = d.AttemptLedger()
    assert led.maxlen == 512


def test_ledger_env_size_respected(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSION_LEDGER_SIZE", "10")
    led = d.AttemptLedger()
    assert led.maxlen == 10


def test_ledger_idempotent_mark():
    """Marking the same hash twice must not grow internal state unboundedly."""
    led = d.AttemptLedger()
    led.mark("h1")
    led.mark("h1")
    assert led.seen("h1")
    # Internal deque should have two entries (deque is NOT a set) but the
    # seen() check still works via the shadow set.
    assert len(led._seen_set) == 1  # set should still be 1 unique hash


# ---------------------------------------------------------------------------
# is_duplicate: ledger OR active_plan_hashes
# ---------------------------------------------------------------------------

def test_active_plan_dup():
    led = d.AttemptLedger()
    h = d.subgoal_hash(("a.py::F",), "x")
    assert d.is_duplicate(h, led, frozenset({h}))


def test_not_duplicate_when_absent_from_both():
    led = d.AttemptLedger()
    h = d.subgoal_hash(("a.py::F",), "novel task")
    assert not d.is_duplicate(h, led, frozenset())


def test_duplicate_from_ledger_not_active_plan():
    led = d.AttemptLedger()
    h = "deadbeef" * 8  # 64-char fake hash
    led.mark(h)
    assert d.is_duplicate(h, led, frozenset())


def test_not_duplicate_similar_but_different_scope():
    led = d.AttemptLedger()
    h1 = d.subgoal_hash(("a.py::F",), "do x")
    h2 = d.subgoal_hash(("a.py::G",), "do x")
    led.mark(h1)
    assert not d.is_duplicate(h2, led, frozenset()), "different scope must not be flagged as dup"
