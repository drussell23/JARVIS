"""Provider Jitter Index + Adaptive Hysteresis — the anti-Flapping-Trap engine.

Right after an outage a GPU cluster WARMS UP: it briefly answers, then drops the
stream (``ClientPayloadError`` / ``StreamRuptureError`` mid-generation) — the
08:39 flap. A fixed "2 consecutive passes → HEALTHY" rule can trip on a lucky
pair inside that jitter window and hand the swarm onto a lane that dies under
load, exhausting the failover cascade (the Flapping Trap).

The fix measures real-time instability instead of guessing with static timers:

  * **Provider Jitter Index (SQLite):** every transient stream error is recorded
    into a ``provider_jitter_events`` table in the SAME ``.jarvis/chunk_strategy.db``
    substrate (#70021/#70030/#70034) — no new DB file, no dependency. The
    ``jitter_index`` is the count of transient errors in the trailing window
    (default 30 min).
  * **Adaptive Hysteresis:** the number of consecutive 2-stage passes the Sentinel
    requires before writing ``HEALTHY`` is ``base + jitter_index``, clamped to a
    ceiling (default 5). Flapping → demand more proof; the rolling window means
    that as stream stability holds and old errors age out, the requirement DECAYS
    back to baseline on its own — no explicit decay timer.
  * **Zero Cost Idle:** purely in-memory arithmetic over stored timestamps — zero
    extra API calls, zero token overhead.

Never raises.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("Ouroboros.ProviderJitter")

_JITTER_TABLE = "provider_jitter_events"
_DEFAULT_WINDOW_S = 1800.0        # 30 minutes
_DEFAULT_BASE = 2
_DEFAULT_CAP = 5

# Transport-flap error classes worth counting toward instability.
_TRANSIENT_MARKERS = (
    "clientpayloaderror", "streamrupture", "no_tokens", "upstream_error",
    "clientconnector", "serverdisconnected", "timeouterror", "payload",
    "connection reset", "connection aborted", "incompleteread",
)


def is_transient_class(error_class: Optional[str]) -> bool:
    """Does *error_class* name a transport-flap class (counts toward jitter)?"""
    ec = (error_class or "").lower()
    return any(m in ec for m in _TRANSIENT_MARKERS)


def _ensure_jitter_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_JITTER_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "provider TEXT NOT NULL, error_class TEXT, ts REAL NOT NULL)"
    )
    conn.commit()


def record_jitter_event(
    conn: Optional[sqlite3.Connection], provider: str, error_class: str,
    *, ts: Optional[float] = None, window_s: float = _DEFAULT_WINDOW_S,
) -> None:
    """Record ONE transient stream error. Cheaply prunes rows older than 2x the
    window so the table stays bounded (zero network). Never raises."""
    if conn is None:
        return
    when = time.time() if ts is None else float(ts)
    try:
        _ensure_jitter_table(conn)
        conn.execute(
            f"INSERT INTO {_JITTER_TABLE} (provider, error_class, ts) VALUES (?, ?, ?)",
            (provider, error_class, when),
        )
        conn.execute(
            f"DELETE FROM {_JITTER_TABLE} WHERE ts < ?", (when - 2.0 * window_s,),
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("[ProviderJitter] record failed", exc_info=True)


def jitter_index(
    conn: Optional[sqlite3.Connection], provider: str = "doubleword",
    *, window_s: float = _DEFAULT_WINDOW_S, now: Optional[float] = None,
) -> int:
    """Count of transient errors in the trailing ``window_s`` (default 30 min).
    Zero-cost: pure SQLite read over stored timestamps. Never raises."""
    if conn is None:
        return 0
    ref = time.time() if now is None else float(now)
    cutoff = ref - window_s
    try:
        _ensure_jitter_table(conn)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_JITTER_TABLE} WHERE provider=? AND ts>=?",
            (provider, cutoff),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def required_consecutive_passes(
    conn: Optional[sqlite3.Connection], provider: str = "doubleword",
    *, base: int = _DEFAULT_BASE, cap: int = _DEFAULT_CAP,
    window_s: float = _DEFAULT_WINDOW_S, now: Optional[float] = None,
) -> int:
    """The Adaptive Hysteresis stability window: ``base + jitter_index``, clamped
    to ``[1, cap]``. jitter 0 → base; each recent flap raises the bar; the rolling
    window decays it back to base as stability holds. Never raises."""
    j = jitter_index(conn, provider, window_s=window_s, now=now)
    return max(1, min(int(cap), int(base) + j))


__all__ = [
    "is_transient_class",
    "jitter_index",
    "record_jitter_event",
    "required_consecutive_passes",
]
