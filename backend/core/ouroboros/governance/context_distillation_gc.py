"""Asynchronous Context Distillation GC — the Endurance Trap fix (2026-07-22).

A long-horizon autonomous agent accumulates state monotonically: every op
in flight carries HEAVY payloads (AST trees, raw stack traces, diff
objects) in the active context that feeds the LLM, and every telemetry
row lands in the SQLite store forever. Left unbounded, the active context
blows the token budget and the DB starves I/O — the process OOMs during
infinite uptime. Deleting the DB on a cron is a workaround, not a fix.

This is the structural countermeasure — distill, don't delete:

1. **Context distillation on terminal.** When an op reaches a terminal
   state (``PROMOTED`` / ``TOMBSTONED`` / ``conflict_aborted`` / any
   terminal), the GC (subscribed to the ``TrinityEventBus``) prunes its
   heavy payloads and replaces them with a COMPRESSED SEMANTIC POINTER
   (op_id, outcome, sha8, file-count, a one-line summary reusing the
   existing ``ContextCompactor._build_summary``). The knowledge survives;
   the megabytes don't. A hard ``max_active_tokens`` bound is enforced —
   over budget, the oldest terminal-eligible ops distill first — so the
   active LLM context is STRICTLY bounded regardless of horizon.

2. **Rolling telemetry compaction.** Telemetry rows older than
   ``window_hours`` (default 72) are aggregated into lightweight per-day
   metric rows (count + numeric sums) and the raw rows deleted, in one
   transaction — freeing I/O without losing the trend signal.

Both run on the EXISTING ``advisor-blast`` ThreadPoolExecutor (zero
event-loop starvation, DRY) and emit via the EXISTING ``HiveEmitter``.
Fail-soft throughout; every knob env-tunable; no hardcoding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_ENABLED_ENV = "JARVIS_CONTEXT_DISTILLATION_ENABLED"
_MAX_ACTIVE_TOKENS_ENV = "JARVIS_DISTILL_MAX_ACTIVE_TOKENS"
_DISTILLED_RING_ENV = "JARVIS_DISTILL_POINTER_RING_SIZE"
_TELEMETRY_WINDOW_ENV = "JARVIS_TELEMETRY_COMPACT_WINDOW_HOURS"
_SUMMARY_MAX_ENV = "JARVIS_DISTILL_SUMMARY_MAX_CHARS"

#: Closed terminal-state vocabulary (op outcomes that free their heavy
#: payload). Env-extendable, never hardcoded to a single value.
_DEFAULT_TERMINAL_STATES = (
    "promoted", "tombstoned", "conflict_aborted", "landed_clean",
    "landed_resolved", "target_dirty", "failed", "blocked", "completed",
    "no_op_cosmetic",
)


def distillation_enabled() -> bool:
    """Master flag — default TRUE."""
    raw = os.environ.get(_ENABLED_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _max_active_tokens() -> int:
    """``JARVIS_DISTILL_MAX_ACTIVE_TOKENS`` — default 120_000. The hard
    ceiling on the active (heavy) context tier."""
    try:
        return max(1000, int(
            os.environ.get(_MAX_ACTIVE_TOKENS_ENV, "120000").strip()
        ))
    except (TypeError, ValueError):
        return 120_000


def _distilled_ring_size() -> int:
    try:
        return max(16, int(
            os.environ.get(_DISTILLED_RING_ENV, "512").strip()
        ))
    except (TypeError, ValueError):
        return 512


def _telemetry_window_hours() -> float:
    try:
        return max(1.0, float(
            os.environ.get(_TELEMETRY_WINDOW_ENV, "72").strip()
        ))
    except (TypeError, ValueError):
        return 72.0


def _summary_max_chars() -> int:
    try:
        return max(40, int(
            os.environ.get(_SUMMARY_MAX_ENV, "240").strip()
        ))
    except (TypeError, ValueError):
        return 240


def _terminal_states() -> Tuple[str, ...]:
    raw = os.environ.get("JARVIS_DISTILL_TERMINAL_STATES", "").strip()
    if raw:
        extra = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
        return tuple(dict.fromkeys(_DEFAULT_TERMINAL_STATES + extra))
    return _DEFAULT_TERMINAL_STATES


def estimate_tokens(obj: Any) -> int:
    """Cheap deterministic token estimate — ~4 chars/token over the
    JSON-serialized payload (the standard heuristic). NEVER raises."""
    try:
        if isinstance(obj, str):
            n = len(obj)
        else:
            n = len(json.dumps(obj, default=str, ensure_ascii=False))
        return (n // 4) + 1
    except Exception:  # noqa: BLE001
        try:
            return (len(str(obj)) // 4) + 1
        except Exception:  # noqa: BLE001
            return 1


@dataclass(frozen=True)
class DistilledPointer:
    """The compressed semantic residue of a distilled op — kilobytes of
    AST/stack/diff collapsed to a pointer the LLM context can afford."""

    op_id: str
    outcome: str
    sha8: str
    file_count: int
    summary: str
    original_tokens: int
    distilled_at_monotonic: float


class ContextDistillationGC:
    """Bounded active-context registry + terminal-driven distillation +
    rolling telemetry compaction. Thread-safe; every public method is
    fail-soft (NEVER raises)."""

    def __init__(
        self,
        *,
        max_active_tokens: Optional[int] = None,
        terminal_states: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._lock = threading.Lock()
        # op_id -> heavy payload entries (AST/stack/diff dicts).
        self._active: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        # op_id -> compressed pointer (bounded ring).
        self._distilled: "OrderedDict[str, DistilledPointer]" = OrderedDict()
        self._max_active_tokens = (
            max_active_tokens if max_active_tokens is not None
            else _max_active_tokens()
        )
        self._terminal = (
            tuple(s.lower() for s in terminal_states)
            if terminal_states is not None else _terminal_states()
        )
        self.stats = {
            "registered": 0, "distilled": 0, "bound_evictions": 0,
            "telemetry_rows_compacted": 0,
        }

    # -- active-context registry ------------------------------------------

    def register_active(
        self, op_id: str, entries: "List[Dict[str, Any]]",
    ) -> None:
        """Record an in-flight op's heavy payloads. NEVER raises."""
        if not op_id:
            return
        try:
            with self._lock:
                self._active[op_id] = list(entries or ())
                self._active.move_to_end(op_id)
                self.stats["registered"] += 1
        except Exception:  # noqa: BLE001
            pass

    def _active_tokens_locked(self) -> int:
        """Sum active-tier tokens. Caller MUST hold ``self._lock``."""
        return sum(
            estimate_tokens(e)
            for entries in self._active.values()
            for e in entries
        )

    def active_token_estimate(self) -> int:
        """Current token weight of the active (heavy) tier. NEVER raises."""
        try:
            with self._lock:
                return self._active_tokens_locked()
        except Exception:  # noqa: BLE001
            return 0

    # -- distillation core -------------------------------------------------

    def _build_pointer(
        self, op_id: str, entries: "List[Dict[str, Any]]", outcome: str,
    ) -> DistilledPointer:
        original_tokens = sum(estimate_tokens(e) for e in entries)
        # Reuse the EXISTING ContextCompactor summariser (DRY) — fall back
        # to a deterministic one-liner if it's unavailable.
        summary = ""
        try:
            from backend.core.ouroboros.governance.context_compaction import (
                ContextCompactor,
            )
            summary = ContextCompactor()._build_summary(entries)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — deterministic fallback
            summary = (
                f"{len(entries)} heavy entr(y/ies) "
                f"[{','.join(sorted({str(e.get('kind', 'payload')) for e in entries}))[:80]}]"
            )
        summary = (summary or "")[: _summary_max_chars()]
        # Extract a stable sha8 + file-count from the payloads if present.
        sha8 = ""
        files: set = set()
        for e in entries:
            sha8 = sha8 or str(e.get("sha8", "") or e.get("hash", ""))[:8]
            fp = e.get("file_path") or e.get("file")
            if fp:
                files.add(str(fp))
        return DistilledPointer(
            op_id=op_id, outcome=outcome, sha8=sha8,
            file_count=len(files), summary=summary,
            original_tokens=original_tokens,
            distilled_at_monotonic=time.monotonic(),
        )

    def _distill_locked(self, op_id: str, outcome: str) -> Optional[DistilledPointer]:
        """Pop the heavy payload and store a compressed pointer. Caller
        holds ``self._lock``."""
        entries = self._active.pop(op_id, None)
        if entries is None:
            return None
        ptr = self._build_pointer(op_id, entries, outcome)
        self._distilled[op_id] = ptr
        self._distilled.move_to_end(op_id)
        # Bound the pointer ring (FIFO).
        ring = _distilled_ring_size()
        while len(self._distilled) > ring:
            self._distilled.popitem(last=False)
        self.stats["distilled"] += 1
        return ptr

    def handle_terminal(
        self, op_id: str, outcome: str = "",
    ) -> Optional[DistilledPointer]:
        """Distill ``op_id`` when its outcome is terminal (the bus
        handler's core). Unknown/non-terminal outcomes are left active.
        NEVER raises."""
        if not distillation_enabled() or not op_id:
            return None
        try:
            _oc = (outcome or "").strip().lower()
            with self._lock:
                if op_id not in self._active:
                    return None
                if _oc and _oc not in self._terminal:
                    return None  # still in flight — keep heavy
                return self._distill_locked(op_id, _oc or "terminal")
        except Exception:  # noqa: BLE001
            return None

    def enforce_token_bound(self) -> int:
        """Distill oldest active ops until the active tier is under the
        token ceiling. Returns the number evicted. NEVER raises. This is
        the STRICT bound guarantee — even a burst of never-terminal
        stragglers cannot blow the budget."""
        evicted = 0
        try:
            with self._lock:
                while (
                    self._active_tokens_locked() > self._max_active_tokens
                    and self._active
                ):
                    oldest = next(iter(self._active))
                    self._distill_locked(oldest, "distilled_for_bound")
                    evicted += 1
                    self.stats["bound_evictions"] += 1
        except Exception:  # noqa: BLE001
            pass
        return evicted

    def snapshot(self) -> Dict[str, Any]:
        try:
            with self._lock:
                return {
                    "active_ops": len(self._active),
                    "active_tokens": self._active_tokens_locked(),
                    "max_active_tokens": self._max_active_tokens,
                    "distilled_pointers": len(self._distilled),
                    **dict(self.stats),
                }
        except Exception:  # noqa: BLE001
            return {}

    # -- rolling telemetry compaction (SQLite) ----------------------------

    def compact_telemetry(
        self,
        conn: Any,
        table: str,
        ts_column: str,
        *,
        window_hours: Optional[float] = None,
        now_epoch: Optional[float] = None,
        numeric_columns: Optional[Tuple[str, ...]] = None,
    ) -> Tuple[int, int]:
        """Aggregate rows older than the window into per-day metric rows,
        delete the raw rows. Returns ``(raw_removed, aggregate_rows)``.

        ``ts_column`` is an epoch-seconds column. Aggregates land in
        ``<table>_agg`` (day_bucket, row_count, + SUM of each numeric
        column). One transaction; rolls back on any error. NEVER raises.

        Threading contract: the sweep dispatches this to the advisor-blast
        pool, so ``conn`` MUST be thread-safe (opened with
        ``check_same_thread=False``, the standard for a cross-thread
        connection-pool handle). A thread-affinity error is caught and
        rolled back like any other fault (fail-soft → returns (0, 0)).
        """
        raw_removed = 0
        agg_rows = 0
        try:
            win = window_hours if window_hours is not None else _telemetry_window_hours()
            now = now_epoch if now_epoch is not None else time.time()
            cutoff = now - win * 3600.0
            cur = conn.cursor()
            # Discover numeric columns if not supplied.
            cols = numeric_columns
            if cols is None:
                cur.execute(f"PRAGMA table_info({table})")
                cols = tuple(
                    r[1] for r in cur.fetchall()
                    if str(r[2]).upper() in ("INTEGER", "REAL", "NUMERIC")
                    and r[1] != ts_column
                )
            agg_table = f"{table}_agg"
            _sum_cols = "".join(f", SUM({c}) AS sum_{c}" for c in cols)
            _sum_defs = "".join(f", sum_{c} REAL" for c in cols)
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {agg_table} "
                f"(day_bucket INTEGER PRIMARY KEY, row_count INTEGER{_sum_defs})"
            )
            # Bucket old rows by UTC day (epoch // 86400).
            cur.execute(
                f"SELECT CAST({ts_column} / 86400 AS INTEGER) AS day_bucket, "
                f"COUNT(*) AS row_count{_sum_cols} "
                f"FROM {table} WHERE {ts_column} < ? GROUP BY day_bucket",
                (cutoff,),
            )
            buckets = cur.fetchall()
            for row in buckets:
                day_bucket = row[0]
                row_count = row[1]
                sums = row[2:]
                _cols_ins = ", ".join(f"sum_{c}" for c in cols)
                _ph = ", ".join("?" for _ in cols)
                _upd = ", ".join(
                    f"sum_{c} = sum_{c} + excluded.sum_{c}" for c in cols
                )
                cur.execute(
                    f"INSERT INTO {agg_table} (day_bucket, row_count"
                    + (", " + _cols_ins if cols else "")
                    + f") VALUES (?, ?" + ((", " + _ph) if cols else "") + ") "
                    f"ON CONFLICT(day_bucket) DO UPDATE SET "
                    f"row_count = row_count + excluded.row_count"
                    + (", " + _upd if cols else ""),
                    (day_bucket, row_count, *sums),
                )
                agg_rows += 1
            cur.execute(
                f"DELETE FROM {table} WHERE {ts_column} < ?", (cutoff,),
            )
            raw_removed = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
            with self._lock:
                self.stats["telemetry_rows_compacted"] += raw_removed
        except Exception:  # noqa: BLE001 — fail-soft: roll back, never crash
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.debug(
                "[ContextDistillGC] telemetry compaction failed", exc_info=True,
            )
        return raw_removed, agg_rows

    # -- the offloaded sweep ----------------------------------------------

    async def sweep(
        self,
        *,
        telemetry_conn: Any = None,
        telemetry_table: str = "",
        telemetry_ts_column: str = "",
    ) -> Dict[str, Any]:
        """Run bound-enforcement + telemetry compaction on the dedicated
        ``advisor-blast`` executor (zero event-loop starvation, DRY).
        NEVER raises; emits a HiveEmitter summary."""
        result: Dict[str, Any] = {
            "evicted": 0, "telemetry_removed": 0, "telemetry_aggregates": 0,
        }
        if not distillation_enabled():
            return result
        try:
            loop = asyncio.get_running_loop()
            from backend.core.ouroboros.governance.operation_advisor import (
                _get_advisor_blast_executor,
            )
            pool = _get_advisor_blast_executor()

            def _blocking() -> Dict[str, Any]:
                out = {"evicted": self.enforce_token_bound()}
                if telemetry_conn is not None and telemetry_table and (
                    telemetry_ts_column
                ):
                    removed, aggs = self.compact_telemetry(
                        telemetry_conn, telemetry_table, telemetry_ts_column,
                    )
                    out["telemetry_removed"] = removed
                    out["telemetry_aggregates"] = aggs
                return out

            result.update(await loop.run_in_executor(pool, _blocking))
        except Exception:  # noqa: BLE001 — sweep is fail-soft
            logger.debug("[ContextDistillGC] sweep failed", exc_info=True)
        try:
            from backend.api.hive_emitter import hive_emit, hive_flush
            snap = self.snapshot()
            hive_emit(
                actor_id="context_distillation_gc",
                subsystem="governance",
                intent="context_distilled",
                summary=(
                    f"GC sweep: {result['evicted']} bound-evict, "
                    f"{result['telemetry_removed']} telemetry rows compacted; "
                    f"active={snap.get('active_tokens', 0)}/"
                    f"{snap.get('max_active_tokens', 0)} tokens, "
                    f"{snap.get('distilled_pointers', 0)} pointers"
                ),
                severity="info",
                detail={k: v for k, v in snap.items() if isinstance(v, int)},
            )
            hive_flush("context_distillation_gc", "context_distilled")
        except Exception:  # noqa: BLE001
            pass
        return result

    # -- bus wiring --------------------------------------------------------

    async def attach_to_bus(
        self, event_bus: Any, *, pattern: str = "op.terminal.#",
    ) -> Optional[str]:
        """Subscribe the terminal-distillation handler to the
        ``TrinityEventBus``. The handler reads ``op_id`` + ``outcome``
        from the event payload and distills. Fail-soft (returns None on
        any subscribe error). Reuses the bus's own ``subscribe`` seam."""
        if not distillation_enabled() or event_bus is None:
            return None

        async def _on_terminal(event: Any) -> None:
            try:
                payload = getattr(event, "payload", None) or {}
                op_id = str(payload.get("op_id", "") or "")
                outcome = str(
                    payload.get("outcome", "")
                    or payload.get("reason_code", "")
                    or getattr(event, "topic", "").rsplit(".", 1)[-1]
                )
                self.handle_terminal(op_id, outcome)
            except Exception:  # noqa: BLE001 — handler never raises
                pass

        try:
            return await event_bus.subscribe(pattern, _on_terminal)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ContextDistillGC] bus subscribe failed", exc_info=True,
            )
            return None


_DEFAULT_GC: Optional[ContextDistillationGC] = None
_DEFAULT_GC_LOCK = threading.Lock()


def get_default_gc() -> ContextDistillationGC:
    """Process-wide default GC (what the orchestrator wires)."""
    global _DEFAULT_GC
    with _DEFAULT_GC_LOCK:
        if _DEFAULT_GC is None:
            _DEFAULT_GC = ContextDistillationGC()
        return _DEFAULT_GC


__all__ = [
    "ContextDistillationGC",
    "DistilledPointer",
    "distillation_enabled",
    "estimate_tokens",
    "get_default_gc",
]
