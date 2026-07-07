"""SIGWINCH proof (spec §9.2): mid-trace resize regenerates the crest at the
new measurement, reveal stays monotonic, cool-down intact. Plus a real
POSIX SIGWINCH delivery against a pty-backed console."""
from __future__ import annotations

import asyncio
import os
import select
import signal
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


@pytest.mark.asyncio
async def test_mid_trace_resize_regenerates_and_stays_monotonic():
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal test")
def test_real_sigwinch_does_not_crash_animation():
    """Deliver a real SIGWINCH while the conductor animates on a pty.

    The child's ``Live`` output must be drained from the pty master side,
    or the kernel tty output buffer fills within a second or two of 60fps
    frames and the child's ``write(2)`` blocks forever -- a blocking OS
    call is NOT an asyncio await point, so ``asyncio.wait_for``'s
    cancellation can never interrupt it. A real terminal emulator drains
    continuously; here the parent runs a tiny reader thread that does the
    same (discarding bytes), and bounds the final wait so a genuine hang
    fails loudly instead of wedging the suite.
    """
    import pty
    pid, fd = pty.fork()
    if pid == 0:  # child: run a short awakening against the pty
        try:
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
            # kqueue/epoll selector fd -- fork() does NOT preserve
            # kqueue/epoll fds (BSD/macOS semantics), so reusing that
            # stale loop in the child crashes. Always build a fresh loop
            # here rather than trusting ``get_event_loop()``.
            loop = aio.new_event_loop()
            aio.set_event_loop(loop)
            loop.run_until_complete(
                aio.wait_for(c.run(), timeout=15.0))
            os._exit(0)
        except BaseException:
            os._exit(3)
    else:
        stop = threading.Event()

        def _drain() -> None:
            while not stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                except (OSError, ValueError):
                    return
                if fd in ready:
                    try:
                        if not os.read(fd, 65536):
                            return
                    except OSError:
                        return

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        import time as _t
        _t.sleep(0.4)
        os.kill(pid, signal.SIGWINCH)                 # the real signal

        deadline = _t.time() + 20.0
        exited = False
        status = None
        while _t.time() < deadline:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                exited = True
                break
            _t.sleep(0.05)

        stop.set()
        reader.join(timeout=1.0)

        if not exited:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("child did not exit within the bounded wait (20s)")

        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
