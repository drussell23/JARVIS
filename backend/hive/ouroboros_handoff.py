"""
Ouroboros Handoff — Thread Consensus to OperationContext
========================================================

Serializes a Hive thread that has reached CONSENSUS into an
:class:`OperationContext` for the Ouroboros governance pipeline.

The resulting context enters the pipeline at CLASSIFY phase with:

- ``description`` extracted from the Reactor's approval reasoning
- ``strategic_memory_prompt`` containing the full serialized thread history
- ``human_instructions`` containing Manifesto principles cited during debate
- ``causal_trace_id`` linking back to the originating thread
- ``correlation_id`` matching the thread for cross-system tracing
"""

from __future__ import annotations

import dataclasses
import json
from typing import Tuple

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    _compute_hash,
    _context_to_hash_dict,
)
from backend.hive.thread_models import (
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_consensus_description(thread: HiveThread) -> str:
    """Return the Reactor's approval reasoning from the thread.

    Iterates *thread.messages* in reverse to find the most recent
    :class:`PersonaReasoningMessage` from the ``reactor`` persona with
    ``intent == VALIDATE`` and ``validate_verdict == "approve"``.

    Falls back to *thread.title* when no matching message is found.
    """
    for msg in reversed(thread.messages):
        if (
            isinstance(msg, PersonaReasoningMessage)
            and msg.persona == "reactor"
            and msg.intent == PersonaIntent.VALIDATE
            and msg.validate_verdict == "approve"
        ):
            return msg.reasoning

    return thread.title


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def serialize_consensus(
    thread: HiveThread,
    *,
    target_files: Tuple[str, ...],
) -> OperationContext:
    """Convert a CONSENSUS-state thread into an initial OperationContext.

    Parameters
    ----------
    thread:
        A :class:`HiveThread` whose ``state`` is ``ThreadState.CONSENSUS``.
    target_files:
        Tuple of file paths the resulting operation will target.

    Returns
    -------
    OperationContext
        A fresh context in CLASSIFY phase, enriched with thread metadata.

    Raises
    ------
    ValueError
        If *thread.state* is not ``ThreadState.CONSENSUS``.
    """
    # 1. Guard: thread must be in CONSENSUS
    if thread.state != ThreadState.CONSENSUS:
        raise ValueError(
            f"Cannot serialize thread {thread.thread_id!r}: "
            f"expected state CONSENSUS, got {thread.state.value}"
        )

    # 2. Extract description from Reactor's approval
    description = _extract_consensus_description(thread)

    # 3. Serialize full thread history as JSON
    thread_history = {
        "thread_id": thread.thread_id,
        "title": thread.title,
        "trigger_event": thread.trigger_event,
        "messages": [m.to_dict() for m in thread.messages],
    }
    thread_history_json = json.dumps(thread_history, sort_keys=True, default=str)

    # 4. Collect Manifesto principles into a human-readable string
    principles_string = ""
    if thread.manifesto_principles:
        principles_string = (
            "Manifesto principles cited during Hive debate:\n"
            + "\n".join(f"- {p}" for p in thread.manifesto_principles)
        )

    # 5. Create initial OperationContext (CLASSIFY phase)
    ctx = OperationContext.create(
        target_files=target_files,
        description=description,
        correlation_id=thread.thread_id,
    )

    # 6. Enrich with thread metadata via dataclasses.replace()
    ctx = dataclasses.replace(
        ctx,
        causal_trace_id=thread.thread_id,
        strategic_memory_prompt=thread_history_json,
        human_instructions=principles_string,
    )

    # 7. Recompute the context hash to cover the replaced fields
    fields_for_hash = _context_to_hash_dict(ctx)
    new_hash = _compute_hash(fields_for_hash)
    ctx = dataclasses.replace(ctx, context_hash=new_hash)

    return ctx
