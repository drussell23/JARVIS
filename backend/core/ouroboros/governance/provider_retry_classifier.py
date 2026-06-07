"""Provider failure → retry decision classifier (Slice 7a).

Empirical context — bt-2026-05-21-214521 X-ray (Slice 6 trace):

    The 35-min "silent window" hang was diagnosed as a fast-fail
    retry storm inside ``CandidateGenerator._call_fallback``. The
    proximate trigger was
    ``SessionBudgetPreflightRefused/CONNECTION_ERROR``: a structural
    cost-budget refusal misclassified as a transient connection
    error. The outer-retry loop interpreted CONNECTION_ERROR as
    retryable and looped at ~2.7s per attempt for the entire
    1800s ``wait_for`` budget — many minutes of churn that looked
    silent to operators but was actively consuming compute.

The root cause is taxonomy ambiguity at the provider→retry
boundary. The pre-existing ``candidate_generator.FailureMode`` enum
distinguishes **why** a call failed (timeout / 5xx / connection
error / etc.) but says nothing about **whether retrying will
help**. ``SessionBudgetPreflightRefused`` is a hard mathematical
refusal (``cost_estimate > session_remaining``); retrying cannot
make a structural inequality true. ``RateLimitError`` is a quota
refusal that *might* resolve after backoff but is terminal within
a short window. Auth/config errors (401, 403, missing model) are
permanently terminal until configuration changes.

This module introduces a **closed-taxonomy** classification — every
provider failure is mapped to exactly one of four ``RetryDecision``
values, and the downstream Circuit Breaker (Slice 7c) consumes the
decision rather than re-interpreting the raw failure_class string
+ failure_mode enum at every retry site.

Design properties (closed-form, AST-pinned):

  * **Pure data.** No I/O, no state, no side effects. ``classify``
    is referentially transparent; same inputs → same output.
  * **Closed enum.** ``RetryDecision`` has exactly four members;
    adding a fifth requires bumping the AST pin + paired test.
  * **Composes existing enum.** Reads
    ``candidate_generator.FailureMode`` (the 7-value structural
    classifier of *why*) and ``SessionBudgetPreflightRefused``
    (the exception class) — does NOT define parallel taxonomies.
  * **Failure-class-string priority.** Specific exception classes
    (``SessionBudgetPreflightRefused``, ``RateLimitError``, etc.)
    take precedence over the coarser ``FailureMode``. This is the
    layer that fixes the empirical CONNECTION_ERROR mis-bucket.
  * **HTTP status fallback.** When neither the failure class nor
    the failure mode is dispositive, a closed table maps HTTP
    status codes (401 / 403 / 429 / 5xx) to decisions.
  * **NEVER raises.** Ambiguous inputs default to
    ``RETRY_TRANSIENT`` — the safest assumption is "the caller's
    existing retry path was fine before; preserve its semantics".

The Circuit Breaker (Slice 7c) reads the ``RetryDecision`` and
decides whether to trip. The classifier ITSELF is decision-free
about retry counts / windows / backoff — it labels one failure
at a time. State machine purity (operator binding):
*"Maintain state machine purity — the breaker is a consumer of
ExhaustionWatcher and SessionBudget, not a parallel store."*

This module is the PURE-DATA half of that contract.

Slice plan reminder:
  * 7a — this module (PURE DATA, no wiring).
  * 7b — BoundedCancellationGuard primitive (transport-layer
    socket abort via ``response.connection.transport.abort()``).
  * 7c — CircuitBreaker state machine (consumes 7a + composes
    ExhaustionWatcher + SessionBudgetAuthority).
  * 7d — Wire BoundedCancellationGuard into ClaudeProvider stream.
  * 7e — Wire CircuitBreaker into CandidateGenerator._call_fallback.
"""

from __future__ import annotations

import enum
from typing import Optional


# ============================================================================
# Closed taxonomy — RetryDecision enum
# ============================================================================


class RetryDecision(str, enum.Enum):
    """Closed 4-value retry-policy classification.

    Members:
      * ``RETRY_TRANSIENT`` — transient fault; retry will likely
        succeed after a brief backoff. Caller's existing retry
        loop is structurally correct. Examples: TimeoutError,
        5xx server errors, transient transport drops, premature
        stream closes.

      * ``TERMINAL_STRUCTURAL`` — mathematically impossible to
        succeed without external state change. Retrying CANNOT
        help. The 35-min hang from bt-2026-05-21-214521 was this:
        ``cost_estimate > session_remaining`` is a hard
        inequality. Examples:
        ``SessionBudgetPreflightRefused``.

      * ``TERMINAL_QUOTA`` — quota or rate-limit refusal. Might
        recover after a longer window, but within the op's
        time budget the call is effectively terminal. Examples:
        HTTP 429, ``RateLimitError``, daily/per-minute API cap.

      * ``TERMINAL_CONFIG`` — configuration / authentication
        fault. Cannot recover without an operator action.
        Examples: HTTP 401 / 403 / 404, missing model,
        invalid API key.

    The downstream Circuit Breaker maps each decision to a state
    transition:

      RETRY_TRANSIENT     → CLOSED (count toward OPEN_TRANSIENT trip)
      TERMINAL_STRUCTURAL → OPEN_TERMINAL (1× trip)
      TERMINAL_QUOTA      → OPEN_TERMINAL (Nth within window)
      TERMINAL_CONFIG     → OPEN_TERMINAL (1× trip)

    The 4-value cardinality is AST-pinned in
    ``tests/governance/test_provider_retry_classifier.py``."""

    RETRY_TRANSIENT     = "retry_transient"
    TERMINAL_STRUCTURAL = "terminal_structural"
    TERMINAL_QUOTA      = "terminal_quota"
    TERMINAL_CONFIG     = "terminal_config"


# ============================================================================
# Specific failure-class registries — fixed mathematical / config faults
# ============================================================================


# Failure class strings that ALWAYS map to TERMINAL_STRUCTURAL
# regardless of the surrounding failure_mode bucketing. The empirical
# fault from bt-2026-05-21-214521 is the canonical entry — adding a
# class here means "retrying this provably cannot help".
_TERMINAL_STRUCTURAL_CLASSES: frozenset = frozenset({
    "SessionBudgetPreflightRefused",
})


# Failure class strings that ALWAYS map to TERMINAL_CONFIG. These
# represent provider auth / configuration faults that cannot recover
# without operator intervention (rotate API key, fix model name, etc.).
_TERMINAL_CONFIG_CLASSES: frozenset = frozenset({
    "AuthenticationError",
    "InvalidApiKey",
    "PermissionDeniedError",
    "ModelNotFound",
    "NotFoundError",
    "BadRequestError",
})


# Failure class strings that map to TERMINAL_QUOTA. The breaker
# (Slice 7c) decides whether to trip on the FIRST quota hit or wait
# for the Nth — that is policy. This module only labels.
_TERMINAL_QUOTA_CLASSES: frozenset = frozenset({
    "RateLimitError",
    "QuotaExceededError",
    "DailyCapReached",
})


# ============================================================================
# HTTP status fallback table — used only when class + mode are inconclusive
# ============================================================================


_HTTP_STATUS_TERMINAL_CONFIG: frozenset = frozenset({401, 403, 404})
_HTTP_STATUS_TERMINAL_QUOTA: frozenset = frozenset({429})
# HTTP 5xx and 408 (Request Timeout) are transient.
_HTTP_STATUS_RETRY_TRANSIENT: frozenset = frozenset({
    408, 500, 502, 503, 504,
})


# ============================================================================
# FailureMode → RetryDecision default mapping
# ============================================================================
#
# Composes the existing 7-value ``candidate_generator.FailureMode``
# enum. The mapping is intentionally lenient on the
# RETRY_TRANSIENT side — the post-Slice-7 outer-retry loop is now
# safe to invoke for these modes because the Circuit Breaker
# (Slice 7c) bounds the total retry count via the trip table.
#
# RATE_LIMITED is the only structurally-terminal failure_mode at
# the default level. CONTENT_FAILURE / CONTEXT_OVERFLOW are
# explicitly transient (the provider infra is healthy; the
# request payload needs re-shaping or the next attempt may
# succeed with a different prompt).
#
# CONNECTION_ERROR is RETRY_TRANSIENT at the default level —
# the empirical mis-bucket was about specific FAILURE-CLASS
# STRINGS being routed through CONNECTION_ERROR, not the mode
# itself. The structural fix is the failure_class registry
# above, which takes precedence over this table.

# Stringly-typed to avoid a hard import dependency on
# ``candidate_generator.FailureMode``. The names match the enum's
# ``.name`` attribute exactly. AST pin in the test verifies
# coverage — every FailureMode member must appear here.
_FAILURE_MODE_DEFAULT: dict = {
    "RATE_LIMITED":         RetryDecision.TERMINAL_QUOTA,
    "TIMEOUT":              RetryDecision.RETRY_TRANSIENT,
    "SERVER_ERROR":         RetryDecision.RETRY_TRANSIENT,
    "CONNECTION_ERROR":     RetryDecision.RETRY_TRANSIENT,
    "CONTENT_FAILURE":      RetryDecision.RETRY_TRANSIENT,
    "CONTEXT_OVERFLOW":     RetryDecision.RETRY_TRANSIENT,
    "TRANSIENT_TRANSPORT":  RetryDecision.RETRY_TRANSIENT,
}


# ============================================================================
# Slice 127 — economic reclassification (gated, §33.1 default-FALSE)
# ============================================================================
#
# Root cause (bt-2026-06-07-040933, verify-first): a Claude HTTP 400
# "Your credit balance is too low to access the Anthropic API" surfaces as
# exception class ``BadRequestError`` — which is in ``_TERMINAL_CONFIG_CLASSES``
# — so it was classified ``TERMINAL_CONFIG`` and the per-op Circuit Breaker
# tripped sticky ``OPEN_TERMINAL`` on the FIRST hit, bricking 16 ops. But a
# "credit balance too low" 400 is an ECONOMIC refusal (recoverable once the
# operator funds the lane / after a window), NOT a permanent config fault.
#
# When the caller passes ``economic_reclassify=True`` (it reads the
# ``JARVIS_ECONOMIC_RECLASSIFY_ENABLED`` master from ``economic_router`` — the
# env read stays OUT of this PURE-DATA module, AST-pinned) and the failure
# carries an error MESSAGE recognised as a hard economic block, ``classify``
# returns the recoverable ``TERMINAL_QUOTA`` (the existing closed-taxonomy
# member for an economic/quota refusal) instead of ``TERMINAL_CONFIG``. This
# stays a 4-value closed taxonomy — no new RetryDecision member.
#
# The economic-marker detection COMPOSES ``economic_router.is_hard_economic_block``
# (lazy import — no module cycle, no duplicate marker table). NEVER raises.


def _is_economic_block_message(failure_message: Optional[str]) -> bool:
    """True iff ``failure_message`` is a hard economic block. Composes the
    canonical detector in ``economic_router`` (single source of truth for the
    "balance too low" / "insufficient" / 402 markers). Lazy import keeps the
    classifier free of any governance cycle. NEVER raises."""
    if not failure_message:
        return False
    try:
        from backend.core.ouroboros.governance.economic_router import (
            is_hard_economic_block,
        )
        return is_hard_economic_block(failure_message) is not None
    except Exception:  # noqa: BLE001 — failure-soft, defer to legacy path
        return False


# ============================================================================
# Public API — classify()
# ============================================================================


def classify(
    failure_class: Optional[str],
    failure_mode: Optional[str] = None,
    *,
    http_status: Optional[int] = None,
    failure_message: Optional[str] = None,
    economic_reclassify: bool = False,
) -> RetryDecision:
    """Classify a provider failure into a closed RetryDecision.

    Priority (highest → lowest, first match wins):

      0. **Economic block (Slice 127, gated).** When
         ``economic_reclassify`` is True and ``failure_message`` is a
         hard economic block ("credit balance too low" / "insufficient"
         / 402), return the recoverable ``TERMINAL_QUOTA`` — never the
         sticky ``TERMINAL_CONFIG`` a ``BadRequestError`` would
         otherwise yield. False (default) → this priority is skipped.
      1. ``failure_class`` matches a known TERMINAL_* registry —
         the class string is dispositive (e.g.
         ``SessionBudgetPreflightRefused`` is always
         ``TERMINAL_STRUCTURAL``).
      2. ``http_status`` is dispositive (4xx/5xx tables).
      3. ``failure_mode`` lookup in ``_FAILURE_MODE_DEFAULT``.
      4. Fallback ``RETRY_TRANSIENT`` (preserve pre-Slice-7
         semantics for unrecognised inputs).

    Parameters
    ----------
    failure_class:
        The exception class name as a string, e.g.
        ``"SessionBudgetPreflightRefused"``,
        ``"TimeoutError"``, ``"RateLimitError"``. The producer
        site (``CandidateGenerator._call_fallback``) already
        carries this value in its
        ``fallback_err_class=...`` log payload.
    failure_mode:
        The ``FailureMode.name`` string from the existing
        candidate_generator enum (``"CONNECTION_ERROR"``,
        ``"TIMEOUT"``, etc.). Optional — when None or unknown,
        the failure_class / http_status take precedence.
    http_status:
        HTTP status code from the provider response, when
        available. Optional — used only when neither class nor
        mode is dispositive.
    failure_message:
        The raw exception message (``str(exc)``), when available.
        Optional — used only by the Slice 127 economic-block
        priority-0 check, and only when ``economic_reclassify`` is
        True. Inspected for public economic markers ("credit balance
        too low" etc.) only — never for secrets.
    economic_reclassify:
        Slice 127 gate. When True (the caller sources it from
        ``economic_router.economic_reclassify_enabled()``, default
        FALSE per §33.1), an economic ``failure_message`` routes to
        the recoverable ``TERMINAL_QUOTA`` instead of the sticky
        ``TERMINAL_CONFIG``. False → byte-identical to pre-Slice-127.

    Returns
    -------
    RetryDecision
        Exactly one of the four closed enum members. NEVER raises.

    Examples
    --------
    >>> classify("SessionBudgetPreflightRefused", "CONNECTION_ERROR")
    <RetryDecision.TERMINAL_STRUCTURAL: 'terminal_structural'>

    >>> classify("RateLimitError")
    <RetryDecision.TERMINAL_QUOTA: 'terminal_quota'>

    >>> classify("TimeoutError", "TIMEOUT")
    <RetryDecision.RETRY_TRANSIENT: 'retry_transient'>

    >>> classify(None, None, http_status=401)
    <RetryDecision.TERMINAL_CONFIG: 'terminal_config'>

    >>> classify("UnknownErrorClass")
    <RetryDecision.RETRY_TRANSIENT: 'retry_transient'>
    """
    # Priority 0 (Slice 127, gated): economic block recognition. A
    # provider "credit balance too low" / "insufficient funds" refusal is
    # recoverable (operator funds the lane / quota window) — NOT a permanent
    # config fault. The caller passes ``economic_reclassify`` from the
    # ``economic_router`` master so OFF is byte-identical to pre-Slice-127.
    if economic_reclassify and _is_economic_block_message(failure_message):
        return RetryDecision.TERMINAL_QUOTA

    # Priority 1: failure_class registries (most specific).
    if failure_class:
        if failure_class in _TERMINAL_STRUCTURAL_CLASSES:
            return RetryDecision.TERMINAL_STRUCTURAL
        if failure_class in _TERMINAL_CONFIG_CLASSES:
            return RetryDecision.TERMINAL_CONFIG
        if failure_class in _TERMINAL_QUOTA_CLASSES:
            return RetryDecision.TERMINAL_QUOTA

    # Priority 2: HTTP status table (dispositive when present).
    if http_status is not None:
        if http_status in _HTTP_STATUS_TERMINAL_CONFIG:
            return RetryDecision.TERMINAL_CONFIG
        if http_status in _HTTP_STATUS_TERMINAL_QUOTA:
            return RetryDecision.TERMINAL_QUOTA
        if http_status in _HTTP_STATUS_RETRY_TRANSIENT:
            return RetryDecision.RETRY_TRANSIENT

    # Priority 3: FailureMode default table.
    if failure_mode and failure_mode in _FAILURE_MODE_DEFAULT:
        return _FAILURE_MODE_DEFAULT[failure_mode]

    # Priority 4: safe fallback — preserve pre-Slice-7 retry
    # semantics for unrecognised inputs. Unknown failures are
    # treated as transient so the existing retry loop's behaviour
    # is byte-equivalent for cases the classifier hasn't been
    # taught yet. The Circuit Breaker's window-based count caps
    # bound the worst case anyway.
    return RetryDecision.RETRY_TRANSIENT


# ============================================================================
# Introspection helpers — used by tests + Slice 7c integration
# ============================================================================


def known_terminal_structural_classes() -> frozenset:
    """Frozen set of failure_class strings that classify as
    TERMINAL_STRUCTURAL. Returned for AST pin coverage."""
    return _TERMINAL_STRUCTURAL_CLASSES


def known_terminal_config_classes() -> frozenset:
    """Frozen set of failure_class strings that classify as
    TERMINAL_CONFIG."""
    return _TERMINAL_CONFIG_CLASSES


def known_terminal_quota_classes() -> frozenset:
    """Frozen set of failure_class strings that classify as
    TERMINAL_QUOTA."""
    return _TERMINAL_QUOTA_CLASSES


def known_failure_modes() -> frozenset:
    """Frozen set of FailureMode names with explicit mappings.
    The Slice 7a AST pin asserts coverage of every
    ``candidate_generator.FailureMode`` enum member."""
    return frozenset(_FAILURE_MODE_DEFAULT.keys())


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "RetryDecision",
    "classify",
    "known_terminal_structural_classes",
    "known_terminal_config_classes",
    "known_terminal_quota_classes",
    "known_failure_modes",
]
