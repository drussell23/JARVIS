"""Model output reaches an attached cockpit.

`StreamRenderer` shows tokens arriving as the model thinks — the difference
between watching work happen and reading a report of it. It draws with Rich
`Live`, an animated in-place widget, so it needs a real TTY and CANNOT be
mirrored: `Live` repaints by moving the cursor, and replaying those escapes on
a remote surface corrupts it rather than animating it.

The local widget is therefore untouched, and a SECOND consumer of the same
token feed emits committed text frames. Two renderings of one stream; neither
a degraded copy of the other.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test.stream_mirror import (
    StreamMirror,
    fan_out_tokens,
    stream_mirror_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def _mirror(frames: List[str], t: Any = None) -> StreamMirror:
    clock = t if t is not None else (lambda: 0.0)
    return StreamMirror(frames.append, clock=clock)


# --------------------------------------------------------------------------
# 1. tokens become frames, not a flood
# --------------------------------------------------------------------------

def test_tokens_are_batched_into_frames() -> None:
    """One bridge write per token would put thousands of frames on the socket
    for a single reply, and the client would parse JSON instead of drawing."""
    frames: List[str] = []
    m = _mirror(frames)
    for _ in range(200):
        m.on_token("word ")
    m.end()
    assert frames, "nothing was emitted"
    assert len(frames) < 30, f"{len(frames)} frames for 200 tokens — a flood"


def test_the_whole_text_survives_batching() -> None:
    """Batching must not lose prose."""
    frames: List[str] = []
    m = _mirror(frames)
    words = [f"w{i} " for i in range(120)]
    for w in words:
        m.on_token(w)
    m.end()
    joined = "".join(frames)
    assert "w0 " in joined and "w119 " in joined


def test_a_slow_stream_still_flushes_on_TIME() -> None:
    """Char count alone would hold text indefinitely when the model is slow —
    the operator would watch a still screen while tokens trickled in."""
    frames: List[str] = []
    clock = {"t": 0.0}
    m = _mirror(frames, lambda: clock["t"])
    m.on_token("a short sentence that ends here. ")
    clock["t"] = 10.0
    m.on_token("more ")
    assert frames, "a slow stream never flushed"


def test_end_emits_the_remainder_boundary_or_not() -> None:
    """A tail with no clean cut must not be silently swallowed."""
    frames: List[str] = []
    m = _mirror(frames)
    m.on_token("no terminator here")
    m.end()
    assert "".join(frames).strip() == "no terminator here"


def test_end_on_an_empty_stream_emits_nothing() -> None:
    frames: List[str] = []
    _mirror(frames).end()
    assert frames == []


def test_whitespace_only_is_not_a_frame() -> None:
    frames: List[str] = []
    m = _mirror(frames)
    m.on_token("   \n  ")
    m.end()
    assert frames == []


# --------------------------------------------------------------------------
# 2. it cuts where the local renderer cuts
# --------------------------------------------------------------------------

def test_it_uses_the_renderers_own_boundary_rule() -> None:
    """Reusing `find_commit_boundary` means both surfaces break text at the
    same places, so they never disagree about what has been "said" — and
    markdown spanning a boundary is not severed mid-structure."""
    src = (_REPO / "backend/core/ouroboros/battle_test/"
           "stream_mirror.py").read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "find_commit_boundary" in imported


def test_a_long_unbroken_paragraph_is_not_held_hostage() -> None:
    """Holding text for a boundary that never arrives is worse than one
    awkward break."""
    frames: List[str] = []
    m = _mirror(frames)
    m.on_token("x" * 500)
    assert frames, "a boundaryless paragraph was never sent"


# --------------------------------------------------------------------------
# 3. a stalled cockpit cannot grow the daemon
# --------------------------------------------------------------------------

def test_the_buffer_is_bounded() -> None:
    """An attached cockpit that stops reading must never become a memory leak
    in the organism."""
    # flush_chars huge so the char trigger never fires, and a frozen clock so
    # the timer never fires — the only way text genuinely accumulates.
    m = StreamMirror(None, flush_chars=10 ** 9, clock=lambda: 0.0)
    for _ in range(5000):
        m.on_token("0123456789")
    assert m.pending_chars <= 8192
    assert m.chars_dropped > 0


def test_dropping_keeps_the_RECENT_text() -> None:
    """When a cockpit falls behind, the recent text is what the operator is
    waiting for — the same rule as the console spooler."""
    frames: List[str] = []
    m = StreamMirror(frames.append, flush_chars=10 ** 9, clock=lambda: 0.0)
    for i in range(3000):
        m.on_token(f"{i:06d} ")
    m.end()
    assert "2999" in "".join(frames)


@pytest.mark.parametrize("junk", [None, 42, object(), b"bytes"])
def test_a_hostile_token_never_raises(junk: Any) -> None:
    frames: List[str] = []
    _mirror(frames).on_token(junk)


def test_a_failing_sink_cannot_break_the_stream() -> None:
    def _boom(_text: str) -> None:
        raise RuntimeError("cockpit gone")

    m = StreamMirror(_boom, clock=lambda: 0.0)
    for _ in range(50):
        m.on_token("word ")
    m.end()          # must not raise


# --------------------------------------------------------------------------
# 4. the local renderer is untouched
# --------------------------------------------------------------------------

def test_the_local_renderer_still_receives_every_token() -> None:
    """`Live` is correct where it runs. This ADDS a consumer; it does not
    redirect one."""
    seen: List[str] = []

    class _Renderer:
        def on_token(self, text: str) -> None:
            seen.append(text)

        def end(self) -> None:
            seen.append("END")

    frames: List[str] = []
    w = fan_out_tokens(_Renderer(), _mirror(frames))
    w.on_token("a ")
    w.on_token("b ")
    w.end()
    assert seen == ["a ", "b ", "END"]


def test_the_local_renderer_is_fed_FIRST() -> None:
    """The terminal that has always worked must not be starved by a mirror
    fault."""
    seen: List[str] = []

    class _Renderer:
        def on_token(self, text: str) -> None:
            seen.append(text)

        def end(self) -> None: ...

    def _boom(_t: str) -> None:
        raise RuntimeError("bridge down")

    w = fan_out_tokens(_Renderer(), StreamMirror(_boom, clock=lambda: 0.0))
    for _ in range(50):
        w.on_token("word ")
    assert len(seen) == 50


def test_an_inner_fault_cannot_blank_the_cockpit() -> None:
    class _Broken:
        def on_token(self, _t: str) -> None:
            raise RuntimeError("Live crashed")

        def end(self) -> None:
            raise RuntimeError("Live crashed")

    frames: List[str] = []
    w = fan_out_tokens(_Broken(), _mirror(frames))
    w.on_token("still visible remotely. ")
    w.end()
    assert frames


def test_everything_else_passes_through() -> None:
    """`start()`, `notify()`, the counters — reimplementing the renderer's
    surface would silently drop whatever it grows next."""
    class _Renderer:
        def on_token(self, _t: str) -> None: ...
        def end(self) -> None: ...
        def start(self, op_id: str, provider: str = "") -> str:
            return f"started:{op_id}"

        @property
        def token_count(self) -> int:
            return 7

    w = fan_out_tokens(_Renderer(), _mirror([]))
    assert w.start("op-1") == "started:op-1"
    assert w.token_count == 7


def test_a_missing_renderer_is_returned_untouched() -> None:
    assert fan_out_tokens(None, _mirror([])) is None


# --------------------------------------------------------------------------
# 5. wiring
# --------------------------------------------------------------------------

def test_the_switch_defaults_on() -> None:
    assert stream_mirror_enabled() is True


def test_the_switch_leaves_local_streaming_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_STREAM_MIRROR_ENABLED", "0")
    assert stream_mirror_enabled() is False
    src = _SRC.read_text()
    assert "if stream_mirror_enabled():" in src
    assert "register_stream_renderer(self._stream_renderer)" in src


def test_the_fan_out_is_registered_not_a_replacement() -> None:
    """Providers keep calling `get_stream_renderer().on_token` and are never
    told there are now two consumers."""
    src = _SRC.read_text()
    assert "fan_out_tokens(" in src
    assert "register_stream_renderer(self._stream_renderer)" in src


def test_the_mirror_is_resolved_PER_FRAME() -> None:
    """SerpentFlow is constructed before the bridge attaches, so a handle
    captured at build time would be None forever."""
    src = _SRC.read_text()
    assert "lambda text: self._mirror_markup(text)" in src
