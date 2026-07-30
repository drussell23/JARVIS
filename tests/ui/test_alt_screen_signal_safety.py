"""The alternate screen's signal path, which runs where almost nothing may.

The defect an operator reported, verbatim::

    Exception ignored in: <function WeakValueDictionary.__init__.<locals>.remove>
    Traceback (most recent call last):
      File ".../weakref.py", line 105, in remove
      File ".../ui/alt_screen.py", line 193, in _handler
        _prev(signum, frame)
    KeyboardInterrupt:

A CPython signal handler is delivered at an ARBITRARY bytecode boundary —
inside a weakref callback, a ``__del__``, a GC finalizer, an ``atexit``
handler. Three things are therefore forbidden on that path, and the handler
did all three. Each is pinned here against the measurement that found it:

  * chaining to a RAISING predecessor (`signal.default_int_handler`) with no
    caller to catch it — the traceback above,
  * taking a lock another thread may hold — 2001 ms in the reproduction,
  * writing through a `TextIOWrapper`, which takes the stream's own lock and
    can deadlock against the very stream it is restoring.

The signals themselves need a real process, so the delivery tests fork one.
"""
from __future__ import annotations

import gc
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from backend.core.ouroboros.governance import exit_guard as G
from backend.core.ouroboros.ui import alt_screen as A

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    """Never let a test write a control sequence at the real terminal."""
    fd = os.open(os.devnull, os.O_WRONLY)
    saved = (A._DEPTH, A._ARMED[0], A._SIGNAL_FD[0], A._IN_HANDLER[0],
             A._SUSPENDED[0], signal.getsignal(signal.SIGINT))
    A._SIGNAL_FD[0] = fd
    A._ARMED[0] = False
    A._IN_HANDLER[0] = False
    A._SUSPENDED[0] = False
    yield
    A._DEPTH, A._ARMED[0], A._SIGNAL_FD[0] = saved[0], saved[1], saved[2]
    A._IN_HANDLER[0], A._SUSPENDED[0] = saved[3], saved[4]
    try:
        signal.signal(signal.SIGINT, saved[5])
    except (ValueError, TypeError):
        pass
    os.close(fd)
    G.uninstall_unraisable_guard()


# ---------------------------------------------------------------------------
# THE reported defect
# ---------------------------------------------------------------------------


class TestUnraisableGuard:
    def test_the_operators_traceback_is_silenced(self, capfd):
        """Replayed exactly: the chained raise, from inside a finalizer."""
        A._DEPTH = 1
        A._ARMED[0] = True
        A._install_backstops()
        handler = signal.getsignal(signal.SIGINT)
        assert getattr(handler, "_ov_alt_screen_chained", False)

        before = G.suppressed_unraisable_count()

        class _Finalizer:
            def __del__(self):
                A._ARMED[0] = True
                handler(signal.SIGINT, None)

        obj = _Finalizer()
        del obj
        gc.collect()

        assert G.suppressed_unraisable_count() > before
        assert "KeyboardInterrupt" not in capfd.readouterr().err

    def test_a_genuine_finalizer_fault_stays_visible(self):
        """The guard is narrow ON PURPOSE. A blanket hook would swallow the
        unraisable traceback from a third-party ``__del__``, where it is
        frequently the only evidence a fault occurred at all.

        In a CHILD, because pytest's own `unraisableexception` plugin owns
        `sys.unraisablehook` inside a test run and COLLECTS unraisables
        rather than printing them. Neither `capsys` nor `capfd` therefore
        sees the traceback, and a version of this test written with either
        one passes while asserting nothing — which is precisely the shape of
        the defect this file exists to catch. The child has no such plugin,
        so what it prints is what an operator would see.
        """
        script = textwrap.dedent(f"""
            import sys, gc
            sys.path.insert(0, {_REPO!r})
            from backend.core.ouroboros.governance.exit_guard import (
                install_unraisable_guard,
            )
            install_unraisable_guard()

            class _Bad:
                def __del__(self):
                    raise ValueError("a genuine bug in someone's finalizer")

            obj = _Bad()
            del obj
            gc.collect()
        """)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=60,
        )
        assert "a genuine bug" in out.stderr, out.stderr
        assert "Exception ignored" in out.stderr

    def test_an_interrupt_in_a_finalizer_prints_nothing(self):
        """The mirror of the test above, end to end in a real process: the
        operator's own reproduction, with the guard in place."""
        script = textwrap.dedent(f"""
            import sys, gc, signal
            sys.path.insert(0, {_REPO!r})
            from backend.core.ouroboros.governance.exit_guard import (
                install_unraisable_guard, suppressed_unraisable_count,
            )
            install_unraisable_guard()

            class _Interrupted:
                def __del__(self):
                    raise KeyboardInterrupt()

            obj = _Interrupted()
            del obj
            gc.collect()
            print("SUPPRESSED", suppressed_unraisable_count())
        """)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=60,
        )
        assert "SUPPRESSED 1" in out.stdout, out.stdout
        assert out.stderr.strip() == "", (
            f"the operator still sees a traceback: {out.stderr!r}"
        )

    def test_it_forwards_to_whatever_hook_was_already_there(self):
        seen = []
        previous = sys.unraisablehook
        sys.unraisablehook = lambda u: seen.append(u)
        try:
            G.uninstall_unraisable_guard()
            assert G.install_unraisable_guard()

            class _Bad:
                def __del__(self):
                    raise ValueError("forward me")

            obj = _Bad()
            del obj
            gc.collect()
            assert seen, "the previous hook was replaced rather than chained"
        finally:
            sys.unraisablehook = previous

    def test_installing_twice_does_not_chain_twice(self):
        G.uninstall_unraisable_guard()
        G.install_unraisable_guard()
        first = sys.unraisablehook
        G.install_unraisable_guard()
        assert sys.unraisablehook is first

    def test_uninstall_only_unwinds_its_own_hook(self):
        """Something else installed on top of ours must not be silently
        uninstalled by our teardown."""
        G.uninstall_unraisable_guard()
        G.install_unraisable_guard()
        theirs = lambda u: None                              # noqa: E731
        sys.unraisablehook = theirs
        G.uninstall_unraisable_guard()
        assert sys.unraisablehook is theirs
        sys.unraisablehook = sys.__unraisablehook__

    def test_the_kill_switch_works(self, monkeypatch):
        G.uninstall_unraisable_guard()
        monkeypatch.setenv("JARVIS_UNRAISABLE_GUARD_ENABLED", "0")
        assert G.install_unraisable_guard() is False
        assert G.unraisable_guard_installed() is False

    def test_suppression_is_counted_not_merely_silent(self):
        """A guard that hides things and keeps no count is indistinguishable
        from a guard that is not working."""
        assert isinstance(G.suppressed_unraisable_count(), int)


# ---------------------------------------------------------------------------
# The forbidden operations
# ---------------------------------------------------------------------------


class TestSignalSafety:
    def test_the_restore_never_blocks_on_the_lock(self):
        """MEASURED: the locked version blocked 2001 ms behind a worker."""
        A._ARMED[0] = True
        held, release = threading.Event(), threading.Event()

        def _hog():
            with A._LOCK:
                held.set()
                release.wait(2.0)

        threading.Thread(target=_hog, daemon=True).start()
        assert held.wait(1.0)
        started = time.monotonic()
        A._restore_signal_safe()
        blocked = time.monotonic() - started
        release.set()
        assert blocked < 0.05, (
            f"the signal path blocked {blocked * 1000:.0f}ms on a lock "
            "another thread held — that is a hang inside a signal handler"
        )

    def test_the_restore_is_idempotent(self):
        """What buys the lock-free flag: two callers both emit `rmcup`, and
        `rmcup` twice is `rmcup`."""
        A._ARMED[0] = True
        assert A._restore_signal_safe() is True
        assert A._restore_signal_safe() is False

    def test_it_writes_to_a_descriptor_not_a_python_stream(self):
        """`TextIOWrapper.write` takes the stream's internal lock, so a
        signal delivered mid-`print` deadlocks the handler against the very
        stream it is restoring."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(A._emit_signal_safe).lstrip())
        attrs = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "write" in attrs and "os" in inspect.getsource(
            A._emit_signal_safe)
        assert "flush" not in attrs, "that is a Python stream, not an fd"

    def test_partial_writes_are_looped(self):
        """A control sequence delivered half-way is worse than one not
        delivered: the terminal renders the remainder as text."""
        r, w = os.pipe()
        try:
            A._SIGNAL_FD[0] = w
            A._ARMED[0] = True
            A._LEAVE_BYTES[0] = b"\x1b[?1049l"
            calls = []
            real_write = os.write

            def _dribble(fd, data):
                calls.append(bytes(data))
                return real_write(fd, bytes(data)[:1])   # one byte at a time

            A.os.write = _dribble
            try:
                assert A._restore_signal_safe() is True
            finally:
                A.os.write = real_write
            assert len(calls) == len(b"\x1b[?1049l")
            assert os.read(r, 64) == b"\x1b[?1049l"
        finally:
            os.close(r)
            os.close(w)

    def test_a_closed_descriptor_is_survivable(self):
        r, w = os.pipe()
        os.close(w)
        os.close(r)
        A._SIGNAL_FD[0] = w
        A._ARMED[0] = True
        assert A._restore_signal_safe() is False        # no raise

    def test_the_descriptor_is_captured_at_entry(self):
        """`sys.__stdout__` may be closed by the time a handler runs, and
        `fileno()` on a closed stream raises — inside a signal handler,
        during shutdown, on the one path whose job is to leave the terminal
        usable."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(A._emit_signal_safe).lstrip())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "fileno" not in names


# ---------------------------------------------------------------------------
# Terminal capability, asked rather than assumed
# ---------------------------------------------------------------------------


class TestCapability:
    def _probe(self, env: dict) -> str:
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            from backend.core.ouroboros.ui import alt_screen as A
            print(A._resolve_sequences())
        """) % _REPO
        environ = dict(os.environ)
        environ.pop("TERM", None)
        environ.update(env)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            env=environ, timeout=60,
        )
        return out.stdout.strip()

    def test_a_terminal_without_an_alternate_screen_is_refused(self):
        """`TERM=dumb` HAS a description and it says there is no alternate
        screen. Writing xterm's literal there prints it as text into the
        operator's session — and hardcoding the literal is what made that
        indistinguishable from success."""
        assert self._probe({"TERM": "dumb"}) == "False"

    def test_an_unknown_terminal_falls_back_rather_than_refusing(self):
        """Terminfo is missing in exactly the environments that DO support
        the sequence — a stripped container, a CI runner. Refusing there
        would be a regression dressed as correctness."""
        assert self._probe({}) == "True"

    def test_a_real_terminal_resolves_from_terminfo(self):
        assert self._probe({"TERM": "xterm-256color"}) == "True"


# ---------------------------------------------------------------------------
# Real signals, in a real process
# ---------------------------------------------------------------------------


def _child(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a process where the signals are real.

    The preamble is dedented and the body dedented SEPARATELY, then joined at
    column zero. Interpolating first and dedenting after computes the common
    prefix across both, which strips the preamble's indent and leaves the
    body's — an IndentationError in the child and an empty stdout in the
    assertion.
    """
    preamble = textwrap.dedent(f"""
        import os, sys, signal, time
        sys.path.insert(0, {_REPO!r})
        from backend.core.ouroboros.ui import alt_screen as A
        _r, _w = os.pipe()
        A._resolve_sequences()
        A._SIGNAL_FD[0] = _w
        A._DEPTH = 1
        A._ARMED[0] = True
        A._install_backstops()
    """)
    script = preamble + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=60,
    )


@pytest.mark.skipif(not hasattr(signal, "SIGTSTP"), reason="POSIX only")
class TestRealSignals:
    def test_sigint_restores_and_still_raises(self):
        """Restoring the terminal must not swallow the interrupt — the
        operator asked to quit."""
        out = _child("""
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.5)
                print("NO-RAISE")
            except KeyboardInterrupt:
                print("RAISED")
            print("EMITTED", os.read(_r, 64) != b"")
        """)
        assert "RAISED" in out.stdout, out.stderr
        assert "EMITTED True" in out.stdout

    def test_a_stop_signal_hands_the_buffer_back(self):
        """prompt_toolkit wraps its OWN Ctrl+Z, but this module claims the
        buffer for the whole boot, before the cockpit mounts. A `kill -TSTP`
        in that window left the shell drawing its prompt on the alternate
        buffer."""
        out = _child("""
            handler = signal.getsignal(signal.SIGTSTP)
            signal.signal(signal.SIGTSTP, signal.SIG_IGN)
            signal.signal(signal.SIGTSTP, handler)
            handler(signal.SIGTSTP, None)
            print("LEFT", os.read(_r, 64))
            print("SUSPENDED_FLAG", A._SUSPENDED[0])
        """)
        assert "1049l" in out.stdout or "LEFT b'\\x1b" in out.stdout, out.stdout
        assert "SUSPENDED_FLAG True" in out.stdout, out.stdout

    def test_resuming_re_enters_the_buffer_it_gave_back(self):
        out = _child("""
            tstp = signal.getsignal(signal.SIGTSTP)
            cont = signal.getsignal(signal.SIGCONT)
            tstp(signal.SIGTSTP, None)
            os.read(_r, 64)
            cont(signal.SIGCONT, None)
            print("REENTERED", os.read(_r, 64) != b"")
            print("ARMED", A._ARMED[0])
        """)
        assert "REENTERED True" in out.stdout, out.stdout
        assert "ARMED True" in out.stdout, out.stdout

    def test_resume_without_a_prior_stop_does_nothing(self):
        """SIGCONT arrives for reasons that have nothing to do with us. Only
        a stop WE handed the buffer back for may re-enter it."""
        out = _child("""
            cont = signal.getsignal(signal.SIGCONT)
            A._ARMED[0] = False
            cont(signal.SIGCONT, None)
            print("ARMED", A._ARMED[0])
        """)
        assert "ARMED False" in out.stdout, out.stdout

    def test_a_second_interrupt_leaves_rather_than_re_entering(self, tmp_path):
        """An impatient operator gets the process to LEAVE, not a deeper
        stack. The harness's own handler writes a partial summary here, which
        is slow enough to be a real target for a second Ctrl+C.

        The evidence is the ledger plus the exit status, not stdout: leaving
        is exactly what stops the child printing. `returncode == -SIGINT`
        means the second press reached the default disposition — and one line
        in the ledger means the slow predecessor ran once.
        """
        ledger = tmp_path / "entries"
        out = _child(f"""
            LEDGER = {str(ledger)!r}
            def _slow(signum, frame):
                with open(LEDGER, "a") as fh:
                    fh.write("entered\\n")
                    fh.flush()
                os.kill(os.getpid(), signal.SIGINT)   # the impatient second
                time.sleep(0.5)
            signal.signal(signal.SIGINT, _slow)
            A._install_backstops()
            h = signal.getsignal(signal.SIGINT)
            h(signal.SIGINT, None)
            print("STILL-HERE")
        """)
        entries = ledger.read_text().splitlines() if ledger.exists() else []
        assert len(entries) == 1, (
            f"the slow predecessor was re-entered {len(entries)} times"
        )
        assert out.returncode == -signal.SIGINT, (
            f"the second interrupt did not reach the default disposition "
            f"(rc={out.returncode}, stdout={out.stdout!r})"
        )
        assert "STILL-HERE" not in out.stdout

    def test_sig_ign_is_not_overruled(self):
        """A predecessor that asked for this signal to do nothing is not
        overruled just because we want the screen back."""
        out = _child("""
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            A._install_backstops()
            h = signal.getsignal(signal.SIGINT)
            h(signal.SIGINT, None)
            print("SURVIVED")
            print("EMITTED", os.read(_r, 64) != b"")
        """)
        assert "SURVIVED" in out.stdout, out.stderr
        assert "EMITTED True" in out.stdout

    def test_chaining_is_not_doubled_on_reinstall(self):
        out = _child("""
            A._install_backstops()
            A._install_backstops()
            h = signal.getsignal(signal.SIGINT)
            prev = h.__defaults__[0]
            print("DOUBLE", getattr(prev, "_ov_alt_screen_chained", False))
        """)
        assert "DOUBLE False" in out.stdout, out.stdout
