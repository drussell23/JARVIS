"""
CompactionManifest — Slice 4 of the Context Preservation arc.
=============================================================

One source of truth for "what happened in every compaction pass".

Scope
-----

Each manifest entry records a single pass of
:class:`PreservationScorer.select_preserved`: which chunks were kept,
compacted into a summary, or dropped, plus the intent snapshot that
drove the decision. The whole record is immutable and append-only so
postmortems can reconstruct preservation behaviour even after the
original chunks are long gone.

Why this matters (vs the existing counter-summary)
---------------------------------------------------

The legacy ``ContextCompactor._build_summary`` returns lines like
"12 tool_call, 3 error, 1 decision". The manifest goes further:

* **per-chunk decision trail** — for every chunk the scorer saw, a
  row with its rule_id-equivalent (score breakdown) and ``decision``
  (keep / compact / drop). Slice 4's IDE surface projects this row
  set so operators see WHY a chunk survived.
* **intent snapshot** — the recent paths / tools / error terms that
  biased the pass. Reproducible against the same inputs.
* **chronology** — passes are ordered, so drift over time is
  recoverable ("15 rounds ago we stopped caring about auth.py").

Boundaries
----------

* §1 — manifest is WRITE-ONLY via the orchestrator. Models never
  author manifest rows. The writer is the compactor (deterministic
  code) plus the scorer's output.
* §7 — immutable entries. Corrections are new entries (same pattern
  as :class:`ContextLedger`).
* §8 — the manifest is the §8 audit trail for preservation decisions.
  Slice 4's GET endpoints and SSE bridge expose it.

Observability surface
---------------------

Slice 4 ships five read-only GET endpoints on the existing
:class:`EventChannelServer`:

    GET /observability/context/ledger/{op_id}
    GET /observability/context/manifest
    GET /observability/context/manifest/{op_id}
    GET /observability/context/intent/{op_id}
    GET /observability/context/pins/{op_id}

and four new SSE event types (published via the existing broker):

    ``ledger_entry_added``   — from :class:`ContextLedger.on_change`
    ``context_compacted``    — on every manifest record
    ``context_pinned``       — from :class:`ContextPinRegistry`
    ``context_unpinned``     — from :class:`ContextPinRegistry`

A sixth event type ``context_pin_expired`` is emitted on TTL expiry.
"""
from __future__ import annotations

import enum
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Callable, Dict, FrozenSet, List, Optional, TYPE_CHECKING, Tuple,
)

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger("Ouroboros.ContextManifest")


CONTEXT_MANIFEST_SCHEMA_VERSION: str = "context_manifest.v1"
CONTEXT_OBSERVABILITY_SCHEMA_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def context_observability_enabled() -> bool:
    """Slice 4 default: OFF. Slice 5 graduates.

    Mirror of the Gap #6 / inline-permission pattern — expose the
    surface behind an explicit opt-in until the end-to-end stack is
    proved.
    """
    return os.environ.get(
        "JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "false",
    ).strip().lower() == "true"


def _manifest_rate_limit() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_CONTEXT_OBSERVABILITY_RATE_LIMIT_PER_MIN", "120",
        )))
    except (TypeError, ValueError):
        return 120


def _max_manifest_records() -> int:
    """Cap per op. Prevents a runaway op from exhausting memory via
    tens of thousands of manifest records."""
    try:
        return max(16, int(os.environ.get(
            "JARVIS_CONTEXT_MANIFEST_MAX_RECORDS", "256",
        )))
    except (TypeError, ValueError):
        return 256


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class PreservationReason(str, enum.Enum):
    """Structured reason codes for why a chunk was kept / dropped.

    Exactly one of these is recorded per chunk; callers combine this
    with the ``breakdown`` list for human detail.
    """

    PINNED = "pinned"
    HIGH_INTENT = "high_intent"
    HIGH_STRUCTURAL = "high_structural"
    RECENT = "recent"
    BUDGET_EXHAUSTED_KEEP_RATIO = "budget_exhausted_keep_ratio"
    BUDGET_EXHAUSTED_DROPPED = "budget_exhausted_dropped"


# ---------------------------------------------------------------------------
# Manifest row + record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestRow:
    """One chunk's outcome in one pass.

    ``chunk_id`` and ``index_in_sequence`` identify the chunk in the
    caller's own indexing scheme. ``decision`` is "keep" / "compact" /
    "drop". ``reason`` is the structured reason-code; ``breakdown``
    echoes the scorer's detail for debugging.
    """

    chunk_id: str
    index_in_sequence: int
    decision: str
    reason: str
    total_score: float
    char_count: int
    breakdown: Tuple[Tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ManifestRecord:
    """One full compaction pass.

    ``pass_id`` is unique per op. ``recorded_at_iso`` anchors the pass
    to wall clock so postmortem replay is possible.
    """

    pass_id: str
    op_id: str
    recorded_at_iso: str
    rows: Tuple[ManifestRow, ...]
    total_chars_before: int
    total_chars_after: int
    kept_count: int
    compacted_count: int
    dropped_count: int
    intent_snapshot: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = CONTEXT_MANIFEST_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# CompactionManifest
# ---------------------------------------------------------------------------


class CompactionManifest:
    """Per-op append-only record of compaction passes.

    Thread-safe. Listener hooks bridge to SSE (Slice 4 observability).
    """

    def __init__(
        self,
        op_id: str,
        *,
        max_records: Optional[int] = None,
    ) -> None:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        self._op_id = op_id
        self._cap = max_records or _max_manifest_records()
        self._lock = threading.Lock()
        self._records: List[ManifestRecord] = []
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- write path ------------------------------------------------------

    def record_pass(
        self,
        *,
        preservation_result: Any,
        intent_snapshot: Any = None,
    ) -> ManifestRecord:
        """Append a manifest record from a :class:`PreservationResult`.

        The :class:`PreservationResult` is duck-typed (not imported) to
        avoid tight coupling — any object with ``kept`` / ``compacted``
        / ``dropped`` / ``total_chars_before`` / ``total_chars_after``
        works.
        """
        rows: List[ManifestRow] = []
        kept_ids = {s.chunk_id for s in preservation_result.kept}
        # Reason attribution: pinned wins, then intent, then structural,
        # then recency; budget overflow for remaining chunks.
        for score in preservation_result.kept:
            rows.append(self._row_for(score, reason_for_keep(score)))
        for score in preservation_result.compacted:
            rows.append(self._row_for(
                score, PreservationReason.BUDGET_EXHAUSTED_KEEP_RATIO.value,
            ))
        for score in preservation_result.dropped:
            rows.append(self._row_for(
                score, PreservationReason.BUDGET_EXHAUSTED_DROPPED.value,
            ))
        rows.sort(key=lambda r: r.index_in_sequence)

        now_iso = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat()
        pass_id = (
            f"mf-"
            f"{abs(hash((self._op_id, now_iso, time.time_ns()))) & 0xFFFFFFFF:08x}"
        )

        intent_proj: Dict[str, Any] = {}
        if intent_snapshot is not None:
            intent_proj = _project_intent(intent_snapshot)

        record = ManifestRecord(
            pass_id=pass_id,
            op_id=self._op_id,
            recorded_at_iso=now_iso,
            rows=tuple(rows),
            total_chars_before=int(
                preservation_result.total_chars_before,
            ),
            total_chars_after=int(preservation_result.total_chars_after),
            kept_count=len(preservation_result.kept),
            compacted_count=len(preservation_result.compacted),
            dropped_count=len(preservation_result.dropped),
            intent_snapshot=intent_proj,
        )

        with self._lock:
            self._records.append(record)
            if len(self._records) > self._cap:
                self._records.pop(0)
        self._fire("context_compacted", record)
        logger.info(
            "[ContextManifest] op=%s pass=%s kept=%d compacted=%d dropped=%d "
            "chars=%d→%d",
            self._op_id, pass_id,
            record.kept_count, record.compacted_count, record.dropped_count,
            record.total_chars_before, record.total_chars_after,
        )
        return record

    # --- read path -------------------------------------------------------

    def all_records(self) -> List[ManifestRecord]:
        with self._lock:
            return list(self._records)

    def latest(self) -> Optional[ManifestRecord]:
        with self._lock:
            return self._records[-1] if self._records else None

    def get(self, pass_id: str) -> Optional[ManifestRecord]:
        with self._lock:
            for r in self._records:
                if r.pass_id == pass_id:
                    return r
        return None

    # --- listener --------------------------------------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, record: ManifestRecord) -> None:
        payload = {
            "event_type": event_type,
            "pass_id": record.pass_id,
            "op_id": record.op_id,
            "projection": project_record_summary(record),
        }
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[ContextManifest] listener exception on %s: %s",
                    event_type, exc,
                )

    # --- internals -------------------------------------------------------

    @staticmethod
    def _row_for(score: Any, reason: str) -> ManifestRow:
        return ManifestRow(
            chunk_id=getattr(score, "chunk_id", ""),
            index_in_sequence=int(getattr(score, "index_in_sequence", 0)),
            decision=(
                getattr(score.decision, "value", str(score.decision))
                if getattr(score, "decision", None) is not None else ""
            ),
            reason=reason,
            total_score=_clamp_float(getattr(score, "total", 0.0)),
            char_count=0,  # char counts aren't tracked on ChunkScore;
            # callers who want them should project via the manifest's
            # per-pass ``total_chars_*`` fields.
            breakdown=tuple(getattr(score, "breakdown", ()) or ()),
        )


def _clamp_float(v: Any) -> float:
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return 0.0
    if math.isinf(f) or math.isnan(f):
        # JSON-safe replacement for infinity (pinned chunks)
        return 1e18 if f > 0 else -1e18
    return f


def reason_for_keep(score: Any) -> str:
    """Attribute a kept chunk to the dominant signal in its breakdown."""
    if getattr(score, "pin_bonus", 0.0) and score.pin_bonus > 0:
        return PreservationReason.PINNED.value
    intent = getattr(score, "intent", 0.0) or 0.0
    structural = getattr(score, "structural", 0.0) or 0.0
    base = getattr(score, "base", 0.0) or 0.0
    if intent >= max(structural, base):
        return PreservationReason.HIGH_INTENT.value
    if structural >= max(intent, base):
        return PreservationReason.HIGH_STRUCTURAL.value
    return PreservationReason.RECENT.value


def project_record_summary(record: ManifestRecord) -> Dict[str, Any]:
    """Bounded, SSE/HTTP-safe projection of a manifest record.

    Excludes the full ``rows`` array to keep payloads light; the GET
    endpoint at ``/observability/context/manifest/{op_id}/{pass_id}``
    returns the full row set when asked.
    """
    return {
        "schema_version": record.schema_version,
        "pass_id": record.pass_id,
        "op_id": record.op_id,
        "recorded_at_iso": record.recorded_at_iso,
        "kept_count": record.kept_count,
        "compacted_count": record.compacted_count,
        "dropped_count": record.dropped_count,
        "total_chars_before": record.total_chars_before,
        "total_chars_after": record.total_chars_after,
        "intent": {
            "turn_count": record.intent_snapshot.get("turn_count", 0),
            "recent_paths": record.intent_snapshot.get("recent_paths", []),
            "recent_tools": record.intent_snapshot.get("recent_tools", []),
        },
    }


def project_record_full(record: ManifestRecord) -> Dict[str, Any]:
    """Full projection including per-chunk rows."""
    d = dict(project_record_summary(record))
    d["rows"] = [
        {
            "chunk_id": r.chunk_id,
            "index_in_sequence": r.index_in_sequence,
            "decision": r.decision,
            "reason": r.reason,
            "total_score": r.total_score,
            "char_count": r.char_count,
            "breakdown": [{"signal": k, "value": v} for k, v in r.breakdown],
        }
        for r in record.rows
    ]
    return d


def _project_intent(snap: Any) -> Dict[str, Any]:
    """Flatten an :class:`IntentSnapshot` to JSON-safe shape."""
    try:
        d = asdict(snap) if not isinstance(snap, dict) else dict(snap)
    except Exception:  # noqa: BLE001
        return {}
    for k in ("recent_paths", "recent_tools", "recent_error_terms"):
        if k in d and isinstance(d[k], tuple):
            d[k] = list(d[k])
    return d


# ---------------------------------------------------------------------------
# Registry-of-manifests
# ---------------------------------------------------------------------------


class CompactionManifestRegistry:
    def __init__(self, *, max_ops: int = 64) -> None:
        self._lock = threading.Lock()
        self._by_op: Dict[str, CompactionManifest] = {}
        self._max_ops = max(4, max_ops)

    def get_or_create(self, op_id: str) -> CompactionManifest:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        with self._lock:
            m = self._by_op.get(op_id)
            if m is not None:
                return m
            if len(self._by_op) >= self._max_ops:
                oldest = next(iter(self._by_op))
                self._by_op.pop(oldest)
            fresh = CompactionManifest(op_id)
            self._by_op[op_id] = fresh
        return fresh

    def get(self, op_id: str) -> Optional[CompactionManifest]:
        with self._lock:
            return self._by_op.get(op_id)

    def active_op_ids(self) -> List[str]:
        with self._lock:
            return list(self._by_op.keys())

    def reset(self) -> None:
        with self._lock:
            self._by_op.clear()


_default_manifest_registry: Optional[CompactionManifestRegistry] = None
_manifest_registry_lock = threading.Lock()


def get_default_manifest_registry() -> CompactionManifestRegistry:
    global _default_manifest_registry
    with _manifest_registry_lock:
        if _default_manifest_registry is None:
            _default_manifest_registry = CompactionManifestRegistry()
        return _default_manifest_registry


def reset_default_manifest_registry() -> None:
    global _default_manifest_registry
    with _manifest_registry_lock:
        if _default_manifest_registry is not None:
            _default_manifest_registry.reset()
        _default_manifest_registry = None


def manifest_for(op_id: str) -> CompactionManifest:
    return get_default_manifest_registry().get_or_create(op_id)


# ---------------------------------------------------------------------------
# IDE observability router
# ---------------------------------------------------------------------------


_OP_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")
_PASS_ID_RE = re.compile(r"^mf-[0-9a-f]{6,32}$")


class ContextObservabilityRouter:
    """Mounts five GETs for the context-preservation surface.

    Reuses loopback / CORS helpers from :mod:`ide_observability` via
    the same pattern as :class:`InlinePermissionObservabilityRouter`.
    """

    def __init__(self) -> None:
        self._rate_tracker: Dict[str, List[float]] = {}

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get(
            "/observability/context/ledger/{op_id}", self._handle_ledger,
        )
        app.router.add_get(
            "/observability/context/intent/{op_id}", self._handle_intent,
        )
        app.router.add_get(
            "/observability/context/pins/{op_id}", self._handle_pins,
        )
        app.router.add_get(
            "/observability/context/manifest", self._handle_manifest_index,
        )
        app.router.add_get(
            "/observability/context/manifest/{op_id}",
            self._handle_manifest_detail,
        )

    # --- helpers ---------------------------------------------------------

    def _client_key(self, request: "web.Request") -> str:
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_rate_limit(self, client_key: str) -> bool:
        limit = _manifest_rate_limit()
        now = time.monotonic()
        window_start = now - 60.0
        hist = self._rate_tracker.setdefault(client_key, [])
        while hist and hist[0] < window_start:
            hist.pop(0)
        if len(hist) >= limit:
            return False
        hist.append(now)
        return True

    def _cors_headers(self, request: "web.Request") -> Dict[str, str]:
        from backend.core.ouroboros.governance.ide_observability import (
            _cors_origin_patterns,
        )
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                    }
            except re.error:
                continue
        return {}

    def _json_response(
        self,
        request: "web.Request",
        status: int,
        payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": CONTEXT_OBSERVABILITY_SCHEMA_VERSION,
                **payload,
            }
        resp = web.json_response(payload, status=status)
        for k, v in self._cors_headers(request).items():
            resp.headers[k] = v
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error_response(
        self,
        request: "web.Request",
        status: int,
        reason_code: str,
    ) -> Any:
        return self._json_response(
            request, status=status,
            payload={"error": True, "reason_code": reason_code},
        )

    def _guard(self, request: "web.Request") -> Optional[Any]:
        if not context_observability_enabled():
            return self._error_response(
                request, 403, "context_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "context_observability.rate_limited",
            )
        return None

    # --- handlers --------------------------------------------------------

    async def _handle_ledger(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400, "context_observability.malformed_op_id",
            )
        try:
            from backend.core.ouroboros.governance.context_ledger import (
                get_default_registry,
            )
        except Exception:  # noqa: BLE001
            return self._json_response(
                request, 200,
                {"op_id": op_id, "summary": None, "unavailable": True},
            )
        ledger = get_default_registry().get(op_id)
        if ledger is None:
            return self._error_response(
                request, 404, "context_observability.unknown_op_id",
            )
        return self._json_response(
            request, 200,
            {"op_id": op_id, "summary": ledger.summary()},
        )

    async def _handle_intent(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400, "context_observability.malformed_op_id",
            )
        try:
            from backend.core.ouroboros.governance.context_intent import (
                get_default_tracker_registry,
            )
        except Exception:  # noqa: BLE001
            return self._error_response(
                request, 503, "context_observability.intent_unavailable",
            )
        tracker = get_default_tracker_registry().get(op_id)
        if tracker is None:
            return self._error_response(
                request, 404, "context_observability.unknown_op_id",
            )
        return self._json_response(
            request, 200,
            {"op_id": op_id, "intent": _project_intent(tracker.current_intent())},
        )

    async def _handle_pins(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400, "context_observability.malformed_op_id",
            )
        try:
            from backend.core.ouroboros.governance.context_pins import (
                get_default_pin_registries,
            )
        except Exception:  # noqa: BLE001
            return self._error_response(
                request, 503, "context_observability.pins_unavailable",
            )
        reg = get_default_pin_registries().get(op_id)
        if reg is None:
            return self._json_response(
                request, 200, {"op_id": op_id, "pins": [], "count": 0},
            )
        from backend.core.ouroboros.governance.context_pins import (
            ContextPinRegistry,
        )
        active = reg.list_active()
        return self._json_response(
            request, 200,
            {
                "op_id": op_id,
                "count": len(active),
                "pins": [
                    ContextPinRegistry._project(p) for p in active
                ],
            },
        )

    async def _handle_manifest_index(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        registry = get_default_manifest_registry()
        op_ids = sorted(registry.active_op_ids())
        return self._json_response(
            request, 200, {"op_ids": op_ids, "count": len(op_ids)},
        )

    async def _handle_manifest_detail(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400, "context_observability.malformed_op_id",
            )
        manifest = get_default_manifest_registry().get(op_id)
        if manifest is None:
            return self._error_response(
                request, 404, "context_observability.unknown_op_id",
            )
        records = manifest.all_records()
        return self._json_response(
            request, 200,
            {
                "op_id": op_id,
                "count": len(records),
                "records": [project_record_summary(r) for r in records],
            },
        )


# ---------------------------------------------------------------------------
# Bridge — ledger / pin / manifest → SSE broker
# ---------------------------------------------------------------------------


_CONTEXT_EVENT_TYPES: FrozenSet[str] = frozenset({
    "ledger_entry_added",
    "context_compacted",
    "context_pinned",
    "context_unpinned",
    "context_pin_expired",
})


def bridge_context_preservation_to_broker(
    *,
    ledger: Any = None,
    pin_registry: Any = None,
    manifest: Any = None,
    broker: Any = None,
) -> Callable[[], None]:
    """Attach listener → broker for ledger + pins + manifest.

    Returns a composite unsubscribe callback. When the SSE stream
    module is not importable, returns a no-op unsub and emits a
    debug log.
    """
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
            stream_enabled,
            _VALID_EVENT_TYPES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ContextBridge] stream import failed: %s", exc)
        return lambda: None

    bk = broker or get_default_broker()
    missing = _CONTEXT_EVENT_TYPES - set(_VALID_EVENT_TYPES)
    if missing:
        logger.warning(
            "[ContextBridge] broker missing event types: %s",
            sorted(missing),
        )

    def _maybe_publish(
        event_type: str, op_id: str, projection: Dict[str, Any],
    ) -> None:
        if event_type not in _CONTEXT_EVENT_TYPES:
            return
        if not stream_enabled():
            return
        try:
            bk.publish(event_type, op_id, projection)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ContextBridge] broker publish failed for %s: %s",
                event_type, exc,
            )

    unsubs: List[Callable[[], None]] = []

    if ledger is not None:
        def _on_ledger(payload: Dict[str, Any]) -> None:
            _maybe_publish(
                payload.get("event_type", ""),
                payload.get("op_id", ""),
                payload.get("projection") or {},
            )
        unsubs.append(ledger.on_change(_on_ledger))

    if pin_registry is not None:
        def _on_pin(payload: Dict[str, Any]) -> None:
            _maybe_publish(
                payload.get("event_type", ""),
                payload.get("op_id", ""),
                payload.get("projection") or {},
            )
        unsubs.append(pin_registry.on_change(_on_pin))

    if manifest is not None:
        def _on_manifest(payload: Dict[str, Any]) -> None:
            _maybe_publish(
                payload.get("event_type", ""),
                payload.get("op_id", ""),
                payload.get("projection") or {},
            )
        unsubs.append(manifest.on_change(_on_manifest))

    def _unsub_all() -> None:
        for u in unsubs:
            try:
                u()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "[ContextBridge] attached "
        "ledger=%s pins=%s manifest=%s",
        ledger is not None, pin_registry is not None, manifest is not None,
    )
    return _unsub_all


__all__ = [
    "CONTEXT_MANIFEST_SCHEMA_VERSION",
    "CONTEXT_OBSERVABILITY_SCHEMA_VERSION",
    "CompactionManifest",
    "CompactionManifestRegistry",
    "ContextObservabilityRouter",
    "ManifestRecord",
    "ManifestRow",
    "PreservationReason",
    "bridge_context_preservation_to_broker",
    "context_observability_enabled",
    "get_default_manifest_registry",
    "manifest_for",
    "project_record_full",
    "project_record_summary",
    "reason_for_keep",
    "reset_default_manifest_registry",
]
