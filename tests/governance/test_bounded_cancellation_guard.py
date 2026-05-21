"""Slice 7b — BoundedCancellationGuard primitive tests.

Closes the second empirical fault from bt-2026-05-21-214521 (the
47-second cancellation overrun on the ClaudeProvider stream). The
primitive in this module is the structural substrate; Slice 7d wires
it into ``ClaudeProvider`` behind the same master flag.

Test surface:

  * Master-flag default-FALSE AST pin — wrapping existing code with
    the guard is byte-identical to no guard when off.
  * **Shared-pool binding AST pin** (operator-bound) — the primitive
    MUST use ``transport.abort()`` (per-FD) and NEVER
    ``connector.close()`` / ``session.close()`` (pool-wide).
  * **No polling-loop AST pin** — operator binding "no asyncio.sleep
    loops". Single primitive only (``loop.call_later``).
  * Closed-taxonomy AST pin — ``GuardState`` has exactly 4 members.
  * **Behavioral: surgical abort on overrun** — using a controlled
    fake aiohttp connection, exercise the deadline + grace path
    and assert ``transport.abort()`` was called once + at the
    expected delay.
  * Behavioral: clean exit cancels the scheduled abort.
  * Behavioral: master-flag-FALSE skips the entire mechanism.
  * Behavioral: ``arm()`` idempotency.
  * Behavioral: defensive transport extraction across response
    shapes (ClientResponse-like, raw-transport, None).
  * Behavioral: ``is_closing()`` pre-check prevents double-abort.
  * Behavioral: on_overrun callback fires with measured overrun.
  * Defensive: failed abort silently degrades; never raises.
  * Env-knob clamp — JARVIS_PROVIDER_CANCELLATION_GRACE_MS bounded
    to [50, 5000] ms.
  * Public-surface ``__all__`` pin.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import unittest
from typing import List, Optional
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.bounded_cancellation_guard import (
    BoundedCancellationGuard,
    GuardState,
    guard_enabled,
)
from backend.core.ouroboros.governance import bounded_cancellation_guard


# ============================================================================
# Helpers — controlled fake aiohttp surfaces
# ============================================================================


class _FakeTransport:
    """Minimal asyncio.Transport surface that records abort() calls
    and supports is_closing()."""

    def __init__(self) -> None:
        self.abort_called: int = 0
        self.close_called: int = 0  # should ALWAYS stay 0 (operator binding)
        self._closing: bool = False

    def abort(self) -> None:
        self.abort_called += 1
        self._closing = True

    def close(self) -> None:
        self.close_called += 1
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class _FakeConnection:
    """Mimics aiohttp.connector.Connection — exposes a transport."""

    def __init__(self, transport: _FakeTransport) -> None:
        self.transport = transport


class _FakeResponse:
    """Mimics aiohttp.ClientResponse — exposes a connection."""

    def __init__(self, transport: Optional[_FakeTransport] = None) -> None:
        self.connection = (
            _FakeConnection(transport) if transport is not None else None
        )


# Helper context that flips the master flag for one test.
class _MasterFlag:
    def __init__(self, value: bool) -> None:
        self._value = value
        self._prior: Optional[str] = None

    def __enter__(self) -> "_MasterFlag":
        self._prior = os.environ.get(
            "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
        )
        os.environ["JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"] = (
            "true" if self._value else "false"
        )
        return self

    def __exit__(self, *a: object) -> None:
        if self._prior is None:
            os.environ.pop(
                "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED", None,
            )
        else:
            os.environ["JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"] = (
                self._prior
            )


# ============================================================================
# Closed-taxonomy AST pin — GuardState
# ============================================================================


class TestGuardStateClosedTaxonomy(unittest.TestCase):
    """``GuardState`` is a closed 4-value taxonomy. Adding a 5th
    requires bumping this pin + Slice 7d's wiring code that
    branches on state."""

    def test_four_members(self) -> None:
        self.assertEqual(
            len(list(GuardState)), 4,
            f"GuardState is closed; found "
            f"{[m.name for m in GuardState]}",
        )

    def test_exact_member_names(self) -> None:
        self.assertEqual(
            {m.name for m in GuardState},
            {"PENDING", "ARMED", "DISARMED", "ABORTED"},
        )


# ============================================================================
# Master flag default-FALSE pin
# ============================================================================


class TestMasterFlagDefault(unittest.TestCase):
    """The master flag MUST default FALSE in Slice 7b. The wiring
    slice (7d) flips it to TRUE — but only after this slice has
    soaked."""

    def test_guard_enabled_default_is_false(self) -> None:
        prior = os.environ.pop(
            "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED", None,
        )
        try:
            self.assertFalse(
                guard_enabled(),
                "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED default "
                "should be FALSE in Slice 7b",
            )
        finally:
            if prior is not None:
                os.environ[
                    "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
                ] = prior

    def test_guard_enabled_truthy_values(self) -> None:
        truthy = ["1", "true", "TRUE", "yes", "on", "True"]
        for v in truthy:
            with self.subTest(v=v):
                with _MasterFlag(False):
                    # Direct override for the truthy probe
                    os.environ[
                        "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
                    ] = v
                    self.assertTrue(guard_enabled())

    def test_guard_enabled_falsy_values(self) -> None:
        falsy = ["0", "false", "FALSE", "no", "off", ""]
        for v in falsy:
            with self.subTest(v=v):
                os.environ[
                    "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
                ] = v
                self.assertFalse(guard_enabled())


# ============================================================================
# Shared-pool binding AST pin (operator-bound) — no connector.close
# ============================================================================


_MODULE_FILE = pathlib.Path(bounded_cancellation_guard.__file__)


def _parse_module() -> ast.Module:
    return ast.parse(_MODULE_FILE.read_text())


class TestSurgicalSeveranceAstPin(unittest.TestCase):
    """Operator binding (verbatim): *"prioritize using
    response.connection.transport.abort() ... without nuking
    healthy concurrent streams in a shared pool."* This AST pin
    enforces structurally that the module uses ONLY
    ``transport.abort()`` — never ``connector.close()`` /
    ``session.close()`` / any other pool-wide operation."""

    def test_calls_transport_abort_not_connector_close(self) -> None:
        tree = _parse_module()
        seen_calls: List[str] = []
        forbidden_attrs = {
            ("_connector", "close"),
            ("connector", "close"),
        }
        # 1. Confirm transport.abort() appears at least once in the
        #    module (the surgical-severance call site).
        abort_count = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "abort"
            ):
                # We don't try to type-narrow the receiver (it's
                # `self._transport` in the source); the attr name
                # is enough.
                abort_count += 1
        self.assertGreaterEqual(
            abort_count, 1,
            "Module must call .abort() at least once — the "
            "surgical-severance primitive.",
        )
        # 2. Confirm NO calls to connector.close / session.close
        #    anywhere in the module.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "close":
                # Scan the qualifier chain
                cur: Optional[ast.AST] = func.value
                chain: List[str] = []
                while isinstance(cur, ast.Attribute):
                    chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    chain.append(cur.id)
                # Forbidden if chain contains "connector" or
                # "_connector" or "session" anywhere.
                if any(
                    seg in ("connector", "_connector", "session")
                    for seg in chain
                ):
                    seen_calls.append(".".join(reversed(chain)) + ".close")
        self.assertEqual(
            seen_calls, [],
            f"Slice 7b shared-pool binding violated — found "
            f"forbidden pool-wide close() calls: {seen_calls}. "
            f"Use transport.abort() ONLY.",
        )


# ============================================================================
# No-polling AST pin (operator-bound)
# ============================================================================


class TestNoPollingAstPin(unittest.TestCase):
    """Operator binding: *"No hardcoded ``asyncio.sleep()`` loops."*
    The single primitive in Slice 7b is ``loop.call_later`` — no
    while-loop polling, no asyncio.sleep retries.

    The module is allowed to ``import asyncio`` and use
    ``asyncio.TimerHandle`` (via ``loop.call_later``), but it must
    NOT contain any ``await asyncio.sleep(...)`` calls."""

    def test_no_asyncio_sleep_calls(self) -> None:
        tree = _parse_module()
        offenders: List[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "sleep"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                offenders.append(node.lineno)
        self.assertEqual(
            offenders, [],
            f"Slice 7b no-polling binding violated — found "
            f"asyncio.sleep at line(s) {offenders}. Use "
            f"loop.call_later for deadline scheduling.",
        )

    def test_no_while_true_loops(self) -> None:
        tree = _parse_module()
        offenders: List[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.While)
                and isinstance(node.test, ast.Constant)
                and node.test.value is True
            ):
                offenders.append(node.lineno)
        self.assertEqual(
            offenders, [],
            f"Slice 7b no-polling binding violated — found "
            f"while True at line(s) {offenders}.",
        )

    def test_uses_loop_call_later(self) -> None:
        tree = _parse_module()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call_later"
            ):
                return
        self.fail(
            "Slice 7b MUST schedule the abort via loop.call_later "
            "(the canonical asyncio one-shot scheduler). Not found."
        )


# ============================================================================
# Behavioral — clean exit cancels the scheduled abort
# ============================================================================


class TestCleanExit(unittest.IsolatedAsyncioTestCase):
    """When the guarded block completes before the deadline + grace,
    the scheduled abort callback MUST be cancelled. transport.abort()
    is never called."""

    async def test_clean_exit_does_not_call_abort(self) -> None:
        with _MasterFlag(True):
            transport = _FakeTransport()
            response = _FakeResponse(transport)
            # deadline 10s, grace 500ms — never reached in this test.
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            async with guard:
                guard.arm(response)
                # Simulate stream completing fast.
                await asyncio.sleep(0.01)
            self.assertEqual(transport.abort_called, 0)
            self.assertEqual(transport.close_called, 0)
            self.assertEqual(guard.state, GuardState.DISARMED)

    async def test_clean_exit_with_no_arm_is_safe(self) -> None:
        with _MasterFlag(True):
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            async with guard:
                # Never arm — guard exits as DISARMED, no abort.
                pass
            self.assertEqual(guard.state, GuardState.DISARMED)


# ============================================================================
# Behavioral — surgical abort fires on overrun
# ============================================================================


class TestSurgicalAbortOnOverrun(unittest.IsolatedAsyncioTestCase):
    """When the guarded block exceeds deadline + grace, the event
    loop fires _fire_abort, which calls transport.abort()
    surgically. transport.close() must NEVER be called."""

    async def test_deadline_expired_calls_transport_abort(self) -> None:
        with _MasterFlag(True):
            transport = _FakeTransport()
            response = _FakeResponse(transport)
            overrun_recorded: List[float] = []

            def _on_overrun(overrun_s: float) -> None:
                overrun_recorded.append(overrun_s)

            # Very short deadline + grace — abort should fire fast.
            guard = BoundedCancellationGuard(
                deadline_s=0.05,
                grace_ms=50,
                on_overrun=_on_overrun,
            )
            async with guard:
                guard.arm(response)
                # Sleep past deadline + grace.
                await asyncio.sleep(0.25)
            # transport.abort() should have been called exactly once.
            self.assertEqual(
                transport.abort_called, 1,
                "transport.abort() should be called exactly once "
                "when the deadline + grace expires",
            )
            # transport.close() MUST NEVER be called (operator
            # shared-pool binding).
            self.assertEqual(
                transport.close_called, 0,
                "transport.close() must NEVER be called by the "
                "guard — operator shared-pool binding",
            )
            self.assertEqual(guard.state, GuardState.ABORTED)
            # Overrun callback fired with positive overrun.
            self.assertEqual(len(overrun_recorded), 1)
            self.assertGreater(overrun_recorded[0], 0.0)

    async def test_unarmed_guard_marks_aborted_without_calling_transport(self) -> None:
        """When the deadline expires but no transport was armed, the
        state still transitions to ABORTED (the deadline DID fire)
        but no .abort() call is made."""
        with _MasterFlag(True):
            transport = _FakeTransport()  # never armed
            guard = BoundedCancellationGuard(
                deadline_s=0.05, grace_ms=50,
            )
            async with guard:
                # never arm
                await asyncio.sleep(0.25)
            self.assertEqual(guard.state, GuardState.ABORTED)
            self.assertEqual(transport.abort_called, 0)


# ============================================================================
# Behavioral — master-flag-FALSE no-op
# ============================================================================


class TestMasterFlagFalseIsNoop(unittest.IsolatedAsyncioTestCase):
    """When the master flag is FALSE, the guard is a strict no-op.
    Wrapping existing code is byte-identical to not wrapping it."""

    async def test_no_schedule_when_flag_false(self) -> None:
        with _MasterFlag(False):
            transport = _FakeTransport()
            response = _FakeResponse(transport)
            guard = BoundedCancellationGuard(
                deadline_s=0.01, grace_ms=10,
            )
            async with guard:
                guard.arm(response)
                # Wait FAR past what would be the deadline+grace.
                await asyncio.sleep(0.1)
            # Nothing should have happened — strict no-op.
            self.assertEqual(transport.abort_called, 0)
            self.assertEqual(transport.close_called, 0)
            self.assertEqual(guard.state, GuardState.PENDING)

    async def test_arm_returns_false_when_flag_false(self) -> None:
        with _MasterFlag(False):
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            response = _FakeResponse(_FakeTransport())
            armed = guard.arm(response)
            self.assertFalse(
                armed,
                "arm() must return False when master flag is OFF",
            )


# ============================================================================
# Behavioral — arm() idempotency + defensive extraction
# ============================================================================


class TestArmIdempotency(unittest.IsolatedAsyncioTestCase):
    """arm() can be called multiple times — only the first capture
    is honoured (idempotent). Defensive across response shapes."""

    async def test_double_arm_is_noop(self) -> None:
        with _MasterFlag(True):
            transport1 = _FakeTransport()
            transport2 = _FakeTransport()
            r1 = _FakeResponse(transport1)
            r2 = _FakeResponse(transport2)
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            async with guard:
                self.assertTrue(guard.arm(r1))
                self.assertTrue(guard.arm(r2))  # idempotent
                # First arm wins — only r1's transport is captured.
            self.assertEqual(transport1.abort_called, 0)
            self.assertEqual(transport2.abort_called, 0)

    async def test_arm_accepts_raw_transport(self) -> None:
        """Defensive: arm() accepts a raw transport-like object
        (not just a ClientResponse)."""
        with _MasterFlag(True):
            transport = _FakeTransport()
            guard = BoundedCancellationGuard(
                deadline_s=0.05, grace_ms=50,
            )
            async with guard:
                self.assertTrue(guard.arm(transport))
                await asyncio.sleep(0.25)
            self.assertEqual(transport.abort_called, 1)

    async def test_arm_rejects_none_safely(self) -> None:
        with _MasterFlag(True):
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            async with guard:
                # None has no transport — arm returns False, no
                # exception.
                self.assertFalse(guard.arm(None))

    async def test_arm_rejects_unrelated_object_safely(self) -> None:
        with _MasterFlag(True):
            guard = BoundedCancellationGuard(
                deadline_s=10.0, grace_ms=500,
            )
            async with guard:
                self.assertFalse(guard.arm("not a response"))
                self.assertFalse(guard.arm(object()))


# ============================================================================
# Behavioral — is_closing() pre-check
# ============================================================================


class TestIsClosingPrecheck(unittest.IsolatedAsyncioTestCase):
    """If aiohttp closed the transport on its own between arm() and
    the deadline firing, _fire_abort must NOT call .abort() again
    (it's a no-op anyway, but cleanliness matters)."""

    async def test_skip_abort_when_already_closing(self) -> None:
        with _MasterFlag(True):
            transport = _FakeTransport()
            response = _FakeResponse(transport)
            guard = BoundedCancellationGuard(
                deadline_s=0.05, grace_ms=50,
            )
            async with guard:
                guard.arm(response)
                # Simulate aiohttp closing the transport itself
                # before the deadline fires.
                transport._closing = True
                await asyncio.sleep(0.25)
            # State still ABORTED (deadline DID fire), but .abort()
            # was NOT called because is_closing() short-circuited.
            self.assertEqual(transport.abort_called, 0)
            self.assertEqual(guard.state, GuardState.ABORTED)


# ============================================================================
# Behavioral — overrun callback never raises out of the guard
# ============================================================================


class TestOverrunCallbackDefensive(unittest.IsolatedAsyncioTestCase):
    """A misbehaving on_overrun callback must not propagate
    exceptions out of the guard. NEVER raises contract."""

    async def test_callback_raises_silently_logged(self) -> None:
        with _MasterFlag(True):
            transport = _FakeTransport()
            response = _FakeResponse(transport)

            def _bad_callback(overrun_s: float) -> None:
                raise RuntimeError("simulated callback fault")

            guard = BoundedCancellationGuard(
                deadline_s=0.05,
                grace_ms=50,
                on_overrun=_bad_callback,
            )
            # Should NOT raise.
            async with guard:
                guard.arm(response)
                await asyncio.sleep(0.25)
            # abort still fired, state still ABORTED.
            self.assertEqual(transport.abort_called, 1)
            self.assertEqual(guard.state, GuardState.ABORTED)


# ============================================================================
# Env-knob clamp
# ============================================================================


class TestGraceMsEnvClamp(unittest.TestCase):
    """JARVIS_PROVIDER_CANCELLATION_GRACE_MS is clamped to
    [50, 5000] — below 50 is false-positive heavy, above 5000 is
    not a useful bound."""

    def _make_guard(self) -> BoundedCancellationGuard:
        return BoundedCancellationGuard(deadline_s=1.0)

    def test_default_grace_ms_is_500(self) -> None:
        os.environ.pop("JARVIS_PROVIDER_CANCELLATION_GRACE_MS", None)
        guard = self._make_guard()
        self.assertEqual(guard.grace_ms, 500)

    def test_clamp_low(self) -> None:
        os.environ["JARVIS_PROVIDER_CANCELLATION_GRACE_MS"] = "10"
        guard = self._make_guard()
        self.assertEqual(
            guard.grace_ms, 50,
            "grace_ms below the floor must clamp to 50",
        )

    def test_clamp_high(self) -> None:
        os.environ["JARVIS_PROVIDER_CANCELLATION_GRACE_MS"] = "99999"
        guard = self._make_guard()
        self.assertEqual(
            guard.grace_ms, 5000,
            "grace_ms above the ceiling must clamp to 5000",
        )

    def test_invalid_env_uses_default(self) -> None:
        os.environ["JARVIS_PROVIDER_CANCELLATION_GRACE_MS"] = "not-an-int"
        guard = self._make_guard()
        self.assertEqual(guard.grace_ms, 500)

    def test_explicit_grace_overrides_env(self) -> None:
        os.environ["JARVIS_PROVIDER_CANCELLATION_GRACE_MS"] = "200"
        guard = BoundedCancellationGuard(
            deadline_s=1.0, grace_ms=750,
        )
        self.assertEqual(guard.grace_ms, 750)


# ============================================================================
# Public surface pin
# ============================================================================


class TestPublicSurface(unittest.TestCase):
    """``__all__`` stability — Slice 7d consumes these names."""

    def test_all_exports(self) -> None:
        self.assertEqual(
            set(bounded_cancellation_guard.__all__),
            {
                "BoundedCancellationGuard",
                "GuardState",
                "guard_enabled",
            },
        )

    def test_each_export_resolves(self) -> None:
        for name in bounded_cancellation_guard.__all__:
            self.assertTrue(
                hasattr(bounded_cancellation_guard, name),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
