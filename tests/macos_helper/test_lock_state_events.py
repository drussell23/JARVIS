"""Screen lock stops being a question asked once a second.

WHAT WAS MEASURED
-----------------
`_system_state_monitoring_loop` slept 1s forever and called
`is_screen_locked()` SYNCHRONOUSLY on the event loop. On a live supervisor:
571 checks in 9.8 minutes, 1,142 log lines (64% of everything the process
said), and the answer never changed once. `screen_lock_detector` frames are
also what the one surviving main-thread StallSampler dump contains -- because
the detector's fallback path shells out to `osascript`.

WHAT THESE PIN
--------------
That the loop reacts to an OS event rather than a timer, that the backstop
survives an OS that never posts (registration proves the transport, never
the producer), that the probe left the loop, and that the delta gate which
was ALREADY correct still only speaks on a transition.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="Darwin notify(3) is macOS-only")

from backend.macos_helper import lock_state_listener as lsl


# ---------------------------------------------------------------------------
# The listener — a descriptor, a thread, and one door back to the loop
# ---------------------------------------------------------------------------

class TestListener:

    def test_it_registers_and_reports_itself_available(self):
        fired = []
        li = lsl.LockStateListener(on_change=lambda: fired.append(1))
        try:
            started = li.start()
            assert started is True
            assert li.available is True
            assert li.stats()["keys"], "registered with zero descriptors"
        finally:
            li.stop()
        assert li.available is False, "stop() left the listener claiming live"

    def test_a_posted_notification_wakes_the_thread(self):
        """The round trip that proves the TRANSPORT. It deliberately does not
        claim macOS posts these keys on lock -- that is the producer, and it
        is why the consumer keeps a backstop."""
        import ctypes, ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("System"), use_errno=True)
        libc.notify_post.restype = ctypes.c_uint32
        libc.notify_post.argtypes = [ctypes.c_char_p]

        woke = threading_event()
        li = lsl.LockStateListener(on_change=woke.set,
                                   keys=("com.apple.screenIsLocked",))
        try:
            assert li.start() is True
            libc.notify_post(b"com.apple.screenIsLocked")
            assert woke.wait(timeout=5.0), "the descriptor never woke"
            assert li.wakeups >= 1
        finally:
            li.stop()

    @pytest.mark.asyncio
    async def test_the_handoff_to_the_loop_is_call_soon_threadsafe(self):
        """A listener thread touching loop state directly is the bug this
        class exists to not have. The callback must arrive ON the loop."""
        loop = asyncio.get_running_loop()
        seen = {}

        def _cb():
            # If this ran on the listener thread, there'd be no running loop.
            seen["loop"] = asyncio.get_event_loop_policy().get_event_loop()
            seen["thread_is_main"] = True

        li = lsl.LockStateListener(on_change=_cb, loop=loop,
                                   keys=("com.apple.screenIsLocked",))
        try:
            assert li.start() is True
            import ctypes, ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("System"))
            libc.notify_post.argtypes = [ctypes.c_char_p]
            libc.notify_post(b"com.apple.screenIsLocked")
            for _ in range(50):
                await asyncio.sleep(0.05)
                if seen:
                    break
            assert seen.get("thread_is_main"), "callback never reached the loop"
        finally:
            li.stop()

    def test_disabled_by_env_means_the_caller_keeps_polling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCK_EVENT_LISTENER_ENABLED", "0")
        li = lsl.LockStateListener(on_change=lambda: None)
        assert li.start() is False
        assert li.available is False

    def test_keys_are_env_replaceable(self, monkeypatch):
        """An OS version that posts something else must not need a code
        change."""
        monkeypatch.setenv("JARVIS_LOCK_NOTIFY_KEYS", "com.example.a,com.example.b")
        assert lsl.notify_keys() == ("com.example.a", "com.example.b")
        monkeypatch.setenv("JARVIS_LOCK_NOTIFY_KEYS", "   ")
        assert lsl.notify_keys() == lsl.DEFAULT_KEYS, "blank must not blind it"

    def test_stop_is_idempotent_and_never_raises(self):
        li = lsl.LockStateListener(on_change=lambda: None)
        li.start()
        li.stop()
        li.stop()          # second stop must not explode on closed fds


def threading_event():
    import threading
    return threading.Event()


# ---------------------------------------------------------------------------
# The monitor — event-primary, adaptive floor, probe off the loop
# ---------------------------------------------------------------------------

@pytest.fixture
def monitor():
    from backend.macos_helper.system_event_monitor import SystemEventMonitor
    m = SystemEventMonitor()
    m._running = True
    return m


class TestTheLoopIsEventPrimary:

    @pytest.mark.asyncio
    async def test_an_event_wakes_it_far_sooner_than_the_backstop(
            self, monitor, monkeypatch):
        """The whole point: a transition is noticed immediately, not on the
        next tick of a timer."""
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MIN_S", "30")
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MAX_S", "30")
        calls = []

        async def _probe():
            calls.append(time.monotonic())
            return False

        monitor._update_screen_lock_status = _probe
        monitor._start_lock_listener = lambda: None

        task = asyncio.create_task(monitor._system_state_monitoring_loop())
        await asyncio.sleep(0.05)
        n_before = len(calls)
        monitor._lock_event.set()               # the OS "said" something moved
        await asyncio.sleep(0.15)
        monitor._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(calls) > n_before, (
            "the event did not wake the loop — it is still timer-driven")

    def test_the_responsive_floor_refuses_a_pathological_interval(
            self, monitor, monkeypatch):
        """An operator must not be able to configure a 1ms poll. The first
        draft of the test above set 0.02s and read the clamped 0.1s back as a
        bug in the loop -- it was the guard doing its job."""
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MIN_S", "0.001")
        assert monitor._lock_backstop_min_s() == 0.1
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_GROWTH", "0.5")
        assert monitor._lock_backstop_growth() >= 1.0, (
            "a growth factor below 1 would SHRINK the backstop forever")

    @pytest.mark.asyncio
    async def test_the_backstop_widens_while_nothing_changes(
            self, monitor, monkeypatch):
        """A machine that never locks must stop paying to ask."""
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MIN_S", "0.1")
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MAX_S", "10")
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_GROWTH", "2")
        stamps = []

        async def _probe():
            stamps.append(time.monotonic())
            return False                        # never any news

        monitor._update_screen_lock_status = _probe
        monitor._start_lock_listener = lambda: None

        task = asyncio.create_task(monitor._system_state_monitoring_loop())
        await asyncio.sleep(1.7)
        monitor._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(stamps) >= 3, "too few samples to see a trend"
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert gaps[-1] > gaps[0] * 1.5, (
            f"backstop never widened: {gaps[0]:.3f}s -> {gaps[-1]:.3f}s")

    @pytest.mark.asyncio
    async def test_a_transition_collapses_the_backstop_to_responsive(
            self, monitor, monkeypatch):
        """Right after a change is when the next one is most likely."""
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MIN_S", "0.1")
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_MAX_S", "10")
        monkeypatch.setenv("JARVIS_LOCK_BACKSTOP_GROWTH", "2")
        stamps, changed = [], {"n": 0}

        async def _probe():
            stamps.append(time.monotonic())
            changed["n"] += 1
            return changed["n"] == 4            # one transition, mid-run

        monitor._update_screen_lock_status = _probe
        monitor._start_lock_listener = lambda: None

        task = asyncio.create_task(monitor._system_state_monitoring_loop())
        await asyncio.sleep(2.2)
        monitor._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert len(gaps) >= 4, f"not enough samples: {len(gaps)}"
        # Probes 1..4 widen (0.1, 0.2, 0.4); probe 4 reports the transition,
        # so the interval BEFORE probe 5 must collapse back to the floor.
        assert gaps[3] < gaps[2], (
            f"a transition did not reset the backstop: {gaps[2]:.3f} -> "
            f"{gaps[3]:.3f}")


class TestTheProbeLeftTheLoop:

    @pytest.mark.asyncio
    async def test_a_slow_lock_probe_does_not_stall_the_loop(self, monitor):
        """`is_screen_locked` falls back to `osascript`. On the loop, that is
        a multi-second stall — the frame the surviving dump caught."""
        import backend.macos_helper.system_event_monitor as sem

        real_import = __import__

        def _slow_is_screen_locked():
            time.sleep(0.4)                     # what osascript costs
            return False

        mod = type(sys)("voice_unlock.objc.server.screen_lock_detector")
        mod.is_screen_locked = _slow_is_screen_locked
        sys.modules["voice_unlock.objc.server.screen_lock_detector"] = mod
        try:
            worst = 0.0
            ticking = True

            async def ticker():
                nonlocal worst
                while ticking:
                    t0 = time.monotonic()
                    await asyncio.sleep(0.01)
                    worst = max(worst, time.monotonic() - t0 - 0.01)

            t = asyncio.create_task(ticker())
            await asyncio.sleep(0.02)
            await monitor._update_screen_lock_status()
            ticking = False
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

            assert worst < 0.2, (
                f"the loop was blocked {worst:.3f}s by the lock probe")
        finally:
            sys.modules.pop("voice_unlock.objc.server.screen_lock_detector", None)


class TestTheDeltaGateAndTheLogging:

    @pytest.mark.asyncio
    async def test_only_a_transition_emits_an_event(self, monitor):
        """This gate was already correct and is deliberately untouched."""
        emitted = []
        monitor._emit_event = lambda e: asyncio.sleep(0, result=emitted.append(e))

        mod = type(sys)("voice_unlock.objc.server.screen_lock_detector")
        state = {"v": False}
        mod.is_screen_locked = lambda: state["v"]
        sys.modules["voice_unlock.objc.server.screen_lock_detector"] = mod
        try:
            assert await monitor._update_screen_lock_status() is False
            assert await monitor._update_screen_lock_status() is False
            assert emitted == [], "an event fired without a transition"

            state["v"] = True
            assert await monitor._update_screen_lock_status() is True
            assert len(emitted) == 1, "the transition did not emit exactly once"

            assert await monitor._update_screen_lock_status() is False
            assert len(emitted) == 1, "a steady state emitted again"
        finally:
            sys.modules.pop("voice_unlock.objc.server.screen_lock_detector", None)

    def test_the_unlocked_fast_path_no_longer_logs_at_INFO(self, caplog):
        """64% of the log was this line saying nothing had changed."""
        from voice_unlock.objc.server import screen_lock_detector as d

        with caplog.at_level(logging.INFO,
                             logger="voice_unlock.objc.server.screen_lock_detector"):
            d._check_cgsession_locked_via_ctypes()
        noisy = [r for r in caplog.records
                 if r.levelno >= logging.INFO and "UNLOCKED" in r.getMessage()]
        assert not noisy, f"still emitting INFO on the no-news path: {noisy}"
