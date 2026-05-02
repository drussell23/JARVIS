"""Q4 Priority #2 Slice 2 — ClosureLoop bounded JSONL ring buffer.

Cross-process flock'd store for :class:`ClosureLoopRecord` outputs.
Mirrors the Priority #5 ``gradient_observer`` history pattern exactly:
``flock_append_line`` for atomic append + ``flock_critical_section``
for ring rotation. Both primitives live in
``backend.core.ouroboros.governance.cross_process_jsonl`` (Tier 1 #3
substrate, NEVER raises).

Authority invariant (AST-pinned in Slice 4):
  This module imports nothing from ``yaml_writer``, ``meta_governor``,
  ``orchestrator``, ``policy``, ``iron_gate``, ``risk_tier``,
  ``change_engine``, ``candidate_generator``, or ``gate``. The
  closure-loop's role is to PREPARE proposals for operator approval —
  the store layer is read/append/rotate over a JSONL ring buffer
  ONLY. No mutation surface reaches a policy primitive.

Schema discipline:
  Every record carries the
  :data:`closure_loop_orchestrator.CLOSURE_LOOP_SCHEMA_VERSION`
  string. Schema-mismatched lines on read are silently dropped
  (cold-start / future-rollforward safety) — same convention as
  PostureStore + InvariantDriftStore + gradient_observer history.
"""
from __future__ import annotations

import enum
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)
from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    CLOSURE_LOOP_SCHEMA_VERSION,
    ClosureLoopRecord,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs — every tunable parameter reads from environment with
# documented defaults + clamps. No hardcoding.
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def closure_loop_history_dir() -> Path:
    """``JARVIS_CLOSURE_LOOP_HISTORY_DIR`` — defaults to ``.jarvis/``
    so the closure-loop history sits alongside posture, semantic
    index, last_session_summary etc."""
    raw = os.environ.get("JARVIS_CLOSURE_LOOP_HISTORY_DIR")
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path(".jarvis").resolve()


def closure_loop_history_path() -> Path:
    """Default path to the JSONL ring buffer."""
    return closure_loop_history_dir() / "closure_loop_history.jsonl"


def closure_loop_history_max_records() -> int:
    """``JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS`` — ring buffer
    capacity. Default 1024, clamped [16, 65536]. Bounded growth is
    a §8 invariant — operators set this once, system rotates."""
    return _env_int_clamped(
        "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS",
        1024, floor=16, ceiling=65536,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy outcome
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy. Matches the convention used by
    coherence_action_bridge.RecordOutcome + gradient_observer
    .RecordOutcome — uniform vocabulary across the verification
    layer's persistence helpers."""

    OK = "ok"
    DEDUPED = "deduped"
    REJECTED = "rejected"
    DISABLED = "disabled"
    PERSIST_ERROR = "persist_error"


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def record_closure_outcome(
    record: ClosureLoopRecord,
    *,
    enabled_override: Optional[bool] = None,
) -> RecordOutcome:
    """Append one :class:`ClosureLoopRecord` to the bounded JSONL
    ring + best-effort rotate.

    Decision tree:

      1. Master-flag check (or ``enabled_override``)
      2. Input shape validation
      3. JSON serialize
      4. ``flock_append_line`` (atomic, cross-process)
      5. ``_rotate_history`` inside flock'd critical section if
         capacity exceeded

    NEVER raises. Returns one of :class:`RecordOutcome` so callers
    can branch on PERSIST_ERROR vs OK without exception handling.
    """
    try:
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
                closure_loop_orchestrator_enabled,
            )
            if not closure_loop_orchestrator_enabled():
                return RecordOutcome.DISABLED

        if not isinstance(record, ClosureLoopRecord):
            return RecordOutcome.REJECTED

        try:
            line = json.dumps(
                record.to_dict(),
                sort_keys=True, ensure_ascii=True,
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[ClosureLoopStore] serialize failed: %s", exc,
            )
            return RecordOutcome.PERSIST_ERROR

        path = closure_loop_history_path()
        appended = flock_append_line(path, line)
        if not appended:
            return RecordOutcome.PERSIST_ERROR

        try:
            _rotate_history(path, closure_loop_history_max_records())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[ClosureLoopStore] rotate failed: %s", exc,
            )

        return RecordOutcome.OK
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ClosureLoopStore] record_closure_outcome: %s", exc,
        )
        return RecordOutcome.PERSIST_ERROR


def _rotate_history(path: Path, max_records: int) -> bool:
    """Truncate JSONL to last ``max_records`` lines under flock'd
    critical section. Same discipline as gradient_observer +
    InvariantDriftStore + coherence_window_store. NEVER raises."""
    if max_records < 1:
        return False
    if not path.exists():
        return True
    try:
        with flock_critical_section(path) as acquired:
            if not acquired:
                return False
            try:
                with path.open("r", encoding="utf-8") as fh:
                    lines = [ln for ln in fh if ln.strip()]
            except OSError:
                return False
            if len(lines) <= max_records:
                return True
            tail = lines[-max_records:]
            try:
                with path.open("w", encoding="utf-8") as fh:
                    for ln in tail:
                        if not ln.endswith("\n"):
                            ln = ln + "\n"
                        fh.write(ln)
                    fh.flush()
                return True
            except OSError:
                return False
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ClosureLoopStore] _rotate_history: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_closure_history(
    *,
    limit: Optional[int] = None,
    since_ts: float = 0.0,
) -> Tuple[ClosureLoopRecord, ...]:
    """Return up to last ``limit`` records with
    ``decided_at_ts >= since_ts``. NEVER raises. Returns empty tuple
    on missing file or any parse fault. Tolerates corrupt lines."""
    try:
        path = closure_loop_history_path()
        if not path.exists():
            return tuple()
        cap = (
            int(limit) if limit is not None
            else closure_loop_history_max_records()
        )
        cap = max(0, min(cap, closure_loop_history_max_records()))
        if cap == 0:
            return tuple()
        try:
            raw_lines = [
                ln for ln in path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines() if ln.strip()
            ]
        except OSError:
            return tuple()
        records: List[ClosureLoopRecord] = []
        for ln in raw_lines:
            try:
                payload = json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("schema_version") != CLOSURE_LOOP_SCHEMA_VERSION:
                continue
            rec = ClosureLoopRecord.from_dict(payload)
            if rec is None:
                continue
            if rec.decided_at_ts < since_ts:
                continue
            records.append(rec)
        records.sort(key=lambda r: r.decided_at_ts)
        if cap < len(records):
            records = records[-cap:]
        return tuple(records)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ClosureLoopStore] read_closure_history: %s", exc,
        )
        return tuple()


# ---------------------------------------------------------------------------
# Test hook
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Drop the JSONL history file + sibling ``.lock``. Production
    code MUST NOT call this."""
    try:
        path = closure_loop_history_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        lock_path = path.with_suffix(path.suffix + ".lock")
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — defensive
        pass


__all__ = [
    "RecordOutcome",
    "closure_loop_history_dir",
    "closure_loop_history_max_records",
    "closure_loop_history_path",
    "read_closure_history",
    "record_closure_outcome",
    "reset_for_tests",
]
