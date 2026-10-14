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
    """A random identity for one attachment -- used when the cockpit has no
    stable terminal to be identified by (see :func:`cockpit_identity`).

    Random rather than derived from pid or socket fd: a reused fd would
    deliver the previous attachment's pending output to whoever inherited
    the descriptor."""
    return uuid.uuid4().hex[:12]


def cockpit_identity() -> str:
    """The cockpit's identity ACROSS attachments: the same operator terminal
    gets the same id, so output addressed to it while it was away (a /session
    answer that finished after the operator detached) and its /session itself
    find it again when `ov` re-attaches (FOREGROUND_SESSION, replay buffer).

    The identity is the terminal, not a descriptor -- the trust boundary the
    random id was guarding stays drawn: ``JARVIS_COCKPIT_ID`` when the
    operator names it; else a digest of the uid and the facts that identify
    THIS terminal window: the Windows Terminal / macOS terminal session ids,
    the tmux server + pane, and the controlling tty WITH its allocation time
    (a recycled /dev/pts/N is a different terminal and gets a different id).
    No terminal facts at all (piped, a service) -> random, as before.
    ``JARVIS_COCKPIT_STABLE_ID=false`` restores per-attachment random ids.
    NEVER raises."""
    try:
        explicit = os.environ.get("JARVIS_COCKPIT_ID", "").strip()
        if explicit:
            return "".join(c for c in explicit if c.isalnum() or c in "-_")[:32] or new_session_id()
        if os.environ.get("JARVIS_COCKPIT_STABLE_ID", "1").strip().lower() not in _TRUTHY:
            return new_session_id()
        facts = []
        for var in ("WT_SESSION", "TERM_SESSION_ID", "TMUX", "TMUX_PANE"):
            val = os.environ.get(var, "").strip()
            if val:
                facts.append(f"{var}={val}")
        try:
            tty = os.ttyname(0)
            facts.append(f"tty={tty}@{int(os.stat(tty).st_ctime)}")
        except OSError:
            pass
        if not facts:
            return new_session_id()
        import hashlib  # noqa: PLC0415
        uid = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "")
        return hashlib.sha256("|".join([uid, *facts]).encode()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return new_session_id()


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


# ---------------------------------------------------------------------------
# Lanes — WHICH worker produced this, orthogonal to which cockpit sees it
# ---------------------------------------------------------------------------

#: The default lane. Output with no worker above it on the stack is the
#: organism speaking as itself, which is exactly the "ambient" the deck
#: already renders — the two vocabularies line up on purpose.
AMBIENT_LANE = "ambient"

#: Which worker's execution this output belongs to.
#:
#: The session ContextVar answers "who asked?". This one answers "who is
#: doing it?". They are independent: one cockpit can watch four lanes, and
#: one lane's output can be addressed to a cockpit that asked about it.
#:
#: Tagging at emission is the only approach that survives contact with the
#: codebase. Parsing output for a worker id fails the moment a worker prints
#: something unformatted; keying on logger name fails because workers share
#: modules; passing an id down the call chain would touch every renderer and
#: every future one. The execution context already knows, and carries itself
#: across every ``await`` for free.
_CURRENT_LANE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ov_lane", default=AMBIENT_LANE,
)


def lane_tagging_enabled() -> bool:
    """``JARVIS_LANE_TAGGING`` (default ON). OFF makes every emission
    ambient, which is the pre-lane behaviour."""
    return os.environ.get(
        "JARVIS_LANE_TAGGING", "1",
    ).strip().lower() in _TRUTHY


def current_lane() -> str:
    """The worker whose execution is producing this output."""
    try:
        return _CURRENT_LANE.get() or AMBIENT_LANE
    except LookupError:      # pragma: no cover
        return AMBIENT_LANE


@contextlib.contextmanager
def lane_scope(lane: Optional[str]) -> Iterator[None]:
    """Attribute everything emitted inside this block to *lane*.

    Nests correctly: a swarm worker that spawns a sub-worker gets the inner
    lane for the inner work and the outer lane restored afterwards, because
    the token is per-``set`` rather than global. NEVER raises."""
    token = None
    try:
        if lane and lane_tagging_enabled():
            token = _CURRENT_LANE.set(str(lane))
        yield
    finally:
        if token is not None:
            try:
                _CURRENT_LANE.reset(token)
            except (ValueError, LookupError):
                _CURRENT_LANE.set(AMBIENT_LANE)


__all__ = [
    "AMBIENT_LANE",
    "as_ambient",
    "cockpit_identity",
    "current_lane",
    "current_session",
    "lane_scope",
    "lane_tagging_enabled",
    "new_session_id",
    "session_routing_enabled",
    "session_scope",
]
