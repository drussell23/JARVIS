"""``/provider`` REPL verb — the DoubleWord resilience dashboard.

Surfaces the Sentinel's SQLite telemetry (state, jitter, adaptive threshold,
ΔTTFT gradient, forecast) into the CLI. Read-only; auto-discovered via
``dispatch_provider_command``.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.ouroboros.governance.dw_outage_forecaster import (
    record_probe_ttft,
    record_outage_start,
    record_recovery,
)
from backend.core.ouroboros.governance.provider_jitter import record_jitter_event
from backend.core.ouroboros.governance.provider_repl import (
    _render_overview,
    dispatch_provider_command,
)
from backend.core.ouroboros.governance.provider_state import mark_degraded, mark_healthy


def _strip(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_matching_and_non_matching_lines() -> None:
    assert dispatch_provider_command("/status").matched is False
    assert dispatch_provider_command("").matched is False
    assert dispatch_provider_command("/provider").matched is True
    assert dispatch_provider_command("/provider help").matched is True
    assert "resilience dashboard" in _strip(dispatch_provider_command("/provider help").text)


def test_dispatch_never_raises_on_missing_db() -> None:
    # Even with no real DB, the dispatcher renders something and never throws.
    res = dispatch_provider_command("/provider")
    assert res.ok is True and res.matched is True
    assert isinstance(res.text, str) and res.text


def test_render_degraded_overview_surfaces_all_telemetry() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    NOW = 100_000.0
    mark_degraded(conn, "doubleword", reason="sentinel_watch_start", ts=NOW - 30)
    # 5 recent flaps → jitter 5 → adaptive threshold 5.
    for i in range(5):
        record_jitter_event(conn, "doubleword", "no_tokens", ts=NOW - 100 - i)
    # Falling TTFT → negative ΔTTFT (stabilizing).
    for i, t in enumerate([6.0, 5.0, 4.0]):
        record_probe_ttft(conn, "doubleword", t, ts=NOW - 200 + i * 30)

    out = _strip(_render_overview(conn, now=NOW))
    assert "DoubleWord — Resilience" in out
    assert "DEGRADED" in out
    assert "Jitter" in out and " 5 " in out
    assert "5 consecutive" in out          # adaptive threshold surfaced
    assert "ΔTTFT" in out and "stabilizing" in out
    assert "Forecast" in out
    assert "Sentinel holds until 5 consecutive" in out


def test_render_healthy_overview() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    NOW = 100_000.0
    # A completed outage so the forecaster has a datapoint.
    record_outage_start(conn, NOW - 700)
    record_recovery(conn, NOW - 700, NOW - 100)
    mark_healthy(conn, "doubleword", reason="staged_2pass_ok x2", ts=NOW - 10)

    out = _strip(_render_overview(conn, now=NOW))
    assert "HEALTHY" in out
    assert "Jitter" in out and " 0 " in out          # no recent flaps
    assert "Sentinel holds until" not in out          # healthy → no hold note
