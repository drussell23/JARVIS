"""intake_dlq — Sovereign Dead-Letter Queue (A1-T1)
==================================================

Persistent dead-letter store for strategic-GOAL envelopes that cannot
be forwarded immediately (e.g., ``_TeeRouter.upstream is None`` at
roadmap-orchestrator boot).

Design constraints
------------------
- **Fail-soft everywhere**: ``append_dlq`` and ``replay_dlq`` NEVER raise.
- **Atomic rewrite**: ``replay_dlq`` uses temp-file + ``os.replace`` so a
  crash mid-replay leaves the original DLQ intact.
- **Dedup by goal_id**: ``replay_dlq`` forwards only the first occurrence of
  each goal_id per invocation; later duplicates are silently dropped.
- **Master switch**: ``JARVIS_INTAKE_DLQ_ENABLED`` (default ``"true"``).
  When disabled, ``append_dlq`` is a no-op and ``read_dlq`` returns ``[]``.
- **No external deps**: stdlib only (json, os, time, logging, asyncio).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Callable, Coroutine, List

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_INTAKE_DLQ_ENABLED"
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    """Return True unless ``JARVIS_INTAKE_DLQ_ENABLED`` is explicitly falsy."""
    val = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _default_path() -> str:
    """Canonical DLQ path relative to repo root."""
    return os.path.join(".jarvis", "intake_dlq.jsonl")


def _goal_id(envelope: Any) -> str:
    """Extract a stable identifier from *envelope* (dict or object)."""
    if isinstance(envelope, dict):
        for key in ("goal_id", "op_id", "id"):
            val = envelope.get(key)
            if val is not None:
                return str(val)
        return ""
    for attr in ("goal_id", "op_id", "id"):
        val = getattr(envelope, attr, None)
        if val is not None:
            return str(val)
    return ""


def _to_serializable(envelope: Any) -> Any:
    """Return a JSON-serialisable representation of *envelope*."""
    try:
        json.dumps(envelope)
        return envelope
    except (TypeError, ValueError):
        return repr(envelope)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_dlq(
    envelope: Any,
    *,
    reason: str,
    path: str | None = None,
) -> None:
    """Persist *envelope* to the DLQ and emit a CRITICAL log line.

    Never raises — any I/O error is caught and logged at WARNING.
    """
    if not _enabled():
        return
    p = path if path is not None else _default_path()
    goal = _goal_id(envelope)
    record = {
        "ts": time.time(),
        "reason": reason,
        "schema_version": _SCHEMA_VERSION,
        "goal_id": goal,
        "envelope": _to_serializable(envelope),
    }
    logger.critical(
        "[IntakeDLQ] orphaned GOAL reason=%s goal=%s", reason, goal
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntakeDLQ] append failed path=%s err=%r", p, exc)


def read_dlq(path: str | None = None) -> List[dict]:
    """Parse the DLQ JSONL file and return all valid rows.

    Returns ``[]`` when disabled, when the file is absent, or if no valid
    lines are present.  Corrupt/unparseable lines are silently skipped.
    """
    if not _enabled():
        return []
    p = path if path is not None else _default_path()
    rows: List[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip corrupt lines
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntakeDLQ] read failed path=%s err=%r", p, exc)
    return rows


async def replay_dlq(
    path: str | None,
    ingest_fn: Callable[[Any], Coroutine[Any, Any, Any]],
) -> int:
    """Re-ingest envelopes from the DLQ, deduping by goal_id.

    Parameters
    ----------
    path:
        DLQ file path (``None`` → ``_default_path()``).
    ingest_fn:
        Async callable that receives a raw envelope dict and forwards it.
        Must be awaitable; exceptions are caught (entry kept in DLQ).

    Returns
    -------
    int
        Count of successfully drained entries.

    Notes
    -----
    - Dedup is first-wins per *goal_id* within this replay call.
    - On success the survivor list is atomically rewritten (temp + rename).
    - On any file-system error the original file is left untouched.
    - NEVER raises.
    """
    if not _enabled():
        return 0
    p = path if path is not None else _default_path()
    rows = read_dlq(p)
    if not rows:
        return 0

    seen_ids: set[str] = set()
    survivors: List[dict] = []
    drained = 0

    for row in rows:
        env = row.get("envelope", row)
        gid = row.get("goal_id") or _goal_id(env)

        # Dedup: skip later duplicates of the same goal_id
        if gid and gid in seen_ids:
            continue
        if gid:
            seen_ids.add(gid)

        try:
            await ingest_fn(env)
            drained += 1
            # Successfully drained — do not add to survivors
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[IntakeDLQ] replay failed goal=%s err=%r; keeping entry",
                gid, exc,
            )
            survivors.append(row)

    # Atomically rewrite DLQ with only the survivors
    try:
        dir_name = os.path.dirname(os.path.abspath(p))
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_name,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            for row in survivors:
                tmp.write(json.dumps(row, default=str) + "\n")
            tmp_name = tmp.name
        os.replace(tmp_name, p)
        if not survivors:
            # Remove the empty file so read_dlq returns [] cleanly
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[IntakeDLQ] atomic rewrite failed path=%s err=%r", p, exc
        )

    return drained
