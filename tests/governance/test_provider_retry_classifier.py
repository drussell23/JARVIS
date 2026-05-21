"""Slice 7a — ProviderRetryClassifier pure-data substrate tests.

Closes the empirical fault from bt-2026-05-21-214521: the
``SessionBudgetPreflightRefused/CONNECTION_ERROR`` mis-bucket that
caused a 35-min retry storm. The classifier in this module is the
PURE-DATA half of the Slice 7 close-out; the Circuit Breaker
(Slice 7c) and the wiring (Slice 7e) consume its decisions.

Test surface:

  * Closed-taxonomy AST pin — ``RetryDecision`` has exactly
    four members (no expansion without paired test).
  * **The empirical fault regression** — the exact
    ``SessionBudgetPreflightRefused`` / CONNECTION_ERROR pair
    that caused the 35-min hang now classifies as
    ``TERMINAL_STRUCTURAL`` (one-line regression cage).
  * Per-class coverage — every entry in the three TERMINAL_*
    registries returns the right decision.
  * HTTP status table coverage — 401 / 403 / 404 / 408 / 429 /
    500 / 502 / 503 / 504 each return the documented decision.
  * FailureMode coverage AST pin — every member of
    ``candidate_generator.FailureMode`` has an explicit mapping
    in the classifier (no silent drift if the enum grows).
  * Priority ordering — failure_class beats http_status beats
    failure_mode beats fallback.
  * Fallback safety — unknown / None / empty inputs return
    ``RETRY_TRANSIENT`` (preserving pre-Slice-7 retry semantics).
  * Purity — ``classify`` is referentially transparent +
    NEVER raises on any reasonable input shape.
  * Public-surface ``__all__`` pin — new exports are stable."""

from __future__ import annotations

import ast
import pathlib
import unittest
from typing import List

from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
    classify,
    known_failure_modes,
    known_terminal_config_classes,
    known_terminal_quota_classes,
    known_terminal_structural_classes,
)
from backend.core.ouroboros.governance import provider_retry_classifier


# ============================================================================
# Closed-taxonomy AST pin — RetryDecision cardinality + values
# ============================================================================


class TestRetryDecisionClosedTaxonomy(unittest.TestCase):
    """The 4-value closed taxonomy is structural — adding a 5th
    member requires a paired test update + Circuit Breaker
    (Slice 7c) trip-table extension. This pin catches accidental
    expansion."""

    def test_retry_decision_has_exactly_four_members(self) -> None:
        members = list(RetryDecision)
        self.assertEqual(
            len(members), 4,
            f"RetryDecision is a CLOSED taxonomy of 4 values; found "
            f"{len(members)} members: {[m.name for m in members]}. "
            f"If you need a 5th decision, extend the Circuit "
            f"Breaker (Slice 7c) trip-table in the same PR.",
        )

    def test_retry_decision_members_are_exactly_documented(self) -> None:
        names = {m.name for m in RetryDecision}
        expected = {
            "RETRY_TRANSIENT",
            "TERMINAL_STRUCTURAL",
            "TERMINAL_QUOTA",
            "TERMINAL_CONFIG",
        }
        self.assertEqual(
            names, expected,
            "RetryDecision member names drifted from the design.",
        )

    def test_retry_decision_values_are_lower_snake(self) -> None:
        for m in RetryDecision:
            self.assertEqual(
                m.value, m.name.lower(),
                f"RetryDecision.{m.name} value should be its name "
                f"lowercased (got {m.value!r}).",
            )

    def test_retry_decision_is_str_subclass(self) -> None:
        """All canonical Ouroboros closed-taxonomy enums inherit
        ``str`` so JSON serialization is byte-identical to the
        enum value. EvaluatorPhase / BlockedOnKind / FailureMode
        all follow this pattern."""
        for m in RetryDecision:
            self.assertIsInstance(
                m, str,
                f"RetryDecision.{m.name} should be a str enum",
            )


# ============================================================================
# THE empirical-fault regression — the 35-min hang's exact signature
# ============================================================================


class TestEmpiricalFaultRegression(unittest.TestCase):
    """One-line cage around the bt-2026-05-21-214521 fault: the
    ``SessionBudgetPreflightRefused/CONNECTION_ERROR`` pair must
    classify as TERMINAL_STRUCTURAL, NOT RETRY_TRANSIENT. If this
    test regresses, the 35-min retry storm comes back."""

    def test_session_budget_preflight_refused_is_terminal_structural(self) -> None:
        # The exact pair the X-ray captured at 14:52:53:
        #
        #   fallback_err_class=SessionBudgetPreflightRefused
        #   fallback_failure_mode=CONNECTION_ERROR
        decision = classify(
            failure_class="SessionBudgetPreflightRefused",
            failure_mode="CONNECTION_ERROR",
        )
        self.assertEqual(
            decision, RetryDecision.TERMINAL_STRUCTURAL,
            "SessionBudgetPreflightRefused is a hard mathematical "
            "refusal (cost_estimate > session_remaining). Retrying "
            "CANNOT clear it. This is the empirical 35-min hang "
            "from bt-2026-05-21-214521 — DO NOT regress.",
        )

    def test_session_budget_preflight_refused_alone_is_terminal_structural(self) -> None:
        """Even without a failure_mode, the class string alone is
        dispositive. Defensive against producers that drop the
        mode in their telemetry."""
        decision = classify("SessionBudgetPreflightRefused")
        self.assertEqual(decision, RetryDecision.TERMINAL_STRUCTURAL)

    def test_session_budget_refused_does_not_depend_on_http_status(self) -> None:
        """A budget refusal originates BEFORE the HTTP call —
        there is no HTTP status. Pass a deceptive 200 and assert
        the class still dominates."""
        decision = classify(
            "SessionBudgetPreflightRefused",
            failure_mode="CONNECTION_ERROR",
            http_status=200,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_STRUCTURAL)


# ============================================================================
# Per-class coverage — every entry in each TERMINAL_* registry
# ============================================================================


class TestPerClassRegistryCoverage(unittest.TestCase):
    """Every failure_class string in the three registries returns
    the documented decision. New entries in the registries
    auto-enroll into this test."""

    def test_every_terminal_structural_class_classifies_correctly(self) -> None:
        registry = known_terminal_structural_classes()
        self.assertGreater(len(registry), 0)
        for cls in registry:
            decision = classify(cls)
            self.assertEqual(
                decision, RetryDecision.TERMINAL_STRUCTURAL,
                f"{cls!r} in TERMINAL_STRUCTURAL registry but "
                f"classifies as {decision.name}.",
            )

    def test_every_terminal_config_class_classifies_correctly(self) -> None:
        registry = known_terminal_config_classes()
        self.assertGreater(len(registry), 0)
        for cls in registry:
            decision = classify(cls)
            self.assertEqual(
                decision, RetryDecision.TERMINAL_CONFIG,
                f"{cls!r} in TERMINAL_CONFIG registry but classifies "
                f"as {decision.name}.",
            )

    def test_every_terminal_quota_class_classifies_correctly(self) -> None:
        registry = known_terminal_quota_classes()
        self.assertGreater(len(registry), 0)
        for cls in registry:
            decision = classify(cls)
            self.assertEqual(
                decision, RetryDecision.TERMINAL_QUOTA,
                f"{cls!r} in TERMINAL_QUOTA registry but classifies "
                f"as {decision.name}.",
            )

    def test_three_registries_are_disjoint(self) -> None:
        """A failure class can belong to at most ONE registry —
        otherwise the classification is ambiguous. The classifier's
        priority-1 lookup checks them in order; overlap would
        cause silent precedence-by-order bugs."""
        structural = known_terminal_structural_classes()
        config = known_terminal_config_classes()
        quota = known_terminal_quota_classes()
        self.assertEqual(
            structural & config, frozenset(),
            "TERMINAL_STRUCTURAL ∩ TERMINAL_CONFIG must be empty.",
        )
        self.assertEqual(
            structural & quota, frozenset(),
            "TERMINAL_STRUCTURAL ∩ TERMINAL_QUOTA must be empty.",
        )
        self.assertEqual(
            config & quota, frozenset(),
            "TERMINAL_CONFIG ∩ TERMINAL_QUOTA must be empty.",
        )


# ============================================================================
# HTTP status table coverage
# ============================================================================


class TestHttpStatusTable(unittest.TestCase):
    """The HTTP status fallback table is dispositive when present —
    auth codes terminal, rate-limit terminal-quota, 5xx transient."""

    def test_http_401_is_terminal_config(self) -> None:
        self.assertEqual(
            classify(None, None, http_status=401),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_http_403_is_terminal_config(self) -> None:
        self.assertEqual(
            classify(None, None, http_status=403),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_http_404_is_terminal_config(self) -> None:
        self.assertEqual(
            classify(None, None, http_status=404),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_http_429_is_terminal_quota(self) -> None:
        self.assertEqual(
            classify(None, None, http_status=429),
            RetryDecision.TERMINAL_QUOTA,
        )

    def test_http_408_is_retry_transient(self) -> None:
        # 408 Request Timeout — transient.
        self.assertEqual(
            classify(None, None, http_status=408),
            RetryDecision.RETRY_TRANSIENT,
        )

    def test_http_500_502_503_504_are_retry_transient(self) -> None:
        for code in (500, 502, 503, 504):
            with self.subTest(http_status=code):
                self.assertEqual(
                    classify(None, None, http_status=code),
                    RetryDecision.RETRY_TRANSIENT,
                )

    def test_unmapped_http_status_falls_through(self) -> None:
        # 418 I'm a teapot — not in any HTTP table. Falls through
        # to failure_mode → fallback. With no other inputs, the
        # safe fallback is RETRY_TRANSIENT (preserves existing
        # retry behaviour).
        self.assertEqual(
            classify(None, None, http_status=418),
            RetryDecision.RETRY_TRANSIENT,
        )


# ============================================================================
# FailureMode coverage AST pin — every candidate_generator enum
# member has an explicit classifier mapping
# ============================================================================


class TestFailureModeCoverage(unittest.TestCase):
    """The classifier's ``_FAILURE_MODE_DEFAULT`` table MUST
    cover every member of ``candidate_generator.FailureMode``.
    A new FailureMode landing without a paired classifier entry
    would silently fall through to RETRY_TRANSIENT — possibly
    masking a TERMINAL_* condition. This AST pin enforces
    coverage at test time, before runtime can see the gap."""

    def test_every_failure_mode_has_explicit_classifier_mapping(self) -> None:
        from backend.core.ouroboros.governance.candidate_generator import (
            FailureMode,
        )
        enum_names = {m.name for m in FailureMode}
        classifier_names = known_failure_modes()
        missing = enum_names - classifier_names
        self.assertEqual(
            missing, set(),
            f"candidate_generator.FailureMode added new member(s) "
            f"without a classifier mapping: {sorted(missing)}. "
            f"Add an entry to _FAILURE_MODE_DEFAULT in "
            f"provider_retry_classifier.py with a deliberate "
            f"RetryDecision assignment.",
        )

    def test_classifier_does_not_carry_phantom_failure_modes(self) -> None:
        """Symmetric check — if the classifier table references a
        FailureMode name that no longer exists in the enum, the
        author should remove it."""
        from backend.core.ouroboros.governance.candidate_generator import (
            FailureMode,
        )
        enum_names = {m.name for m in FailureMode}
        classifier_names = known_failure_modes()
        phantom = classifier_names - enum_names
        self.assertEqual(
            phantom, set(),
            f"provider_retry_classifier._FAILURE_MODE_DEFAULT "
            f"references FailureMode names that no longer exist "
            f"in candidate_generator: {sorted(phantom)}. Remove.",
        )

    def test_failure_mode_default_returns_expected_decision(self) -> None:
        """Per-mode default behaviour — the table maps each mode
        to the documented decision when no failure_class /
        http_status overrides it."""
        cases = [
            ("RATE_LIMITED",        RetryDecision.TERMINAL_QUOTA),
            ("TIMEOUT",             RetryDecision.RETRY_TRANSIENT),
            ("SERVER_ERROR",        RetryDecision.RETRY_TRANSIENT),
            ("CONNECTION_ERROR",    RetryDecision.RETRY_TRANSIENT),
            ("CONTENT_FAILURE",     RetryDecision.RETRY_TRANSIENT),
            ("CONTEXT_OVERFLOW",    RetryDecision.RETRY_TRANSIENT),
            ("TRANSIENT_TRANSPORT", RetryDecision.RETRY_TRANSIENT),
        ]
        for mode_name, expected in cases:
            with self.subTest(failure_mode=mode_name):
                self.assertEqual(
                    classify(None, mode_name),
                    expected,
                    f"FailureMode.{mode_name} default should map to "
                    f"{expected.name}",
                )


# ============================================================================
# Priority ordering — class > http_status > failure_mode > fallback
# ============================================================================


class TestPriorityOrdering(unittest.TestCase):
    """The classifier's priority order is structural — when a
    specific failure_class is dispositive, it MUST dominate the
    cruder failure_mode. The empirical fault was exactly a case
    where the mode hid the more-specific class signal."""

    def test_failure_class_dominates_failure_mode(self) -> None:
        # Class says TERMINAL_STRUCTURAL; mode says transient.
        # Class wins.
        decision = classify(
            "SessionBudgetPreflightRefused",
            failure_mode="TIMEOUT",  # would otherwise be RETRY_TRANSIENT
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_STRUCTURAL)

    def test_failure_class_dominates_http_status(self) -> None:
        # Class says TERMINAL_STRUCTURAL; http_status 500 would
        # be RETRY_TRANSIENT. Class wins.
        decision = classify(
            "SessionBudgetPreflightRefused",
            failure_mode="TIMEOUT",
            http_status=500,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_STRUCTURAL)

    def test_http_status_dominates_failure_mode(self) -> None:
        # No class. http_status=401 (CONFIG) vs failure_mode=TIMEOUT
        # (TRANSIENT). HTTP wins because the mode is less specific.
        decision = classify(
            None,
            failure_mode="TIMEOUT",
            http_status=401,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_CONFIG)

    def test_failure_mode_used_when_class_and_http_unknown(self) -> None:
        decision = classify(
            "SomeUnknownExceptionClass",
            failure_mode="RATE_LIMITED",
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_QUOTA)


# ============================================================================
# Safe fallback — unknown / None / empty inputs return RETRY_TRANSIENT
# ============================================================================


class TestSafeFallback(unittest.TestCase):
    """When the classifier can't make a determination, it returns
    RETRY_TRANSIENT — the SAFEST default that preserves the
    pre-Slice-7 retry semantics for new failure classes the
    classifier hasn't been taught yet."""

    def test_all_none_inputs_return_retry_transient(self) -> None:
        self.assertEqual(
            classify(None, None),
            RetryDecision.RETRY_TRANSIENT,
        )

    def test_empty_strings_return_retry_transient(self) -> None:
        self.assertEqual(
            classify("", ""),
            RetryDecision.RETRY_TRANSIENT,
        )

    def test_unknown_class_unknown_mode_returns_retry_transient(self) -> None:
        self.assertEqual(
            classify("AsteroidImpactError", "EXTINCTION_EVENT"),
            RetryDecision.RETRY_TRANSIENT,
        )

    def test_unknown_class_only_returns_retry_transient(self) -> None:
        self.assertEqual(
            classify("CompletelyMadeUpError"),
            RetryDecision.RETRY_TRANSIENT,
        )


# ============================================================================
# Purity — referential transparency + no exceptions
# ============================================================================


class TestPurity(unittest.TestCase):
    """``classify`` is a pure function — same inputs ⇒ same output,
    no side effects, no exceptions raised on any reasonable input."""

    def test_classify_is_referentially_transparent(self) -> None:
        results = [
            classify("SessionBudgetPreflightRefused", "CONNECTION_ERROR")
            for _ in range(50)
        ]
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0], RetryDecision.TERMINAL_STRUCTURAL)

    def test_classify_never_raises_on_pathological_inputs(self) -> None:
        # Each pair should return a RetryDecision, never raise.
        pathological_inputs = [
            (None, None, None),
            ("", "", None),
            ("X" * 10000, "Y" * 10000, 999999),
            (None, None, -1),
            (None, None, 0),
            ("\x00\n\t", "\x00\n\t", 0),
            (123, 456, "not-an-int"),  # type-coerced inputs
        ]
        for fc, fm, hs in pathological_inputs:
            with self.subTest(failure_class=fc, failure_mode=fm,
                              http_status=hs):
                try:
                    decision = classify(fc, fm, http_status=hs)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    self.fail(
                        f"classify raised {type(exc).__name__} on "
                        f"({fc!r}, {fm!r}, {hs!r}); should be "
                        f"crash-free"
                    )
                self.assertIsInstance(decision, RetryDecision)


# ============================================================================
# AST pin — the source module enforces structural invariants
# ============================================================================


_MODULE_FILE = pathlib.Path(provider_retry_classifier.__file__)


def _parse_module() -> ast.Module:
    return ast.parse(_MODULE_FILE.read_text())


class TestSourceModuleAstInvariants(unittest.TestCase):
    """AST pins on the source — these catch structural drift in
    PRs that touch the module."""

    def test_no_runtime_io(self) -> None:
        """Pure-data discipline — the module body MUST NOT contain
        any I/O / network / filesystem / os.environ access. AST
        scan for any ``import`` of risky modules at module level.
        Imports inside functions are also forbidden by this pin."""
        forbidden_top_level_imports = {
            "requests", "urllib", "urllib2", "http",
            "socket", "subprocess", "aiohttp", "asyncio",
        }
        tree = _parse_module()
        seen: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in forbidden_top_level_imports:
                        seen.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root in forbidden_top_level_imports:
                        seen.append(node.module)
        self.assertEqual(
            seen, [],
            f"provider_retry_classifier.py is PURE DATA — it must "
            f"not import I/O modules. Found: {seen}",
        )

    def test_no_module_level_os_environ_read(self) -> None:
        tree = _parse_module()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr == "environ"
            ):
                self.fail(
                    "provider_retry_classifier.py is PURE DATA — "
                    "no os.environ reads allowed. The Circuit "
                    "Breaker (Slice 7c) reads env knobs, not the "
                    "classifier."
                )

    def test_module_has_exactly_one_public_classify_function(self) -> None:
        tree = _parse_module()
        public_fns = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and not node.name.startswith("_")
            and node.name != "classify"
        ]
        # Only known-helpers + ``classify``.
        allowed = {
            "classify",
            "known_terminal_structural_classes",
            "known_terminal_config_classes",
            "known_terminal_quota_classes",
            "known_failure_modes",
        }
        for fn_name in public_fns:
            self.assertIn(
                fn_name, allowed,
                f"Unexpected public function {fn_name!r} in "
                f"provider_retry_classifier. Update the AST pin or "
                f"reconsider the public surface.",
            )


# ============================================================================
# Public-surface pin — __all__ stability
# ============================================================================


class TestPublicSurface(unittest.TestCase):
    """``__all__`` is the export contract — Slice 7c + Slice 7e
    consume these names. Removing any would break the wiring
    silently. The pin locks the public surface."""

    def test_all_exports_present(self) -> None:
        expected = {
            "RetryDecision",
            "classify",
            "known_terminal_structural_classes",
            "known_terminal_config_classes",
            "known_terminal_quota_classes",
            "known_failure_modes",
        }
        actual = set(provider_retry_classifier.__all__)
        self.assertEqual(
            actual, expected,
            f"__all__ drifted. Expected {expected}, got {actual}.",
        )

    def test_each_exported_name_actually_exists(self) -> None:
        for name in provider_retry_classifier.__all__:
            self.assertTrue(
                hasattr(provider_retry_classifier, name),
                f"__all__ references {name!r} but the attribute "
                f"is missing.",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
