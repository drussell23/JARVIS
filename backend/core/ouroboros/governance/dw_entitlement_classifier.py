"""
DW entitlement classifier — Task #86 root fix
=============================================

Pure-function classifier for HTTP 4xx responses from DoubleWord's
``/v1/chat/completions`` endpoint.  Distinguishes three response
shapes that the legacy probe paths conflated as "auth failure":

1. ``AUTH_FAILURE`` — global credential problem (bad key, expired
   token, no auth header).  TRANSIENT — affects every model under
   this key; retrying with a fresh credential could recover.  No
   single model deserves blame.

2. ``ENTITLEMENT_BLOCKED`` — per-model routing-rule rejection (the
   account is authenticated but THIS model is not in the account's
   entitled set).  PERMANENT for the lifetime of the catalog
   snapshot — the policy will not change mid-snapshot; further
   probes are wasted budget.  Single ground-truth signal → flip
   the model's breaker to TERMINAL_OPEN and let the classifier's
   next refresh exclude it.

3. ``OTHER_4XX`` — schema rejection, rate-limit, request
   malformation, etc.  Caller's existing logic applies.

This classifier is the SINGLE SEAM where the legacy
``status in (401, 403) → VERDICT_UNKNOWN`` rule was producing the
wrong behavior for per-model entitlement blocks.  Once routed to
``VERDICT_NON_CHAT`` (modality_probe) + TERMINAL_OPEN (sentinel),
the catalog classifier autonomously filters entitlement-blocked
models on the next discovery cycle — no hardcoded model list, no
manual operator config required.

Why this module is pure
-----------------------
* No I/O — caller passes ``status`` and ``body``.
* No state — every call is independent.
* No env reads at module-import time — marker list resolved at call
  time (operator can extend without restart).

This shape means the same classifier composes into both
``dw_modality_probe.py`` and ``dw_heavy_probe.py`` without coupling.

Operator-tunable marker list
----------------------------
The default marker set covers DoubleWord's currently-observed
entitlement-block response body patterns:

  * ``"blocked by a routing rule"`` — the canonical phrase
  * ``"contact your administrator"`` — common variant
  * ``"request access"`` — alternative DW phrasing

Operators can extend (or replace) the marker list via
``JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS`` (CSV).  Empty/missing →
defaults.  Markers are matched case-insensitively as substrings of
the response body excerpt.

Why marker-based, not status-only
---------------------------------
HTTP 403 is overloaded.  DoubleWord (and most provider APIs) returns
403 for BOTH "your API key is wrong" AND "this model is not in your
plan".  The status alone cannot disambiguate.  The body marker is
the deterministic discriminant — and it comes from DW itself, so we
adapt automatically to whatever phrasing DW uses without hardcoding
model IDs.

Authority invariant
-------------------
This module imports only stdlib.  AST-pinned in the Task #86 spine
(``tests/governance/test_dw_entitlement_classifier.py``).  It carries
no authority — the modality_probe + heavy_probe + sentinel decide
what to DO with the classification; the classifier only labels.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


# ---------------------------------------------------------------------------
# Closed taxonomy (operator §33.1 discipline)
# ---------------------------------------------------------------------------


KIND_AUTH_FAILURE: str = "auth_failure"
KIND_ENTITLEMENT_BLOCKED: str = "entitlement_blocked"
KIND_OTHER_4XX: str = "other_4xx"

_VALID_KINDS = frozenset({
    KIND_AUTH_FAILURE,
    KIND_ENTITLEMENT_BLOCKED,
    KIND_OTHER_4XX,
})


# ---------------------------------------------------------------------------
# Env knob (operator-tunable, read at call time)
# ---------------------------------------------------------------------------


_ENV_MARKERS = "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS"

# Defaults observed empirically in DW responses (bt-2026-05-14-000028
# soak).  These are response-body substrings DW emits when a
# per-model routing rule blocks access — distinct from a global
# auth failure.  Matching is case-insensitive substring (lowercase
# pre-folded for hot-path speed).
_DEFAULT_MARKERS_LOWER: Tuple[str, ...] = (
    "blocked by a routing rule",
    "contact your administrator",
    "request access",
)


def _resolved_markers_lower() -> Tuple[str, ...]:
    """Resolve the operator-tunable marker list.

    Read at call time so a runtime ``os.environ[...] = ...`` propagates
    without process restart (preserves monkey-patch + hot-reload).
    Empty/whitespace → defaults.  CSV-parsed, individual entries
    whitespace-trimmed and lowercased.
    """
    raw = os.environ.get(_ENV_MARKERS, "").strip()
    if not raw:
        return _DEFAULT_MARKERS_LOWER
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else _DEFAULT_MARKERS_LOWER


# ---------------------------------------------------------------------------
# Result + classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """The classifier's labeling of one 4xx response.

    Attributes
    ----------
    kind:
        One of :data:`KIND_AUTH_FAILURE`, :data:`KIND_ENTITLEMENT_BLOCKED`,
        :data:`KIND_OTHER_4XX`.  Closed taxonomy — caller can pattern-match
        exhaustively.
    matched_marker:
        For ``KIND_ENTITLEMENT_BLOCKED`` — the specific marker substring
        that matched (operator-actionable for log forensics).  Empty
        string for other kinds.
    is_permanent:
        ``True`` iff the failure is per-model permanent and the caller
        should mark the model's circuit breaker TERMINAL_OPEN.  Only
        ``KIND_ENTITLEMENT_BLOCKED`` produces ``True``.  Derived field,
        kept on the result for callers that want a single bool.
    """

    kind: str
    matched_marker: str = ""
    is_permanent: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"ClassificationResult.kind must be one of "
                f"{sorted(_VALID_KINDS)!r}, got {self.kind!r}"
            )


def classify_4xx(status: int, body: str) -> ClassificationResult:
    """Classify a 4xx HTTP response from DW's chat-completions endpoint.

    Parameters
    ----------
    status:
        HTTP status code (200-599; values outside 400-499 are valid
        callers but always classify as ``KIND_OTHER_4XX`` since the
        classifier specializes 4xx).
    body:
        Response body excerpt (caller is responsible for length-bounding
        — the classifier does NOT slice; it accepts whatever the caller
        provides and matches via ``in`` on a lowercased copy).

    Returns
    -------
    ClassificationResult
        Closed-taxonomy verdict.

    Decision table
    --------------

    +--------+---------------------+-----------------------+
    | status | body matches marker | kind                  |
    +========+=====================+=======================+
    | 401    | *                   | AUTH_FAILURE          |
    | 403    | yes                 | ENTITLEMENT_BLOCKED   |
    | 403    | no                  | AUTH_FAILURE          |
    | other  | yes                 | ENTITLEMENT_BLOCKED   |
    |        |                     |   (e.g. some APIs use |
    |        |                     |    402/451 for this)  |
    | other  | no                  | OTHER_4XX             |
    +--------+---------------------+-----------------------+

    Notes:
      * 401 is ALWAYS auth — entitlement blocks come back as 403/4xx.
        DW has never been observed returning 401 with the entitlement
        marker in the body (per soak evidence).
      * 403 without marker is treated as auth (the legacy assumption is
        preserved — adding the marker discriminant is purely additive).
      * Non-{401,403} status codes with the marker are still classified
        ENTITLEMENT_BLOCKED — accommodates providers using 402 (Payment
        Required) or 451 (Unavailable For Legal Reasons) for the same
        semantic.
    """
    body_lower = (body or "").lower()
    has_marker = any(m in body_lower for m in _resolved_markers_lower())

    # 401 is always auth — no provider mixes entitlement-block into 401.
    if status == 401:
        return ClassificationResult(
            kind=KIND_AUTH_FAILURE,
            matched_marker="",
            is_permanent=False,
        )

    if has_marker:
        # Find the specific marker that matched for log forensics.
        matched = ""
        for m in _resolved_markers_lower():
            if m in body_lower:
                matched = m
                break
        return ClassificationResult(
            kind=KIND_ENTITLEMENT_BLOCKED,
            matched_marker=matched,
            is_permanent=True,
        )

    if status == 403:
        # 403 without marker → legacy auth interpretation preserved.
        return ClassificationResult(
            kind=KIND_AUTH_FAILURE,
            matched_marker="",
            is_permanent=False,
        )

    return ClassificationResult(
        kind=KIND_OTHER_4XX,
        matched_marker="",
        is_permanent=False,
    )


__all__ = [
    "KIND_AUTH_FAILURE",
    "KIND_ENTITLEMENT_BLOCKED",
    "KIND_OTHER_4XX",
    "ClassificationResult",
    "classify_4xx",
]
