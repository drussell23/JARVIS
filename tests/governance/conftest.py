"""Shared pytest fixtures for governance tests."""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _neutralize_dw_cold_start(monkeypatch):
    """Slice 184 — the cold-start seal forces DW-batch for the first ~90s of a FRESH process.
    Every test process is freshly booted, so without this the seal would force batch in every
    steady-state DW-routing test (and flip "healthy stream → RT" assertions). Neutralize by
    default — push the process-start into the distant past so cold-start reads expired. Tests
    that exercise the cold-start explicitly re-set `_PROCESS_START` to `time.monotonic()`."""
    try:
        from backend.core.ouroboros.governance import doubleword_provider as _dw
        monkeypatch.setattr(_dw, "_PROCESS_START", time.monotonic() - 1_000_000.0, raising=False)
    except Exception:  # noqa: BLE001 — never let the fixture break collection
        pass


@pytest.fixture(autouse=True)
def _isolate_goal_reconciliation_ledger(monkeypatch, tmp_path):
    """The goal-reconciliation ledger is durable production state that the
    roadmap reader writes to on every emission (dispatch rows). A test that
    drives emit_roadmap_envelopes with a router — several do — must never
    land rows in the operator's ledger (observed 2026-09-07: 21 fixture
    goals in .jarvis/goal_reconciliation_ledger.jsonl, chain broken for
    every real row after). Tests that set the env themselves override this.
    """
    import os
    if "JARVIS_GOAL_RECONCILIATION_LEDGER_PATH" not in os.environ:
        monkeypatch.setenv(
            "JARVIS_GOAL_RECONCILIATION_LEDGER_PATH",
            str(tmp_path / "goal_reconciliation_ledger.jsonl"),
        )
    yield
