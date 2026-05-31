"""Slice 48 — boot-time stale .jarvis/*.lock reaper.

v43 flagged a ~53h-old .jarvis/metrics_history.jsonl.lock (debris from a long
dead session). flock auto-releases on process death, so these files are inert
crumbs — but they accumulate and pollute the workspace. This reaper purges
.lock files whose mtime is older than a 24h (env-tunable) threshold at boot.

Pins:
  §1  a lock older than the threshold is purged
  §2  a fresh lock is preserved
  §3  nested locks (e.g. aegis/spend.jsonl.lock) are reached recursively
  §4  intake_router.lock is left to its dedicated PID-aware handler
  §5  non-.lock files are never touched
  §6  missing .jarvis dir → returns 0, never raises
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.ouroboros_battle_test import _reap_stale_jarvis_locks

_DAY_S = 86400.0


def _age(path: Path, seconds_old: float) -> None:
    """Backdate a file's mtime by ``seconds_old``."""
    past = time.time() - seconds_old
    os.utime(path, (past, past))


# ── §1 old lock purged ──────────────────────────────────────────────────
def test_old_lock_is_purged(tmp_path: Path) -> None:
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    stale = jarvis / "metrics_history.jsonl.lock"
    stale.write_text("")
    _age(stale, 25 * 3600)  # 25h old

    reaped = _reap_stale_jarvis_locks(jarvis, max_age_s=_DAY_S)

    assert reaped == 1
    assert not stale.exists()


# ── §2 fresh lock preserved ─────────────────────────────────────────────
def test_fresh_lock_preserved(tmp_path: Path) -> None:
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    fresh = jarvis / "live.jsonl.lock"
    fresh.write_text("")

    reaped = _reap_stale_jarvis_locks(jarvis, max_age_s=_DAY_S)

    assert reaped == 0
    assert fresh.exists()


# ── §3 nested locks reached recursively ─────────────────────────────────
def test_nested_lock_reaped(tmp_path: Path) -> None:
    jarvis = tmp_path / ".jarvis"
    (jarvis / "aegis").mkdir(parents=True)
    nested = jarvis / "aegis" / "spend.jsonl.lock"
    nested.write_text("")
    _age(nested, 48 * 3600)

    reaped = _reap_stale_jarvis_locks(jarvis, max_age_s=_DAY_S)

    assert reaped == 1
    assert not nested.exists()


# ── §4 intake_router.lock left alone (dedicated handler) ────────────────
def test_intake_router_lock_preserved(tmp_path: Path) -> None:
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    router = jarvis / "intake_router.lock"
    router.write_text('{"pid": 123}')
    _age(router, 99 * 3600)  # ancient, but PID-aware handler owns it

    reaped = _reap_stale_jarvis_locks(jarvis, max_age_s=_DAY_S)

    assert router.exists()
    assert reaped == 0


# ── §5 non-.lock files untouched ────────────────────────────────────────
def test_non_lock_files_untouched(tmp_path: Path) -> None:
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    data = jarvis / "spend.jsonl"
    data.write_text("{}")
    _age(data, 100 * 3600)

    reaped = _reap_stale_jarvis_locks(jarvis, max_age_s=_DAY_S)

    assert reaped == 0
    assert data.exists()


# ── §6 missing dir is safe ──────────────────────────────────────────────
def test_missing_jarvis_dir_is_safe(tmp_path: Path) -> None:
    reaped = _reap_stale_jarvis_locks(tmp_path / "nope", max_age_s=_DAY_S)
    assert reaped == 0
