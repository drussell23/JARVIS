"""Phase 2 (A5) — Generic error classifier substrate.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Compose intelligent_retry_manager (or lift only the pieces
   you need: transient vs permanent classification + jitter
   policy) into candidate_generator / Move 6.5 watchdog retry
   paths only — single budget source: cost_governor / existing
   caps. No second parallel retry loop with divergent env knobs
   without consolidating names."

Pinned coverage (~38 tests):
  * Master flag default-FALSE per §33.1
  * Master-off → UNKNOWN unconditionally (zero behavior change
    pre-graduation)
  * Master-on classifications:
    - asyncio.TimeoutError → TRANSIENT
    - ConnectionError / ConnectionRefusedError → TRANSIENT
    - 15 TRANSIENT_PATTERNS → TRANSIENT
    - 9 PERMANENT_PATTERNS → PERMANENT
    - ValueError / TypeError / KeyError → PERMANENT
      (validation-class)
    - Unmatched → UNKNOWN
    - None → UNKNOWN (defensive)
  * Semantic override: pattern matching wins over type-based
    classification (e.g. ValueError('rate limit') → TRANSIENT)
  * compute_retry_delay_s:
    - PERMANENT → 0.0 (caller MUST NOT retry)
    - TRANSIENT → composes full_jitter_backoff_s, base 1s
    - UNKNOWN → composes full_jitter_backoff_s, base 5s
    - attempt clamps to ≥ 0
    - base/cap overrides honored
    - defensive on jitter import failure
  * Pattern table accessors return sorted tuples
  * Frozen pattern tables — TRANSIENT (15 entries) +
    PERMANENT (9 entries)
  * 7 AST pins clean (parametrized) + targeted regression
    fires:
    - taxonomy (synthetic regression: 4-value enum drift)
    - no_retry_loop (synthetic: while-with-attempt-counter)
    - no_config_dataclass (synthetic: RetryConfig class)
    - composes_canonical_jitter (synthetic: missing import)
    - pattern_tables_canonical (synthetic: list literal)
  * Public API surface complete + register_flags + swallows
    registry errors
  * Cross-kingdom boundary unchanged (boundary scan = 0)
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "error_classifier.py"
    )


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_ERROR_CLASSIFIER_ENABLED", v,
        )
        assert master_enabled() is True


def test_master_off_returns_unknown_unconditionally(
    monkeypatch,
):
    """Operator binding: 'no behavior change pre-graduation'.
    When master flag off, even errors that would obviously
    classify (TimeoutError, ConnectionError, validation-class)
    MUST return UNKNOWN."""
    monkeypatch.delenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        asyncio.TimeoutError(),
    ) is ErrorClass.UNKNOWN
    assert classify_error(
        ConnectionError("test"),
    ) is ErrorClass.UNKNOWN
    assert classify_error(
        ValueError("bad input"),
    ) is ErrorClass.UNKNOWN
    assert classify_error(
        RuntimeError("rate limit"),
    ) is ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# Type-based TRANSIENT classifications
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc", [
        asyncio.TimeoutError(),
        ConnectionError("socket gone"),
        ConnectionRefusedError("refused"),
    ],
)
def test_type_based_transient(monkeypatch, exc):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(exc) is ErrorClass.TRANSIENT


# ---------------------------------------------------------------------------
# Pattern-based TRANSIENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg", [
        "request timeout",
        "connection timed out",
        "connection reset by peer",
        "connection refused on port 443",
        "service is temporarily unavailable",
        "503 service unavailable",
        "please try again",
        "rate limit exceeded",
        "too many requests",
        "server is overloaded",
        "endpoint is busy",
        "temporary failure in name resolution",
        "transient error",
        "retry the request",
        "intermittent network issue",
    ],
)
def test_pattern_based_transient(monkeypatch, msg):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        RuntimeError(msg),
    ) is ErrorClass.TRANSIENT


# ---------------------------------------------------------------------------
# Pattern-based PERMANENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg", [
        "not found in registry",
        "invalid token",
        "unauthorized request",
        "forbidden by policy",
        "bad request",
        "validation error",
        "missing required field",
        "permission denied",
        "operation not allowed",
    ],
)
def test_pattern_based_permanent(monkeypatch, msg):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        RuntimeError(msg),
    ) is ErrorClass.PERMANENT


# ---------------------------------------------------------------------------
# Type-based PERMANENT (validation-class)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc", [
        ValueError("bad input"),
        TypeError("wrong type"),
        KeyError("missing"),
    ],
)
def test_type_based_permanent(monkeypatch, exc):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(exc) is ErrorClass.PERMANENT


# ---------------------------------------------------------------------------
# Semantic override: pattern wins over type
# ---------------------------------------------------------------------------


def test_semantic_override_value_error_rate_limit(
    monkeypatch,
):
    """Operator binding 'advanced, dynamic, adaptive,
    intelligent' — pattern matching MUST win over
    type-based fallback. ValueError('rate limit') →
    TRANSIENT, not PERMANENT."""
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        ValueError("rate limit hit"),
    ) is ErrorClass.TRANSIENT


def test_semantic_override_type_error_overloaded(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        TypeError("server overloaded"),
    ) is ErrorClass.TRANSIENT


def test_semantic_override_runtime_error_permanent(
    monkeypatch,
):
    """RuntimeError isn't in any type list, but 'unauthorized'
    matches PERMANENT_PATTERNS."""
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(
        RuntimeError("unauthorized access"),
    ) is ErrorClass.PERMANENT


# ---------------------------------------------------------------------------
# UNKNOWN fallback + defensive
# ---------------------------------------------------------------------------


def test_unknown_fallback_unmatched_runtime_error(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    # RuntimeError with no pattern match + not in any type
    # list → UNKNOWN
    assert classify_error(
        RuntimeError("mysterious failure"),
    ) is ErrorClass.UNKNOWN


def test_defensive_none_returns_unknown(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )
    assert classify_error(None) is ErrorClass.UNKNOWN  # type: ignore


def test_defensive_unprintable_exception(monkeypatch):
    """Defensive: exception whose str() raises MUST not
    crash the classifier."""
    monkeypatch.setenv(
        "JARVIS_ERROR_CLASSIFIER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, classify_error,
    )

    class _BadStr(Exception):
        def __str__(self):
            raise RuntimeError("can't stringify me")

    # MUST NOT raise; falls through to UNKNOWN
    assert classify_error(
        _BadStr(),
    ) is ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# compute_retry_delay_s — composes canonical jitter
# ---------------------------------------------------------------------------


def test_compute_delay_permanent_zero():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    assert compute_retry_delay_s(
        ErrorClass.PERMANENT, 0,
    ) == 0.0
    assert compute_retry_delay_s(
        ErrorClass.PERMANENT, 5,
    ) == 0.0


def test_compute_delay_transient_within_band():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    # base 1s, cap 30s, attempt 0 → AWS full-jitter:
    # delay = uniform(0, min(30, 1 * 2^0)) = uniform(0, 1)
    delay = compute_retry_delay_s(
        ErrorClass.TRANSIENT, 0,
    )
    assert 0.0 <= delay <= 1.0


def test_compute_delay_unknown_within_band():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    # base 5s, attempt 0 → uniform(0, 5)
    delay = compute_retry_delay_s(
        ErrorClass.UNKNOWN, 0,
    )
    assert 0.0 <= delay <= 5.0


def test_compute_delay_attempt_negative_clamps():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    # Negative attempt → clamped to 0; should not raise
    delay = compute_retry_delay_s(
        ErrorClass.TRANSIENT, -5,
    )
    assert 0.0 <= delay <= 1.0


def test_compute_delay_overrides_honored():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    # base override 10s, cap 60s, attempt 0 → uniform(0, 10)
    delay = compute_retry_delay_s(
        ErrorClass.TRANSIENT, 0,
        base_s_override=10.0, cap_s_override=60.0,
    )
    assert 0.0 <= delay <= 10.0


def test_compute_delay_zero_base_returns_zero():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        ErrorClass, compute_retry_delay_s,
    )
    delay = compute_retry_delay_s(
        ErrorClass.TRANSIENT, 0,
        base_s_override=0.0,
    )
    assert delay == 0.0


# ---------------------------------------------------------------------------
# Pattern table accessors
# ---------------------------------------------------------------------------


def test_get_transient_patterns_count():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        get_transient_patterns,
    )
    patterns = get_transient_patterns()
    assert len(patterns) == 15
    # Sorted for determinism
    assert list(patterns) == sorted(patterns)


def test_get_permanent_patterns_count():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        get_permanent_patterns,
    )
    patterns = get_permanent_patterns()
    assert len(patterns) == 9
    assert list(patterns) == sorted(patterns)


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "error_classifier_master_default_false",
        "error_classifier_authority_asymmetry",
        "error_classifier_taxonomy_3_values",
        "error_classifier_no_retry_loop",
        "error_classifier_no_config_dataclass",
        "error_classifier_composes_canonical_jitter",
        "error_classifier_pattern_tables_canonical",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ErrorClass:
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    EXTRA_VALUE = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_taxonomy_3_values"
        )
    )
    assert pin.validate(tree, bad)


def test_no_retry_loop_pin_fires_on_attempt_counter():
    """Synthetic regression: a while loop with attempt
    counter increment MUST trip the pin."""
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def bad_retry():
    attempt = 0
    while attempt < 5:
        try:
            do_something()
            break
        except Exception:
            attempt += 1  # forbidden — operator binding
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_no_retry_loop"
        )
    )
    assert pin.validate(tree, bad)


def test_no_config_dataclass_pin_fires_on_RetryConfig():
    """Synthetic regression: defining RetryConfig MUST trip
    the pin (operator binding 'no parallel env-knob
    surface')."""
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class RetryConfig:
    max_attempts: int = 3
    base_delay_ms: float = 1000.0
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_no_config_dataclass"
        )
    )
    assert pin.validate(tree, bad)


def test_no_config_dataclass_pin_fires_on_manager():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class IntelligentRetryManager:
    pass
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_no_config_dataclass"
        )
    )
    assert pin.validate(tree, bad)


def test_composes_canonical_jitter_pin_fires_on_missing():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def compute_retry_delay_s(error_class, attempt, **kwargs):
    return attempt * 1.0  # no full_jitter import — local math forbidden
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_composes_canonical_jitter"
        )
    )
    assert pin.validate(tree, bad)


def test_pattern_tables_pin_fires_on_list_literal():
    """Synthetic regression: pattern tables MUST be
    frozenset literals; list/set literals fail the pin."""
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
_TRANSIENT_PATTERNS = ["timeout", "rate limit"]  # list, not frozenset
_PERMANENT_PATTERNS = ["not found"]
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_pattern_tables_canonical"
        )
    )
    assert pin.validate(tree, bad)


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "error_classifier_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (  # noqa: E501
        error_classifier as mod,
    )
    expected = {
        "ERROR_CLASSIFIER_SCHEMA_VERSION",
        "ErrorClass",
        "classify_error",
        "compute_retry_delay_s",
        "get_permanent_patterns",
        "get_transient_patterns",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_master():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    assert (
        registry.register.call_args.kwargs["name"]
        == "JARVIS_ERROR_CLASSIFIER_ENABLED"
    )


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.error_classifier import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


# ---------------------------------------------------------------------------
# Cross-kingdom boundary unchanged
# ---------------------------------------------------------------------------


def test_cross_kingdom_boundary_unchanged():
    """Phase 0 boundary holds — error_classifier is in
    governance/ and imports zero from coding_council/."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    assert scan_governance_tree() == ()
