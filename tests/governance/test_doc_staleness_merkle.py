"""Slice 11.6.b regression spine — DocStalenessSensor + Merkle Cartographer.

Pins:
  §1 Per-sensor flag — JARVIS_DOCSTALE_USE_MERKLE default false +
                       truthy/falsy parsing
  §2 Cold-start: full scan even with merkle on (cache empty)
  §3 Steady state with no changes: short-circuit; cached findings returned
  §4 Steady state with changes: full scan; cache refreshed
  §5 Master flag off (cartographer): fail-safe to full scan
  §6 Cartographer error: fail-safe to full scan (NEVER raises)
  §7 health() exposes merkle_short_circuits + merkle_full_scans
  §8 Backward compat: merkle flag off → byte-identical legacy
  §9 Source-level pins (lazy import + branch ordering)
"""
from __future__ import annotations

import asyncio  # noqa: F401  — pytest-asyncio plugin contract
import os  # noqa: F401  — env-var reads in fixtures
from pathlib import Path
from typing import Any, List  # noqa: F401  — used in body, not header
from unittest.mock import AsyncMock, MagicMock  # noqa: F401

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    doc_staleness_sensor as dss,
)
from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
    DocFinding,  # noqa: F401  — type reference for readers
    DocStalenessSensor,
    merkle_consult_enabled,
)
from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _undocumented_module(num_public: int = 4) -> str:
    """Return Python source whose module-level AST has ``num_public``
    undocumented public functions and no module docstring — matches
    DocStalenessSensor's emission criteria (coverage < 0.5)."""
    fns = "\n\n".join(
        f"def public_fn_{i}(x):\n    return x + {i}"
        for i in range(num_public)
    )
    return fns + "\n"


@pytest.fixture
def repo_with_undocumented(tmp_path: Path) -> Path:
    """Synthetic repo with two undocumented modules + one well-documented
    module under a single watched scan path (``app/``)."""
    app = tmp_path / "backend"
    app.mkdir()
    # Two undocumented modules — should each emit a finding
    (app / "undocumented_a.py").write_text(_undocumented_module(num_public=4))
    (app / "undocumented_b.py").write_text(_undocumented_module(num_public=5))
    # One well-documented module — should NOT emit a finding
    (app / "documented.py").write_text(
        '"""Module-level docstring."""\n\n'
        'def well_doc_a(x):\n    """Docstring."""\n    return x\n\n'
        'def well_doc_b(y):\n    """Docstring."""\n    return y\n\n'
        'def well_doc_c(z):\n    """Docstring."""\n    return z\n'
    )
    return tmp_path


@pytest.fixture
def make_sensor(repo_with_undocumented: Path):
    """Factory: produces DocStalenessSensors rooted at the synthetic repo."""

    def _make(**kwargs) -> DocStalenessSensor:
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        return DocStalenessSensor(
            repo="JARVIS",
            router=router,
            poll_interval_s=86400.0,
            project_root=repo_with_undocumented,
            scan_paths=("backend/",),
            **kwargs,
        )

    return _make


@pytest.fixture
def isolated_cartographer(
    repo_with_undocumented: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Fresh cartographer rooted at the synthetic repo + isolated state dir."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo_with_undocumented))
    mc.reset_default_cartographer_for_tests()
    yield
    mc.reset_default_cartographer_for_tests()


# ===========================================================================
# §1 — Per-sensor flag
# ===========================================================================


def test_merkle_consult_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_DOCSTALE_USE_MERKLE", raising=False)
    assert merkle_consult_enabled() is False


def test_merkle_consult_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", val)
        assert merkle_consult_enabled() is True


def test_merkle_consult_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", val)
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
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    findings = await sensor.scan_once()
    # Two undocumented modules in fixture → 2 findings
    assert len(findings) == 2
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001
    assert len(sensor._merkle_cached_findings) == len(findings)  # noqa: SLF001


# ===========================================================================
# §3 — Steady state: cartographer says no change → short-circuit
# ===========================================================================


@pytest.mark.asyncio
async def test_short_circuit_when_no_changes(
    make_sensor, repo_with_undocumented: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    # Build cartographer's tree first so the sensor's first scan
    # captures a stable baseline.
    cart = mc.get_default_cartographer(repo_root=repo_with_undocumented)
    await cart.update_full()

    sensor = make_sensor()

    # First scan — cold start: walks disk + records baseline
    first_findings = await sensor.scan_once()
    initial_count = len(first_findings)
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    assert sensor._merkle_last_seen_root_hash != ""  # noqa: SLF001

    # Second scan — no changes happened, cartographer's root hash
    # unchanged, sensor short-circuits using the stored baseline.
    second_findings = await sensor.scan_once()
    assert sensor._merkle_short_circuits == 1  # noqa: SLF001
    assert sensor._merkle_full_scans == 1  # noqa: SLF001 (unchanged)
    assert len(second_findings) == initial_count
    # Cached findings returned (a NEW list — but same content)
    assert second_findings == first_findings


# ===========================================================================
# §4 — has_changed=True → full scan, cache refreshed
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_cartographer_reports_change(
    make_sensor, repo_with_undocumented: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    # Build cartographer's tree first
    cart = mc.get_default_cartographer(repo_root=repo_with_undocumented)
    await cart.update_full()

    sensor = make_sensor()

    # Cold-start scan — captures baseline
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    baseline_hash = sensor._merkle_last_seen_root_hash  # noqa: SLF001
    assert baseline_hash != ""

    # Add a NEW undocumented module — file system genuinely changes
    (repo_with_undocumented / "backend" / "undocumented_c.py").write_text(
        _undocumented_module(num_public=4),
    )
    # Refresh cartographer to pick up the new file → root hash changes
    await cart.update_full()
    new_hash = cart.current_root_hash()
    assert new_hash != baseline_hash, (
        "cartographer root hash should change when a new file is added"
    )

    # Next scan: cartographer's hash differs from sensor's baseline
    # → no short-circuit, full scan + baseline refreshed.
    second_findings = await sensor.scan_once()
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001
    file_paths = {f.file_path for f in second_findings}
    assert any("undocumented_c.py" in p for p in file_paths)
    # Baseline updated to new hash
    assert sensor._merkle_last_seen_root_hash == new_hash  # noqa: SLF001


# ===========================================================================
# §5 — Master flag off → fail-safe full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_cartographer_master_flag_off_falls_through_to_full_scan(
    make_sensor, repo_with_undocumented: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    """Per-sensor flag on, but cartographer master flag OFF →
    current_root_hash returns "" → sensor treats as 'always changed'
    → full scan every cycle (legacy behavior preserved)."""
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
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
    make_sensor, repo_with_undocumented: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cartographer module fails to import / current_root_hash
    raises, sensor falls through to legacy full scan. NEVER blocks."""
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
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
    findings = await sensor.scan_once()
    assert isinstance(findings, list)
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
    assert "merkle_cached_findings" in h
    # Cold sensor — zero metrics
    assert h["merkle_short_circuits"] == 0
    assert h["merkle_full_scans"] == 0
    assert h["merkle_cached_findings"] == 0


@pytest.mark.asyncio
async def test_health_after_scan_reflects_metrics(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
    isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_DOCSTALE_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    h = sensor.health()
    assert h["merkle_full_scans"] == 1
    assert h["merkle_cached_findings"] >= 1


# ===========================================================================
# §8 — Backward compat: merkle flag off → byte-identical legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_merkle_flag_off_full_scan_every_cycle(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When per-sensor flag is off, sensor must NEVER consult
    cartographer — legacy behavior preserved."""
    monkeypatch.delenv("JARVIS_DOCSTALE_USE_MERKLE", raising=False)
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
    isn't called."""
    monkeypatch.delenv("JARVIS_DOCSTALE_USE_MERKLE", raising=False)

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
    NOT at module top level — keeps doc_staleness_sensor's module
    load path independent of the cartographer module."""
    import inspect
    src = inspect.getsource(
        DocStalenessSensor._merkle_current_root_hash,
    )
    assert (
        "from backend.core.ouroboros.governance.merkle_cartographer"
        in src
    )


def test_source_short_circuit_branch_in_scan_once() -> None:
    """The merkle short-circuit branch must fire BEFORE the
    `loop.run_in_executor(self._scan_files_sync)` call so the AST
    sweep is genuinely skipped."""
    import inspect
    src = inspect.getsource(DocStalenessSensor.scan_once)
    sc_idx = src.index("_merkle_should_short_circuit")
    walk_idx = src.index("run_in_executor")
    assert sc_idx < walk_idx


def test_source_no_top_level_cartographer_import() -> None:
    """Module top-level imports must NOT include merkle_cartographer —
    importing the sensor should never transitively load the
    cartographer module (graduation safety)."""
    import inspect
    src = inspect.getsource(dss)
    # Find the first ``def `` or ``class `` to mark end of header imports
    header_end = min(
        (i for i in (src.find("\ndef "), src.find("\nclass "))
         if i > 0),
        default=len(src),
    )
    header = src[:header_end]
    assert "from backend.core.ouroboros.governance.merkle_cartographer" not in header
