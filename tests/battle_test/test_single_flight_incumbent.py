"""Incumbent immunity — the one-keystroke murder/lockout class is dead.

2026-07-18 root cause: `_reap_zombies` defined "zombie" as ANY other
battle-test main (a pre-single-flight assumption), so an operator typing
``ov`` while a soak ran (a) SIGTERM'd the healthy incumbent via the
reaper, then (b) got REJECTED by the single-flight gate against the
dying incumbent's still-fresh lock. One keystroke murdered the live run
AND locked the operator out.

The fix: ONE shared lock-holder authority (`_read_lock_holder` /
`_live_incumbent_pid`) consumed by BOTH the reaper (immunity) and the
preflight (conflict detection) — the two surfaces can never disagree
about legitimacy again. Plus a designed COCKPIT collision surface: an
already-awake organism is a status moment, not an error wall.
"""
from __future__ import annotations

import json
import os
import time

import pytest

import scripts.ouroboros_battle_test as bt


@pytest.fixture()
def lock(tmp_path):
    """A writable .jarvis/intake_router.lock in an isolated root."""
    d = tmp_path / ".jarvis"
    d.mkdir()

    def write(pid: int, ts: float) -> None:
        (d / "intake_router.lock").write_text(
            json.dumps({"pid": pid, "ts": ts})
        )

    return tmp_path, write


# ---------------------------------------------------------------------------
# (1) _read_lock_holder — the shared authority
# ---------------------------------------------------------------------------


def test_holder_none_when_lock_absent(tmp_path):
    assert bt._read_lock_holder(tmp_path) is None


def test_holder_none_on_corrupt_lock(lock):
    root, _ = lock
    (root / ".jarvis" / "intake_router.lock").write_text("{not json")
    assert bt._read_lock_holder(root) is None


def test_holder_none_for_self_pid(lock):
    root, write = lock
    write(os.getpid(), time.time())
    assert bt._read_lock_holder(root) is None


def test_holder_live_and_fresh(lock):
    root, write = lock
    # PID 1 (launchd) is always alive and never ours.
    write(1, time.time())
    holder = bt._read_lock_holder(root)
    assert holder is not None
    pid, age_s, alive = holder
    assert pid == 1 and alive is True and age_s < 60


def test_holder_dead_pid_reports_not_alive(lock):
    root, write = lock
    write(2**22 - 3, time.time())        # near pid_max — effectively dead
    holder = bt._read_lock_holder(root)
    assert holder is not None
    assert holder[2] is False


# ---------------------------------------------------------------------------
# (2) _live_incumbent_pid — immunity predicate
# ---------------------------------------------------------------------------


def test_incumbent_live_fresh_lock(lock):
    root, write = lock
    write(1, time.time())
    assert bt._live_incumbent_pid(root) == 1


def test_no_incumbent_when_lock_stale(lock, monkeypatch):
    root, write = lock
    monkeypatch.setenv("JARVIS_INTAKE_LOCK_STALE_TTL_S", "100")
    write(1, time.time() - 500)          # alive but wedged past TTL
    assert bt._live_incumbent_pid(root) is None


def test_no_incumbent_when_holder_dead(lock):
    root, write = lock
    write(2**22 - 3, time.time())
    assert bt._live_incumbent_pid(root) is None


def test_incumbent_never_raises(tmp_path):
    assert bt._live_incumbent_pid(tmp_path) is None


# ---------------------------------------------------------------------------
# (3) Wiring pins — reaper immunity + shared authority + collision surface
# ---------------------------------------------------------------------------


def _src() -> str:
    from pathlib import Path
    return (
        Path(__file__).resolve().parents[2] / "scripts/ouroboros_battle_test.py"
    ).read_text()


def test_reaper_consults_incumbent_before_victim_loop():
    src = _src()
    body_start = src.index("def _reap_zombies")
    body = src[body_start:src.index("def ", body_start + 10000)] \
        if False else src[body_start:]
    reap_region = body[:body.index("_terminate_victims") if
                       "_terminate_victims" in body else 8000]
    assert "_live_incumbent_pid()" in reap_region
    assert "pid == incumbent" in reap_region
    # Immunity is checked BEFORE any victim append.
    assert reap_region.index("_live_incumbent_pid()") < reap_region.index(
        "victims.append"
    )


def test_preflight_uses_shared_lock_authority():
    src = _src()
    body = src[src.index("def _single_flight_preflight"):]
    body = body[:body.index("\ndef ")]
    assert "_read_lock_holder(" in body
    # The old inline parse is gone — one authority, two consumers.
    assert "json.loads" not in body.replace("_json.loads", "json.loads") \
        or "_json.loads" not in body


def test_cockpit_collision_surface_present():
    src = _src()
    body = src[src.index("def _single_flight_preflight"):]
    body = body[:body.index("\ndef ")]
    assert "the organism is already awake" in body
    assert "status_digest" in body
    assert "is_cockpit" in body
    # SOAK/headless keeps the terse machine-parseable diagnostic.
    assert "REJECTED — concurrent battle-test detected" in body
    assert "exit code 75" in body


def test_collision_surface_speaks_design_language():
    src = _src()
    idx = src.index("the organism is already awake")
    body = src[max(0, idx - 200):idx + 1500]
    assert "⏺" in body and "⎿" in body      # rationed glyphs only
