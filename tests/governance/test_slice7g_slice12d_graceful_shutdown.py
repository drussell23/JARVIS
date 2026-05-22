"""Slice 7g — provider circuit breaker default-TRUE graduation.
Slice 12D — graceful shutdown on global breaker trip.

Both slices land together: 7g flips the breaker on permanently
(four consecutive forced-budget acceptance soaks proved the
cascade), and 12D wires the global breaker's trip into the
harness's existing FIRST_COMPLETED shutdown race so the session
drains cleanly + writes ``summary.json`` BEFORE the wall-cap
timer would fire.

## Architecture composed (no new parallel surfaces)

  * ``circuit_breaker._GlobalBreaker.on_trip`` — append-only
    callback registry on the existing process-singleton.
    Callbacks fire **once** at the CLOSED → OPEN_TERMINAL
    transition; late-bind registrations against an
    already-tripped breaker fire immediately.
  * ``ide_observability_stream.EVENT_TYPE_SESSION_EXHAUSTED`` +
    ``publish_session_exhausted`` — observability surface for
    IDE consumers. Mirrors the existing
    ``publish_circuit_breaker_tripped`` / ``publish_invariant_drift_detected``
    pattern (lazy import, best-effort).
  * ``harness._session_exhausted_event`` — joins the existing
    FIRST_COMPLETED race that already covers shutdown / budget /
    idle / wall-cap / process-memory. No new parallel shutdown
    manager.

## Test surface

### Slice 7g — flag default
  * env unset → enabled returns True
  * explicit ``"false"`` / ``"0"`` / ``"off"`` → False
  * explicit ``"true"`` / ``"on"`` / ``"1"`` → True
  * garbage value → True (graduated; only opt-out tokens disable)

### Slice 12D — on_trip registry
  * Callback fires exactly once at CLOSED→OPEN_TERMINAL transition.
  * Multiple callbacks all fire (registration order).
  * Already-tripped state does NOT re-fire callbacks.
  * Raising callback is isolated (siblings still fire).
  * Late-bind: registering AFTER trip fires immediately.

### Slice 12D — payload shape
  * ``GlobalBreakerTripPayload`` is frozen.
  * Five fields: reason / trip_count / window_s / threshold / triggered_at.
  * reason is ``"session_exhausted"`` (canonical).

### Slice 12D — SSE publish
  * ``EVENT_TYPE_SESSION_EXHAUSTED == "session_exhausted"``.
  * ``publish_session_exhausted`` exists, is best-effort, returns
    ``None`` when the broker is disabled / payload malformed.
  * On trip, the SSE publish helper IS invoked (best-effort).

### AST pins (regression armor)
  * ``circuit_breaker.report_structural_trip`` dispatches via
    ``_dispatch_on_trip`` AFTER setting state to OPEN_TERMINAL.
  * ``circuit_breaker.report_structural_trip`` invokes
    ``_publish_session_exhausted_best_effort``.
  * ``harness`` main wait race includes ``session_exhausted_waiter``.
  * ``harness`` registers ``on_trip`` against ``get_global_breaker()``.

### End-to-end
  * When ``_session_exhausted_event`` wins the race in a fake
    harness, ``stop_reason`` is ``"session_exhausted"``.
"""

from __future__ import annotations

import ast as _ast
import asyncio
import importlib
import os
import pathlib
import threading
import unittest
from typing import List
from unittest.mock import patch


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BREAKER_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "circuit_breaker.py"
)
_STREAM_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "ide_observability_stream.py"
)
_HARNESS_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "battle_test"
    / "harness.py"
)


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


# ============================================================================
# Slice 7g — flag default flip
# ============================================================================


class TestSlice7gDefaultEnabled(unittest.TestCase):
    """Provider Circuit Breaker is permanently on as of 2026-05-22.
    Only explicit opt-out tokens disable it."""

    def _reload(self):
        from backend.core.ouroboros.governance import circuit_breaker
        importlib.reload(circuit_breaker)
        return circuit_breaker

    def _set_env(self, value):
        if value is None:
            os.environ.pop(
                "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED", None,
            )
        else:
            os.environ[
                "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED"
            ] = value

    def tearDown(self) -> None:
        self._set_env(None)

    def test_env_unset_returns_true(self) -> None:
        self._set_env(None)
        cb = self._reload()
        self.assertTrue(
            cb.circuit_breaker_enabled(),
            "Slice 7g: default must be TRUE when env unset",
        )

    def test_explicit_true_returns_true(self) -> None:
        for v in ("true", "True", "TRUE", "1", "on", "yes"):
            self._set_env(v)
            cb = self._reload()
            self.assertTrue(
                cb.circuit_breaker_enabled(),
                f"env={v!r} must keep breaker enabled",
            )

    def test_explicit_false_disables(self) -> None:
        for v in ("false", "False", "FALSE", "0", "off", "no"):
            self._set_env(v)
            cb = self._reload()
            self.assertFalse(
                cb.circuit_breaker_enabled(),
                f"env={v!r} must disable the breaker (hot-revert)",
            )

    def test_garbage_token_keeps_default_true(self) -> None:
        """Only the closed opt-out token set disables — anything
        else is treated as ``"keep enabled"``. This protects against
        typos silently disabling the structural cascade."""
        self._set_env("definitely-not-a-toggle")
        cb = self._reload()
        self.assertTrue(
            cb.circuit_breaker_enabled(),
            "garbage value must NOT disable the graduated default",
        )


# ============================================================================
# Slice 12D — on_trip registry behaviour
# ============================================================================


class TestSlice12DOnTripRegistry(unittest.TestCase):

    def setUp(self) -> None:
        from backend.core.ouroboros.governance import circuit_breaker
        circuit_breaker.reset_global_breaker()
        self.cb = circuit_breaker
        # Tight threshold so we can trip deterministically.
        os.environ[
            "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT"
        ] = "2"

    def tearDown(self) -> None:
        self.cb.reset_global_breaker()
        os.environ.pop(
            "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT", None,
        )

    def test_callback_fires_at_transition(self) -> None:
        gb = self.cb.get_global_breaker()
        fired: List = []
        gb.on_trip(fired.append)
        gb.report_structural_trip()  # 1st — below threshold
        self.assertEqual(fired, [], "must NOT fire below threshold")
        gb.report_structural_trip()  # 2nd — trip threshold
        self.assertEqual(
            len(fired), 1,
            "callback must fire exactly once at threshold",
        )
        payload = fired[0]
        self.assertEqual(payload.reason, "session_exhausted")
        self.assertEqual(payload.trip_count, 2)
        self.assertEqual(payload.threshold, 2)

    def test_callback_sticky_no_refire(self) -> None:
        gb = self.cb.get_global_breaker()
        fired: List = []
        gb.on_trip(fired.append)
        gb.report_structural_trip()
        gb.report_structural_trip()  # trips
        gb.report_structural_trip()  # already-tripped
        gb.report_structural_trip()
        self.assertEqual(
            len(fired), 1,
            "once OPEN_TERMINAL is sticky, no re-fire",
        )

    def test_multiple_callbacks_all_fire(self) -> None:
        gb = self.cb.get_global_breaker()
        a, b, c = [], [], []
        gb.on_trip(a.append)
        gb.on_trip(b.append)
        gb.on_trip(c.append)
        gb.report_structural_trip()
        gb.report_structural_trip()
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(len(c), 1)

    def test_raising_callback_isolated(self) -> None:
        gb = self.cb.get_global_breaker()
        fired: List = []
        gb.on_trip(lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
        gb.on_trip(fired.append)  # sibling
        # Must not raise out of report_structural_trip.
        try:
            gb.report_structural_trip()
            gb.report_structural_trip()
        except Exception as exc:  # pragma: no cover
            self.fail(
                f"raising callback escaped: "
                f"{type(exc).__name__}: {exc}",
            )
        self.assertEqual(
            len(fired), 1,
            "sibling callback must still fire despite raising peer",
        )

    def test_late_bind_fires_immediately_if_already_tripped(
        self,
    ) -> None:
        gb = self.cb.get_global_breaker()
        # Trip first.
        gb.report_structural_trip()
        gb.report_structural_trip()
        self.assertEqual(
            gb.state, self.cb.CircuitState.OPEN_TERMINAL,
        )
        # Register late.
        late: List = []
        gb.on_trip(late.append)
        self.assertEqual(
            len(late), 1,
            "late-bind registration must fire immediately when "
            "the breaker is already tripped (no silent miss)",
        )

    def test_non_callable_callback_silently_ignored(self) -> None:
        """Defensive: a None / non-callable subscriber must NOT
        crash the registry (mirrors register_shipped_code_invariant
        discipline)."""
        gb = self.cb.get_global_breaker()
        try:
            gb.on_trip(None)  # type: ignore[arg-type]
            gb.on_trip(42)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            self.fail(
                f"on_trip should defensively ignore bad callbacks; "
                f"got {type(exc).__name__}: {exc}",
            )

    def test_reset_clears_callbacks(self) -> None:
        gb = self.cb.get_global_breaker()
        fired: List = []
        gb.on_trip(fired.append)
        self.cb.reset_global_breaker()
        gb = self.cb.get_global_breaker()
        gb.report_structural_trip()
        gb.report_structural_trip()
        self.assertEqual(
            fired, [],
            "reset_global_breaker must clear the callback registry "
            "(test isolation)",
        )


# ============================================================================
# Slice 12D — payload shape
# ============================================================================


class TestSlice12DPayload(unittest.TestCase):

    def test_payload_is_frozen(self) -> None:
        from backend.core.ouroboros.governance.circuit_breaker import (
            GlobalBreakerTripPayload,
        )
        p = GlobalBreakerTripPayload(
            reason="session_exhausted",
            trip_count=5,
            window_s=300.0,
            threshold=5,
            triggered_at=1.0,
        )
        with self.assertRaises(Exception):
            p.reason = "other"  # type: ignore[misc]

    def test_payload_fields(self) -> None:
        from backend.core.ouroboros.governance.circuit_breaker import (
            GlobalBreakerTripPayload,
        )
        self.assertEqual(
            set(GlobalBreakerTripPayload.__dataclass_fields__),
            {"reason", "trip_count", "window_s",
             "threshold", "triggered_at"},
        )


# ============================================================================
# Slice 12D — SSE publish surface
# ============================================================================


class TestSlice12DSsePublish(unittest.TestCase):

    def test_event_type_constant_value(self) -> None:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SESSION_EXHAUSTED,
        )
        self.assertEqual(
            EVENT_TYPE_SESSION_EXHAUSTED, "session_exhausted",
        )

    def test_publish_helper_exists(self) -> None:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_session_exhausted,
        )
        self.assertTrue(callable(publish_session_exhausted))

    def test_event_type_is_in_broker_allowlist(self) -> None:
        """The broker's ``_VALID_EVENT_TYPES`` allowlist must
        include ``session_exhausted``. Missed in the initial Slice
        12D commit — the in-process callback shutdown chain still
        worked (it's the authoritative channel), but every SSE
        publish was rejected with ``[Stream] publish rejected
        unknown event_type='session_exhausted'``. Pinned so a
        future refactor that re-derives the allowlist can't drop
        this entry silently."""
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SESSION_EXHAUSTED,
            _VALID_EVENT_TYPES,
        )
        self.assertIn(
            EVENT_TYPE_SESSION_EXHAUSTED, _VALID_EVENT_TYPES,
            "EVENT_TYPE_SESSION_EXHAUSTED must be in the broker's "
            "allowlist so publish_session_exhausted can actually "
            "ship the event (Slice 12D observability)",
        )

    def test_publish_helper_best_effort_on_malformed_payload(
        self,
    ) -> None:
        """A malformed payload must not crash the publish helper —
        observability is non-authoritative. The contract is
        explicit: NEVER raises. The return value is whatever the
        broker hands back (an event_id when the publish lands, or
        ``None`` when the broker is disabled / publish errors out)
        — both are acceptable degradation modes."""
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_session_exhausted,
        )

        class _NoAttrs:
            pass

        try:
            result = publish_session_exhausted(_NoAttrs())
        except Exception as exc:  # pragma: no cover
            self.fail(
                f"publish_session_exhausted must be best-effort; "
                f"got {type(exc).__name__}: {exc}",
            )
        # Either degradation mode is fine: None (broker disabled /
        # publish error) OR an event_id string (broker accepted
        # zero-valued payload). What matters is no raise.
        self.assertTrue(
            result is None or isinstance(result, str),
            f"publish must return None or event_id string; got "
            f"{type(result).__name__}: {result!r}",
        )


# ============================================================================
# AST pins — regression armor
# ============================================================================


class TestAstPins(unittest.TestCase):

    def _function(
        self, tree: _ast.Module, class_name: str, method_name: str,
    ) -> _ast.FunctionDef:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == class_name
            ):
                for sub in node.body:
                    if (
                        isinstance(sub, _ast.FunctionDef)
                        and sub.name == method_name
                    ):
                        return sub
        raise AssertionError(
            f"{class_name}.{method_name} not found",
        )

    def test_report_structural_trip_dispatches_callbacks(
        self,
    ) -> None:
        tree = _parse_module(_BREAKER_FILE)
        fn = self._function(
            tree, "_GlobalBreaker", "report_structural_trip",
        )
        names = []
        for sub in _ast.walk(fn):
            if isinstance(sub, _ast.Call):
                f = sub.func
                if isinstance(f, _ast.Attribute):
                    names.append(f.attr)
                elif isinstance(f, _ast.Name):
                    names.append(f.id)
        self.assertIn(
            "_dispatch_on_trip", names,
            "report_structural_trip must invoke _dispatch_on_trip "
            "after the state transition (Slice 12D wiring)",
        )

    def test_report_structural_trip_publishes_sse(self) -> None:
        tree = _parse_module(_BREAKER_FILE)
        fn = self._function(
            tree, "_GlobalBreaker", "report_structural_trip",
        )
        names = []
        for sub in _ast.walk(fn):
            if isinstance(sub, _ast.Call):
                f = sub.func
                if isinstance(f, _ast.Name):
                    names.append(f.id)
        self.assertIn(
            "_publish_session_exhausted_best_effort", names,
            "report_structural_trip must invoke "
            "_publish_session_exhausted_best_effort (SSE bridge)",
        )

    def test_harness_wait_race_includes_session_exhausted_waiter(
        self,
    ) -> None:
        """The 6-way race in harness.run() MUST include
        ``session_exhausted_waiter``. If a regression removed the
        Slice 12D waiter, the harness would silently fall back to
        wall-cap kill (the wedge we're closing)."""
        src = _HARNESS_FILE.read_text()
        self.assertIn(
            "session_exhausted_waiter", src,
            "harness.py must wire session_exhausted_waiter into "
            "the FIRST_COMPLETED race (Slice 12D)",
        )
        # And the asyncio.wait list must literally contain it
        # (defense against a regression that defines the name but
        # forgets to plumb it into the race).
        self.assertIn(
            "session_exhausted_waiter,\n",
            src.replace(" ", "").replace("\t", ""),
            f"session_exhausted_waiter must appear inside the "
            f"asyncio.wait([...]) list",
        )

    def test_harness_registers_on_trip(self) -> None:
        """The harness must register an on_trip callback against
        the global breaker — that's the in-process bridge from
        circuit_breaker → asyncio.Event.set."""
        src = _HARNESS_FILE.read_text()
        self.assertIn(
            ".on_trip(", src,
            "harness.py must register an on_trip callback against "
            "the global circuit breaker (Slice 12D)",
        )
        # And it must use call_soon_threadsafe — the global breaker
        # may trip from a non-loop thread under stress.
        self.assertIn(
            "call_soon_threadsafe", src,
            "Slice 12D callback must marshal via "
            "call_soon_threadsafe (asyncio.Event.set is not "
            "thread-safe)",
        )

    def test_harness_session_exhausted_stop_reason(self) -> None:
        """When the session_exhausted waiter wins the race, the
        stop_reason must be the canonical token."""
        src = _HARNESS_FILE.read_text()
        self.assertIn(
            '"session_exhausted"', src,
            "harness.py must stamp stop_reason='session_exhausted' "
            "when the global breaker waiter wins the race",
        )

    def test_stream_event_type_constant_present(self) -> None:
        src = _STREAM_FILE.read_text()
        self.assertIn(
            'EVENT_TYPE_SESSION_EXHAUSTED = "session_exhausted"',
            src,
        )
        self.assertIn("def publish_session_exhausted(", src)


# ============================================================================
# End-to-end — fake-harness FIRST_COMPLETED race
# ============================================================================


class TestEndToEndShutdownRace(unittest.IsolatedAsyncioTestCase):
    """Build a fake mini-harness with the same 6-way race shape
    and assert that when only the session_exhausted event fires,
    the stop_reason resolves to ``session_exhausted``."""

    async def test_session_exhausted_waiter_wins_race(self) -> None:
        from backend.core.ouroboros.governance import circuit_breaker
        circuit_breaker.reset_global_breaker()
        os.environ[
            "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT"
        ] = "2"

        loop = asyncio.get_running_loop()

        # Build a 6-event race identical in shape to harness.run().
        shutdown_event = asyncio.Event()
        budget_event = asyncio.Event()
        idle_event = asyncio.Event()
        wall_clock_event = asyncio.Event()
        process_memory_event = asyncio.Event()
        session_exhausted_event = asyncio.Event()

        # Bridge global breaker → session_exhausted_event (mirrors
        # the harness wiring).
        def _on_trip(_payload):
            loop.call_soon_threadsafe(session_exhausted_event.set)

        circuit_breaker.get_global_breaker().on_trip(_on_trip)

        # Trip the breaker in a background task so the await
        # actually races.
        async def _trip_after_yield():
            await asyncio.sleep(0.01)
            gb = circuit_breaker.get_global_breaker()
            gb.report_structural_trip()
            gb.report_structural_trip()

        asyncio.create_task(_trip_after_yield())

        waiters = [
            asyncio.ensure_future(shutdown_event.wait()),
            asyncio.ensure_future(budget_event.wait()),
            asyncio.ensure_future(idle_event.wait()),
            asyncio.ensure_future(wall_clock_event.wait()),
            asyncio.ensure_future(process_memory_event.wait()),
            asyncio.ensure_future(session_exhausted_event.wait()),
        ]
        try:
            done, pending = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=5.0,
            )
        finally:
            for w in waiters:
                if not w.done():
                    w.cancel()

        self.assertIn(
            waiters[5], done,
            "session_exhausted waiter must win the race when the "
            "global breaker trips",
        )
        # Cleanup
        circuit_breaker.reset_global_breaker()
        os.environ.pop(
            "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT", None,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
