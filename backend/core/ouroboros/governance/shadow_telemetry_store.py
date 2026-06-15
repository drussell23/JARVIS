"""Async, bounded, fail-soft SQLite store for shadow-vs-legacy
comparison rows (Unit A).

Never blocks the event loop (sqlite calls run in ``asyncio.to_thread``)
and never raises into the caller (observer contract: shadow telemetry
must not break the FSM). Producers are fire-and-forget. A two-phase
upsert keyed by ``(op_id, agent)`` joins the shadow verdict (known
early) with the legacy outcome (known later); alignment is computed by
an injected evaluator once both halves are present. A rolling per-agent
FIFO cap keeps the footprint microscopic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sqlite3
from typing import Callable, Optional

logger = logging.getLogger("Ouroboros.ShadowTelemetryStore")

# evaluator signature: (agent, legacy_dict, shadow_dict) -> (aligned, reason)
EvaluatorFn = Callable[[str, dict, dict], "tuple[bool, str]"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_comparison (
    op_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    ts REAL NOT NULL,
    seq INTEGER NOT NULL,
    legacy_outcome TEXT,
    shadow_outcome TEXT,
    aligned INTEGER,
    divergence_reason TEXT,
    PRIMARY KEY (op_id, agent)
);
CREATE INDEX IF NOT EXISTS idx_agent_seq ON shadow_comparison(agent, seq);
CREATE TABLE IF NOT EXISTS agent_seq (agent TEXT PRIMARY KEY, next INTEGER);
"""


def store_enabled() -> bool:
    raw = os.environ.get("JARVIS_SHADOW_TELEMETRY_STORE_ENABLED")
    return raw is None or raw.strip().lower() in ("true", "1", "yes")


def _queue_max() -> int:
    try:
        return max(8, int(os.environ.get(
            "JARVIS_SHADOW_TELEMETRY_QUEUE_MAX", "256")))
    except (TypeError, ValueError):
        return 256


def _cap_per_agent() -> int:
    try:
        return max(50, int(os.environ.get(
            "JARVIS_SHADOW_TELEMETRY_MAX_ROWS_PER_AGENT", "1000")))
    except (TypeError, ValueError):
        return 1000


class ShadowTelemetryStore:
    def __init__(
        self,
        *,
        db_path: Optional[pathlib.Path] = None,
        evaluator: Optional[EvaluatorFn] = None,
        cap_per_agent: Optional[int] = None,
    ) -> None:
        self._db_path = pathlib.Path(
            db_path or pathlib.Path(".jarvis") / "shadow_telemetry.db"
        )
        self._evaluator = evaluator
        self._cap = cap_per_agent or _cap_per_agent()
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=_queue_max())
        self._task: Optional[asyncio.Task] = None
        self._dropped = 0

    # -- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._init_db)
        self._task = asyncio.ensure_future(self._writer_loop())

    async def aclose(self) -> None:
        if self._task is None:
            return
        await self._queue.put({"_stop": True})
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except Exception:  # noqa: BLE001
            self._task.cancel()
        self._task = None

    async def drain(self) -> None:
        """Test helper — block until the queue is fully processed."""
        await self._queue.join()

    # -- producers (fire-and-forget, never raise) ---------------------
    def record_shadow_nowait(
        self, *, op_id: str, agent: str, ts: float, shadow_outcome: dict,
    ) -> None:
        self._enqueue({
            "op_id": op_id, "agent": agent, "ts": ts,
            "shadow": shadow_outcome,
        })

    def record_legacy_nowait(
        self, *, op_id: str, agent: str, ts: float, legacy_outcome: dict,
    ) -> None:
        self._enqueue({
            "op_id": op_id, "agent": agent, "ts": ts,
            "legacy": legacy_outcome,
        })

    def _enqueue(self, item: dict) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # drop-oldest: discard one, retry once; bounded memory.
            try:
                _ = self._queue.get_nowait()
                self._queue.task_done()
                self._dropped += 1
                self._queue.put_nowait(item)
            except Exception:  # noqa: BLE001
                self._dropped += 1
        except Exception:  # noqa: BLE001
            self._dropped += 1

    # -- writer -------------------------------------------------------
    async def _writer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.get("_stop"):
                    self._queue.task_done()
                    return
                await asyncio.to_thread(self._apply, item)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[ShadowTelemetryStore] write failed (non-fatal)",
                    exc_info=True,
                )
            finally:
                if not item.get("_stop"):
                    self._queue.task_done()

    # -- blocking sqlite (runs in to_thread) --------------------------
    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _next_seq(self, con: sqlite3.Connection, agent: str) -> int:
        cur = con.execute(
            "SELECT next FROM agent_seq WHERE agent = ?", (agent,))
        row = cur.fetchone()
        nxt = (row[0] if row else 0) + 1
        con.execute(
            "INSERT INTO agent_seq(agent, next) VALUES(?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET next = excluded.next",
            (agent, nxt))
        return nxt

    def _apply(self, item: dict) -> None:
        agent = item["agent"]
        op_id = item["op_id"]
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT legacy_outcome, shadow_outcome, seq "
                "FROM shadow_comparison WHERE op_id = ? AND agent = ?",
                (op_id, agent))
            existing = cur.fetchone()
            if existing is None:
                seq = self._next_seq(con, agent)
                legacy = json.dumps(item["legacy"]) if "legacy" in item else None
                shadow = json.dumps(item["shadow"]) if "shadow" in item else None
                con.execute(
                    "INSERT INTO shadow_comparison"
                    "(op_id, agent, ts, seq, legacy_outcome, shadow_outcome,"
                    " aligned, divergence_reason) VALUES(?,?,?,?,?,?,?,?)",
                    (op_id, agent, item["ts"], seq, legacy, shadow, None, None))
            else:
                legacy = existing[0]
                shadow = existing[1]
                if "legacy" in item:
                    legacy = json.dumps(item["legacy"])
                if "shadow" in item:
                    shadow = json.dumps(item["shadow"])
                con.execute(
                    "UPDATE shadow_comparison SET legacy_outcome = ?, "
                    "shadow_outcome = ? WHERE op_id = ? AND agent = ?",
                    (legacy, shadow, op_id, agent))

            # Compute alignment once both halves present + evaluator wired.
            if legacy is not None and shadow is not None and self._evaluator:
                aligned, reason = self._evaluator(
                    agent, json.loads(legacy), json.loads(shadow))
                con.execute(
                    "UPDATE shadow_comparison SET aligned = ?, "
                    "divergence_reason = ? WHERE op_id = ? AND agent = ?",
                    (1 if aligned else 0, reason or None, op_id, agent))

            self._prune(con, agent)
            con.commit()
        finally:
            con.close()

    def _prune(self, con: sqlite3.Connection, agent: str) -> None:
        con.execute(
            "DELETE FROM shadow_comparison WHERE agent = ? AND seq <= "
            "((SELECT MAX(seq) FROM shadow_comparison WHERE agent = ?) - ?)",
            (agent, agent, self._cap))

    # -- read side ----------------------------------------------------
    async def last_n(self, agent: str, n: int) -> list:
        return await asyncio.to_thread(self._last_n, agent, n)

    def _last_n(self, agent: str, n: int) -> list:
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT op_id, seq, aligned, divergence_reason "
                "FROM shadow_comparison WHERE agent = ? "
                "ORDER BY seq DESC LIMIT ?", (agent, n))
            return [
                {"op_id": r[0], "seq": r[1], "aligned": r[2],
                 "divergence_reason": r[3]}
                for r in cur.fetchall()
            ]
        finally:
            con.close()

    async def recent_aligned_streak(self, agent: str) -> int:
        return await asyncio.to_thread(self._recent_aligned_streak, agent)

    def _recent_aligned_streak(self, agent: str) -> int:
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT aligned FROM shadow_comparison WHERE agent = ? "
                "ORDER BY seq DESC", (agent,))
            streak = 0
            for (aligned,) in cur.fetchall():
                if aligned is None:
                    continue  # incomplete row: skip, don't break
                if aligned == 1:
                    streak += 1
                else:
                    break  # a single divergence resets the streak
            return streak
        finally:
            con.close()
