"""Slice 11.6.c regression spine — OpportunityMinerSensor + Merkle Cartographer.

Subtree-scoped beef-up vs. 11.6.a/b: this sensor consults *per-watched-path*
subtree hashes, not just a single root hash. A change in ``docs/`` (outside
the miner's ``_scan_paths``) does NOT bust short-circuit. Only mutations
under a watched subtree force a full scan.

Pins:
  §1 Per-sensor flag — JARVIS_OPPMINER_USE_MERKLE default false +
                       truthy/falsy parsing
  §2 Cold-start: full scan even with merkle on (cache empty)
  §3 Steady state, no changes: short-circuit; cached candidates returned;
                               no router calls; no cycle counter advance
  §4 Watched-subtree change: full scan; cache + baseline refreshed
  §5 OUT-OF-SCOPE change does NOT bust cache (the beef)
  §6 Master flag off (cartographer): fail-safe to full scan
  §7 Cartographer error: fail-safe to full scan (NEVER raises)
  §8 Empty subtree hash (path doesn't exist) → fail-safe full scan
  §9 health() exposes merkle metrics + watched paths
  §10 Backward compat: merkle flag off → byte-identical legacy
  §11 Source-level pins
  §12 Path normalization (POSIX, ./ → root, trailing slash stripped)
"""
from __future__ import annotations

import asyncio  # noqa: F401  — pytest-asyncio plugin contract
import os  # noqa: F401  — env-var reads in fixtures
from pathlib import Path
from typing import Any, List  # noqa: F401  — used in body, not header
from unittest.mock import AsyncMock, MagicMock  # noqa: F401

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    opportunity_miner_sensor as oms,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
    StaticCandidate,  # noqa: F401  — type ref for readers
    merkle_consult_enabled,
)
from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _complex_module(num_branches: int = 12) -> str:
    """A module dense enough to clear the miner's complexity threshold
    (default 10). Series of nested ifs in a single function."""
    body = "    pass\n"
    for i in range(num_branches):
        body = f"    if x == {i}:\n    " + body.replace("\n", "\n    ").rstrip() + "\n"
    return (
        '"""Synthetic complex module for Slice 11.6.c tests."""\n\n'
        "def __init__(self):\n    pass\n\n"
        f"def complex_fn(x):\n{body}\n    return x\n"
    )


@pytest.fixture
def repo_with_opportunities(tmp_path: Path) -> Path:
    """Synthetic repo with two complex modules under ``backend/`` and
    one decoy file under ``docs/`` (outside the miner's scan scope but
    inside the cartographer's default include list)."""
    backend = tmp_path / "backend"
    backend.mkdir()
    # Make backend a package + add complex modules
    (backend / "__init__.py").write_text("")
    (backend / "complex_a.py").write_text(_complex_module(15))
    (backend / "complex_b.py").write_text(_complex_module(13))

    # docs/ — inside cartographer's default include but outside miner's
    # scan_paths. Used by §5 to prove out-of-scope changes don't bust cache.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "readme.md").write_text("# initial readme\n")

    return tmp_path


@pytest.fixture
def make_sensor(repo_with_opportunities: Path):
    """Factory: produces OpportunityMinerSensors rooted at the synthetic
    repo, scoped to ``backend/`` only."""

    def _make(**kwargs) -> OpportunityMinerSensor:
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        return OpportunityMinerSensor(
            repo_root=repo_with_opportunities,
            router=router,
            scan_paths=["backend"],
            complexity_threshold=5,  # low threshold so synthetic files clear it
            repo="JARVIS",
            **kwargs,
        )

    return _make


@pytest.fixture
def isolated_cartographer(
    repo_with_opportunities: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Fresh cartographer rooted at the synthetic repo + isolated state dir."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo_with_opportunities))
    mc.reset_default_cartographer_for_tests()
    yield
    mc.reset_default_cartographer_for_tests()


# ===========================================================================
# §1 — Per-sensor flag
# ===========================================================================


def test_merkle_consult_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_OPPMINER_USE_MERKLE", raising=False)
    assert merkle_consult_enabled() is False


def test_merkle_consult_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", val)
        assert merkle_consult_enabled() is True


def test_merkle_consult_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", val)
        assert merkle_consult_enabled() is False


# ===========================================================================
# §2 — Cold-start: must full-scan even with merkle on
# ===========================================================================


@pytest.mark.asyncio
async def test_cold_start_full_scan_even_with_merkle_on(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
    isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    candidates = await sensor.scan_once()
    assert sensor._merkle_full_scans == 1  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001
    assert sensor._merkle_cached_candidates == candidates  # noqa: SLF001
    # Baseline now populated for next cycle
    assert "backend" in sensor._merkle_last_seen_subtree_hashes  # noqa: SLF001


# ===========================================================================
# §3 — Steady state: no changes → short-circuit
# ===========================================================================


@pytest.mark.asyncio
async def test_short_circuit_when_no_changes(
    make_sensor, repo_with_opportunities: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    cart = mc.get_default_cartographer(repo_root=repo_with_opportunities)
    await cart.update_full()

    sensor = make_sensor()
    first = await sensor.scan_once()
    cycle_after_first = sensor._scan_cycle  # noqa: SLF001

    # Snapshot router call count before second scan
    router_calls_before = sensor._router.ingest.call_count

    second = await sensor.scan_once()
    assert sensor._merkle_short_circuits == 1  # noqa: SLF001
    assert sensor._merkle_full_scans == 1  # noqa: SLF001 (unchanged)
    assert second == first
    # Cycle counter MUST NOT advance on short-circuit (no work was done)
    assert sensor._scan_cycle == cycle_after_first  # noqa: SLF001
    # Router was NOT called on the short-circuit cycle
    assert sensor._router.ingest.call_count == router_calls_before


# ===========================================================================
# §4 — Watched-subtree change → full scan + cache refreshed
# ===========================================================================


@pytest.mark.asyncio
async def test_full_scan_when_watched_subtree_changes(
    make_sensor, repo_with_opportunities: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    cart = mc.get_default_cartographer(repo_root=repo_with_opportunities)
    await cart.update_full()

    sensor = make_sensor()
    await sensor.scan_once()
    baseline = dict(sensor._merkle_last_seen_subtree_hashes)  # noqa: SLF001
    assert baseline.get("backend"), "backend subtree hash must be populated"

    # Mutate something INSIDE backend/ → cartographer must report change
    (repo_with_opportunities / "backend" / "complex_c.py").write_text(
        _complex_module(20),
    )
    await cart.update_full()
    assert cart.subtree_hash("backend") != baseline["backend"]

    await sensor.scan_once()
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001
    # Baseline updated to fresh subtree hash
    new_baseline = sensor._merkle_last_seen_subtree_hashes  # noqa: SLF001
    assert new_baseline["backend"] != baseline["backend"]


# ===========================================================================
# §5 — THE BEEF: out-of-scope change does NOT bust cache
# ===========================================================================


@pytest.mark.asyncio
async def test_out_of_scope_change_does_not_bust_cache(
    make_sensor, repo_with_opportunities: Path,
    monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    """The cartographer's root hash includes ``docs/``, but the miner
    only watches ``backend/``. A change to ``docs/readme.md`` MUST NOT
    invalidate the miner's short-circuit — the subtree hash for
    ``backend/`` is unchanged."""
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    cart = mc.get_default_cartographer(repo_root=repo_with_opportunities)
    await cart.update_full()

    sensor = make_sensor()
    await sensor.scan_once()
    pre_root = cart.current_root_hash()
    pre_backend = cart.subtree_hash("backend")

    # Mutate OUTSIDE the miner's scope — root hash WILL change but
    # backend subtree hash MUST NOT
    (repo_with_opportunities / "docs" / "readme.md").write_text(
        "# different readme content\n"
    )
    await cart.update_full()
    post_root = cart.current_root_hash()
    post_backend = cart.subtree_hash("backend")

    assert post_root != pre_root, (
        "root hash should change when any tracked dir changes"
    )
    assert post_backend == pre_backend, (
        "backend subtree hash MUST NOT change when only docs/ changed"
    )

    # Sensor must short-circuit despite the root-hash change
    await sensor.scan_once()
    assert sensor._merkle_short_circuits == 1  # noqa: SLF001
    assert sensor._merkle_full_scans == 1  # noqa: SLF001 (unchanged)


# ===========================================================================
# §6 — Master flag off → fail-safe full scan
# ===========================================================================


@pytest.mark.asyncio
async def test_cartographer_master_flag_off_falls_through_to_full_scan(
    make_sensor, monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.delenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", raising=False)
    sensor = make_sensor()

    await sensor.scan_once()
    await sensor.scan_once()
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 3  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001


# ===========================================================================
# §7 — Cartographer error → fail-safe full scan (NEVER raises)
# ===========================================================================


@pytest.mark.asyncio
async def test_cartographer_error_falls_through_to_full_scan(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()  # cold scan to populate cache

    def _broken_subtree(self, *args, **kwargs):
        raise RuntimeError("simulated cartographer crash")

    monkeypatch.setattr(
        mc.MerkleCartographer, "subtree_hash", _broken_subtree,
    )
    monkeypatch.setattr(
        mc.MerkleCartographer, "current_root_hash", _broken_subtree,
    )

    candidates = await sensor.scan_once()
    assert isinstance(candidates, list)
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001


# ===========================================================================
# §8 — Empty subtree hash (path missing) → fail-safe
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_subtree_hash_falls_through_to_full_scan(
    make_sensor, monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    """If the cartographer can't resolve a watched subtree (path doesn't
    exist on disk yet, hasn't been hydrated, etc.) the sensor must fail
    safe and full-scan rather than treating empty hash as 'unchanged'."""
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    # Cold scan to seed cache
    await sensor.scan_once()

    # Inject empty subtree hash via monkeypatch
    def _empty_hash(self, *args, **kwargs):
        return ""

    monkeypatch.setattr(
        mc.MerkleCartographer, "subtree_hash", _empty_hash,
    )

    await sensor.scan_once()
    assert sensor._merkle_full_scans >= 2  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001


# ===========================================================================
# §9 — health() exposes merkle metrics + watched paths
# ===========================================================================


def test_health_exposes_merkle_metrics(make_sensor) -> None:
    sensor = make_sensor()
    h = sensor.health()
    assert h["sensor"] == "OpportunityMinerSensor"
    assert "merkle_consult_enabled" in h
    assert "merkle_short_circuits" in h
    assert "merkle_full_scans" in h
    assert "merkle_watched_paths" in h
    assert "merkle_cached_candidates" in h
    assert h["merkle_short_circuits"] == 0
    assert h["merkle_full_scans"] == 0
    assert h["merkle_cached_candidates"] == 0


@pytest.mark.asyncio
async def test_health_after_scan_reflects_metrics(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
    isolated_cartographer,
) -> None:
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    h = sensor.health()
    assert h["merkle_full_scans"] == 1
    assert "backend" in h["merkle_watched_paths"]


# ===========================================================================
# §10 — Backward compat: merkle flag off → byte-identical legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_merkle_flag_off_full_scan_every_cycle(
    make_sensor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_OPPMINER_USE_MERKLE", raising=False)
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
    monkeypatch.delenv("JARVIS_OPPMINER_USE_MERKLE", raising=False)

    call_count = 0
    original_get = mc.get_default_cartographer

    def _spy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(mc, "get_default_cartographer", _spy)
    sensor = make_sensor()
    await sensor.scan_once()
    await sensor.scan_once()
    assert call_count == 0


# ===========================================================================
# §11 — Source-level pins
# ===========================================================================


def test_source_imports_cartographer_lazily() -> None:
    """The cartographer import must be inside the consult method, NOT
    at module top level — keeps opportunity_miner_sensor's module load
    path independent of the cartographer module."""
    import inspect
    src = inspect.getsource(
        OpportunityMinerSensor._merkle_subtree_hashes,
    )
    assert (
        "from backend.core.ouroboros.governance.merkle_cartographer"
        in src
    )


def test_source_short_circuit_branch_in_scan_once() -> None:
    """The merkle short-circuit branch must fire BEFORE the
    `loop.run_in_executor(self._scan_files_sync, ...)` call so the AST
    sweep is genuinely skipped."""
    import inspect
    src = inspect.getsource(OpportunityMinerSensor.scan_once)
    sc_idx = src.index("_merkle_should_short_circuit")
    walk_idx = src.index("run_in_executor")
    assert sc_idx < walk_idx


def test_source_no_top_level_cartographer_import() -> None:
    """Module top-level imports must NOT include merkle_cartographer."""
    import inspect
    src = inspect.getsource(oms)
    header_end = min(
        (i for i in (src.find("\ndef "), src.find("\nclass "))
         if i > 0),
        default=len(src),
    )
    header = src[:header_end]
    assert "from backend.core.ouroboros.governance.merkle_cartographer" not in header


def test_source_uses_subtree_hash_not_just_root_hash() -> None:
    """The beef: this sensor MUST consult subtree_hash, not just
    current_root_hash. Pins the subtree-scoped contract source-level."""
    import inspect
    src = inspect.getsource(OpportunityMinerSensor._merkle_subtree_hashes)
    assert "subtree_hash" in src, (
        "Slice 11.6.c contract: subtree-scoped consultation required"
    )


# ===========================================================================
# §12 — Path normalization
# ===========================================================================


def test_normalized_scan_paths_dot_becomes_root(
    make_sensor,
) -> None:
    sensor = make_sensor()
    sensor._scan_paths = ["."]  # noqa: SLF001
    norm = sensor._merkle_normalized_scan_paths()  # noqa: SLF001
    assert norm == [""]


def test_normalized_scan_paths_strips_trailing_slash(
    make_sensor,
) -> None:
    sensor = make_sensor()
    sensor._scan_paths = ["backend/", "/tests/", "scripts"]  # noqa: SLF001
    norm = sensor._merkle_normalized_scan_paths()  # noqa: SLF001
    assert norm == ["backend", "tests", "scripts"]


def test_normalized_scan_paths_handles_windows_seps(
    make_sensor,
) -> None:
    sensor = make_sensor()
    sensor._scan_paths = ["backend\\subdir"]  # noqa: SLF001
    norm = sensor._merkle_normalized_scan_paths()  # noqa: SLF001
    assert norm == ["backend/subdir"]


@pytest.mark.asyncio
async def test_scan_path_topology_change_busts_cache(
    make_sensor, monkeypatch: pytest.MonkeyPatch, isolated_cartographer,
) -> None:
    """If operator reconfigures _scan_paths between scans, the baseline
    keys won't match the current keys — must fail-safe to full scan."""
    monkeypatch.setenv("JARVIS_OPPMINER_USE_MERKLE", "true")
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    sensor = make_sensor()
    await sensor.scan_once()
    assert sensor._merkle_full_scans == 1  # noqa: SLF001

    # Simulate operator adding a new watched path mid-session
    sensor._scan_paths = ["backend", "tests"]  # noqa: SLF001

    await sensor.scan_once()
    # Topology changed → full scan, no short-circuit
    assert sensor._merkle_full_scans == 2  # noqa: SLF001
    assert sensor._merkle_short_circuits == 0  # noqa: SLF001
