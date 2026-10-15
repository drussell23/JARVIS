"""
command_identity - the identity a user's command carries from its transport
to the pipeline that runs it.

A typed command is sent with a ``command_id`` and may arrive more than once:
over the WebSocket, and again over REST when the client's delivery or answer
is late. The pipeline makes the command idempotent by that id
(``UnifiedCommandProcessor.process_command``), but between the transport and
the pipeline sit context handlers, voice APIs and wrappers that each took the
command text alone -- so the id never reached the dedupe and every resend ran
the command again (two searches, two windows, two voices, 2026-10-14).

The transport sets :data:`command_request_id` where the command enters; the
pipeline reads it. A ContextVar follows the command through every await and
into the tasks it starts, without each layer in between having to pass it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping, Optional

from backend.core.module_identity import unify as _unify_module_identity

_unify_module_identity(__name__)

#: The id of the command being processed in this context, or None.
command_request_id: ContextVar[Optional[str]] = ContextVar("jarvis_command_request_id", default=None)


def request_id_of(message: Mapping[str, Any]) -> Optional[str]:
    """The id a client gave a command (``command_id``; ``request_id`` and the
    camel-case ``requestId`` are the other spellings in use), or None."""
    for key in ("command_id", "request_id", "requestId"):
        value = message.get(key)
        if value:
            return str(value)
    return None


@contextmanager
def command_scope(request_id: Optional[str]) -> Iterator[None]:
    """Run the enclosed processing as the command *request_id*."""
    token = command_request_id.set(request_id)
    try:
        yield
    finally:
        command_request_id.reset(token)


__all__ = ["command_request_id", "command_scope", "request_id_of"]
