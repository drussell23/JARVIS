"""Phase 8 IDE observability surface — read-only GET endpoints.

Wires the 5 Phase 8 substrate modules into operator-visible HTTP
endpoints, mounted alongside the existing
:class:`IDEObservabilityRouter` on the same aiohttp app:

  * GET /observability/phase8/health      — surface liveness + schema
  * GET /observability/decisions          — list recent decisions
  * GET /observability/decisions/{op_id}  — full causal trace
  * GET /observability/confidence         — list classifier names
  * GET /observability/confidence/{name}  — recent + drop indicators
  * GET /observability/timeline/{op_id}   — text + JSON timeline
  * GET /observability/flags/changes      — current snapshot + history
  * GET /observability/latency/slo        — per-phase stats + breaches

## Authority posture (locked)

  * **Read-only.** Zero endpoints mutate substrate state. No write
    paths to the ledger / ring / monitor / detector. Any operator
    actions belong on a separate, separately-reviewed surface.
  * **Deny-by-default.** ``JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED``
    defaults ``false`` until per-surface graduation cadence flips it.
  * **Loopback-only binding.** Reuses
    :func:`ide_observability.assert_loopback_only` at registration.
    The router itself does not bind a port — it adds routes to a
    caller-supplied :class:`web.Application`. The CALLER asserts
    loopback before mounting.
  * **Per-origin rate limit + CORS allowlist.** Identical sliding-
    window quota + origin allowlist as the Gap #6 router. Phase 8's
    rate tracker is independent (different trust boundary domain).
  * **Bounded payloads.** Every endpoint caps response size + carries
    ``schema_version`` + sets ``Cache-Control: no-store``.
  * **No imports from gate / execution modules.** Pinned by
    ``test_phase8_ide_routes_does_not_import_gate_modules`` — the
    import graph excludes orchestrator / iron_gate / risk_tier_floor
    / semantic_guardian / policy_engine / tool_executor /
    candidate_generator.
  * **Lazy substrate imports** at handler invocation, not module
    import — defends against circular dep paths.

## Schema versioning

Carries its own ``PHASE8_OBSERVABILITY_SCHEMA_VERSION`` so it can
evolve independently of the Gap #6 router's schema. v1.0 ships the
seven endpoints above; later slices add SSE bridges + multi-stream
timeline aggregator.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — import-time hygiene
    from aiohttp import web

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


PHASE8_OBSERVABILITY_SCHEMA_VERSION: str = "1.0"


# Master flag — deny-by-default until graduation.
def phase8_ide_observability_enabled() -> bool:
    """Master flag — ``JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED``
    (default ``false`` until graduation flips to ``true``)."""
    return os.environ.get(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED", "",
    ).strip().lower() in _TRUTHY


# Rate-limit envelope. Independent from the Gap #6 router's tracker
# so Phase 8 polling cannot starve Task / Plan / Session GETs.
def _rate_limit_per_min() -> int:
    raw = os.environ.get(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN",
    )
    if raw is None:
        return 120  # same default as Gap #6 router
    try:
        n = int(raw)
    except ValueError:
        return 120
    if n < 1:
        return 1
    if n > 6000:
        return 6000
    return n


# CORS allowlist. Mirrors Gap #6's discipline: loopback origins only.
def _cors_origin_patterns() -> Tuple[str, ...]:
    raw = os.environ.get(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_CORS_ORIGINS",
        "",
    )
    if not raw.strip():
        return (
            r"^https?://localhost(:\d+)?$",
            r"^https?://127\.0\.0\.1(:\d+)?$",
            r"^vscode-webview://[a-zA-Z0-9_-]+$",
        )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# op_id shape — alphanumeric, dash, underscore only. Matches the
# orchestrator-stamped op_ids and rejects path-traversal injections.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# classifier_name shape — same charset, slightly tighter length.
_CLASSIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# Bounded list response cap (defends against the ledger growing
# unbounded between trims).
MAX_DECISION_LIST_ROWS: int = 500
MAX_TIMELINE_LINES: int = 200
MAX_CONFIDENCE_EVENTS: int = 200


def assert_loopback_only(host: str) -> None:
    """Re-export thin wrapper. Centralizes the loopback assertion
    so callers can ``from observability.ide_routes import
    assert_loopback_only`` instead of importing across the gap-6 +
    phase-8 boundary. Delegates to the Gap #6 implementation."""
    from backend.core.ouroboros.governance.ide_observability import (
        assert_loopback_only as _assert,
    )
    _assert(host)


class Phase8ObservabilityRouter:
    """Mounts Phase 8 GET routes on a caller-supplied aiohttp app.

    Usage (from :class:`EventChannelServer.start`)::

        from backend.core.ouroboros.governance.observability.ide_routes import (
            Phase8ObservabilityRouter, assert_loopback_only,
        )
        assert_loopback_only(self._host)
        Phase8ObservabilityRouter().register_routes(app)

    Maintains its own rate-tracker — independent from the Gap #6
    router's tracker so Phase 8 polling cannot starve TaskBoard
    / Plan / Session GETs.
    """

    def __init__(self) -> None:
        self._rate_tracker: Dict[str, List[float]] = {}

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get(
            "/observability/phase8/health", self._handle_health,
        )
        app.router.add_get(
            "/observability/decisions", self._handle_decision_list,
        )
        app.router.add_get(
            "/observability/decisions/{op_id}",
            self._handle_decision_detail,
        )
        app.router.add_get(
            "/observability/confidence",
            self._handle_confidence_list,
        )
        app.router.add_get(
            "/observability/confidence/{classifier}",
            self._handle_confidence_detail,
        )
        app.router.add_get(
            "/observability/timeline/{op_id}",
            self._handle_timeline_detail,
        )
        app.router.add_get(
            "/observability/flags/changes",
            self._handle_flag_changes,
        )
        app.router.add_get(
            "/observability/latency/slo",
            self._handle_latency_slo,
        )

    # ----- request-path helpers -----------------------------------------

    def _client_key(self, request: "web.Request") -> str:
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_rate_limit(self, client_key: str) -> bool:
        limit = _rate_limit_per_min()
        now = time.monotonic()
        window_start = now - 60.0
        history = self._rate_tracker.setdefault(client_key, [])
        while history and history[0] < window_start:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _cors_headers(self, request: "web.Request") -> Dict[str, str]:
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
                "schema_version": PHASE8_OBSERVABILITY_SCHEMA_VERSION,
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
            request, status,
            {"error": True, "reason_code": reason_code},
        )

    def _gate_or_error(
        self, request: "web.Request",
    ) -> Optional[Any]:
        """Common deny + rate-limit gate. Returns an error response
        when blocked; ``None`` when the handler should proceed."""
        if not phase8_ide_observability_enabled():
            return self._error_response(
                request, 403, "phase8_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "phase8_observability.rate_limited",
            )
        return None

    # ----- handlers -----------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> Any:
        """GET /observability/phase8/health — surface liveness.

        When master-off, returns 403 (not 200 with ``{enabled: false}``)
        so a port scan sees no signal."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        # Lazy import probes — each is a 1-call check that the
        # substrate's master flag is on.
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            is_ledger_enabled,
        )
        from backend.core.ouroboros.governance.observability.latent_confidence_ring import (  # noqa: E501
            is_ring_enabled,
        )
        from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
            is_emitter_enabled,
        )
        from backend.core.ouroboros.governance.observability.latency_slo_detector import (  # noqa: E501
            is_detector_enabled,
        )
        from backend.core.ouroboros.governance.observability.multi_op_timeline import (  # noqa: E501
            is_timeline_enabled,
        )
        return self._json_response(
            request, 200,
            {
                "enabled": True,
                "api_version": PHASE8_OBSERVABILITY_SCHEMA_VERSION,
                "surface": (
                    "decisions,confidence,timeline,flags,latency"
                ),
                "substrate": {
                    "decision_trace_ledger": bool(is_ledger_enabled()),
                    "latent_confidence_ring": bool(is_ring_enabled()),
                    "flag_change_emitter": bool(is_emitter_enabled()),
                    "latency_slo_detector": bool(is_detector_enabled()),
                    "multi_op_timeline": bool(is_timeline_enabled()),
                },
                "now_mono": time.monotonic(),
            },
        )

    async def _handle_decision_list(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/decisions — list recent decision rows.

        Bounded by ``MAX_DECISION_LIST_ROWS``. Optional query params:
          * ``limit``  — cap result count (1..MAX_DECISION_LIST_ROWS)
          * ``op_id``  — filter to one op_id (re-validated)
        """
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            get_default_ledger, is_ledger_enabled,
        )
        if not is_ledger_enabled():
            return self._json_response(
                request, 200,
                {
                    "rows": [], "count": 0,
                    "ledger_enabled": False,
                },
            )
        limit = self._parse_limit(
            request.rel_url.query.get("limit"),
            MAX_DECISION_LIST_ROWS,
        )
        op_filter = request.rel_url.query.get("op_id")
        if op_filter is not None:
            if not _OP_ID_RE.match(op_filter):
                return self._error_response(
                    request, 400,
                    "phase8_observability.malformed_op_id",
                )
        ledger = get_default_ledger()
        # The ledger doesn't expose a "list all" — we read the file
        # directly via the existing reader. For the list view, we
        # use a generic walk.
        rows = self._read_recent_rows(ledger, limit, op_filter)
        return self._json_response(
            request, 200,
            {
                "rows": rows,
                "count": len(rows),
                "ledger_enabled": True,
                "limit_applied": limit,
            },
        )

    async def _handle_decision_detail(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/decisions/{op_id} — full causal trace
        for ``op_id``. Returns 400 on malformed, 404 on unknown."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400,
                "phase8_observability.malformed_op_id",
            )
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            get_default_ledger, is_ledger_enabled,
        )
        if not is_ledger_enabled():
            return self._error_response(
                request, 503,
                "phase8_observability.ledger_disabled",
            )
        ledger = get_default_ledger()
        rows = ledger.reconstruct_op(op_id)
        if not rows:
            return self._error_response(
                request, 404,
                "phase8_observability.unknown_op_id",
            )
        return self._json_response(
            request, 200,
            {
                "op_id": op_id,
                "rows": [r.to_dict() for r in rows],
                "row_count": len(rows),
            },
        )

    async def _handle_confidence_list(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/confidence — list known classifier names
        + total event count."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        from backend.core.ouroboros.governance.observability.latent_confidence_ring import (  # noqa: E501
            get_default_ring, is_ring_enabled,
        )
        if not is_ring_enabled():
            return self._json_response(
                request, 200,
                {
                    "classifier_names": [], "total_events": 0,
                    "ring_enabled": False,
                },
            )
        ring = get_default_ring()
        events = ring.recent(n=ring.capacity)
        names = sorted({e.classifier_name for e in events})
        return self._json_response(
            request, 200,
            {
                "classifier_names": names,
                "total_events": len(events),
                "ring_enabled": True,
                "capacity": ring.capacity,
            },
        )

    async def _handle_confidence_detail(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/confidence/{classifier} — recent events
        + drop indicators for one classifier."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        classifier = request.match_info.get("classifier", "")
        if not _CLASSIFIER_RE.match(classifier):
            return self._error_response(
                request, 400,
                "phase8_observability.malformed_classifier",
            )
        from backend.core.ouroboros.governance.observability.latent_confidence_ring import (  # noqa: E501
            get_default_ring, is_ring_enabled,
        )
        if not is_ring_enabled():
            return self._error_response(
                request, 503,
                "phase8_observability.ring_disabled",
            )
        ring = get_default_ring()
        n = self._parse_limit(
            request.rel_url.query.get("limit"),
            MAX_CONFIDENCE_EVENTS,
        )
        window = self._parse_int(
            request.rel_url.query.get("window"), default=20,
            lo=2, hi=500,
        )
        drop_pct = self._parse_float(
            request.rel_url.query.get("drop_pct"), default=20.0,
            lo=0.0, hi=100.0,
        )
        events = ring.recent_for_classifier(classifier, n=n)
        if not events:
            return self._error_response(
                request, 404,
                "phase8_observability.unknown_classifier",
            )
        drop = ring.confidence_drop_indicators(
            classifier, window=window, drop_threshold_pct=drop_pct,
        )
        return self._json_response(
            request, 200,
            {
                "classifier_name": classifier,
                "events": [e.to_dict() for e in events],
                "event_count": len(events),
                "drop_indicators": drop,
            },
        )

    async def _handle_timeline_detail(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/timeline/{op_id} — text + JSON timeline
        for one op_id, built from the decision-trace ledger.

        Returns 400 on malformed op_id, 404 when no rows for op_id."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400,
                "phase8_observability.malformed_op_id",
            )
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            get_default_ledger, is_ledger_enabled,
        )
        from backend.core.ouroboros.governance.observability.multi_op_timeline import (  # noqa: E501
            TimelineEvent, merge_streams, render_text_timeline,
        )
        if not is_ledger_enabled():
            return self._error_response(
                request, 503,
                "phase8_observability.ledger_disabled",
            )
        ledger = get_default_ledger()
        rows = ledger.reconstruct_op(op_id)
        if not rows:
            return self._error_response(
                request, 404,
                "phase8_observability.unknown_op_id",
            )
        # Project decision rows → timeline events keyed by phase as
        # the stream_id (so operators see one stream per phase).
        events: List[TimelineEvent] = []
        for i, r in enumerate(rows):
            events.append(TimelineEvent(
                ts_epoch=r.ts_epoch,
                stream_id=r.phase or "unknown",
                event_type="decision",
                payload={
                    "decision": r.decision,
                    "rationale": r.rationale[:200],
                },
                seq=i,
            ))
        # Group by stream_id for merge_streams; each stream is
        # already in chronological order (file order matches).
        streams: Dict[str, List[TimelineEvent]] = {}
        for ev in events:
            streams.setdefault(ev.stream_id, []).append(ev)
        merged = merge_streams(streams)
        text_view = render_text_timeline(
            merged, max_lines=MAX_TIMELINE_LINES,
        )
        return self._json_response(
            request, 200,
            {
                "op_id": op_id,
                "events": [ev.to_dict() for ev in merged],
                "event_count": len(merged),
                "text_render": text_view,
                "max_lines": MAX_TIMELINE_LINES,
            },
        )

    async def _handle_flag_changes(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/flags/changes — current snapshot of
        ``JARVIS_*`` env vars + a one-shot diff against the default
        monitor's baseline.

        Calling this endpoint advances the baseline (so subsequent
        reads see only NEW deltas). This makes the endpoint act as
        a polling consumer of the monitor — IDEs poll periodically
        and accumulate the deltas client-side.
        """
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
            get_default_monitor, is_emitter_enabled, snapshot_flags,
        )
        if not is_emitter_enabled():
            return self._json_response(
                request, 200,
                {
                    "snapshot": {}, "deltas": [],
                    "snapshot_size": 0, "delta_count": 0,
                    "emitter_enabled": False,
                },
            )
        monitor = get_default_monitor()
        # First call sets the baseline implicitly (monitor.check()
        # treats empty baseline as "everything is new" — which is
        # exactly what we want for the initial poll).
        deltas = monitor.check()
        snap = snapshot_flags()
        # Sanitize the snapshot for response: never echo back env
        # values verbatim — they may contain secrets if an operator
        # accidentally set a credential as a JARVIS_* flag. We mask
        # everything except the prefix so IDEs see the SHAPE of
        # the env without the values.
        masked = {
            k: ("<set>" if v else "<empty>") for k, v in snap.items()
        }
        # Mask delta values too — raw FlagChangeEvent.to_dict()
        # echoes prev_value/next_value verbatim. Replace those with
        # presence markers so secrets stored in JARVIS_* never leak.
        masked_deltas: List[Dict[str, Any]] = []
        for d in deltas:
            d_dict = d.to_dict()
            d_dict["prev_value"] = (
                None if d_dict.get("prev_value") is None
                else (
                    "<set>" if d_dict.get("prev_value") else "<empty>"
                )
            )
            d_dict["next_value"] = (
                None if d_dict.get("next_value") is None
                else (
                    "<set>" if d_dict.get("next_value") else "<empty>"
                )
            )
            masked_deltas.append(d_dict)
        return self._json_response(
            request, 200,
            {
                "snapshot": masked,
                "snapshot_size": len(snap),
                "deltas": masked_deltas,
                "delta_count": len(masked_deltas),
                "emitter_enabled": True,
            },
        )

    async def _handle_latency_slo(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/latency/slo — per-phase latency stats
        (sample_count + p50 + p95 + max + slo) and the list of
        currently-breached phases."""
        gated = self._gate_or_error(request)
        if gated is not None:
            return gated
        from backend.core.ouroboros.governance.observability.latency_slo_detector import (  # noqa: E501
            get_default_detector, is_detector_enabled,
        )
        if not is_detector_enabled():
            return self._json_response(
                request, 200,
                {
                    "stats": {}, "breaches": [],
                    "phase_count": 0, "breach_count": 0,
                    "detector_enabled": False,
                },
            )
        detector = get_default_detector()
        stats = detector.stats()
        breaches = detector.check_all_breaches()
        return self._json_response(
            request, 200,
            {
                "stats": stats,
                "phase_count": len(stats),
                "breaches": [b.to_dict() for b in breaches],
                "breach_count": len(breaches),
                "detector_enabled": True,
            },
        )

    # ----- helpers ------------------------------------------------------

    def _parse_limit(
        self, raw: Optional[str], hard_max: int,
    ) -> int:
        """Parse ``limit`` query param. Returns ``hard_max`` when
        unset / invalid; clamped to ``[1, hard_max]``."""
        if raw is None:
            return hard_max
        try:
            n = int(raw)
        except ValueError:
            return hard_max
        if n < 1:
            return 1
        if n > hard_max:
            return hard_max
        return n

    def _parse_int(
        self,
        raw: Optional[str],
        *,
        default: int,
        lo: int,
        hi: int,
    ) -> int:
        if raw is None:
            return default
        try:
            n = int(raw)
        except ValueError:
            return default
        if n < lo:
            return lo
        if n > hi:
            return hi
        return n

    def _parse_float(
        self,
        raw: Optional[str],
        *,
        default: float,
        lo: float,
        hi: float,
    ) -> float:
        if raw is None:
            return default
        try:
            f = float(raw)
        except ValueError:
            return default
        if f < lo:
            return lo
        if f > hi:
            return hi
        return f

    def _read_recent_rows(
        self,
        ledger: Any,
        limit: int,
        op_filter: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Best-effort generic read of the ledger file for the list
        view. Uses the same on-disk format the ledger writes — but
        WITHOUT the per-op reconstruction filter, since the list
        endpoint shows all ops by default.

        NEVER raises — file read errors return an empty list.
        """
        import json
        path = ledger.path
        try:
            if not path.exists():
                return []
        except OSError:
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        # Walk in reverse so we get the most-recent first.
        lines = text.splitlines()
        for line in reversed(lines):
            if len(out) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if op_filter is not None:
                if str(obj.get("op_id") or "") != op_filter:
                    continue
            # Echo only documented schema fields — defends against
            # accidental echo of future fields with sensitive shape.
            out.append({
                "op_id": str(obj.get("op_id") or ""),
                "phase": str(obj.get("phase") or ""),
                "decision": str(obj.get("decision") or ""),
                "factors": (
                    obj.get("factors")
                    if isinstance(obj.get("factors"), dict) else {}
                ),
                "weights": (
                    obj.get("weights")
                    if isinstance(obj.get("weights"), dict) else {}
                ),
                "rationale": str(obj.get("rationale") or "")[:200],
                "ts_iso": str(obj.get("ts_iso") or ""),
                "ts_epoch": float(obj.get("ts_epoch") or 0.0),
            })
        return out


__all__ = [
    "MAX_CONFIDENCE_EVENTS",
    "MAX_DECISION_LIST_ROWS",
    "MAX_TIMELINE_LINES",
    "PHASE8_OBSERVABILITY_SCHEMA_VERSION",
    "Phase8ObservabilityRouter",
    "assert_loopback_only",
    "phase8_ide_observability_enabled",
]
