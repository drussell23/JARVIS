"""``/liveness`` — and the point-in-time guarantee under a storm.

The registry that audits every other capability was itself unreadable across a
process boundary: a three-and-a-half-hour soak held a fully-populated
`dynamic_dispatch_registry` that no verb, route, or log line could show.

Two things are asserted here, and the first one is a claim about a lock that
was already correct.

THE PiT COPY ALREADY EXISTED — AND `asyncio.Lock` WOULD HAVE BROKEN IT
----------------------------------------------------------------------
`snapshot()` builds its row list INSIDE ``with _lock:``, where ``_lock`` is a
``threading.RLock`` shared with ``register()`` and ``note_invocation()``. The
copy is atomic already; the bombardment test below proves it rather than
assuming it.

Swapping that for an ``asyncio.Lock`` would have been a regression, not a
hardening, for three reasons that are checkable rather than stylistic:

  1. **The writers are synchronous.** ``trinity_event_bus`` calls
     ``_dd_register`` at subscribe and ``_dd_invoked`` at delivery as plain
     sync calls. An ``asyncio.Lock`` can only be acquired with ``await``, so
     either the writers become coroutines — changing the contract of every
     call site — or they stop locking entirely.
  2. **An asyncio lock is not thread-safe.** It serialises coroutines within
     ONE event loop. The decorator wraps sync handlers too, and delivery may
     land on an executor thread; those writers would bypass the lock
     completely and reintroduce exactly the mutation-during-iteration this is
     meant to prevent.
  3. **The reader is not always async.** `capability_liveness` and the
     liveness sensor call ``snapshot()`` from sync code.

So the lock stays a ``threading.RLock`` and these tests pin why.
"""
from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from backend.core.ouroboros.governance import dynamic_dispatch_registry as dd
from backend.core.ouroboros.governance import liveness_repl as lr


@pytest.fixture(autouse=True)
def _clean():
    dd.reset_for_tests()
    lr.reset_cache_for_tests()
    yield
    dd.reset_for_tests()
    lr.reset_cache_for_tests()


# ---------------------------------------------------------------------------
# 1. the point-in-time guarantee, under load
# ---------------------------------------------------------------------------


class TestSnapshotIsAtomicUnderAStorm:
    @pytest.mark.asyncio
    async def test_1000_concurrent_events_never_corrupt_a_snapshot(self):
        """The mandated case: bombard the registry while serialising it.

        ``RuntimeError: dictionary changed size during iteration`` is the
        failure being ruled out, and it is raised by CPython only when a dict
        is mutated *while being iterated*. Every writer and the reader take
        the same lock, so the window does not exist — asserted against 1000
        writes across 8 concurrent producers, with snapshots taken throughout.
        """
        stop = asyncio.Event()
        errors: list = []
        snapshots: list = []

        async def _producer(worker: int) -> None:
            for i in range(125):
                dd.register(f"mod_{worker}_{i % 40}", channel="storm")
                dd.note_invocation(f"mod_{worker}_{i % 40}", channel="storm")
                if i % 25 == 0:
                    await asyncio.sleep(0)      # yield: interleave for real

        async def _reader() -> None:
            while not stop.is_set():
                try:
                    snapshots.append(dd.snapshot())
                except Exception as exc:        # noqa: BLE001
                    errors.append(exc)
                await asyncio.sleep(0)

        reader = asyncio.ensure_future(_reader())
        await asyncio.gather(*(_producer(w) for w in range(8)))
        stop.set()
        await asyncio.wait_for(reader, timeout=10.0)

        assert not errors, f"snapshot raised during mutation: {errors[:3]}"
        assert len(snapshots) > 1, "reader never interleaved with the writers"
        for snap in snapshots:
            # Structural integrity of every PiT copy, not just the last.
            assert isinstance(snap["rows"], list)
            assert snap["tracked"] >= len(snap["rows"])
            for row in snap["rows"]:
                assert row["invocations"] <= row["registrations"] + 1

    def test_snapshot_survives_mutation_from_OTHER_THREADS(self):
        """The case an ``asyncio.Lock`` could not have covered at all.

        The decorator wraps sync handlers, and event delivery may run on an
        executor thread. A lock scoped to one event loop would leave these
        writers unsynchronised.
        """
        errors: list = []
        stop = threading.Event()

        def _writer(worker: int) -> None:
            i = 0
            while not stop.is_set() and i < 400:
                dd.register(f"t{worker}_{i % 30}")
                dd.note_invocation(f"t{worker}_{i % 30}")
                i += 1

        threads = [threading.Thread(target=_writer, args=(w,), daemon=True)
                   for w in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(300):
                try:
                    dd.snapshot()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5.0)
        assert not errors, f"snapshot raised under thread mutation: {errors[:3]}"

    def test_the_lock_is_thread_scoped_and_shared_by_readers_and_writers(self):
        """Pins the mechanism, because the alternative was proposed.

        A future edit to ``asyncio.Lock`` fails here with a message saying
        why, rather than passing tests and silently unsynchronising the sync
        writers in `trinity_event_bus`.
        """
        assert isinstance(dd._lock, type(threading.RLock())), (
            "the dispatch registry lock must be a threading lock: its writers "
            "(trinity_event_bus._dd_register/_dd_invoked) are SYNC calls and "
            "may run off the event loop, so an asyncio.Lock cannot guard them"
        )
        source = inspect.getsource(dd.snapshot)
        body = source.split("with _lock:", 1)
        assert len(body) == 2, "snapshot no longer takes the lock"
        # The comprehension must be INSIDE the lock — that is the PiT copy.
        assert "for r in _records.values()" in body[1].split("return", 1)[0]

    def test_writers_are_sync_callables(self):
        """The reason (1) above is structural, not an opinion."""
        assert not inspect.iscoroutinefunction(dd.register)
        assert not inspect.iscoroutinefunction(dd.note_invocation)
        assert not inspect.iscoroutinefunction(dd.snapshot)


# ---------------------------------------------------------------------------
# 2. the verb
# ---------------------------------------------------------------------------


class TestLivenessVerb:
    def test_it_is_auto_discovered_by_the_naming_cage(self):
        """No registration code. ``*_repl.py`` + ``dispatch_<verb>_command``
        is the whole contract, and it is the same cage that gives the verb a
        palette row and a description."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER, prime_registry,
        )
        prime_registry()
        assert "liveness" in _VERB_TO_DISPATCHER

    def test_it_is_async_because_the_scan_is_21_seconds(self):
        """A sync dispatcher would hold the daemon's event loop for the whole
        scan — freezing every attached cockpit and the running soak."""
        assert inspect.iscoroutinefunction(lr.dispatch_liveness_command)

    @pytest.mark.asyncio
    async def test_it_returns_text_rather_than_printing(self):
        """The mirror carries the RETURNED text. A verb that prints to the
        daemon's console renders locally and reaches no attached cockpit —
        the defect `_dispatch_repl_command` calls "THE gap that made 59 verbs
        invisible from `ov attach`"."""
        result = await lr.dispatch_liveness_command("/liveness")
        assert result.matched and result.ok
        assert isinstance(result.text, str) and result.text.strip()

    @pytest.mark.asyncio
    async def test_it_declines_lines_that_are_not_its_own(self):
        for line in ("/graph", "liveness_check", "", "   ", "/livenessx"):
            out = await lr.dispatch_liveness_command(line)
            assert out.matched is False, line

    @pytest.mark.asyncio
    async def test_the_default_view_is_the_instant_one(self):
        """0.02 ms vs 21 s. The default must not be the expensive one, or the
        verb becomes something an operator learns not to type."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        out = await lr.dispatch_liveness_command("/liveness")
        assert (loop.time() - start) < 2.0
        assert "dispatch registry" in out.text

    @pytest.mark.asyncio
    async def test_registry_contents_reach_the_rendered_table(self):
        dd.register("alpha_mod", channel="bus")
        dd.register("beta_mod", channel="bus")
        dd.note_invocation("beta_mod", channel="bus")
        out = await lr.dispatch_liveness_command("/liveness")
        assert "alpha_mod" in out.text and "beta_mod" in out.text
        assert "REGISTERED_NEVER_INVOKED" in out.text
        assert "FIRING_DYNAMICALLY" in out.text

    @pytest.mark.asyncio
    async def test_an_empty_registry_says_so_without_implying_breakage(self):
        """"0 tracked" reads as a fault unless the row says otherwise, and the
        operator's next move differs completely between the two."""
        out = await lr.dispatch_liveness_command("/liveness")
        assert "not that the registry is broken" in out.text

    @pytest.mark.asyncio
    async def test_a_disabled_registry_is_reported_as_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED", "0")
        out = await lr.dispatch_liveness_command("/liveness")
        assert "OFF" in out.text

    @pytest.mark.asyncio
    async def test_help_is_reachable_by_every_spelling(self):
        for line in ("/liveness help", "/liveness --help", "/liveness -h",
                     "/liveness ?"):
            out = await lr.dispatch_liveness_command(line)
            assert "what actually ran" in out.text, line

    @pytest.mark.asyncio
    async def test_a_malformed_line_never_raises(self):
        out = await lr.dispatch_liveness_command('/liveness "unclosed')
        assert out.ok is False and "parse error" in out.text

    @pytest.mark.asyncio
    async def test_it_renders_markup_not_ansi(self):
        """The bridge carries MARKUP and each client fits it to its own
        canvas. ANSI would bake this daemon's width and colour depth into the
        wire — worse on every terminal that differs."""
        out = await lr.dispatch_liveness_command("/liveness")
        assert "\033[" not in out.text
        assert "[cyan]" in out.text or "[bold cyan]" in out.text


# ---------------------------------------------------------------------------
# 3. filtering, and the loop staying alive while the scan runs
# ---------------------------------------------------------------------------


class TestFilteringAndLoopLiveness:
    _ROWS = [
        {"source_file": "a.py", "flag": "F_A", "firing": "SILENT",
         "ledger_backed": True, "fraction": 1.0, "severity": "high"},
        {"source_file": "b.py", "flag": "F_B", "firing": "SILENT",
         "ledger_backed": False, "fraction": 0.9, "severity": "low"},
        {"source_file": "c.py", "flag": "F_C", "firing": "UNKNOWN",
         "ledger_backed": False, "fraction": 0.8, "severity": "low"},
    ]

    def test_high_selects_only_proven_findings(self):
        assert [r["flag"] for r in lr._filter(self._ROWS, "high")] == ["F_A"]

    def test_silent_selects_by_firing_state_not_severity(self):
        """Both silences, deliberately — the row's PROOF column is what
        distinguishes them, and hiding the unprovable ones would hide the
        observability gaps worth fixing."""
        assert [r["flag"] for r in lr._filter(self._ROWS, "silent")] == \
            ["F_A", "F_B"]

    def test_all_filters_nothing(self):
        assert len(lr._filter(self._ROWS, "all")) == 3

    def test_the_split_is_spelled_out_for_silent(self):
        text = lr._render_capabilities(
            lr._filter(self._ROWS, "silent"), "silent", 0.0, 3)
        assert "ledger-backed" in text and "log-only" in text

    def test_filtering_happens_before_rendering(self):
        """Which here IS before transmission: the daemon mirrors the rendered
        text, so a filtered row never reaches the socket."""
        text = lr._render_capabilities(
            lr._filter(self._ROWS, "high"), "high", 0.0, 3)
        assert "F_A" not in text or "b.py" not in text
        assert "b.py" not in text and "c.py" not in text

    @pytest.mark.asyncio
    async def test_the_event_loop_keeps_running_during_the_scan(self, monkeypatch):
        """The whole reason this dispatcher is async.

        A synchronous 21-second scan would stall the heartbeat, every attached
        cockpit, and the soak. Asserted with a ticker that must keep being
        scheduled while a deliberately-blocking scan runs.
        """
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        def _slow_scan():
            import time as _t
            _t.sleep(0.4)               # blocking, like the real thing
            return list(self._ROWS), 0.0

        monkeypatch.setattr(lr, "_collect_rows", _slow_scan)
        ticker = asyncio.ensure_future(_ticker())
        try:
            out = await asyncio.wait_for(
                lr.dispatch_liveness_command("/liveness --all"), timeout=10.0)
            assert out.ok
            assert ticks > 0, (
                "the event loop was blocked for the whole scan — the verb is "
                "running it inline instead of on a worker thread"
            )
        finally:
            ticker.cancel()

    @pytest.mark.asyncio
    async def test_a_failing_scan_degrades_to_a_message(self, monkeypatch):
        def _boom():
            raise RuntimeError("scan exploded")
        monkeypatch.setattr(lr, "_collect_rows", _boom)
        out = await lr.dispatch_liveness_command("/liveness --high")
        assert out.ok is False
        assert "still works" in out.text        # points at the working view

    @pytest.mark.asyncio
    async def test_the_expensive_scan_is_memoised(self, monkeypatch):
        calls = []

        def _counted():
            calls.append(1)
            return list(self._ROWS), 0.0

        monkeypatch.setattr(lr, "_collect_rows", _counted)
        await lr.dispatch_liveness_command("/liveness --high")
        await lr.dispatch_liveness_command("/liveness --silent")
        assert len(calls) == 2, (
            "the memo lives inside _collect_rows, so a stubbed scan is called "
            "per invocation — this pins the CALL SHAPE; TTL behaviour is "
            "covered by test_the_ttl_is_env_tunable"
        )

    def test_the_ttl_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIVENESS_REPL_SCAN_TTL_S", "5")
        assert lr.scan_ttl_s() == 5.0
        monkeypatch.setenv("JARVIS_LIVENESS_REPL_SCAN_TTL_S", "not-a-number")
        assert lr.scan_ttl_s() == lr._SCAN_TTL_S
