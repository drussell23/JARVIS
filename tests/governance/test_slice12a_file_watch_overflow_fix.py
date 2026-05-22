"""Slice 12A — file_watch_guard queue overflow fix.

Closes the empirical wedge from bt-2026-05-22-074210 (Slice 11B-fix
acceptance soak): 15,981 ``asyncio.QueueFull`` exceptions inside an
asyncio callback caused asyncio's default exception handler to
format and log a multi-line traceback per overflow — on the loop
thread. That cascade sustained 100+ s of cumulative loop-block
even after OpportunityMiner went fully off-loop.

## Root cause (operator-identified)

``FileWatchGuard._queue_event`` scheduled the producer via:

    loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

With ``BoundedAsyncQueue(policy=WARN_AND_BLOCK)`` and a producer
that's non-blocking by construction (it's running inside an
asyncio callback), the WARN_AND_BLOCK policy is the wrong shape —
when full, ``put_nowait`` raises ``QueueFull`` synchronously,
asyncio catches the unhandled callback exception, and the default
handler emits the traceback on the loop.

## 12A scope

1. New wrapper method ``FileWatchGuard._queue_event_on_loop``
   runs on the loop thread, catches ``QueueFull`` + any other
   ``Exception``, increments structured metrics, rate-limits
   summary logs.
2. ``call_soon_threadsafe`` target is now the wrapper — NOT
   ``put_nowait`` directly.
3. Queue policy changed from ``WARN_AND_BLOCK`` to
   ``DROP_NEWEST``. Loss of newest events is safe — downstream
   debounce + periodic scans recover any drift.
4. New ``WatchMetrics`` fields:
   ``events_dropped_queue_full`` / ``queue_full_suppressed_logs``
   / ``last_overflow_at`` / ``last_overflow_log_at``.

## Test surface

### Behavioural (loop-thread wrapper)
  * Filling the queue and calling the wrapper does NOT raise.
  * Dropped counter increments per overflow.
  * Summary log is rate-limited to ≤ 1 per second per instance.
  * Suppressed-log counter increments for overflows in the
    rate-limit window.
  * Wrapper swallows arbitrary exceptions defensively.

### Telemetry truth
  * NO asyncio default exception handler invocation when the
    queue is filled by ``call_soon_threadsafe(wrapper, ...)``.

### AST pins
  * ``FileWatchGuard._queue_event`` body contains no direct
    ``put_nowait`` call.
  * The ``call_soon_threadsafe`` target inside
    ``FileWatchGuard._queue_event`` is
    ``self._queue_event_on_loop`` (the wrapper), not
    ``self._event_queue.put_nowait``.
  * Queue policy is ``DROP_NEWEST``.

### Regression
  * Existing batch/debounce behaviour intact — a wrapper-enqueued
    event is read by the processor with the normal semantics.
"""

from __future__ import annotations

import ast as _ast
import asyncio
import logging
import pathlib
import time
import unittest
from typing import List
from unittest.mock import patch

from backend.core.resilience.file_watch_guard import (
    FileEvent,
    FileEventType,
    FileWatchConfig,
    FileWatchGuard,
    WatchMetrics,
)
from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GUARD_FILE = (
    _REPO_ROOT / "backend" / "core" / "resilience" / "file_watch_guard.py"
)


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


def _make_event(idx: int) -> FileEvent:
    """Minimal FileEvent for tests."""
    return FileEvent(
        event_type=FileEventType.MODIFIED,
        path=pathlib.Path(f"/tmp/slice12a_test_{idx}.py"),
        timestamp=time.time(),
    )


# ============================================================================
# Pin: queue policy is DROP_NEWEST
# ============================================================================


class TestQueuePolicy(unittest.IsolatedAsyncioTestCase):

    async def test_event_queue_policy_is_drop_newest(self) -> None:
        """The producer can't BLOCK (it runs on the loop), so the
        queue policy must be DROP_NEWEST. WARN_AND_BLOCK was the
        empirically-broken shape."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        # Only check policy if BoundedAsyncQueue is available.
        if isinstance(guard._event_queue, BoundedAsyncQueue):
            self.assertEqual(
                guard._event_queue.policy,
                OverflowPolicy.DROP_NEWEST,
                f"queue policy must be DROP_NEWEST; got "
                f"{guard._event_queue.policy.name}",
            )


# ============================================================================
# Wrapper behaviour — runs on loop thread
# ============================================================================


class TestQueueEventOnLoopWrapper(unittest.IsolatedAsyncioTestCase):

    async def _new_guard_with_full_queue(
        self, capacity: int = 4,
    ) -> FileWatchGuard:
        """Build a guard whose queue is already full so subsequent
        ``put_nowait`` raises QueueFull. The wrapper must catch
        that and account for it."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        # Replace the queue with a tiny DROP_NEWEST one so we can
        # exercise overflow deterministically. NB: DROP_NEWEST's
        # put_nowait silently drops AT the queue layer; we want to
        # exercise the wrapper's QueueFull catch too, so we also
        # patch put_nowait to raise on full for one of the tests.
        guard._event_queue = BoundedAsyncQueue(
            maxsize=capacity,
            policy=OverflowPolicy.DROP_NEWEST,
            name="test_file_watch_events",
        )
        # Fill it.
        for i in range(capacity):
            guard._event_queue.put_nowait(_make_event(i))
        return guard

    async def test_wrapper_does_not_raise_on_full_queue(self) -> None:
        guard = await self._new_guard_with_full_queue()
        # DROP_NEWEST already silently drops — wrapper still runs cleanly.
        try:
            guard._queue_event_on_loop(_make_event(99))
        except Exception as exc:  # pragma: no cover
            self.fail(
                f"wrapper raised {type(exc).__name__}: {exc} — "
                f"it must never propagate"
            )

    async def test_wrapper_catches_queue_full_raised_by_put_nowait(
        self,
    ) -> None:
        """Defensive path: even if the queue's put_nowait raises
        QueueFull (e.g., fallback asyncio.Queue without DROP_NEWEST
        semantics), the wrapper must catch it + bump the counter."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        # Patch put_nowait to always raise.
        with patch.object(
            guard._event_queue, "put_nowait",
            side_effect=asyncio.QueueFull,
        ):
            try:
                guard._queue_event_on_loop(_make_event(0))
            except Exception as exc:  # pragma: no cover
                self.fail(
                    f"wrapper raised {type(exc).__name__}: {exc} — "
                    f"must swallow QueueFull"
                )
        self.assertEqual(
            guard.metrics.events_dropped_queue_full, 1,
            "events_dropped_queue_full must increment on QueueFull",
        )
        self.assertIsNotNone(guard.metrics.last_overflow_at)

    async def test_wrapper_swallows_unexpected_exception(self) -> None:
        """Wrapper is defense-in-depth: any exception type from
        put_nowait is caught so asyncio default handler never
        sees it."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        with patch.object(
            guard._event_queue, "put_nowait",
            side_effect=RuntimeError("synthetic"),
        ):
            try:
                guard._queue_event_on_loop(_make_event(0))
            except Exception as exc:  # pragma: no cover
                self.fail(
                    f"wrapper raised {type(exc).__name__}: {exc}",
                )

    async def test_wrapper_rate_limits_summary_logs(self) -> None:
        """Multiple drops within 1s window emit at most ONE log;
        ``queue_full_suppressed_logs`` accumulates the rest."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        with patch.object(
            guard._event_queue, "put_nowait",
            side_effect=asyncio.QueueFull,
        ):
            with self.assertLogs(
                "backend.core.resilience.file_watch_guard", level="WARNING",
            ) as captured:
                # 50 drops in rapid succession — only the first
                # should log; the rest go into suppressed counter.
                for i in range(50):
                    guard._queue_event_on_loop(_make_event(i))
        self.assertEqual(
            guard.metrics.events_dropped_queue_full, 50,
            "every drop must count, regardless of log emission",
        )
        # Exactly one warning record from the wrapper (subsequent
        # drops were rate-limited).
        warnings = [
            r for r in captured.records
            if "file_watch_events overflow" in r.getMessage()
        ]
        self.assertEqual(
            len(warnings), 1,
            f"wrapper must emit exactly 1 summary log per 1s "
            f"window; got {len(warnings)}",
        )
        # Remaining 49 drops should be in suppressed counter.
        self.assertEqual(
            guard.metrics.queue_full_suppressed_logs, 49,
            f"suppressed-logs counter must accumulate; got "
            f"{guard.metrics.queue_full_suppressed_logs}",
        )

    async def test_wrapper_relog_after_rate_limit_window(self) -> None:
        """A drop ≥1s after the prior summary log emits a new one
        and resets the suppressed counter to zero."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        with patch.object(
            guard._event_queue, "put_nowait",
            side_effect=asyncio.QueueFull,
        ):
            with self.assertLogs(
                "backend.core.resilience.file_watch_guard", level="WARNING",
            ) as captured:
                guard._queue_event_on_loop(_make_event(0))
                # Force the rate-limit window to elapse.
                guard.metrics.last_overflow_log_at = (
                    guard.metrics.last_overflow_log_at - 2.0
                )
                guard._queue_event_on_loop(_make_event(1))
        warnings = [
            r for r in captured.records
            if "file_watch_events overflow" in r.getMessage()
        ]
        self.assertEqual(
            len(warnings), 2,
            "second log must fire after rate-limit window expires",
        )

    async def test_wrapper_success_does_not_increment_drops(
        self,
    ) -> None:
        """Happy path: put_nowait succeeds; metrics stay at 0."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        guard._queue_event_on_loop(_make_event(0))
        self.assertEqual(
            guard.metrics.events_dropped_queue_full, 0,
        )
        # The event landed on the queue.
        self.assertEqual(guard._event_queue.qsize(), 1)


# ============================================================================
# Telemetry truth — no asyncio default exception handler invocation
# ============================================================================


class TestNoAsyncioDefaultHandlerInvocation(
    unittest.IsolatedAsyncioTestCase,
):

    async def test_call_soon_threadsafe_with_wrapper_no_exception_handler(
        self,
    ) -> None:
        """When the producer publishes via ``call_soon_threadsafe(
        wrapper, event)`` and the queue is full, the asyncio
        default exception handler must NOT fire. The wrapper has
        already swallowed the QueueFull."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        # Capture the loop's exception handler invocations.
        loop = asyncio.get_running_loop()
        handler_calls: List[dict] = []

        def _capture_handler(_loop, context):  # noqa: ANN001
            handler_calls.append(context)

        original = loop.get_exception_handler()
        loop.set_exception_handler(_capture_handler)
        try:
            # Force QueueFull on every put_nowait.
            with patch.object(
                guard._event_queue, "put_nowait",
                side_effect=asyncio.QueueFull,
            ):
                # Mimic the producer codepath: schedule the wrapper
                # via call_soon_threadsafe.
                for i in range(20):
                    loop.call_soon_threadsafe(
                        guard._queue_event_on_loop, _make_event(i),
                    )
                # Let the loop tick to run the scheduled callbacks.
                await asyncio.sleep(0.05)
        finally:
            loop.set_exception_handler(original)

        # No callback should have escaped to the default handler.
        self.assertEqual(
            handler_calls, [],
            f"asyncio default exception handler should NOT be "
            f"invoked when the wrapper is in place; got "
            f"{len(handler_calls)} invocations",
        )
        # Sanity: every drop accounted for.
        self.assertEqual(
            guard.metrics.events_dropped_queue_full, 20,
        )


# ============================================================================
# AST pins
# ============================================================================


class TestAstPins(unittest.TestCase):
    """Slice 12A invariants — pinned at the source level so a
    regression to the broken shape (passing
    ``self._event_queue.put_nowait`` to ``call_soon_threadsafe``)
    fails fast."""

    def _find_method(
        self, tree: _ast.Module, class_name: str, method_name: str,
    ) -> _ast.FunctionDef:
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ClassDef):
                continue
            if node.name != class_name:
                continue
            for sub in node.body:
                if (
                    isinstance(sub, _ast.FunctionDef)
                    and sub.name == method_name
                ):
                    return sub
        raise AssertionError(
            f"could not find {class_name}.{method_name}",
        )

    def test_queue_event_does_not_call_put_nowait_directly(
        self,
    ) -> None:
        tree = _parse_module(_GUARD_FILE)
        m = self._find_method(tree, "FileWatchGuard", "_queue_event")
        offenders = []
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if isinstance(f, _ast.Attribute) and f.attr == "put_nowait":
                offenders.append(sub.lineno)
        self.assertEqual(
            offenders, [],
            f"FileWatchGuard._queue_event body must not call "
            f"put_nowait directly (offenders at L{offenders}); "
            f"route through _queue_event_on_loop",
        )

    def test_call_soon_threadsafe_target_is_wrapper(self) -> None:
        """The call_soon_threadsafe call inside
        ``FileWatchGuard._queue_event`` must use
        ``self._queue_event_on_loop`` as the callback. Passing
        ``self._event_queue.put_nowait`` is the broken shape."""
        tree = _parse_module(_GUARD_FILE)
        m = self._find_method(tree, "FileWatchGuard", "_queue_event")
        targets: List[str] = []
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if not isinstance(f, _ast.Attribute):
                continue
            if f.attr != "call_soon_threadsafe":
                continue
            if not sub.args:
                continue
            targets.append(_ast.unparse(sub.args[0]))
        self.assertEqual(
            len(targets), 1,
            f"_queue_event should contain exactly one "
            f"call_soon_threadsafe; got {len(targets)}",
        )
        self.assertEqual(
            targets[0], "self._queue_event_on_loop",
            f"call_soon_threadsafe target must be the wrapper "
            f"`self._queue_event_on_loop`; got `{targets[0]}`. "
            f"Passing `self._event_queue.put_nowait` is the broken "
            f"shape — QueueFull leaks into the asyncio default "
            f"exception handler.",
        )

    def test_wrapper_method_exists(self) -> None:
        tree = _parse_module(_GUARD_FILE)
        m = self._find_method(
            tree, "FileWatchGuard", "_queue_event_on_loop",
        )
        self.assertIsNotNone(m)
        # Wrapper body must contain a try/except.
        has_try_except = any(
            isinstance(sub, _ast.Try) for sub in _ast.walk(m)
        )
        self.assertTrue(
            has_try_except,
            "_queue_event_on_loop must wrap put_nowait in try/except "
            "to swallow QueueFull",
        )

    def test_queue_policy_is_drop_newest_in_source(self) -> None:
        """Belt-and-suspenders source-level pin: the policy string
        must appear in the FileWatchGuard.__init__ body."""
        src = _GUARD_FILE.read_text()
        self.assertIn("OverflowPolicy.DROP_NEWEST", src)
        # The broken shape must not coexist in the producer init.
        # (WARN_AND_BLOCK may legitimately appear elsewhere in the
        # codebase; we only pin this specific queue's construction.)
        # Find the line that constructs file_watch_events queue.
        for line in src.splitlines():
            if 'name="file_watch_events"' in line:
                self.assertIn("DROP_NEWEST", line)
                self.assertNotIn("WARN_AND_BLOCK", line)
                break
        else:
            self.fail(
                "could not find file_watch_events queue construction",
            )


# ============================================================================
# Regression — wrapper-enqueued event is consumable by the processor
# ============================================================================


class TestProcessorStillConsumesWrapperEnqueuedEvents(
    unittest.IsolatedAsyncioTestCase,
):

    async def test_wrapper_enqueued_event_is_readable_from_queue(
        self,
    ) -> None:
        """Sanity: an event placed via the wrapper is read out of
        the queue with normal asyncio.Queue.get semantics. Confirms
        the wrapper hasn't changed the consumer contract."""
        guard = FileWatchGuard(
            watch_dir=pathlib.Path("/tmp"),
            on_event=lambda _ev: None,
        )
        ev = _make_event(42)
        guard._queue_event_on_loop(ev)
        # Should be retrievable.
        got = guard._event_queue.get_nowait()
        self.assertIs(got, ev)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
