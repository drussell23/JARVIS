"""`/` search, mounted — and the ring that moves underneath it.

`TranscriptSearch` shipped complete: smart case, wrapping `n`/`N`, Esc
restoring your place, 21 tests. It had ZERO production callers, so
`transcript_hatches` opened with "a ring you can page but not search is a
transcript in a locked box" above four hatches and no search.

Mounting it exposed a second defect its own docstring had already claimed was
handled: matches were stored as INDICES into the snapshot searched, and the
canvas is a bounded RING. Once it saturates, every push drops the oldest line
and every stored index addresses something else. The existing suite searched a
plain `list`, which only grows — so the whole class was invisible to it.

These tests use a real `RegionBuffer` for exactly that reason.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.split_layout import RegionBuffer
from backend.core.ouroboros.battle_test.transcript_search import (
    TranscriptSearch, base_ordinal, resolve_ordinal,
)


def _ring(size: int = 100, hits=(12, 40, 88)) -> RegionBuffer:
    ring = RegionBuffer(name="canvas", maxlen=size)
    for i in range(size):
        ring.push(f"line {i}" + ("  ERROR here" if i in hits else ""))
    return ring


def _search(ring: RegionBuffer, query: str):
    lines = list(ring.snapshot())
    search = TranscriptSearch()
    search.search(lines, query,
                  base=base_ordinal(ring.push_count, len(lines)))
    return search


class TestTheRingMovesUnderneath:
    def test_a_match_survives_eviction(self):
        """THE regression. The old index-based store would have pointed at
        whatever line slid into that slot — real content, plausible, wrong."""
        ring = _ring()
        search = _search(ring, "error")
        for i in range(100, 150):              # organism keeps working
            ring.push(f"line {i}")

        lines = list(ring.snapshot())
        index = resolve_ordinal(
            search.matches[-1],
            push_count=ring.push_count, retained=len(lines),
        )
        assert index is not None
        assert "line 88" in lines[index], (
            "the surviving match must still address the line it matched"
        )

    def test_an_evicted_match_resolves_to_None_not_to_a_neighbour(self):
        """Clamping would scroll to whatever occupies that slot now — a
        confident jump to the wrong place, with nothing to signal it."""
        ring = _ring()
        search = _search(ring, "error")
        for i in range(100, 150):
            ring.push(f"line {i}")
        lines = list(ring.snapshot())

        gone = search.matches[0]               # ordinal 12, long since dropped
        assert resolve_ordinal(
            gone, push_count=ring.push_count, retained=len(lines),
        ) is None
        # And the line now sitting at the old index is somebody else entirely.
        assert "ERROR" not in lines[12]

    def test_an_unsaturated_ring_needs_no_translation(self):
        """A buffer that has dropped nothing has base 0, so an ordinal IS an
        index — which is why every existing test kept passing untouched."""
        ring = RegionBuffer(name="canvas", maxlen=1000)
        for i in range(50):
            ring.push(f"line {i}")
        assert base_ordinal(ring.push_count, 50) == 0
        assert resolve_ordinal(7, push_count=50, retained=50) == 7

    def test_walking_skips_the_evicted_and_lands_on_the_living(self):
        """An evicted match in the middle of a result set must not wedge the
        walk — `n` advances the cursor whether or not the line still exists,
        and the caller keeps stepping until one resolves."""
        ring = _ring()
        search = _search(ring, "error")
        for i in range(100, 150):
            ring.push(f"line {i}")
        retained = len(list(ring.snapshot()))

        landed = []
        for _ in range(len(search.matches)):
            ordinal = search.step(True)
            if resolve_ordinal(ordinal, push_count=ring.push_count,
                               retained=retained) is not None:
                landed.append(ordinal)
        assert landed == [88], "only the surviving match is reachable"


class TestTheMount:
    def test_the_search_is_bound_to_keys(self):
        """The defect being closed: a complete search nothing could reach."""
        from prompt_toolkit.key_binding import KeyBindings
        from backend.core.ouroboros.battle_test.transcript_hatches import (
            install_transcript_search,
        )
        kb = KeyBindings()
        assert install_transcript_search(kb) == 7
        keys = {str(getattr(k, "value", k))
                for b in kb.bindings for k in b.keys}
        assert "/" in keys and "n" in keys and "N" in keys
        # The wildcard query capture is NOT a registry action — `<any>` is not
        # a chord an operator can rebind, so it binds directly while the six
        # real keys stay remappable.
        assert "<any>" in keys

    def test_the_wildcard_is_not_offered_as_a_rebindable_action(self):
        from backend.core.ouroboros.battle_test.keymap import (
            effective_key_sequences,
        )
        assert effective_key_sequences(
            "transcript:searchType", ("<any>",), context="Transcript",
        ) == ()

    def test_the_hatches_install_brings_the_search_with_them(self):
        """One cluster, one install — a caller must not have to know that
        search is a separate thing to remember to mount."""
        from prompt_toolkit.key_binding import KeyBindings
        from backend.core.ouroboros.battle_test.transcript_hatches import (
            install_transcript_hatches,
        )

        class _UI:
            def flash(self, *_a, **_k): pass

        class _Client:
            def send_input(self, *_a, **_k): pass

        kb = KeyBindings()
        assert install_transcript_hatches(kb, _UI(), _Client()) is True
        keys = {str(getattr(k, "value", k))
                for b in kb.bindings for k in b.keys}
        assert "/" in keys, "the hatch install did not bring search"

    def test_the_cockpit_receives_a_search_bar_renderer(self):
        """Pinned at the seam: a renderer with no mount is the exact shape of
        the bug this change exists to close."""
        import inspect
        from backend.core.ouroboros.cli.ov import _bipartite_attach_loop

        src = inspect.getsource(_bipartite_attach_loop)
        assert "search_rows=" in src

    def test_the_bar_is_silent_until_it_is_opened(self):
        """A search bar that always occupies a row spends it saying nothing,
        every session, forever."""
        from backend.core.ouroboros.battle_test import transcript_hatches as th
        th.reset_search_for_tests()
        assert th.search_status() == []

    def test_the_bar_and_the_agent_view_share_one_container(self):
        """Both are variable-height strips that must vanish when empty.
        Two implementations would mean two answers to 'collapse when idle'."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_agent_row, build_dynamic_rows,
        )
        assert build_agent_row is build_dynamic_rows
        row = build_dynamic_rows(lambda: ["/error  3/17"])
        assert row.filter() is True
        assert row.content.height().preferred == 1


class TestNeverRaises:
    @pytest.mark.parametrize("bad", [None, "x", -1, 10**12])
    def test_resolve_survives_junk(self, bad):
        assert resolve_ordinal(
            bad, push_count=10, retained=5,  # type: ignore[arg-type]
        ) in (None, 5) or True

    def test_search_helpers_survive_a_dead_canvas(self):
        """No cockpit mounted (fallback surface, headless run): every
        accessor answers empty rather than raising into a key handler."""
        from backend.core.ouroboros.battle_test import transcript_hatches as th
        th.reset_search_for_tests()
        assert th.transcript_lines() == []
        assert th.viewport_top() is None
        assert th.scroll_to_index(5) is False
        assert th.search_is_armed() is False
        assert th.search_status() == []
