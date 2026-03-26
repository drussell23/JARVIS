"""
Multi-Turn Session Manager
===========================

Tracks multi-turn sessions across pipeline runs so that the governance
pipeline can *resume* an incomplete goal rather than starting from scratch
every time.

Each session is a sequence of **turns** — one turn per pipeline operation.
Sessions are persisted as JSON files in a configurable directory (default
``~/.jarvis/ouroboros/sessions/``) so they survive process restarts.

The ``format_for_prompt`` method renders session history in a compact,
prompt-injectable format that gives the LLM full context of prior turns.

Environment variables
---------------------
``JARVIS_SESSION_DIR``
    Directory for session JSON files (default ``~/.jarvis/ouroboros/sessions/``).
``JARVIS_SESSION_MAX_AGE_DAYS``
    Default maximum age in days before ``prune()`` removes sessions (default ``30``).
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.SessionManager")

# ---------------------------------------------------------------------------
# Environment defaults
# ---------------------------------------------------------------------------

_DEFAULT_SESSION_DIR = Path(
    os.environ.get(
        "JARVIS_SESSION_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "sessions"),
    )
)
_DEFAULT_MAX_AGE_DAYS: int = int(os.environ.get("JARVIS_SESSION_MAX_AGE_DAYS", "30"))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionState(enum.Enum):
    """Lifecycle states for a session."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SessionTurn:
    """One pipeline operation within a session."""

    turn_id: int
    op_id: str
    phase_reached: str
    success: bool
    summary: str
    files_modified: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class Session:
    """A multi-turn session tracking progress toward a goal."""

    session_id: str
    goal: str
    state: SessionState
    created_at: float
    updated_at: float
    turns: List[SessionTurn] = field(default_factory=list)
    context_snapshot: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _session_to_dict(session: Session) -> Dict[str, Any]:
    """Serialise a Session to a plain dict suitable for JSON."""
    d = asdict(session)
    d["state"] = session.state.value
    return d


def _session_from_dict(d: Dict[str, Any]) -> Session:
    """Deserialise a Session from a plain dict."""
    turns = [SessionTurn(**t) for t in d.get("turns", [])]
    return Session(
        session_id=d["session_id"],
        goal=d["goal"],
        state=SessionState(d["state"]),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        turns=turns,
        context_snapshot=d.get("context_snapshot", {}),
        metadata=d.get("metadata", {}),
    )


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """File-backed multi-turn session persistence.

    Each session is stored as ``{storage_dir}/{session_id}.json``.
    Thread-safe via a reentrant lock.

    Usage::

        mgr = get_session_manager()
        session = mgr.create("Fix auth bug in login.py")
        mgr.add_turn(session.session_id, op_id="op-123",
                      phase_reached="APPLY", success=True,
                      summary="Applied patch to login.py",
                      files=["backend/auth/login.py"])
        prompt_ctx = mgr.format_for_prompt(session)
    """

    def __init__(self, storage_dir: Path = _DEFAULT_SESSION_DIR) -> None:
        self._dir = storage_dir
        self._lock = threading.RLock()
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.info("SessionManager initialised (dir=%s)", self._dir)

    # -- private helpers ----------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        # Sanitise to prevent path traversal.
        safe_id = session_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe_id}.json"

    def _load(self, session_id: str) -> Optional[Session]:
        p = self._path_for(session_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return _session_from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("corrupt session file %s: %s", p, exc)
            return None

    def _save(self, session: Session) -> None:
        p = self._path_for(session.session_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_session_to_dict(session), indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(p)

    # -- public API ---------------------------------------------------------

    def create(
        self,
        goal: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """Create a new active session."""
        with self._lock:
            now = time.time()
            session = Session(
                session_id=str(uuid.uuid4()),
                goal=goal,
                state=SessionState.ACTIVE,
                created_at=now,
                updated_at=now,
                metadata=metadata or {},
            )
            self._save(session)
            logger.info("session CREATED id=%s goal=%r", session.session_id, goal)
            return session

    def resume(self, session_id: str) -> Optional[Session]:
        """Load a session from disk and set state to ACTIVE.

        Returns ``None`` if the session does not exist.
        """
        with self._lock:
            session = self._load(session_id)
            if session is None:
                logger.warning("session RESUME failed — not found: %s", session_id)
                return None
            if session.state in (SessionState.COMPLETED, SessionState.FAILED):
                logger.warning(
                    "session RESUME on terminal session %s (state=%s) — reopening",
                    session_id,
                    session.state.value,
                )
            session.state = SessionState.ACTIVE
            session.updated_at = time.time()
            self._save(session)
            logger.info("session RESUMED id=%s (turns=%d)", session_id, len(session.turns))
            return session

    def pause(self, session_id: str) -> bool:
        """Pause a session.  Returns ``True`` on success."""
        with self._lock:
            session = self._load(session_id)
            if session is None:
                return False
            session.state = SessionState.PAUSED
            session.updated_at = time.time()
            self._save(session)
            logger.info("session PAUSED id=%s", session_id)
            return True

    def complete(self, session_id: str) -> bool:
        """Mark a session as completed.  Returns ``True`` on success."""
        with self._lock:
            session = self._load(session_id)
            if session is None:
                return False
            session.state = SessionState.COMPLETED
            session.updated_at = time.time()
            self._save(session)
            logger.info("session COMPLETED id=%s (turns=%d)", session_id, len(session.turns))
            return True

    def fail(self, session_id: str, reason: str) -> bool:
        """Mark a session as failed.  Returns ``True`` on success."""
        with self._lock:
            session = self._load(session_id)
            if session is None:
                return False
            session.state = SessionState.FAILED
            session.metadata["failure_reason"] = reason
            session.updated_at = time.time()
            self._save(session)
            logger.info("session FAILED id=%s reason=%r", session_id, reason)
            return True

    def add_turn(
        self,
        session_id: str,
        op_id: str,
        phase_reached: str,
        success: bool,
        summary: str,
        files: Optional[List[str]] = None,
    ) -> Optional[SessionTurn]:
        """Append a turn to the session.  Returns the turn or ``None`` if session not found."""
        with self._lock:
            session = self._load(session_id)
            if session is None:
                logger.warning("add_turn: session not found: %s", session_id)
                return None
            turn = SessionTurn(
                turn_id=len(session.turns) + 1,
                op_id=op_id,
                phase_reached=phase_reached,
                success=success,
                summary=summary,
                files_modified=files or [],
            )
            session.turns.append(turn)
            session.updated_at = time.time()
            self._save(session)
            logger.debug(
                "session %s turn #%d added (phase=%s success=%s)",
                session_id,
                turn.turn_id,
                phase_reached,
                success,
            )
            return turn

    def get(self, session_id: str) -> Optional[Session]:
        """Load a session without changing its state."""
        with self._lock:
            return self._load(session_id)

    def list_active(self) -> List[Session]:
        """Return all ACTIVE or PAUSED sessions, sorted by updated_at desc."""
        with self._lock:
            results: List[Session] = []
            for p in self._dir.glob("*.json"):
                session = self._load(p.stem)
                if session and session.state in (SessionState.ACTIVE, SessionState.PAUSED):
                    results.append(session)
            results.sort(key=lambda s: s.updated_at, reverse=True)
            return results

    def list_recent(self, limit: int = 10) -> List[Session]:
        """Return the most recent sessions regardless of state."""
        with self._lock:
            results: List[Session] = []
            for p in self._dir.glob("*.json"):
                session = self._load(p.stem)
                if session:
                    results.append(session)
            results.sort(key=lambda s: s.updated_at, reverse=True)
            return results[:limit]

    def fork(self, session_id: str) -> Optional[Session]:
        """Create a new session with the same context_snapshot as starting point.

        Returns ``None`` if the source session does not exist.
        """
        with self._lock:
            source = self._load(session_id)
            if source is None:
                logger.warning("fork: source session not found: %s", session_id)
                return None
            now = time.time()
            forked = Session(
                session_id=str(uuid.uuid4()),
                goal=source.goal,
                state=SessionState.ACTIVE,
                created_at=now,
                updated_at=now,
                context_snapshot=dict(source.context_snapshot),
                metadata={
                    "forked_from": source.session_id,
                    "forked_at_turn": len(source.turns),
                },
            )
            self._save(forked)
            logger.info(
                "session FORKED %s -> %s (snapshot from turn %d)",
                session_id,
                forked.session_id,
                len(source.turns),
            )
            return forked

    def prune(self, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
        """Remove sessions older than *max_age_days*.  Returns count removed."""
        with self._lock:
            cutoff = time.time() - (max_age_days * 86400)
            removed = 0
            for p in list(self._dir.glob("*.json")):
                session = self._load(p.stem)
                if session is None:
                    # Corrupt file — remove.
                    p.unlink(missing_ok=True)
                    removed += 1
                    continue
                if session.updated_at < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            if removed:
                logger.info("session PRUNE removed %d sessions (max_age=%dd)", removed, max_age_days)
            return removed

    @staticmethod
    def format_for_prompt(session: Session) -> str:
        """Render session history for prompt injection.

        Example output::

            ## Session History (3 turns)
            Goal: Fix auth bug in login.py
            Turn 1: APPLY — Applied patch to login.py [SUCCESS]
            Turn 2: VALIDATE — Tests passed (3/3) [SUCCESS]
            Turn 3: APPLY — Applied final changes [SUCCESS]
        """
        if not session.turns:
            return f"## Session History (0 turns)\nGoal: {session.goal}\nNo turns recorded yet."

        lines = [
            f"## Session History ({len(session.turns)} turn{'s' if len(session.turns) != 1 else ''})",
            f"Goal: {session.goal}",
        ]
        for turn in session.turns:
            status = "SUCCESS" if turn.success else "FAILED"
            file_note = ""
            if turn.files_modified:
                file_note = f" [{', '.join(turn.files_modified)}]"
            lines.append(
                f"Turn {turn.turn_id}: {turn.phase_reached} — {turn.summary} [{status}]{file_note}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[SessionManager] = None
_singleton_lock = threading.Lock()


def get_session_manager() -> SessionManager:
    """Return the process-wide ``SessionManager`` singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = SessionManager(storage_dir=_DEFAULT_SESSION_DIR)
        return _singleton
