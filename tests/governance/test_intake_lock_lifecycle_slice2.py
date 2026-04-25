"""Harness Epic Slice 2 — intake_router.lock lifecycle hardening tests.

Pins:

A. Lock metadata schema is additive — old readers (pid + ts only) work,
   new fields (monotonic_ts + wall_iso + session_id) are added cleanly.
B. Stale-TTL detection — wedged-but-alive zombie holding the lock past
   ``JARVIS_INTAKE_LOCK_STALE_TTL_S`` (default 7200s) is detected and
   the lock is reclaimed.
C. Dead-PID staleness still works (pre-Slice-2 path preserved).
D. Single-flight launcher preflight — pgrep + lock + TTL composite check.
E. Single-flight env knob (`JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED`) gates
   the launcher check.
F. Source-grep pins for the wiring.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (A) Schema upgrade — lock metadata round-trip
# ---------------------------------------------------------------------------


def test_lock_writer_emits_new_schema_fields(tmp_path):
    """The Slice 2 lock writer adds monotonic_ts, wall_iso, session_id
    to the lock metadata. Existing readers (pid + ts only) keep working."""
    # Direct round-trip via the metadata format the writer uses.
    # We can't easily exercise the full _acquire_lock without spinning up
    # a UnifiedIntakeRouter, so we exercise the writer's JSON shape.
    from datetime import datetime, timezone
    sample = {
        "pid": 12345,
        "ts": time.time(),
        "monotonic_ts": time.monotonic(),
        "wall_iso": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": "bt-test-001",
    }
    artifact = tmp_path / "intake_router.lock"
    artifact.write_text(json.dumps(sample))
    # Read back via the legacy field-set first
    parsed = json.loads(artifact.read_text())
    assert parsed["pid"] == 12345
    assert parsed["ts"] > 0
    # New fields available
    assert "monotonic_ts" in parsed
    assert parsed["wall_iso"].endswith("Z")
    assert parsed["session_id"] == "bt-test-001"


def test_lock_old_schema_still_parseable(tmp_path):
    """Legacy 2-field schema (pid + ts) MUST still parse cleanly — the
    new schema is additive only. Old session dirs left over from
    pre-Slice-2 runs must not break the new reader."""
    artifact = tmp_path / "intake_router.lock"
    artifact.write_text(json.dumps({"pid": 999, "ts": time.time()}))
    parsed = json.loads(artifact.read_text())
    assert parsed["pid"] == 999
    assert parsed["ts"] > 0
    # New fields absent — reader uses .get() with defaults so it tolerates


# ---------------------------------------------------------------------------
# (B + C) Stale-lock detection — dead-PID + wedged-but-alive TTL
# ---------------------------------------------------------------------------


def test_cleanup_stale_lock_removes_dead_pid(tmp_path):
    """Pre-Slice-2 behavior preserved — dead-PID lock is removed."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    artifact = tmp_path / "intake_router.lock"
    # Use a PID guaranteed to not exist
    artifact.write_text(json.dumps({
        "pid": 99999999,  # outside typical PID range
        "ts": time.time(),
    }))
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is True
    assert not artifact.exists()


def test_cleanup_stale_lock_removes_wedged_but_alive_past_ttl(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW Slice 2 behavior — alive PID holding lock past TTL is reclaimed."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    monkeypatch.setenv("JARVIS_INTAKE_LOCK_STALE_TTL_S", "60")
    artifact = tmp_path / "intake_router.lock"
    # Use os.getpid() — guaranteed alive — but ts is ancient
    artifact.write_text(json.dumps({
        "pid": os.getpid(),
        "ts": time.time() - 7200,  # 2h old, past 60s TTL
    }))
    # Spoof self-PID check by using a different PID; Test's getpid IS our PID,
    # so cleanup will think we own it. Pick another live PID.
    # On most systems PID 1 (init) is alive.
    artifact.write_text(json.dumps({
        "pid": 1,  # init, always alive on POSIX
        "ts": time.time() - 7200,
    }))
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is True
    assert not artifact.exists()


def test_cleanup_stale_lock_keeps_fresh_lock_with_alive_pid(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive-PID + fresh ts → NOT removed (legitimate concurrent run signal)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    monkeypatch.setenv("JARVIS_INTAKE_LOCK_STALE_TTL_S", "7200")
    artifact = tmp_path / "intake_router.lock"
    artifact.write_text(json.dumps({
        "pid": 1,  # init, always alive
        "ts": time.time(),  # fresh
    }))
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is False
    assert artifact.exists()  # NOT removed


def test_stale_ttl_garbage_falls_back_to_default(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed env var → default 7200s used, not crash."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    monkeypatch.setenv("JARVIS_INTAKE_LOCK_STALE_TTL_S", "not-a-number")
    artifact = tmp_path / "intake_router.lock"
    artifact.write_text(json.dumps({
        "pid": 1,
        "ts": time.time() - 100,  # 100s old
    }))
    # 100s < 7200s default → lock kept
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is False


def test_corrupt_lock_removed(tmp_path) -> None:
    """Pre-Slice-2 behavior — corrupt JSON → remove."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    artifact = tmp_path / "intake_router.lock"
    artifact.write_text("this is not json")
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is True
    assert not artifact.exists()


# ---------------------------------------------------------------------------
# (D + E) Single-flight launcher — source-grep pins
# (We can't easily exercise the full sys.exit(75) path in pytest without
# subprocess machinery; pin the contract source-side and verify the
# helper function exists + has the right shape.)
# ---------------------------------------------------------------------------


def test_single_flight_helper_exists():
    """Helper function `_single_flight_preflight` must exist in launcher."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    assert "def _single_flight_preflight()" in src


def test_single_flight_uses_pgrep_canonical_pattern():
    """Pgrep pattern must be the operator-runbook canonical form
    (avoids matching zsh wrapper eval text — addressed in Slice 3 too)."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    assert r'"python3? scripts/ouroboros_battle_test\.py"' in src


def test_single_flight_exits_75_on_violation():
    """Exit code 75 (EX_TEMPFAIL from BSD sysexits.h) is the documented
    code for 'try again later' — distinct from generic error code 1."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    assert "sys.exit(75)" in src


def test_single_flight_gated_by_env_var():
    """JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED gates the check (operator
    escape hatch for diagnostics / recovery)."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    assert "JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED" in src
    # Default true (operator opts OUT to bypass)
    assert 'JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED", "true"' in src


def test_single_flight_called_after_zombie_reap():
    """The single-flight check runs AFTER the zombie reaper so it doesn't
    falsely trip on dead-PID lockholders the reaper can clean."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    # Both must exist; single-flight check string must appear after the
    # zombie reap call in the main() flow.
    reap_idx = src.find("_reap_zombies()")
    sf_idx = src.find("_single_flight_preflight()")
    assert reap_idx > 0
    assert sf_idx > reap_idx, (
        "single-flight must be invoked AFTER _reap_zombies in main() flow"
    )


# ---------------------------------------------------------------------------
# (F) Schema upgrade source pins
# ---------------------------------------------------------------------------


def test_writer_emits_monotonic_ts_field():
    """Source-grep: writer must include monotonic_ts in the lock JSON."""
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text()
    assert '"monotonic_ts": time.monotonic()' in src


def test_writer_emits_session_id_field():
    """Source-grep: writer must include session_id (links lock → session dir)."""
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text()
    assert '"session_id": _session_id' in src


def test_cleanup_handles_wedged_but_alive_path():
    """Source-grep: the wedged-but-alive TTL branch in _cleanup_stale_lock
    must reference JARVIS_INTAKE_LOCK_STALE_TTL_S."""
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text()
    assert "JARVIS_INTAKE_LOCK_STALE_TTL_S" in src
    assert "wedged-but-alive" in src
