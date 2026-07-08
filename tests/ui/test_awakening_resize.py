"""SIGWINCH proof (spec §9.2): mid-trace resize regenerates the crest at the
new measurement, reveal stays monotonic, cool-down intact. Plus a real
POSIX SIGWINCH delivery -- via ``TIOCSWINSZ`` ioctl -- against a pty-backed
console, which is load-bearing: the child proves it observed the resize
(``regenerations >= 1``) or exits non-zero, so the test goes red without the
feature."""
from __future__ import annotations

import asyncio
import os
import select
import sys
import threading

import pytest
from rich.console import Console

from backend.core.ouroboros.ui.awakening import AwakeningConductor
from backend.core.ouroboros.ui import theme
from tests.ui.test_awakening import FakeClock, FakeTimer


class ResizableConsole(Console):
    """Console whose reported size we mutate mid-run."""
    _forced = (80, 30)
    @property
    def size(self):
        from rich.console import ConsoleDimensions
        return ConsoleDimensions(*self._forced)


class ShrinkConsole(Console):
    """Console with a per-instance forced size (no shared class state)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._forced_size = (80, 40)
    @property
    def size(self):
        from rich.console import ConsoleDimensions
        return ConsoleDimensions(*self._forced_size)


@pytest.mark.asyncio
async def test_mid_trace_resize_regenerates_and_stays_monotonic():
    ResizableConsole._forced = (80, 30)               # reset shared class state
    console = ResizableConsole(file=open("/dev/null", "w"),
                               force_terminal=True, color_system="truecolor")
    theme.ensure_theme(console)
    clock, timer = FakeClock(), FakeTimer()
    c = AwakeningConductor(console, timer=timer, clock=clock)
    timer.emit("sensors online", False)

    revealed_counts = []
    orig = c._render_crest_text
    def spy(frame, elapsed, tier):
        revealed = sum(1 for cell in frame.cells if cell.delay_s <= elapsed)
        revealed_counts.append((frame.cols, revealed / max(1, len(frame.cells))))
        return orig(frame, elapsed, tier)
    c._render_crest_text = spy

    task = asyncio.create_task(c.run())
    for i in range(120):
        clock.t += 0.02
        await asyncio.sleep(0.001)
        if i == 40:
            ResizableConsole._forced = (50, 30)     # SIGWINCH effect
    clock.t += 60.0
    await asyncio.wait_for(task, timeout=5.0)

    assert c.regenerations >= 1
    cols_seen = {cols for cols, _ in revealed_counts}
    assert 50 in cols_seen                            # regenerated at new width
    # monotonic reveal FRACTION across the resize boundary (no flash-back)
    fracs = [f for _, f in revealed_counts]
    assert all(b >= a - 1e-9 for a, b in zip(fracs, fracs[1:]))


@pytest.mark.asyncio
async def test_mid_trace_shrink_below_minimum_degrades_to_cooldown():
    """Terminal shrinks below the crest minimum mid-trace: the conductor must
    degrade gracefully (request skip -> cool-down) WITHOUT adopting the
    unavailable frame and WITHOUT crashing. ``regenerations`` stays 0."""
    console = ShrinkConsole(file=open("/dev/null", "w"),
                            force_terminal=True, color_system="truecolor")
    theme.ensure_theme(console)
    clock, timer = FakeClock(), FakeTimer()
    c = AwakeningConductor(console, timer=timer, clock=clock)
    timer.emit("sensors online", False)

    task = asyncio.create_task(c.run())
    for i in range(120):
        clock.t += 0.02
        await asyncio.sleep(0.001)
        if i == 30:
            console._forced_size = (30, 40)          # below crest min (46)
    clock.t += 60.0
    await asyncio.wait_for(task, timeout=5.0)         # completes, never raises

    assert c.regenerations == 0                       # never adopted unavailable
    assert c._skip_requested is True                  # degraded to cool-down


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal test")
def test_real_sigwinch_does_not_crash_animation():
    """Deliver a REAL, kernel-originated SIGWINCH mid-trace by resizing the
    child's controlling pty via ``TIOCSWINSZ`` -- and prove the child
    observed it (load-bearing).

    Why the ioctl (not a bare ``os.kill(pid, SIGWINCH)``): Python's default
    disposition for SIGWINCH is *ignore* with no handler installed, so a
    plain ``kill`` is dropped by the kernel and, crucially, changes nothing
    the child measures -- such a test passes with OR without the feature and
    proves nothing. ``TIOCSWINSZ`` on the pty master both delivers a genuine
    kernel SIGWINCH to the child (session leader on that controlling tty)
    AND actually changes what the child's ``console.size`` reports, finally
    exercising ``_check_resize`` under real signal delivery.

    Load-bearing exit-code contract (child):
      * real crash / hang / missing attribute -> ``os._exit(3)``  (fail)
      * feature present but never fired (``regenerations == 0``) -> ``4``  (fail)
      * feature worked under real SIGWINCH (``regenerations >= 1``) -> ``0``
    Only the real, working feature yields exit 0.

    The child's ``Live`` output must be drained from the pty master side, or
    the kernel tty output buffer fills within a second or two of 60fps frames
    and the child's ``write(2)`` blocks forever -- a blocking OS call is NOT
    an asyncio await point, so ``asyncio.wait_for``'s cancellation can never
    interrupt it. A real terminal emulator drains continuously; here the
    parent runs a tiny reader thread that does the same.
    """
    import fcntl
    import pty
    import struct
    import termios
    import time as _t

    pid, master_fd = pty.fork()
    if pid == 0:  # child: run a real awakening on the pty and prove the resize
        try:
            # COLUMNS/LINES in the env would short-circuit Rich's live pty
            # measurement -- drop them so console.size reflects TIOCSWINSZ.
            os.environ.pop("COLUMNS", None)
            os.environ.pop("LINES", None)
            # Establish a known-good initial size on our controlling pty
            # (race-free: we own fd 1 before the parent can touch it), so the
            # first frame is available and we take the animated path.
            fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 80, 0, 0))

            import asyncio as aio
            from rich.console import Console as C
            from backend.core.ouroboros.ui.awakening import AwakeningConductor as A
            from backend.core.ouroboros.ui import theme as th
            console = C(force_terminal=True, color_system="truecolor")
            th.ensure_theme(console)
            from tests.ui.test_awakening import FakeTimer as FT
            t = FT()
            c = A(console, timer=t)
            t.emit("sensors online", False)
            # A prior async test in this pytest session may have left a
            # "current" event loop registered on this thread, backed by a
            # kqueue/epoll selector fd -- fork() does NOT preserve those fds
            # (BSD/macOS semantics), so reusing that stale loop crashes.
            # Always build a fresh loop rather than trusting get_event_loop().
            loop = aio.new_event_loop()
            aio.set_event_loop(loop)
            loop.run_until_complete(aio.wait_for(c.run(), timeout=15.0))
            # Load-bearing proof: did the animated loop actually observe the
            # parent's mid-trace resize and regenerate the crest?
            os._exit(0 if c.regenerations >= 1 else 4)
        except BaseException:
            os._exit(3)
    else:
        stop = threading.Event()

        def _drain() -> None:
            while not stop.is_set():
                try:
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                except (OSError, ValueError):
                    return
                if master_fd in ready:
                    try:
                        if not os.read(master_fd, 65536):
                            return
                    except OSError:
                        return

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        def _set_size(cols: int, rows: int) -> None:
            try:
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

        # Toggle between two valid widths (both >= 46 min) across the whole
        # animation window. The child's crest reveal runs ~2.3s real-time; by
        # continuously toggling every 0.25s from 0.4s onward we guarantee at
        # least one genuine mid-trace size change lands inside the tick loop
        # regardless of the child's (variable) import time -- robust, not racy.
        widths = [(60, 40), (80, 40)]
        deadline = _t.time() + 25.0
        next_resize = _t.time() + 0.4
        toggle = 0
        exited = False
        status = None
        while _t.time() < deadline:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                exited = True
                break
            if _t.time() >= next_resize:
                cols, rows = widths[toggle % 2]
                toggle += 1
                _set_size(cols, rows)
                next_resize = _t.time() + 0.25
            _t.sleep(0.02)

        stop.set()
        reader.join(timeout=1.0)

        if not exited:
            os.kill(pid, __import__("signal").SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("child did not exit within the bounded wait (25s)")

        # exit 0 == feature observed a real resize; 3 == crash/hang/missing
        # attribute; 4 == feature present but never fired.
        assert os.WIFEXITED(status), f"child killed by signal: {status}"
        code = os.WEXITSTATUS(status)
        assert code == 0, (
            f"child exit {code} "
            f"({'crash/missing-attr' if code == 3 else 'regenerations==0' if code == 4 else 'unexpected'})"
        )
