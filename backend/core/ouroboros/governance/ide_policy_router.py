"""Gap #2 Slice 4 — IDE policy router (write authority surface).

NEW write-side companion to ``ide_observability.py`` (which is
AST-pinned read-only via ``test_ide_observability.py``). Lives in a
separate module BY DESIGN: the read surface's authority invariant
is structurally protected by its module-level import allowlist; a
write surface in the same file would weaken that pin. Two modules,
two pins, one cage.

## Surfaces

  * ``POST /policy/confidence/proposals`` — operator submits a
    proposed ``ConfidencePolicy`` delta. Body shape (JSON)::

        {
          "current":    {"floor": 0.05, "window_k": 16, ...},
          "proposed":   {"floor": 0.10, "window_k": 16, ...},
          "evidence_summary": "floor 0.05 → 0.10; observed N events",
          "observation_count": 5,
          "operator":   "alice",
          "proposal_id": "conf-...."   // optional; auto-generated if omitted
        }

    The router runs ``compute_policy_diff`` (Slice 1), classifies
    the kind via ``classify_proposal_kind`` (Slice 2), composes the
    payload via ``build_proposed_state_payload`` (Slice 2), and
    submits to ``AdaptationLedger.propose``. The substrate's
    surface validator (Slice 2) + universal cage rule run inside
    propose. On OK → emits ``confidence_policy_proposed`` SSE.

  * ``POST /policy/confidence/proposals/{proposal_id}/approve`` —
    operator approves a pending proposal. Body: ``{"operator":
    "..."}``. Calls ``AdaptationLedger.approve``. Emits
    ``confidence_policy_approved`` SSE on OK.

  * ``POST /policy/confidence/proposals/{proposal_id}/reject`` —
    operator rejects. Body: ``{"operator": "...", "reason":
    "..."}``. Calls ``AdaptationLedger.reject``. Emits
    ``confidence_policy_rejected`` SSE on OK.

  * ``GET /policy/confidence`` — current effective policy
    snapshot + adapted-YAML metadata + recent proposal counts.
    Read-only summary; the propose/approve/reject paths are the
    only write surfaces.

## Authority discipline

  * **Loopback-only bind** — reuses ``assert_loopback_only`` from
    ``ide_observability`` (one-way utility import; sibling-router
    helper, not a protocol layering violation).
  * **Per-IP rate limit** — separate sliding-window state from
    the read surface. Different trust boundary (write is more
    consequential than read).
  * **Bounded body** — POST body capped at 16 KiB. A proposal
    payload is structurally tiny (<1 KiB); the cap is a defensive
    upper bound against memory exhaustion.
  * **CORS** — same allowlist as ``ide_observability`` (localhost
    + vscode-webview); no wildcard credentials.
  * **AST-pinned import allowlist** (Slice 5 will harden this in
    ``shipped_code_invariants``):
      - ``adaptation.ledger`` (substrate writes)
      - ``verification.confidence_policy`` (Slice 1 substrate)
      - ``adaptation.confidence_threshold_tightener`` (Slice 2 helpers)
      - ``adaptation.adapted_confidence_loader`` (Slice 3 reads)
      - ``verification.confidence_monitor`` (current-state reads)
      - ``ide_observability_stream`` (publish only)
      - ``ide_observability`` (assert_loopback_only ONLY)
    MUST NOT import: orchestrator, iron_gate, policy_engine,
    risk_engine, change_engine, tool_executor, providers,
    candidate_generator, semantic_guardian, semantic_firewall,
    scoped_tool_backend, subagent_scheduler.

## Default-off

``JARVIS_IDE_POLICY_ROUTER_ENABLED`` (default ``false`` until
Slice 5 graduation). When off, every route returns ``403`` with
reason_code ``ide_policy_router.disabled`` — port scanners see no
signal about what's behind the listener.

## What ships in Slice 4

Three write paths (proposed / approved / rejected) + one read
snapshot. The fourth event (``confidence_policy_applied``) is
defined here but emitted by Slice 5's YAML writer hook — keeps
the router free of YAML I/O (writer is a separate concern,
mirrors Pass C's `/adapt approve` → `yaml_writer` split).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple, TYPE_CHECKING

from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (
    is_loader_enabled as adapted_loader_enabled,
    load_adapted_thresholds,
)
from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (
    build_proposed_state_payload,
    classify_proposal_kind,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    DecisionStatus,
    OperatorDecisionStatus,
    ProposeStatus,
    get_default_ledger,
)
from backend.core.ouroboros.governance.ide_observability import (
    assert_loopback_only,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CONFIDENCE_POLICY_APPLIED,
    EVENT_TYPE_CONFIDENCE_POLICY_APPROVED,
    EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED,
    EVENT_TYPE_CONFIDENCE_POLICY_REJECTED,
    StreamEventBroker,
    get_default_broker,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    confidence_approaching_factor,
    confidence_floor,
    confidence_monitor_enforce,
    confidence_window_k,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    ConfidencePolicy,
    ConfidencePolicyOutcome,
    compute_policy_diff,
    confidence_policy_enabled,
)

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger(__name__)


IDE_POLICY_ROUTER_SCHEMA_VERSION: str = "ide_policy_router.1"


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------

_TRUTHY = ("1", "true", "yes", "on")


def ide_policy_router_enabled() -> bool:
    """``JARVIS_IDE_POLICY_ROUTER_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in Gap #2 Slice 5). Matches the
    discipline of ``JARVIS_IDE_OBSERVABILITY_ENABLED`` (the read
    surface): the write surface is structurally safe by
    construction (loopback-only bind + per-IP rate limit + cage
    validator + monotonic-tightening universal rule + bounded
    body), so default-true is the correct posture for an IDE-
    facing policy panel. Operator hot-reverts via explicit
    ``=false``; on revert every route returns ``403``.

    NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "",
        ).strip().lower()
        if raw == "":
            return True  # graduated 2026-05-02
        return raw in _TRUTHY
    except Exception:  # noqa: BLE001 — defensive
        return False


def _rate_limit_per_min() -> int:
    """Max writes / minute / client key. Default 30 — operator
    proposals are deliberate; 30/min is generous against an IDE
    flake while protecting against storms. Lower than the read
    surface's 120/min by design (writes are more consequential)."""
    try:
        return max(
            1, int(os.environ.get(
                "JARVIS_IDE_POLICY_ROUTER_RATE_LIMIT_PER_MIN", "30",
            )),
        )
    except (TypeError, ValueError):
        return 30


def _max_body_bytes() -> int:
    """Hard cap on POST body size. Default 16 KiB — proposal
    payload is <1 KiB structurally; cap is defensive."""
    try:
        return max(
            1024, int(os.environ.get(
                "JARVIS_IDE_POLICY_ROUTER_MAX_BODY_BYTES",
                str(16 * 1024),
            )),
        )
    except (TypeError, ValueError):
        return 16 * 1024


def _cors_origin_patterns() -> Tuple[str, ...]:
    """Same default allowlist as the read surface — operators run
    the IDE on localhost (or VS Code webview) by design."""
    raw = os.environ.get(
        "JARVIS_IDE_POLICY_ROUTER_CORS_ORIGINS",
        r"^https?://localhost(:\d+)?$,"
        r"^https?://127\.0\.0\.1(:\d+)?$,"
        r"^vscode-webview://[a-z0-9-]+$",
    )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# ---------------------------------------------------------------------------
# URL parameter validation
# ---------------------------------------------------------------------------


_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


def _is_valid_proposal_id(s: str) -> bool:
    if not isinstance(s, str):
        return False
    return bool(_PROPOSAL_ID_RE.fullmatch(s))


# ---------------------------------------------------------------------------
# Router class
# ---------------------------------------------------------------------------


class IDEPolicyRouter:
    """Mounts the POST + GET ``/policy/confidence/*`` routes on a
    caller-supplied aiohttp :class:`Application`.

    Construction is best-effort: no I/O, no validation other than
    the loopback-bind assertion. Call ``register_routes(app)`` to
    install handlers.

    Maintains its own rate-tracker state — separate from the read
    surface's tracker because write trust boundary differs from
    read.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        *,
        ledger: Optional[AdaptationLedger] = None,
        broker: Optional[StreamEventBroker] = None,
    ) -> None:
        # The loopback assertion mirrors the read surface — a write
        # router exposed to the network would let any LAN client
        # mutate the cage. Refuse at construction time.
        assert_loopback_only(host)
        self._host = host
        self._rate_tracker: Dict[str, List[float]] = {}
        self._ledger = ledger
        self._broker = broker

    def register_routes(self, app: "web.Application") -> None:
        """Mount the four routes. Idempotent at the aiohttp layer
        only insofar as the caller hasn't already mounted them
        (aiohttp itself raises on duplicate routes)."""
        app.router.add_post(
            "/policy/confidence/proposals",
            self._handle_propose,
        )
        app.router.add_post(
            "/policy/confidence/proposals/{proposal_id}/approve",
            self._handle_approve,
        )
        app.router.add_post(
            "/policy/confidence/proposals/{proposal_id}/reject",
            self._handle_reject,
        )
        app.router.add_get(
            "/policy/confidence",
            self._handle_snapshot,
        )

    # --- helpers --------------------------------------------------------

    def _get_ledger(self) -> AdaptationLedger:
        if self._ledger is not None:
            return self._ledger
        return get_default_ledger()

    def _get_broker(self) -> StreamEventBroker:
        if self._broker is not None:
            return self._broker
        return get_default_broker()

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

    def _cors_headers(
        self, request: "web.Request",
    ) -> Dict[str, str]:
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        "Access-Control-Allow-Methods": (
                            "GET, POST, OPTIONS"
                        ),
                        "Access-Control-Allow-Headers": (
                            "Content-Type"
                        ),
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
                "schema_version": IDE_POLICY_ROUTER_SCHEMA_VERSION,
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
        detail: str = "",
    ) -> Any:
        return self._json_response(
            request, status,
            {
                "error": True,
                "reason_code": reason_code,
                "detail": detail[:240],
            },
        )

    async def _read_bounded_body(
        self, request: "web.Request",
    ) -> Optional[bytes]:
        """Read the body with a hard cap. Returns ``None`` if the
        body exceeds ``_max_body_bytes()``. NEVER raises."""
        try:
            cap = _max_body_bytes()
            body = await request.read()
            if not isinstance(body, (bytes, bytearray)):
                return None
            if len(body) > cap:
                return None
            return bytes(body)
        except Exception:  # noqa: BLE001 — defensive
            return None

    def _emit_sse(
        self,
        event_type: str,
        proposal_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Best-effort SSE publish. NEVER raises."""
        try:
            broker = self._get_broker()
            broker.publish(event_type, proposal_id, payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEPolicyRouter] SSE publish failed: %s", exc,
            )

    def _gate_check(
        self, request: "web.Request",
    ) -> Optional[Any]:
        """Run the master flag + rate-limit gate. Returns an
        error response if the request must be rejected, else
        ``None`` (caller proceeds)."""
        if not ide_policy_router_enabled():
            return self._error_response(
                request, 403, "ide_policy_router.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_policy_router.rate_limited",
            )
        return None

    # --- handlers -------------------------------------------------------

    async def _handle_propose(
        self, request: "web.Request",
    ) -> Any:
        """POST /policy/confidence/proposals — submit a tightening."""
        gate = self._gate_check(request)
        if gate is not None:
            return gate

        body = await self._read_bounded_body(request)
        if body is None:
            return self._error_response(
                request, 413,
                "ide_policy_router.payload_too_large",
            )
        try:
            doc = json.loads(body)
        except json.JSONDecodeError as exc:
            return self._error_response(
                request, 400,
                "ide_policy_router.invalid_json",
                str(exc)[:120],
            )
        if not isinstance(doc, dict):
            return self._error_response(
                request, 400,
                "ide_policy_router.body_not_object",
            )

        # --- Field extraction + validation ---
        operator = str(doc.get("operator") or "").strip()
        if not operator:
            return self._error_response(
                request, 400,
                "ide_policy_router.operator_required",
            )

        current_raw = doc.get("current")
        proposed_raw = doc.get("proposed")
        if not isinstance(current_raw, dict):
            return self._error_response(
                request, 400,
                "ide_policy_router.current_not_object",
            )
        if not isinstance(proposed_raw, dict):
            return self._error_response(
                request, 400,
                "ide_policy_router.proposed_not_object",
            )

        evidence_summary = str(
            doc.get("evidence_summary") or "",
        ).strip()
        if not evidence_summary:
            return self._error_response(
                request, 400,
                "ide_policy_router.evidence_summary_required",
            )

        try:
            observation_count = int(doc.get("observation_count") or 0)
        except (TypeError, ValueError):
            return self._error_response(
                request, 400,
                "ide_policy_router.observation_count_not_int",
            )
        if observation_count < 1:
            return self._error_response(
                request, 400,
                "ide_policy_router.observation_count_below_one",
            )

        # --- Build policy snapshots ---
        try:
            current = ConfidencePolicy.from_dict(current_raw)
            proposed = ConfidencePolicy.from_dict(proposed_raw)
        except Exception as exc:  # noqa: BLE001 — defensive
            return self._error_response(
                request, 400,
                "ide_policy_router.policy_construction_failed",
                f"{type(exc).__name__}",
            )

        # --- Run substrate decision (Slice 1 predicate) ---
        # Force-enable the master flag here: the policy substrate's
        # master gates a different concern (whether the diff
        # function should run AT ALL — relevant for unit tests).
        # The HTTP router's master is JARVIS_IDE_POLICY_ROUTER_ENABLED;
        # if THAT is on, the substrate predicate must run regardless
        # of its own enabled() result.
        diff = compute_policy_diff(
            current=current, proposed=proposed,
            enabled_override=True,
        )
        if diff.outcome is ConfidencePolicyOutcome.INVALID:
            return self._error_response(
                request, 400,
                "ide_policy_router.policy_invalid",
                diff.detail,
            )
        if diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN:
            return self._error_response(
                request, 400,
                "ide_policy_router.policy_would_loosen",
                diff.detail,
            )
        if diff.outcome is ConfidencePolicyOutcome.FAILED:
            return self._error_response(
                request, 500,
                "ide_policy_router.policy_diff_failed",
                diff.detail,
            )
        if not diff.kinds:
            return self._error_response(
                request, 400,
                "ide_policy_router.no_op_proposal_rejected",
            )

        kind = classify_proposal_kind(diff)
        if kind is None:
            return self._error_response(
                request, 400,
                "ide_policy_router.kind_classification_failed",
            )

        # --- Compose AdaptationLedger inputs ---
        proposal_id_raw = str(doc.get("proposal_id") or "").strip()
        if proposal_id_raw and not _is_valid_proposal_id(
            proposal_id_raw,
        ):
            return self._error_response(
                request, 400,
                "ide_policy_router.proposal_id_malformed",
            )
        proposal_id = (
            proposal_id_raw
            or f"conf-{uuid.uuid4().hex[:16]}"
        )

        payload = build_proposed_state_payload(
            current=current, proposed=proposed,
        )
        evidence = AdaptationEvidence(
            window_days=1,
            observation_count=observation_count,
            summary=evidence_summary[:512],
        )

        ledger = self._get_ledger()
        try:
            result = ledger.propose(
                proposal_id=proposal_id,
                surface=AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
                proposal_kind=kind,
                evidence=evidence,
                current_state_hash=current.state_hash(),
                proposed_state_hash=proposed.state_hash(),
                proposed_state_payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEPolicyRouter] ledger.propose raised: %s", exc,
            )
            return self._error_response(
                request, 500,
                "ide_policy_router.ledger_propose_raised",
                f"{type(exc).__name__}",
            )

        if result.status is not ProposeStatus.OK:
            return self._error_response(
                request, 400,
                f"ide_policy_router.propose_{result.status.value.lower()}",
                result.detail,
            )

        # SSE publish (best-effort)
        self._emit_sse(
            EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED,
            proposal_id,
            {
                "operator": operator,
                "kind": kind,
                "moved_dimensions": [k.value for k in diff.kinds],
                "evidence_summary": evidence_summary[:240],
                "current_hash": current.state_hash(),
                "proposed_hash": proposed.state_hash(),
            },
        )

        return self._json_response(
            request, 201,
            {
                "ok": True,
                "proposal_id": proposal_id,
                "kind": kind,
                "moved_dimensions": [k.value for k in diff.kinds],
                "current_state_hash": current.state_hash(),
                "proposed_state_hash": proposed.state_hash(),
                "monotonic_tightening_verdict": (
                    diff.monotonic_tightening_verdict
                ),
            },
        )

    async def _handle_approve(
        self, request: "web.Request",
    ) -> Any:
        """POST /policy/confidence/proposals/{proposal_id}/approve."""
        gate = self._gate_check(request)
        if gate is not None:
            return gate
        return await self._handle_decision(
            request, target=OperatorDecisionStatus.APPROVED,
            event_type=EVENT_TYPE_CONFIDENCE_POLICY_APPROVED,
        )

    async def _handle_reject(
        self, request: "web.Request",
    ) -> Any:
        """POST /policy/confidence/proposals/{proposal_id}/reject."""
        gate = self._gate_check(request)
        if gate is not None:
            return gate
        return await self._handle_decision(
            request, target=OperatorDecisionStatus.REJECTED,
            event_type=EVENT_TYPE_CONFIDENCE_POLICY_REJECTED,
        )

    async def _handle_decision(
        self,
        request: "web.Request",
        *,
        target: OperatorDecisionStatus,
        event_type: str,
    ) -> Any:
        """Shared body for approve + reject. Both wrap the same
        ledger call shape; the difference is which method we
        dispatch to + which SSE event_type fires."""
        proposal_id = request.match_info.get("proposal_id", "")
        if not _is_valid_proposal_id(proposal_id):
            return self._error_response(
                request, 400,
                "ide_policy_router.proposal_id_malformed",
            )

        body = await self._read_bounded_body(request)
        if body is None:
            return self._error_response(
                request, 413,
                "ide_policy_router.payload_too_large",
            )
        try:
            doc = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            return self._error_response(
                request, 400,
                "ide_policy_router.invalid_json",
                str(exc)[:120],
            )
        if not isinstance(doc, dict):
            return self._error_response(
                request, 400,
                "ide_policy_router.body_not_object",
            )

        operator = str(doc.get("operator") or "").strip()
        if not operator:
            return self._error_response(
                request, 400,
                "ide_policy_router.operator_required",
            )

        ledger = self._get_ledger()
        try:
            if target is OperatorDecisionStatus.APPROVED:
                decision = ledger.approve(
                    proposal_id, operator=operator,
                )
            else:
                decision = ledger.reject(
                    proposal_id, operator=operator,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEPolicyRouter] ledger decision raised: %s",
                exc,
            )
            return self._error_response(
                request, 500,
                "ide_policy_router.ledger_decision_raised",
                f"{type(exc).__name__}",
            )

        if decision.status is not DecisionStatus.OK:
            # Map ledger status to HTTP code: NOT_FOUND→404,
            # NOT_PENDING→409, OPERATOR_REQUIRED→400, others→400.
            http_status = {
                DecisionStatus.NOT_FOUND: 404,
                DecisionStatus.NOT_PENDING: 409,
                DecisionStatus.OPERATOR_REQUIRED: 400,
                DecisionStatus.DISABLED: 403,
                DecisionStatus.PERSIST_ERROR: 500,
            }.get(decision.status, 400)
            return self._error_response(
                request, http_status,
                f"ide_policy_router.decision_"
                f"{decision.status.value.lower()}",
                decision.detail,
            )

        reason = str(doc.get("reason") or "").strip()[:240]
        self._emit_sse(
            event_type,
            proposal_id,
            {
                "operator": operator,
                "target_status": target.value,
                "reason": reason,
            },
        )

        # Slice 5 cage close: on APPROVED, materialize the
        # tightening into the live loader's adapted YAML and emit
        # confidence_policy_applied. Best-effort — the ledger
        # transition is the source of truth; the YAML write is
        # the activation step. A YAML write failure does NOT
        # invalidate the approval (the operator has a
        # decision-recorded approval; replay can re-materialize).
        applied_payload: Dict[str, Any] = {
            "operator": operator,
            "yaml_path": "",
            "write_status": "skipped",
            "write_detail": "",
        }
        if target is OperatorDecisionStatus.APPROVED:
            try:
                proposal = self._lookup_approved_proposal(
                    ledger, proposal_id,
                )
                if proposal is not None:
                    write_result = self._materialize_to_yaml(proposal)
                    applied_payload = {
                        "operator": operator,
                        "yaml_path": write_result.yaml_path or "",
                        "write_status": write_result.status.value,
                        "write_detail": write_result.detail[:240],
                    }
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[IDEPolicyRouter] materialize raised: %s",
                    exc,
                )
                applied_payload["write_status"] = "raised"
                applied_payload["write_detail"] = (
                    f"{type(exc).__name__}"
                )

            self._emit_sse(
                EVENT_TYPE_CONFIDENCE_POLICY_APPLIED,
                proposal_id,
                applied_payload,
            )

        return self._json_response(
            request, 200,
            {
                "ok": True,
                "proposal_id": proposal_id,
                "operator_decision": target.value,
                "operator": operator,
                "applied": (
                    applied_payload
                    if target is OperatorDecisionStatus.APPROVED
                    else None
                ),
            },
        )

    def _lookup_approved_proposal(
        self, ledger: AdaptationLedger, proposal_id: str,
    ) -> Any:
        """Find the freshly-approved proposal in the ledger so the
        YAML writer can materialize its payload. Best-effort —
        returns ``None`` on any failure."""
        try:
            history = ledger.history(
                surface=AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
                limit=200,
            )
            for p in history:
                if p.proposal_id == proposal_id and (
                    p.operator_decision
                    is OperatorDecisionStatus.APPROVED
                ):
                    return p
        except Exception:  # noqa: BLE001 — defensive
            return None
        return None

    def _materialize_to_yaml(self, proposal: Any) -> Any:
        """Lazy-import the writer so the router stays free of
        YAML I/O at module-import time. Best-effort dispatch into
        ``adaptation.yaml_writer.write_confidence_proposal_to_yaml``.

        NEVER raises — the writer itself returns a structured
        WriteResult on every code path."""
        from backend.core.ouroboros.governance.adaptation.yaml_writer import (  # noqa: E501
            write_confidence_proposal_to_yaml,
        )
        return write_confidence_proposal_to_yaml(proposal)

    async def _handle_snapshot(
        self, request: "web.Request",
    ) -> Any:
        """GET /policy/confidence — current effective state +
        adapted-YAML metadata + recent proposal counts.

        Read-only summary. The propose/approve/reject paths are
        the only write surfaces — this endpoint merely *projects*
        the state for IDE rendering."""
        gate = self._gate_check(request)
        if gate is not None:
            return gate

        # Current effective policy — same accessors the runtime
        # ConfidenceMonitor reads. Defensive: any accessor failure
        # falls through to None so the response shape stays stable.
        try:
            current_effective = {
                "floor": confidence_floor(),
                "window_k": confidence_window_k(),
                "approaching_factor": (
                    confidence_approaching_factor()
                ),
                "enforce": confidence_monitor_enforce(),
            }
        except Exception:  # noqa: BLE001 — defensive
            current_effective = {
                "floor": None, "window_k": None,
                "approaching_factor": None, "enforce": None,
            }

        # Adapted YAML metadata (Slice 3 loader)
        try:
            adapted = load_adapted_thresholds()
            adapted_block = {
                "loader_enabled": adapted_loader_enabled(),
                "in_effect": not adapted.is_empty(),
                "values": {
                    "floor": adapted.floor,
                    "window_k": adapted.window_k,
                    "approaching_factor": adapted.approaching_factor,
                    "enforce": adapted.enforce,
                },
                "proposal_id": adapted.proposal_id,
                "approved_at": adapted.approved_at,
                "approved_by": adapted.approved_by,
            }
        except Exception:  # noqa: BLE001 — defensive
            adapted_block = {
                "loader_enabled": False,
                "in_effect": False,
                "values": {},
                "proposal_id": "",
                "approved_at": "",
                "approved_by": "",
            }

        # Recent proposal counts (best-effort; ledger may have its
        # own master flag off — that's fine).
        ledger = self._get_ledger()
        proposals_block = self._summarize_recent_proposals(ledger)

        return self._json_response(
            request, 200,
            {
                "current_effective": current_effective,
                "adapted": adapted_block,
                "proposals": proposals_block,
                "policy_substrate_enabled": (
                    confidence_policy_enabled()
                ),
            },
        )

    def _summarize_recent_proposals(
        self, ledger: AdaptationLedger,
    ) -> Dict[str, Any]:
        """Best-effort projection of confidence-surface proposal
        counts. Newest-first, capped at 50 items. NEVER raises."""
        try:
            # Surface-filtered history (newest-first, capped at 50).
            recent = ledger.history(
                surface=AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
                limit=50,
            )
        except Exception:  # noqa: BLE001 — defensive
            return {
                "pending": 0, "approved": 0, "rejected": 0,
                "items": [],
            }
        counts = {"pending": 0, "approved": 0, "rejected": 0}
        items: List[Dict[str, Any]] = []
        for proposal in recent:
            try:
                status_key = proposal.operator_decision.value
                if status_key in counts:
                    counts[status_key] += 1
                items.append({
                    "proposal_id": proposal.proposal_id,
                    "kind": proposal.proposal_kind,
                    "status": status_key,
                    "proposed_at": proposal.proposed_at,
                    "operator_decision_by": (
                        proposal.operator_decision_by or ""
                    ),
                    "current_state_hash": proposal.current_state_hash,
                    "proposed_state_hash": proposal.proposed_state_hash,
                })
            except Exception:  # noqa: BLE001 — per-row defensive
                continue
        return {**counts, "items": items}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "IDE_POLICY_ROUTER_SCHEMA_VERSION",
    "IDEPolicyRouter",
    "ide_policy_router_enabled",
]


def register_shipped_invariants() -> list:
    """Slice 5 cage close — module-owned shipped-code invariant for
    the Confidence-policy HTTP write surface (Gap #2 Slice 4).

    Pinned guarantees:
      * Loopback-binding assertion symbol referenced (defense
        against refactor that drops the loopback-only check).
      * Surface validator integration: cage substrate symbols
        (compute_policy_diff, build_proposed_state_payload,
        AdaptationLedger, AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS)
        all referenced.
      * All 4 SSE event constants referenced.
      * No authority-carrying imports (orchestrator / iron_gate /
        policy_engine / risk_engine / change_engine /
        tool_executor / providers / candidate_generator).

    NEVER raises. Discovery loop catches exceptions."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_router_surface(tree, source) -> tuple:
        violations = []
        required = (
            ("assert_loopback_only",
             "loopback-bind assertion must be referenced"),
            ("compute_policy_diff",
             "cage substrate predicate must be referenced"),
            ("build_proposed_state_payload",
             "Slice 2 helper must be referenced"),
            ("AdaptationLedger",
             "ledger substrate type must be referenced"),
            ("CONFIDENCE_MONITOR_THRESHOLDS",
             "surface enum value must be referenced"),
            ("EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED",
             "Slice 4 SSE event constant must be referenced"),
            ("EVENT_TYPE_CONFIDENCE_POLICY_APPROVED",
             "Slice 4 SSE event constant must be referenced"),
            ("EVENT_TYPE_CONFIDENCE_POLICY_REJECTED",
             "Slice 4 SSE event constant must be referenced"),
            ("EVENT_TYPE_CONFIDENCE_POLICY_APPLIED",
             "Slice 5 cage-close SSE event constant"),
            ("JARVIS_IDE_POLICY_ROUTER_ENABLED",
             "master flag name canonical"),
        )
        for symbol, reason in required:
            if symbol not in source:
                violations.append(
                    f"ide_policy_router dropped {symbol!r} — "
                    f"{reason}"
                )
        # Walk actual ImportFrom nodes (substring matching the
        # source would false-positive on this very pin's
        # forbidden-token data).
        import ast as _ast
        forbidden_module_tokens = (
            ".orchestrator", ".iron_gate", ".policy_engine",
            ".risk_engine", ".change_engine", ".tool_executor",
            ".providers", ".candidate_generator",
            ".semantic_guardian", ".semantic_firewall",
            ".scoped_tool_backend", ".subagent_scheduler",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                for tok in forbidden_module_tokens:
                    if module.endswith(tok):
                        violations.append(
                            f"ide_policy_router smuggled "
                            f"authority import: {module}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="gap2_ide_policy_router_authority",
            target_file=(
                "backend/core/ouroboros/governance/"
                "ide_policy_router.py"
            ),
            description=(
                "Gap #2 Slice 4 HTTP write surface: loopback "
                "assertion + cage substrate symbols + 4 SSE event "
                "constants + master flag canonical + no "
                "authority-carrying imports. Catches refactor that "
                "weakens the cage discipline of the panel write "
                "surface."
            ),
            validate=_validate_router_surface,
        ),
    ]
