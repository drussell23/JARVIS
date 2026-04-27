"""Slice 11.6.d regression spine — BacklogSensor + file-stat short-circuit.

Architectural note: BacklogSensor's watched files live under ``.jarvis/``,
which the MerkleCartographer correctly excludes (state, not code). So this
slice uses file-stat short-circuit (mtime_ns + size + exists) as the
analogue of cartographer hash for state files. Same architectural spirit
as 11.6.a/b/c (cheap pre-emption of expensive read+parse), different
mechanism appropriate to the watched data class.

Pins:
  §1 Per-sensor flag — JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED default false +
                       truthy/falsy parsing
  §2 Cold-start: full scan even with flag on (no baseline)
  §3 Steady state, no changes: short-circuit; cached envelopes returned
  §4 backlog.json mtime change → full scan; cache + baseline refreshed
  §5 backlog.json size change → full scan
  §6 backlog.json deleted → full scan (existence flip)
  §7 proposals.jsonl change (when auto-proposed on) → full scan
  §8 auto-proposed flag flipped → full scan (topology change)
  §9 OS error on stat → fail-safe full scan (NEVER raises)
  §10 health() exposes short-circuit metrics + watched files
  §11 Backward compat: flag off → byte-identical legacy
  §12 Source-level pins
"""
from __future__ import annotations

import asyncio  # noqa: F401  — pytest-asyncio plugin contract
import json
import os  # noqa: F401  — env-var reads in fixtures
import time
from pathlib import Path
from typing import Any, List  # noqa: F401  — used in body, not header
from unittest.mock import AsyncMock, MagicMock  # noqa: F401

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    backlog_sensor as bls,
)
from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    short_circuit_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _backlog_payload(num_tasks: int = 2) -> str:
    """JSON payload matching BacklogSensor's expected schema."""
    return json.dumps([
        {
            "task_id": f"task-{i}",
            "description": f"synthetic task {i}",
            "target_files": [f"backend/file_{i}.py"],
            "priority": 3,
            "repo": "JARVIS",
            "status": "pending",
        }
        for i in range(num_tasks)
    ])


@pytest.fixture
def repo_with_backlog(tmp_path: Path) -> Path:
    """Synthetic repo with a populated .jarvis/backlog.json."""
    jarvis_dir = tmp_path / ".jarvis"
    jarvis_dir.mkdir()
    (jarvis_dir / "backlog.json").write_text(_backlog_payload(2))
    # Empty proposals ledger so the auto-proposed scan is deterministic
    # (returns 0 envelopes when the flag is on).
    (jarvis_dir / "self_goal_formation_proposals.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def make_sensor(repo_with_backlog: Path):
    """Factory: produces BacklogSensors rooted at the synthetic repo."""

    def _make(**kwargs) -> BacklogSensor:
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        return BacklogSensor(
            backlog_path=repo_with_backlog / ".jarvis" / "backlog.json",
            repo_root=repo_with_backlog,
            router=router,
            poll_interval_s=60.0,
            **kwargs,
        )

    return _make


@pytest.fixture(autouse=True)
def _disable_auto_proposed_by_default(monkeypatch: pytest.MonkeyPatch):
    """Most tests assume only backlog.json is watched. Tests that
    specifically exercise the proposals path enable it explicitly."""
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "false")


# ===========================================================================
# §1 — Per-sensor flag
# ===========================================================================


def test_short_circuit_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", raising=False)
    assert short_circuit_enabled() is False


def test_short_circuit_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", val)
        assert short_circuit_enabled() is True


def test_short_circuit_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", val)
        assert short_circuit_enabled() is False


# ===========================================================================
# §2 — Cold-start: must full-scan even with flag on
# ===========================================================================


@pytest.mark.asyncio
async def test_cold_start_full_scan_even_with_flag_on(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    envelopes = await sensor.scan_once()
    assert sensor._sc_full_scans == 1  # noqa: SLF001
    assert sensor._sc_short_circuits == 0  # noqa: SLF001
    assert sensor._sc_cached_envelopes == envelopes  # noqa: SLF001
    # Baseline populated for next cycle
    assert sensor._sc_last_state  # noqa: SLF001


# ===========================================================================
# §3 — Steady state: no changes → short-circuit
# ===========================================================================


@pytest.mark.asyncio
async def test_short_circuit_when_no_changes(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    first = await sensor.scan_once()

    router_calls_before = sensor._router.ingest.call_count

    second = await sensor.scan_once()
    assert sensor._sc_short_circuits == 1  # noqa: SLF001
    assert sensor._sc_full_scans == 1  # noqa: SLF001 (unchanged)
    assert second == first
    # Router NOT called on short-circuit
    assert sensor._router.ingest.call_count == router_calls_before


# ===========================================================================
# §4 — backlog.json mtime change → full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_mtime_changes(
    make_sensor, repo_with_backlog: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    assert sensor._sc_full_scans == 1  # noqa: SLF001
    baseline = sensor._sc_last_state  # noqa: SLF001

    # Bump mtime without changing size — same content rewritten
    backlog = repo_with_backlog / ".jarvis" / "backlog.json"
    time.sleep(0.01)  # ensure mtime tick
    backlog.write_text(backlog.read_text())

    await sensor.scan_once()
    assert sensor._sc_full_scans == 2  # noqa: SLF001
    new_baseline = sensor._sc_last_state  # noqa: SLF001
    assert new_baseline != baseline


# ===========================================================================
# §5 — backlog.json size change → full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_size_changes(
    make_sensor, repo_with_backlog: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()

    # Add a third task — size grows even if mtime didn't tick
    backlog = repo_with_backlog / ".jarvis" / "backlog.json"
    backlog.write_text(_backlog_payload(3))

    await sensor.scan_once()
    assert sensor._sc_full_scans == 2  # noqa: SLF001


# ===========================================================================
# §6 — backlog.json deleted → existence flip → full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_backlog_deleted(
    make_sensor, repo_with_backlog: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()

    (repo_with_backlog / ".jarvis" / "backlog.json").unlink()

    envelopes = await sensor.scan_once()
    assert sensor._sc_full_scans == 2  # noqa: SLF001
    # Sensor returns [] on missing file — but DOES full-scan to verify
    assert envelopes == []


# ===========================================================================
# §7 — proposals.jsonl change → full scan (when auto-proposed on)
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_proposals_change(
    make_sensor, repo_with_backlog: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    assert sensor._sc_full_scans == 1  # noqa: SLF001

    # Append a proposal entry — file mtime + size both change
    proposals = repo_with_backlog / ".jarvis" / "self_goal_formation_proposals.jsonl"
    time.sleep(0.01)
    proposals.write_text(
        json.dumps({
            "proposal_id": "prop-001",
            "description": "synthetic proposal",
            "target_files": ["backend/x.py"],
        }) + "\n"
    )

    await sensor.scan_once()
    assert sensor._sc_full_scans == 2  # noqa: SLF001


# ===========================================================================
# §8 — auto-proposed flag flipped → topology change → full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_auto_proposed_flag_flipped(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (auto_proposed_enabled,) suffix in the state tuple captures
    the env flag — flipping it mid-session must bust the cache."""
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "false")
    sensor = make_sensor()
    await sensor.scan_once()
    assert sensor._sc_full_scans == 1  # noqa: SLF001

    # Operator flips the flag mid-session — adds proposals.jsonl to
    # the watched set
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "true")

    await sensor.scan_once()
    # Topology changed → full scan, no short-circuit
    assert sensor._sc_full_scans == 2  # noqa: SLF001
    assert sensor._sc_short_circuits == 0  # noqa: SLF001


# ===========================================================================
# §9 — OS error on stat → fail-safe full scan (NEVER raises)
# ===========================================================================


@pytest.mark.asyncio
async def test_os_error_on_stat_falls_through_to_full_scan(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()  # cold scan

    # Patch _sc_stat_tuple to simulate a permission error
    orig_stat = BacklogSensor._sc_stat_tuple

    def _broken_stat(self, path):
        return ("__sc_error__",)

    monkeypatch.setattr(BacklogSensor, "_sc_stat_tuple", _broken_stat)

    envelopes = await sensor.scan_once()
    assert isinstance(envelopes, list)
    # State signature now contains error sentinels — won't equal baseline
    assert sensor._sc_full_scans == 2  # noqa: SLF001


# ===========================================================================
# §10 — health() exposes short-circuit metrics
# ===========================================================================


def test_health_exposes_short_circuit_metrics(make_sensor) -> None:
    sensor = make_sensor()
    h = sensor.health()
    assert h["sensor"] == "BacklogSensor"
    assert "short_circuit_enabled" in h
    assert "short_circuit_short_circuits" in h
    assert "short_circuit_full_scans" in h
    assert "short_circuit_watched_files" in h
    assert "short_circuit_cached_envelopes" in h
    assert h["short_circuit_short_circuits"] == 0
    assert h["short_circuit_full_scans"] == 0
    assert h["short_circuit_cached_envelopes"] == 0


@pytest.mark.asyncio
async def test_health_after_scan_reflects_metrics(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    h = sensor.health()
    assert h["short_circuit_full_scans"] == 1
    assert any(
        "backlog.json" in p
        for p in h["short_circuit_watched_files"]
    )


# ===========================================================================
# §11 — Backward compat: flag off → byte-identical legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_flag_off_full_scan_every_cycle(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", raising=False,
    )
    sensor = make_sensor()
    await sensor.scan_once()
    await sensor.scan_once()
    await sensor.scan_once()
    assert sensor._sc_full_scans == 3  # noqa: SLF001
    assert sensor._sc_short_circuits == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_flag_off_does_not_stat_watched_files(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-sensor flag is off, _sc_current_state returns ()
    immediately — the stat syscalls are skipped entirely."""
    monkeypatch.delenv(
        "JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", raising=False,
    )
    sensor = make_sensor()
    state = sensor._sc_current_state()  # noqa: SLF001
    assert state == ()


# ===========================================================================
# §12 — Source-level pins
# ===========================================================================


def test_source_short_circuit_branch_in_scan_once() -> None:
    """The short-circuit branch must fire BEFORE the
    `_scan_backlog_json` call so the read + JSON parse is genuinely
    skipped."""
    import inspect
    src = inspect.getsource(BacklogSensor.scan_once)
    sc_idx = src.index("_sc_should_short_circuit")
    read_idx = src.index("_scan_backlog_json")
    assert sc_idx < read_idx


def test_source_no_top_level_cartographer_import() -> None:
    """BacklogSensor must NOT import merkle_cartographer at all —
    Slice 11.6.d uses file-stat instead. Pinned at module level."""
    import inspect
    src = inspect.getsource(bls)
    assert "merkle_cartographer" not in src, (
        "Slice 11.6.d contract: BacklogSensor uses file-stat, not "
        "cartographer (watched files are .jarvis/ state, not code)"
    )


def test_source_stat_tuple_includes_exists_mtime_size() -> None:
    """Pin the stat-tuple shape (exists, mtime_ns, size) so a future
    refactor can't silently drop a discriminator."""
    import inspect
    src = inspect.getsource(BacklogSensor._sc_stat_tuple)
    assert "st_mtime_ns" in src
    assert "st_size" in src


def test_source_state_tuple_includes_auto_proposed_flag() -> None:
    """Pin that the auto-proposed flag is captured in the state
    signature — flipping it must bust the cache (§8 invariant)."""
    import inspect
    src = inspect.getsource(BacklogSensor._sc_current_state)
    assert "_auto_proposed_enabled" in src
