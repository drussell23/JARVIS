"""Slice 39 Task 5 — DoubleWord Transport-vs-Upstream Failure Disambiguator.

Bifurcation rationale (v34 evidence, PRD §49.6.2)
---------------------------------------------------
The v34 battle-test soak surfaced two structurally distinct DW failure
shapes that require DIFFERENT recovery strategies:

  TRANSPORT — a socket/protocol-level fault.  The peer dropped the TCP
    connection, the TTFT timer expired before any byte arrived, or
    aiohttp signalled ServerDisconnectedError / ClientConnectorError.
    The model never had a chance to speak.  Recovery: flush the
    connection pool and immediately re-probe a *different* socket
    (Task 6 hard-flush path).

  UPSTREAM — the transport layer was *healthy*; the server answered with
    HTTP 200 and a well-formed SSE envelope, but emitted zero content
    tokens before [DONE].  Classic DW ``done_before_content``.  Also
    covers HTTP 5xx responses: the server answered, meaning the socket
    was fine — the model or endpoint faulted server-side.  Recovery:
    demote the model in the PromotionLedger; flushing the connection
    pool would be counterproductive (the socket is healthy) and risks a
    brute-force re-probe loop against a capacity-constrained upstream.

Design goals
------------
1. Pure function — ``classify_surface_failure`` has no I/O, no env
   reads, no side-effects.  Fully deterministic from the ProbeOutcome
   fields alone.
2. Precedence: UPSTREAM markers are checked before TRANSPORT markers.
   ``done_before_content`` therefore always wins, even if a transport
   keyword appears coincidentally in the same message (shouldn't happen,
   but defensive ordering prevents misclassification).
3. Conservative default: ambiguous outcomes (no recognised marker, no
   5xx) resolve to UPSTREAM.  We never flush a connection pool on
   ambiguous signal; Task 6's raw bypass-probe is the mechanism that
   promotes ambiguous → transport when warranted.

References
----------
* v34 soak postmortem — ``memory/project_v34_soak_postmortem.md``
* PRD §49.6.2 — Transport Disambiguation
* Slice 39 plan — ``memory/project_slice_39_transport_health.md``
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("Ouroboros.TransportDisambiguator")

# ---------------------------------------------------------------------------
# Marker tables — all matched case-insensitively as substrings of the
# concatenated (error_message + " " + error_body) blob.
# ---------------------------------------------------------------------------

# Outcomes that indicate the upstream *server* is at fault even when the
# transport layer was healthy (HTTP 200 + clean SSE envelope that produced
# zero content tokens, or a structured server-side fault).
_UPSTREAM_MARKERS: tuple[str, ...] = ("done_before_content",)

# Outcomes that indicate a socket/protocol-level fault.  The model never
# had an opportunity to produce output.
_TRANSPORT_MARKERS: tuple[str, ...] = (
    "stream_closed_early",
    "ttft_timeout",
    "asyncio.wait_for",
    "serverdisconnected",
    "clientconnector",
    "connectionreset",
    "connection reset",
    "session_acquire_failed",
    "prober_raised",      # inner adapter catch (preflight_probe.py:753)
    "probe_raised",       # outer run_preflight catch (preflight_probe.py:428)
    "connecttimeout",
    "connect timeout",
    # ``transport:{ExcType}:...`` prefix is emitted ONLY on the streaming
    # socket-fault path (dw_heavy_probe.py:790) — covers every aiohttp
    # transport exception (ServerTimeoutError / ClientPayloadError /
    # ClientOSError / ...) by prefix instead of enumerating exception names.
    # Safe: this prefix never appears on done_before_content / status_* /
    # entitlement paths, and UPSTREAM markers are checked first regardless.
    "transport:",
)


class FailureClass(str, Enum):
    """Closed taxonomy of surface-failure classifications.

    Values are lowercase strings so they can be embedded directly in
    structured log lines and SSE payloads without further conversion.
    """

    NONE = "none"
    """ProbeOutcome.success is True — no failure to classify."""

    TRANSPORT = "transport"
    """Socket/protocol-level fault — connection pool flush warranted (Task 6)."""

    UPSTREAM = "upstream"
    """Server-side fault or healthy-transport empty-completion — no pool flush."""


def classify_surface_failure(outcome) -> FailureClass:
    """Classify a ``ProbeOutcome`` into a ``FailureClass``.

    Pure function — no I/O, no env reads, no side-effects.

    Precedence (highest to lowest):
      1. success=True                        → NONE
      2. UPSTREAM marker in blob             → UPSTREAM  (checked before transport)
      3. TRANSPORT marker in blob            → TRANSPORT
      4. HTTP 5xx status_code                → UPSTREAM  (server answered = socket ok)
      5. anything else                       → UPSTREAM  (conservative default)

    The conservative default (step 5) is intentional: we never flush a
    connection pool on an ambiguous signal.  Task 6's raw bypass-probe
    is the escalation mechanism for ambiguous → transport promotion.

    Args:
        outcome: Any object with ``success``, ``error_message``,
                 ``error_body``, and ``status_code`` attributes
                 (structurally duck-typed so tests and adapters need not
                 import this module to satisfy the type).

    Returns:
        ``FailureClass`` enum member.
    """
    # 1. Healthy probe — nothing to classify.
    if getattr(outcome, "success", False):
        return FailureClass.NONE

    # 2+3. Build a single lowercase blob from both message fields (handle None
    #      defensively — callers may pass partially-populated outcomes).
    msg = getattr(outcome, "error_message", None) or ""
    body = getattr(outcome, "error_body", None) or ""
    blob = (msg + " " + body).lower()

    # 2. UPSTREAM markers take unconditional precedence.
    if any(marker in blob for marker in _UPSTREAM_MARKERS):
        logger.debug("classify_surface_failure: UPSTREAM (upstream_marker) blob=%r", blob[:120])
        return FailureClass.UPSTREAM

    # 3. Transport markers — socket/protocol fault.
    if any(marker in blob for marker in _TRANSPORT_MARKERS):
        logger.debug("classify_surface_failure: TRANSPORT (transport_marker) blob=%r", blob[:120])
        return FailureClass.TRANSPORT

    # 4. HTTP 5xx — server answered, socket was healthy.
    status_code = getattr(outcome, "status_code", 0) or 0
    if 500 <= status_code < 600:
        logger.debug(
            "classify_surface_failure: UPSTREAM (http_5xx) status=%s", status_code
        )
        return FailureClass.UPSTREAM

    # 5. Conservative default — ambiguous signal; never flush on ambiguity.
    logger.debug(
        "classify_surface_failure: UPSTREAM (default_conservative) blob=%r status=%s",
        blob[:120],
        status_code,
    )
    return FailureClass.UPSTREAM
