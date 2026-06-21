"""Tests for I5 — prove explicit env=false is overridden by the DeterministicLock (G4).

The lock emits:
    [DeterministicLock] forced isolation+boundary despite env …
whenever it arms BOTH flags regardless of JARVIS_FILE_ISOLATION_ENABLED in the
environment.  I5 is satisfied iff that marker is present in the boot/session
debug log, the primary checkout is still pristine, and both flags are confirmed
armed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = _ROOT / "scripts" / "verify_file_isolation.py"
    spec = importlib.util.spec_from_file_location("vfi_i5_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


vfi = _load()

# Canonical marker emitted by DeterministicLock when env=false is overridden.
_LOCK_LINE = (
    "[DeterministicLock] forced isolation+boundary despite env "
    "(primary checkout, autonomous) root=/x session=s1"
)


def test_i5_passes_when_lock_fired_and_pristine():
    inv = vfi.assess_i5_override(
        debug_log=_LOCK_LINE,
        primary_dirty=False,
        file_iso_armed=True,
        exec_boundary_armed=True,
    )
    assert inv.status == vfi.PASS


def test_i5_fails_when_lock_marker_absent():
    inv = vfi.assess_i5_override(
        debug_log="no marker here",
        primary_dirty=False,
        file_iso_armed=True,
        exec_boundary_armed=True,
    )
    assert inv.status == vfi.FAIL


def test_i5_fails_when_primary_dirty():
    inv = vfi.assess_i5_override(
        debug_log=_LOCK_LINE,
        primary_dirty=True,
        file_iso_armed=True,
        exec_boundary_armed=True,
    )
    assert inv.status == vfi.FAIL


def test_i5_fails_when_a_flag_not_armed():
    inv = vfi.assess_i5_override(
        debug_log=_LOCK_LINE,
        primary_dirty=False,
        file_iso_armed=True,
        exec_boundary_armed=False,
    )
    assert inv.status == vfi.FAIL
