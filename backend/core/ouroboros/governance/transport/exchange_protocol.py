"""Stage-1 cross-host acceptance exchange protocol (Task-5 wiring, 2026-07-04).

The ValidationExchange (driver-side, unit-proven) needs a REAL Brain-side
counterpart to clear the loopback guard. This module is the shared, pure
vocabulary between the Mac driver's endpoint adapters and the Brain's echo
responder -- zero I/O, fully unit-testable.

Round-trip semantics
--------------------
Mac -> Brain proof: the Mac publishes exchange events; the Brain's responder
ECHOES each one back (``echo_of_op`` = the original op_id). An echo observed on
the Mac proves the Brain OBSERVED the original -- strictly stronger than the
loopback it replaces.

Brain -> Mac proof: the Mac publishes a *command* (``cmd=publish`` + a nonce);
the responder answers with a fresh BRAIN-ORIGIN event carrying the nonce. A
nonce observed on the Mac is an event the Brain itself published.

Loop safety: the responder never reacts to its own outputs (``origin`` set) nor
to commands' echoes, and only ops with the exchange prefix are ever touched --
the organism's real ``task_started`` traffic is left alone. The WS bridges
additionally dedup by qualified event id.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

# The exchange rides a VALID StreamEventBroker event type (broker rejects
# unknown types); the op-id prefix is the load-bearing discriminator.
EXCHANGE_EVENT_TYPE = "task_started"
EXCHANGE_OP_PREFIX = "brain-accept-"

K_TAG = "tag"
K_CMD = "cmd"
CMD_PUBLISH = "publish"
K_NONCE = "nonce"
K_ORIGIN = "origin"
ORIGIN_BRAIN = "brain"
ORIGIN_ECHO = "brain-echo"
K_ECHO_OF_OP = "echo_of_op"


def is_exchange_op(op_id: str) -> bool:
    return isinstance(op_id, str) and op_id.startswith(EXCHANGE_OP_PREFIX)


def make_publish_cmd(nonce: str) -> Dict[str, Any]:
    """Payload asking the Brain to publish a brain-origin event with ``nonce``."""
    return {K_CMD: CMD_PUBLISH, K_NONCE: nonce}


def respond(
    event_type: str, op_id: str, payload: Optional[Mapping[str, Any]],
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """The Brain responder's pure decision: given one observed event, return the
    list of ``(event_type, op_id, payload)`` publishes it must answer with.

    - a publish-command -> one BRAIN-ORIGIN event carrying the nonce;
    - a plain exchange event -> one ECHO carrying the original op_id;
    - anything self-originated / already-echoed / non-exchange -> nothing.
    """
    if event_type != EXCHANGE_EVENT_TYPE or not is_exchange_op(op_id):
        return []
    p = dict(payload or {})
    if p.get(K_ORIGIN) in (ORIGIN_BRAIN, ORIGIN_ECHO) or K_ECHO_OF_OP in p:
        return []  # never react to our own outputs
    if p.get(K_CMD) == CMD_PUBLISH:
        nonce = str(p.get(K_NONCE, ""))
        if not nonce:
            return []
        return [(EXCHANGE_EVENT_TYPE, op_id,
                 {K_NONCE: nonce, K_ORIGIN: ORIGIN_BRAIN})]
    if K_TAG in p:
        return [(EXCHANGE_EVENT_TYPE, op_id,
                 {K_ECHO_OF_OP: op_id, K_ORIGIN: ORIGIN_ECHO})]
    return []


def parse_observed(
    event_type: str, op_id: str, payload: Optional[Mapping[str, Any]],
) -> Optional[Tuple[str, str]]:
    """Mac-side classification of an inbound event: returns
    ``("echo", original_op_id)`` for a Brain echo, ``("nonce", nonce)`` for a
    brain-origin nonce event, None for anything else."""
    if event_type != EXCHANGE_EVENT_TYPE or not is_exchange_op(op_id):
        return None
    p = dict(payload or {})
    if p.get(K_ORIGIN) == ORIGIN_ECHO and p.get(K_ECHO_OF_OP):
        return ("echo", str(p[K_ECHO_OF_OP]))
    if p.get(K_ORIGIN) == ORIGIN_BRAIN and p.get(K_NONCE):
        return ("nonce", str(p[K_NONCE]))
    return None
