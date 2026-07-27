"""The boot crest fills the terminal and sits in the middle of it — and the
act of booting leaves no debris on the way in or out.

Four defects, all visible in one paste of ``ov`` starting up:

1. The crest capped at 88 columns on a 200-column terminal. 88 was chosen
   when the reference geometry was drawn and never measured against a real
   window, so the emblem occupied under half the available width.

2. Raising that cap alone would make the crest DISAPPEAR on a wide-but-short
   window: sizing was width-only with height as a veto, and a wider crest
   needs proportionally more rows. The two dimensions have to be solved
   together or the fix trades one bad output for a worse one.

3. Every log line came out stamped ``[worker]``. The slash palette primes its
   verb registry by importing each module in the dispatch packages, and one
   of them called ``logging.basicConfig`` at import — configuring the ROOT
   logger for the whole cockpit.

4. Ctrl+C printed ``Error in atexit._run_exitfuncs`` and a traceback.

Written against observable output rather than internals wherever possible:
the recurring failure in this arc has been tests that pass on the inputs
while the rendered screen stays wrong.
"""
from __future__ import annotations

import atexit
import io
import logging
import re
from typing import Tuple

import pytest

from backend.core.ouroboros.ui import crest as C


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("JARVIS_OV_CREST_MIN_COLS", "JARVIS_OV_CREST_MAX_COLS",
                 "JARVIS_OV_CREST_ROW_RESERVE", "JARVIS_OV_CREST_CENTER"):
        monkeypatch.delenv(name, raising=False)
    yield


def _rendered(width: int, height: int) -> Tuple[int, list]:
    """Size a crest for a terminal and return (cols, rendered lines)."""
    frame = C.generate_crest(
        width, height, tier=C.ColorTier.TRUECOLOR, unicode_ok=True,
    )
    text = C.render_crest_auto(frame, C.ColorTier.TRUECOLOR, term_cols=width)
    return frame.cols, str(text).split("\n")


# --------------------------------------------------------------------------
# 1. it grows with the terminal
# --------------------------------------------------------------------------

def test_a_wide_terminal_gets_a_wide_crest() -> None:
    """THE operator report: 200 columns, and the emblem used 88 of them."""
    cols, _ = _rendered(200, 50)
    assert cols > 88, (
        f"crest is {cols} columns on a 200-column terminal — still pinned to "
        f"the old hardcoded ceiling"
    )


def test_the_crest_grows_monotonically_with_width() -> None:
    """A bigger window must never produce a smaller emblem."""
    seen = [_rendered(w, 60)[0] for w in (60, 90, 120, 160, 200)]
    assert seen == sorted(seen), f"non-monotonic sizing: {seen}"


def test_it_never_touches_the_right_edge() -> None:
    """One column of margin — a crest exactly as wide as the terminal wraps
    its last cell and every row sheds a detached artifact."""
    for w in (60, 100, 137, 200):
        cols, _ = _rendered(w, 60)
        assert cols <= w - 1, f"crest {cols} leaves no margin in {w} columns"


# --------------------------------------------------------------------------
# 2. height is solved, not merely vetoed
# --------------------------------------------------------------------------

def test_a_short_wide_terminal_shrinks_instead_of_vanishing() -> None:
    """The regression a width-only cap raise would have introduced."""
    frame = C.generate_crest(
        200, 20, tier=C.ColorTier.TRUECOLOR, unicode_ok=True,
    )
    assert frame.unavailable_reason is None, (
        f"crest vanished on a 200x20 terminal: {frame.unavailable_reason}"
    )
    assert frame.cols < _rendered(200, 60)[0], (
        "a short terminal got the same crest as a tall one — height is not "
        "participating in the fit"
    )


@pytest.mark.parametrize("width,height", [
    (200, 50), (200, 24), (160, 30), (120, 40), (90, 25), (60, 20),
])
def test_the_crest_always_fits_the_rows_it_is_given(
    width: int, height: int,
) -> None:
    """Measured on the RENDERED output, not on the geometry that claims to
    describe it — the two have disagreed before."""
    _cols, lines = _rendered(width, height)
    assert len(lines) <= height, (
        f"crest drew {len(lines)} rows into a {height}-row terminal"
    )


def test_both_renderers_agree_on_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """The halfblock raster and the quadrant path are sized by separate
    functions; a 256-colour terminal takes the raster, so a cap left behind
    in one of them survives every test aimed at the other. That is precisely
    how the 88 outlived the first attempt to remove it."""
    for w, h in ((200, 50), (120, 40), (60, 20)):
        frame = C.generate_crest(
            w, h, tier=C.ColorTier.TRUECOLOR, unicode_ok=True,
        )
        pf = C.generate_crest_pixels(w, h)
        assert pf is not None, f"raster unavailable at {w}x{h}"
        assert pf.cols == frame.cols, (
            f"{w}x{h}: raster {pf.cols} vs quadrant {frame.cols}"
        )


def test_an_impossible_terminal_degrades_rather_than_raising() -> None:
    for w, h in ((10, 4), (200, 2), (0, 0), (-5, -5)):
        frame = C.generate_crest(
            w, h, tier=C.ColorTier.TRUECOLOR, unicode_ok=True,
        )
        assert isinstance(frame, C.CrestFrame)


# --------------------------------------------------------------------------
# 3. it is centred
# --------------------------------------------------------------------------

def test_the_crest_is_centred_in_the_terminal() -> None:
    """Asserted on the drawn glyphs: the blank margin left of the emblem and
    the blank margin right of it must be within a column of each other."""
    width = 200
    cols, lines = _rendered(width, 60)
    inked = [ln for ln in lines if ln.strip()]
    assert inked, "nothing was drawn"
    left = min(len(ln) - len(ln.lstrip(" ")) for ln in inked)
    right = width - max(len(ln.rstrip(" ")) for ln in inked)
    assert abs(left - right) <= 1, (
        f"crest is off-centre: {left} columns of margin on the left, "
        f"{right} on the right"
    )


def test_centering_never_pushes_the_crest_off_the_right_edge() -> None:
    for w in (60, 100, 200):
        _cols, lines = _rendered(w, 60)
        longest = max((len(ln.rstrip(" ")) for ln in lines), default=0)
        assert longest <= w, f"line of {longest} in a {w}-column terminal"


def test_a_crest_wider_than_its_terminal_is_not_negatively_padded() -> None:
    assert C.center_pad(120, 80) == 0


def test_centering_is_defeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Left-aligned output is what every prior consumer expected; an operator
    or a downstream composer must be able to get it back."""
    monkeypatch.setenv("JARVIS_OV_CREST_CENTER", "0")
    assert C.center_pad(60, 200) == 0


# --------------------------------------------------------------------------
# 4. booting leaves no debris
# --------------------------------------------------------------------------

def test_priming_the_palette_does_not_configure_the_root_logger() -> None:
    """THE ``[worker]`` line noise. Importing modules to read their names off
    them must not reconfigure logging for the host process."""
    from backend.core.ouroboros.battle_test.repl_completion import (
        registry_from_dispatch,
    )
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    buf = io.StringIO()
    try:
        import contextlib
        with contextlib.redirect_stderr(buf):
            registry_from_dispatch()
        assert root.handlers == before, (
            f"palette priming installed root handlers: "
            f"{[getattr(h.formatter, '_fmt', None) for h in root.handlers]}"
        )
        assert root.level == level
    finally:
        root.handlers[:] = before
        root.setLevel(level)


def test_the_guard_survives_a_hostile_import() -> None:
    """Fixing the one offending module is not the fix — any module acquires
    the same power by being importable."""
    from backend.core.ouroboros.battle_test.repl_completion import (
        _root_logging_preserved,
    )
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        with _root_logging_preserved():
            logging.basicConfig(format="%(asctime)s [hostile] %(message)s")
            logging.getLogger().setLevel(logging.DEBUG)
        assert root.handlers == before
        assert root.level == level
    finally:
        root.handlers[:] = before
        root.setLevel(level)


def test_the_worker_still_configures_itself_when_run_as_a_subprocess() -> None:
    """The format was moved, not deleted — the ``python3 -m`` entry point
    still needs it, and stderr must stay the stream because the protocol
    owns stdout."""
    import inspect

    from backend.core.ouroboros.governance import isolated_agent_worker as w

    src = inspect.getsource(w._configure_worker_logging)
    assert "[worker]" in src and "sys.stderr" in src
    whole = inspect.getsource(w)
    body = whole.split('if __name__ == "__main__":')[-1]
    assert "_configure_worker_logging()" in body, (
        "logging moved off import but was never called from the entry point"
    )
    assert not re.search(r"^logging\.basicConfig", whole, re.M), (
        "a module-level basicConfig is back — importing this module will "
        "reconfigure the root logger of whatever process does it"
    )


def test_an_exit_handler_cannot_print_a_traceback_on_ctrl_c() -> None:
    """``KeyboardInterrupt`` is a BaseException, so the ``except Exception``
    already sitting in these handlers never caught it."""
    from backend.core.ouroboros.governance.exit_guard import run_guarded

    ran = []

    def blocking_handler() -> None:
        ran.append("started")
        raise KeyboardInterrupt()

    run_guarded(blocking_handler)          # must not raise
    assert ran == ["started"]


@pytest.mark.parametrize("exc", [
    KeyboardInterrupt, SystemExit, RuntimeError, MemoryError,
])
def test_nothing_escapes_a_guarded_handler(exc) -> None:
    from backend.core.ouroboros.governance.exit_guard import run_guarded

    def handler() -> None:
        raise exc()

    run_guarded(handler)


def test_a_guarded_handler_can_be_withdrawn() -> None:
    """The wrapper is returned precisely so ``atexit.unregister`` works — a
    handler that cannot be removed leaks into every test after it."""
    from backend.core.ouroboros.governance.exit_guard import (
        guarded_atexit_register,
    )
    calls = []
    wrapper = guarded_atexit_register(calls.append, "x")
    try:
        wrapper()
        assert calls == ["x"]
    finally:
        atexit.unregister(wrapper)


def test_the_blocking_reaper_is_registered_through_the_guard() -> None:
    """Structural: cascade_terminate WAITS on children, which is what holds
    the shutdown window open long enough to be interrupted."""
    import inspect

    from backend.core.ouroboros.governance import child_reaper

    src = inspect.getsource(child_reaper)
    assert "guarded_atexit_register(cascade_terminate)" in src
    assert "atexit.register(lambda: cascade_terminate())" not in src
