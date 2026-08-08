"""Virtual-PTY harness — prove the TUI autonomously, without a human terminal.

The testing limitation, precisely
---------------------------------
The cockpit is gated on a real terminal in two places:

    bipartite_layout.should_run_bipartite()  -> real_stdout_isatty()
    ov._can_run_split_plane()                -> sys.stdin.isatty()

In a headless run every stream is a pipe, so both gates close, `ov` degrades to
``_legacy_pump_loop``, and the Braille oscilloscope is never CONSTRUCTED — it
lives inside ``render_cockpit_header``'s gutter, which only exists in the
bipartite layout. Nothing about the visualizer was broken; the code path simply
never ran.

The fix supplies a real TTY rather than defeating the guards
------------------------------------------------------------
``pty.openpty()`` returns a genuine master/slave pair from the kernel. Binding
a subprocess's stdio to the slave makes ``isatty()`` return True *for real*, so
the production gates open on their own terms. Nothing is mocked, monkeypatched
or bypassed — which matters, because a test that stubbed
``should_run_bipartite`` would prove the renderer works in a world that does
not exist.

Standard library only: ``pty``, ``os``, ``subprocess``, ``threading``. No
pexpect, no tmux.

Environment note
----------------
``pty.openpty()`` raises ``OSError: out of pty devices`` in some sandboxes
(pty allocation is a restricted syscall). These tests SKIP there rather than
fail — an environment restriction is not a code defect, and a permanently-red
suite trains people to ignore it. They run normally on a developer machine and
in CI runners that permit pty allocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _require_pty():
    """Allocate a pty pair or skip. Returns (master_fd, slave_fd).

    Delegates to the ONE gate in ``tests/pty_gate``. This used to be a second,
    independently written copy of the same decision — and two copies of "may
    this suite run" is how the skip stayed invisible in both."""
    from tests.pty_gate import open_pty
    return open_pty("test_headless_tui_integration")


class PtySession:
    """A subprocess whose stdio is bound to a real pseudo-terminal.

    Output is drained on a background thread from the instant the child starts.
    That ordering is load-bearing: a pty has a finite kernel buffer, so a child
    that writes more than it can hold BLOCKS until someone reads. Draining
    after ``wait()`` deadlocks a chatty TUI — and reading only after closing the
    slave loses everything still buffered (a bug this harness hit while being
    written)."""

    def __init__(self, argv, *, cwd=None, env=None) -> None:
        self._master, self._slave = _require_pty()
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.proc = subprocess.Popen(
            argv,
            stdin=self._slave, stdout=self._slave, stderr=self._slave,
            cwd=str(cwd or _REPO), env=env, close_fds=True,
            start_new_session=True,             # own process group -> killable as one
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                return                          # EIO once every slave fd closes
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)

    # -- interaction ----------------------------------------------------

    def send(self, text: str) -> None:
        """Type into the terminal. A trailing newline is what prompt_toolkit
        sees as Enter."""
        os.write(self._master, text.encode())

    def output(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", "replace")

    def wait_for(self, needle: str, *, timeout: float = 20.0) -> bool:
        """Poll the captured buffer. Polling rather than blocking-read keeps a
        silent child from hanging the suite."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.output():
                return True
            time.sleep(0.05)
        return False

    # -- teardown -------------------------------------------------------

    def close(self, *, timeout: float = 5.0) -> None:
        """Terminate the child and release BOTH descriptors.

        Leaking a pty is not free — the pool is finite per-system, and an
        exhausted pool is exactly the ``out of pty devices`` failure this
        harness skips on. Every fd is closed on every path."""
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:  # noqa: BLE001
            pass
        self._stop.set()
        for fd in (self._slave, self._master):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._reader.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PtySession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _env():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONPATH", str(_REPO))
    return env


# ---------------------------------------------------------------------------
# the foundation: a synthetic TTY really is a TTY
# ---------------------------------------------------------------------------


def test_pty_makes_isatty_true_in_the_child():
    """Everything else rests on this. If a pty did not satisfy isatty(), the
    production gates would stay shut and the harness would prove nothing."""
    with PtySession([
        sys.executable, "-c",
        "import sys;print('IN',sys.stdin.isatty(),'OUT',sys.stdout.isatty(),flush=True)",
    ], env=_env()) as s:
        assert s.wait_for("IN True OUT True", timeout=20), s.output()


def test_headless_pipe_is_not_a_tty_control():
    """Negative control: without the pty the same probe reports False, which is
    exactly the condition that degraded `ov` to legacy mode."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys;print('IN',sys.stdin.isatty(),'OUT',sys.stdout.isatty())"],
        capture_output=True, text=True, timeout=30, cwd=str(_REPO),
    )
    assert "IN False OUT False" in r.stdout


# ---------------------------------------------------------------------------
# (1) the production gates open on their own terms — nothing mocked
# ---------------------------------------------------------------------------


def test_bipartite_gate_opens_under_a_real_pty():
    """(1) `should_run_bipartite()` consults real_stdout_isatty(). Under a pty
    it returns True WITHOUT the guard being patched — the whole point."""
    probe = (
        "from backend.core.ouroboros.battle_test.bipartite_layout import "
        "should_run_bipartite;"
        "print('GATE', should_run_bipartite(), flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("GATE", timeout=60), s.output()
        assert "GATE True" in s.output(), (
            f"cockpit gate stayed shut under a real TTY: {s.output()[-400:]}"
        )


def test_split_plane_gate_opens_under_a_real_pty():
    """The sibling gate in ov.py, checked the same honest way."""
    probe = (
        "from backend.core.ouroboros.cli.ov import _can_run_split_plane;"
        "print('SPLIT', _can_run_split_plane(), flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("SPLIT", timeout=60), s.output()
        assert "SPLIT True" in s.output(), s.output()[-400:]


# ---------------------------------------------------------------------------
# (2) the oscilloscope renders into the PTY buffer
# ---------------------------------------------------------------------------


def test_braille_oscilloscope_renders_into_the_pty_output():
    """(2) The component the operator is meant to SEE, emitted through a real
    terminal via the production header renderer — not a unit-test string
    comparison."""
    probe = (
        "import math;"
        "from backend.core.ouroboros.ui.audio_scope import BrailleScope, AudioPlane;"
        "from backend.core.ouroboros.ui.crest_animator import render_cockpit_header;"
        "sc=BrailleScope(width=20); sc.set_plane(AudioPlane.SYSTEM);"
        "sc.extend([abs(math.sin(i/3.0)) for i in range(40)]);"
        "out=render_cockpit_header(None,['O+V v0.1.0','healthy','~/repo'],100,"
        "right_gutter=lambda: sc.render_rich());"
        "print('HDR_START',flush=True); print(out,flush=True); print('HDR_END',flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("HDR_END", timeout=60), s.output()
        out = s.output()
        braille = [c for c in out if 0x2800 <= ord(c) <= 0x28FF]
        assert braille, f"no Braille glyphs reached the terminal: {out[-500:]}"
        assert len(braille) >= 10, f"only {len(braille)} glyphs rendered"
        assert "O+V" in out, "header body missing"


def test_plane_colour_reaches_the_terminal_as_ansi():
    """Venom-green for Karen must survive the render to a real TTY — a colour
    that only exists in markup is not a colour the operator sees."""
    probe = (
        "from backend.core.ouroboros.ui.audio_scope import BrailleScope, AudioPlane;"
        "from backend.core.ouroboros.ui.crest_animator import render_cockpit_header;"
        "sc=BrailleScope(width=12); sc.set_plane(AudioPlane.SYSTEM);"
        "sc.extend([1.0]*24);"
        "print(render_cockpit_header(None,['O+V'],80,"
        "right_gutter=lambda: sc.render_rich()),flush=True);"
        "print('DONE',flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("DONE", timeout=60), s.output()
        out = s.output()
        assert "\x1b[" in out, "no ANSI emitted — colour never reached the terminal"
        assert "⣿" in out, "full-scale glyph missing"


# ---------------------------------------------------------------------------
# input routing
# ---------------------------------------------------------------------------


def test_typed_input_reaches_the_child_through_the_master():
    """Writing to the master is genuinely 'typing': the child reads it on
    stdin. This is the mechanism a `wake` injection depends on."""
    probe = (
        "import sys;"
        "line=sys.stdin.readline().strip();"
        "print('GOT:'+line, flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        s.send("wake\n")
        assert s.wait_for("GOT:wake", timeout=30), s.output()


def test_wake_verb_routes_to_the_audio_lane_not_chat(tmp_path):
    """The routing decision `wake` triggers, exercised through the REAL router
    rather than asserted about. Runs under a pty so the surrounding module
    behaves as it does in a cockpit."""
    # A class cannot be defined in a semicolon-joined `-c` string (SyntaxError),
    # so the probe goes to a file. Real multi-line probes need real modules.
    probe_file = tmp_path / "probe_route.py"
    probe_file.write_text(
        "from backend.core.ouroboros.cli.ov import _route_operator_line\n"
        "sent = []\n"
        "class C:\n"
        "    def send_audio(self, c): sent.append(('audio', c))\n"
        "    def send_input(self, t): sent.append(('chat', t))\n"
        "r = _route_operator_line(C(), None, 'wake')\n"
        "print('ROUTE', r, sent, flush=True)\n"
    )
    with PtySession([sys.executable, str(probe_file)], env=_env()) as s:
        assert s.wait_for("ROUTE", timeout=60), s.output()
        out = s.output()
        assert "handled" in out, out[-300:]
        assert "audio" in out and "wake" in out, "verb did not take the audio lane"
        assert "chat" not in out, "arming verb leaked into the chat lane"


# ---------------------------------------------------------------------------
# (3) cleanup
# ---------------------------------------------------------------------------


def test_session_closes_descriptors_and_reaps_the_child():
    """(3) A leaked pty is not free — the pool is finite, and exhausting it IS
    the 'out of pty devices' error this suite skips on."""
    s = PtySession([sys.executable, "-c", "import time;time.sleep(30)"], env=_env())
    master, slave, pid = s._master, s._slave, s.proc.pid
    s.close(timeout=5.0)

    assert s.proc.poll() is not None, "child survived close()"
    for fd in (master, slave):
        with pytest.raises(OSError):
            os.fstat(fd)               # closed fds must not stat


def test_close_is_idempotent():
    s = PtySession([sys.executable, "-c", "pass"], env=_env())
    s.close()
    s.close()                          # must not raise on already-closed fds


def test_repeated_sessions_do_not_exhaust_the_pty_pool():
    """Ten sequential sessions prove descriptors are actually returned; a leak
    would surface here as 'out of pty devices' rather than silently later."""
    for _ in range(10):
        with PtySession(
            [sys.executable, "-c", "print('ok',flush=True)"], env=_env(),
        ) as s:
            assert s.wait_for("ok", timeout=30)


# ---------------------------------------------------------------------------
# the invisible-idle regression
# ---------------------------------------------------------------------------


def test_idle_scope_is_visible_in_a_real_terminal():
    """A live cockpit showed NOTHING in the gutter. The wiring was correct all
    along — the scope was rendering 20 x U+2800, the BLANK braille pattern, so
    a silent meter was literally whitespace and looked identical to an
    uninstalled feature.

    Asserted through the production header renderer into a real terminal,
    because that is the only place the bug was observable: every unit test
    compared strings and a string of blanks compares fine."""
    probe = (
        "from backend.core.ouroboros.ui.audio_scope import BrailleScope;"
        "from backend.core.ouroboros.ui.crest_animator import render_cockpit_header;"
        "sc=BrailleScope(width=20);"          # never fed — the idle case
        "out=render_cockpit_header(None,['O+V v0.1.0','healthy','~/repo'],100,"
        "right_gutter=lambda: sc.render_rich());"
        "print('HDR',flush=True); print(out,flush=True); print('END',flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("END", timeout=60), s.output()
        out = s.output()
        blanks = [c for c in out if ord(c) == 0x2800]
        marks = [c for c in out if 0x2801 <= ord(c) <= 0x28FF]
        assert not blanks, "idle scope emitted BLANK braille — invisible again"
        assert len(marks) >= 10, (
            f"idle scope drew no visible baseline (got {len(marks)} glyphs)"
        )


def test_idle_and_active_scopes_are_visually_distinct():
    """The baseline must not be so prominent that a silent meter reads as a
    live one — quiet and loud have to look different at a glance."""
    probe = (
        "from backend.core.ouroboros.ui.audio_scope import BrailleScope;"
        "a=BrailleScope(width=10);"
        "b=BrailleScope(width=10); b.extend([1.0]*20);"
        "print('IDLE:'+a.render(),flush=True);"
        "print('LOUD:'+b.render(),flush=True);"
        "print('END',flush=True)"
    )
    with PtySession([sys.executable, "-c", probe], env=_env()) as s:
        assert s.wait_for("END", timeout=60), s.output()
        out = s.output()
        idle = [l for l in out.splitlines() if l.startswith("IDLE:")][0][5:]
        loud = [l for l in out.splitlines() if l.startswith("LOUD:")][0][5:]
        assert idle != loud, "idle and full-scale render identically"
        assert "⣿" in loud, "full scale missing"
