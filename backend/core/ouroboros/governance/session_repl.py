"""``/session`` -- start, resume and end a foreground conversation.

Auto-discovered by ``repl_dispatch_registry`` (``dispatch_session_command``).
Synchronous by contract: this runs on the operator input queue's single
consumer, so it only changes session state; a turn runs on its own task in
:class:`foreground_session.SessionManager`.

Inside a session, plain text is a turn (``harness`` routes it through
:func:`route_plain_text`); outside one, plain text keeps its /chat routing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict

_HELP = (
    "[bold]/session[/bold] — talk to the organism; it reads, answers and proposes, "
    "and every change goes through the governed pipeline.\n"
    "[dim]  /session                start one (or return to yours)\n"
    "  /session list           every session this daemon holds\n"
    "  /session resume <id>    return to an earlier one\n"
    "  /session apply          send the last proposed change through the pipeline\n"
    "  /session cancel         stop the turn in flight\n"
    "  /session end            leave; plain text goes back to /chat[/dim]"
)


@dataclass(frozen=True)
class SessionReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "matched": self.matched}


_NO_MATCH = SessionReplDispatchResult(ok=False, text="", matched=False)


def _cockpit() -> str:
    """The typing cockpit -- read on the dispatch task, where the ContextVar
    is set (the turn task reads it from the session instead)."""
    try:
        from backend.core.ouroboros.battle_test.attach_session import current_session
        return str(current_session() or "")
    except Exception:  # noqa: BLE001
        return ""


def dispatch_session_command(line: str) -> SessionReplDispatchResult:
    """Route one ``/session`` line. NEVER raises."""
    s = (line or "").strip()
    if not (s == "/session" or s.startswith("/session ")):
        return _NO_MATCH
    parts = s.split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    try:
        from backend.core.ouroboros.governance.foreground_session import get_manager
        mgr = get_manager()
        cockpit = _cockpit()
        if sub in ("help", "?"):
            return SessionReplDispatchResult(True, _HELP)
        if sub == "":
            sess = mgr.start(cockpit)
            return SessionReplDispatchResult(
                True, f"session {sess.id} · type to talk · /session end to leave"
                      + (f" · {len(sess.turns)} earlier turn(s)" if sess.turns else ""))
        if sub == "end":
            sess = mgr.end(cockpit)
            return SessionReplDispatchResult(
                sess is not None, f"left session {sess.id}" if sess else "not in a session")
        if sub == "list":
            rows = [f"  {x.id}  {len(x.turns)} turn(s)  started "
                    f"{time.strftime('%H:%M', time.localtime(x.created))}"
                    f"{'  (ended)' if x.ended else ''}{'  ← you' if mgr.active(cockpit) is x else ''}"
                    for x in mgr.list()]
            return SessionReplDispatchResult(True, "\n".join(rows) or "no sessions")
        if sub == "resume" and len(parts) > 2:
            sess = mgr.resume(cockpit, parts[2])
            return SessionReplDispatchResult(
                sess is not None,
                f"back in session {sess.id} ({len(sess.turns)} turn(s))" if sess
                else f"no live session {parts[2]}")
        if sub == "cancel":
            return SessionReplDispatchResult(True, "turn cancelled" if mgr.cancel_turn(cockpit)
                                             else "no turn in flight")
        if sub == "apply":
            from backend.core.ouroboros.governance.foreground_session_ops import apply_last
            ok, text = apply_last(mgr, cockpit)
            return SessionReplDispatchResult(ok, text)
        return SessionReplDispatchResult(False, f"unknown: /session {sub}\n{_HELP}")
    except Exception as exc:  # noqa: BLE001
        return SessionReplDispatchResult(False, f"/session failed: {type(exc).__name__}: {exc}")


def route_plain_text(text: str) -> bool:
    """True when *text* was a turn in the typing cockpit's session. Called by
    the harness before the /chat bridge, so outside a session nothing that
    exists today changes behaviour. NEVER raises."""
    try:
        if not (text or "").strip() or text.lstrip().startswith("/"):
            return False
        from backend.core.ouroboros.governance.foreground_session import get_manager
        mgr = get_manager()
        cockpit = _cockpit()
        if mgr.active(cockpit) is None:
            return False
        mgr.submit(cockpit, text)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["SessionReplDispatchResult", "dispatch_session_command", "route_plain_text"]
