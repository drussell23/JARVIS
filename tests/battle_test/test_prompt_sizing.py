"""The prompt is exactly as tall as what is typed.

An empty prompt rendered as an eight-row black slab under the deck. The cause
is a piece of prompt_toolkit arithmetic that is easy to read backwards:

    height=Dimension(min=1, max=8)

That looks like "one row, grow to eight if needed". It is not. `HSplit` hands
each child its PREFERRED size — which defaults to `min`, so 1 — and then
distributes the LEFTOVER rows by weight. Any child whose `max` exceeds its
preferred is willing to absorb slack, and on a tall terminal there is plenty,
so the prompt took its full eight rows and held them whether or not anything
was typed into them.

Pinning `min == max == preferred` leaves nothing to distribute into.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.input_continuation import (
    max_prompt_rows, prompt_height, prompt_rows,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,rows", [
    ("", 1),
    ("fix the flaky test", 1),
    ("line one\nline two", 2),
    ("a\nb\nc\nd", 4),
])
def test_rows_track_content(text: str, rows: int) -> None:
    assert prompt_rows(text) == rows


def test_an_empty_prompt_is_ONE_row() -> None:
    """The regression. Every other row belongs to the deck."""
    assert prompt_rows("") == 1
    dim = prompt_height(lambda: "")()
    assert (dim.min, dim.max, dim.preferred) == (1, 1, 1)


def test_growth_is_capped() -> None:
    """A pasted stack trace must not swallow the whole screen."""
    assert prompt_rows("\n".join("x" * 3 for _ in range(200))) == max_prompt_rows()


def test_the_cap_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """How much screen a half-written thought deserves is taste and screen
    size, not a constant."""
    monkeypatch.setenv("JARVIS_INPUT_MAX_ROWS", "3")
    assert prompt_rows("a\nb\nc\nd\ne") == 3


def test_the_dimension_is_EXACT_not_a_range() -> None:
    """min == max == preferred is the whole fix: it leaves HSplit nothing to
    inflate."""
    dim = prompt_height(lambda: "a\nb")()
    assert dim.min == dim.max == dim.preferred == 2


def test_a_raising_text_source_still_yields_a_size() -> None:
    """A prompt must always have a height, even mid-teardown."""
    def _boom() -> str:
        raise RuntimeError("buffer gone")

    assert prompt_height(_boom)().preferred == 1


# --------------------------------------------------------------------------
# the arithmetic that caused it, pinned so it cannot come back
# --------------------------------------------------------------------------

def test_a_ranged_dimension_would_absorb_slack() -> None:
    """Documents WHY exactness matters, against the real library rather than
    a belief about it: the ranged form's preferred is 1 while its max is 8,
    and that gap is what HSplit fills."""
    from prompt_toolkit.layout.dimension import Dimension

    ranged = Dimension(min=1, max=8)
    assert ranged.preferred == 1 and ranged.max == 8, "the gap HSplit fills"
    exact = Dimension.exact(1)
    assert exact.max == exact.preferred, "nothing to distribute into"


def test_the_cockpit_prompt_declares_no_ranged_height() -> None:
    src = (_REPO / "backend/core/ouroboros/battle_test/"
           "bipartite_layout.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "TextArea"):
            continue
        for kw in node.keywords:
            assert kw.arg != "height", (
                "the prompt declares a static height again; it must be "
                "content-sized after construction"
            )


# --------------------------------------------------------------------------
# what the terminal actually renders
# --------------------------------------------------------------------------

_DRIVER = """
import os, pty, sys, select, time
def child():
    os.environ["TERM"] = "xterm-256color"
    sys.path.insert(0, "__REPO__")
    from prompt_toolkit.layout.dimension import to_dimension
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, build_bipartite_application)
    mux = BipartiteLayout(width=120, height=40, title="t")
    app = build_bipartite_application(mux, on_accept=lambda t: None)
    found = []
    def walk(c):
        b = getattr(getattr(c, "content", None), "buffer", None)
        if b is not None and getattr(b, "accept_handler", None) is not None:
            found.append(c)
        for attr in ("children", "content", "container", "body"):
            x = getattr(c, attr, None)
            if isinstance(x, list):
                for y in x: walk(y)
            elif x is not None and x is not c: walk(x)
    walk(app.layout.container)
    target = found[0]
    buf = target.content.buffer
    def rows():
        return to_dimension(target.height).preferred
    print("READY=1")
    print("EMPTY=" + str(rows()))
    buf.text = "one line"
    print("ONE=" + str(rows()))
    buf.text = "a\\nb\\nc"
    print("THREE=" + str(rows()))
    sys.stdout.flush()
    os._exit(0)

pid, fd = pty.fork()
if pid == 0:
    child()
out = b""
end = time.time() + 40
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try:
            c = os.read(fd, 65536)
        except OSError:
            break
        if not c:
            break
        out += c
        if b"\\x1b[6n" in c:
            os.write(fd, b"\\x1b[40;1R")
    if os.waitpid(pid, os.WNOHANG)[0]:
        time.sleep(0.2)
        try:
            while True:
                c = os.read(fd, 65536)
                if not c:
                    break
                out += c
        except OSError:
            pass
        break
sys.stdout.write(out.decode("utf-8", "replace"))
"""


@pytest.mark.timeout(120)
def test_the_terminal_renders_one_row_for_an_empty_prompt(
    tmp_path: Path,
) -> None:
    """Under a real PTY, because the slab was a LAYOUT outcome — reading the
    configured value would have reported `preferred=1` and looked correct
    while the screen showed eight rows."""
    driver = tmp_path / "drv.py"
    driver.write_text(_DRIVER.replace("__REPO__", str(_REPO)))
    try:
        proc = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            timeout=90, cwd=str(_REPO),
            env={**os.environ, "PYTHONPATH": str(_REPO)},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty unavailable: {exc}")
    if "READY=1" not in proc.stdout:
        pytest.skip(f"pty child never started: {proc.stdout[-300:]}")

    assert "EMPTY=1" in proc.stdout, "the empty prompt is a slab again"
    assert "ONE=1" in proc.stdout
    assert "THREE=3" in proc.stdout


# --------------------------------------------------------------------------
# mouse capture — the trade it makes
# --------------------------------------------------------------------------

def test_the_cockpit_captures_the_mouse_when_it_owns_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        mouse_enabled,
    )

    monkeypatch.delenv("JARVIS_DISABLE_MOUSE", raising=False)
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    assert mouse_enabled() is True
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "0")
    assert mouse_enabled() is False, (
        "capturing the mouse without a scrollable deck takes the terminal's "
        "own selection away and gives nothing back"
    )


def test_the_mouse_can_be_declined_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capturing the mouse stops the terminal's native click-and-drag
    selection — the most common friction point of a full-screen TUI. An
    operator who selects more than they scroll keeps the rendering and drops
    the capture."""
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        fullscreen_enabled, mouse_enabled,
    )

    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    monkeypatch.setenv("JARVIS_DISABLE_MOUSE", "1")
    assert mouse_enabled() is False
    assert fullscreen_enabled() is True, "rendering is kept either way"
