"""Slice 7e — wiring of CircuitBreaker into CandidateGenerator._call_fallback.

The FINAL wiring slice of the Slice 7 close-out for the
bt-2026-05-21-214521 retry storm. Consumes Slice 7a's
``RetryDecision`` + Slice 7c's ``CircuitBreaker`` + emits 3 new
SSE event types via the canonical StreamEventBroker.

Master flag stays FALSE — graduation to TRUE waits for Slice 7f
soak.

Test surface:

  * **SSE event registration pins** — 3 new event types appear
    in ``_VALID_EVENT_TYPES`` + 3 paired publishers importable.
  * **Wiring AST pin** — ``_call_fallback`` imports the breaker +
    classifier + 3 publishers; the failure-decision block calls
    ``classify()`` + ``breaker.evaluate()``.
  * **Trip-path AST pin** — TERMINATE_UNRESOLVED branch calls
    ``publish_circuit_breaker_tripped`` + ``self._raise_exhausted``.
  * **Full-Jitter wiring AST pin** — the backoff-sleep call uses
    ``verdict.backoff_s`` when ``verdict.action ==
    RETRY_AFTER_BACKOFF`` (not the fixed
    _FALLBACK_OUTER_RETRY_BACKOFF_S).
  * **Master-flag default-FALSE pin** — Slice 7c invariant
    rolled forward; Slice 7f flips it.
  * **No-shared-pool-collateral pin** — the new wiring code
    introduces no ``connector.close()`` / ``session.close()``.
  * Publisher behavioural — each of the 3 publishers returns an
    event_id when stream-enabled; never raises.
"""

from __future__ import annotations

import ast
import os
import pathlib
import unittest
from typing import List, Optional

from backend.core.ouroboros.governance.circuit_breaker import (
    circuit_breaker_enabled,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CIRCUIT_BREAKER_STATE_CHANGE,
    EVENT_TYPE_CIRCUIT_BREAKER_TRIPPED,
    EVENT_TYPE_PROVIDER_FAILURE_CLASSIFIED,
    _VALID_EVENT_TYPES,
    publish_circuit_breaker_state_change,
    publish_circuit_breaker_tripped,
    publish_provider_failure_classified,
    stream_enabled,
)


# ============================================================================
# SSE event-type registration
# ============================================================================


class TestSseEventRegistration(unittest.TestCase):
    """All 3 new event types MUST appear in ``_VALID_EVENT_TYPES``
    or the broker will reject publish() at runtime."""

    def test_provider_failure_classified_constant(self) -> None:
        self.assertEqual(
            EVENT_TYPE_PROVIDER_FAILURE_CLASSIFIED,
            "provider_failure_classified",
        )
        self.assertIn(
            EVENT_TYPE_PROVIDER_FAILURE_CLASSIFIED, _VALID_EVENT_TYPES,
        )

    def test_state_change_constant(self) -> None:
        self.assertEqual(
            EVENT_TYPE_CIRCUIT_BREAKER_STATE_CHANGE,
            "circuit_breaker_state_change",
        )
        self.assertIn(
            EVENT_TYPE_CIRCUIT_BREAKER_STATE_CHANGE, _VALID_EVENT_TYPES,
        )

    def test_tripped_constant(self) -> None:
        self.assertEqual(
            EVENT_TYPE_CIRCUIT_BREAKER_TRIPPED,
            "circuit_breaker_tripped",
        )
        self.assertIn(
            EVENT_TYPE_CIRCUIT_BREAKER_TRIPPED, _VALID_EVENT_TYPES,
        )

    def test_all_three_publishers_importable(self) -> None:
        self.assertTrue(callable(publish_provider_failure_classified))
        self.assertTrue(callable(publish_circuit_breaker_state_change))
        self.assertTrue(callable(publish_circuit_breaker_tripped))


# ============================================================================
# Master-flag default-FALSE (Slice 7e wiring lands; 7f graduation flips)
# ============================================================================


class TestMasterFlagDefault(unittest.TestCase):
    """Slice 7g graduated the breaker default to TRUE on 2026-05-22
    after four consecutive forced-budget acceptance soaks proved
    the cascade (per-op terminal_structural → global trip →
    session-exhausted shutdown). Hot-revert is via explicit
    ``JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED=false``."""

    def test_default_is_true(self) -> None:
        prior = os.environ.pop(
            "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED", None,
        )
        try:
            self.assertTrue(
                circuit_breaker_enabled(),
                "Slice 7g (graduated 2026-05-22): "
                "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED is now "
                "default-TRUE. Hot-revert path is explicit "
                "`=false`.",
            )
        finally:
            if prior is not None:
                os.environ[
                    "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED"
                ] = prior


# ============================================================================
# Wiring AST pins
# ============================================================================


_CANDIDATE_GEN_FILE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


def _parse_candidate_generator() -> ast.Module:
    return ast.parse(_CANDIDATE_GEN_FILE.read_text())


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


class TestCallFallbackWiringAstPins(unittest.TestCase):
    """Structural verification that _call_fallback wires the
    Slice 7e substrate correctly. Each pin catches a specific
    drift mode that would silently bypass the breaker."""

    def setUp(self) -> None:
        self._tree = _parse_candidate_generator()
        self._func = _find_function(self._tree, "_call_fallback")
        self.assertIsNotNone(
            self._func,
            "_call_fallback MUST exist in candidate_generator.py",
        )

    def _func_source(self) -> str:
        return ast.unparse(self._func)  # type: ignore[arg-type]

    def test_imports_circuit_breaker(self) -> None:
        self.assertIn(
            "circuit_breaker",
            self._func_source(),
            "_call_fallback MUST import from circuit_breaker — "
            "Slice 7e wiring missing",
        )

    def test_imports_provider_retry_classifier(self) -> None:
        self.assertIn(
            "provider_retry_classifier",
            self._func_source(),
            "_call_fallback MUST import classify() from "
            "provider_retry_classifier",
        )

    def test_constructs_circuit_breaker_instance(self) -> None:
        src = self._func_source()
        self.assertIn(
            "CircuitBreaker(", src,
            "_call_fallback MUST construct a CircuitBreaker instance",
        )

    def test_calls_classify_on_failure(self) -> None:
        src = self._func_source()
        # `classify(` is referenced via the `_slice7e_classify` alias
        # AND its direct attribute access via the import alias.
        self.assertTrue(
            "_slice7e_classify(" in src or "classify(" in src,
            "_call_fallback MUST call classify() in the failure-"
            "decision block",
        )

    def test_calls_breaker_evaluate(self) -> None:
        src = self._func_source()
        self.assertIn(
            ".evaluate(",
            src,
            "_call_fallback MUST call breaker.evaluate() to consume "
            "the classification",
        )

    def test_publishes_failure_classification(self) -> None:
        src = self._func_source()
        self.assertIn(
            "publish_provider_failure_classified",
            src,
            "_call_fallback MUST emit "
            "publish_provider_failure_classified per failure",
        )

    def test_publishes_circuit_breaker_tripped_on_terminal(self) -> None:
        src = self._func_source()
        self.assertIn(
            "publish_circuit_breaker_tripped",
            src,
            "Terminal verdict branch MUST emit "
            "publish_circuit_breaker_tripped before raising "
            "exhausted",
        )

    def test_publishes_state_change(self) -> None:
        src = self._func_source()
        self.assertIn(
            "publish_circuit_breaker_state_change",
            src,
            "Non-terminal state transitions MUST emit "
            "publish_circuit_breaker_state_change",
        )

    def test_raises_exhausted_on_terminate_verdict(self) -> None:
        """The TERMINATE_UNRESOLVED branch MUST call
        ``self._raise_exhausted(...)``. This is what closes the
        35-min retry storm — instead of looping, raise terminal."""
        src = self._func_source()
        self.assertIn(
            "TERMINATE_UNRESOLVED",
            src,
            "_call_fallback MUST branch on TERMINATE_UNRESOLVED",
        )
        self.assertIn(
            "self._raise_exhausted",
            src,
            "TERMINATE_UNRESOLVED branch MUST raise exhausted",
        )


# ============================================================================
# Full-Jitter wiring AST pin (operator binding rolled forward from 7c)
# ============================================================================


class TestFullJitterWiringAstPin(unittest.TestCase):
    """The backoff site MUST consult ``verdict.backoff_s`` when
    the verdict is RETRY_AFTER_BACKOFF — that's the Full-Jitter
    anti-thundering-herd dispersal. Falling back to the fixed
    _FALLBACK_OUTER_RETRY_BACKOFF_S only when verdict is
    RETRY_OK / no-breaker."""

    def test_backoff_branch_consumes_verdict_backoff_s(self) -> None:
        func = _find_function(
            _parse_candidate_generator(), "_call_fallback",
        )
        self.assertIsNotNone(func)
        src = ast.unparse(func)  # type: ignore[arg-type]
        self.assertIn(
            "RETRY_AFTER_BACKOFF",
            src,
            "Backoff branch MUST check verdict.action ==  "
            "RETRY_AFTER_BACKOFF",
        )
        self.assertIn(
            "backoff_s",
            src,
            "Backoff branch MUST consult verdict.backoff_s for the "
            "Full-Jitter delay",
        )
        self.assertIn(
            "asyncio.sleep",
            src,
            "Backoff must await asyncio.sleep with the delay",
        )


# ============================================================================
# No-shared-pool-collateral pin (operator binding — rolled forward from 7b)
# ============================================================================


class TestNoSharedPoolCollateralInWiring(unittest.TestCase):
    """The Slice 7e wiring code MUST NOT introduce
    ``connector.close()`` / ``_connector.close()`` calls in
    _call_fallback. Operator shared-pool binding rolled forward
    from Slices 7b/7d. (The function pre-existed with no such
    calls; this pin ensures the Slice 7e edit didn't introduce
    any.)"""

    def test_no_connector_close_in_call_fallback(self) -> None:
        func = _find_function(
            _parse_candidate_generator(), "_call_fallback",
        )
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
            if any(seg in ("connector", "_connector") for seg in chain):
                offenders.append(".".join(reversed(chain)) + ".close")
        self.assertEqual(
            offenders, [],
            f"Slice 7e wiring introduced forbidden pool-wide "
            f"close() calls in _call_fallback: {offenders}",
        )


# ============================================================================
# Publisher behavioural tests
# ============================================================================


class TestPublisherBehavior(unittest.TestCase):
    """Each of the 3 publishers returns an event_id when the stream
    is enabled, and NEVER raises on pathological inputs."""

    def test_publish_failure_classified_works(self) -> None:
        if not stream_enabled():
            self.skipTest("stream disabled in env")
        ev_id = publish_provider_failure_classified(
            failure_class="SessionBudgetPreflightRefused",
            failure_mode="CONNECTION_ERROR",
            decision="terminal_structural",
            provider="claude",
            op_id="op-slice7e-test",
        )
        self.assertIsNotNone(ev_id)
        self.assertGreater(len(ev_id), 0)

    def test_publish_state_change_works(self) -> None:
        if not stream_enabled():
            self.skipTest("stream disabled in env")
        ev_id = publish_circuit_breaker_state_change(
            prior_state="closed",
            new_state="open_transient",
            op_id="op-slice7e-test",
            scope="per_op",
        )
        self.assertIsNotNone(ev_id)

    def test_publish_tripped_works(self) -> None:
        if not stream_enabled():
            self.skipTest("stream disabled in env")
        ev_id = publish_circuit_breaker_tripped(
            terminal_reason_code=(
                "circuit_breaker_tripped:terminal_structural"
            ),
            op_id="op-slice7e-test",
            scope="per_op",
            backoff_attempt=0,
        )
        self.assertIsNotNone(ev_id)

    def test_publishers_never_raise_on_pathological_inputs(self) -> None:
        """Bounded payload truncation + best-effort discipline —
        none of the publishers may raise on extreme inputs."""
        try:
            publish_provider_failure_classified(
                failure_class="x" * 5000,
                failure_mode="y" * 5000,
                decision="z" * 5000,
                provider="p" * 5000,
                op_id="o" * 5000,
            )
            publish_circuit_breaker_state_change(
                prior_state="a" * 5000,
                new_state="b" * 5000,
                op_id="",
                scope="weird-scope-name",
            )
            publish_circuit_breaker_tripped(
                terminal_reason_code="c" * 5000,
                op_id="",
                scope="",
                backoff_attempt=-999,
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"Publisher raised on pathological inputs: "
                f"{type(exc).__name__}: {exc}"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
