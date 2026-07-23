"""Soak-Execution Lock — the atomic single-launch claim for the AWE Trigger.

When the DW Sentinel signals recovery (``provider_state`` → ``HEALTHY``), the
Autonomous Wake-and-Execute trigger must launch the definitive Agentic Swarm
soak EXACTLY ONCE — even if DoubleWord rapidly flaps HEALTHY↔DEGRADED↔HEALTHY
during the swarm's own startup window (the classic double-launch race).

Rather than invent a new locking scheme, this mirrors the ESTABLISHED
single-winner idiom already used in this same ``.jarvis/chunk_strategy.db``:
``dw_outage_forecaster.record_recovery`` claims an open outage row with a
predicate-guarded ``UPDATE ... WHERE <predicate>; if cur.rowcount == 0`` — the
row-count of a guarded UPDATE is SQLite's atomic compare-and-swap. Two callers
racing the same claim: only one UPDATE flips ``claimed 0→1`` and gets
``rowcount == 1``; the loser's WHERE no longer matches and gets ``rowcount == 0``.

The claim is **cooldown-aware**, and that single mechanism serves BOTH duties:

  * **Flap guard** — a second HEALTHY edge milliseconds after the first finds a
    fresh claim (``claimed_ts`` within ``cooldown_s``) → the WHERE excludes it →
    ``rowcount == 0`` → suppressed. No second parallel swarm.
  * **Legitimate re-arm** — a genuinely new outage→recovery cycle HOURS later
    (``claimed_ts`` older than ``cooldown_s``) → the WHERE matches again →
    ``rowcount == 1`` → the next recovery fires. The lock is one-shot *per
    outage cycle*, not one-shot forever.

Cross-process safety degrades fail-CLOSED: without a busy_timeout SQLite raises
``database is locked`` under contention, which is caught and reported as "did
not win the claim" — the correct default for a launch lock (never double-fire).
A small ``busy_timeout`` PRAGMA (local to this table's use, not a global policy
change) reduces spurious losses. Never raises.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("Ouroboros.SoakExecutionLock")

_LOCK_TABLE = "soak_execution_lock"

DEFAULT_LOCK_NAME = "agentic_swarm_soak"
_DEFAULT_COOLDOWN_S = 3600.0  # a rapid flap re-fires nothing for an hour


def default_relaunch_cooldown_s() -> float:
    """Env-tunable flap/re-arm window (seconds). Clamped to a sane floor so a
    misconfigured ``0`` can never disable the flap guard entirely."""
    try:
        v = float(os.environ.get("JARVIS_AWE_RELAUNCH_COOLDOWN_S", _DEFAULT_COOLDOWN_S))
    except (TypeError, ValueError):
        return _DEFAULT_COOLDOWN_S
    return max(1.0, v)


def ensure_soak_lock_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL. Composes the #70021 DB (same file, distinct table)."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_LOCK_TABLE} ("
        "lock_name TEXT PRIMARY KEY, "
        "claimed INTEGER NOT NULL DEFAULT 0, "
        "run_id TEXT, "
        "claimed_ts REAL, "
        "claimed_wall TEXT, "
        "released_ts REAL)"
    )
    # Local busy_timeout so a concurrent writer waits briefly rather than
    # instantly losing the claim — improves cross-process fairness without
    # imposing a global journal-mode/WAL policy on the shared DB.
    try:
        conn.execute("PRAGMA busy_timeout=2000")
    except sqlite3.Error:
        pass
    conn.commit()


def try_claim_soak_lock(
    conn: Optional[sqlite3.Connection],
    run_id: str,
    *,
    lock_name: str = DEFAULT_LOCK_NAME,
    cooldown_s: Optional[float] = None,
    now: Optional[float] = None,
) -> bool:
    """Atomically claim the soak lock. Returns ``True`` iff THIS caller won.

    The guarded UPDATE is the atomic compare-and-swap: the row is claimable iff
    it is unclaimed OR its last claim is older than ``cooldown_s``. Exactly one
    concurrent caller can flip it. Fail-closed (returns ``False``) on any error —
    a lock we cannot prove we hold is a lock we do not hold. Never raises."""
    if conn is None:
        return False
    cd = default_relaunch_cooldown_s() if cooldown_s is None else max(0.0, float(cooldown_s))
    when = time.time() if now is None else float(now)
    threshold = when - cd
    wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    try:
        ensure_soak_lock_table(conn)
        # Ensure the single baseline row exists (unclaimed) without disturbing an
        # existing claim — INSERT OR IGNORE is a no-op when the row is present.
        conn.execute(
            f"INSERT OR IGNORE INTO {_LOCK_TABLE} (lock_name, claimed) VALUES (?, 0)",
            (lock_name,),
        )
        cur = conn.execute(
            f"UPDATE {_LOCK_TABLE} "
            f"SET claimed=1, run_id=?, claimed_ts=?, claimed_wall=?, released_ts=NULL "
            f"WHERE lock_name=? "
            f"AND (claimed=0 OR claimed_ts IS NULL OR claimed_ts < ?)",
            (run_id, when, wall, lock_name, threshold),
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error:
        logger.debug("[SoakLock] claim failed (fail-closed)", exc_info=True)
        return False


def release_soak_lock(
    conn: Optional[sqlite3.Connection],
    *,
    lock_name: str = DEFAULT_LOCK_NAME,
    now: Optional[float] = None,
) -> None:
    """Release the lock so the next recovery may re-fire. Only used when a launch
    fails to even START (so a genuine failure does not wedge the trigger forever)
    — a successfully detached soak deliberately HOLDS the lock for the cooldown.
    Never raises."""
    if conn is None:
        return
    when = time.time() if now is None else float(now)
    try:
        ensure_soak_lock_table(conn)
        conn.execute(
            f"UPDATE {_LOCK_TABLE} SET claimed=0, released_ts=? WHERE lock_name=?",
            (when, lock_name),
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("[SoakLock] release failed", exc_info=True)


def read_soak_lock(
    conn: Optional[sqlite3.Connection], *, lock_name: str = DEFAULT_LOCK_NAME,
) -> Optional[dict]:
    """Read the lock row as a dict (or ``None``). Never raises."""
    if conn is None:
        return None
    try:
        ensure_soak_lock_table(conn)
        row = conn.execute(
            f"SELECT lock_name, claimed, run_id, claimed_ts, claimed_wall, released_ts "
            f"FROM {_LOCK_TABLE} WHERE lock_name=?",
            (lock_name,),
        ).fetchone()
        if not row:
            return None
        return {
            "lock_name": row[0], "claimed": int(row[1] or 0), "run_id": row[2],
            "claimed_ts": row[3], "claimed_wall": row[4], "released_ts": row[5],
        }
    except sqlite3.Error:
        return None


__all__ = [
    "DEFAULT_LOCK_NAME",
    "default_relaunch_cooldown_s",
    "ensure_soak_lock_table",
    "read_soak_lock",
    "release_soak_lock",
    "try_claim_soak_lock",
]
