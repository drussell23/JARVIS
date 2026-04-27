"""Slice 11.6.a regression spine — TodoScannerSensor + Merkle Cartographer.

Pins:
  §1 Per-sensor flag — JARVIS_TODO_USE_MERKLE default false +
                       truthy/falsy parsing
  §2 Cold-start: full scan even with merkle on (cache empty)
  §3 Steady state with no changes: short-circuit; cached items returned
  §4 Steady state with changes: full scan; cache refreshed
  §5 Master flag off (cartographer): fail-safe to full scan
  §6 Cartographer error: fail-safe to full scan (NEVER raises)
  §7 health() exposes merkle_short_circuits + merkle_full_scans
  §8 Backward compat: merkle flag off → byte-identical legacy
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    todo_scanner_sensor as tss,
)
from backend.core.ouroboros.governance.intake.sensors.todo_scanner_sensor import (
    TodoScannerSensor,
    TodoItem,
    merkle_consult_enabled,
)
from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_todos(tmp_path: Path) -> Path:
    """Synthetic repo with a few TODO/FIXME markers."""
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "foo.py").write_text(
        "def foo():\n    # TODO: refactor this\n    return 1\n"
    )
    (backend / "bar.py").write_text(
        "def bar():\n    # FIXME: handle edge case\n    return 2\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_foo.py").write_text(
        "def test_foo():\n    pass  # HACK: mock this\n"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "tool.py").write_text("# clean script\nx = 1\n")
    return tmp_path


@pytest.fixture
def make_sensor(repo_with_todos: Path):
    """Factory: produces TodoScannerSensors rooted at the synthetic repo."""

    def _make(**kwargs) -> TodoScannerSensor:
        router = AsyncMock()
        return TodoScannerSensor(
            repo="JARVIS",
            router=router,
            project_root=repo_with_todos,
            **kwargs,
        )

    return _make


@pytest.fixture
def isolated_cartographer(
    repo_with_todos: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Fresh cartographer rooted at the synthetic repo + isolated state dir."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo_with_todos))
    mc.reset_default_cartographer_for_tests()
    yield
    mc.reset_default_cartographer_for_tests()


# ===========================================================================
# §1 — Per-sensor flag
# ===========================================================================


def test_merkle_consult_default_on_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 11.7 graduation flip: unset/empty env returns True."""
    monkeypatch.delenv("JARVIS_TODO_USE_MERKLE", raising=False)
    assert merkle_consult_enabled() is True


def test_merkle_consult_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", val)
        assert merkle_consult_enabled() is True


def test_merkle_consult_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-graduation: empty string is the unset-marker for default
    True, so it's NOT in the falsy list. Hot-revert requires an
    explicit ``false``-class string."""
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", val)
        assert merkle_consult_enabled() is False


# ===========================================================================
# §2 — Cold-start: must full-scan even with merkle on
# ===========================================================================


@pytest.mark.asyncio
async def test_cold_start_full_scan_even_with_merkle_on(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
    isolated_cartographer,
) -> None:
    """First scan must walk disk to populate the cache. Subsequent
    cycles can short-circuit."""
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    items = await sensor.scan_once()
    assert len(items) >= 3  # TODO + FIXME + HACK from fixture
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001
    assert len(sensor._merkle_cached_items) == len(items)  # noqa: SLF001


# ===========================================================================
# §3 — Steady state: cartographer says no change → short-circuit
# ===========================================================================


@pytest.mark.asyncio
async def test_short_circuit_when_no_changes(
    make_sensor, repo_with_todos: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    # Build the cartographer's tree FIRST so the sensor's first scan
    # captures a stable baseline.
    cart = mc.get_default_cartographer(repo_root=repo_with_todos)
    await cart.update_full()

    sensor = make_sensor()

    # First scan — cold start: walks disk + records baseline
    first_items = await sensor.scan_once()
    initial_count = len(first_items)
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    assert sensor._merkle_last_seen_root_hash != ""  # noqa: SLF001

    # Second scan — no changes happened, cartographer's root hash
    # unchanged, sensor short-circuits using the stored baseline.
    second_items = await sensor.scan_once()
    assert sensor._merkle_short_circuits == 1  # noqa: SLF001
    assert sensor._merkle_full_scans == 1  # noqa: SLF001 (unchanged)
    assert len(second_items) == initial_count
    # Cached items returned (a NEW list — but same content)
    assert second_items == first_items


# ===========================================================================
# §4 — has_changed=True → full scan, cache refreshed
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_cartographer_reports_change(
    make_sensor, repo_with_todos: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    # Build cartographer's tree first
    cart = mc.get_default_cartographer(repo_root=repo_with_todos)
    await cart.update_full()

    sensor = make_sensor()

    # Cold-start scan — captures baseline = cart.current_root_hash()
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    baseline_hash = sensor._merkle_last_seen_root_hash  # noqa: SLF001
    assert baseline_hash != ""

    # Add a NEW TODO somewhere — file system genuinely changes
    (repo_with_todos / "backend" / "new.py").write_text(
        "# TODO: extra item\nx = 2\n"
    )
    # Refresh cartographer to pick up the new file → root hash changes
    await cart.update_full()
    new_hash = cart.current_root_hash()
    assert new_hash != baseline_hash, (
        "cartographer root hash should change when a new file is added"
    )

    # Next scan: cartographer's hash differs from sensor's baseline
    # → no short-circuit, full scan + baseline refreshed.
    second_items = await sensor.scan_once()
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001
    file_paths = {item.file_path for item in second_items}
    assert any("new.py" in p for p in file_paths)
    # Baseline updated to new hash
    assert sensor._merkle_last_seen_root_hash == new_hash  # noqa: SLF001


# ===========================================================================
# §5 — Master flag off → fail-safe full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_cartographer_master_flag_off_falls_through_to_full_scan(
    make_sensor, repo_with_todos: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    """Per-sensor flag on, but cartographer master flag OFF →
    current_root_hash returns "" → sensor treats as 'always changed'
    → full scan every cycle (legacy behavior preserved)."""
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.delenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", raising=False)
    sensor = make_sensor()

    # Multiple scans: each one full-scans because cartographer disabled
    await sensor.scan_once()
    await sensor.scan_once()
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 3  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001


# ===========================================================================
# §6 — Cartographer error → fail-safe full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_cartographer_error_falls_through_to_full_scan(
    make_sensor, repo_with_todos: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cartographer module fails to import / current_root_hash
    raises, sensor falls through to legacy full scan. NEVER blocks."""
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    # Cold scan first to populate cache
    await sensor.scan_once()

    # Monkey-patch current_root_hash to raise
    def _broken_current(self, *args, **kwargs):
        raise RuntimeError("simulated cartographer crash")

    monkeypatch.setattr(
        mc.MerkleCartographer, "current_root_hash", _broken_current,
    )

    # Next scan must NOT raise; must fall through to full scan
    items = await sensor.scan_once()
    assert isinstance(items, list)
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001


# ===========================================================================
# §7 — health() exposes merkle metrics
# ===========================================================================


def test_health_exposes_merkle_metrics(make_sensor) -> None:
    sensor = make_sensor()
    h = sensor.health()
    assert "merkle_consult_enabled" in h
    assert "merkle_short_circuits" in h
    assert "merkle_full_scans" in h
    assert "merkle_cached_items" in h
    # Cold sensor — zero metrics
    assert h["merkle_short_circuits"] == 0
    assert h["merkle_full_scans"] == 0
    assert h["merkle_cached_items"] == 0


@pytest.mark.asyncio
async def test_health_after_scan_reflects_metrics(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
    isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    h = sensor.health()
    assert h["merkle_full_scans"] == 1
    assert h["merkle_cached_items"] >= 1


# ===========================================================================
# §8 — Backward compat: merkle flag off → byte-identical legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_merkle_flag_off_full_scan_every_cycle(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When per-sensor flag is off, sensor must NEVER consult
    cartographer — legacy behavior preserved. Hot-revert path."""
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "false")
    sensor = make_sensor()
    await sensor.scan_once()
    await sensor.scan_once()
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 3  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_merkle_flag_off_does_not_call_cartographer(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-level guarantee: flag off → cartographer never imported.

    Pinned at runtime by checking the monkeypatched import path
    isn't called. Post-graduation, must explicitly set the flag false
    (delenv now defaults to True)."""
    monkeypatch.setenv("JARVIS_TODO_USE_MERKLE", "false")

    call_count = 0
    original_get = mc.get_default_cartographer

    def _spy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(
        mc, "get_default_cartographer", _spy,
    )
    sensor = make_sensor()
    await sensor.scan_once()
    await sensor.scan_once()
    # Flag off → cartographer get_default never called
    assert call_count == 0


# ===========================================================================
# §9 — Source-level pins
# ===========================================================================


def test_source_imports_cartographer_lazily() -> None:
    """The cartographer import must be inside the consult method,
    NOT at module top level — keeps todo_scanner_sensor's module
    load path independent of the cartographer module."""
    import inspect
    src = inspect.getsource(
        TodoScannerSensor._merkle_current_root_hash,
    )
    assert (
        "from backend.core.ouroboros.governance.merkle_cartographer"
        in src
    )


def test_source_short_circuit_branch_in_scan_once() -> None:
    """The merkle short-circuit branch must fire BEFORE the
    `loop.run_in_executor(self._scan_files_sync)` call so the disk
    walk is genuinely skipped."""
    import inspect
    src = inspect.getsource(TodoScannerSensor.scan_once)
    sc_idx = src.index("_merkle_should_short_circuit")
    walk_idx = src.index("run_in_executor")
    assert sc_idx < walk_idx
