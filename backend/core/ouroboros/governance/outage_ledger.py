"""outage_ledger.py -- Durable DW outage->recovery record + async TrinityEventBus export.

Design
------
- Append-only bounded JSONL ring at ``.jarvis/outage_ledger.jsonl``.
- ``OutageRecord`` carries the outage lifecycle: open (started_ts only) ->
  close (ended_ts + duration_s stamped, optionally served_by_jprime).
- ``OutageLedger`` is the durable store: open/close/recent/has_open_outage.
- ``emit_outage_event`` is a synchronous fire-and-forget entry point that
  schedules an async coroutine via ``asyncio.create_task``. Strong refs are
  kept in ``_INFLIGHT_TASKS`` so GC cannot reap in-flight tasks. No-op when
  no running loop exists.
- All methods fail-soft: NEVER raise into the FSM.

Env gates
---------
- ``JARVIS_OUTAGE_LEDGER_ENABLED``      default "true"
- ``JARVIS_OUTAGE_LEDGER_PATH``         default ".jarvis/outage_ledger.jsonl"
- ``JARVIS_OUTAGE_LEDGER_MAX``          default 200 (bounded ring size)
- ``JARVIS_TRINITY_OUTAGE_EXPORT_ENABLED`` default "true"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level strong refs for in-flight asyncio tasks (prevent GC reap)
# ---------------------------------------------------------------------------
_INFLIGHT_TASKS: set = set()


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: str = "true") -> bool:
    val = os.environ.get(name, default).strip().lower()
    return val not in {"0", "false", "no", "off"}


def _ledger_enabled() -> bool:
    return _env_bool("JARVIS_OUTAGE_LEDGER_ENABLED", "true")


def _export_enabled() -> bool:
    return _env_bool("JARVIS_TRINITY_OUTAGE_EXPORT_ENABLED", "true")


def _default_path() -> str:
    return os.environ.get(
        "JARVIS_OUTAGE_LEDGER_PATH",
        os.path.join(".jarvis", "outage_ledger.jsonl"),
    )


def _max_records() -> int:
    try:
        return int(os.environ.get("JARVIS_OUTAGE_LEDGER_MAX", "200"))
    except (ValueError, TypeError):
        return 200


# ---------------------------------------------------------------------------
# OutageRecord
# ---------------------------------------------------------------------------

class OutageRecord:
    """Lightweight value object representing one DW outage lifecycle event.

    Uses ``__slots__`` to keep memory overhead minimal (many may be live
    in the bounded ring simultaneously).
    """

    __slots__ = (
        "outage_id",
        "started_ts",
        "ended_ts",
        "duration_s",
        "failure_mode",
        "error_codes",
        "lane",
        "model_ids",
        "dilation_hops",
        "served_by_jprime",
        "jprime_uptime_s",
    )

    def __init__(
        self,
        outage_id: str,
        started_ts: float,
        ended_ts: Optional[float] = None,
        duration_s: Optional[float] = None,
        failure_mode: str = "TIMEOUT",
        error_codes: Optional[List[str]] = None,
        lane: str = "batch+realtime",
        model_ids: Optional[List[str]] = None,
        dilation_hops: int = 0,
        served_by_jprime: bool = False,
        jprime_uptime_s: Optional[float] = None,
    ) -> None:
        self.outage_id = outage_id
        self.started_ts = started_ts
        self.ended_ts = ended_ts
        self.duration_s = duration_s
        self.failure_mode = failure_mode
        self.error_codes: List[str] = error_codes if error_codes is not None else []
        self.lane = lane
        self.model_ids: List[str] = model_ids if model_ids is not None else []
        self.dilation_hops = dilation_hops
        self.served_by_jprime = served_by_jprime
        self.jprime_uptime_s = jprime_uptime_s

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "outage_id": self.outage_id,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "duration_s": self.duration_s,
            "failure_mode": self.failure_mode,
            "error_codes": list(self.error_codes),
            "lane": self.lane,
            "model_ids": list(self.model_ids),
            "dilation_hops": self.dilation_hops,
            "served_by_jprime": self.served_by_jprime,
            "jprime_uptime_s": self.jprime_uptime_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OutageRecord":
        return cls(
            outage_id=str(d.get("outage_id", "")),
            started_ts=float(d.get("started_ts", 0.0)),
            ended_ts=float(d["ended_ts"]) if d.get("ended_ts") is not None else None,
            duration_s=float(d["duration_s"]) if d.get("duration_s") is not None else None,
            failure_mode=str(d.get("failure_mode", "TIMEOUT")),
            error_codes=list(d.get("error_codes") or []),
            lane=str(d.get("lane", "batch+realtime")),
            model_ids=list(d.get("model_ids") or []),
            dilation_hops=int(d.get("dilation_hops", 0)),
            served_by_jprime=bool(d.get("served_by_jprime", False)),
            jprime_uptime_s=float(d["jprime_uptime_s"]) if d.get("jprime_uptime_s") is not None else None,
        )

    def __repr__(self) -> str:
        return (
            f"OutageRecord(id={self.outage_id!r}, lane={self.lane!r}, "
            f"started={self.started_ts}, ended={self.ended_ts}, "
            f"failure_mode={self.failure_mode!r})"
        )


# ---------------------------------------------------------------------------
# OutageLedger
# ---------------------------------------------------------------------------

class OutageLedger:
    """Durable bounded JSONL ring for DW outage records.

    Parameters
    ----------
    path:
        File path for the JSONL ledger. Defaults to the env-configured path.
    max_records:
        Maximum records to retain. Older records are trimmed on persist.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        max_records: Optional[int] = None,
    ) -> None:
        self._path = path if path is not None else _default_path()
        self._max = max_records if max_records is not None else _max_records()

    # ------------------------------------------------------------------
    # Internal persistence
    # ------------------------------------------------------------------

    def _load(self) -> List[OutageRecord]:
        """Load all records from the JSONL file. Returns [] on any error."""
        records: List[OutageRecord] = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                        records.append(OutageRecord.from_dict(d))
                    except Exception:  # noqa: BLE001 -- skip corrupt lines
                        pass
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OutageLedger] load failed path=%s err=%r", self._path, exc)
        return records

    def _persist(self, records: List[OutageRecord]) -> None:
        """Atomically rewrite the ledger, trimmed to newest ``_max`` records."""
        # Keep the newest _max records (trim oldest first)
        trimmed = records[-self._max :]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
            dir_name = os.path.dirname(os.path.abspath(self._path))
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for rec in trimmed:
                        fh.write(json.dumps(rec.to_dict(), default=str) + "\n")
                os.replace(tmp_path, self._path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OutageLedger] persist failed path=%s err=%r", self._path, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_outage(
        self,
        *,
        failure_mode: str = "TIMEOUT",
        error_codes: Optional[List[str]] = None,
        lane: str = "batch+realtime",
        model_ids: Optional[List[str]] = None,
        dilation_hops: int = 0,
    ) -> str:
        """Record the start of a DW outage. Returns the outage_id.

        Dedup: if an outage with the same ``lane`` and ``ended_ts is None``
        already exists, returns its id without creating a duplicate.
        """
        if not _ledger_enabled():
            return ""
        try:
            records = self._load()
            # Dedup: return existing open outage for same lane
            for rec in reversed(records):
                if rec.lane == lane and rec.ended_ts is None:
                    return rec.outage_id
            outage_id = str(uuid.uuid4())
            rec = OutageRecord(
                outage_id=outage_id,
                started_ts=time.time(),
                failure_mode=failure_mode,
                error_codes=error_codes,
                lane=lane,
                model_ids=model_ids,
                dilation_hops=dilation_hops,
            )
            records.append(rec)
            self._persist(records)
            logger.info(
                "[OutageLedger] outage OPENED id=%s lane=%s failure_mode=%s hops=%s",
                outage_id, lane, failure_mode, dilation_hops,
            )
            return outage_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OutageLedger] open_outage failed err=%r", exc)
            return ""

    def close_outage(
        self,
        outage_id: str,
        *,
        served_by_jprime: bool = False,
        jprime_uptime_s: Optional[float] = None,
    ) -> None:
        """Stamp ``ended_ts`` and ``duration_s`` on the matching open outage."""
        if not _ledger_enabled():
            return
        try:
            records = self._load()
            now = time.time()
            for rec in records:
                if rec.outage_id == outage_id and rec.ended_ts is None:
                    rec.ended_ts = now
                    rec.duration_s = now - rec.started_ts
                    rec.served_by_jprime = served_by_jprime
                    rec.jprime_uptime_s = jprime_uptime_s
                    self._persist(records)
                    logger.info(
                        "[OutageLedger] outage CLOSED id=%s duration_s=%.1f jprime=%s",
                        outage_id, rec.duration_s, served_by_jprime,
                    )
                    return
            logger.debug(
                "[OutageLedger] close_outage: id=%s not found or already closed",
                outage_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OutageLedger] close_outage failed err=%r", exc)

    def recent(self, n: int = 50) -> List[OutageRecord]:
        """Return the most recent *n* records (newest last).

        Returns [] on any error.
        """
        if not _ledger_enabled():
            return []
        try:
            records = self._load()
            return records[-n:] if n > 0 else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OutageLedger] recent failed err=%r", exc)
            return []

    def has_open_outage(self) -> bool:
        """Return True if any outage record has ``ended_ts is None``."""
        if not _ledger_enabled():
            return False
        try:
            records = self._load()
            return any(rec.ended_ts is None for rec in records)
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[OutageLedger] = None


def get_outage_ledger() -> OutageLedger:
    """Return (or lazily create) the process-wide OutageLedger singleton."""
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = OutageLedger()
    return _singleton


# ---------------------------------------------------------------------------
# Async TrinityEventBus export (fire-and-forget)
# ---------------------------------------------------------------------------

async def _publish_outage_event(kind: str, record: OutageRecord) -> None:
    """Async coroutine: lazy-import TrinityEventBus and publish.

    Never raises -- any error is caught and logged at WARNING.
    """
    if not _export_enabled():
        return
    try:
        from backend.core.trinity_event_bus import (  # noqa: PLC0415
            get_event_bus_if_exists,
            TrinityEvent,
            EventPriority,
            RepoType,
        )
        bus = get_event_bus_if_exists()
        if bus is None or not getattr(bus, "_running", False):
            return
        event = TrinityEvent(
            topic=kind,
            source=RepoType.JARVIS,
            priority=EventPriority.HIGH,
            payload=record.to_dict(),
        )
        await bus.publish(event, persist=True)
        logger.debug("[OutageLedger] exported event kind=%s id=%s", kind, record.outage_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OutageLedger] event export failed kind=%s err=%r", kind, exc)


def emit_outage_event(kind: str, record: OutageRecord) -> None:
    """Synchronous fire-and-forget entry point for Trinity event export.

    Schedules ``_publish_outage_event`` as an asyncio task. Strong refs are
    kept in ``_INFLIGHT_TASKS`` so GC cannot reap the task before it
    completes. Task completion removes the ref automatically.

    No-op (no exception) if there is no running event loop.
    """
    if not _export_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop -- silence (batch or startup context)
        return
    try:
        task = loop.create_task(_publish_outage_event(kind, record))
        _INFLIGHT_TASKS.add(task)
        task.add_done_callback(_INFLIGHT_TASKS.discard)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OutageLedger] emit_outage_event schedule failed err=%r", exc)
