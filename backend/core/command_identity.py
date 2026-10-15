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

The conversation a command follows on from travels the same way
(:data:`command_conversation`): a follow-up ("which one is on teams?") was
decided, fetched and answered by layers that each saw only its words, so it
was classified alone, read the wrong day and was answered from the screen.
Every layer that decides something reads the exchange from here.

And where the command's answer is heard (:data:`command_voice`): JARVIS spoke
every line on the speakers of the machine it runs on, so a user on another
device -- a browser across the network, a Remote Desktop client -- heard it
late or not at all, and nothing could stop a line once it started. A client
that plays JARVIS's voice itself declares it; its transport sets the voice
here, and the one speech primitive (``safe_say``) delivers every line said
for that command -- the reply, an acknowledgement, a description -- to it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator, List, Mapping, Optional, Sequence, Tuple

from backend.core.module_identity import unify as _unify_module_identity

_unify_module_identity(__name__)

#: The id of the command being processed in this context, or None.
command_request_id: ContextVar[Optional[str]] = ContextVar("jarvis_command_request_id", default=None)

#: The exchange the command in this context follows on from (``role`` /
#: ``content`` turns, oldest first); empty when it starts a conversation.
command_conversation: ContextVar[Tuple[Mapping[str, Any], ...]] = ContextVar(
    "jarvis_command_conversation", default=())

#: Delivers one spoken line -- ``(text, source)`` -- to the client the command
#: came from, True once that client has it to play. None: the host speaks.
VoiceSink = Callable[[str, str], Awaitable[bool]]

#: Where the lines said for the command in this context are heard, or None.
command_voice: ContextVar[Optional[VoiceSink]] = ContextVar("jarvis_command_voice", default=None)

#: What a client declares (``subscribe``) when it plays JARVIS's voice itself.
VOICE_PLAYBACK_CAPABILITY = "voice_playback"

#: The reply text already said for the command in this context, sentence by
#: sentence as it was written (backend.core.speech_segments), or None outside
#: a command. The command's end reads it so a voiced reply is not said twice.
command_voiced: ContextVar[Optional[List[str]]] = ContextVar("jarvis_command_voiced", default=None)


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


@contextmanager
def voice_scope(sink: Optional[VoiceSink]) -> Iterator[None]:
    """Run the enclosed processing with its lines heard through *sink*
    (None: on the host's speakers)."""
    token = command_voice.set(sink)
    try:
        yield
    finally:
        command_voice.reset(token)


@contextmanager
def voiced_scope() -> Iterator[List[str]]:
    """Record, for the enclosed command, the reply text voiced while it was written."""
    record: List[str] = []
    token = command_voiced.set(record)
    try:
        yield record
    finally:
        command_voiced.reset(token)


def note_voiced(text: str) -> None:
    """Record *text* as said for the command in this context (no command: nothing)."""
    record = command_voiced.get()
    if record is not None and text:
        record.append(text)


def in_command() -> bool:
    """Is a user's command being answered in this context? Its reply is then
    what is said (plus one acknowledgement when slow): a narrator that speaks
    for its own step -- "Let me check your calendar.", "Done." -- is a second
    voice over the answer, heard with nothing on screen (2026-10-15)."""
    return command_voiced.get() is not None


def voiced_covers(reply: str) -> bool:
    """Was *reply* already said, sentence by sentence, for this command?
    Compared by its words: spacing and the splitter's cuts do not matter."""
    record = command_voiced.get()
    if not record or not reply:
        return False
    return " ".join(" ".join(record).split()) == " ".join(reply.split())


@contextmanager
def conversation_scope(turns: Sequence[Mapping[str, Any]]) -> Iterator[None]:
    """Run the enclosed processing as a command following on from *turns*."""
    token = command_conversation.set(tuple(turns))
    try:
        yield
    finally:
        command_conversation.reset(token)


def conversation_text(turns: Optional[Sequence[Mapping[str, Any]]] = None) -> str:
    """The exchange as a prompt states it -- each turn as said, oldest first,
    one per line -- or "" when there is none. *turns* defaults to the
    conversation of the command being processed."""
    said = command_conversation.get() if turns is None else turns
    lines = []
    for turn in said:
        content = str(turn.get("content", "")).strip()
        if content:
            who = "User" if turn.get("role") == "user" else "JARVIS"
            lines.append(f"- {who}: {content}")
    return "\n".join(lines)


def conversation_preamble() -> str:
    """The conversation, introduced for a prompt that decides something about
    the command, or "" when the command starts a conversation."""
    said = conversation_text()
    if not said:
        return ""
    return ("The conversation so far, oldest first -- the request may follow on from it: "
            "a word pointing back, or a detail left out because it was already said "
            "(the day, the event, the message), means what the conversation said; a "
            "request complete on its own takes nothing from it:\n" + said + "\n\n")


__all__ = ["VOICE_PLAYBACK_CAPABILITY", "VoiceSink", "command_conversation", "command_request_id",
           "command_scope", "command_voice", "command_voiced", "conversation_preamble",
           "conversation_scope", "conversation_text", "in_command", "note_voiced", "request_id_of",
           "voice_scope", "voiced_covers", "voiced_scope"]
