"""Slice 127 Phase 1 — economic reclassification (root fix).

Ground truth: bt-2026-06-07-040933 line 1592 — Claude HTTP 400
"credit balance too low" (class ``BadRequestError``) was classified
``TERMINAL_CONFIG`` and sticky-bricked 16 ops. With economic
reclassification ON, that exact message must classify as the
recoverable ``TERMINAL_QUOTA``, never ``TERMINAL_CONFIG``.

``classify`` stays PURE-DATA (AST-pinned: no os.environ read). The
gate is an explicit ``economic_reclassify`` parameter that the caller
sources from ``economic_router.economic_reclassify_enabled()``
(default-FALSE per §33.1). Marker detection COMPOSES
``economic_router.is_hard_economic_block`` (no duplicate table).
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
    classify,
)
from backend.core.ouroboros.governance.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    VerdictAction,
    reset_global_breaker,
)


# The exact bt-2026-06-07-040933 fallback_err_msg signature.
_CLAUDE_CREDIT_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too "
    "low to access the Anthropic API. Please go to Plans & Billing to "
    "upgrade or purchase credits.'}}"
)


class TestEconomicReclassification(unittest.TestCase):
    def test_claude_credit_400_is_quota_not_config_when_enabled(self) -> None:
        # The exact bt-2026-06-07-040933 signature: BadRequestError + the
        # credit-balance message + the gate on → recoverable TERMINAL_QUOTA.
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message=_CLAUDE_CREDIT_400,
            economic_reclassify=True,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_QUOTA)

    def test_insufficient_funds_variants_are_quota(self) -> None:
        for msg in (
            "Error 402 - insufficient balance",
            "Account balance too low, please add credits",
            "payment required",
            "please upgrade or purchase credits",
        ):
            with self.subTest(msg=msg):
                self.assertEqual(
                    classify(
                        "BadRequestError", failure_message=msg,
                        economic_reclassify=True,
                    ),
                    RetryDecision.TERMINAL_QUOTA,
                )

    def test_non_economic_badrequest_still_terminal_config(self) -> None:
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message="Error code: 400 - malformed tool schema",
            economic_reclassify=True,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_CONFIG)

    def test_gate_off_preserves_legacy_terminal_config(self) -> None:
        # economic_reclassify defaults to False → byte-identical to pre-127.
        self.assertEqual(
            classify(
                "BadRequestError", "TIMEOUT",
                failure_message=_CLAUDE_CREDIT_400,
            ),
            RetryDecision.TERMINAL_CONFIG,
        )
        self.assertEqual(
            classify(
                "BadRequestError", "TIMEOUT",
                failure_message=_CLAUDE_CREDIT_400,
                economic_reclassify=False,
            ),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_message_param_is_optional_byte_identical(self) -> None:
        # No failure_message → identical to the legacy 3-arg signature.
        self.assertEqual(
            classify("RateLimitError"), RetryDecision.TERMINAL_QUOTA,
        )
        self.assertEqual(
            classify(None, None, http_status=401),
            RetryDecision.TERMINAL_CONFIG,
        )
        self.assertEqual(
            classify("TimeoutError", "TIMEOUT"),
            RetryDecision.RETRY_TRANSIENT,
        )

    def test_empty_message_is_safe(self) -> None:
        # Empty / None message must never raise and must not reclassify.
        self.assertEqual(
            classify(
                "BadRequestError", failure_message="",
                economic_reclassify=True,
            ),
            RetryDecision.TERMINAL_CONFIG,
        )
        self.assertEqual(
            classify(
                "BadRequestError", failure_message=None,
                economic_reclassify=True,
            ),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_economic_routes_to_quota_despite_config_class(self) -> None:
        # BadRequestError IS a config class; the economic message must still
        # route to recoverable QUOTA when the gate is on — that's the fix.
        self.assertEqual(
            classify(
                "BadRequestError", "TIMEOUT",
                failure_message=_CLAUDE_CREDIT_400,
                http_status=400,
                economic_reclassify=True,
            ),
            RetryDecision.TERMINAL_QUOTA,
        )


class TestEconomicReclassifyBreakerEndToEnd(unittest.TestCase):
    """The whole point: an economic 400 must NOT sticky-brick the op on the
    1st hit. Feed the reclassified decision into the REAL Slice-7 breaker and
    assert the 1st verdict is the recoverable RETRY_AFTER_BACKOFF — the exact
    opposite of TERMINAL_CONFIG's immediate TERMINATE_UNRESOLVED that bricked
    16 ops in bt-2026-06-07-040933."""

    def setUp(self) -> None:
        reset_global_breaker()

    def tearDown(self) -> None:
        reset_global_breaker()

    def test_economic_400_first_hit_is_recoverable_not_terminal(self) -> None:
        breaker = CircuitBreaker(op_id="op-econ-test")
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message=_CLAUDE_CREDIT_400,
            economic_reclassify=True,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_QUOTA)
        verdict = breaker.evaluate(decision)
        # 1st economic hit → recoverable backoff, breaker stays non-terminal.
        self.assertEqual(verdict.action, VerdictAction.RETRY_AFTER_BACKOFF)
        self.assertNotEqual(breaker.state, CircuitState.OPEN_TERMINAL)

    def test_legacy_economic_400_bricks_immediately(self) -> None:
        # Regression cage proving the OLD path (gate OFF) is the bug: the
        # SAME message → TERMINAL_CONFIG → immediate TERMINATE_UNRESOLVED.
        breaker = CircuitBreaker(op_id="op-econ-legacy")
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message=_CLAUDE_CREDIT_400,
            economic_reclassify=False,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_CONFIG)
        verdict = breaker.evaluate(decision)
        self.assertEqual(verdict.action, VerdictAction.TERMINATE_UNRESOLVED)
        self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)


if __name__ == "__main__":
    unittest.main()
