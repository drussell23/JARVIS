"""Bulletproof spine for the State-Reactive Autonomous Supervisor.

Mandated assertions, all against the REAL substrate (real SQLite soak_intent +
provider_state on a temp file, real StreamEventBroker) with ONLY the subprocess
spawn faked (a controllable FakeProc) so the lifecycle is deterministic:

  (1) injecting a pending soak intent while DW is DEGRADED autonomously SPAWNS
      the Sentinel subprocess and arms the supervisor,
  (2) simulating a SIGKILL on the mock Sentinel triggers the Watchdog's automatic
      exponential-backoff RESTART, and
  (3) clearing the pending queue + a broker completion event autonomously
      TERMINATES the Sentinel (SIGTERM) and DISARMS the system.
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3

import pytest

from backend.core.ouroboros.governance.autonomous_supervisor import (
    STATE_ARMED,
    STATE_DORMANT,
    AutonomousSupervisor,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_OPERATION_TERMINAL,
    get_default_broker,
    reset_default_broker,
)
from backend.core.ouroboros.governance.provider_state import mark_degraded, mark_healthy
from backend.core.ouroboros.governance.soak_intent import (
    clear_soak_intent,
    enqueue_soak_intent,
)


async def _wait_for(cond, timeout: float = 3.0) -> None:
    async def _loop() -> None:
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout)


class _FakeStream:
    """A subprocess stdout that yields nothing then EOF (telemetry pump exits)."""

    async def readline(self):
        return b""


class _FakeProc:
    """A controllable stand-in for an asyncio subprocess. Faithful to the real
    contract: ``wait()`` may be awaited by MULTIPLE independent callers and
    cancelling one waiter never affects the others or the process (an Event, not
    a shared cancellable future)."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self.stdout = _FakeStream()
        self.signals: list = []
        self._rc = None
        self._exited = asyncio.Event()

    async def wait(self):
        await self._exited.wait()          # per-caller future; cancel-isolated
        self.returncode = self._rc
        return self._rc

    def _exit(self, rc: int) -> None:
        if not self._exited.is_set():
            self._rc = rc
            self.returncode = rc
            self._exited.set()

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        if not self._exited.is_set():      # SIGTERM → clean exit
            self._exit(0)

    def kill(self) -> None:
        self.signals.append(signal.SIGKILL)
        self._exit(-9)

    def crash(self, rc: int = -9) -> None:
        """Simulate an unexpected death (SIGKILL / network exception)."""
        self._exit(rc)


class _SpawnRecorder:
    def __init__(self) -> None:
        self.procs: list = []

    async def __call__(self):
        proc = _FakeProc(pid=1000 + len(self.procs))
        self.procs.append(proc)
        return proc

    @property
    def count(self) -> int:
        return len(self.procs)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "chunk_strategy.db")


def _make_supervisor(db_path, spawn, crumbs, *, no_awe=True):
    return AutonomousSupervisor(
        spawn_fn=spawn,
        db_factory=lambda: sqlite3.connect(db_path),
        breadcrumb_fn=lambda et, p: crumbs.append((et, dict(p))),
        # Disarm test drives the broker; keep AWE out of the picture here so the
        # assertions isolate the supervisor's own lifecycle (AWE has its own spine).
        awe_factory=(lambda: None) if no_awe else None,
    )


async def test_intent_driven_autospawn(db_path, monkeypatch):
    """(1) pending intent + DEGRADED → autonomous spawn + ARMED."""
    monkeypatch.setenv("JARVIS_SUPERVISOR_BACKOFF_BASE_S", "0.01")
    reset_default_broker()
    conn = sqlite3.connect(db_path)
    mark_degraded(conn, "doubleword", reason="test")
    enqueue_soak_intent(conn, kind="agentic_swarm_soak", priority=1)
    conn.close()

    spawn = _SpawnRecorder()
    crumbs: list = []
    sup = _make_supervisor(db_path, spawn, crumbs)
    try:
        armed = await sup.evaluate()
        assert armed is True
        assert sup.state == STATE_ARMED
        assert spawn.count == 1
        assert any(et == "supervisor_armed" for et, _ in crumbs)
    finally:
        await sup.stop()


async def test_no_arm_when_healthy_or_empty(db_path):
    """The intent gate is real: no arm if the queue is empty OR DW is HEALTHY."""
    reset_default_broker()
    spawn = _SpawnRecorder()
    sup = _make_supervisor(db_path, spawn, [])

    # Empty queue + DEGRADED → no arm.
    conn = sqlite3.connect(db_path)
    mark_degraded(conn, "doubleword", reason="test")
    conn.close()
    assert await sup.evaluate() is False
    assert spawn.count == 0

    # Pending + HEALTHY → no arm (nothing to watch for).
    conn = sqlite3.connect(db_path)
    enqueue_soak_intent(conn, priority=1)
    mark_healthy(conn, "doubleword", reason="test")
    conn.close()
    assert await sup.evaluate() is False
    assert spawn.count == 0
    assert sup.state == STATE_DORMANT


async def test_watchdog_restarts_on_sigkill(db_path, monkeypatch):
    """(2) SIGKILL the Sentinel while armed → watchdog auto-restarts (backoff)."""
    monkeypatch.setenv("JARVIS_SUPERVISOR_BACKOFF_BASE_S", "0.01")
    monkeypatch.setenv("JARVIS_SUPERVISOR_BACKOFF_CAP_S", "0.05")
    reset_default_broker()
    conn = sqlite3.connect(db_path)
    mark_degraded(conn, "doubleword", reason="test")
    enqueue_soak_intent(conn, priority=1)
    conn.close()

    spawn = _SpawnRecorder()
    crumbs: list = []
    sup = _make_supervisor(db_path, spawn, crumbs)
    try:
        await sup.evaluate()
        assert spawn.count == 1
        await _wait_for(lambda: sup._watchdog_task is not None)

        # Kill the Sentinel unexpectedly while DW is STILL degraded + queue pending.
        spawn.procs[0].crash(rc=-9)

        # The watchdog must self-heal: a second spawn appears.
        await _wait_for(lambda: spawn.count == 2)
        assert sup.state == STATE_ARMED, "supervisor stays armed across a self-heal"
        assert any(et == "sentinel_restarted" for et, _ in crumbs)
        # And the restart breadcrumb records the backoff attempt.
        restart = next(p for et, p in crumbs if et == "sentinel_restarted")
        assert restart["attempt"] == 1 and restart["backoff_s"] >= 0.0
    finally:
        await sup.stop()


async def test_expected_healthy_exit_does_not_respawn(db_path, monkeypatch):
    """A clean exit AFTER the HEALTHY handoff is the expected terminus, NOT a
    crash — the watchdog must not respawn (else it fights its own recovery)."""
    monkeypatch.setenv("JARVIS_SUPERVISOR_BACKOFF_BASE_S", "0.01")
    reset_default_broker()
    conn = sqlite3.connect(db_path)
    mark_degraded(conn, "doubleword", reason="test")
    enqueue_soak_intent(conn, priority=1)
    conn.close()

    spawn = _SpawnRecorder()
    sup = _make_supervisor(db_path, spawn, [])
    try:
        await sup.evaluate()
        await _wait_for(lambda: sup._watchdog_task is not None)
        # Sentinel writes HEALTHY then self-exits cleanly (rc=0).
        conn = sqlite3.connect(db_path)
        mark_healthy(conn, "doubleword", reason="recovered")
        conn.close()
        spawn.procs[0].crash(rc=0)
        # Give the watchdog time — it must NOT respawn.
        await asyncio.sleep(0.2)
        assert spawn.count == 1, "clean HEALTHY handoff must not trigger a respawn"
    finally:
        await sup.stop()


async def test_queue_clear_disarms_via_broker(db_path, monkeypatch):
    """(3) clearing the queue + a broker completion event → SIGTERM + DISARM."""
    monkeypatch.setenv("JARVIS_SUPERVISOR_BACKOFF_BASE_S", "0.01")
    reset_default_broker()
    conn = sqlite3.connect(db_path)
    mark_degraded(conn, "doubleword", reason="test")
    enqueue_soak_intent(conn, kind="agentic_swarm_soak", priority=1)
    conn.close()

    spawn = _SpawnRecorder()
    crumbs: list = []
    sup = _make_supervisor(db_path, spawn, crumbs)
    try:
        await sup.evaluate()
        assert sup.state == STATE_ARMED
        proc = spawn.procs[0]
        await _wait_for(lambda: sup._completion_task is not None)

        # The soak cleared the queue; a terminal event now flows on the broker.
        conn = sqlite3.connect(db_path)
        clear_soak_intent(conn, kind="agentic_swarm_soak")
        conn.close()
        get_default_broker().publish(
            EVENT_TYPE_OPERATION_TERMINAL, "op-x", {"op_id": "op-x", "state": "COMPLETE"},
        )

        await _wait_for(lambda: sup.state == STATE_DORMANT)
        assert signal.SIGTERM in proc.signals, "Sentinel received a clean SIGTERM"
        assert any(et == "supervisor_disarmed" for et, _ in crumbs)
    finally:
        await sup.stop()
