"""One meaning for "the operator submitted a line", whichever surface it came from.

    serpent_flow.py:6322   result = self._on_command(line); await result
    harness.py:4576        loop.create_task(self._handle_repl_command_for(...))

Typing at the daemon terminal was ordered and backpressured. Typing the
identical line into an attached cockpit spawned a task per line, with no
ordering and no bound — so `/pause` then `/status` could report the
PRE-pause state, and a pasted block became N racing handlers.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.operator_input_queue import (
    OperatorInputQueue,
    active_queue_snapshot,
    render_queue,
    set_active_queue,
)


async def _collector(delay: float = 0.005):
    seen: list = []

    async def handler(text, session=None):
        await asyncio.sleep(delay)
        seen.append(text)

    q = OperatorInputQueue(handler)
    q.start()
    return q, seen


class TestOrderIsPreserved:
    @pytest.mark.asyncio
    async def test_a_fast_paste_stays_in_submission_order(self):
        """THE defect. N concurrent handlers finish in whatever order the
        loop pleases."""
        q, seen = await _collector()
        for i in range(8):
            q.submit(f"line {i}")
        await asyncio.sleep(0.25)
        assert seen == [f"line {i}" for i in range(8)]

    @pytest.mark.asyncio
    async def test_a_slow_handler_does_not_let_the_next_overtake_it(self):
        seen: list = []

        async def handler(text, session=None):
            await asyncio.sleep(0.08 if text == "slow" else 0.0)
            seen.append(text)

        q = OperatorInputQueue(handler)
        q.start()
        q.submit("slow")
        q.submit("fast")
        await asyncio.sleep(0.3)
        assert seen == ["slow", "fast"]

    @pytest.mark.asyncio
    async def test_submitting_never_blocks_the_caller(self):
        """`submit` runs on the bridge's READ loop. Blocking it would
        stall every cockpit's input, heartbeat and telemetry behind one
        slow handler — which is why the original was fire-and-forget."""
        q, _ = await _collector(delay=0.5)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(20):
            q.submit(f"x{i}")
        assert loop.time() - t0 < 0.05


class TestItRefusesRatherThanDrops:
    @pytest.mark.asyncio
    async def test_nothing_submitted_is_ever_lost_silently(self):
        """Operator intent is not telemetry. Every line is either
        PROCESSED or REFUSED-with-a-reason; the one outcome forbidden is
        vanishing while the operator believes it landed."""
        processed: list = []

        async def handler(text, session=None):
            await asyncio.sleep(0.002)
            processed.append(text)

        q = OperatorInputQueue(handler, max_depth=4)
        q.start()
        results = [q.submit(f"x{i}") for i in range(12)]
        await asyncio.sleep(0.4)
        accepted = [f"x{i}" for i, r in enumerate(results) if r.accepted]
        refused = [r for r in results if not r.accepted]
        assert sorted(processed) == sorted(accepted)
        assert len(accepted) + len(refused) == 12
        assert all(r.reason for r in refused), "a line vanished without a why"

    @pytest.mark.asyncio
    async def test_refusal_carries_a_reason_and_a_count(self):
        async def handler(text, session=None):
            await asyncio.sleep(1.0)

        q = OperatorInputQueue(handler, max_depth=2)
        q.start()
        results = [q.submit(f"x{i}") for i in range(5)]
        refused = [r for r in results if not r.accepted]
        assert refused, "a bounded queue accepted everything"
        assert all(r.reason for r in refused)
        assert q.refused == len(refused)

    @pytest.mark.asyncio
    async def test_empty_input_is_ignored_not_refused(self):
        q, _ = await _collector()
        for junk in ("", "   ", "\n", None):
            res = q.submit(junk)
            assert res.accepted is False
            assert res.reason == "empty"


class TestItCannotWedge:
    @pytest.mark.asyncio
    async def test_a_raising_handler_does_not_stop_the_queue(self):
        """A consumer that dies on one bad handler wedges every later
        line — strictly worse than the unordered path it replaced."""
        seen: list = []

        async def handler(text, session=None):
            if text == "boom":
                raise RuntimeError("handler died")
            seen.append(text)

        q = OperatorInputQueue(handler)
        q.start()
        for t in ("a", "boom", "b", "c"):
            q.submit(t)
        await asyncio.sleep(0.15)
        assert seen == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_a_sync_handler_works_too(self):
        seen: list = []
        q = OperatorInputQueue(lambda t, s=None: seen.append(t))
        q.start()
        q.submit("plain")
        await asyncio.sleep(0.05)
        assert seen == ["plain"]

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        q, seen = await _collector()
        first = q.start()
        assert q.start() is first

    @pytest.mark.asyncio
    async def test_close_stops_accepting(self):
        q, _ = await _collector()
        await q.aclose()
        assert q.submit("late").accepted is False


class TestTheOperatorCanSEEIt:
    @pytest.mark.asyncio
    async def test_depth_zero_renders_NOTHING(self):
        """A queue keeping up should be invisible. It earns a row only
        when the operator is ahead of the organism."""
        q, _ = await _collector()
        assert render_queue(q.snapshot()) == []

    @pytest.mark.asyncio
    async def test_a_backlog_shows_depth_and_what_is_next(self):
        """Depth alone is not enough — someone who typed three lines wants
        to know WHICH are still waiting, or the queue is just another
        opaque delay."""
        async def handler(text, session=None):
            await asyncio.sleep(0.6)

        q = OperatorInputQueue(handler)
        q.start()
        for t in ("first goal", "second goal", "third goal"):
            q.submit(t)
        await asyncio.sleep(0.01)
        rows = render_queue(q.snapshot(), width=100)
        assert rows and "queued" in rows[0]
        assert "second goal" in rows[0]

    @pytest.mark.asyncio
    async def test_refusals_are_surfaced(self):
        async def handler(text, session=None):
            await asyncio.sleep(1.0)

        q = OperatorInputQueue(handler, max_depth=1)
        q.start()
        for i in range(4):
            q.submit(f"x{i}")
        rows = render_queue(q.snapshot(), width=100)
        assert any("refused" in r for r in rows)

    @pytest.mark.asyncio
    async def test_it_rides_the_heartbeat(self):
        """The cockpit is a different process; the depth crosses on the
        same lane every other live state does."""
        from backend.core.ouroboros.battle_test.attach_heartbeat import (
            _input_queue_payload,
        )

        async def handler(text, session=None):
            await asyncio.sleep(0.6)

        q = OperatorInputQueue(handler)
        q.start()
        set_active_queue(q)
        try:
            q.submit("a")
            q.submit("b")
            await asyncio.sleep(0.01)
            payload = _input_queue_payload()
            assert payload.get("depth", 0) >= 1
            assert payload.get("running") or payload.get("pending")
        finally:
            set_active_queue(None)

    def test_no_live_queue_is_an_empty_payload_not_a_crash(self):
        set_active_queue(None)
        assert active_queue_snapshot() == {}


class TestTheWiring:
    def test_the_attach_path_uses_the_queue(self):
        import inspect

        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        assert "_operator_input_queue" in src

    def test_the_legacy_path_survives_as_a_fallback(self):
        """Losing ORDER is bad; losing the line entirely is worse. With
        the master flag off — or if the queue cannot be built — the
        original `create_task` path must still deliver the line."""
        import ast as _ast
        import pathlib as _p

        src = _p.Path(
            "backend/core/ouroboros/battle_test/harness.py").read_text()
        tree = _ast.parse(src)
        # The fallback must remain REACHABLE: a `create_task(...)` call on
        # the same handler, outside the queue branch.
        fallbacks = [
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.Call)
            and getattr(n.func, "attr", "") == "create_task"
            and "_handle_repl_command_for" in _ast.dump(n)
        ]
        assert fallbacks, "the legacy delivery path was removed entirely"

    def test_ops_are_NOT_serialized_only_handlers(self):
        """`_handle_repl_command` schedules work and returns. Serializing
        the OPS would be a different and wrong change — the organism is
        concurrent by design."""
        import inspect

        from backend.core.ouroboros.battle_test import operator_input_queue
        doc = inspect.getdoc(operator_input_queue) or ""
        assert "does NOT serialize" in doc.replace("\n", " ") or \
            "NOT serialize" in doc


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: render_queue(None),
        lambda: render_queue("junk"),
        lambda: render_queue({"depth": "x"}),
        lambda: active_queue_snapshot(),
    ])
    def test_junk_degrades(self, call):
        assert call() is not None

    @pytest.mark.asyncio
    async def test_master_flag_off_falls_back(self, monkeypatch):
        from backend.core.ouroboros.battle_test.operator_input_queue import (
            input_queue_enabled,
        )
        monkeypatch.setenv("JARVIS_OPERATOR_INPUT_QUEUE_ENABLED", "0")
        assert input_queue_enabled() is False


class TestTheDemoShowsIt:
    """`ov demo live` is where this gets WATCHED. A surface only the real
    organism can drive is a surface nobody checks."""

    def test_the_backlog_appears_and_drains(self):
        from backend.core.ouroboros.cli import ov_demo as d
        lo, hi = d._BACKLOG
        assert d._queue_rows(lo + 0.2, 90), "no backlog during the window"
        assert d._queue_rows(hi + 2.0, 90) == [], "it never drained"

    def test_it_is_silent_outside_the_window(self):
        from backend.core.ouroboros.cli import ov_demo as d
        assert d._queue_rows(1.0, 90) == []

    def test_it_uses_the_COCKPITS_renderer(self):
        import inspect

        from backend.core.ouroboros.cli import ov_demo as d
        assert "render_queue" in inspect.getsource(d._queue_rows)

    def test_the_demo_MOUNTS_it(self):
        import inspect

        from backend.core.ouroboros.cli import ov_demo as d
        src = inspect.getsource(d.scene_live)
        assert "queue_rows=" in src and "panic_rows=" in src
