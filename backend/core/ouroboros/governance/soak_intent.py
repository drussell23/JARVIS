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
from typing import NamedTuple, Optional

logger = logging.getLogger("Ouroboros.SoakIntent")

_INTENT_TABLE = "soak_intent_queue"

STATUS_PENDING = "pending"
STATUS_CLEARED = "cleared"


def ensure_intent_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL. Composes the #70021 DB (same file, distinct table).

    ``manifest_json`` holds the AST-aware checkpoint manifest (the deterministic
    per-chunk progress ledger — #70051). Added via additive migration so a DB
    created before the checkpoint engine upgrades in place, never re-created."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_INTENT_TABLE} ("
        "intent_id TEXT PRIMARY KEY, "
        "kind TEXT NOT NULL, "
        "target TEXT, "
        "priority INTEGER NOT NULL DEFAULT 5, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "enqueued_ts REAL, "
        "cleared_ts REAL, "
        "manifest_json TEXT)"
    )
    # Additive migration: a table created before manifest_json existed gets the
    # column now (PRAGMA-guarded so it runs at most once). Never raises.
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_INTENT_TABLE})").fetchall()}
        if "manifest_json" not in cols:
            conn.execute(f"ALTER TABLE {_INTENT_TABLE} ADD COLUMN manifest_json TEXT")
    except sqlite3.Error:
        logger.debug("[SoakIntent] manifest_json migration skipped", exc_info=True)
    conn.commit()


def enqueue_soak_intent(
    conn: Optional[sqlite3.Connection],
    *,
    kind: str = "agentic_swarm_soak",
    target: str = "",
    priority: int = 1,
    intent_id: Optional[str] = None,
    manifest_json: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """Record a pending high-priority soak workload. ``manifest_json`` carries the
    AST-aware checkpoint manifest (deterministic per-chunk progress). Returns the
    intent_id, or ``None`` on failure. Never raises."""
    if conn is None:
        return None
    iid = intent_id or uuid.uuid4().hex[:12]
    when = time.time() if now is None else float(now)
    try:
        ensure_intent_table(conn)
        conn.execute(
            f"INSERT OR IGNORE INTO {_INTENT_TABLE} "
            f"(intent_id, kind, target, priority, status, enqueued_ts, manifest_json) "
            f"VALUES (?, ?, ?, ?, '{STATUS_PENDING}', ?, ?)",
            (iid, kind, target, int(priority), when, manifest_json),
        )
        conn.commit()
        return iid
    except sqlite3.Error:
        logger.debug("[SoakIntent] enqueue failed", exc_info=True)
        return None


def get_manifest_json(
    conn: Optional[sqlite3.Connection], intent_id: str,
) -> Optional[str]:
    """Read the raw manifest JSON for an intent row (or ``None``). Never raises."""
    if conn is None:
        return None
    try:
        ensure_intent_table(conn)
        row = conn.execute(
            f"SELECT manifest_json FROM {_INTENT_TABLE} WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


class PendingIntent(NamedTuple):
    """The single highest-priority pending intent, as the AWE dispatcher needs it.

    ``target`` is load-bearing and easy to get wrong: a manifest's per-chunk
    ``file_path`` entries are stored RELATIVE to this target (the canary's chunks
    are ``mathy.py`` / ``strings_util.py`` / ``widgets.py``, not repo-relative
    paths). ``build_checkpointed_swarm_fn`` opens ``os.path.join(repo_root,
    file_path)``, so the dispatcher must pass ``target`` as ``repo_root`` — NOT
    the repository root, which would resolve every chunk to a nonexistent file.
    """

    intent_id: str
    kind: str
    target: str
    manifest_json: Optional[str]

    @property
    def has_manifest(self) -> bool:
        """True iff this intent carries a manifest worth resuming. An intent with
        no manifest is real work, but not *checkpointed* work — the dispatcher
        routes it to the generic soak rather than fabricating an empty run."""
        return bool(self.manifest_json)


def next_pending_intent(
    conn: Optional[sqlite3.Connection], *, max_priority: Optional[int] = None,
) -> Optional[PendingIntent]:
    """The highest-priority pending intent (ties broken oldest-first), or ``None``.

    Companion to :func:`pending_soak_count` — that answers "is there work?", this
    answers "which work, and where does it live?". Same ordering the supervisor's
    arm gate implies, so the intent the supervisor armed FOR is the intent the AWE
    dispatcher runs. Never raises."""
    if conn is None:
        return None
    try:
        ensure_intent_table(conn)
        sql = (
            f"SELECT intent_id, kind, COALESCE(target, ''), manifest_json "
            f"FROM {_INTENT_TABLE} WHERE status=?"
        )
        params: tuple = (STATUS_PENDING,)
        if max_priority is not None:
            sql += " AND priority<=?"
            params = (STATUS_PENDING, int(max_priority))
        sql += " ORDER BY priority ASC, enqueued_ts ASC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        return PendingIntent(
            intent_id=str(row[0]), kind=str(row[1]),
            target=str(row[2] or ""), manifest_json=row[3],
        )
    except sqlite3.Error:
        logger.debug("[SoakIntent] next_pending_intent failed", exc_info=True)
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
    "PendingIntent",
    "clear_soak_intent",
    "enqueue_soak_intent",
    "ensure_intent_table",
    "get_manifest_json",
    "next_pending_intent",
    "pending_soak_count",
]
