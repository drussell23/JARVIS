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
