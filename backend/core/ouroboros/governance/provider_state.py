"""Provider-state SQLite IPC — the Single-Writer handoff channel.

The recovery Sentinel and the Ouroboros orchestrator are separate processes.
Letting the Sentinel touch git / the filesystem / launch subprocesses created a
concurrent-``.git/index`` mutation vector (the #70033 fixture leak). The fix is
a strict Single-Writer Principle mediated by ONE tiny SQLite table:

  * The **Sentinel** is headless + read-only except for a single row write: when
    its 2-stage payload verification passes it ``UPDATE``s ``provider_state`` to
    ``HEALTHY`` + a timestamp, then self-terminates. It owns NO git / fs / exec.
  * The **orchestrator** is the exclusive owner of the git index and
    ``SagaApplyStrategy``. It polls this table; on ``HEALTHY`` it does the
    fixture-seed + AIMD slow-start launch itself.

DRY: a lightweight table in the SAME ``.jarvis/chunk_strategy.db`` the
StrategyOutcomeLogger / DW-outage forecaster (#70021/#70030) use — no Redis, no
broker. Never raises.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("Ouroboros.ProviderState")

_STATE_TABLE = "provider_state"

STATE_HEALTHY = "HEALTHY"
STATE_DEGRADED = "DEGRADED"
STATE_UNKNOWN = "UNKNOWN"


def ensure_state_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL. Composes the #70021 DB (same file, distinct table)."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE} ("
        "provider TEXT PRIMARY KEY, "
        "state TEXT NOT NULL, "
        "reason TEXT, "
        "updated_ts REAL, "
        "updated_wall TEXT)"
    )
    conn.commit()


def set_provider_state(
    conn: Optional[sqlite3.Connection],
    provider: str,
    state: str,
    *,
    reason: str = "",
    ts: Optional[float] = None,
) -> None:
    """Upsert the provider's state + timestamp. The Sentinel's ONE mutation.
    Never raises."""
    if conn is None:
        return
    when = time.time() if ts is None else float(ts)
    wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    try:
        ensure_state_table(conn)
        conn.execute(
            f"INSERT INTO {_STATE_TABLE} "
            f"(provider, state, reason, updated_ts, updated_wall) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(provider) DO UPDATE SET "
            f"state=excluded.state, reason=excluded.reason, "
            f"updated_ts=excluded.updated_ts, updated_wall=excluded.updated_wall",
            (provider, state, reason, when, wall),
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("[ProviderState] set_provider_state failed", exc_info=True)


def get_provider_state(
    conn: Optional[sqlite3.Connection], provider: str,
) -> Optional[dict]:
    """Read the provider's state row as a dict, or None. Never raises."""
    if conn is None:
        return None
    try:
        ensure_state_table(conn)
        row = conn.execute(
            f"SELECT provider, state, reason, updated_ts, updated_wall "
            f"FROM {_STATE_TABLE} WHERE provider=?",
            (provider,),
        ).fetchone()
        if not row:
            return None
        return {
            "provider": row[0], "state": row[1], "reason": row[2],
            "updated_ts": row[3], "updated_wall": row[4],
        }
    except sqlite3.Error:
        return None


def mark_healthy(
    conn: Optional[sqlite3.Connection], provider: str = "doubleword",
    *, reason: str = "", ts: Optional[float] = None,
) -> None:
    set_provider_state(conn, provider, STATE_HEALTHY, reason=reason, ts=ts)


def mark_degraded(
    conn: Optional[sqlite3.Connection], provider: str = "doubleword",
    *, reason: str = "", ts: Optional[float] = None,
) -> None:
    set_provider_state(conn, provider, STATE_DEGRADED, reason=reason, ts=ts)


def is_healthy(conn: Optional[sqlite3.Connection], provider: str = "doubleword") -> bool:
    """The orchestrator's poll: has the Sentinel signaled HEALTHY?"""
    st = get_provider_state(conn, provider)
    return bool(st and st.get("state") == STATE_HEALTHY)


__all__ = [
    "STATE_DEGRADED",
    "STATE_HEALTHY",
    "STATE_UNKNOWN",
    "ensure_state_table",
    "get_provider_state",
    "is_healthy",
    "mark_degraded",
    "mark_healthy",
    "set_provider_state",
]
