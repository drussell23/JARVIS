"""Slice 43 — Async batch timeout alignment.

FORCE_BATCH ops must NOT be severed by the 30s RT reflex cap
(_PRIMARY_MAX_TIMEOUT_S): the provider's internal poll_and_retrieve runs for
minutes (the registry waits up to _DW_MAX_WAIT_S=3600). _compute_primary_budget
gains a force_batch branch returning a batch-appropriate budget. RT path is
byte-identical. Phase 2 (terminal-state failover) is already implemented in
_adaptive_poll_batch — pinned here so it can't regress.
"""
from __future__ import annotations

import ast
import pathlib

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
)

_budget = CandidateGenerator._compute_primary_budget
_LIGHT = "Qwen/Qwen3.5-35B-A3B-FP8"


def test_force_batch_uses_remaining_under_cap(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    # remaining (250) < default batch cap (300) → use remaining.
    assert _budget(250.0, model_id=_LIGHT, force_batch=True) == 250.0


def test_force_batch_caps_at_batch_timeout(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    # remaining (500) > default batch cap (300) → cap at 300.
    assert _budget(500.0, model_id=_LIGHT, force_batch=True) == 300.0


def test_force_batch_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "120")
    assert _budget(500.0, force_batch=True) == 120.0


def test_force_batch_far_exceeds_30s_reflex_cap(monkeypatch):
    # The whole point: a 35B (non-heavy) batch op gets >> 30s now.
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    assert _budget(300.0, model_id=_LIGHT, force_batch=True) >= 200.0


def test_rt_path_unchanged_light_model(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    # force_batch=False → legacy 30s reflex cap (byte-identical pre-Slice-43).
    assert _budget(250.0, model_id=_LIGHT, force_batch=False) == 30.0


def test_rt_path_default_is_not_force_batch(monkeypatch):
    # Default (no force_batch kwarg) must remain the RT reflex path.
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    assert _budget(250.0, model_id=_LIGHT) == 30.0


def test_force_batch_zero_remaining():
    assert _budget(0.0, force_batch=True) == 0.0


def test_phase2_pin_adaptive_poll_terminal_returns_none():
    """Phase 2 already-satisfied pin: _adaptive_poll_batch breaks on
    failed/expired/cancelled and returns None (clean failover, no hang)."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_adaptive_poll_batch"
    )
    dump = ast.dump(fn)
    # The terminal-state comparison must be present...
    assert "failed" in dump and "expired" in dump and "cancelled" in dump, (
        "_adaptive_poll_batch must handle terminal batch states"
    )
    # ...and the terminal branch must contain a `return None` (failover).
    terminal_ifs = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.If) and "expired" in ast.dump(node.test)
    ]
    assert terminal_ifs, "terminal-state branch not found"
    assert any(
        isinstance(s, ast.Return)
        and (s.value is None or (isinstance(s.value, ast.Constant) and s.value.value is None))
        for blk in terminal_ifs for s in ast.walk(blk)
    ), "terminal-state branch must return None for clean failover"
