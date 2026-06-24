"""agent_message_bus -- the Zero-Trust per-graph swarm bus (Phase 1c, G3).

The inter-agent coordination layer for the Sovereign Multi-Agent Swarm. Lets
synthesized workers (Phase 1a) exchange ARTIFACT_HANDOFF / CLARIFICATION /
FINDING / STATUS messages WITHOUT ever trusting one another.

Security model -- Zero-Trust, fail-CLOSED, advisory-only:
    * The bus is constructed per-ExecutionGraph with a graph-scoped secret
      (``secrets.token_bytes(32)``). It is torn down + GC'd on DAG completion
      (the scheduler graph ``finally``). There is NO global / persistent bus.
    * Every ingress message passes a Zero-Trust gate: HMAC signature verify
      (``hmac.compare_digest``), sender authenticity (must be a REGISTERED
      member of THIS graph -- a worker cannot claim to be the Commander),
      privilege-injection ban (the payload is DATA, never authority -- no
      ``grant_tool`` / ``raise_budget`` / ``authority`` / ``role`` directives),
      Tier -1 sanitization (control-char strip + length cap + secret-shape
      redaction), and bounded admission (per-worker inbox maxsize, dedup LRU,
      TTL expiry, drop-oldest backpressure + single lag signal).
    * Any verify/parse/probe failure DROPS the message and emits a
      SovereignYield. A delivered message can NEVER grant tools, raise a
      budget, alter scope, or carry governance directives -- it is data.
    * A signature minted in graph A fails verification in graph B (different
      secret) -> DROP. Cross-graph isolation is structural.

**Gated ``JARVIS_SWARM_MESSAGE_BUS_ENABLED`` (default false).** OFF -> no bus
is created by the scheduler; workers stay silent exactly as Phase 1b.

REUSE (extends, does not fork):
    * ``CommandBus`` bounded-heap discipline (bounded maxsize + dedup LRU +
      drop-oldest backpressure) -- mirrored, NOT subclassed (different message
      semantics: per-worker inbox vs L1 command priority queue).
    * ``secure_logging.sanitize_for_log`` + ``conversation_bridge.redact_secrets``
      -- the Tier -1 sanitizer.
    * ``hmac`` + ``hmac.compare_digest`` -- graph-scoped signature.
    * ``ide_observability_stream.publish_sovereign_yield`` -- spoof/elevation
      drop + deadlock SSE yield (best-effort).
"""
from __future__ import annotations

import collections
import enum
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.conversation_bridge import redact_secrets
from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)

_BUS_SCHEMA_VERSION = "swarm.msg.1c"


# ---------------------------------------------------------------------------
# env helpers (mirror ephemeral_memory_sandbox conventions)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def bus_enabled() -> bool:
    """Master gate. Default FALSE -> no bus (workers silent as Phase 1b)."""
    return _env_bool("JARVIS_SWARM_MESSAGE_BUS_ENABLED", False)


def _inbox_maxsize() -> int:
    return _env_int("JARVIS_SWARM_BUS_INBOX_MAXSIZE", 256)


def _dedup_capacity() -> int:
    return _env_int("JARVIS_SWARM_BUS_DEDUP_CAPACITY", 1024)


def _max_payload_bytes() -> int:
    return _env_int("JARVIS_SWARM_BUS_MAX_PAYLOAD_BYTES", 65536)


def _max_payload_str_len() -> int:
    return _env_int("JARVIS_SWARM_BUS_MAX_STR_LEN", 8192)


def _max_payload_depth() -> int:
    return _env_int("JARVIS_SWARM_BUS_MAX_PAYLOAD_DEPTH", 8)


def _default_ttl_s() -> float:
    return float(_env_int("JARVIS_SWARM_BUS_DEFAULT_TTL_S", 300))


# ---------------------------------------------------------------------------
# message kinds
# ---------------------------------------------------------------------------


class MessageKind(enum.Enum):
    """The advisory-coordination message taxonomy. NEVER an authority verb."""

    ARTIFACT_HANDOFF = "artifact_handoff"
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_RESPONSE = "clarification_response"
    FINDING = "finding"
    STATUS = "status"


# ---------------------------------------------------------------------------
# privilege-injection ban (ContextElevation)
# ---------------------------------------------------------------------------

# The payload is DATA. A received message can NEVER grant tools, raise a
# budget, alter scope, or carry governance directives. Any key OR string value
# matching these privilege-elevation shapes -> DROP + SovereignYield. This is
# the structural ban on authority-as-data.
_ELEVATION_KEYS = frozenset(
    {
        "elevate",
        "grant_tool",
        "grant_tools",
        "raise_budget",
        "mutation_budget",
        "budget_override",
        "authority",
        "context_elevation",
        "system",
        "role",
        "tool_allowlist",
        "allowed_tools",
        "tools",
        "scope",
        "scope_paths",
        "owned_paths",
        "privilege",
        "sudo",
        "admin",
        "override",
        "system_prompt",
        "system_prompt_template",
    }
)

# Substrings that, when appearing in a key OR a string value, signal an
# embedded control directive / role-injection / privilege-elevation attempt.
_ELEVATION_SUBSTRINGS = (
    "grant_tool",
    "raise_budget",
    "context_elevation",
    "mutation_budget",
    "tool_allowlist",
    "you are now",
    "ignore previous",
    "ignore all previous",
    "act as",
    "as the commander",
    "as fleet_commander",
    "you are the commander",
    "system:",
    "role:",
    "sudo ",
)


def _is_elevation_attempt(payload: Any, *, _depth: int = 0) -> bool:
    """Recursively scan a payload for privilege-elevation shapes.

    Fail-CLOSED: any scan error -> treat as an elevation attempt (DROP).
    """
    try:
        if _depth > _max_payload_depth():
            # Pathologically deep -> treat as hostile.
            return True
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = str(key).strip().lower()
                if key_norm in _ELEVATION_KEYS:
                    return True
                if any(sub in key_norm for sub in _ELEVATION_SUBSTRINGS):
                    return True
                if _is_elevation_attempt(value, _depth=_depth + 1):
                    return True
            return False
        if isinstance(payload, (list, tuple)):
            return any(
                _is_elevation_attempt(item, _depth=_depth + 1) for item in payload
            )
        if isinstance(payload, str):
            low = payload.strip().lower()
            return any(sub in low for sub in _ELEVATION_SUBSTRINGS)
        return False
    except Exception:  # noqa: BLE001 -- fail-CLOSED
        logger.debug("[AgentBus] elevation scan raised -> treating as attempt", exc_info=True)
        return True


# ---------------------------------------------------------------------------
# the message
# ---------------------------------------------------------------------------


@dataclass
class AgentMessage:
    """One AI-to-AI coordination message. Advisory only -- never authority.

    ``signature`` is computed over the canonical form of every field EXCEPT
    itself, using the graph-scoped secret. ``to_worker`` may be a worker id or
    a topic string.
    """

    msg_id: str
    from_worker: str
    to_worker: str
    kind: MessageKind
    payload: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    ttl_s: float = 0.0
    ts: float = 0.0
    signature: str = ""
    schema_version: str = _BUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.ts <= 0.0:
            self.ts = time.time()
        if self.ttl_s <= 0.0:
            self.ttl_s = _default_ttl_s()

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        ref = time.time() if now is None else now
        return (ref - self.ts) > self.ttl_s

    def canonical_bytes(self) -> bytes:
        """Deterministic byte representation of every field EXCEPT signature.

        Used as the HMAC message. ``sort_keys`` + compact separators make the
        encoding stable regardless of dict insertion order, so a tampered
        payload (re-ordered or mutated) changes the digest.
        """
        body = {
            "msg_id": self.msg_id,
            "from_worker": self.from_worker,
            "to_worker": self.to_worker,
            "kind": self.kind.value if isinstance(self.kind, MessageKind) else str(self.kind),
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "ttl_s": self.ttl_s,
            "ts": self.ts,
            "schema_version": self.schema_version,
        }
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")


class _DropReason(str, enum.Enum):
    SPOOFED_SENDER = "spoofed_sender"
    UNREGISTERED_SENDER = "unregistered_sender"
    BAD_SIGNATURE = "bad_signature"
    CONTEXT_ELEVATION = "context_elevation_attempt"
    MALFORMED = "malformed_message"
    OVERSIZED = "oversized_payload"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    UNKNOWN_RECIPIENT = "unknown_recipient"
    INBOX_FULL = "inbox_full"


def _emit_yield(op_id: str, reason: str) -> None:
    """Best-effort SovereignYield emission. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_sovereign_yield,
        )

        publish_sovereign_yield(op_id, reason)
    except Exception:  # noqa: BLE001 -- fail-soft
        logger.debug("[AgentBus] publish_sovereign_yield failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# the Zero-Trust per-graph bus
# ---------------------------------------------------------------------------


class AgentMessageBus:
    """A per-ExecutionGraph, HMAC-signed, fail-CLOSED inter-agent bus.

    Constructed with a graph-scoped secret. Each registered worker has a
    bounded inbox (CommandBus-style discipline). The Zero-Trust ingress gate
    runs on EVERY send; a delivered message is data, never authority.
    """

    def __init__(
        self,
        *,
        graph_id: str,
        op_id: str = "",
        secret: Optional[bytes] = None,
        inbox_maxsize: Optional[int] = None,
    ) -> None:
        self.graph_id = graph_id
        self.op_id = op_id or graph_id
        # The graph-scoped secret -- minted fresh per bus. A signature from
        # another graph fails here (cross-graph isolation).
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._inbox_maxsize = inbox_maxsize if inbox_maxsize else _inbox_maxsize()
        # Registered members of THIS graph -- only these may send.
        self._members: set[str] = set()
        # Per-worker bounded inbox (drop-oldest backpressure).
        self._inboxes: Dict[str, Deque[AgentMessage]] = {}
        # Dedup LRU by msg_id (bounded).
        self._seen_ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._dedup_capacity = _dedup_capacity()
        # Pending request/response correlation (request() round-trip).
        self._responses: Dict[str, AgentMessage] = {}
        # Counters (operator signal; never content).
        self.dropped: Dict[str, int] = collections.defaultdict(int)
        self.delivered: int = 0
        self.lag_signalled: bool = False
        self._destroyed = False

    # -- identity ---------------------------------------------------------

    def register_worker(self, worker_id: str) -> None:
        """Register ``worker_id`` as a member of THIS graph (at spawn).

        Only registered members may send. Registration also provisions the
        worker's bounded inbox.
        """
        if self._destroyed:
            return
        wid = str(worker_id)
        if not wid:
            return
        self._members.add(wid)
        self._inboxes.setdefault(wid, collections.deque(maxlen=self._inbox_maxsize))

    def is_member(self, worker_id: str) -> bool:
        return str(worker_id) in self._members

    def members(self) -> Tuple[str, ...]:
        return tuple(sorted(self._members))

    # -- signing ----------------------------------------------------------

    def sign(self, msg: AgentMessage) -> str:
        """Compute the graph-scoped HMAC-SHA256 over the canonical message."""
        return hmac.new(
            self._secret, msg.canonical_bytes(), hashlib.sha256
        ).hexdigest()

    def make_signed(
        self,
        *,
        from_worker: str,
        to_worker: str,
        kind: MessageKind,
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: str = "",
        ttl_s: float = 0.0,
        msg_id: Optional[str] = None,
    ) -> AgentMessage:
        """Build a message and stamp it with THIS bus's signature.

        Convenience for legitimate in-graph senders. A rogue worker that does
        not hold the secret cannot produce a valid signature.
        """
        msg = AgentMessage(
            msg_id=msg_id or secrets.token_hex(8),
            from_worker=str(from_worker),
            to_worker=str(to_worker),
            kind=kind,
            payload=dict(payload or {}),
            correlation_id=str(correlation_id),
            ttl_s=ttl_s,
        )
        msg.signature = self.sign(msg)
        return msg

    # -- dedup ------------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:
        return msg_id in self._seen_ids

    def _record_seen(self, msg_id: str) -> None:
        self._seen_ids[msg_id] = None
        self._seen_ids.move_to_end(msg_id)
        while len(self._seen_ids) > self._dedup_capacity:
            self._seen_ids.popitem(last=False)

    # -- ingress gate (THE security boundary) -----------------------------

    def _verify_signature(self, msg: AgentMessage) -> bool:
        """Constant-time signature verify against the graph secret.

        A signature minted with a different secret (another graph) fails here.
        """
        try:
            expected = self.sign(msg)
            provided = str(msg.signature or "")
            if not provided:
                return False
            return hmac.compare_digest(expected, provided)
        except Exception:  # noqa: BLE001 -- fail-CLOSED
            return False

    def _sanitize_payload(self, payload: Any, *, _depth: int = 0) -> Any:
        """Tier -1 sanitize all string content: control-char strip, length cap,
        secret-shape redaction. Recursive on dict/list. Fail-CLOSED on error."""
        if _depth > _max_payload_depth():
            return None
        if isinstance(payload, str):
            cleaned = sanitize_for_log(payload, max_len=_max_payload_str_len())
            redacted, _ = redact_secrets(cleaned)
            return redacted
        if isinstance(payload, dict):
            return {
                str(sanitize_for_log(str(k), max_len=256)): self._sanitize_payload(
                    v, _depth=_depth + 1
                )
                for k, v in payload.items()
            }
        if isinstance(payload, (list, tuple)):
            return [self._sanitize_payload(item, _depth=_depth + 1) for item in payload]
        # Numbers, bools, None pass through unchanged.
        if isinstance(payload, (int, float, bool)) or payload is None:
            return payload
        # Unknown type -> coerce to sanitized string (never trust repr).
        cleaned = sanitize_for_log(str(payload), max_len=_max_payload_str_len())
        return cleaned

    def _validate_payload_bounds(self, payload: Any) -> bool:
        """Reject non-dict / oversized / un-serializable payloads. No crash."""
        if not isinstance(payload, dict):
            return False
        try:
            encoded = json.dumps(payload, default=str).encode("utf-8")
        except Exception:  # noqa: BLE001 -- un-serializable -> reject
            return False
        return len(encoded) <= _max_payload_bytes()

    def _drop(self, op_id: str, reason: _DropReason, *, yield_it: bool = False) -> bool:
        self.dropped[reason.value] += 1
        logger.warning(
            "[AgentBus] graph=%s DROP reason=%s",
            self.graph_id,
            reason.value,
        )
        if yield_it:
            _emit_yield(op_id, reason.value)
        return False

    def send(self, msg: AgentMessage) -> bool:
        """Zero-Trust ingress. Returns True iff the message was DELIVERED.

        Every failure DROPS (returns False) -- fail-CLOSED. Security-relevant
        drops (spoof / forged signature / elevation / cross-graph) also emit a
        SovereignYield. The bus NEVER raises on a hostile message.
        """
        if self._destroyed:
            return False
        op_id = self.op_id
        try:
            # 0. Structural sanity -- a non-AgentMessage / missing fields.
            if not isinstance(msg, AgentMessage):
                return self._drop(op_id, _DropReason.MALFORMED)
            if not msg.msg_id or not isinstance(msg.kind, MessageKind):
                return self._drop(op_id, _DropReason.MALFORMED)

            # 1. Signature -- missing / forged / tampered -> DROP + yield.
            if not self._verify_signature(msg):
                return self._drop(op_id, _DropReason.BAD_SIGNATURE, yield_it=True)

            # 2. Sender authenticity -- must be a REGISTERED member of THIS
            #    graph. A worker claiming "fleet_commander" / a Commander id /
            #    an unregistered worker -> DROP + yield (spoofed_sender). The
            #    signature alone is not enough: the sender tag must be real.
            sender = str(msg.from_worker or "")
            if sender not in self._members:
                # Distinguish a Commander-impersonation spoof from a plain
                # unregistered sender for clearer telemetry, but both DROP.
                reason = (
                    _DropReason.SPOOFED_SENDER
                    if "command" in sender.lower()
                    else _DropReason.UNREGISTERED_SENDER
                )
                return self._drop(op_id, reason, yield_it=True)

            # 3. Privilege-injection ban (ContextElevation) -- payload is DATA,
            #    never authority. Scan keys + values for elevation shapes.
            if _is_elevation_attempt(msg.payload):
                return self._drop(op_id, _DropReason.CONTEXT_ELEVATION, yield_it=True)

            # 4. Bounded -- reject non-dict / oversized / un-serializable.
            if not self._validate_payload_bounds(msg.payload):
                return self._drop(op_id, _DropReason.OVERSIZED)

            # 5. Sanitize (Tier -1) -- control-char strip + length cap +
            #    secret-shape redaction on all string content.
            msg.payload = self._sanitize_payload(msg.payload)
            if not isinstance(msg.payload, dict):
                return self._drop(op_id, _DropReason.MALFORMED)

            # 6. TTL expiry.
            if msg.is_expired():
                return self._drop(op_id, _DropReason.EXPIRED)

            # 7. Dedup by msg_id (replay of a consumed id -> deduped).
            if self._is_duplicate(msg.msg_id):
                return self._drop(op_id, _DropReason.DUPLICATE)

            # 8. Recipient resolution -- a message to a dead/unknown/expired
            #    recipient is dropped + logged; the sender never blocks.
            recipient = str(msg.to_worker or "")
            inbox = self._inboxes.get(recipient)
            if inbox is None:
                # Topic / unknown recipient -> dropped (advisory; no fanout in
                # 1c). Logged, sender does not block.
                return self._drop(op_id, _DropReason.UNKNOWN_RECIPIENT)

            # 9. Bounded admission -- drop-oldest backpressure + single lag.
            if len(inbox) >= self._inbox_maxsize:
                # deque(maxlen=...) drops the oldest on append; emit a single
                # lag signal per bus so a flood produces one signal, not a
                # storm.
                if not self.lag_signalled:
                    self.lag_signalled = True
                    logger.warning(
                        "[AgentBus] graph=%s inbox lag (maxsize=%d) to=%s",
                        self.graph_id,
                        self._inbox_maxsize,
                        recipient,
                    )
                self.dropped[_DropReason.INBOX_FULL.value] += 1

            self._record_seen(msg.msg_id)
            inbox.append(msg)
            self.delivered += 1

            # Correlation bookkeeping for request/response round-trips.
            if msg.kind is MessageKind.CLARIFICATION_RESPONSE and msg.correlation_id:
                self._responses[msg.correlation_id] = msg
            return True
        except Exception:  # noqa: BLE001 -- the bus NEVER crashes on a message.
            logger.debug("[AgentBus] send raised -> DROP (fail-closed)", exc_info=True)
            self.dropped[_DropReason.MALFORMED.value] += 1
            return False

    # -- egress -----------------------------------------------------------

    def subscribe(self, worker_id: str) -> Deque[AgentMessage]:
        """Return the worker's bounded inbox deque (auto-provisions on demand
        for a registered member; unknown workers get an empty bounded deque)."""
        wid = str(worker_id)
        inbox = self._inboxes.get(wid)
        if inbox is None:
            inbox = collections.deque(maxlen=self._inbox_maxsize)
            if wid in self._members:
                self._inboxes[wid] = inbox
        return inbox

    def request(
        self,
        *,
        from_worker: str,
        to_worker: str,
        payload: Dict[str, Any],
        timeout_s: float = 0.0,
        correlation_id: Optional[str] = None,
    ) -> Optional[AgentMessage]:
        """Send a CLARIFICATION_REQUEST and (synchronously) return any already
        delivered CLARIFICATION_RESPONSE bound to the correlation id.

        This is the round-trip primitive that drives the stagnation / deadlock
        check (callers feed the request+response transcript to the detector).
        A message to a dead/unknown recipient is dropped; the caller never
        blocks (returns None). ``timeout_s`` is reserved for an async caller
        that polls ``response_for``; this sync form does not sleep.
        """
        corr = correlation_id or secrets.token_hex(8)
        msg = self.make_signed(
            from_worker=from_worker,
            to_worker=to_worker,
            kind=MessageKind.CLARIFICATION_REQUEST,
            payload=dict(payload or {}),
            correlation_id=corr,
        )
        self.send(msg)
        return self._responses.get(corr)

    def response_for(self, correlation_id: str) -> Optional[AgentMessage]:
        """Return the CLARIFICATION_RESPONSE bound to ``correlation_id`` if any."""
        return self._responses.get(str(correlation_id))

    # -- teardown ---------------------------------------------------------

    def destroy(self) -> None:
        """Per-graph lifecycle isolation: tear down the bus + zero the secret.

        Called in the scheduler graph ``finally`` (alongside the 1b sandbox
        vaporization). After destroy(), the bus is inert (every send DROPs) and
        the secret is gone, so no message -- even a validly-signed one minted
        before destroy -- can be replayed against this bus. NEVER raises.
        """
        try:
            self._destroyed = True
            self._members.clear()
            self._inboxes.clear()
            self._seen_ids.clear()
            self._responses.clear()
            # Overwrite the secret so it cannot be recovered from the object.
            self._secret = b""
        except Exception:  # noqa: BLE001 -- teardown must never break cleanup.
            logger.debug("[AgentBus] destroy raised (non-fatal)", exc_info=True)

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Pure-read operator metrics -- never content. NEVER raises."""
        try:
            return {
                "graph_id": self.graph_id,
                "members": len(self._members),
                "delivered": int(self.delivered),
                "dropped": dict(self.dropped),
                "lag_signalled": bool(self.lag_signalled),
                "destroyed": bool(self._destroyed),
                "schema_version": _BUS_SCHEMA_VERSION,
            }
        except Exception:  # noqa: BLE001
            return {"graph_id": self.graph_id, "destroyed": True}
