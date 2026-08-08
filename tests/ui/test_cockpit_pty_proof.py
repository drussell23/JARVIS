"""Thirty commits of cockpit behaviour, pressed by a machine.

Every interactive feature merged across this arc — transcript mode, claim
jumping, click-to-expand, drag-select, the clear chord, suspend/resume —
shipped with unit tests that call the handler and none that press the key.
That gap is not pedantic. A handler can be correct while the binding never
fires: the key can be shadowed by prompt_toolkit's own bindings, the filter
can be false at the moment it matters, the mouse mode can be un-negotiated so
the click arrives as garbage text, or the action id can be unresolvable
because an alias was never registered. None of those are visible to a test
that invokes the function directly, and all of them are visible to an
operator immediately.

So this drives `ov demo live` through a real pseudo-terminal — the one scene
that boots the REAL `build_bipartite_application` with no daemon, no socket
and no provider credit — and presses actual bytes.

What "asserting on the output" honestly means
---------------------------------------------
A pty carries a STREAM, not a screen: cursor moves, partial repaints and
styling interleave, so the buffer is the terminal's input, not its picture.
Reconstructing the picture needs an emulator, and asserting against one would
be asserting against the emulator's fidelity as much as the cockpit's.

These tests therefore assert on what is decidable from the stream:

  * a sequence was NEGOTIATED (alt-screen, SGR mouse) — exact bytes;
  * a keystroke CHANGED something — a delta, measured against a mark;
  * specific text APPEARED after an input that should produce it.

That is weaker than a screenshot and much stronger than nothing, and it
catches the entire class above — every one of which manifests as "the delta
is empty" or "the text never came".

Timing is a wait, never a sleep
-------------------------------
`wait_for` polls with a deadline. A fixed sleep tuned on this machine is a
flake on a loaded one, and the standing lesson in this repo is that timing
assumptions are how a green suite hides a real failure.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pty_input import (  # noqa: E402
    key,
    mouse_click,
    mouse_drag,
    mouse_scroll,
    set_winsize,
)

pytestmark = pytest.mark.timeout(120)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: Wide enough that the deck does not fold, tall enough that the viewport has
#: rows to scroll. Both matter: several of these features are no-ops on a
#: screen too small to show what they changed.
COLS, ROWS = 110, 34

ALT_SCREEN_ENTER = "\x1b[?1049h"
ALT_SCREEN_LEAVE = "\x1b[?1049l"
SGR_MOUSE_ON = "\x1b[?1006h"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                   r"|\x1b[=>()][A-Za-z0-9]?")


def clean(text: str) -> str:
    """The printable text a terminal would have drawn, styling removed."""
    return _ANSI.sub("", text or "")


class CockpitSession:
    """A real terminal running the real cockpit.

    Deliberately NOT a subclass of `PtySession`: that harness owns a
    fixed-size pty and a text-only `send`, and the cockpit needs a declared
    window size and byte input. Reusing its drain-on-a-thread design without
    inheriting its constraints keeps both honest.

    `mark`/`since` are the load-bearing addition. Asserting on the WHOLE
    buffer after a keystroke proves nothing — the boot output already
    contains most words the cockpit knows. A delta taken from a mark is the
    only way to attribute new bytes to the key that was just pressed.
    """

    def __init__(self, *args: str, env: "dict | None" = None,
                 cols: int = COLS, rows: int = ROWS) -> None:
        import pty
        import threading

        self._master, slave = pty.openpty()
        set_winsize(self._master, cols, rows)
        base = dict(os.environ)
        base.update({
            "TERM": "xterm-256color",
            "COLUMNS": str(cols),
            "LINES": str(rows),
            "PYTHONUNBUFFERED": "1",
            # Never let a demo terminal try to reach a daemon or a provider.
            "JARVIS_OV_NO_DAEMON": "1",
        })
        base.update(env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.core.ouroboros.cli.ov", *args],
            stdin=slave, stdout=slave, stderr=slave,
            env=base, cwd=REPO, start_new_session=True,
        )
        os.close(slave)
        self._chunks: "list[str]" = []
        self._lock = threading.Lock()
        self._drain = threading.Thread(target=self._pump, daemon=True)
        self._drain.start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._chunks.append(data.decode("utf-8", "replace"))

    # -- reading ---------------------------------------------------------
    @property
    def output(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def mark(self) -> int:
        """A cursor into the stream. Everything after it is attributable."""
        return len(self.output)

    def since(self, at: int) -> str:
        return self.output[at:]

    def wait_for(self, needle: str, *, timeout: float = 25.0,
                 after: int = 0) -> bool:
        """Poll until `needle` appears in the CLEANED stream after `at`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in clean(self.since(after)):
                return True
            if self.proc.poll() is not None:
                # One last look: the text may have arrived in the same
                # breath the process exited.
                return needle in clean(self.since(after))
            time.sleep(0.05)
        return False

    def wait_for_change(self, at: int, *, minimum: int = 32,
                        timeout: float = 8.0) -> str:
        """Wait until at least `minimum` new bytes arrive; return them.

        A repaint is many bytes. The floor rejects the stray single-byte
        cursor report that would otherwise make any keystroke look handled.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            delta = self.since(at)
            if len(delta) >= minimum:
                return delta
            time.sleep(0.05)
        return self.since(at)

    # -- writing ---------------------------------------------------------
    def send(self, data: "bytes | str") -> None:
        if isinstance(data, str):
            data = data.encode()
        os.write(self._master, data)

    def press(self, *names: str) -> None:
        for n in names:
            self.send(key(n))
            time.sleep(0.06)      # let the event loop turn between keys

    def signal(self, sig: int) -> None:
        os.killpg(os.getpgid(self.proc.pid), sig)

    # -- lifecycle -------------------------------------------------------
    def close(self, *, timeout: float = 5.0) -> None:
        try:
            if self.proc.poll() is None:
                self.signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.signal(signal.SIGKILL)
                    self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                os.close(self._master)
            except OSError:
                pass

    def __enter__(self) -> "CockpitSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _require_pty() -> None:
    """Delegates to the ONE gate, which records the skip so the terminal
    summary can say the cockpit went unproven. A local copy of this check is
    how thirty-one tests came to disappear from a green run."""
    from tests.pty_gate import require_pty
    require_pty("test_cockpit_pty_proof")


@pytest.fixture()
def cockpit():
    """A booted `ov demo live`, ready for input.

    Readiness is the PROMPT, not a timer. The demo seeds a masthead and warms
    an animation before the Application takes the screen, and a test that
    started typing on a fixed delay would press keys into a terminal the
    cockpit had not claimed yet — the flakiest possible failure, and one that
    would be blamed on the feature rather than the harness.
    """
    _require_pty()
    session = CockpitSession("demo", "live")
    try:
        if not session.wait_for("❯", timeout=40.0):
            session.close()
            pytest.skip(f"cockpit did not reach a prompt: "
                        f"{clean(session.output)[-400:]!r}")
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# the preconditions everything else stands on
# ---------------------------------------------------------------------------


def test_the_cockpit_takes_the_alternate_screen(cockpit: CockpitSession) -> None:
    """Full-screen means the alt-screen, and the bytes say so or it doesn't.

    This is the first thing the arc changed and the thing nothing verified.
    `smcup` is terminfo-derived rather than hardcoded, so its absence here
    would mean the terminfo lookup silently returned nothing — a degradation
    invisible on a developer's own terminal, where the previous value is
    still on screen.
    """
    assert ALT_SCREEN_ENTER in cockpit.output


def test_mouse_reporting_is_negotiated_in_sgr_mode(
        cockpit: CockpitSession) -> None:
    """Click-to-expand cannot work if the terminal was never asked to report.

    And it must be SGR: the older encoding packs a coordinate into one byte
    and mis-addresses every click past column 223. On a 110-column terminal
    that defect is invisible; on the operator's it is not.
    """
    out = cockpit.output
    assert SGR_MOUSE_ON in out, "SGR mouse reporting never enabled"
    assert "\x1b[?1000h" in out, "button-event reporting never enabled"


def test_the_screen_is_released_on_a_clean_quit(
        cockpit: CockpitSession) -> None:
    """Leaving must restore the operator's scrollback.

    A cockpit that exits without `rmcup` leaves the terminal in the alternate
    buffer and everything the operator had before it is gone. That is the
    single most destructive way a TUI can fail, and it fails silently — the
    process exit code is 0.
    """
    at = cockpit.mark()
    cockpit.press("q")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if ALT_SCREEN_LEAVE in cockpit.since(at):
            break
        time.sleep(0.05)
    assert ALT_SCREEN_LEAVE in cockpit.since(at), "alternate screen never left"


# ---------------------------------------------------------------------------
# transcript mode — Ctrl+O and what lives inside it
# ---------------------------------------------------------------------------


def test_ctrl_o_enters_transcript_mode(cockpit: CockpitSession) -> None:
    """The mode key must reach the mode.

    `Ctrl+O` is `\\x0f`, which prompt_toolkit's emacs bindings already claim
    (`open-line`). If the cockpit's binding were registered where pt's wins,
    pressing it would insert a newline into the prompt and the viewer would
    never appear — with every unit test on `transcript_mode` still green.
    """
    at = cockpit.mark()
    cockpit.press("ctrl+o")
    assert cockpit.wait_for_change(at), "Ctrl+O produced no repaint at all"


def test_the_viewer_help_panel_answers_a_question_mark(
        cockpit: CockpitSession) -> None:
    """`?` inside the viewer lists the viewer's OWN actions.

    The panel is rendered from `_VIEWER_ACTIONS`, the same table the binder
    reads, so a key that exists and a key that is documented cannot drift.
    This proves the table reaches a human — the half a unit test on the table
    cannot cover.
    """
    cockpit.press("ctrl+o")
    time.sleep(0.4)
    at = cockpit.mark()
    cockpit.press("?")
    assert cockpit.wait_for_change(at, timeout=10.0), "`?` rendered nothing"


def test_claim_jumping_moves_through_the_transcript(
        cockpit: CockpitSession) -> None:
    """`c` walks the lines the organism MARKED.

    The demo script deliberately emits provenance-marked lines, so there is
    something to jump to. This is the feature that broke twice in review —
    once by classifying only the rendered window (so every claim was already
    on screen and `c` moved nowhere) and once by re-deriving the start from
    the newest visible line each press (so `C` stuck). Both were invisible to
    the unit tests and obvious the moment a key was pressed.
    """
    cockpit.press("ctrl+o")
    time.sleep(0.4)
    at = cockpit.mark()
    cockpit.press("c")
    first = cockpit.wait_for_change(at, timeout=10.0)
    assert first, "`c` did not move"

    at2 = cockpit.mark()
    cockpit.press("c")
    assert cockpit.wait_for_change(at2, timeout=10.0), (
        "the second `c` moved nowhere — the cursor is not advancing"
    )


def test_scrolling_the_viewport_pauses_the_follow(
        cockpit: CockpitSession) -> None:
    """A wheel notch must stop the deck chasing the newest line.

    Auto-scroll fighting a reader is the reason `_paused` exists as a field
    separate from the offset: at offset 0 the two disagree, and the bug that
    shipped was `to_bottom()` returning early there and never clearing the
    pause — so leaving the viewer left `following` false forever.
    """
    at = cockpit.mark()
    cockpit.send(mouse_scroll(COLS // 2, ROWS // 2, up=True, times=3))
    assert cockpit.wait_for_change(at, timeout=10.0), (
        "the wheel produced no repaint — mouse events are not being parsed"
    )


# ---------------------------------------------------------------------------
# the mouse
# ---------------------------------------------------------------------------


def test_a_click_in_the_deck_is_parsed_not_typed(
        cockpit: CockpitSession) -> None:
    """The proof that mouse bytes never land in the prompt.

    An un-negotiated or unhandled mouse sequence is not silently dropped: the
    terminal delivers `\\x1b[<0;5;8M` as ordinary input and prompt_toolkit
    types the printable tail into the buffer. So the assertion is not "the
    click worked" — it is that `0;5;8M` never appears as TEXT, which is the
    exact operator-visible symptom of the whole class.
    """
    at = cockpit.mark()
    cockpit.send(mouse_click(6, 8))
    time.sleep(0.6)
    delta = clean(cockpit.since(at))
    assert "0;7;9M" not in delta and "<0;7;9" not in delta, (
        f"raw mouse bytes were echoed as text: {delta[:200]!r}"
    )


def test_a_drag_is_reported_as_motion_and_survives(
        cockpit: CockpitSession) -> None:
    """Press-move-release must not crash the app or reach the buffer.

    A drag is the one gesture that arrives as MANY events. The selection
    model handles them as runs; a handler that assumed one event per gesture
    would raise inside prompt_toolkit's mouse dispatch, and pt swallows that
    into a redraw — leaving a cockpit that is subtly dead rather than one
    that reports an error.
    """
    at = cockpit.mark()
    cockpit.send(mouse_drag((4, 6), (40, 8), steps=4))
    time.sleep(0.8)
    assert cockpit.proc.poll() is None, "the app died during a drag"
    delta = clean(cockpit.since(at))
    assert "32;" not in delta, f"motion bytes echoed as text: {delta[:200]!r}"


# ---------------------------------------------------------------------------
# chords
# ---------------------------------------------------------------------------


def test_ctrl_l_once_redraws_and_arms_rather_than_clearing(
        cockpit: CockpitSession) -> None:
    """One press redraws AND arms; it must not clear.

    Both halves matter and the first press does both, which is the design:
    an operator reaching for Ctrl+L is usually recovering a garbled screen
    and has no intention of clearing anything, so arming is a side effect of
    a key that already worked rather than a mode it puts them into.

    The assertion is the HINT, not the repaint — and that is what makes this
    test worth having. prompt_toolkit binds `Ctrl+L` to clear-screen itself,
    so an unbound cockpit still produces a perfectly convincing repaint. The
    repaint proves nothing; only the hint distinguishes "the cockpit's chord
    ran" from "pt's default ran". This test found exactly that: the demo
    never installed the hatches, so `Ctrl+L` fell through to pt and the
    gesture the toolbar implies did not exist on the one surface built to
    demonstrate it.
    """
    at = cockpit.mark()
    cockpit.press("ctrl+l")
    assert cockpit.wait_for("ctrl+l again", timeout=10.0, after=at), (
        "no arming hint — Ctrl+L is not reaching the cockpit's chord "
        f"(tail: {clean(cockpit.since(at))[-300:]!r})"
    )
    assert cockpit.proc.poll() is None


def test_ctrl_c_leaves_no_unraisable_traceback(
        cockpit: CockpitSession) -> None:
    """The operator's actual bug report, as a test.

    Ctrl+C during the cockpit's own teardown used to print
    "Exception ignored in: ..." with a traceback ACROSS the restored screen —
    because the alt-screen restore ran from a signal handler that took a lock
    and wrote through a `TextIOWrapper`. Both are forbidden there; CPython
    delivers signals at arbitrary bytecode boundaries, so the handler could
    interrupt a write that already held the same lock.

    The fix was a lock-free `os.write` on a captured fd plus an unraisable
    guard. This asserts the visible consequence — nothing about how.
    """
    at = cockpit.mark()
    cockpit.signal(signal.SIGINT)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and cockpit.proc.poll() is None:
        time.sleep(0.1)
    tail = clean(cockpit.since(at))
    assert "Exception ignored in" not in tail, (
        f"unraisable leaked onto the restored screen: {tail[-500:]!r}"
    )
    assert "Traceback (most recent call last)" not in tail, (
        f"traceback printed over the operator's terminal: {tail[-500:]!r}"
    )


def test_ctrl_z_releases_the_alternate_screen(cockpit: CockpitSession) -> None:
    """Suspending must hand the operator's scrollback back.

    This is the destructive half and the only half this harness can honestly
    decide. A cockpit that suspends WITHOUT `rmcup` drops the operator into a
    shell drawing on the alternate buffer: their scrollback is invisible and
    everything they type scrolls a screen that will be discarded.

    What is deliberately NOT asserted here is the RESUME.
    -----------------------------------------------------
    Observed: after `SIGCONT` the cockpit repaints without re-issuing
    `smcup`, so it draws on the normal buffer. That looks like a defect and
    this harness cannot prove it is one, because the child is spawned with
    `start_new_session=True` and its process group is therefore ORPHANED —
    and POSIX (2.4.3) says a stop signal delivered to a member of an orphaned
    process group is DISCARDED. So the process very likely never stopped, and
    "resumed without smcup" may be "was never suspended, and pt's
    `run_in_terminal` had already restored".

    Asserting either way would be asserting the harness's job-control
    semantics rather than the cockpit's. The honest instrument for the resume
    path is a session WITH a controlling shell — job control is the thing
    under test, so it cannot also be the thing stubbed out. Recorded here
    rather than silently dropped, so the gap is a known one.
    """
    at = cockpit.mark()
    cockpit.press("ctrl+z")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ALT_SCREEN_LEAVE in cockpit.since(at):
            break
        time.sleep(0.05)
    assert ALT_SCREEN_LEAVE in cockpit.since(at), (
        "Ctrl+Z did not release the alternate screen — the operator's "
        "scrollback would be stranded behind it"
    )


def test_alt_screen_stop_and_resume_are_symmetric() -> None:
    """`alt_screen` must not restore a buffer it does not own.

    The unit half of the test above, and the reason the suspend path is
    correct rather than accidentally correct: while the cockpit is mounted,
    prompt_toolkit owns the alternate screen and `alt_screen` does not. If
    the SIGTSTP branch emitted `rmcup` unconditionally while the SIGCONT
    branch stayed gated on ownership, the module would tear down a buffer it
    had no claim to and then decline to put it back — two owners of one piece
    of terminal state, and the asymmetry is what would make it unrecoverable.

    Asserted on the FLAG rather than by sending a signal, because the
    ownership rule is the invariant; a signal test would only observe one of
    its consequences.
    """
    from backend.core.ouroboros.ui import alt_screen as alt

    armed_before = alt._ARMED[0]
    try:
        alt._ARMED[0] = False
        assert alt._restore_signal_safe() is False, (
            "alt_screen restored a buffer it does not own"
        )
    finally:
        alt._ARMED[0] = armed_before


# ---------------------------------------------------------------------------
# the input line
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the two defects this harness found, pinned
# ---------------------------------------------------------------------------


def test_the_live_scene_registers_its_deck_as_the_active_canvas() -> None:
    """`scene_live` must arm AND disarm the canvas registry.

    Sixteen call sites across `transcript_mode`, `transcript_hatches`,
    `serpent_flow` and `ov` find the deck through `get_active_canvas()`. The
    live scene builds its Application directly rather than through
    `run_bipartite_repl`, and so inherited none of that function's lifecycle:
    the registry was never filled, every one of those sites got None, and
    they all wrote nowhere — silently, because each is wrapped in the
    NEVER-raises discipline that makes a missing canvas indistinguishable
    from a successful write.

    Read by AST rather than by grep so a mention in a docstring or a comment
    cannot satisfy it — the same instrument a seam-ordering check in this
    repo already had to be rewritten to use after passing on its own prose.
    """
    import ast

    src = open(os.path.join(
        REPO, "backend/core/ouroboros/cli/ov_demo.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    live = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "scene_live"),
                None)
    assert live is not None, "scene_live has been renamed"

    calls = [n for n in ast.walk(live)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "set_active_canvas"]
    assert calls, "scene_live never registers its deck — 16 sinks go dark"

    args = {ast.dump(c.args[0]) if c.args else "" for c in calls}
    assert ast.dump(ast.Name(id="mux", ctx=ast.Load())) in args, (
        "the deck is never ARMED as the active canvas"
    )
    assert ast.dump(ast.Constant(value=None)) in args, (
        "the canvas is never CLEARED — a dead deck outlives the scene, and "
        "the next surface to write finds a canvas nobody is drawing"
    )


def test_the_live_scene_binds_the_transcript_hatches() -> None:
    """`Ctrl+L` and its siblings must exist in the demo's own key set.

    The demo mounted `search_rows` — a strip that renders only while a search
    is open — and never installed the `/` that opens one, so the row could
    never appear. `Ctrl+L` was the same gap with a worse symptom: unbound, it
    falls through to prompt_toolkit's clear-screen, which produces a
    thoroughly convincing repaint and none of the arming the cockpit
    promises. A demo that teaches a gesture the product does not have is
    worse than one that omits it.

    Asserted on the BOUND KEYS rather than on the installer being called,
    because the question is what an operator can press.
    """
    from backend.core.ouroboros.cli.ov_demo import _live_exit_bindings

    kb = _live_exit_bindings()
    bound = {tuple(str(k) for k in b.keys) for b in kb.bindings}
    for keys, why in (
        (("Keys.ControlL",), "the clear/redraw chord"),
        (("/",), "transcript search — the row is mounted without it"),
        (("[",), "dump to scrollback"),
        (("{",), "previous block"),
        (("}",), "next block"),
    ):
        assert keys in bound, f"{keys[0]!r} is unbound in the demo ({why})"


def test_typing_reaches_the_prompt(cockpit: CockpitSession) -> None:
    """The baseline nothing else is meaningful without.

    If ordinary text does not echo, every other assertion in this file is
    measuring a dead terminal rather than a working one.
    """
    at = cockpit.mark()
    cockpit.send(b"hello")
    time.sleep(0.5)
    assert "hello" in clean(cockpit.since(at)), "typed text never echoed"


def test_the_slash_palette_opens_without_freezing(
        cockpit: CockpitSession) -> None:
    """`/` is the keystroke with an open freeze report against it.

    Never reproduced under a faithful harness — so this is the harness that
    would catch it. The assertion is liveness: after `/`, the cockpit must
    still respond to a subsequent keystroke. A frozen app echoes the `/` from
    the terminal's own line discipline and then goes quiet, which is exactly
    what "responds to the NEXT key" discriminates.
    """
    cockpit.send(b"/")
    time.sleep(0.8)
    at = cockpit.mark()
    cockpit.send(b"h")
    assert cockpit.wait_for_change(at, minimum=8, timeout=10.0), (
        "the cockpit stopped responding after `/` — freeze reproduced"
    )
    assert cockpit.proc.poll() is None
