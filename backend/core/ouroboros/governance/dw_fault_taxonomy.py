"""Slice 185 — strict-type exception segregation (the fault boundary).

Slice 185 research found the smoking gun: a `NameError` in our own RT dispatch code was being
caught, run through the vendor failure-classifier, matched no HTTP/stream regex, and fell into
the catch-all `else → FailureSource.LIVE_TRANSPORT`. We blamed DoubleWord's *network* for OUR
*logic* bug — and worse, recorded it into the vendor surface-health ledger, corrupting the
learned rupture rate ~2×.

This module draws the boundary. An INTERNAL fault (a Python logical error — NameError,
TypeError, AttributeError, …) is OUR codebase bug. It must NEVER be classified as a vendor
rupture, NEVER recorded to the vendor ledger, and NEVER silently degraded — it bubbles up and
crashes loudly so we fix it. A VENDOR fault (transport rupture, HTTP 5xx/429, a malformed
*vendor* JSON response) is the resilience layer's job.
"""
from __future__ import annotations

import json

# Python runtime errors that unambiguously indicate OUR bug, not the vendor's network.
_INTERNAL_FAULT_TYPES = (
    NameError,        # undefined name (incl. UnboundLocalError, its subclass)
    TypeError,        # wrong type / bad call signature
    AttributeError,   # missing attribute
    KeyError,         # missing dict key
    IndexError,       # out-of-range
    ImportError,      # broken import wiring
    AssertionError,   # violated internal invariant
)


def is_internal_fault(exc: BaseException) -> bool:
    """True iff ``exc`` is a Python LOGICAL error (our bug), which must bypass the vendor
    resilience path and crash loudly — NEVER be blamed on the vendor's network.

    Carve-out: ``json.JSONDecodeError`` is a ``ValueError`` subclass but represents a malformed
    *vendor* response, so it stays in the vendor lane (a real DW data fault, not our logic).
    NEVER raises."""
    try:
        if isinstance(exc, json.JSONDecodeError):
            return False  # malformed vendor payload — a vendor fault, not ours
        if isinstance(exc, _INTERNAL_FAULT_TYPES):
            return True
        # ValueError is ambiguous (parse vs logic); treat as internal UNLESS it carries a
        # vendor status_code (i.e., it came structured from the provider layer).
        if isinstance(exc, ValueError):
            return getattr(exc, "status_code", None) is None
        return False
    except Exception:  # noqa: BLE001 — the taxonomy must never itself throw
        return False


# Slice 241 — OP-LEVEL generation/exploration budget exhaustion markers. These are
# RuntimeErrors the Venom tool loop raises when OUR budget runs out (deadline /
# max-rounds / round-starved / ttft-floor) — they bill $0, involve no socket, and
# are NOT a DoubleWord transport rupture. Matched by message because they are
# generic RuntimeErrors (deliberately outside _INTERNAL_FAULT_TYPES, which is for
# Python logic bugs). All carry the unambiguous ``tool_loop_`` prefix.
_GENERATION_TIMEOUT_MARKERS = (
    "tool_loop_deadline",
    "tool_loop_max_rounds",
    "tool_loop_round_budget",
    "tool_loop_starved",
    "generation_timeout",
)


def is_generation_timeout(exc: BaseException) -> bool:
    """True iff ``exc`` is an OP-LEVEL generation/tool-loop BUDGET exhaustion (our
    budget ran out before a candidate was produced), NOT a vendor transport
    rupture. Such failures must be classified ``GENERATION_TIMEOUT`` — never
    ``LIVE_TRANSPORT`` — so they do NOT falsely degrade DoubleWord's surface
    health, inflate the live_transport lane-sever counter, or feed the dead-Claude
    cascade. Message-matched (markers all carry the ``tool_loop_`` prefix), so a
    genuine transport error never matches. NEVER raises → False."""
    try:
        msg = str(exc).lower()
        return any(m in msg for m in _GENERATION_TIMEOUT_MARKERS)
    except Exception:  # noqa: BLE001 — the taxonomy must never itself throw
        return False


# Sovereign Exception Taxonomy (2026-06-20) — FSM-EXHAUSTION segregation. When the
# DW primary yields no candidate AND the Claude fallback is not configured (pure-DW
# autarky), the per-model sentinel dispatch wraps it as a RuntimeError whose message
# is one of these OUR-side FSM-control markers. These are NOT vendor transport
# ruptures — no socket failed, the vendor never even rejected anything. Cloud soak
# 2026-06-20 proved every one of the 16 models got mislabeled ``LIVE_TRANSPORT`` from
# a single ``all_providers_exhausted:fallback_skipped:no_fallback_configured``,
# which then SEVERED the whole DW lane (Slice 73/83 fast-cascade) and corrupted the
# surface-health ledger — turning one op's no-candidate into a lane-wide outage.
_FSM_EXHAUSTION_MARKERS = (
    "no_fallback_configured",
    "all_providers_exhausted",
    "fallback_skipped",
    "sentinel_dispatch_no_fallback",
    "background_dw_blocked_by_topology",
    "speculative_deferred",
)


def is_fsm_exhaustion(exc: BaseException) -> bool:
    """True iff ``exc`` is an OUR-side FSM dispatch exhaustion (DW produced no
    candidate AND no fallback is configured), NOT a vendor transport rupture. Must
    be classified ``FSM_EXHAUSTED`` — never ``LIVE_TRANSPORT`` — so it fails only
    the specific op WITHOUT severing the DW lane or corrupting the vendor
    surface-health ledger (the 2026-06-20 cloud-soak lane-wide-outage root cause).
    Message-matched; a genuine transport error never matches. NEVER raises → False."""
    try:
        msg = str(exc).lower()
        return any(m in msg for m in _FSM_EXHAUSTION_MARKERS)
    except Exception:  # noqa: BLE001 — the taxonomy must never itself throw
        return False


# Dynamic Lane Escalation (Part 2, T4) -- BATCH-LANE RETRIEVAL TIMEOUT. The live
# C2 soak proved the wedge: a DW batch poll/retrieval deadline (the batch was
# POSTed 201, polled 200, but never COMPLETED inside DOUBLEWORD_MAX_WAIT_S) raises
# ``DoublewordInfraError("Batch retrieval failed", status_code=0)``. In pure-DW
# autarky (no Claude fallback) the dispatch loop wraps THAT into a
# ``RuntimeError("all_providers_exhausted:...")`` carrying an ``.exhaustion_report``
# whose ``fsm_failure_mode == "TIMEOUT"`` and ``primary_err_class ==
# "DoublewordInfraError"`` / ``primary_err_msg`` = "Batch retrieval failed". That
# wrapper matches ``is_fsm_exhaustion`` -> classified FSM_EXHAUSTED -> the
# transport breaker's record allowlist deliberately DROPS it (it cannot tell a
# batch-poll deadline from an our-side no-candidate exhaustion). This predicate
# draws the ONE clean line that re-arms the breaker's vision: a *batch-lane*
# *retrieval* TIMEOUT, and NOTHING else. It NEVER matches a generic exhaustion, a
# tool-loop generation deadline, a LOCAL_EGRESS_OVERWEIGHT, or a batch SUBMISSION
# fault -- so it cannot spuriously trip the transport lane on an our-side bug.
_BATCH_RETRIEVAL_TIMEOUT_MARKER = "batch retrieval"


def _carries_batch_retrieval_timeout(exc: BaseException) -> bool:
    """True iff ``exc`` (directly or via its ``.exhaustion_report``) is a DW batch
    *retrieval/poll* deadline -- the ``DoublewordInfraError("Batch retrieval
    failed")`` raised when ``poll_and_retrieve`` returns None on timeout.

    Two shapes are accepted:
      * the BARE ``DoublewordInfraError`` (non-autarky path -- recorded before the
        cascade wraps it), recognised by class name + "batch retrieval" message;
      * the WRAPPED ``RuntimeError("all_providers_exhausted:...")`` (pure-DW
        autarky) carrying ``.exhaustion_report`` with ``fsm_failure_mode ==
        "TIMEOUT"`` AND ``primary_err_class == "DoublewordInfraError"`` AND a
        "batch retrieval" ``primary_err_msg``.

    A batch *submission* fault ("Batch submission failed") is NOT a retrieval
    timeout (different lifecycle stage, often a real 4xx) and is rejected. NEVER
    raises -> False.
    """
    try:
        # Shape 1: the bare DoublewordInfraError (or any exc whose own message is
        # the batch-retrieval marker).
        if type(exc).__name__ == "DoublewordInfraError":
            if _BATCH_RETRIEVAL_TIMEOUT_MARKER in str(exc).lower():
                return True
        # Shape 2: the wrapped all_providers_exhausted RuntimeError. The wrapper's
        # OWN message is "all_providers_exhausted:..." -- the batch-retrieval
        # signal lives in the structured exhaustion_report, NOT the str(exc).
        report = getattr(exc, "exhaustion_report", None)
        if isinstance(report, dict):
            mode = str(report.get("fsm_failure_mode", "")).upper()
            primary_cls = str(report.get("primary_err_class", ""))
            primary_msg = str(report.get("primary_err_msg", "")).lower()
            if (
                mode == "TIMEOUT"
                and primary_cls == "DoublewordInfraError"
                and _BATCH_RETRIEVAL_TIMEOUT_MARKER in primary_msg
            ):
                return True
        return False
    except Exception:  # noqa: BLE001 -- the taxonomy must never itself throw
        return False


def is_batch_lane_retrieval_timeout(exc: BaseException, *, lane: str) -> bool:
    """True iff ``exc`` is a DW *batch-lane* *retrieval* TIMEOUT trippable by the
    transport circuit breaker -- the ONE failure class that should rotate the op
    off the wedged batch lane onto realtime.

    Both conditions MUST hold:
      * ``lane == "batch"`` -- the attempt was actually on the batch lane (a
        realtime attempt must NEVER feed a batch-lane trip), AND
      * ``exc`` carries a batch-retrieval deadline (see
        :func:`_carries_batch_retrieval_timeout`).

    Excluded by construction (so the breaker is never spuriously tripped on an
    our-side fault): generic FSM exhaustion (no batch-retrieval signal),
    tool-loop generation deadlines (``is_generation_timeout``),
    ``LocalEgressOverweightError`` (``is_local_egress_overweight``), batch
    SUBMISSION faults, and any internal Python logic bug. NEVER raises -> False.
    """
    try:
        if lane != "batch":
            return False
        if exc is None:
            return False
        # An our-side egress block or a tool-loop generation deadline can never be
        # a batch-retrieval timeout; reject defensively even though their messages
        # already wouldn't match the marker.
        if is_local_egress_overweight(exc) or is_generation_timeout(exc):
            return False
        return _carries_batch_retrieval_timeout(exc)
    except Exception:  # noqa: BLE001 -- the taxonomy must never itself throw
        return False


# Dynamic Lane Escalation (Part 2, T5) -- REALTIME-LANE COLLAPSE TIMEOUT. After
# T4 rotates a wedged op off the batch lane onto realtime, the realtime lane can
# ALSO time out (DW under heavy global load -- the streaming/generation deadline
# elapses with no candidate). That is "lane collapse": BOTH transport lanes have
# now failed by TIMEOUT for the SAME op. Left unhandled the immortal queue would
# re-attempt forever at the same (too-small) deadline. T5 detects this so the
# dispatcher can emit [SOVEREIGN YIELD: LANE COLLAPSE] and DILATE the deadline a
# bounded number of times before falling through to the existing immortal/DLQ
# backstop. This predicate mirrors ``is_batch_lane_retrieval_timeout`` exactly --
# it draws the ONE clean line: a *realtime-lane* generation/streaming TIMEOUT,
# and nothing else (never a batch timeout, a tool-loop generation budget, an
# our-side egress block, or an internal Python bug).
_REALTIME_LANE_TIMEOUT_MODE = "TIMEOUT"


def _carries_realtime_timeout(exc: BaseException) -> bool:
    """True iff ``exc`` (directly or via its ``.exhaustion_report``) is a DW
    *realtime/streaming* generation TIMEOUT -- the deadline elapsed before a
    candidate was produced on the realtime lane.

    Two shapes are accepted, mirroring :func:`_carries_batch_retrieval_timeout`:
      * the WRAPPED ``RuntimeError("all_providers_exhausted:...")`` (pure-DW
        autarky) carrying ``.exhaustion_report`` with ``fsm_failure_mode ==
        "TIMEOUT"`` -- the realtime lane never returned a candidate inside the
        per-op deadline; this is the dominant shape after a batch->realtime
        rotation in autarky;
      * the BARE ``asyncio.TimeoutError`` (the realtime ``wait_for`` budget
        elapsed and was recorded before any cascade wrapping).

    A *batch* retrieval timeout (recognised by the "batch retrieval" message)
    is explicitly NOT a realtime timeout and is rejected so the two predicates
    never overlap. NEVER raises -> False.
    """
    try:
        # A batch-retrieval timeout is a different lane class -- exclude it so
        # the realtime predicate can never fire on a batch wedge.
        if _carries_batch_retrieval_timeout(exc):
            return False
        # Shape 1: the wrapped all_providers_exhausted RuntimeError whose
        # structured report records a TIMEOUT failure mode.
        report = getattr(exc, "exhaustion_report", None)
        if isinstance(report, dict):
            mode = str(report.get("fsm_failure_mode", "")).upper()
            if mode == _REALTIME_LANE_TIMEOUT_MODE:
                return True
        # Shape 2: the bare asyncio.TimeoutError (realtime wait_for budget
        # elapsed). Matched by class name to avoid importing asyncio here.
        if type(exc).__name__ in ("TimeoutError", "CancelledError"):
            # CancelledError is the timeout's underlying mechanism in some
            # asyncio paths; treat a bare cancellation on realtime as a timeout.
            if type(exc).__name__ == "CancelledError":
                # Only when not carrying a non-timeout report.
                return report is None
            return True
        return False
    except Exception:  # noqa: BLE001 -- the taxonomy must never itself throw
        return False


def is_realtime_lane_timeout(exc: BaseException, *, lane: str) -> bool:
    """True iff ``exc`` is a DW *realtime-lane* generation TIMEOUT -- the
    failure class that, AFTER a batch->realtime rotation, signals LANE COLLAPSE
    (both transport lanes exhausted by timeout for this op).

    Both conditions MUST hold:
      * ``lane == "realtime"`` -- the attempt was actually on the realtime lane
        (a batch attempt must NEVER feed a realtime-collapse signal), AND
      * ``exc`` carries a realtime generation/streaming TIMEOUT (see
        :func:`_carries_realtime_timeout`).

    Excluded by construction (so a collapse is never spuriously declared on an
    our-side fault): batch-retrieval timeouts (``is_batch_lane_retrieval_timeout``
    -- a different lane class), tool-loop generation deadlines
    (``is_generation_timeout``), ``LocalEgressOverweightError``
    (``is_local_egress_overweight``), and any internal Python logic bug. NEVER
    raises -> False.
    """
    try:
        if lane != "realtime":
            return False
        if exc is None:
            return False
        # An our-side egress block or a tool-loop generation budget deadline can
        # never be a transport-lane realtime timeout; reject defensively.
        if is_local_egress_overweight(exc) or is_generation_timeout(exc):
            return False
        return _carries_realtime_timeout(exc)
    except Exception:  # noqa: BLE001 -- the taxonomy must never itself throw
        return False


def is_local_egress_overweight(exc: BaseException) -> bool:
    """True iff ``exc`` is a ``LocalEgressOverweightError`` — OUR-side egress
    interceptor refusing to dispatch a body bigger than the local ceiling
    (Sovereign Egress Interceptor Mesh, T1). This is NOT a vendor rupture: no
    socket failed, DoubleWord never even received the request — WE blocked it to
    stay a good API citizen. Like FSM_EXHAUSTED it must classify weight-0.0
    (``FailureSource.LOCAL_EGRESS_OVERWEIGHT``) so it NEVER trips the DW model /
    topology breaker or corrupts surface-health; instead the orchestrator routes
    it BACK to context-aware chunking. Type-matched (no fragile string match) via
    an isolated lazy import so this taxonomy module stays import-light and
    NEVER raises → False."""
    try:
        from backend.core.ouroboros.governance.dw_egress_interceptor import (  # noqa: PLC0415
            LocalEgressOverweightError,
        )
        return isinstance(exc, LocalEgressOverweightError)
    except Exception:  # noqa: BLE001 — the taxonomy must never itself throw
        return False
