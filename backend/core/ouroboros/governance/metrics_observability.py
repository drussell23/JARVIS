"""P4 Slice 4 — convergence-metrics observability surfaces.

Per OUROBOROS_VENOM_PRD.md §9 Phase 4 P4 acceptance criteria:

  > Surface in ``summary.json`` + ``/metrics`` REPL + IDE
  > observability stream.
  > Persisted to ``.jarvis/metrics_history.jsonl`` (cross-session)
  > IDE GET ``/observability/metrics``

This slice owns three orthogonal observability surfaces, all
**best-effort** (failure NEVER raises into the FSM):

  1. :class:`MetricsSessionObserver` — post-VERIFY hook that:
       - computes a fresh :class:`MetricsSnapshot` via the Slice 1
         engine,
       - appends it to the Slice 2 :class:`MetricsHistoryLedger`,
       - merges the snapshot dict into the session's
         ``summary.json`` under a ``metrics:`` key,
       - publishes a :data:`EVENT_TYPE_METRICS_UPDATED` SSE event
         via the existing :class:`StreamEventBroker` (best-effort,
         never required).

  2. :func:`register_metrics_routes` — adds 4 GET endpoints to a
     caller-supplied aiohttp ``Application``:
       - ``/observability/metrics``                  — latest snapshot
       - ``/observability/metrics/window?days=N``    — window aggregate
       - ``/observability/metrics/composite``        — composite history
       - ``/observability/metrics/sessions/{id}``    — drill into one
     Mirrors the IDE-observability pattern (gate check → handler →
     ``_json_response`` style) without modifying
     :class:`IDEObservabilityRouter` directly so this slice stays
     additive + revert-safe. Slice 5 graduation wires the
     registration into ``EventChannelServer.start``.

  3. :func:`publish_metrics_updated` — thin SSE bridge so other
     callers (e.g., a future webhook surface) can fire the same
     event without re-implementing the broker plumbing.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: ``metrics_engine`` + ``metrics_history`` (own slice
    family) + ``ide_observability_stream`` (broker) +
    ``ide_observability`` helpers (CORS / rate-limit / loopback
    pattern only — endpoint code is duplicated NOT inherited so the
    metrics module never imports a class that itself imports a wider
    surface).
  * Allowed I/O: the ledger JSONL append (delegated to Slice 2) +
    the per-session ``summary.json`` merge (only writes inside the
    session_dir the harness owns; defensive against directory
    traversal). No subprocess / env mutation / network.
  * Best-effort throughout — every disk / broker / serialization
    operation is wrapped in ``try / except`` with a single warn
    log; failures never propagate.
  * SSE event payload is **summary-only** (op_id, schema_version,
    trend, composite_score_session_mean) — full snapshot lives at
    the GET endpoint. Mirrors the inline-approval grant pattern
    pinned by §8.

Default-off behind ``JARVIS_METRICS_SUITE_ENABLED`` until Slice 5
graduation. Module remains importable + callable when off so future
slices + tests + REPL consumers continue to work without flag flips.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    get_default_engine,
)
from backend.core.ouroboros.governance.metrics_history import (
    DEFAULT_WINDOW_30D_DAYS,
    DEFAULT_WINDOW_7D_DAYS,
    AggregatedMetrics,
    MetricsHistoryLedger,
    get_default_ledger,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")

# Schema version stamped into every JSON response so IDE clients can
# pin a parser version. Independent of the snapshot schema (which is
# the wire format).
METRICS_OBSERVABILITY_SCHEMA_VERSION: str = "1.0"

# Per-session summary.json filename. Pinned because the harness
# convention has lived in that name since Phase 1 P0.
SUMMARY_JSON_FILENAME: str = "summary.json"

# Cap on summary.json bytes we'll re-read before merging — defends
# against accidentally loading a corrupted multi-MB blob.
MAX_SUMMARY_JSON_BYTES: int = 4 * 1024 * 1024  # 4 MiB

# Window day-count caps for the IDE GET endpoint. 0 < days <= 365.
MIN_WINDOW_DAYS: int = 1
MAX_WINDOW_DAYS: int = 365

# Composite-history GET cap. Mirrors the REPL dispatcher's
# COMPOSITE_HISTORY_MAX_ROWS so the two surfaces show the same data.
COMPOSITE_HISTORY_MAX_ROWS: int = 8_192

# Session-id grammar — same character class as the existing
# `_SESSION_ID_RE` in ``ide_observability.py`` so this surface
# accepts the same ids the rest of the IDE GET routes do.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


def is_enabled() -> bool:
    """Master flag — ``JARVIS_METRICS_SUITE_ENABLED`` (default
    **true** post Slice 5 graduation).

    All Slice 4 surfaces gate on this. When off:
      * ``MetricsSessionObserver.record_session_end`` returns early
        with ``notes=("master_off",)`` (no compute, no append, no
        merge, no SSE).
      * IDE GET endpoints return 403 ``ide_observability.disabled``
        so port scanners see no signal about the surface.
      * SSE publish is a no-op (drop silently)."""
    return os.environ.get(
        "JARVIS_METRICS_SUITE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# MetricsSessionObserver — post-VERIFY hook
# ---------------------------------------------------------------------------


@dataclass
class SessionObservation:
    """Result of one ``MetricsSessionObserver.record_session_end`` call.

    Returned to the harness so it can log + assert the snapshot landed
    correctly. ``snapshot`` is None when master-off OR when computation
    failed; ``ledger_appended`` reflects the JSONL write outcome;
    ``summary_merged`` is True when the per-session ``summary.json``
    was successfully updated (False on missing dir / read-only / etc);
    ``sse_published`` is True when the broker accepted the event.
    """

    snapshot: Optional[MetricsSnapshot]
    ledger_appended: bool
    summary_merged: bool
    sse_published: bool
    notes: tuple = ()


class MetricsSessionObserver:
    """Wires the engine + ledger + summary.json + SSE broker into one
    post-VERIFY observer call. Master-off short-circuits everything
    so harness wiring stays a no-op until Slice 5 graduation flips
    the default.

    Slice 5 graduation wires this against the harness's session-end
    path; until then, callers (tests + future Slice 5) instantiate
    explicitly."""

    def __init__(
        self,
        engine: Optional[MetricsEngine] = None,
        ledger: Optional[MetricsHistoryLedger] = None,
        broker_publisher: Optional[Callable[[str, str, Mapping[str, Any]], Any]] = None,
        clock=time.time,
    ) -> None:
        self._engine = engine
        self._ledger = ledger
        self._broker_publisher = broker_publisher
        self._clock = clock
        # Bound the warn-once log so a permanently-broken broker
        # doesn't spam the log.
        self._broker_warned = False
        self._summary_warned = False
        self._lock = threading.Lock()

    def _eng(self) -> MetricsEngine:
        return self._engine or get_default_engine()

    def _led(self) -> MetricsHistoryLedger:
        return self._ledger or get_default_ledger()

    # ---- public entry point ----

    def record_session_end(
        self,
        *,
        session_id: str,
        session_dir: Optional[Path] = None,
        ops: Sequence[Mapping[str, Any]] = (),
        sessions_history: Sequence[Mapping[str, Any]] = (),
        posture_dwells: Sequence[Mapping[str, Any]] = (),
        total_cost_usd: Any = 0.0,
        commits: Any = 0,
    ) -> SessionObservation:
        """Compute + persist + announce one session's metrics snapshot.

        Best-effort: no individual failure (compute / append /
        summary.json / SSE) propagates to the caller. The harness
        invokes this from a ``finally`` block at session-end so it
        runs even on abnormal exit."""
        if not is_enabled():
            return SessionObservation(
                snapshot=None, ledger_appended=False,
                summary_merged=False, sse_published=False,
                notes=("master_off",),
            )

        notes: List[str] = []

        # 1. Compute snapshot.
        try:
            snap = self._eng().compute_for_session(
                session_id=session_id,
                ops=ops,
                sessions_history=sessions_history,
                posture_dwells=posture_dwells,
                total_cost_usd=total_cost_usd,
                commits=commits,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObserver] compute failed for %s: %s",
                session_id, exc,
            )
            return SessionObservation(
                snapshot=None, ledger_appended=False,
                summary_merged=False, sse_published=False,
                notes=("compute_failed",),
            )

        # 2. Append to ledger.
        ledger_appended = False
        try:
            ledger_appended = self._led().append(snap)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObserver] ledger append failed for %s: %s",
                session_id, exc,
            )
            notes.append("ledger_append_failed")

        # 3. Merge into per-session summary.json.
        summary_merged = False
        if session_dir is not None:
            summary_merged = self._safe_merge_summary(session_dir, snap)
            if not summary_merged:
                notes.append("summary_merge_failed")

        # 4. Publish SSE event (best-effort).
        sse_published = self._safe_publish_sse(snap)
        if not sse_published:
            notes.append("sse_publish_failed")

        return SessionObservation(
            snapshot=snap,
            ledger_appended=ledger_appended,
            summary_merged=summary_merged,
            sse_published=sse_published,
            notes=tuple(notes),
        )

    # ---- internals ----

    def _safe_merge_summary(
        self,
        session_dir: Path,
        snap: MetricsSnapshot,
    ) -> bool:
        """Merge ``snap.to_dict()`` into ``<session_dir>/summary.json``
        under a top-level ``metrics`` key. Read-modify-write under the
        observer lock so concurrent writers don't clobber each other.

        Defensive contract:
          * Path resolution: only writes ``session_dir / SUMMARY_JSON_FILENAME``.
            No traversal allowed.
          * Missing file → create new with ``{"metrics": ...}``.
          * Existing file > MAX_SUMMARY_JSON_BYTES → skip merge
            (refuse to load a suspicious blob), warn once.
          * Any I/O failure → log once + return False.
        """
        try:
            summary_path = session_dir / SUMMARY_JSON_FILENAME
            with self._lock:
                existing: Dict[str, Any] = {}
                if summary_path.exists():
                    try:
                        size = summary_path.stat().st_size
                        if size > MAX_SUMMARY_JSON_BYTES:
                            if not self._summary_warned:
                                logger.warning(
                                    "[MetricsObserver] summary.json at %s "
                                    "exceeds MAX_SUMMARY_JSON_BYTES=%d "
                                    "(was %d) — merge skipped",
                                    summary_path, MAX_SUMMARY_JSON_BYTES,
                                    size,
                                )
                                self._summary_warned = True
                            return False
                        text = summary_path.read_text(encoding="utf-8")
                        if text.strip():
                            existing = json.loads(text)
                            if not isinstance(existing, dict):
                                # Existing file not a JSON object —
                                # don't risk overwriting unknown shape.
                                logger.warning(
                                    "[MetricsObserver] summary.json at %s "
                                    "is not a JSON object — merge skipped",
                                    summary_path,
                                )
                                return False
                    except (OSError, json.JSONDecodeError) as exc:
                        # Treat as empty — write a fresh blob.
                        logger.debug(
                            "[MetricsObserver] summary.json read failed "
                            "(%s) — writing fresh", exc,
                        )
                        existing = {}
                existing["metrics"] = snap.to_dict()
                # Stamp a metadata field so future readers can tell this
                # block was written by Phase 4 P4 Slice 4.
                existing["metrics_observability_schema"] = (
                    METRICS_OBSERVABILITY_SCHEMA_VERSION
                )
                # Atomic-ish write: temp file + rename.
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = summary_path.with_suffix(".json.tmp")
                tmp_path.write_text(
                    json.dumps(existing, default=str, indent=2),
                    encoding="utf-8",
                )
                tmp_path.replace(summary_path)
            return True
        except OSError as exc:
            if not self._summary_warned:
                logger.warning(
                    "[MetricsObserver] summary.json merge failed: %s",
                    exc,
                )
                self._summary_warned = True
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObserver] summary.json unexpected error: %s",
                exc,
            )
            return False

    def _safe_publish_sse(self, snap: MetricsSnapshot) -> bool:
        """Publish ``EVENT_TYPE_METRICS_UPDATED`` via the broker.

        Payload is summary-only (op_id, schema_version, trend,
        composite mean). Operators get a live ping; the full record
        lives at the GET endpoint."""
        publisher = self._broker_publisher or _default_broker_publisher
        if publisher is None:
            return False
        try:
            event_id = publisher(
                _METRICS_UPDATED_EVENT_TYPE,
                snap.session_id,
                {
                    "session_id": snap.session_id,
                    "schema_version": snap.schema_version,
                    "trend": snap.trend.value,
                    "composite_score_session_mean": (
                        snap.composite_score_session_mean
                    ),
                },
            )
            return event_id is not None
        except Exception as exc:  # noqa: BLE001
            if not self._broker_warned:
                logger.warning(
                    "[MetricsObserver] SSE publish failed: %s "
                    "(further failures suppressed)", exc,
                )
                self._broker_warned = True
            return False


# ---------------------------------------------------------------------------
# SSE bridge helper (so non-observer callers can fire the same event)
# ---------------------------------------------------------------------------


# String literal — looking up the constant via late import keeps the
# module structurally importable even if ide_observability_stream is
# absent (e.g. minimal test environment without aiohttp).
_METRICS_UPDATED_EVENT_TYPE: str = "metrics_updated"


def _default_broker_publisher(
    event_type: str, op_id: str, payload: Mapping[str, Any],
) -> Optional[str]:
    """Sentinel used when no publisher is wired. Returns None so the
    observer's SSE step records ``sse_publish_failed`` without
    raising. Real publisher (broker.publish) is injected by Slice 5
    graduation."""
    return None


def publish_metrics_updated(
    snap: MetricsSnapshot,
) -> Optional[str]:
    """Fire the ``metrics_updated`` SSE event for ``snap``.

    Returns the broker-assigned event id when published, else None
    (broker missing / disabled / publish raised). Best-effort —
    never raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_METRICS_UPDATED,
            get_default_broker,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_METRICS_UPDATED,
            op_id=snap.session_id,
            payload={
                "session_id": snap.session_id,
                "schema_version": snap.schema_version,
                "trend": snap.trend.value,
                "composite_score_session_mean": (
                    snap.composite_score_session_mean
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MetricsObservability] publish_metrics_updated swallowed: %s",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# IDE GET endpoints — register on a caller-supplied aiohttp Application
# ---------------------------------------------------------------------------


def register_metrics_routes(
    app: Any,
    *,
    engine: Optional[MetricsEngine] = None,
    ledger: Optional[MetricsHistoryLedger] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None,
) -> None:
    """Mount 4 GET routes on ``app``.

    Mirrors the :class:`IDEObservabilityRouter` shape (gate → handler
    → JSON response with ``schema_version`` + ``Cache-Control:
    no-store`` + CORS). Caller supplies the rate-limit + CORS
    callables (Slice 5 wires those from
    :class:`IDEObservabilityRouter` so all `/observability/*` routes
    share one rate-limit budget + one CORS allowlist).

    When called with ``rate_limit_check=None``, every request is
    allowed (test convenience). Production callers MUST supply both
    callables."""
    handler = _MetricsRoutesHandler(
        engine=engine,
        ledger=ledger,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/metrics", handler.handle_current,
    )
    app.router.add_get(
        "/observability/metrics/window", handler.handle_window,
    )
    app.router.add_get(
        "/observability/metrics/composite", handler.handle_composite,
    )
    app.router.add_get(
        "/observability/metrics/sessions/{session_id}",
        handler.handle_session_detail,
    )


@dataclass
class _MetricsRoutesHandler:
    engine: Optional[MetricsEngine] = None
    ledger: Optional[MetricsHistoryLedger] = None
    rate_limit_check: Optional[Callable[[Any], bool]] = None
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None

    def _eng(self) -> MetricsEngine:
        return self.engine or get_default_engine()

    def _led(self) -> MetricsHistoryLedger:
        return self.ledger or get_default_ledger()

    # ---- shared gate ----

    def _gate_check(self, request: Any) -> Optional[Any]:
        if not is_enabled():
            return self._error(request, 403,
                               "ide_observability.disabled")
        if self.rate_limit_check is not None:
            try:
                if not self.rate_limit_check(request):
                    return self._error(request, 429,
                                       "ide_observability.rate_limited")
            except Exception:  # noqa: BLE001
                # Defensive: a broken rate limiter shouldn't 500 the
                # endpoint. Treat as allowed so observability stays
                # available; log debug.
                logger.debug(
                    "[MetricsObservability] rate_limit_check raised",
                    exc_info=True,
                )
        return None

    # ---- response helpers ----

    def _json(
        self, request: Any, status: int, payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": METRICS_OBSERVABILITY_SCHEMA_VERSION,
                **payload,
            }
        resp = web.json_response(payload, status=status)
        if self.cors_headers is not None:
            try:
                for k, v in self.cors_headers(request).items():
                    resp.headers[k] = v
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[MetricsObservability] cors_headers raised",
                    exc_info=True,
                )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error(self, request: Any, status: int, code: str) -> Any:
        return self._json(
            request, status,
            {"error": True, "reason_code": code},
        )

    # ---- handlers ----

    async def handle_current(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            rows = self._led().read_all(limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObservability] current read failed: %s", exc,
            )
            return self._json(request, 200,
                              {"snapshot": None,
                               "reason_code": "read_failed"})
        if not rows:
            return self._json(request, 200, {"snapshot": None})
        return self._json(request, 200, {"snapshot": rows[-1]})

    async def handle_window(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        days_raw = request.query.get("days", str(DEFAULT_WINDOW_7D_DAYS))
        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            return self._error(request, 400,
                               "ide_observability.malformed_days")
        if days < MIN_WINDOW_DAYS or days > MAX_WINDOW_DAYS:
            return self._error(request, 400,
                               "ide_observability.days_out_of_range")
        try:
            agg = self._led().aggregate_window(days)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObservability] window read failed: %s", exc,
            )
            return self._json(request, 200,
                              {"aggregate": None,
                               "reason_code": "read_failed"})
        return self._json(request, 200, {"aggregate": agg.to_dict()})

    async def handle_composite(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            limit_raw = request.query.get(
                "limit", str(COMPOSITE_HISTORY_MAX_ROWS),
            )
            limit = max(1, min(int(limit_raw), COMPOSITE_HISTORY_MAX_ROWS))
        except (TypeError, ValueError):
            return self._error(request, 400,
                               "ide_observability.malformed_limit")
        try:
            rows = self._led().read_all(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObservability] composite read failed: %s", exc,
            )
            return self._json(request, 200,
                              {"composite_history": [],
                               "reason_code": "read_failed"})
        history = [
            {
                "session_id": r.get("session_id"),
                "computed_at_unix": r.get("computed_at_unix"),
                "composite_score_session_mean": r.get(
                    "composite_score_session_mean"),
                "trend": r.get("trend"),
            }
            for r in rows if isinstance(r, dict)
        ]
        return self._json(request, 200, {
            "composite_history": history,
            "rows_seen": len(rows),
        })

    async def handle_session_detail(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        session_id = request.match_info.get("session_id", "")
        if not _SESSION_ID_RE.match(session_id):
            return self._error(request, 400,
                               "ide_observability.bad_session_id")
        try:
            rows = self._led().read_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsObservability] session-detail read failed: %s",
                exc,
            )
            return self._json(request, 200,
                              {"snapshot": None,
                               "reason_code": "read_failed"})
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("session_id") == session_id),
            None,
        )
        if match is None:
            return self._error(request, 404,
                               "ide_observability.session_not_found")
        return self._json(request, 200, {"snapshot": match})


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_observer: Optional[MetricsSessionObserver] = None
_default_observer_lock = threading.Lock()


def get_default_observer() -> MetricsSessionObserver:
    """Process-wide observer. Lazy-construct on first call. No master
    flag on the accessor — observer is callable when reverted (its
    ``record_session_end`` short-circuits, returning a SessionObservation
    that explicitly records ``("master_off",)`` in notes)."""
    global _default_observer
    with _default_observer_lock:
        if _default_observer is None:
            _default_observer = MetricsSessionObserver()
    return _default_observer


def reset_default_observer() -> None:
    """Reset the singleton — for tests."""
    global _default_observer
    with _default_observer_lock:
        _default_observer = None


__all__ = [
    "COMPOSITE_HISTORY_MAX_ROWS",
    "MAX_SUMMARY_JSON_BYTES",
    "MAX_WINDOW_DAYS",
    "METRICS_OBSERVABILITY_SCHEMA_VERSION",
    "MIN_WINDOW_DAYS",
    "MetricsSessionObserver",
    "SUMMARY_JSON_FILENAME",
    "SessionObservation",
    "get_default_observer",
    "is_enabled",
    "publish_metrics_updated",
    "register_metrics_routes",
    "reset_default_observer",
]
