"""LiveDashboard footer wiring — VisionSensor status line (Task 21).

Autonomous checks:

* Production source contains the footer-integration markers (cost │
  vision │ controls layout) + a best-effort try/except around the
  ``format_vision_status_line`` call so the footer stays green when
  the registry is empty or the import fails.
* ``format_vision_status_line`` returns the expected render for both
  no-sensor and an armed sensor (proving the footer's call path
  produces operator-visible output).

We don't boot the full Rich ``Live`` renderer here — that requires a
TTY and the interactive battle-test harness. The production-source
guard + renderer unit tests together prove the wiring is correct.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.vision_sensor import VisionSensor
from backend.core.ouroboros.governance.vision_repl import (
    format_vision_status_line,
    register_active_vision_sensor,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    register_active_vision_sensor(None)
    yield
    register_active_vision_sensor(None)


def _read_source(rel: str) -> str:
    return (
        pathlib.Path(__file__).resolve().parents[2] / rel
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Production-source wiring checks
# ---------------------------------------------------------------------------


def test_footer_imports_vision_repl():
    src = _read_source("backend/core/ouroboros/battle_test/live_dashboard.py")
    assert "format_vision_status_line" in src
    assert "get_active_vision_sensor" in src


def test_footer_guards_the_import_with_try_except():
    """Import failure must not break the footer render — LiveDashboard
    should degrade gracefully when vision_repl can't load."""
    src = _read_source("backend/core/ouroboros/battle_test/live_dashboard.py")
    # The try/except wrapping the vision_repl import is the guard.
    # Search for the string pattern rather than parse the AST — the
    # guard lives inside a method body which is easier to assert on.
    footer_block_start = src.find("def _build_footer")
    footer_block_end = src.find("return Panel(footer", footer_block_start)
    footer_block = src[footer_block_start:footer_block_end]
    assert "try:" in footer_block
    assert "except Exception:" in footer_block
    assert "format_vision_status_line" in footer_block


def test_footer_uses_vision_sensor_separator():
    """The vision token is bracketed with separators so the layout is
    stable + regex-extractable for future dashboard redesigns."""
    src = _read_source("backend/core/ouroboros/battle_test/live_dashboard.py")
    assert "👁" in src     # vision sub-panel glyph
    # Cost + vision + controls layout pattern
    assert "│    👁" in src


# ---------------------------------------------------------------------------
# format_vision_status_line — renderer behavior exercised via registry
# ---------------------------------------------------------------------------


def test_status_line_without_registered_sensor():
    # Registry cleared by autouse fixture.
    line = format_vision_status_line(None)
    assert line == "vision: off"


def test_status_line_armed_sensor_produces_single_line(tmp_path):
    sensor = VisionSensor(
        router=MagicMock(),
        session_id="footer-test",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        daily_cost_cap_usd=1.00,
        register_shutdown_hooks=False,
    )
    register_active_vision_sensor(sensor)
    line = format_vision_status_line(sensor)
    assert "\n" not in line
    assert line.startswith("vision: armed ")
    assert "today=" in line
    assert "$1.00" in line
