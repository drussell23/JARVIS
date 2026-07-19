"""Cockpit console hygiene — the operator terminal is sacred.

Operator report 2026-07-18 (first clean `ov` run on the repaired
install): the awakening crest was stomped mid-draw by (a) a FULL aiohttp
ERROR traceback for a benign client-disconnect inside the Aegis child
(which inherits the cockpit tty) and (b) the TestWatcher's raw-stdout
READY marker. Three pins + two behavior tests kill the classes:

  1. Aegis forward handler catches the ConnectionResetError family and
     answers 499 quietly — a dropped local client is a detach, not an
     ERROR traceback on the operator's screen.
  2. The TestWatcher raw-stdout marker is soak-only; cockpit keeps the
     logger twin (absorbed by the ERROR-only console threshold).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


# ---------------------------------------------------------------------------
# (1) Aegis benign-disconnect class
# ---------------------------------------------------------------------------


def test_daemon_forward_catches_client_reset():
    src = _read("backend/core/ouroboros/aegis/daemon.py")
    body = src[src.index("async def _do_forward"):][:2500]
    assert "ConnectionResetError" in body
    assert "499" in body                          # client-closed, not a fault
    assert "logger.debug" in body                 # ONE quiet line, no traceback


def test_daemon_reset_catch_wraps_forward_request():
    src = _read("backend/core/ouroboros/aegis/daemon.py")
    body = src[src.index("async def _do_forward"):][:2500]
    # The await sits INSIDE the try — the class can't escape to aiohttp's
    # web_protocol ERROR logger.
    assert body.index("try:") < body.index("await forward_request(")
    assert body.index("await forward_request(") < body.index(
        "except (ConnectionResetError"
    )


def test_aiohttp_reset_is_connection_reset_subclass():
    """The catch relies on aiohttp's ClientConnectionResetError being a
    ConnectionResetError subclass — pin the assumption against the
    installed aiohttp."""
    aiohttp = pytest.importorskip("aiohttp")
    exc = getattr(
        aiohttp.client_exceptions, "ClientConnectionResetError", None,
    )
    if exc is None:
        pytest.skip("installed aiohttp predates ClientConnectionResetError")
    assert issubclass(exc, ConnectionResetError)


# ---------------------------------------------------------------------------
# (2) TestWatcher marker — soak-only on stdout
# ---------------------------------------------------------------------------


def test_testwatcher_marker_gated_off_cockpit():
    src = _read(
        "backend/core/ouroboros/governance/intake/sensors/"
        "test_failure_sensor.py"
    )
    idx = src.index("print(TESTWATCHER_READY_MARKER")
    region = src[max(0, idx - 800):idx]
    assert "is_cockpit" in region
    assert "if not _is_cockpit():" in region
    # The logger twin survives unconditionally (session logs + soak greps).
    after = src[idx:idx + 600]
    assert 'logger.info("%s", TESTWATCHER_READY_MARKER)' in after


def test_marker_constant_unchanged_for_soak_greps():
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (  # noqa: E501
        TESTWATCHER_READY_MARKER,
    )
    assert TESTWATCHER_READY_MARKER == "[TestWatcher] READY subscribed=fs.changed.*"
