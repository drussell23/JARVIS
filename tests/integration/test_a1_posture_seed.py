"""A1 posture-context seeding — the organism natively understands its
operating context.

Run a1-brain-20260705-233225: 63 doc_staleness + 3 todo_scanner background
signals swamped the loop during an A1 ignition rehearsal while the blast=1
vector's lane sat idle. Rather than a hardcoded sensor-disable flag (a
governance bypass), the driver seeds the EXISTING durable posture triplet
(``.jarvis/posture_current.json``) with HARDEN before organism boot. The
organism's own metacognitive stack then reshapes everything natively:

  * SensorGovernor weighted caps — HARDEN starves discovery/consolidation
    sensors (0.3-0.6x) and boosts TestFailure (1.8x) via the seed-spec
    weight algebra (sensor_governor_seed.py);
  * StrategicDirection / CONTEXT_EXPANSION posture prompt injection;
  * L3 subagent_scheduler direct enforcement (Slice 5 Arc B).

The seed is a *starting state*, not a lock: PostureObserver's own cadence +
hysteresis may legitimately move off it if live signals demand — honest,
dynamic, no authority bypass. Round-trips through the REAL PostureStore
(fakes-mirror-real-contract).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, "Cannot load %s from %s" % (name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_driver_mod = _load_script("isomorphic_a1_local")
_seed_posture_context = _driver_mod._seed_posture_context


def _load_current(root: Path):
    """Read back through the REAL store — the same code path the
    organism's SensorGovernor + PostureObserver consume at boot."""
    from backend.core.ouroboros.governance.posture_store import PostureStore

    return PostureStore(root / ".jarvis").load_current()


def test_seed_writes_harden_reading_real_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_A1_POSTURE_SEED", raising=False)
    assert _seed_posture_context(str(tmp_path)) is True
    reading = _load_current(tmp_path)
    assert reading is not None, "seeded reading rejected by the real store"
    assert reading.posture.value == "HARDEN"
    # High-confidence starting state (>= the observer's 0.75 bypass tier).
    assert reading.confidence >= 0.75


def test_seed_env_selects_other_posture(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", "EXPLORE")
    assert _seed_posture_context(str(tmp_path)) is True
    reading = _load_current(tmp_path)
    assert reading is not None
    assert reading.posture.value == "EXPLORE"


@pytest.mark.parametrize("off", ["", "0", "false", "off", "none", "OFF"])
def test_seed_disabled_writes_nothing(tmp_path, monkeypatch, off):
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", off)
    assert _seed_posture_context(str(tmp_path)) is False
    assert not (tmp_path / ".jarvis" / "posture_current.json").exists()


def test_seed_invalid_value_fails_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", "TURBO_MODE")
    # Never raises, never writes garbage the organism would then trust.
    assert _seed_posture_context(str(tmp_path)) is False
    assert not (tmp_path / ".jarvis" / "posture_current.json").exists()


def test_seed_never_clobbers_existing_reading(tmp_path, monkeypatch):
    """A pre-existing posture state (resumed overlay, operator-set) is
    NEVER overwritten — the seed is a cold-start default, not authority."""
    monkeypatch.delenv("JARVIS_A1_POSTURE_SEED", raising=False)
    assert _seed_posture_context(str(tmp_path)) is True
    first = _load_current(tmp_path)
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", "EXPLORE")
    assert _seed_posture_context(str(tmp_path)) is False
    second = _load_current(tmp_path)
    assert second is not None and second.posture == first.posture


def test_spine_driver_seeds_inside_isomorphic_block():
    """The seed call must live in the driver's IsomorphicEnv block —
    before organism boot, against the overlay root."""
    src = (_REPO_ROOT / "scripts" / "isomorphic_a1_local.py").read_text(
        encoding="utf-8"
    )
    assert "_seed_posture_context(" in src.split("with IsomorphicEnv", 1)[1], (
        "driver no longer seeds posture context inside the IsomorphicEnv "
        "block — the organism boots context-blind and background sensors "
        "swamp A1 rehearsals again"
    )


# ===========================================================================
# Ignite forwarding — operator-explicit seed override reaches the node
# ===========================================================================


def _load_ignite():
    return _load_script("ignite_a1_brain")


def test_ignite_forwards_operator_posture_seed(monkeypatch):
    ignite = _load_ignite()
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", "off")
    cmd = ignite._compose_remote_command(
        remote_run_dir="/opt/trinity/jarvis/a1_iso_runs/x", seed=0,
        verbose=False, soak_wall_s=1800, dw_session_budget=5.0,
    )
    assert "JARVIS_A1_POSTURE_SEED=off " in cmd


def test_ignite_omits_seed_when_unset(monkeypatch):
    ignite = _load_ignite()
    monkeypatch.delenv("JARVIS_A1_POSTURE_SEED", raising=False)
    cmd = ignite._compose_remote_command(
        remote_run_dir="/opt/trinity/jarvis/a1_iso_runs/x", seed=0,
        verbose=False, soak_wall_s=1800, dw_session_budget=5.0,
    )
    # Default lives in the driver (HARDEN); ignite adds nothing.
    assert "JARVIS_A1_POSTURE_SEED" not in cmd


def test_ignite_drops_non_token_seed_value(monkeypatch):
    """The remote cmd runs inside a single-quoted bash -c wrapper — a
    non-token value must be dropped, never quoted-through (the run-8
    inner-single-quote dispatch-hang class)."""
    ignite = _load_ignite()
    monkeypatch.setenv("JARVIS_A1_POSTURE_SEED", "HARDEN'; rm -rf /tmp; '")
    cmd = ignite._compose_remote_command(
        remote_run_dir="/opt/trinity/jarvis/a1_iso_runs/x", seed=0,
        verbose=False, soak_wall_s=1800, dw_session_budget=5.0,
    )
    assert "JARVIS_A1_POSTURE_SEED" not in cmd
    assert "rm -rf" not in cmd
