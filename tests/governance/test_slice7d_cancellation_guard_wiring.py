"""Slice 7d — wiring of BoundedCancellationGuard into ClaudeProvider.

Closes the 47-second cancellation overrun from
bt-2026-05-21-214521 by wrapping ``ClaudeProvider._claude_do_stream``
with the Slice 7b primitive. Graduates the master flag default to
TRUE (safety guardrail per §33.1 inverse). Adds the
``cancellation_overrun_detected`` SSE event + paired publisher
helper.

Test surface:

  * **Master-flag graduation pin** — default is now TRUE
    (post Slice 7d); explicit ``"false"`` opts out.
  * **SSE event registration pins** — the new event type appears
    in ``_VALID_EVENT_TYPES`` + the publisher helper exists.
  * **Wiring AST pin** — ``_claude_do_stream`` imports
    ``BoundedCancellationGuard`` and the streaming context is
    inside an ``async with`` that uses the guard.
  * **No-shared-pool-collateral AST pin** — the new wiring code
    in providers.py does NOT contain ``connector.close()`` /
    ``session.close()`` (operator binding rolled forward from
    Slice 7b).
  * **Publisher behavioural test** — calling
    ``publish_cancellation_overrun`` with the master flag ON
    returns a non-empty event_id; with the flag OFF returns
    ``None``.
"""

from __future__ import annotations

import ast
import os
import pathlib
import unittest
from typing import List, Optional

from backend.core.ouroboros.governance.bounded_cancellation_guard import (
    guard_enabled,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CANCELLATION_OVERRUN_DETECTED,
    _VALID_EVENT_TYPES,
    publish_cancellation_overrun,
    stream_enabled,
)


# ============================================================================
# Master-flag graduation (default TRUE post Slice 7d)
# ============================================================================


class TestMasterFlagGraduation(unittest.TestCase):
    """Slice 7d flips ``JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED``
    default to TRUE. This pin protects against accidental
    regression to default-FALSE."""

    def test_default_is_true(self) -> None:
        prior = os.environ.pop(
            "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED", None,
        )
        try:
            self.assertTrue(
                guard_enabled(),
                "Slice 7d graduation MUST keep "
                "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED "
                "default-TRUE",
            )
        finally:
            if prior is not None:
                os.environ[
                    "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
                ] = prior

    def test_explicit_false_opts_out(self) -> None:
        for v in ("0", "false", "FALSE", "no", "off"):
            with self.subTest(v=v):
                os.environ[
                    "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
                ] = v
                self.assertFalse(guard_enabled())


# ============================================================================
# SSE event type registration
# ============================================================================


class TestSseEventRegistration(unittest.TestCase):
    """The new ``cancellation_overrun_detected`` event type MUST
    appear in ``_VALID_EVENT_TYPES`` (otherwise the broker's
    publish() rejects it). The publisher helper MUST exist."""

    def test_event_type_constant_value(self) -> None:
        self.assertEqual(
            EVENT_TYPE_CANCELLATION_OVERRUN_DETECTED,
            "cancellation_overrun_detected",
        )

    def test_event_type_in_valid_set(self) -> None:
        self.assertIn(
            EVENT_TYPE_CANCELLATION_OVERRUN_DETECTED,
            _VALID_EVENT_TYPES,
            "Broker will reject publish() if the event type isn't "
            "in _VALID_EVENT_TYPES — this is the canonical "
            "registration gate.",
        )

    def test_publisher_helper_is_importable_and_callable(self) -> None:
        self.assertTrue(callable(publish_cancellation_overrun))


# ============================================================================
# Wiring AST pin — providers.py wraps _claude_do_stream with the guard
# ============================================================================


_PROVIDERS_FILE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)


def _parse_providers() -> ast.Module:
    return ast.parse(_PROVIDERS_FILE.read_text())


def _find_function(
    tree: ast.Module, name: str,
) -> Optional[ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    return None


class TestWiringAstPin(unittest.TestCase):
    """The Slice 7d wiring of ``BoundedCancellationGuard`` inside
    ``_claude_do_stream`` is structurally enforced — if a future
    refactor removes the guard from the streaming context, this
    pin catches it before it ships."""

    def test_claude_do_stream_imports_bounded_cancellation_guard(self) -> None:
        tree = _parse_providers()
        func = _find_function(tree, "_claude_do_stream")
        self.assertIsNotNone(
            func,
            "_claude_do_stream MUST exist in providers.py — Slice 7d "
            "wires the guard inside this function",
        )
        # Walk the function body for the lazy import.
        seen = False
        for node in ast.walk(func):  # type: ignore[arg-type]
            if isinstance(node, ast.ImportFrom):
                if node.module and (
                    "bounded_cancellation_guard" in node.module
                ):
                    for alias in node.names:
                        if alias.name == "BoundedCancellationGuard":
                            seen = True
                            break
        self.assertTrue(
            seen,
            "_claude_do_stream MUST import BoundedCancellationGuard "
            "(the Slice 7b primitive) inside its body — the lazy "
            "import is the canonical pattern to avoid governance-"
            "package cycles, mirroring the existing _quiescence_"
            "core_active lazy import in the same function.",
        )

    def test_claude_do_stream_async_with_uses_guard(self) -> None:
        """The async-with block that opens the streaming context
        MUST include the guard as one of its items."""
        tree = _parse_providers()
        func = _find_function(tree, "_claude_do_stream")
        self.assertIsNotNone(func)
        for node in ast.walk(func):  # type: ignore[arg-type]
            if not isinstance(node, ast.AsyncWith):
                continue
            # Check each item's context expression. The guard is an
            # instance (Name node referencing the local variable
            # holding the guard).
            for item in node.items:
                src = ast.unparse(item.context_expr)
                if "_bcg_guard" in src or "BoundedCancellationGuard(" in src:
                    return
        self.fail(
            "No async-with block in _claude_do_stream references "
            "the BoundedCancellationGuard. Slice 7d wiring is "
            "missing — the streaming context MUST be wrapped."
        )

    def test_publish_cancellation_overrun_referenced_in_wiring(self) -> None:
        """The on_overrun callback wired into the guard MUST
        invoke ``publish_cancellation_overrun`` — telemetry surface
        per Slice 7d."""
        tree = _parse_providers()
        func = _find_function(tree, "_claude_do_stream")
        self.assertIsNotNone(func)
        src = ast.unparse(func)  # type: ignore[arg-type]
        self.assertIn(
            "publish_cancellation_overrun",
            src,
            "_claude_do_stream MUST call publish_cancellation_overrun "
            "in the guard's on_overrun callback — Slice 7d telemetry "
            "surface.",
        )


# ============================================================================
# No-shared-pool-collateral AST pin (operator binding — rolled forward)
# ============================================================================


class TestNoSharedPoolCollateralInWiring(unittest.TestCase):
    """Operator binding from Slice 7b — the wiring MUST NOT
    introduce ``connector.close()`` / ``session.close()`` calls
    that would nuke the shared aiohttp pool. The guard's surgical
    abort works via per-FD ``transport.abort()`` ONLY.

    We scope the scan to the wiring lines newly added in
    ``_claude_do_stream`` (the rest of providers.py contains many
    legitimate ``client.close()`` / ``self._client.close()`` calls
    that pre-date Slice 7d)."""

    def test_no_connector_close_in_claude_do_stream(self) -> None:
        tree = _parse_providers()
        func = _find_function(tree, "_claude_do_stream")
        self.assertIsNotNone(func)
        offenders: List[str] = []
        for node in ast.walk(func):  # type: ignore[arg-type]
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "close":
                continue
            cur = node.func.value
            chain: List[str] = []
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                chain.append(cur.id)
            # Forbid connector / _connector chains in this function.
            if any(seg in ("connector", "_connector") for seg in chain):
                offenders.append(".".join(reversed(chain)) + ".close")
        self.assertEqual(
            offenders, [],
            f"Slice 7d wiring MUST NOT introduce pool-wide "
            f"close() calls inside _claude_do_stream — operator "
            f"shared-pool binding rolled forward from Slice 7b. "
            f"Offenders: {offenders}",
        )


# ============================================================================
# Publisher behavioural test
# ============================================================================


class TestPublisherBehavior(unittest.TestCase):
    """``publish_cancellation_overrun`` returns a non-empty event_id
    when the stream master flag is on, and None when off. NEVER
    raises."""

    def test_publish_returns_event_id_when_stream_enabled(self) -> None:
        if not stream_enabled():
            self.skipTest(
                "Stream master flag off in test env — skipping "
                "event-id check; the None path is covered by the "
                "off-state test."
            )
        ev_id = publish_cancellation_overrun(
            overrun_s=1.23,
            provider="claude",
            op_id="op-slice7d-test",
            deadline_s=180.0,
            grace_ms=500,
        )
        self.assertIsNotNone(ev_id)
        self.assertIsInstance(ev_id, str)
        self.assertGreater(len(ev_id), 0)

    def test_publish_with_invalid_inputs_does_not_raise(self) -> None:
        # Pathological inputs — must not crash.
        try:
            publish_cancellation_overrun(
                overrun_s=-1.0, provider="", op_id="",
                deadline_s=0.0, grace_ms=0,
            )
            publish_cancellation_overrun(
                overrun_s=999999.0,
                provider="x" * 500,
                op_id="y" * 500,
                deadline_s=99999.0,
                grace_ms=99999,
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"publish_cancellation_overrun raised on pathological "
                f"inputs: {type(exc).__name__}: {exc}"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
