"""The diff overlay must never cost the cockpit a frame.

`DiffArchive` is an in-memory ring, so fetching `diff_text` is a dict lookup —
the frame-dropping cost is SYNTAX HIGHLIGHTING (Pygments over thousands of diff
lines is hundreds of milliseconds of pure CPU) and any git resolution of a
`review_branch`. Both belong off the loop.

A prompt_toolkit render callable is synchronous, so an overlay that fetched during
render would block the frame whichever thread it used. The split that works is
pure-pull render + out-of-band fill, and
`test_a_large_render_never_stalls_the_event_loop` is the assertion that actually
holds the mandate: it measures loop liveness rather than trusting the shape of
the code.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.battle_test.diff_archive import DiffArchive
from backend.core.ouroboros.battle_test.diff_overlay import (
    DiffOverlayController,
)

_DIFF = "\n".join(
    ["--- a/backend/x.py", "+++ b/backend/x.py", "@@ -1,3 +1,4 @@"]
    + [f"+    added line {i}" for i in range(40)]
)


def _archive(n: int = 1, diff: str = _DIFF) -> DiffArchive:
    archive = DiffArchive()
    for i in range(n):
        archive.add(op_id=f"op-{i}", risk_tier="NOTIFY_APPLY",
                    file_paths=("backend/x.py", "tests/test_x.py"),
                    diff_text=diff, summary=f"candidate {i}")
    return archive


@pytest.fixture(autouse=True)
def _clean_arbiter():
    from backend.core.ouroboros.battle_test import overlay_arbiter as oa
    oa.reset_for_tests()
    yield
    oa.reset_for_tests()


class TestTheFrameBudget:
    """The mandate, measured."""

    @pytest.mark.asyncio
    async def test_a_large_render_never_stalls_the_event_loop(self):
        """A 6000-line diff renders while a 10ms heartbeat keeps ticking.

        Asserts LOOP LIVENESS, not code shape: a future refactor that moved the
        render back onto the loop would keep every other test in this file green
        and fail only this one.
        """
        big = "\n".join(
            ["--- a/x.py", "+++ b/x.py", "@@ -1,5 +1,6 @@"]
            + [f"+    line {i} of a very large candidate diff"
               for i in range(6000)]
        )
        archive = _archive(diff=big)
        controller = DiffOverlayController(archive=archive,
                                          width_fn=lambda: 120)
        stalls = []

        async def heartbeat():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                if now - last > 0.15:
                    stalls.append(round((now - last) * 1000))
                last = now

        beat = asyncio.ensure_future(heartbeat())
        try:
            controller.open("d-1")
            for _ in range(400):
                await asyncio.sleep(0.01)
                if len(controller.rows()) > 8:
                    break
        finally:
            beat.cancel()
        assert len(controller.rows()) > 100, "the diff never rendered"
        assert not stalls, (
            f"the event loop stalled for {stalls}ms — the render is back on the "
            f"loop and the cockpit is dropping frames")

    @pytest.mark.asyncio
    async def test_rows_are_available_immediately_and_never_block(self):
        """`rows()` is what the renderer calls every frame. It must be O(1) and
        must not wait for the payload — the overlay shows a placeholder first."""
        controller = DiffOverlayController(archive=_archive(),
                                           width_fn=lambda: 100)
        controller.open("d-1")
        started = time.perf_counter()
        rows = controller.rows()
        assert (time.perf_counter() - started) < 0.05
        assert rows and "rendering" in rows[0]


class TestEpochsAndRaces:
    """The operator moves faster than a 500ms render."""

    @pytest.mark.asyncio
    async def test_dismissing_mid_render_does_not_resurrect_the_overlay(self):
        """A finishing task must not reopen something already closed — worse
        than a slow overlay, because the operator did not ask for it."""
        controller = DiffOverlayController(archive=_archive(),
                                           width_fn=lambda: 100)
        controller.open("d-1")
        controller.dismiss()
        for _ in range(30):
            await asyncio.sleep(0.01)
        assert controller.is_active() is False
        assert controller.rows() == []

    @pytest.mark.asyncio
    async def test_the_newest_open_wins_a_race(self):
        """Two opens in flight: whichever THREAD finishes first must not decide
        what is on screen. Only the last ref asked for may land."""
        archive = _archive(n=3)
        controller = DiffOverlayController(archive=archive,
                                           width_fn=lambda: 100)
        controller.open("d-1")
        controller.open("d-3")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(controller.rows()) > 8:
                break
        joined = "\n".join(controller.rows())
        assert "d-3" in joined
        assert "d-1 ·" not in joined

    @pytest.mark.asyncio
    async def test_a_stale_result_is_discarded_not_merged(self):
        """Directly at the seam: an absorb carrying a superseded epoch is a
        no-op rather than a partial overwrite."""
        controller = DiffOverlayController(archive=_archive(),
                                           width_fn=lambda: 100)
        controller.open("d-1")
        stale = -1
        controller._absorb(stale, ["GHOST"])
        assert "GHOST" not in "\n".join(controller.rows())


class TestTheOverlayContract:
    def test_it_is_active_from_the_instant_it_opens(self):
        """NOT "once rows exist". The overlay is up while it loads, so `Escape`
        closes it during a slow render instead of falling through to rewind."""
        controller = DiffOverlayController(archive=_archive())
        assert controller.is_active() is False
        controller.open("d-1")
        assert controller.is_active() is True

    def test_dismiss_clears_everything(self):
        controller = DiffOverlayController(archive=_archive())
        controller.open("d-1")
        controller.dismiss()
        assert controller.is_active() is False
        assert controller.rows() == []

    def test_no_ref_opens_the_most_recent(self):
        """The overwhelmingly common intent — and it saves reading a ref off the
        screen only to type it back."""
        controller = DiffOverlayController(archive=_archive(n=3),
                                           width_fn=lambda: 100)
        assert controller.open() is True
        assert "d-3" in "\n".join(controller.rows())

    def test_a_bare_number_resolves_to_a_ref(self):
        """`3` for `d-3` is reasonable; the prefix is display grammar. Tried only
        AFTER the literal, so a future numeric ref shape is never shadowed."""
        controller = DiffOverlayController(archive=_archive(n=3),
                                           width_fn=lambda: 100)
        controller.open("2")
        assert "d-2" in "\n".join(controller.rows())

    def test_an_unknown_ref_says_so_and_lists_what_exists(self):
        """The archive is a ring, so a ref read minutes ago may be evicted. "No
        such diff" alone invites doubt about typing; the live refs answer the
        real question."""
        controller = DiffOverlayController(archive=_archive(n=2),
                                           width_fn=lambda: 100)
        controller.open("d-999")
        joined = "\n".join(controller.rows())
        assert "no diff archived" in joined
        assert "d-1" in joined and "d-2" in joined

    def test_an_empty_archive_explains_itself(self):
        controller = DiffOverlayController(archive=DiffArchive())
        controller.open()
        assert "archive is empty" in "\n".join(controller.rows())

    def test_a_diff_with_no_text_is_reported_not_blank(self):
        archive = DiffArchive()
        archive.add(op_id="op", risk_tier="NOTIFY_APPLY",
                    file_paths=("x.py",), diff_text="", summary="empty")
        controller = DiffOverlayController(archive=archive,
                                           width_fn=lambda: 100)
        controller.open("d-1")
        assert "no diff text" in "\n".join(controller.rows())

    def test_the_header_carries_the_outcome(self):
        """The question an operator opening an ARCHIVED diff is asking — "did
        this land?" — and the one thing a transient gate preview cannot tell
        them."""
        controller = DiffOverlayController(archive=_archive(),
                                           width_fn=lambda: 100)
        controller.open("d-1")
        joined = "\n".join(controller.rows())
        assert "apply" in joined and "verify" in joined

    def test_a_broken_archive_never_raises(self):
        """It runs on the operator's critical path; a lookup that explodes must
        degrade, not crash the cockpit."""
        class _Broken:
            def lookup(self, _ref):
                raise RuntimeError("archive corrupt")

            def list_recent(self):
                raise RuntimeError("archive corrupt")

            def all_refs(self):
                raise RuntimeError("archive corrupt")

        controller = DiffOverlayController(archive=_Broken())
        assert controller.open("d-1") is True
        assert controller.rows()

    def test_it_works_with_no_event_loop_at_all(self):
        """Headless — a test, CI, a transcript compose — has no frame budget to
        protect and no loop to schedule on. Rendering inline there is the correct
        answer for that context, not a fallback."""
        controller = DiffOverlayController(archive=_archive(),
                                           width_fn=lambda: 100)
        controller.open("d-1")
        rows = controller.rows()
        assert len(rows) > 8, "no inline render happened without a loop"
        assert "added line 39" in "\n".join(rows)


class TestArbiterIntegration:
    def test_registering_makes_escape_contextually_eager(self):
        """The overlay declares itself; the arbiter never learns this class
        exists. That is what lets `Escape` become eager for it without an edit
        to the keymap."""
        from backend.core.ouroboros.battle_test import overlay_arbiter as oa
        controller = DiffOverlayController(archive=_archive())
        assert controller.register() is True
        assert oa.overlay_active() is False
        controller.open("d-1")
        assert oa.overlay_active() is True

    def test_escape_dismisses_it_through_the_arbiter(self):
        from backend.core.ouroboros.battle_test import overlay_arbiter as oa
        controller = DiffOverlayController(archive=_archive())
        controller.register()
        controller.open("d-1")
        assert oa.dismiss_top() == "diff_preview"
        assert controller.is_active() is False

    def test_a_panic_outranks_the_diff(self):
        """A crash outranks a preview the operator opened for themselves, so the
        first Escape closes the panic."""
        from backend.core.ouroboros.battle_test import overlay_arbiter as oa
        controller = DiffOverlayController(archive=_archive())
        controller.register()
        controller.open("d-1")
        oa.register_overlay("panic", z=oa.Z_PANIC, is_active=lambda: True,
                            dismiss=lambda: None)
        top = oa.top_overlay()
        assert top is not None and top.name == "panic"


class TestTheFloatMount:
    """Inherits the panic overlay's structural pattern — see
    `bipartite_layout`'s hoisted float host."""

    def _floats(self, app):
        from prompt_toolkit.layout import walk
        for node in walk(app.layout.container):
            if getattr(node, "floats", None):
                return node.floats
        return []

    def _label(self, float_):
        from prompt_toolkit.formatted_text import to_formatted_text
        from prompt_toolkit.layout import walk
        from prompt_toolkit.layout.controls import FormattedTextControl
        for node in walk(float_.content):
            control = getattr(node, "content", None)
            if not isinstance(control, FormattedTextControl):
                continue
            try:
                value = (control.text() if callable(control.text)
                         else control.text)
                text = "".join(f[1] for f in to_formatted_text(value))
            except Exception:  # noqa: BLE001
                continue
            if "DIFF-SENTINEL" in text:
                return "DIFF"
            if "PANIC-SENTINEL" in text:
                return "PANIC"
        return "?"

    def _app(self, **kwargs):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
        return build_bipartite_application(
            BipartiteLayout(), on_accept=lambda _t: None, **kwargs)

    def test_the_diff_float_is_actually_mounted(self):
        """It was NOT, on the first attempt: the block appended under an
        `isinstance(root, FloatContainer)` guard that was False, because the
        panic block that created the container ran after it. Mounted nothing and
        reported nothing."""
        app = self._app(diff_rows=lambda: ["DIFF-SENTINEL"])
        assert [self._label(f) for f in self._floats(app)] == ["DIFF"]

    def test_the_panic_draws_on_top_of_the_diff(self):
        """Floats draw in list order, so the LAST is on top. This must agree
        with `overlay_arbiter`'s Z constants, or Escape would dismiss something
        other than what the operator is looking at."""
        app = self._app(diff_rows=lambda: ["DIFF-SENTINEL"],
                        panic_rows=lambda: ["PANIC-SENTINEL"])
        assert [self._label(f) for f in self._floats(app)] == ["DIFF", "PANIC"]

    def test_either_overlay_alone_still_mounts(self):
        assert len(self._floats(self._app(
            panic_rows=lambda: ["PANIC-SENTINEL"]))) == 1
        assert len(self._floats(self._app(
            diff_rows=lambda: ["DIFF-SENTINEL"]))) == 1

    def test_no_overlays_means_no_float_host(self):
        """An empty FloatContainer renders identically, but wrapping
        unconditionally changes the container tree for every cockpit that has no
        overlays and buys nothing."""
        assert self._floats(self._app()) == []
