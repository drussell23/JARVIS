"""swarm_sentinel -- the Sentinel Ingress Filter (Epistemic Purity Sanitizer).

Phase 1d security-critical boundary. Sits at the bus->recipient delivery seam:
BEFORE a recipient worker ingests a peer message's free-text content, that
content passes :func:`epistemic_purity_filter`.

THE RULE (epistemic purity):
    Workers may pass DECLARATIVE data + clarification context to one another.
    They may NEVER inject new EXECUTION INSTRUCTIONS. The Fleet Commander is
    the ONLY entity authorized to issue imperative instructions. A worker that
    has been prompt-injected and tries to relay "ignore previous instructions"
    / "you must now ..." / a role-override to a peer is a jailbreak vector --
    the Sentinel strips or drops the offending content so the peer's context
    is never poisoned.

COMPOSITION (the Sentinel is the CONTENT/semantic layer, NOT a replacement):
    * The AgentMessageBus Zero-Trust identity gate (HMAC per-worker signature)
      + structural data-only ``quarantine_payload`` run FIRST, at ingress. A
      FORGED message (spoofed sender / cross-graph secret) is DROPPED there and
      NEVER reaches the Sentinel.
    * The Sentinel runs at READ time -- on a message that already PASSED the
      identity/structure layers -- and inspects the SEMANTIC content for
      imperative-injection. It is defense-in-depth ATOP identity, not instead
      of it.

REUSE (no new injection regexes, no new transport):
    * ``semantic_firewall.scan_tool_output`` -- the EXACT 11 prompt-injection
      detectors (role-override, ``<|system|>``, XML instruction-injection,
      gate-bypass, "you are now", "ignore previous instructions", ...) already
      maintained for the GENERAL subagent tool-output scanner. The Sentinel
      does NOT define its own patterns -- it reuses this scanner and acts on
      its ``injection_count`` / ``redacted`` output. Credential shapes are
      intentionally excluded (a worker may legitimately hand a peer a config
      value); imperative-injection is the threat here.

FAIL-CLOSED:
    Any unparseable input / detector exception / ambiguity is treated AS an
    injection: the message is dropped (default) or stripped, NEVER passed
    through untouched.

SENDER-AWARE:
    A Commander-sourced message (``sender_is_commander=True``) MAY carry
    imperative instructions -- the Commander is the sole imperative authority,
    so its content passes UNTOUCHED. A worker->worker message
    (``sender_is_commander=False``) carrying imperative/injection content is
    stripped or dropped per :func:`sentinel_mode`.

Gated by the swarm master flags (the bus gate). When the bus is OFF no message
is ever delivered, so the Sentinel is never reached -- byte-identical to 1c.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_SENTINEL_SCHEMA_VERSION = "swarm.sentinel.1d"

# The marker the reused scanner substitutes for a matched imperative span. We
# expose it so the strip-mode result can be reasoned about + tested without
# importing the firewall's private constant.
_REDACTION_MARKER = "[TOOL_INJECTION_REDACTED]"


# ---------------------------------------------------------------------------
# env knobs (mirror agent_message_bus conventions)
# ---------------------------------------------------------------------------


def sentinel_mode() -> str:
    """Disposition for a worker->worker imperative-injection.

    ``JARVIS_SWARM_SENTINEL_MODE`` in {``strip``, ``drop``}. Default ``drop``
    (fail-CLOSED: the safest disposition -- the poisoned message never enters
    the recipient's context at all). ``strip`` redacts the offending spans and
    delivers the cleaned remainder. Any unrecognized value falls back to the
    fail-CLOSED ``drop``.
    """
    raw = (os.environ.get("JARVIS_SWARM_SENTINEL_MODE", "drop") or "drop").strip().lower()
    return raw if raw in ("strip", "drop") else "drop"


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


class FilterDisposition(str, enum.Enum):
    """What the Sentinel decided about a message's content."""

    PASS = "pass"      # declarative / clarification -> delivered untouched
    STRIPPED = "stripped"  # imperative spans redacted; cleaned remainder delivered
    DROPPED = "dropped"    # imperative content -> message withheld entirely


@dataclass(frozen=True)
class FilterResult:
    """Structured outcome of the epistemic-purity filter.

    ``allowed`` is the authoritative deliver/withhold signal: True means the
    recipient may ingest ``content`` (which is the ORIGINAL text on PASS, or
    the redacted remainder on STRIPPED). False (DROPPED) means the message must
    be withheld from the recipient entirely.
    """

    allowed: bool
    disposition: FilterDisposition
    content: str = ""
    injection_count: int = 0
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = _SENTINEL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# telemetry (best-effort, fail-soft -- never raises into the filter)
# ---------------------------------------------------------------------------


def _emit_sentinel_block(op_id: str, reason: str) -> None:
    """Best-effort ``swarm_sentinel_block`` telemetry. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_swarm_sentinel_block,
        )

        publish_swarm_sentinel_block(op_id, reason)
    except Exception:  # noqa: BLE001 -- fail-soft
        logger.debug(
            "[SwarmSentinel] publish_swarm_sentinel_block failed (non-fatal)",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# the filter (THE epistemic-purity boundary)
# ---------------------------------------------------------------------------


def epistemic_purity_filter(
    message: str,
    *,
    sender_is_commander: bool,
    op_id: str = "",
) -> FilterResult:
    """Filter peer message free-text for imperative-injection.

    Parameters
    ----------
    message:
        The free-text content the recipient worker is about to ingest.
    sender_is_commander:
        True iff the sender is the Fleet Commander -- the ONLY entity allowed
        to issue imperative instructions. Commander content passes UNTOUCHED.
        False for any worker->worker message: imperative/injection content is
        stripped or dropped per :func:`sentinel_mode`.
    op_id:
        For ``swarm_sentinel_block`` telemetry context (observability only).

    Returns
    -------
    FilterResult
        ``allowed`` + ``disposition`` are authoritative. NEVER raises.

    Security model:
        * Declarative data ("here is the parsed AST {...}", "the test at line
          40 failed with X") contains no imperative-injection signatures ->
          PASS, delivered untouched.
        * A worker->worker message with an imperative/injection signature
          ("ignore previous instructions", "you must now", "you are now",
          ``<|system|>``, XML instruction-injection) -> STRIPPED or DROPPED
          (``sentinel_mode``) + ``swarm_sentinel_block`` telemetry.
        * Commander-sourced imperative content -> PASS (sole imperative
          authority).
        * Fail-CLOSED: unparseable / detector exception / ambiguity is treated
          AS injection (drop/strip), never passed through.
    """
    # 0. Coerce / validate input. A non-str message is ambiguous -> fail-CLOSED.
    try:
        if not isinstance(message, str):
            return _fail_closed(op_id, reason="non_string_message")
    except Exception:  # noqa: BLE001 -- the type check itself raised: fail-CLOSED
        return _fail_closed(op_id, reason="message_inspection_raised")

    # 1. Run the REUSED semantic_firewall injection scanner. It returns the
    #    redacted text + how many of the 11 prompt-injection patterns fired.
    #    No new regexes are defined here.
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            scan_tool_output,
        )

        scan = scan_tool_output(message, tool_name="swarm_peer_message")
        injection_count = int(getattr(scan, "injection_count", 0) or 0)
        redacted = getattr(scan, "redacted", None)
        if not isinstance(redacted, str):
            # The scanner contract guarantees a str; a non-str is anomalous ->
            # fail-CLOSED rather than trust an unexpected shape.
            return _fail_closed(op_id, reason="scanner_returned_non_string")
    except Exception:  # noqa: BLE001 -- detector raised -> fail-CLOSED (treat as injection)
        logger.debug(
            "[SwarmSentinel] scan_tool_output raised -> fail-CLOSED", exc_info=True
        )
        return _fail_closed(op_id, reason="scanner_exception")

    # 2. Clean (no imperative-injection detected) -> PASS untouched. This is
    #    the declarative-data common case for both workers AND the Commander.
    if injection_count <= 0:
        return FilterResult(
            allowed=True,
            disposition=FilterDisposition.PASS,
            content=message,
            injection_count=0,
        )

    # 3. Imperative-injection detected.
    #    The Fleet Commander is the SOLE imperative authority -> its content
    #    (even containing instructions) passes UNTOUCHED. A worker is NOT
    #    authorized to issue imperatives -> strip or drop.
    if sender_is_commander:
        return FilterResult(
            allowed=True,
            disposition=FilterDisposition.PASS,
            content=message,
            injection_count=injection_count,
            reasons=("commander_imperative_authorized",),
        )

    # worker -> worker with imperative content: this is the jailbreak vector.
    mode = sentinel_mode()
    _emit_sentinel_block(op_id, "worker_imperative_injection:" + mode)
    logger.warning(
        "[SwarmSentinel] op=%s BLOCK worker->worker imperative-injection "
        "patterns=%d mode=%s",
        op_id or "?",
        injection_count,
        mode,
    )

    if mode == "strip":
        # Deliver the redacted remainder (the imperative spans replaced by the
        # firewall's marker). The declarative parts survive; the instruction is
        # neutralized.
        return FilterResult(
            allowed=True,
            disposition=FilterDisposition.STRIPPED,
            content=redacted,
            injection_count=injection_count,
            reasons=("worker_imperative_stripped",),
        )

    # mode == "drop" (default, fail-CLOSED): withhold entirely.
    return FilterResult(
        allowed=False,
        disposition=FilterDisposition.DROPPED,
        content="",
        injection_count=injection_count,
        reasons=("worker_imperative_dropped",),
    )


def _fail_closed(op_id: str, *, reason: str) -> FilterResult:
    """Build the fail-CLOSED result for an ambiguous/erroring input.

    Honors :func:`sentinel_mode`: ``strip`` yields an empty cleaned remainder
    (delivered, but with nothing left); ``drop`` (default) withholds entirely.
    Either way the ORIGINAL (untrusted, unparseable) content is NEVER passed
    through. Emits ``swarm_sentinel_block`` telemetry. NEVER raises.
    """
    try:
        mode = sentinel_mode()
    except Exception:  # noqa: BLE001 -- even the env read failed -> hard drop
        mode = "drop"
    _emit_sentinel_block(op_id, "fail_closed:" + reason)
    logger.warning(
        "[SwarmSentinel] op=%s fail-CLOSED reason=%s mode=%s", op_id or "?", reason, mode
    )
    if mode == "strip":
        return FilterResult(
            allowed=True,
            disposition=FilterDisposition.STRIPPED,
            content="",
            injection_count=1,
            reasons=("fail_closed:" + reason,),
        )
    return FilterResult(
        allowed=False,
        disposition=FilterDisposition.DROPPED,
        content="",
        injection_count=1,
        reasons=("fail_closed:" + reason,),
    )


__all__ = [
    "FilterDisposition",
    "FilterResult",
    "epistemic_purity_filter",
    "sentinel_mode",
]
