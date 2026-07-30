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


class TestTheWiring:
    """`/expand d-N` → overlay → mounted float, all one controller."""

    @pytest.fixture(autouse=True)
    def _fresh_singleton(self):
        from backend.core.ouroboros.battle_test import diff_overlay as do
        do.reset_default_controller_for_tests()
        yield
        do.reset_default_controller_for_tests()

    def test_the_verb_and_the_mount_share_one_controller(self):
        """The whole wiring, in one assertion. Two instances would give
        `/expand d-N` a surface to fill that nothing renders — the
        wired-but-inert shape, reached from a third direction."""
        from backend.core.ouroboros.battle_test import cockpit_mount as cm
        from backend.core.ouroboros.battle_test import diff_overlay as do
        provider = cm.build_daemon_mount(None)["diff_rows"]
        assert provider is not None
        assert provider.__self__ is do.get_default_controller()

    def test_the_singleton_registers_itself_with_the_arbiter(self):
        """So `Escape` closes it without any surface remembering to ask."""
        from backend.core.ouroboros.battle_test import diff_overlay as do
        from backend.core.ouroboros.battle_test import overlay_arbiter as oa
        controller = do.get_default_controller()
        assert oa.overlay_active() is False
        controller.open()      # empty archive still opens (explains itself)
        assert oa.overlay_active() is True
        assert oa.dismiss_top() == "diff_preview"

    def test_the_singleton_reads_the_process_archive(self):
        """Bound to `get_default_archive()` rather than taking one, so the
        controller and the archive cannot drift apart."""
        from backend.core.ouroboros.battle_test.diff_archive import (
            get_default_archive,
        )
        from backend.core.ouroboros.battle_test import diff_overlay as do
        entry = get_default_archive().add(
            op_id="wired", risk_tier="NOTIFY_APPLY", file_paths=("x.py",),
            diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+wired\n",
            summary="wired")
        controller = do.get_default_controller()
        controller.open(entry.ref)
        assert entry.ref in "\n".join(controller.rows())

    def test_binding_invalidate_reaches_the_singleton(self):
        """Without it the rows land and nothing redraws until an unrelated
        frame — indistinguishable from the blocking render this avoided."""
        from backend.core.ouroboros.battle_test import diff_overlay as do
        beats = []
        assert do.bind_invalidate(lambda: beats.append(1)) is True
        do.get_default_controller().open()
        assert beats, "opening the overlay did not request a repaint"

    def test_expand_diff_falls_back_to_the_console_with_no_app(self):
        """`_open_diff_overlay` gates on a LIVE Application, not on the
        controller existing — the singleton builds on first touch whether or not
        a cockpit is up, so asking it alone would claim the diff is on screen
        while the operator stares at a console."""
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow as sf

        src = inspect.getsource(sf.SerpentREPL._open_diff_overlay
                                if hasattr(sf, "SerpentREPL")
                                else sf._open_diff_overlay)
        assert "get_app_or_none" in src

    def test_expand_routes_d_refs_through_the_overlay_first(self):
        """Structural, and by AST rather than substring: the docstrings around
        this handler name the overlay in prose to explain the design, and a text
        search matches the explanation instead of the call."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow as sf

        fn = [
            n for n in ast.walk(ast.parse(inspect.getsource(sf)))
            if isinstance(n, ast.FunctionDef) and n.name == "_expand_diff"
        ][0]
        called = {
            (getattr(c.func, "attr", "") or getattr(c.func, "id", ""))
            for c in ast.walk(fn) if isinstance(c, ast.Call)
        }
        assert "_open_diff_overlay" in called, (
            "/expand d-N no longer tries the overlay — it would print a flat, "
            "unhighlighted diff on a cockpit that can draw a real one")

    def test_the_daemon_cockpit_passes_diff_rows(self):
        """The float has to be mounted, not merely available."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow as sf

        calls = [
            n for n in ast.walk(ast.parse(inspect.getsource(sf)))
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", "") or getattr(n.func, "attr", ""))
            == "run_bipartite_repl"
        ]
        assert calls
        passed = {kw.arg for c in calls for kw in c.keywords if kw.arg}
        assert "diff_rows" in passed


class TestOverlaysOcclude:
    """An overlay that does not cover what is beneath it is not an overlay.

    Reproduced from a screenshot: the FATAL traceback had deck bullets (`⏺`, `💭`,
    `●`) running down its left margin. The float was inset at `left=2`, so the
    deck's GLYPH GUTTER — columns 0-2 — stayed visible beside it, and a crash
    report read as interleaved with the transcript underneath.
    """

    def _render(self, **kwargs):
        """One real frame, floats included. Call from an ASYNC test.

        The Application's input buffer schedules a history load on the running
        loop, so a synchronous caller dies with "no running event loop" — the same
        constraint the arbiter's KeyProcessor tests hit.

        `write_to_screen` DEFERS float drawing; `Screen.draw_all_floats()` is what
        actually paints them. Without that call the first version of this test
        reported no overlay at all and would have "passed" for a float that never
        rendered.
        """
        from prompt_toolkit.layout.containers import to_container
        from prompt_toolkit.layout.mouse_handlers import MouseHandlers
        from prompt_toolkit.layout.screen import Screen, WritePosition

        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
        mux = BipartiteLayout()
        for i in range(40):
            mux.push_raw(f"CANVAS-LINE-{i}")
        app = build_bipartite_application(
            mux, on_accept=lambda _t: None, **kwargs)
        width, height = 60, 20
        screen = Screen(default_char=None, initial_width=width,
                        initial_height=height)
        to_container(app.layout.container).write_to_screen(
            screen, MouseHandlers(), WritePosition(0, 0, width, height),
            "", False, None)
        screen.draw_all_floats()
        return [
            "".join(screen.data_buffer[y][x].char for x in range(width))
            for y in range(height)
        ]

    @pytest.mark.asyncio
    async def test_the_panic_overlay_hides_the_deck_beneath_it(self):
        rows = self._render(
            panic_rows=lambda: ["FATAL LINE A", "", "", "FATAL LINE D"])
        overlay = rows[1:5]
        assert any("FATAL LINE A" in r for r in overlay)
        for r in overlay:
            assert "CANVAS-" not in r, (
                f"deck bled through the overlay: {r.rstrip()!r} — the float is "
                f"inset again and the glyph gutter shows beside it")

    @pytest.mark.asyncio
    async def test_a_blank_overlay_line_still_occludes(self):
        """The subtle half. Rows 2 and 3 of the payload are EMPTY, and an empty
        line that paints nothing lets the transcript show through the middle of a
        traceback."""
        rows = self._render(
            panic_rows=lambda: ["HEADER", "", "", "FOOTER"])
        assert "CANVAS-" not in rows[2]
        assert "CANVAS-" not in rows[3]

    @pytest.mark.asyncio
    async def test_the_diff_overlay_occludes_too(self):
        """Same contract, same helpers — it would be pointless to fix one."""
        rows = self._render(diff_rows=lambda: ["DIFF HEADER", "", "+ added"])
        for r in rows[1:4]:
            assert "CANVAS-" not in r

    def test_both_overlays_share_one_position_contract(self):
        """Two spellings of "where does an overlay sit" is how one of them gets
        inset again."""
        from backend.core.ouroboros.battle_test import bipartite_layout as bl
        assert bl._OVERLAY_FLOAT_POSITION["left"] == 0
        assert bl._OVERLAY_FLOAT_POSITION["right"] == 0

    def test_the_overlay_style_carries_a_background(self):
        """A foreground-only style leaves prompt_toolkit painting the float's
        cells with the DEFAULT background, which on a themed terminal is not the
        deck's — so the overlay reads as text floating on the transcript."""
        from backend.core.ouroboros.battle_test import bipartite_layout as bl
        assert "bg:" in bl._overlay_style("alert")


class TestTheRenderPool:
    @pytest.fixture(autouse=True)
    def _restore_pool(self):
        """These tests retire the pool deliberately, and the broken flag is
        module-global BY DESIGN. Without restoring it, every later test in the
        process silently runs on the thread path — which is exactly how the
        #70283 stall assertion started failing on test ORDER rather than on a
        change to the code it guards."""
        from backend.core.ouroboros.battle_test import diff_overlay as do
        do.reset_render_pool_for_tests()
        yield
        do.reset_render_pool_for_tests()


    """Pygments holds the GIL, so `to_thread` never actually freed the loop.

    #70283 asserted "no stall > 150ms" and passed on an idle machine — that
    threshold is not sensitive enough to see GIL contention, which is why the claim
    survived a merge. The measure that DOES see it is how many times the loop got to
    run while the render was in flight:

        with the pool   131 loop ticks
        thread only      25 loop ticks

    ~5.2x, on a loop kept deliberately busy. Wall time is LONGER with the pool (a
    child pays 155ms to import rich, plus IPC for the payload) and that is the right
    trade: the operator cares that the cockpit stays responsive, not that a
    background render finishes sooner.
    """

    def test_the_worker_is_module_level_and_takes_plain_data(self):
        """`ProcessPoolExecutor` pickles the callable AND its arguments, so a bound
        method on a controller holding a lock, an archive and callbacks cannot cross
        the boundary at all."""
        import pickle

        from backend.core.ouroboros.battle_test import diff_overlay as do
        payload = {"diff_text": "--- a\n+++ b\n@@ -1 +1 @@\n+x = 1\n",
                   "width": 80}
        assert pickle.loads(pickle.dumps(do.highlight_diff_body)) is not None
        assert pickle.loads(pickle.dumps(payload)) == payload

    def test_the_worker_renders_without_any_controller(self):
        from backend.core.ouroboros.battle_test import diff_overlay as do
        rows = do.highlight_diff_body(
            {"diff_text": "--- a\n+++ b\n@@ -1 +1 @@\n+x = 1\n", "width": 80})
        assert rows and any("x = 1" in r for r in rows)

    def test_an_empty_payload_is_reported_not_blank(self):
        from backend.core.ouroboros.battle_test import diff_overlay as do
        assert "no diff text" in "\n".join(
            do.highlight_diff_body({"diff_text": "", "width": 80}))

    def test_a_broken_pool_degrades_to_the_thread_and_still_renders(self):
        """The pool cannot start in a sandbox, in a frozen app, or where
        `multiprocessing` spawn has no importable `__main__` — and its worker can be
        SIGTERM'd mid-render by `graceful_preemption.halt_child_workers`, which
        already reaps this process's children. Every one of those means the child is
        unavailable NOW, and the operator still wants the diff."""
        import asyncio

        from backend.core.ouroboros.battle_test import diff_overlay as do
        from backend.core.ouroboros.battle_test.diff_archive import DiffArchive

        do.shutdown_render_pool()          # marks the pool permanently broken
        assert do._render_pool() is None

        archive = DiffArchive()
        entry = archive.add(op_id="op", risk_tier="NOTIFY_APPLY",
                            file_paths=("x.py",),
                            diff_text="--- a\n+++ b\n@@ -1 +1 @@\n+fallback\n",
                            summary="s")

        async def drive():
            controller = do.DiffOverlayController(archive=archive,
                                                  width_fn=lambda: 80)
            controller.open(entry.ref)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(controller.rows()) > 3:
                    break
            return controller.rows()

        rows = asyncio.run(drive())
        assert any("fallback" in r for r in rows), (
            "the thread fallback did not render — a broken pool must never cost "
            "the operator the diff")

    def test_a_retired_pool_stays_retired(self):
        """A pool whose worker was halted raises on every submit. Retrying per
        render would pay the failure cost forever."""
        from backend.core.ouroboros.battle_test import diff_overlay as do
        do.shutdown_render_pool()
        assert do._render_pool() is None
        assert do._render_pool() is None

    def test_shutdown_is_idempotent(self):
        from backend.core.ouroboros.battle_test import diff_overlay as do
        do.shutdown_render_pool()
        assert do.shutdown_render_pool() is False

    def test_shutdown_is_registered_so_no_child_outlives_the_cockpit(self):
        import inspect

        from backend.core.ouroboros.battle_test import diff_overlay as do
        src = inspect.getsource(do)
        assert "atexit" in src and "shutdown_render_pool" in src, (
            "an unreaped worker keeps the process alive at exit")
