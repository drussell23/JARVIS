"""P5 Arc C Slice 5a regression spine — shared process-tree probe.

Pins:
  * contract: probe returns a positive float OR None (never raises)
  * parity: harness BattleTestHarness._probe_process_tree_rss_mb
    DELEGATES to the shared fn (moved fn == old behavior — proven by
    monkeypatching the shared fn and observing the wrapper return it)
  * AST: harness wrapper has NO inlined psutil/resource probe (no
    second probe implementation — single source of truth)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import process_tree_probe
from backend.core.ouroboros.governance.process_tree_probe import (
    probe_process_tree_rss_mb,
)
from backend.core.ouroboros.battle_test.harness import BattleTestHarness

_HARNESS_SRC = (
    Path(__file__).resolve().parents[2]
    / "backend/core/ouroboros/battle_test/harness.py"
)


def test_probe_contract_positive_float_or_none():
    v = probe_process_tree_rss_mb()
    assert v is None or (isinstance(v, float) and v > 0.0)


def test_harness_wrapper_delegates_to_shared_probe(monkeypatch):
    # Parity: the harness staticmethod must return exactly what the
    # shared fn returns (delegation, not a divergent copy).
    sentinel = 4242.0
    monkeypatch.setattr(
        process_tree_probe, "probe_process_tree_rss_mb",
        lambda: sentinel,
    )
    assert BattleTestHarness._probe_process_tree_rss_mb() == sentinel


def test_harness_wrapper_propagates_none(monkeypatch):
    monkeypatch.setattr(
        process_tree_probe, "probe_process_tree_rss_mb", lambda: None,
    )
    assert BattleTestHarness._probe_process_tree_rss_mb() is None


def test_ast_pin_harness_has_no_second_probe_impl():
    """The harness wrapper must DELEGATE — no inlined psutil/resource
    probe (that would resurrect the duplication Slice 5a removed)."""
    tree = ast.parse(_HARNESS_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_probe_process_tree_rss_mb"
    )
    body = ast.unparse(fn)
    assert "probe_process_tree_rss_mb" in body, "must delegate"
    assert "memory_info" not in body, "no inlined psutil probe"
    assert "getrusage" not in body, "no inlined resource probe"
    assert "children(recursive" not in body
