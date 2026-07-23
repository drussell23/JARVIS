"""Soak-Intent Queue — the declarative workload intent the Supervisor reacts to.

The Autonomous Supervisor is intent-driven, not flag-driven: it arms itself when
there is *work waiting* and DoubleWord is down, and disarms when the work clears.
"Work waiting" needs a durable, cross-process record — the in-memory
``record_pending_strategy`` dict (chunked_generation_bridge) is process-local and
cannot be seen by a supervisor querying the substrate at boot.

So this is the declarative state: a tiny ``soak_intent_queue`` table in the SAME
``.jarvis/chunk_strategy.db`` every other resilience subsystem composes
(provider_state / jitter / ttft / soak_execution_lock). One row per pending
high-priority soak workload (e.g. a queued ``SagaApplyStrategy`` / big-file swarm
run). ``status`` moves ``pending`` → ``cleared``; the supervisor's arm gate is
simply "does ``pending_soak_count() > 0``". Enqueue expresses intent (staging a
soak and walking away); the engine does the rest. Never raises.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Optional

logger = logging.getLogger("Ouroboros.SoakIntent")

_INTENT_TABLE = "soak_intent_queue"

STATUS_PENDING = "pending"
STATUS_CLEARED = "cleared"


def ensure_intent_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL. Composes the #70021 DB (same file, distinct table)."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_INTENT_TABLE} ("
        "intent_id TEXT PRIMARY KEY, "
        "kind TEXT NOT NULL, "
        "target TEXT, "
        "priority INTEGER NOT NULL DEFAULT 5, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "enqueued_ts REAL, "
        "cleared_ts REAL)"
    )
    conn.commit()


def enqueue_soak_intent(
    conn: Optional[sqlite3.Connection],
    *,
    kind: str = "agentic_swarm_soak",
    target: str = "",
    priority: int = 1,
    intent_id: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """Record a pending high-priority soak workload. Returns the intent_id, or
    ``None`` on failure. Never raises."""
    if conn is None:
        return None
    iid = intent_id or uuid.uuid4().hex[:12]
    when = time.time() if now is None else float(now)
    try:
        ensure_intent_table(conn)
        conn.execute(
            f"INSERT OR IGNORE INTO {_INTENT_TABLE} "
            f"(intent_id, kind, target, priority, status, enqueued_ts) "
            f"VALUES (?, ?, ?, ?, '{STATUS_PENDING}', ?)",
            (iid, kind, target, int(priority), when),
        )
        conn.commit()
        return iid
    except sqlite3.Error:
        logger.debug("[SoakIntent] enqueue failed", exc_info=True)
        return None


def pending_soak_count(
    conn: Optional[sqlite3.Connection], *, max_priority: Optional[int] = None,
) -> int:
    """How many pending soak workloads are queued. ``max_priority`` (lower ==
    higher priority) filters to at-least-this-urgent intents. Never raises."""
    if conn is None:
        return 0
    try:
        ensure_intent_table(conn)
        if max_priority is None:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {_INTENT_TABLE} WHERE status=?",
                (STATUS_PENDING,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {_INTENT_TABLE} "
                f"WHERE status=? AND priority<=?",
                (STATUS_PENDING, int(max_priority)),
            ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def clear_soak_intent(
    conn: Optional[sqlite3.Connection],
    *,
    intent_id: Optional[str] = None,
    kind: Optional[str] = None,
    now: Optional[float] = None,
) -> int:
    """Mark matching pending intents ``cleared`` (the soak reached terminal).
    With no filter, clears ALL pending. Returns the count cleared. Never raises."""
    if conn is None:
        return 0
    when = time.time() if now is None else float(now)
    try:
        ensure_intent_table(conn)
        if intent_id is not None:
            cur = conn.execute(
                f"UPDATE {_INTENT_TABLE} SET status=?, cleared_ts=? "
                f"WHERE intent_id=? AND status=?",
                (STATUS_CLEARED, when, intent_id, STATUS_PENDING),
            )
        elif kind is not None:
            cur = conn.execute(
                f"UPDATE {_INTENT_TABLE} SET status=?, cleared_ts=? "
                f"WHERE kind=? AND status=?",
                (STATUS_CLEARED, when, kind, STATUS_PENDING),
            )
        else:
            cur = conn.execute(
                f"UPDATE {_INTENT_TABLE} SET status=?, cleared_ts=? WHERE status=?",
                (STATUS_CLEARED, when, STATUS_PENDING),
            )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error:
        logger.debug("[SoakIntent] clear failed", exc_info=True)
        return 0


__all__ = [
    "STATUS_CLEARED",
    "STATUS_PENDING",
    "clear_soak_intent",
    "enqueue_soak_intent",
    "ensure_intent_table",
    "pending_soak_count",
]
