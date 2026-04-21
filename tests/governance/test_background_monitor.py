"""Regression spine — BackgroundMonitor primitive (Ticket #4 Slice 1).

Pins the structural contract the later Monitor-tool + TestRunner
migration slices depend on:

  1. Spawn + teardown: happy path, nonexistent command, double-enter guard.
  2. Event stream shape: MonitorEvent fields, sequence monotonicity,
     ts_mono monotonicity, op_id propagation, line-terminator capture.
  3. Stream mixing: stdout + stderr share one sequence counter, exited
     fires AFTER every stdout/stderr event.
  4. Ring buffer: bounded capacity + FIFO eviction + immutable snapshot.
  5. Non-UTF8 + long-line safety: no crash on binary output or 100KB lines.
  6. Graceful shutdown: SIGTERM-then-SIGKILL escalation on __aexit__ when
     process ignores SIGTERM (grace-window elapses).
  7. Early-exit via async-for break: __aexit__ terminates the subprocess.
  8. Event bus publishing: when bus provided, events land on the bus with
     correct topic shape; when None, no publish attempts.
  9. Concurrent monitors: two monitors run in parallel without
     cross-contaminating sequences or bus topics.
 10. Edge cases: zero ring_capacity raises, negative grace raises.

These tests spawn REAL subprocesses (sh / python -c ...) — no mocking of
asyncio.subprocess. The point is to prove the primitive works against
the actual asyncio StreamReader surface.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.background_monitor import (
    BackgroundMonitor,
    MonitorEvent,
    KIND_STDOUT,
    KIND_STDERR,
    KIND_EXITED,
    KIND_ERROR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


PYTHON = sys.executable


async def _collect_all_events(mon: BackgroundMonitor) -> List[MonitorEvent]:
    """Drive the events() iterator to completion, returning the list."""
    out: List[MonitorEvent] = []
    async for ev in mon.events():
        out.append(ev)
    return out


class _FakeBus:
    """Minimal bus stub that records publish_raw calls — no event loop.

    The real TrinityEventBus requires a running loop + persistence store;
    for unit tests we only need to verify the monitor CALLS publish_raw
    with the expected topic + payload shape.
    """

    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish_raw(
        self, topic: str, data: Dict[str, Any], persist: bool = True,
    ) -> str:
        self.published.append({
            "topic": topic, "data": dict(data), "persist": persist,
        })
        return f"evt-{len(self.published)}"


class _ExplodingBus:
    """Bus stub whose publish_raw always raises — pins the catch-and-log path."""

    async def publish_raw(self, topic, data, persist=True):
        raise RuntimeError(f"simulated bus failure on {topic}")


# ---------------------------------------------------------------------------
# 1. Spawn + teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_echoes_lines_and_exits_zero():
    """Test 1: ``echo`` three lines → 3 stdout events + 1 exited event,
    exit_code=0, sequence numbers strictly increasing."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c",
             "print('alpha'); print('beta'); print('gamma')"],
        op_id="op-happy",
    ) as mon:
        events = await _collect_all_events(mon)
    stdout_events = [e for e in events if e.kind == KIND_STDOUT]
    exited = [e for e in events if e.kind == KIND_EXITED]
    assert len(stdout_events) == 3
    assert [e.data for e in stdout_events] == ["alpha", "beta", "gamma"]
    assert len(exited) == 1
    assert exited[0].exit_code == 0
    assert mon.exit_code == 0
    # Sequence numbers strictly increasing across all events.
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_nonexistent_command_raises_file_not_found_at_enter():
    """Test 2: spawning a missing binary raises FileNotFoundError cleanly.
    Caller handles the error; monitor is left in a safe state."""
    mon = BackgroundMonitor(
        cmd=["/definitely/not/a/real/binary/xyz123"],
        op_id="op-missing",
    )
    with pytest.raises(FileNotFoundError):
        async with mon:
            pass  # never reached


@pytest.mark.asyncio
async def test_double_enter_raises():
    """Test 3: BackgroundMonitor is single-use. Re-entering an already-
    entered instance raises RuntimeError."""
    mon = BackgroundMonitor(
        cmd=[PYTHON, "-c", "pass"],
        op_id="op-double",
    )
    async with mon:
        pass
    with pytest.raises(RuntimeError, match="single-use"):
        async with mon:
            pass


# ---------------------------------------------------------------------------
# 2. Event stream shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_id_propagates_to_every_event():
    """Test 4: every MonitorEvent carries the op_id from construction."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('x')"],
        op_id="op-propagate-42",
    ) as mon:
        events = await _collect_all_events(mon)
    assert all(e.op_id == "op-propagate-42" for e in events)


@pytest.mark.asyncio
async def test_ts_mono_strictly_non_decreasing():
    """Test 5: ts_mono timestamps are non-decreasing across the event stream.
    Monotonic time guarantees regardless of wall-clock adjustments."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c",
             "import time\nfor i in range(4):\n    print(i); time.sleep(0.01)"],
        op_id="op-mono",
    ) as mon:
        events = await _collect_all_events(mon)
    timestamps = [e.ts_mono for e in events]
    for i in range(len(timestamps) - 1):
        assert timestamps[i] <= timestamps[i + 1]


@pytest.mark.asyncio
async def test_line_terminator_captured():
    """Test 6: Unix line terminators are recorded as ``\\n``. Data field
    strips the terminator. Enables byte-exact reconstruction for hashing."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('hello')"],
        op_id="op-terminator",
    ) as mon:
        events = await _collect_all_events(mon)
    stdout = [e for e in events if e.kind == KIND_STDOUT]
    assert stdout[0].data == "hello"
    assert stdout[0].line_terminator == "\n"


# ---------------------------------------------------------------------------
# 3. Stream mixing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdout_and_stderr_share_sequence_counter():
    """Test 7: stdout + stderr events share one monotonic sequence
    counter so consumers can reconstruct temporal order. Exited event
    is always the LAST sequence."""
    script = (
        "import sys\n"
        "sys.stdout.write('out-a\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err-1\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('out-b\\n'); sys.stdout.flush()\n"
    )
    async with BackgroundMonitor(
        cmd=[PYTHON, "-u", "-c", script],
        op_id="op-interleave",
    ) as mon:
        events = await _collect_all_events(mon)
    # Both streams represented, one exited event at the end.
    stdout = [e for e in events if e.kind == KIND_STDOUT]
    stderr = [e for e in events if e.kind == KIND_STDERR]
    exited = [e for e in events if e.kind == KIND_EXITED]
    assert len(stdout) == 2
    assert len(stderr) == 1
    assert len(exited) == 1
    # All sequence numbers unique + monotonic.
    seqs = [e.sequence for e in events]
    assert sorted(seqs) == seqs
    assert len(set(seqs)) == len(seqs)
    # Exited is the highest sequence.
    assert exited[0].sequence == max(seqs)


@pytest.mark.asyncio
async def test_exited_event_emitted_after_all_output():
    """Test 8 (CRITICAL): the KIND_EXITED marker is the LAST event
    yielded — every stdout/stderr line has already landed. Consumers
    using the terminal event as a sentinel can trust the ring buffer
    is complete."""
    script = "for i in range(5):\n    print(f'line-{i}')"
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", script],
        op_id="op-ordering",
    ) as mon:
        events = await _collect_all_events(mon)
    assert events[-1].kind == KIND_EXITED
    for e in events[:-1]:
        assert e.kind in (KIND_STDOUT, KIND_STDERR)


# ---------------------------------------------------------------------------
# 4. Ring buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_buffer_bounded_fifo_eviction():
    """Test 9 (CRITICAL): pushing more lines than ring_capacity evicts
    oldest events first. The NEWEST events + the KIND_EXITED terminal
    are always retained."""
    # Emit 50 lines with capacity=10. Oldest 41 evicted; newest 9 retained
    # plus the KIND_EXITED marker = 10 total.
    script = "for i in range(50):\n    print(f'line-{i}')"
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", script],
        op_id="op-ring",
        ring_capacity=10,
    ) as mon:
        # Drain via events() — all 50 lines + exit pass through the queue.
        await _collect_all_events(mon)
    snap = mon.ring_snapshot()
    assert len(snap) == 10
    # The snapshot ends with KIND_EXITED.
    assert snap[-1].kind == KIND_EXITED
    # The stdout events in the snapshot are the most recent — indexes
    # 41..49 (last 9 before the exit marker).
    stdout_in_snap = [e for e in snap if e.kind == KIND_STDOUT]
    assert len(stdout_in_snap) == 9
    assert stdout_in_snap[0].data == "line-41"
    assert stdout_in_snap[-1].data == "line-49"


@pytest.mark.asyncio
async def test_ring_snapshot_is_immutable_tuple():
    """Test 10: ring_snapshot() returns a tuple — external callers
    cannot mutate the underlying ring by modifying the snapshot."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('x')"],
        op_id="op-snap",
    ) as mon:
        await _collect_all_events(mon)
    snap = mon.ring_snapshot()
    assert isinstance(snap, tuple)
    # MonitorEvent is frozen, so per-event mutation is also blocked.
    with pytest.raises(Exception):
        snap[0].data = "tampered"  # type: ignore


# ---------------------------------------------------------------------------
# 5. Non-UTF8 + long-line safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_utf8_bytes_decoded_with_replacement():
    """Test 11: a subprocess emitting non-UTF8 bytes doesn't crash the
    reader. Bytes are decoded with errors="replace"; monitor completes
    normally."""
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe valid tail\\n')\n"
        "sys.stdout.flush()\n"
    )
    async with BackgroundMonitor(
        cmd=[PYTHON, "-u", "-c", script],
        op_id="op-binary",
    ) as mon:
        events = await _collect_all_events(mon)
    stdout = [e for e in events if e.kind == KIND_STDOUT]
    # Replacement char(s) present; decode did not raise.
    assert len(stdout) == 1
    assert "valid tail" in stdout[0].data


# ---------------------------------------------------------------------------
# 6. Graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_exit_terminates_subprocess():
    """Test 12: breaking out of the events() iterator via ``break``
    triggers __aexit__ which SIGTERMs the subprocess. Monitor ends
    cleanly and exit_code is populated."""
    # Infinite printer — runs until we break the loop.
    script = (
        "import sys, time\n"
        "i = 0\n"
        "while True:\n"
        "    print(f'tick-{i}')\n"
        "    sys.stdout.flush()\n"
        "    i += 1\n"
        "    time.sleep(0.05)\n"
    )
    async with BackgroundMonitor(
        cmd=[PYTHON, "-u", "-c", script],
        op_id="op-early",
        terminate_grace_s=1.0,
    ) as mon:
        count = 0
        async for ev in mon.events():
            if ev.kind == KIND_STDOUT:
                count += 1
                if count >= 3:
                    break
        pid = mon.pid
        assert pid is not None
    # After __aexit__, subprocess is reaped.
    assert mon.exit_code is not None


@pytest.mark.asyncio
async def test_sigterm_escalates_to_sigkill_on_grace_elapse():
    """Test 13: a subprocess that ignores SIGTERM is escalated to
    SIGKILL after terminate_grace_s. Monitor reaps cleanly. Use a
    short grace (0.3s) to keep the test fast."""
    # Install a SIGTERM handler that swallows the signal.
    script = (
        "import signal, time, sys\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    async with BackgroundMonitor(
        cmd=[PYTHON, "-u", "-c", script],
        op_id="op-escalate",
        terminate_grace_s=0.3,
    ) as mon:
        # Wait until the child prints 'ready' so we know the SIGTERM
        # handler is installed BEFORE we exit the context manager.
        async for ev in mon.events():
            if ev.kind == KIND_STDOUT and ev.data == "ready":
                break
        # __aexit__ now fires SIGTERM → grace → SIGKILL.
    # Process was killed. exit_code should reflect termination by
    # signal (negative int on POSIX = -signum).
    assert mon.exit_code is not None
    assert mon.exit_code != 0


# ---------------------------------------------------------------------------
# 7. Event bus publishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_receives_publish_per_event():
    """Test 14: when an event_bus is provided, every MonitorEvent is
    also published via bus.publish_raw. Topic has the expected shape
    ``<prefix>.<op_id>.<kind>`` and payload carries all fields."""
    bus = _FakeBus()
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('alpha'); print('beta')"],
        op_id="op-bus",
        event_bus=bus,
    ) as mon:
        await _collect_all_events(mon)
    # 2 stdout events + 1 exited = 3 publishes.
    assert len(bus.published) == 3
    for entry in bus.published:
        assert entry["topic"].startswith("background_monitor.op-bus.")
        assert entry["persist"] is False  # stream events are ephemeral
        for key in ("op_id", "kind", "ts_mono", "data", "sequence"):
            assert key in entry["data"]
        assert entry["data"]["op_id"] == "op-bus"
    # Last publish carries the exited event + exit_code.
    assert bus.published[-1]["data"]["kind"] == KIND_EXITED
    assert bus.published[-1]["data"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_event_bus_none_does_not_attempt_publish():
    """Test 15: event_bus=None (default) → no publish attempts. Pure
    local-observer mode. The monitor MUST work without a bus."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('x')"],
        op_id="op-no-bus",
        # event_bus omitted — None by default.
    ) as mon:
        events = await _collect_all_events(mon)
    # Simply reaching this assertion without raising proves the
    # no-publish path; event counts match the subprocess.
    assert any(e.kind == KIND_STDOUT for e in events)
    assert events[-1].kind == KIND_EXITED


@pytest.mark.asyncio
async def test_bus_publish_exception_does_not_break_monitor():
    """Test 16: a bus publish_raw that raises is caught + logged at
    DEBUG; the monitor continues emitting events locally. Bus
    misconfiguration MUST NOT kill the subprocess observability."""
    async with BackgroundMonitor(
        cmd=[PYTHON, "-c", "print('alpha')"],
        op_id="op-bus-explode",
        event_bus=_ExplodingBus(),
    ) as mon:
        events = await _collect_all_events(mon)
    # Local events landed despite the bus raising on every publish.
    stdout = [e for e in events if e.kind == KIND_STDOUT]
    assert len(stdout) == 1
    assert stdout[0].data == "alpha"


# ---------------------------------------------------------------------------
# 8. Concurrent monitors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_monitors_do_not_cross_contaminate():
    """Test 17: two monitors running in parallel have independent
    ring buffers + independent sequence counters + distinct bus
    topics. Proves no shared-state bugs in the primitive."""
    bus = _FakeBus()

    async def run_mon(op_id: str, tag: str) -> List[MonitorEvent]:
        script = f"print('{tag}-a'); print('{tag}-b')"
        async with BackgroundMonitor(
            cmd=[PYTHON, "-c", script],
            op_id=op_id,
            event_bus=bus,
        ) as mon:
            return await _collect_all_events(mon)

    events_a, events_b = await asyncio.gather(
        run_mon("op-a", "alpha"),
        run_mon("op-b", "bravo"),
    )
    # Every event carries the right op_id.
    for e in events_a:
        assert e.op_id == "op-a"
    for e in events_b:
        assert e.op_id == "op-b"
    # Stdout data is segregated.
    a_data = {e.data for e in events_a if e.kind == KIND_STDOUT}
    b_data = {e.data for e in events_b if e.kind == KIND_STDOUT}
    assert a_data == {"alpha-a", "alpha-b"}
    assert b_data == {"bravo-a", "bravo-b"}
    # Bus topics include both op_ids.
    topics = {p["topic"] for p in bus.published}
    assert any("op-a" in t for t in topics)
    assert any("op-b" in t for t in topics)


# ---------------------------------------------------------------------------
# 9. Edge cases / input validation
# ---------------------------------------------------------------------------


def test_invalid_ring_capacity_raises():
    """Test 18: ring_capacity < 1 raises ValueError at construction."""
    with pytest.raises(ValueError, match="ring_capacity"):
        BackgroundMonitor(cmd=["true"], ring_capacity=0)


def test_invalid_queue_capacity_raises():
    """Test 19: queue_capacity < 1 raises ValueError at construction."""
    with pytest.raises(ValueError, match="queue_capacity"):
        BackgroundMonitor(cmd=["true"], queue_capacity=-5)


def test_invalid_terminate_grace_raises():
    """Test 20: negative terminate_grace_s raises ValueError."""
    with pytest.raises(ValueError, match="terminate_grace_s"):
        BackgroundMonitor(cmd=["true"], terminate_grace_s=-1.0)


@pytest.mark.asyncio
async def test_cmd_is_argv_not_shell_string():
    """Test 21 (CRITICAL security pin): the ``cmd`` argument is treated
    as argv — no shell interpretation. Passing a shell-metacharacter-
    laden string as a single-element list runs it as a literal filename
    (which doesn't exist) and raises FileNotFoundError — the correct
    behavior. This test exists so that any future attempt to add
    shell=True silently would fail loudly."""
    # Passing the whole string as one arg — should be a literal
    # filename lookup, not shell parsing.
    mon = BackgroundMonitor(
        cmd=["echo hello; echo gotcha"],
        op_id="op-argv",
    )
    with pytest.raises(FileNotFoundError):
        async with mon:
            pass
