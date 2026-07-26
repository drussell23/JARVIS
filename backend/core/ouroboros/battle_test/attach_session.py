"""Which attached cockpit asked — carried, not passed.

The problem
-----------
59 REPL verbs execute correctly in the daemon and render to its local console.
An attached ``ov`` terminal sends the command, the daemon runs it, and the
operator sees nothing: ``serpent_flow`` prints verb results straight to
``self._flow.console``, which is the daemon's stdout, not the wire.

Mirroring them is one line. Mirroring them CORRECTLY is not, because the
moment two cockpits are attached, "broadcast the output" means terminal A's
``/moltbook`` lands in terminal B's scrollback with no indication of why.

Why a ContextVar
----------------
The publish call is ~15 frames below the dispatch site, inside render helpers
that legitimately know nothing about IPC sessions. Threading a ``session_id``
parameter down would mean touching every renderer, every verb, and every
future one — a change proportional to the surface rather than to the defect.
That is the switch statement wearing a different hat.

:class:`contextvars.ContextVar` is the language's answer to exactly this: a
value scoped to the current task, propagated automatically across every
``await`` in that task and copied into child tasks at creation. Set it once
where the command enters, read it once where output leaves.

The ambient case is the important one
-------------------------------------
Most daemon output has NO originating session: op chrome, breadcrumbs,
provider failover, receipts, moltbook posts written by the reaction engine.
That output is situational awareness and every cockpit should see it. So the
contract is:

    session set   → deliver to THAT cockpit only  (operator asked)
    session unset → broadcast to all              (organism volunteered)

Unset is the default, so existing behaviour is byte-identical unless a verb is
actually dispatched from an attached terminal.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import uuid
from typing import Iterator, Optional

logger = logging.getLogger("Ouroboros.AttachSession")

_TRUTHY = ("1", "true", "yes", "on")

#: The cockpit that asked for whatever is currently being rendered, or None
#: when the organism is speaking on its own initiative.
_CURRENT_SESSION: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ov_attach_session", default=None,
)


def session_routing_enabled() -> bool:
    """``JARVIS_ATTACH_SESSION_ROUTING`` (default ON).

    OFF restores pure broadcast — every attached cockpit sees every verb
    result, which is the pre-session behaviour and the only honest A/B for
    this change."""
    return os.environ.get(
        "JARVIS_ATTACH_SESSION_ROUTING", "1",
    ).strip().lower() in _TRUTHY


def new_session_id() -> str:
    """A cockpit's identity for the life of one attachment.

    Random rather than derived from pid or socket fd: a client may reconnect,
    and a reused identity would deliver the previous attachment's pending
    output to whoever inherited the descriptor."""
    return uuid.uuid4().hex[:12]


def current_session() -> Optional[str]:
    """The cockpit awaiting this output, or None for ambient output."""
    try:
        return _CURRENT_SESSION.get()
    except LookupError:      # pragma: no cover — default makes this impossible
        return None


@contextlib.contextmanager
def session_scope(session_id: Optional[str]) -> Iterator[None]:
    """Attribute everything rendered inside this block to *session_id*.

    Restores the previous value on exit, including on exception, so a verb
    that raises cannot leave later ambient output addressed to one terminal.
    NEVER raises."""
    token = None
    try:
        if session_id and session_routing_enabled():
            token = _CURRENT_SESSION.set(session_id)
        yield
    finally:
        if token is not None:
            try:
                _CURRENT_SESSION.reset(token)
            except (ValueError, LookupError):
                # The block was entered in one context and left in another
                # (a task boundary). Falling back to an explicit clear is
                # correct: leaking a session is worse than losing one.
                _CURRENT_SESSION.set(None)


def as_ambient() -> "contextlib.AbstractContextManager[None]":
    """Force the enclosed output to broadcast even inside a verb dispatch.

    For the case where a verb legitimately produces something every cockpit
    should see — an operator pausing intake concerns all of them."""
    return session_scope(None) if current_session() is None else _Ambient()


class _Ambient(contextlib.AbstractContextManager):
    def __enter__(self) -> None:
        self._token = _CURRENT_SESSION.set(None)
        return None

    def __exit__(self, *exc: object) -> None:
        try:
            _CURRENT_SESSION.reset(self._token)
        except (ValueError, LookupError):
            _CURRENT_SESSION.set(None)


__all__ = [
    "as_ambient",
    "current_session",
    "new_session_id",
    "session_routing_enabled",
    "session_scope",
]
