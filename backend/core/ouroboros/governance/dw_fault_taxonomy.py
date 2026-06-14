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
