from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest


@pytest.fixture
def supervisor_module(monkeypatch, tmp_path: Path):
    import unified_supervisor as us

    isolated_locks = tmp_path / "locks"
    isolated_locks.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(us, "LOCKS_DIR", isolated_locks)
    return us


def test_allocate_ephemeral_port_returns_bindable_port(supervisor_module):
    us = supervisor_module

    # Ephemeral port allocation can race with unrelated processes, so retry a
    # few times before failing the test.
    for _ in range(5):
        port = us._allocate_ephemeral_port("127.0.0.1")
        assert isinstance(port, int) and port > 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
            return
        except OSError:
            continue

    pytest.fail("Could not bind any allocated ephemeral port")


def test_startup_lock_reclaims_stale_lock_without_force(supervisor_module):
    us = supervisor_module
    lock = us.StartupLock(lock_name="stale-reclaim")

    stale_payload = {
        "pid": 999999,
        "acquired_at": "1970-01-01T00:00:00",
        "kernel_version": "test",
        "hostname": "test-host",
    }
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path.write_text(json.dumps(stale_payload))

    assert lock.acquire(force=False) is True
    acquired_payload = json.loads(lock.lock_path.read_text())
    assert acquired_payload.get("pid") == os.getpid()

    lock.release()


def test_startup_lock_rejects_live_lock_steal_by_default(supervisor_module, monkeypatch):
    us = supervisor_module
    owner = us.StartupLock(lock_name="live-holder")
    contender = us.StartupLock(lock_name="live-holder")
    contender.pid = owner.pid + 1

    # Force the lock liveness predicate to treat owner's PID as alive for this test.
    monkeypatch.setattr(
        us.StartupLock,
        "_holder_alive",
        staticmethod(lambda data: data.get("pid") == owner.pid),
    )

    assert owner.acquire(force=False) is True
    try:
        assert contender.acquire(force=False) is False
        assert contender.acquire(force=True) is False
    finally:
        owner.release()


# ------------------------------------------------- the holder is an identity
# 2026-10-14: a dead kernel's lock named PID 117732; Windows gave that PID to
# a VS Code GPU process, and the boot reported "kernel already running",
# tried to force-kill VS Code (refused by the kill guard) and gave up.

def _write(lock, **payload):
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path.write_text(json.dumps({"acquired_at": "x", "kernel_version": "t",
                                          "hostname": "h", **payload}))


def test_a_recycled_pid_does_not_hold_the_lock(supervisor_module):
    us = supervisor_module
    from backend.core.process_identity import process_started_at
    lock = us.StartupLock(lock_name="recycled")
    # A live process (this one) whose start time is not the recorded one.
    _write(lock, pid=os.getpid(), started_at=process_started_at(os.getpid()) - 3600)
    assert lock.is_locked() == (False, None)
    assert lock.acquire() is True


def test_the_process_that_took_the_lock_still_holds_it(supervisor_module):
    us = supervisor_module
    first = us.StartupLock(lock_name="held")
    assert first.acquire() is True
    recorded = json.loads(first.lock_path.read_text())
    assert recorded["pid"] == os.getpid() and recorded["started_at"] is not None
    other = us.StartupLock(lock_name="held")
    other.pid = os.getpid() + 1  # another kernel
    assert other.is_locked() == (True, os.getpid())
    assert other.acquire() is False


def test_an_old_lock_is_held_only_by_a_process_running_trinity_code(supervisor_module, monkeypatch):
    """No start time recorded: the holder must run Trinity's code -- judged
    structurally, never by the word 'jarvis' (every process under the
    Windows user 'Jarvis' carries it)."""
    us = supervisor_module
    from backend.core import process_identity as pi
    lock = us.StartupLock(lock_name="legacy")
    _write(lock, pid=os.getpid())
    monkeypatch.setattr(pi, "is_trinity_process", lambda proc, roots=None: False)
    assert lock.is_locked() == (False, None)
    monkeypatch.setattr(pi, "is_trinity_process", lambda proc, roots=None: True)
    assert lock.is_locked() == (True, os.getpid())


def test_an_unreadable_lock_holds_nothing(supervisor_module):
    us = supervisor_module
    lock = us.StartupLock(lock_name="garbled")
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    for content in ("not json", "[1, 2]", json.dumps({"pid": "abc"}), ""):
        lock.lock_path.write_text(content)
        assert lock.is_locked() == (False, None)
