"""The generation reaches an attached cockpit, a line at a time.

`stream_renderer` skips its Rich `Live` widget whenever a SerpentREPL is
active, because Live "writes via direct cursor manipulation that bypasses
`patch_stdout` and clobbers the input prompt". In `ov` the REPL is ALWAYS
active, so the visible token stream has never rendered there — and enabling
it would corrupt the line the operator is typing on. The widget is not the
answer; it is the thing that does not fit.

What fits is the channel the cockpit already has: a mirrored, line-oriented
deck. These tests pin that the generation arrives there, that untrusted model
text is escaped before it does, and that nothing about the stream itself can
be broken by the mirror failing.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import cockpit_attach as ca
from backend.core.ouroboros.battle_test.stream_renderer import StreamRenderer


class _Bridge:
    def __init__(self) -> None:
        self._clients = {"c": object()}
        self.sent: list = []

    def publish_markup(self, text, session=None):
        self.sent.append(str(text))


@pytest.fixture
def bridge():
    b = _Bridge()
    ca.set_active_bridge(b)
    yield b
    ca.set_active_bridge(None)


def _renderer() -> StreamRenderer:
    r = StreamRenderer.__new__(StreamRenderer)
    r._buffer = ""
    r._mirrored_offset = 0
    r._mirror_opened = False
    r._op_id = "7759-86"
    return r


def _feed(r, *chunks, final=False):
    for c in chunks:
        r._buffer += c
        r._mirror_completed_lines()
    if final:
        r._mirror_completed_lines(final=True)


class TestItReachesTheDeck:
    def test_completed_lines_are_mirrored(self, bridge):
        _feed(_renderer(), "the vision floor raises\n")
        assert bridge.sent == ["⏺ the vision floor raises"]

    def test_the_block_opens_ONCE(self, bridge):
        """One ⏺ opens the block, as assistant prose does in the deck's
        grammar; continuations indent under it."""
        _feed(_renderer(), "first\n", "second\n", "third\n")
        assert bridge.sent[0].startswith("⏺ ")
        assert all(s.startswith("  ") for s in bridge.sent[1:])

    def test_a_partial_line_WAITS_for_its_newline(self, bridge):
        """Emitting a half-written sentence would make the deck stutter
        words rather than deliver lines."""
        _feed(_renderer(), "the vision floor ")
        assert bridge.sent == []

    def test_the_final_tail_is_not_LOST(self, bridge):
        """Content after the last newline would otherwise never be sent, so
        every generation would lose its closing sentence."""
        r = _renderer()
        _feed(r, "line one\n", "no trailing newline", final=True)
        assert bridge.sent[-1].strip() == "no trailing newline"

    def test_a_code_fence_survives(self, bridge):
        """`find_commit_boundary` refuses to split a fence because Rich
        re-parses Markdown in scrollback. The deck draws escaped text and
        does not re-parse, so borrowing that rule would show NOTHING until
        the fence closed — the silence this exists to fix."""
        _feed(_renderer(), "```python\n", "def f():\n", "    raise\n")
        assert any("def f():" in s for s in bridge.sent)


class TestModelTextIsInert:
    def test_markup_in_the_generation_is_ESCAPED(self, bridge):
        """The channel is documented as styled chrome around inert data.
        A model that emits `[bold red]` must not restyle the operator's
        deck."""
        _feed(_renderer(), "consider [bold red]this[/bold red] case\n")
        assert "\\[bold red]" in bridge.sent[0]

    def test_an_enormous_line_is_bounded(self, bridge):
        """A model can emit a 40k-character line; the deck is a ring of
        terminal rows."""
        _feed(_renderer(), "x" * 40000 + "\n")
        assert len(bridge.sent[0]) < 2200
        assert bridge.sent[0].endswith("…")


class TestItNeverBreaksTheStream:
    def test_no_cockpit_attached_sends_nothing_and_does_not_raise(self):
        ca.set_active_bridge(None)
        r = _renderer()
        _feed(r, "some output\n", final=True)
        assert r._mirrored_offset > 0   # it still advanced; nothing hung

    def test_a_raising_bridge_is_swallowed(self):
        class _Broken:
            _clients = {"c": 1}
            def publish_markup(self, *a, **k):
                raise RuntimeError("bridge down")
        ca.set_active_bridge(_Broken())
        try:
            _feed(_renderer(), "output\n", final=True)
        finally:
            ca.set_active_bridge(None)

    def test_the_master_flag_silences_it(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_STREAM_MIRROR_ENABLED", "0")
        _feed(_renderer(), "output\n", final=True)
        assert bridge.sent == []

    def test_the_mirror_owns_its_OWN_cursor(self):
        """`_committed_offset` belongs to the local Live widget's scrollback
        commits. Sharing one cursor would let a local terminal's rendering
        decisions silently truncate a remote deck."""
        import inspect
        src = inspect.getsource(StreamRenderer._mirror_completed_lines)
        assert "_mirrored_offset" in src
        assert "_committed_offset" not in src


class TestItIsWiredToTheDrainLoop:
    def test_the_consumer_mirrors_on_every_batch(self):
        import inspect
        src = inspect.getsource(StreamRenderer._consume)
        assert "_mirror_completed_lines()" in src

    def test_end_of_stream_flushes_the_tail(self):
        import inspect
        src = inspect.getsource(StreamRenderer.end)
        assert "_mirror_completed_lines(final=True)" in src

    def test_it_publishes_through_the_registry_the_presence_check_uses(self):
        """"Is anyone listening" and "send it to them" must not disagree
        about which bridge is live."""
        import inspect
        src = inspect.getsource(ca.publish_markup_global)
        assert "_ACTIVE_BRIDGE" in src and "attached_cockpits" in src
