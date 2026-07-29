"""A background death the operator can see, within milliseconds.

Three times in one arc a missing symbol was swallowed by a broad `except`
and the system carried on describing a world that had stopped existing.
The failure is not the `except` blocks — it is that a detached task can die
and nothing tells anyone.
"""
from __future__ import annotations

import asyncio
import gc

import pytest

from backend.core.ouroboros.battle_test import panic_arbiter as pa


@pytest.fixture(autouse=True)
def _clean():
    pa.reset_for_tests()
    yield
    pa.reset_for_tests()


class TestTheMandateEndToEnd:
    @pytest.mark.asyncio
    async def test_a_raising_detached_task_trips_the_arbiter_and_the_UI(self):
        """THE required proof: a mocked detached task raising RuntimeError
        trips the handler, routes FATAL_PANIC through a mocked UDS
        dispatcher, and triggers the UI overlay callback."""
        uds_frames: list = []
        pa.register_sink(uds_frames.append)
        pa.install()

        async def deliberate():
            raise RuntimeError("deliberate background fault")

        pa.spawn_supervised(deliberate(), origin="test.detached")
        await asyncio.sleep(0.05)

        assert uds_frames, "nothing reached the UDS dispatcher"
        frame = uds_frames[0]
        assert frame["type"] == pa.PANIC_KIND
        assert frame["exc_type"] == "RuntimeError"
        assert "deliberate background fault" in frame["message"]
        assert "RuntimeError" in frame["traceback"]

        # …and the UI overlay callback renders it.
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        ui._terminal_size = lambda: (100, 30)   # type: ignore[method-assign]
        ui.on_telemetry({**frame, "kind": pa.PANIC_KIND})
        rows = ui._panic_rows()
        assert rows, "the overlay drew nothing"
        assert "FATAL" in rows[0]
        assert any("RuntimeError" in r for r in rows)


class TestTwoDetectorsBecauseOneIsNotEnough:
    @pytest.mark.asyncio
    async def test_the_loop_handler_alone_MISSES_a_referenced_task(self):
        """The load-bearing nuance. asyncio surfaces an unretrieved task
        exception at GARBAGE COLLECTION; anything holding a reference — a
        task registry, `self._tasks` — defers that indefinitely. A daemon
        holding its own tasks is exactly the case that never fires."""
        fired: list = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: fired.append(ctx))

        async def boom():
            raise RuntimeError("silent death")

        task = asyncio.ensure_future(boom())     # deliberately UNsupervised
        await asyncio.sleep(0.05)
        assert not fired, "if this passes, the GC gap closed upstream"
        keep = task                              # a live reference
        await asyncio.sleep(0.05)
        assert not fired
        del task, keep
        gc.collect()
        await asyncio.sleep(0.05)
        assert fired, "the backstop should eventually fire, at GC"

    @pytest.mark.asyncio
    async def test_supervision_fires_WITHOUT_gc(self):
        """Which is why the immediate detector exists."""
        seen: list = []
        pa.register_sink(seen.append)

        async def boom():
            raise RuntimeError("caught immediately")

        t = pa.spawn_supervised(boom(), origin="held")
        held = t                                  # reference kept ON PURPOSE
        await asyncio.sleep(0.05)
        assert seen, "the immediate detector did not fire"
        assert held is not None

    @pytest.mark.asyncio
    async def test_the_backstop_catches_what_supervision_cannot(self):
        """Callback errors and transport faults have no task to supervise."""
        seen: list = []
        pa.register_sink(seen.append)
        pa.install()
        asyncio.get_running_loop().call_exception_handler(
            {"message": "transport failed", "exception": ValueError("bad frame")})
        await asyncio.sleep(0.01)
        assert seen and seen[0]["exc_type"] == "ValueError"


class TestItDoesNotCryWolf:
    def test_a_cascade_is_ONE_panic(self):
        """Ten failures from one root must not be ten overlays."""
        seen: list = []
        pa.register_sink(seen.append)
        for _ in range(10):
            try:
                raise RuntimeError("same root")
            except RuntimeError as exc:
                pa.report(exc, origin="x")
        assert len(seen) == 1

    def test_distinct_fault_SITES_are_distinct_panics(self):
        seen: list = []
        pa.register_sink(seen.append)

        def _site_a():
            raise ValueError("from a")

        def _site_b():
            raise TypeError("from b")

        for fn in (_site_a, _site_b):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                pa.report(exc, origin="x")
        assert len(seen) == 2

    def test_the_SAME_site_dedups_even_with_a_new_message(self):
        """Dedup is by fault SITE on purpose. A task dying every second at
        one line must not produce sixty overlays — and keying on the
        message would defeat dedup entirely the moment a message contained
        an op-id or a timestamp."""
        seen: list = []
        pa.register_sink(seen.append)
        for i in range(5):
            try:
                raise ValueError(f"attempt {i} at op-{i}")
            except ValueError as exc:
                pa.report(exc, origin="x")
        assert len(seen) == 1

    @pytest.mark.parametrize("exc", [
        asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(),
    ])
    def test_control_flow_is_not_a_panic(self, exc):
        """An alarm that fires on every clean shutdown trains the operator
        to ignore the one that matters."""
        seen: list = []
        pa.register_sink(seen.append)
        pa.report(exc, origin="shutdown")
        assert seen == []

    def test_anything_not_provably_benign_IS_a_panic(self):
        """The failure this module ends is silence, so the default answer
        is 'report it'."""
        seen: list = []
        pa.register_sink(seen.append)
        pa.report(ImportError("cannot import name 'get_active_harness'"),
                  origin="the exact shape that bit us three times")
        assert len(seen) == 1


class TestItReusesWhatExists:
    def test_it_rides_the_EXISTING_uds_envelope(self):
        """No new transport, no new frame convention: clients that predate
        this ignore an unknown kind exactly as they always have."""
        import inspect

        from backend.core.ouroboros.battle_test import cockpit_attach
        src = inspect.getsource(cockpit_attach.install_panic_broadcast)
        assert "publish_telemetry_global" in src

    def test_it_reuses_the_palette_FloatContainer(self):
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout
        src = inspect.getsource(bipartite_layout.build_bipartite_application)
        assert "panic_rows" in src
        assert "FloatContainer" in src

    def test_both_existing_handlers_DELEGATE_rather_than_multiply(self):
        """A third `set_exception_handler` would be the duplication we
        were told to avoid; the two curated handlers stay authoritative."""
        import pathlib
        for rel in ("battle_test/serpent_flow.py", "battle_test/harness.py"):
            src = pathlib.Path(
                f"backend/core/ouroboros/{rel}").read_text()
            assert "panic_arbiter" in src, rel


class TestNeverRaises:
    def test_the_arbiter_cannot_become_a_second_silent_death(self):
        def _bad_sink(_p):
            raise RuntimeError("sink exploded")
        pa.register_sink(_bad_sink)
        assert pa.report(ValueError("real"), origin="x") is not None

    @pytest.mark.parametrize("call", [
        lambda: pa.report(None),
        lambda: pa.arbitrate(None, None),          # type: ignore[arg-type]
        lambda: pa.arbitrate(None, {"junk": 1}),
        lambda: pa.render_panic(None),
        lambda: pa.render_panic({"traceback": None}),
        lambda: pa.supervise(None),
        lambda: pa.install(object()),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_the_master_flag_silences_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PANIC_ARBITER_ENABLED", "0")
        seen: list = []
        pa.register_sink(seen.append)
        pa.report(RuntimeError("x"), origin="y")
        assert seen == []

    def test_the_overlay_keeps_the_LAST_frames(self):
        """A traceback's head is framework scaffolding; the tail is where
        the organism actually died."""
        tb = "\n".join(f"  File \"f{i}.py\", line {i}" for i in range(40))
        rows = pa.render_panic({"exc_type": "E", "message": "m",
                                "traceback": tb}, max_frames=5)
        assert any("f39.py" in r for r in rows)
        assert not any("f0.py" in r for r in rows)


class TestTheDemoShowsIt:
    def test_the_overlay_appears_in_the_demo(self):
        from backend.core.ouroboros.cli import ov_demo as d
        lo, _hi = d._PANIC_AT
        rows = d._panic_rows(lo + 0.5, 90)
        assert rows and "FATAL" in rows[0]

    def test_it_is_absent_before_the_fault(self):
        from backend.core.ouroboros.cli import ov_demo as d
        assert d._panic_rows(1.0, 90) == []

    def test_it_uses_a_REAL_traceback(self):
        """A pre-formatted block would keep looking right through a
        regression in `render_panic`'s frame selection."""
        import inspect

        from backend.core.ouroboros.cli import ov_demo as d
        src = inspect.getsource(d._panic_rows)
        assert "render_panic" in src
        assert "format_exception" in src


class TestTheOverlayDefectsSeenLive:
    """Three defects visible in one screenshot: empty fields rendered as
    `?:` / `origin: ?`, the FATAL notice drawn in the same green as a
    success, and "esc dismisses" advertised while nothing was bound to it
    — so the overlay could not be cleared."""

    def test_an_empty_payload_raises_NO_overlay(self):
        """`?:` and `origin: ?` told the operator nothing while covering
        their screen. A payload with no content is a bug in whoever built
        it, not a panic worth showing."""
        assert pa.render_panic({"exc_type": "", "message": "",
                                "origin": "", "traceback": ""}) == []
        assert pa.render_panic({}) == []

    def test_a_partial_payload_still_renders_what_it_HAS(self):
        rows = pa.render_panic({"exc_type": "ImportError", "message": "",
                                "origin": "", "traceback": ""})
        assert rows and "ImportError" in rows[1]
        assert not any("origin" in r for r in rows)   # absent, not "?"

    def test_a_message_with_no_type_still_names_the_failure(self):
        rows = pa.render_panic({"message": "the loop stopped"})
        assert rows and "unknown error" in rows[1]

    def test_the_overlay_is_styled_from_the_SEMANTIC_layer(self):
        """`class:panic` was never registered in any prompt_toolkit Style,
        so a FATAL notice inherited the default and rendered green."""
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout
        src = inspect.getsource(
            bipartite_layout.build_bipartite_application)
        assert "style_for(\"alert\")" in src or "style_for('alert')" in src
        assert 'lambda: [("class:panic"' not in src

    def test_the_rich_style_translates_to_prompt_toolkit(self):
        """The two engines spell colours differently — Rich says
        `bright_yellow`, prompt_toolkit says `ansibrightyellow`. Assuming
        either dialect is how the overlay silently rendered default."""
        from backend.core.ouroboros.ui.semantic_tokens import style_for
        raw = style_for("alert")
        assert raw == "bright_yellow"
        assert "ansi" + raw.replace("_", "") == "ansibrightyellow"

    def test_esc_is_actually_BOUND_to_dismiss(self):
        """`dismiss_panic` had ZERO callers. The overlay advertised a key
        nothing listened for, so the notice was permanent."""
        import inspect

        from backend.core.ouroboros.cli import ov
        src = inspect.getsource(ov._client_extra_bindings)
        assert "dismiss_panic" in src
        assert "app:dismissPanic" in src
        assert '("escape",)' in src

    def test_dismiss_clears_the_sticky_notice(self):
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        ui.on_telemetry({"kind": "fatal_panic", "exc_type": "RuntimeError",
                         "message": "boom", "origin": "x",
                         "traceback": "  File \"a.py\""})
        assert ui._panic_rows()
        ui.dismiss_panic()
        assert ui._panic_rows() == []
