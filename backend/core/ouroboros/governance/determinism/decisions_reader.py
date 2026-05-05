"""Upgrade 2 (PRD §31.3) Slice 3 — Shared read primitives for the
decisions ledger. Used by :mod:`decisions_repl` and
:mod:`decisions_observability` so the filesystem walk + JSONL
parse logic lives in ONE place.

Architectural locks (operator mandate):

  * **Pure read-only** — every function reads the ledger; none
    write. AST-pinned at Slice 5.
  * **Reuses existing path resolution** — derives ledger paths
    via :func:`session_replay._ledger_dir`. NEVER hardcodes
    ``.jarvis/determinism``.
  * **Cross-process tear-safe** — uses
    :func:`flock_critical_section` for the read so a concurrent
    in-flight session cannot tear mid-line.
  * **NEVER raises out** of any public function — defensive
    exception handling everywhere; failures surface as empty
    tuples / structured diagnostics.
  * **Authority asymmetry** (AST-pinned at Slice 5) — module
    MUST NOT import orchestrator / iron_gate / providers /
    urgency_router / candidate_generator / tool_executor /
    auto_action_router / strategic_direction.
  * **Bounded** — all read functions accept ``limit`` parameter
    with hard caps. Storage corruption / runaway ledgers cannot
    cause unbounded memory growth.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


DECISIONS_READER_SCHEMA_VERSION: str = "decisions_reader.1"


# ---------------------------------------------------------------------------
# Hard caps — env-tunable but bounded
# ---------------------------------------------------------------------------


_DEFAULT_RECORDS_PER_SESSION: int = 100
_MAX_RECORDS_PER_SESSION: int = 10_000
_MAX_LEDGER_FILE_BYTES: int = 100 * 1024 * 1024  # 100 MB
_MAX_SESSIONS_LISTED: int = 1_000


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Bounded integer env-knob read. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def default_record_limit() -> int:
    """``JARVIS_DECISIONS_READER_DEFAULT_LIMIT`` — default
    records-per-query when caller doesn't supply a limit. Default
    100; clamped [1, 10000]."""
    return _read_int_knob(
        "JARVIS_DECISIONS_READER_DEFAULT_LIMIT",
        _DEFAULT_RECORDS_PER_SESSION,
        1,
        _MAX_RECORDS_PER_SESSION,
    )


def max_records_per_session() -> int:
    """Hard ceiling on per-session record count. Default 10000;
    clamped [100, 1000000]."""
    return _read_int_knob(
        "JARVIS_DECISIONS_READER_MAX_RECORDS",
        _MAX_RECORDS_PER_SESSION,
        100,
        1_000_000,
    )


def max_sessions_listed() -> int:
    """Hard ceiling on session-list size. Default 1000; clamped
    [10, 100000]."""
    return _read_int_knob(
        "JARVIS_DECISIONS_READER_MAX_SESSIONS",
        _MAX_SESSIONS_LISTED,
        10,
        100_000,
    )


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionListEntry:
    """One discoverable session under the ledger directory.
    Frozen for safe propagation."""

    session_id: str
    decisions_path: str
    """String form of the absolute path (JSON-friendly)."""
    file_size_bytes: int
    mtime_unix: float
    record_count_estimate: int
    """Approximate count via line-count of the file. Used for
    overview surfaces; precise counts come from
    :func:`read_records_for_session`."""


@dataclass(frozen=True)
class KindAggregation:
    """Histogram entry — one DecisionKind value + its count.
    Frozen for safe propagation."""

    kind: str
    count: int


@dataclass(frozen=True)
class DecisionsQueryResult:
    """Aggregate result of a query. Frozen + JSON-projectable."""

    session_id: str
    records: Tuple[Dict[str, Any], ...] = field(
        default_factory=tuple,
    )
    """Each record is a JSON-decoded dict (NOT a DecisionRecord
    instance — keeps the result trivially serializable for
    HTTP routes)."""
    total_records_in_file: int = 0
    """Total count of records in the file (independent of
    limit)."""
    elapsed_s: float = 0.0
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Path resolution — defers to existing session_replay primitive
# ---------------------------------------------------------------------------


def _resolved_ledger_dir() -> Path:
    """Resolve the ledger root via the existing
    ``session_replay._ledger_dir`` primitive. NEVER raises —
    falls through to ``.jarvis/determinism`` on any import
    failure."""
    try:
        from backend.core.ouroboros.governance.determinism.session_replay import (  # noqa: E501
            _ledger_dir,
        )
        return _ledger_dir()
    except Exception:  # noqa: BLE001 — defensive
        return Path(".jarvis") / "determinism"


def _decisions_path_for(session_id: str) -> Path:
    """Build the canonical ``<ledger>/<session>/decisions.jsonl``
    path. NEVER raises."""
    sid = (str(session_id).strip() if session_id else "")
    return _resolved_ledger_dir() / sid / "decisions.jsonl"


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def list_available_sessions(
    *,
    limit: Optional[int] = None,
) -> Tuple[SessionListEntry, ...]:
    """Discover all session directories under the ledger root.
    Returns sessions ordered by mtime descending (newest first).
    NEVER raises."""
    cap = limit if limit is not None else max_sessions_listed()
    if cap > max_sessions_listed():
        cap = max_sessions_listed()
    if cap < 1:
        cap = 1
    out = []
    try:
        root = _resolved_ledger_dir()
        if not root.exists() or not root.is_dir():
            return tuple()
        # Walk one level deep — each subdir is a session
        for child in root.iterdir():
            try:
                if not child.is_dir():
                    continue
                decisions = child / "decisions.jsonl"
                if not decisions.exists():
                    continue
                stat = decisions.stat()
                # Bounded line-count estimate — for "tracked"
                # surfaces we don't actually need to scan the
                # file; mtime + size is enough. Real counts
                # come from read_records_for_session.
                record_estimate = 0
                try:
                    if stat.st_size <= _MAX_LEDGER_FILE_BYTES:
                        with open(
                            decisions, "rb",
                        ) as f:
                            record_estimate = sum(
                                1 for _ in f
                            )
                except OSError:
                    record_estimate = 0
                out.append(SessionListEntry(
                    session_id=child.name,
                    decisions_path=str(decisions),
                    file_size_bytes=int(stat.st_size),
                    mtime_unix=float(stat.st_mtime),
                    record_count_estimate=int(record_estimate),
                ))
            except Exception:  # noqa: BLE001 — defensive
                continue
        # Newest first
        out.sort(key=lambda e: e.mtime_unix, reverse=True)
        return tuple(out[:cap])
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[decisions_reader] list_available_sessions "
            "raised: %s", exc,
        )
        return tuple()


def read_records_for_session(
    session_id: str,
    *,
    limit: Optional[int] = None,
    kind_filter: Optional[str] = None,
) -> DecisionsQueryResult:
    """Read up to ``limit`` records (most-recent N by file
    order — append-only ledger means newest are last) from one
    session's decisions.jsonl.

    Args:
        session_id: Session identifier matching ledger dir name.
        limit: Max records to return (default
            :func:`default_record_limit`; clamped
            [1, :func:`max_records_per_session`]).
        kind_filter: Optional :class:`DecisionKind`-value-or-
            freeform-string filter. When set, only records
            whose ``kind`` field equals the filter are returned.

    NEVER raises out. Returns a frozen :class:`DecisionsQueryResult`
    with structured diagnostics on any read failure."""
    import time as _time
    started = _time.monotonic()
    cap = limit if limit is not None else default_record_limit()
    if cap > max_records_per_session():
        cap = max_records_per_session()
    if cap < 1:
        cap = 1

    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        return DecisionsQueryResult(
            session_id="",
            elapsed_s=_time.monotonic() - started,
            diagnostics=(
                "session_id is required",
            ),
        )

    decisions_path = _decisions_path_for(sid)
    if not decisions_path.exists():
        return DecisionsQueryResult(
            session_id=sid,
            elapsed_s=_time.monotonic() - started,
            diagnostics=(
                f"decisions.jsonl not found at {decisions_path}",
            ),
        )

    # Cross-process tear-safe read — re-uses the same
    # flock_critical_section primitive that DecisionRuntime
    # uses for writes. No new locking code.
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
        with flock_critical_section(decisions_path) as acquired:
            if not acquired:
                return DecisionsQueryResult(
                    session_id=sid,
                    elapsed_s=_time.monotonic() - started,
                    diagnostics=(
                        "cross-process lock not acquired; "
                        "concurrent writer likely",
                    ),
                )
            try:
                stat = decisions_path.stat()
                if stat.st_size > _MAX_LEDGER_FILE_BYTES:
                    return DecisionsQueryResult(
                        session_id=sid,
                        elapsed_s=_time.monotonic() - started,
                        diagnostics=(
                            f"file_too_large: {stat.st_size} "
                            f"bytes > {_MAX_LEDGER_FILE_BYTES}",
                        ),
                    )
                text = decisions_path.read_text(
                    encoding="utf-8",
                )
            except OSError as exc:
                return DecisionsQueryResult(
                    session_id=sid,
                    elapsed_s=_time.monotonic() - started,
                    diagnostics=(
                        f"read_failed: {exc}",
                    ),
                )
    except Exception as exc:  # noqa: BLE001 — defensive
        return DecisionsQueryResult(
            session_id=sid,
            elapsed_s=_time.monotonic() - started,
            diagnostics=(
                f"flock_critical_section raised: "
                f"{type(exc).__name__}",
            ),
        )

    lines = text.splitlines()
    parsed: list = []
    skipped = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if not isinstance(obj, dict):
                skipped += 1
                continue
            if kind_filter is not None:
                if str(obj.get("kind", "")) != str(kind_filter):
                    continue
            parsed.append(obj)
        except json.JSONDecodeError:
            skipped += 1
            continue

    total = len(parsed)
    # Most-recent N — append-only ledger, so tail is newest
    tail = parsed[-cap:] if total > cap else parsed
    diagnostics = (
        ()
        if skipped == 0
        else (f"skipped_{skipped}_malformed_lines",)
    )

    return DecisionsQueryResult(
        session_id=sid,
        records=tuple(tail),
        total_records_in_file=total,
        elapsed_s=_time.monotonic() - started,
        diagnostics=diagnostics,
    )


def aggregate_kinds_for_session(
    session_id: str,
) -> Tuple[KindAggregation, ...]:
    """Histogram of DecisionKind values for one session.
    Returns entries sorted by count descending. NEVER raises."""
    result = read_records_for_session(
        session_id,
        limit=max_records_per_session(),
    )
    counts: Dict[str, int] = {}
    for r in result.records:
        try:
            k = str(r.get("kind", ""))
            if k:
                counts[k] = counts.get(k, 0) + 1
        except Exception:  # noqa: BLE001 — defensive
            continue
    out = [
        KindAggregation(kind=k, count=v)
        for k, v in counts.items()
    ]
    out.sort(key=lambda e: (-e.count, e.kind))
    return tuple(out)


def recent_records_across_sessions(
    *,
    limit: Optional[int] = None,
    kind_filter: Optional[str] = None,
) -> Tuple[Tuple[str, Dict[str, Any]], ...]:
    """Most-recent N records across ALL sessions, ordered by
    session mtime then by record order within session. Each
    entry is a ``(session_id, record_dict)`` tuple. NEVER
    raises."""
    cap = limit if limit is not None else default_record_limit()
    if cap > max_records_per_session():
        cap = max_records_per_session()
    if cap < 1:
        cap = 1
    sessions = list_available_sessions(limit=20)  # most-recent 20 sessions
    out = []
    remaining = cap
    for session_entry in sessions:
        if remaining <= 0:
            break
        result = read_records_for_session(
            session_entry.session_id,
            limit=remaining,
            kind_filter=kind_filter,
        )
        for record in reversed(result.records):
            out.append((session_entry.session_id, record))
            remaining -= 1
            if remaining <= 0:
                break
    return tuple(out)


__all__ = [
    "DECISIONS_READER_SCHEMA_VERSION",
    "DecisionsQueryResult",
    "KindAggregation",
    "SessionListEntry",
    "aggregate_kinds_for_session",
    "default_record_limit",
    "list_available_sessions",
    "max_records_per_session",
    "max_sessions_listed",
    "read_records_for_session",
    "recent_records_across_sessions",
]
