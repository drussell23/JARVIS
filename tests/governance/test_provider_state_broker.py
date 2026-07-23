"""Provider-state → broker bridge: SQLite transitions onto the event bus + TUI.

The watcher polls the Sentinel's provider_state SQLite row and emits a snapshot
on each DEGRADED↔HEALTHY transition; the breadcrumb formats the calm one-liner;
the publish helper routes it onto the StreamEventBroker (→ TUI listener + SSE/HUD).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from backend.core.ouroboros.governance.provider_jitter import record_jitter_event
from backend.core.ouroboros.governance.provider_state import mark_degraded, mark_healthy
from backend.core.ouroboros.governance.provider_state_broker import (
    ProviderStateWatcher,
    build_provider_snapshot,
    format_provider_breadcrumb,
)


def _db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


async def test_snapshot_carries_full_resilience_state() -> None:
    conn = _db()
    NOW = 1000.0
    mark_degraded(conn, "doubleword", reason="sentinel_watch_start", ts=NOW - 10)
    for i in range(3):
        record_jitter_event(conn, "doubleword", "no_tokens", ts=NOW - 5 - i)
    snap = build_provider_snapshot(conn, now=NOW)
    assert snap["state"] == "DEGRADED"
    assert snap["jitter"] == 3
    assert snap["threshold"] == 5           # 2 + 3 jitter
    assert "forecast_ttr" in snap and "ttft_slope" in snap


async def test_watcher_emits_only_on_transition() -> None:
    conn = _db()
    mark_degraded(conn, "doubleword", reason="down")
    emitted = []
    w = ProviderStateWatcher(conn, lambda s: emitted.append(s), emit_on_first=True)

    await w.poll_once()                      # first observation → DEGRADED (emit_on_first)
    assert len(emitted) == 1 and emitted[-1]["state"] == "DEGRADED"

    await w.poll_once()                      # unchanged → no emit
    assert len(emitted) == 1

    mark_healthy(conn, "doubleword", reason="staged_2pass_ok x5 (jitter-adaptive+pulse)")
    snap = await w.poll_once()               # DEGRADED → HEALTHY → emit
    assert len(emitted) == 2
    assert snap["state"] == "HEALTHY" and snap["previous_state"] == "DEGRADED"


async def test_watcher_first_observation_silent_by_default() -> None:
    conn = _db()
    mark_degraded(conn, "doubleword", reason="down")
    emitted = []
    w = ProviderStateWatcher(conn, lambda s: emitted.append(s))  # emit_on_first=False
    await w.poll_once()                      # baseline only, no emit
    assert emitted == []
    mark_healthy(conn, "doubleword", reason="recovered")
    await w.poll_once()                      # the transition emits
    assert len(emitted) == 1 and emitted[-1]["state"] == "HEALTHY"


def test_breadcrumb_formatting() -> None:
    deg = format_provider_breadcrumb(
        {"provider": "doubleword", "state": "DEGRADED", "jitter": 14,
         "ttft_slope": 0.001, "threshold": 5}
    )
    assert "doubleword ● DEGRADED" in deg
    assert "jitter 14" in deg and "worsening" in deg and "need 5 stable" in deg

    heal = format_provider_breadcrumb(
        {"provider": "doubleword", "state": "HEALTHY", "reason": "staged_2pass_ok x5"}
    )
    assert "HEALTHY ✓" in heal and "staged_2pass_ok x5" in heal

    stab = format_provider_breadcrumb(
        {"state": "DEGRADED", "jitter": 2, "ttft_slope": -0.02}
    )
    assert "stabilizing" in stab


async def test_publish_helper_accepted_by_broker_allowlist(monkeypatch) -> None:
    # The new event type must be in _VALID_EVENT_TYPES or publish drops it.
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    from backend.core.ouroboros.governance import ide_observability_stream as S
    eid = S.publish_provider_state_changed(
        {"provider": "doubleword", "state": "HEALTHY", "jitter": 0}
    )
    # eid is a non-None event id when accepted (broker enabled); None only if the
    # stream is disabled — never a rejection for an unknown type.
    assert "provider_state_changed" in S._VALID_EVENT_TYPES
    # A genuinely unknown type IS rejected (proves the allowlist is the gate).
    assert S.get_default_broker().publish("totally_unknown_type", "k", {}) is None
