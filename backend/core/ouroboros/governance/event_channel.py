"""
EventChannelServer — Real-time event push into Ouroboros pipeline.

Critical Gap: Ouroboros sensors POLL (hourly). Claude Code has Channels
that push events in real-time. This module accepts webhook pushes from
external systems and routes them as IntentEnvelopes instantly.

Supported sources:
  - GitHub Actions (workflow_run completed/failed)
  - GitHub webhooks (push, pull_request, issues)
  - Generic webhooks (CI systems, monitoring, custom)
  - Local process signals (file watchers, test runners)

Architecture:
  EventChannelServer runs a lightweight FastAPI HTTP endpoint alongside
  the main supervisor. External systems POST JSON events to it.
  Events are validated, classified, and injected into the IntakeRouter
  as IntentEnvelopes — same path as sensor-detected events, but with
  zero polling delay.

Boundary Principle:
  Deterministic: HTTP endpoint, JSON validation, event classification,
  signature verification (GitHub HMAC).
  Agentic: Remediation of the event is handled by the Ouroboros pipeline.

Security:
  - Optional webhook secret verification (HMAC-SHA256 for GitHub)
  - Source allowlist (only configured sources can push events)
  - Rate limiting (max events per minute per source)
  - Bounded payload size (256KB max)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CHANNEL_PORT = int(os.environ.get("JARVIS_CHANNEL_PORT", "8099"))
_CHANNEL_HOST = os.environ.get("JARVIS_CHANNEL_HOST", "127.0.0.1")
_WEBHOOK_SECRET = os.environ.get("JARVIS_WEBHOOK_SECRET", "")
_DW_WEBHOOK_SECRET = os.environ.get("DOUBLEWORD_WEBHOOK_SECRET", "")
_MAX_PAYLOAD_BYTES = int(os.environ.get("JARVIS_CHANNEL_MAX_PAYLOAD", "262144"))
_MAX_EVENTS_PER_MINUTE = int(os.environ.get("JARVIS_CHANNEL_RATE_LIMIT", "30"))
_ENABLED = os.environ.get(
    "JARVIS_EVENT_CHANNELS_ENABLED", "true"
).lower() in ("true", "1", "yes")


@dataclass
class ChannelEvent:
    """One event received from an external system."""
    source: str                # "github", "ci", "webhook", "local"
    event_type: str            # "workflow_run", "push", "issue", "alert"
    payload: Dict[str, Any]
    received_at: float = field(default_factory=time.time)
    signature_valid: bool = True


@dataclass
class ChannelStats:
    """Cumulative channel statistics."""
    total_events: int = 0
    events_by_source: Dict[str, int] = field(default_factory=dict)
    events_routed: int = 0
    events_rejected: int = 0
    last_event_at: float = 0.0


class EventChannelServer:
    """Real-time event push server for Ouroboros.

    Runs a lightweight HTTP endpoint that accepts webhook POSTs from
    external systems. Events are classified and injected into the
    IntakeRouter as IntentEnvelopes — zero polling delay.

    Lifecycle:
      server = EventChannelServer(router=intake_router)
      await server.start()   # Starts HTTP server in background
      ...
      await server.stop()    # Graceful shutdown
    """

    def __init__(
        self,
        router: Any,  # UnifiedIntakeRouter
        port: int = _CHANNEL_PORT,
        host: str = _CHANNEL_HOST,
        batch_registry: Any = None,  # Optional[BatchFutureRegistry]
        github_issue_sensor: Any = None,  # Optional[GitHubIssueSensor]
        doc_staleness_sensor: Any = None,  # Optional[DocStalenessSensor]
        cross_repo_drift_sensor: Any = None,  # Optional[CrossRepoDriftSensor]
        performance_regression_sensor: Any = None,  # Optional[PerformanceRegressionSensor]
        scheduler: Any = None,  # Gap #3 Slice 5 — Optional[SubagentScheduler] for worktree topology GET routes
        worktree_manager: Any = None,  # Gap #3 Slice 5 — Optional[WorktreeManager] for topology git query
    ) -> None:
        self._router = router
        self._port = port
        self._host = host
        self._batch_registry = batch_registry
        # Gap #3 Slice 5 — duck-typed refs threaded through to the
        # IDEObservabilityRouter so the worktree topology GET
        # routes can project scheduler in-memory state. Both default
        # to None — the routes return 503 cleanly when unwired
        # (graceful degradation; matches the cancel-route discipline).
        self._scheduler = scheduler
        self._worktree_manager = worktree_manager
        # Phase B Slice 1 (gap #4 migration): when wired and
        # ``JARVIS_GITHUB_WEBHOOK_ENABLED=true``, GitHub ``issues`` events
        # short-circuit to the sensor's ``ingest_webhook`` so the emitted
        # envelope is shape-identical to the poll path (source,
        # evidence.category, evidence fields). Without this, the generic
        # ``_route_event`` path would emit ``source=runtime_health``
        # envelopes and downstream consumers (ExhaustionWatcher counters,
        # orchestrator postmortems keyed on ``github_issue``) would not
        # recognize them.
        self._github_issue_sensor = github_issue_sensor
        # Slice 4 — GitHub push events route to DocStalenessSensor when
        # wired. Second ``event_type`` handled by the same ``_handle_github``
        # endpoint, demonstrating the EventChannelServer pattern fanning
        # out to multiple sensors in parallel. Kept optional so partial
        # wiring (only issues, or only push) continues to work.
        self._doc_staleness_sensor = doc_staleness_sensor
        self._stats = ChannelStats()
        self._rate_tracker: Dict[str, List[float]] = {}
        self._server_task: Optional[asyncio.Task] = None
        self._site: Optional[Any] = None
        # Gap #4 telemetry — last time a GitHub webhook successfully
        # short-circuited to the sensor. Used by /channel/health so
        # operators can tell if the webhook path is live or quiet.
        self._last_github_webhook_at: float = 0.0
        self._github_webhooks_emitted: int = 0
        self._github_webhooks_ignored: int = 0
        # Slice 4 counters for DocStalenessSensor push deliveries
        self._last_doc_push_at: float = 0.0
        self._doc_pushes_emitted: int = 0
        self._doc_pushes_ignored: int = 0
        # Slice 5 — CrossRepoDriftSensor push deliveries
        self._cross_repo_drift_sensor = cross_repo_drift_sensor
        self._last_drift_push_at: float = 0.0
        self._drift_pushes_emitted: int = 0
        self._drift_pushes_ignored: int = 0
        # Slice 6 — PerformanceRegressionSensor CI webhook deliveries.
        # First CI-surface sensor; uses the single-short-circuit pattern
        # from Slices 1/2 (vs the fan-out Slice 5 uses for multi-sensor
        # push). If a second CI-reactive sensor lands, refactor to the
        # same fan-out dispatcher pattern.
        self._performance_regression_sensor = performance_regression_sensor
        self._last_perf_ci_at: float = 0.0
        self._perf_ci_emitted: int = 0
        self._perf_ci_ignored: int = 0

    @property
    def is_enabled(self) -> bool:
        return _ENABLED

    async def start(self) -> None:
        """Start the event channel HTTP server."""
        if not self.is_enabled:
            logger.info("[EventChannel] Disabled via JARVIS_EVENT_CHANNELS_ENABLED")
            return

        try:
            from aiohttp import web

            app = web.Application()
            app.router.add_post("/webhook/github", self._handle_github)
            app.router.add_post("/webhook/ci", self._handle_ci)
            app.router.add_post("/webhook/generic", self._handle_generic)
            app.router.add_post("/webhook/doubleword", self._handle_doubleword)
            app.router.add_get("/channel/health", self._handle_health)

            ide_router_mounted = False
            ide_stream_mounted = False
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    IDEObservabilityRouter,
                    assert_loopback_only,
                    ide_observability_enabled,
                )
                if ide_observability_enabled():
                    assert_loopback_only(self._host)
                    # Gap #3 Slice 5 — pass scheduler + worktree_manager
                    # refs so the worktree topology GET routes can
                    # project live state. Defaults remain None when
                    # the EventChannelServer was constructed without
                    # them (older callers); the routes degrade to 503.
                    IDEObservabilityRouter(
                        scheduler=self._scheduler,
                        worktree_manager=self._worktree_manager,
                    ).register_routes(app)
                    ide_router_mounted = True
            except ValueError as loopback_exc:
                logger.warning(
                    "[EventChannel] IDE observability refused: %s", loopback_exc,
                )
            except Exception as ide_exc:
                logger.warning(
                    "[EventChannel] IDE observability wiring failed: %s", ide_exc,
                )

            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    assert_loopback_only as _assert_loopback_stream,
                )
                from backend.core.ouroboros.governance.ide_observability_stream import (
                    IDEStreamRouter,
                    stream_enabled,
                )
                if stream_enabled():
                    _assert_loopback_stream(self._host)
                    IDEStreamRouter().register_routes(app)
                    ide_stream_mounted = True
            except ValueError as stream_loopback_exc:
                logger.warning(
                    "[EventChannel] IDE stream refused: %s",
                    stream_loopback_exc,
                )
            except Exception as stream_exc:
                logger.warning(
                    "[EventChannel] IDE stream wiring failed: %s", stream_exc,
                )

            # P4 Slice 5 — convergence-metrics observability surface.
            # Mounts /observability/metrics{,/window,/composite,
            # /sessions/{id}}. Loopback + CORS invariants mirror the
            # existing IDE router; authority invariant
            # (no orchestrator / gate / policy imports) is grep-pinned
            # in tests/governance/test_metrics_observability.py +
            # the Slice 5 graduation suite.
            metrics_router_mounted = False
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    IDEObservabilityRouter as _IDEObsRouter,
                    assert_loopback_only as _assert_loopback_metrics,
                )
                from backend.core.ouroboros.governance.metrics_observability import (  # noqa: E501
                    is_enabled as _metrics_enabled,
                    register_metrics_routes,
                )
                if _metrics_enabled():
                    _assert_loopback_metrics(self._host)
                    # Construct a dedicated IDEObservabilityRouter
                    # instance whose _check_rate_limit + _cors_headers
                    # we reuse as callables. Per-instance rate state
                    # is independent from the main IDE router but uses
                    # the same env-driven cap + CORS allowlist so the
                    # operator-visible surface stays uniform.
                    _metrics_helper = _IDEObsRouter()
                    register_metrics_routes(
                        app,
                        rate_limit_check=lambda req: (
                            _metrics_helper._check_rate_limit(
                                _metrics_helper._client_key(req),
                            )
                        ),
                        cors_headers=_metrics_helper._cors_headers,
                    )
                    metrics_router_mounted = True
            except ValueError as metrics_loopback_exc:
                logger.warning(
                    "[EventChannel] metrics observability refused: %s",
                    metrics_loopback_exc,
                )
            except Exception as metrics_exc:
                logger.warning(
                    "[EventChannel] metrics observability wiring failed: %s",
                    metrics_exc,
                )

            # P5 Slice 5 — adversarial reviewer observability surface.
            # Mounts /observability/adversarial{,/history,/stats,
            # /{op_id}}. Loopback + CORS invariants mirror the
            # existing IDE router; authority invariant
            # (no orchestrator / gate / policy imports + read-only
            # over the JSONL ledger) is grep-pinned in
            # tests/governance/test_adversarial_observability.py +
            # the Slice 5 graduation suite.
            adversarial_router_mounted = False
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    IDEObservabilityRouter as _IDEObsRouterAdv,
                    assert_loopback_only as _assert_loopback_adversarial,
                )
                from backend.core.ouroboros.governance.adversarial_observability import (  # noqa: E501
                    register_adversarial_routes,
                )
                from backend.core.ouroboros.governance.adversarial_reviewer import (  # noqa: E501
                    is_enabled as _adversarial_enabled,
                )
                if _adversarial_enabled():
                    _assert_loopback_adversarial(self._host)
                    # Dedicated IDEObservabilityRouter helper instance
                    # whose _check_rate_limit + _cors_headers we reuse
                    # as callables. Per-instance rate state is
                    # independent from the main IDE router but uses
                    # the same env-driven cap + CORS allowlist so the
                    # operator-visible surface stays uniform (mirrors
                    # the P4 metrics wiring pattern).
                    _adv_helper = _IDEObsRouterAdv()
                    register_adversarial_routes(
                        app,
                        rate_limit_check=lambda req: (
                            _adv_helper._check_rate_limit(
                                _adv_helper._client_key(req),
                            )
                        ),
                        cors_headers=_adv_helper._cors_headers,
                    )
                    adversarial_router_mounted = True
            except ValueError as adversarial_loopback_exc:
                logger.warning(
                    "[EventChannel] adversarial observability refused: %s",
                    adversarial_loopback_exc,
                )
            except Exception as adversarial_exc:
                logger.warning(
                    "[EventChannel] adversarial observability wiring failed: %s",
                    adversarial_exc,
                )

            # Priority D Slice D1 — postmortem ledger discoverability.
            # Mirrors the adversarial wiring pattern: dedicated
            # IDEObservabilityRouter helper for shared rate-limit +
            # CORS, gated on master flag, loopback-asserted.
            postmortem_router_mounted = False
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    IDEObservabilityRouter as _IDEObsRouterPm,
                    assert_loopback_only as _assert_loopback_postmortem,
                )
                from backend.core.ouroboros.governance.postmortem_observability import (  # noqa: E501
                    postmortem_observability_enabled as _pm_enabled,
                    register_postmortem_routes,
                )
                if _pm_enabled():
                    _assert_loopback_postmortem(self._host)
                    _pm_helper = _IDEObsRouterPm()
                    register_postmortem_routes(
                        app,
                        rate_limit_check=lambda req: (
                            _pm_helper._check_rate_limit(
                                _pm_helper._client_key(req),
                            )
                        ),
                        cors_headers=_pm_helper._cors_headers,
                    )
                    postmortem_router_mounted = True
            except ValueError as pm_loopback_exc:
                logger.warning(
                    "[EventChannel] postmortem observability refused: %s",
                    pm_loopback_exc,
                )
            except Exception as pm_exc:
                logger.warning(
                    "[EventChannel] postmortem observability wiring failed: %s",
                    pm_exc,
                )

            # Move 3 Slice 4 — auto-action router observability
            # routes. Loopback + rate-limit + CORS via the shared
            # IDEObservabilityRouter helper; gated on the master
            # flag check inside the auto-action handler (per-request).
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    IDEObservabilityRouter as _IDEObsRouterAA,
                    assert_loopback_only as _assert_loopback_aa,
                )
                from backend.core.ouroboros.governance.auto_action_router import (
                    auto_action_router_enabled as _aa_enabled,
                    register_auto_action_routes,
                    install_shadow_observer,
                )
                if _aa_enabled():
                    _assert_loopback_aa(self._host)
                    _aa_helper = _IDEObsRouterAA()
                    register_auto_action_routes(
                        app,
                        rate_limit_check=lambda req: (
                            _aa_helper._check_rate_limit(
                                _aa_helper._client_key(req),
                            )
                        ),
                        cors_headers=_aa_helper._cors_headers,
                    )
                    # Boot the shadow observer alongside the routes
                    # so the producer + consumer surfaces come up
                    # together. Idempotent — safe to re-call.
                    install_shadow_observer()
            except ValueError as aa_loopback_exc:
                logger.warning(
                    "[EventChannel] auto-action observability "
                    "refused: %s", aa_loopback_exc,
                )
            except Exception as aa_exc:  # noqa: BLE001
                logger.warning(
                    "[EventChannel] auto-action observability "
                    "wiring failed: %s", aa_exc,
                )

            # Move 4 Slice 5 — InvariantDriftAuditor boot wiring.
            # Mirrors the auto_action_router block above: master-
            # flag-gated, loopback-asserted, rate-limited, CORS-
            # aware. Mounts read-only GETs alongside boot-snapshot
            # capture + observer task + auto-action bridge install
            # so producer + consumer surfaces come up together.
            try:
                from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
                    IDEObservabilityRouter as _IDEObsRouterID,
                    assert_loopback_only as _assert_loopback_id,
                )
                from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
                    invariant_drift_auditor_enabled as _id_enabled,
                )
                from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
                    register_invariant_drift_routes,
                )
                from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
                    install_boot_snapshot,
                )
                from backend.core.ouroboros.governance.invariant_drift_observer import (  # noqa: E501
                    get_default_observer as _id_get_observer,
                    observer_enabled as _id_observer_enabled,
                )
                from backend.core.ouroboros.governance.invariant_drift_auto_action_bridge import (  # noqa: E501
                    bridge_enabled as _id_bridge_enabled,
                    install_auto_action_bridge as _id_install_bridge,
                )
                if _id_enabled():
                    _assert_loopback_id(self._host)
                    _id_helper = _IDEObsRouterID()
                    register_invariant_drift_routes(
                        app,
                        rate_limit_check=lambda req: (
                            _id_helper._check_rate_limit(
                                _id_helper._client_key(req),
                            )
                        ),
                        cors_headers=_id_helper._cors_headers,
                    )
                    # Boot snapshot — best-effort, idempotent
                    try:
                        install_boot_snapshot()
                    except Exception as boot_exc:  # noqa: BLE001
                        logger.warning(
                            "[EventChannel] invariant-drift boot "
                            "snapshot failed: %s", boot_exc,
                        )
                    # Bridge install — must precede observer.start()
                    # so the observer's first emit lands in the
                    # auto-action ledger.
                    if _id_bridge_enabled():
                        try:
                            _id_install_bridge()
                        except Exception as br_exc:  # noqa: BLE001
                            logger.warning(
                                "[EventChannel] invariant-drift "
                                "bridge install failed: %s", br_exc,
                            )
                    # Observer task — fires the periodic re-
                    # validation cycle. Idempotent on double-start.
                    if _id_observer_enabled():
                        try:
                            _id_get_observer().start()
                        except Exception as ob_exc:  # noqa: BLE001
                            logger.warning(
                                "[EventChannel] invariant-drift "
                                "observer start failed: %s", ob_exc,
                            )
            except ValueError as id_loopback_exc:
                logger.warning(
                    "[EventChannel] invariant-drift observability "
                    "refused: %s", id_loopback_exc,
                )
            except Exception as id_exc:  # noqa: BLE001
                logger.warning(
                    "[EventChannel] invariant-drift observability "
                    "wiring failed: %s", id_exc,
                )

            # Move 5 Slice 5b — Confidence Probe observability GET
            # routes. Mirrors the auto-action / invariant-drift mount
            # pattern above: loopback-asserted, rate-limited via the
            # shared IDEObservabilityRouter helper, CORS-aware. Unlike
            # Move 4 the mount is unconditional — the probe module's
            # per-request _gate() runs the bridge_enabled() master-flag
            # check on every request, so operators can live-toggle the
            # flag without restarting the harness (see
            # confidence_probe_observability.py:246 for the design
            # intent). Producer side (probe runner + observer) is
            # owned by governed_loop_service.py boot. Per-arc fresh
            # IDEObservabilityRouter instance preserves Move 4's
            # per-arc rate-limit bucket convention.
            try:
                from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
                    IDEObservabilityRouter as _IDEObsRouterCP,
                    assert_loopback_only as _assert_loopback_cp,
                )
                from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
                    register_confidence_probe_routes,
                )
                _assert_loopback_cp(self._host)
                _cp_helper = _IDEObsRouterCP()
                register_confidence_probe_routes(
                    app,
                    rate_limit_check=lambda req: (
                        _cp_helper._check_rate_limit(
                            _cp_helper._client_key(req),
                        )
                    ),
                    cors_headers=_cp_helper._cors_headers,
                )
            except ValueError as cp_loopback_exc:
                logger.warning(
                    "[EventChannel] confidence-probe observability "
                    "refused: %s", cp_loopback_exc,
                )
            except Exception as cp_exc:  # noqa: BLE001
                logger.warning(
                    "[EventChannel] confidence-probe observability "
                    "wiring failed: %s", cp_exc,
                )

            # Priority #1 Slice 5b — Coherence Auditor observability
            # GET routes. Mirrors the Move 5 confidence-probe block
            # above: loopback-asserted, rate-limited via the shared
            # IDEObservabilityRouter helper, CORS-aware, mount is
            # unconditional (per-request _gate() runs
            # coherence_auditor_enabled() so operators can live-toggle
            # the master flag). Producer side (CoherenceObserver +
            # window store + action bridge) is owned by
            # governed_loop_service.py boot. Per-arc fresh
            # IDEObservabilityRouter preserves Move 4's per-arc
            # rate-limit bucket convention.
            try:
                from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
                    IDEObservabilityRouter as _IDEObsRouterCO,
                    assert_loopback_only as _assert_loopback_co,
                )
                from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
                    register_coherence_routes,
                )
                _assert_loopback_co(self._host)
                _co_helper = _IDEObsRouterCO()
                register_coherence_routes(
                    app,
                    rate_limit_check=lambda req: (
                        _co_helper._check_rate_limit(
                            _co_helper._client_key(req),
                        )
                    ),
                    cors_headers=_co_helper._cors_headers,
                )
            except ValueError as co_loopback_exc:
                logger.warning(
                    "[EventChannel] coherence observability "
                    "refused: %s", co_loopback_exc,
                )
            except Exception as co_exc:  # noqa: BLE001
                logger.warning(
                    "[EventChannel] coherence observability "
                    "wiring failed: %s", co_exc,
                )

            # Inline Permission Slice 5 — observability router + bridge.
            # Loopback + CORS invariants mirror the existing IDE surface;
            # authority invariant (no orchestrator / gate imports) is
            # grep-pinned by
            # tests/governance/test_inline_permission_observability.py.
            inline_perm_mounted = False
            try:
                from backend.core.ouroboros.governance.ide_observability import (
                    assert_loopback_only as _assert_loopback_inline,
                )
                from backend.core.ouroboros.governance.inline_permission_observability import (  # noqa: E501
                    InlinePermissionObservabilityRouter,
                    bridge_inline_permission_to_broker,
                    inline_permission_observability_enabled,
                )
                if inline_permission_observability_enabled():
                    _assert_loopback_inline(self._host)
                    InlinePermissionObservabilityRouter().register_routes(app)
                    inline_perm_mounted = True
                    # Bridge controller + store → SSE broker. Best-effort:
                    # stream_enabled() is rechecked inside the bridge's
                    # publish path, so when stream is off we still keep
                    # the listeners wired but drop publishes silently.
                    try:
                        from pathlib import Path as _P
                        from backend.core.ouroboros.governance.inline_permission_prompt import (  # noqa: E501
                            get_default_controller as _get_inline_ctrl,
                        )
                        from backend.core.ouroboros.governance.inline_permission_memory import (  # noqa: E501
                            get_store_for_repo as _get_inline_store,
                        )
                        # Per-repo store defaults to current working
                        # directory. Servers that need a specific repo
                        # scope can override via injection in a future
                        # slice; the current contract mirrors the
                        # lazy-resolution in the observability router.
                        self._inline_perm_unsub = (
                            bridge_inline_permission_to_broker(
                                controller=_get_inline_ctrl(),
                                store=_get_inline_store(_P.cwd()),
                            )
                        )
                    except Exception as bridge_exc:  # noqa: BLE001
                        logger.warning(
                            "[EventChannel] inline-permission bridge "
                            "attach failed: %s", bridge_exc,
                        )
            except ValueError as inline_loopback_exc:
                logger.warning(
                    "[EventChannel] inline-permission observability "
                    "refused: %s", inline_loopback_exc,
                )
            except Exception as inline_exc:
                logger.warning(
                    "[EventChannel] inline-permission wiring failed: %s",
                    inline_exc,
                )

            runner = web.AppRunner(app)
            await runner.setup()
            self._site = web.TCPSite(runner, self._host, self._port)
            await self._site.start()

            extras = []
            if ide_router_mounted:
                extras.append("/observability/*")
            if ide_stream_mounted:
                extras.append("/observability/stream")
            extras_str = (", " + ", ".join(extras)) if extras else ""
            logger.info(
                "[EventChannel] Server started on %s:%d "
                "(endpoints: /webhook/github, /webhook/ci, /webhook/generic%s)",
                self._host, self._port, extras_str,
            )
        except Exception as exc:
            logger.warning("[EventChannel] Failed to start: %s", exc)

    async def stop(self) -> None:
        """Stop the event channel server."""
        if self._site is not None:
            await self._site.stop()
            logger.info("[EventChannel] Server stopped")

    # ------------------------------------------------------------------
    # Webhook handlers (deterministic — HTTP parse + classify + route)
    # ------------------------------------------------------------------

    async def _handle_github(self, request: Any) -> Any:
        """Handle GitHub webhook events (push, PR, workflow_run, issues)."""
        from aiohttp import web

        body = await self._read_bounded_body(request)
        if body is None:
            return web.Response(status=413, text="Payload too large")

        # Verify GitHub signature if secret configured
        if _WEBHOOK_SECRET:
            sig_header = request.headers.get("X-Hub-Signature-256", "")
            if not self._verify_github_signature(body, sig_header):
                self._stats.events_rejected += 1
                return web.Response(status=401, text="Invalid signature")

        # Rate limit
        if not self._check_rate_limit("github"):
            return web.Response(status=429, text="Rate limited")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        event_type = request.headers.get("X-GitHub-Event", "unknown")

        # --- gap #4 short-circuit: route issues events to the sensor ----
        #
        # When the sensor is wired AND the master flag is on, the
        # authoritative handler for ``issues`` events is
        # ``GitHubIssueSensor.ingest_webhook`` — it emits an envelope
        # shape-identical to the poll path. Bypasses the generic
        # ``_route_event`` to prevent double-emission with a mismatched
        # source. All other GitHub event types (workflow_run, push, ...)
        # continue through the generic path unchanged.
        if event_type == "issues" and self._github_issue_sensor is not None:
            try:
                from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
                    webhook_enabled as _gh_webhook_enabled,
                )
                flag_on = _gh_webhook_enabled()
            except Exception:
                flag_on = False
            if flag_on:
                # §3: bound every agentic thread in time so a pathological
                # ingest cannot starve the HTTP handler. 30s cap picked so a
                # slow gh CLI fallback inside the sensor still has room but
                # no single webhook ties up an aiohttp worker indefinitely.
                action_hint = str(payload.get("action", "?"))
                t0 = time.monotonic()
                try:
                    emitted = await asyncio.wait_for(
                        self._github_issue_sensor.ingest_webhook(payload),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[EventChannel] GitHubIssueSensor.ingest_webhook timed "
                        "out after 30s (action=%s)", action_hint,
                    )
                    emitted = False
                except Exception:
                    logger.debug(
                        "[EventChannel] GitHubIssueSensor.ingest_webhook raised",
                        exc_info=True,
                    )
                    emitted = False
                duration_ms = int((time.monotonic() - t0) * 1000)

                self._stats.total_events += 1
                self._stats.last_event_at = time.time()
                self._stats.events_by_source["github"] = (
                    self._stats.events_by_source.get("github", 0) + 1
                )
                self._last_github_webhook_at = time.time()
                if emitted:
                    self._stats.events_routed += 1
                    self._github_webhooks_emitted += 1
                else:
                    self._github_webhooks_ignored += 1
                # Stable key=value telemetry — same convention as
                # [SemanticGuard] and [REVIEW-SHADOW]. split("=") parses
                # into rollup counters for "what percent of GitHub
                # webhooks produced envelopes" dashboards.
                logger.info(
                    "[EventChannel] github/issues delivered "
                    "sensor=GitHubIssueSensor action=%s emitted=%s "
                    "duration_ms=%d",
                    action_hint, emitted, duration_ms,
                )
                return web.Response(status=200, text="OK")

        # --- Slice 5: push events fan out to all active push sensors ---
        #
        # Earlier slices used an ``if/elif`` short-circuit where the
        # first matching sensor "won" and subsequent handlers never saw
        # the event. That doesn't scale once two sensors care about the
        # same event type — both DocStalenessSensor and
        # CrossRepoDriftSensor have independent reasons to react to a
        # push. The dispatcher below invokes **every** active sensor
        # concurrently via ``asyncio.gather`` (each in its own
        # ``wait_for(30s)`` so one slow sensor can't starve the others).
        # Falls through to the generic ``_route_event`` path only if
        # zero sensors are active, preserving pre-Slice-4 behavior.
        if event_type == "push":
            push_handlers = await self._collect_active_push_handlers()
            if push_handlers:
                ref_hint = str(payload.get("ref", "?"))
                t0 = time.monotonic()
                results = await asyncio.gather(
                    *[
                        self._invoke_push_handler(name, sensor, payload)
                        for name, sensor in push_handlers
                    ],
                    return_exceptions=False,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                self._stats.total_events += 1
                self._stats.last_event_at = time.time()
                self._stats.events_by_source["github"] = (
                    self._stats.events_by_source.get("github", 0) + 1
                )

                fired_names: List[str] = []
                emitted_names: List[str] = []
                for (name, _sensor), emitted in zip(push_handlers, results):
                    fired_names.append(name)
                    if name == "DocStalenessSensor":
                        self._last_doc_push_at = time.time()
                        if emitted:
                            self._doc_pushes_emitted += 1
                            emitted_names.append(name)
                        else:
                            self._doc_pushes_ignored += 1
                    elif name == "CrossRepoDriftSensor":
                        self._last_drift_push_at = time.time()
                        if emitted:
                            self._drift_pushes_emitted += 1
                            emitted_names.append(name)
                        else:
                            self._drift_pushes_ignored += 1
                    if emitted:
                        self._stats.events_routed += 1

                logger.info(
                    "[EventChannel] github/push fan-out ref=%s "
                    "fired=[%s] emitted=[%s] duration_ms=%d",
                    ref_hint,
                    ",".join(fired_names),
                    ",".join(emitted_names) if emitted_names else "none",
                    duration_ms,
                )
                return web.Response(status=200, text="OK")

        event = ChannelEvent(
            source="github",
            event_type=event_type,
            payload=payload,
        )

        await self._route_event(event)
        return web.Response(status=200, text="OK")

    # ------------------------------------------------------------------
    # Slice 5 — fan-out dispatcher for GitHub push events
    # ------------------------------------------------------------------

    async def _collect_active_push_handlers(
        self,
    ) -> "List[Tuple[str, Any]]":
        """Return (name, sensor) for every wired sensor whose webhook
        flag is currently on.

        Each sensor's flag is re-checked per-request so operators can
        flip flags at runtime (env change) without needing to restart.
        Sensors whose import or flag-check raises are silently skipped —
        the observability contract here is at the per-delivery log line,
        not at the handler-collection step.
        """
        handlers: List[Tuple[str, Any]] = []

        if self._doc_staleness_sensor is not None:
            try:
                from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
                    webhook_enabled as _doc_webhook_enabled,
                )
                if _doc_webhook_enabled():
                    handlers.append(
                        ("DocStalenessSensor", self._doc_staleness_sensor),
                    )
            except Exception:
                pass

        if self._cross_repo_drift_sensor is not None:
            try:
                from backend.core.ouroboros.governance.intake.sensors.cross_repo_drift_sensor import (
                    webhook_enabled as _drift_webhook_enabled,
                )
                if _drift_webhook_enabled():
                    handlers.append(
                        ("CrossRepoDriftSensor", self._cross_repo_drift_sensor),
                    )
            except Exception:
                pass

        return handlers

    async def _invoke_push_handler(
        self,
        name: str,
        sensor: Any,
        payload: Dict[str, Any],
    ) -> bool:
        """Invoke one sensor's ``ingest_webhook`` under a 30s timeout.

        Returns the sensor's emission boolean on clean completion,
        ``False`` on timeout or any exception (logged at DEBUG/WARNING).
        Never raises — the fan-out gather relies on this invariant so
        one sensor cannot poison the batch for the others.
        """
        try:
            return bool(
                await asyncio.wait_for(sensor.ingest_webhook(payload), timeout=30.0)
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[EventChannel] %s.ingest_webhook timed out after 30s",
                name,
            )
            return False
        except Exception:
            logger.debug(
                "[EventChannel] %s.ingest_webhook raised",
                name, exc_info=True,
            )
            return False

    async def _handle_ci(self, request: Any) -> Any:
        """Handle CI system webhook events (Jenkins, GitHub Actions, etc.)."""
        from aiohttp import web

        body = await self._read_bounded_body(request)
        if body is None:
            return web.Response(status=413, text="Payload too large")

        if not self._check_rate_limit("ci"):
            return web.Response(status=429, text="Rate limited")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        # --- Slice 6 short-circuit — CI → PerformanceRegressionSensor ---
        #
        # First CI-surface sensor to migrate. Uses a single-sensor
        # short-circuit (pre-Slice-5 pattern). When a second
        # CI-reactive sensor lands, refactor to a fan-out dispatcher
        # identical to the push-handler pattern in `_handle_github`.
        # Falls through to the generic ``_route_event`` path when the
        # sensor isn't wired or the flag is off — preserves pre-Slice-6
        # behavior so operators who haven't opted in see no change.
        if self._performance_regression_sensor is not None:
            try:
                from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
                    webhook_enabled as _perf_webhook_enabled,
                )
                perf_flag_on = _perf_webhook_enabled()
            except Exception:
                perf_flag_on = False
            if perf_flag_on:
                status_hint = str(
                    payload.get("status", payload.get("conclusion", "?"))
                )
                t0 = time.monotonic()
                try:
                    emitted = await asyncio.wait_for(
                        self._performance_regression_sensor.ingest_webhook(payload),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[EventChannel] PerformanceRegressionSensor.ingest_webhook "
                        "timed out after 30s (status=%s)", status_hint,
                    )
                    emitted = False
                except Exception:
                    logger.debug(
                        "[EventChannel] PerformanceRegressionSensor.ingest_webhook raised",
                        exc_info=True,
                    )
                    emitted = False
                duration_ms = int((time.monotonic() - t0) * 1000)

                self._stats.total_events += 1
                self._stats.last_event_at = time.time()
                self._stats.events_by_source["ci"] = (
                    self._stats.events_by_source.get("ci", 0) + 1
                )
                self._last_perf_ci_at = time.time()
                if emitted:
                    self._stats.events_routed += 1
                    self._perf_ci_emitted += 1
                else:
                    self._perf_ci_ignored += 1
                logger.info(
                    "[EventChannel] ci/event delivered "
                    "sensor=PerformanceRegressionSensor status=%s "
                    "emitted=%s duration_ms=%d",
                    status_hint, emitted, duration_ms,
                )
                return web.Response(status=200, text="OK")

        event = ChannelEvent(
            source="ci",
            event_type=payload.get("event", payload.get("action", "unknown")),
            payload=payload,
        )

        await self._route_event(event)
        return web.Response(status=200, text="OK")

    async def _handle_generic(self, request: Any) -> Any:
        """Handle generic webhook events from any source."""
        from aiohttp import web

        body = await self._read_bounded_body(request)
        if body is None:
            return web.Response(status=413, text="Payload too large")

        source = request.headers.get("X-Event-Source", "webhook")
        if not self._check_rate_limit(source):
            return web.Response(status=429, text="Rate limited")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        event = ChannelEvent(
            source=source,
            event_type=payload.get("type", payload.get("event", "unknown")),
            payload=payload,
        )

        await self._route_event(event)
        return web.Response(status=200, text="OK")

    async def _handle_doubleword(self, request: Any) -> Any:
        """DoubleWord webhook — batch.completed / batch.failed (Standard Webhooks).

        Resolves or rejects the corresponding asyncio.Future in the
        BatchFutureRegistry so callers can await batch results with
        zero polling (Manifesto §3).
        """
        from aiohttp import web
        import base64

        body = await self._read_bounded_body(request)
        if body is None:
            return web.Response(status=413, text="Payload too large")

        # Standard Webhooks signature verification
        if _DW_WEBHOOK_SECRET:
            wh_id = request.headers.get("webhook-id", "")
            wh_ts = request.headers.get("webhook-timestamp", "")
            wh_sig = request.headers.get("webhook-signature", "")
            try:
                key = base64.b64decode(_DW_WEBHOOK_SECRET.removeprefix("whsec_"))
                signed_content = f"{wh_id}.{wh_ts}.{body.decode()}".encode()
                expected = base64.b64encode(
                    hmac.new(key, signed_content, hashlib.sha256).digest()
                ).decode()
                valid = any(
                    hmac.compare_digest(s[3:], expected)
                    for s in wh_sig.split() if s.startswith("v1,")
                )
            except Exception:
                valid = False
            if not valid:
                self._stats.events_rejected += 1
                return web.Response(status=401, text="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        event_type = payload.get("type", "")
        data = payload.get("data", {})
        batch_id = data.get("batch_id", "")

        if self._batch_registry is None:
            logger.warning("[EventChannel] DW webhook received but no BatchFutureRegistry wired")
            return web.Response(status=200, text="OK (no registry)")

        if event_type == "batch.completed":
            output_file_id = data.get("output_file_id", "")
            resolved = self._batch_registry.resolve(batch_id, output_file_id)
            logger.info("[EventChannel] DW batch.completed %s (resolved=%s)", batch_id, resolved)
        elif event_type == "batch.failed":
            reason = data.get("error", {}).get("message", "unknown")
            self._batch_registry.reject(batch_id, reason)
            logger.warning("[EventChannel] DW batch.failed %s: %s", batch_id, reason)
        else:
            logger.debug("[EventChannel] DW webhook unknown type: %s", event_type)

        self._stats.total_events += 1
        self._stats.last_event_at = time.time()
        return web.Response(status=200, text="OK")

    async def _handle_health(self, request: Any) -> Any:
        """Health check endpoint.

        Gap #4 observability: exposes whether webhook-primary mode is
        active per sensor, so operators can watch the ``sensors``
        ratio trend (events by sensor / total events) and catch silent
        regressions where the webhook stops arriving.
        """
        from aiohttp import web

        # Ask the sensor itself rather than trusting a cached flag —
        # ``webhook_enabled()`` re-reads the env so tests and live
        # reconfigurations stay honest.
        sensor_wired = self._github_issue_sensor is not None
        webhook_mode = False
        if sensor_wired:
            try:
                from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
                    webhook_enabled as _gh_webhook_enabled,
                )
                webhook_mode = bool(_gh_webhook_enabled())
            except Exception:
                webhook_mode = False

        # Slice 4 — expose DocStalenessSensor push-webhook state too.
        doc_wired = self._doc_staleness_sensor is not None
        doc_webhook_mode = False
        if doc_wired:
            try:
                from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
                    webhook_enabled as _doc_webhook_enabled,
                )
                doc_webhook_mode = bool(_doc_webhook_enabled())
            except Exception:
                doc_webhook_mode = False

        # Slice 5 — expose CrossRepoDriftSensor push-webhook state.
        drift_wired = self._cross_repo_drift_sensor is not None
        drift_webhook_mode = False
        if drift_wired:
            try:
                from backend.core.ouroboros.governance.intake.sensors.cross_repo_drift_sensor import (
                    webhook_enabled as _drift_webhook_enabled,
                )
                drift_webhook_mode = bool(_drift_webhook_enabled())
            except Exception:
                drift_webhook_mode = False

        # Slice 6 — expose PerformanceRegressionSensor CI-webhook state.
        perf_wired = self._performance_regression_sensor is not None
        perf_webhook_mode = False
        if perf_wired:
            try:
                from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
                    webhook_enabled as _perf_webhook_enabled,
                )
                perf_webhook_mode = bool(_perf_webhook_enabled())
            except Exception:
                perf_webhook_mode = False

        return web.json_response({
            "status": "healthy",
            "total_events": self._stats.total_events,
            "events_routed": self._stats.events_routed,
            "events_rejected": self._stats.events_rejected,
            "events_by_source": dict(self._stats.events_by_source),
            "last_event": self._stats.last_event_at,
            "github_issue_sensor": {
                "wired": sensor_wired,
                "webhook_mode": webhook_mode,
                "last_webhook_at": self._last_github_webhook_at,
                "webhooks_emitted": self._github_webhooks_emitted,
                "webhooks_ignored": self._github_webhooks_ignored,
            },
            "doc_staleness_sensor": {
                "wired": doc_wired,
                "webhook_mode": doc_webhook_mode,
                "last_push_at": self._last_doc_push_at,
                "pushes_emitted": self._doc_pushes_emitted,
                "pushes_ignored": self._doc_pushes_ignored,
            },
            "cross_repo_drift_sensor": {
                "wired": drift_wired,
                "webhook_mode": drift_webhook_mode,
                "last_push_at": self._last_drift_push_at,
                "pushes_emitted": self._drift_pushes_emitted,
                "pushes_ignored": self._drift_pushes_ignored,
            },
            "performance_regression_sensor": {
                "wired": perf_wired,
                "webhook_mode": perf_webhook_mode,
                "last_ci_at": self._last_perf_ci_at,
                "ci_emitted": self._perf_ci_emitted,
                "ci_ignored": self._perf_ci_ignored,
            },
        })

    # ------------------------------------------------------------------
    # Event routing (deterministic — classify + emit IntentEnvelope)
    # ------------------------------------------------------------------

    async def _route_event(self, event: ChannelEvent) -> None:
        """Classify event and route to IntakeRouter as IntentEnvelope."""
        self._stats.total_events += 1
        self._stats.last_event_at = time.time()
        self._stats.events_by_source[event.source] = \
            self._stats.events_by_source.get(event.source, 0) + 1

        # Classify urgency and description
        urgency, description, target_files, repo = self._classify_event(event)

        try:
            envelope = make_envelope(
                source="runtime_health",
                description=f"[CHANNEL:{event.source}] {description}",
                target_files=target_files,
                repo=repo,
                confidence=0.90,
                urgency=urgency,
                evidence={
                    "category": "event_channel",
                    "channel_source": event.source,
                    "event_type": event.event_type,
                    "payload_keys": list(event.payload.keys())[:10],
                    "received_at": event.received_at,
                },
                requires_human_ack=False,
            )
            result = await self._router.ingest(envelope)
            if result == "enqueued":
                self._stats.events_routed += 1
                logger.info(
                    "[EventChannel] %s:%s -> %s (urgency=%s)",
                    event.source, event.event_type, result, urgency,
                )
        except Exception:
            logger.debug("[EventChannel] Route failed", exc_info=True)

        # Phase 4 Event Spine: bridge to TrinityEventBus for cross-repo visibility
        try:
            from backend.core.trinity_event_bus import get_event_bus_if_exists
            bus = get_event_bus_if_exists()
            if bus is not None:
                await bus.publish_raw(
                    topic=f"webhook.{event.source}",
                    data={
                        "source": event.source,
                        "event_type": event.event_type,
                        "urgency": urgency,
                        "description": description,
                        "payload_keys": list(event.payload.keys())[:10],
                    },
                    persist=True,
                )
        except Exception:
            pass  # Bridge failures are non-fatal

    def _classify_event(
        self, event: ChannelEvent,
    ) -> Tuple[str, str, Tuple[str, ...], str]:
        """Classify event into urgency, description, target_files, repo.

        Deterministic — pattern matching on event type and payload.
        """
        payload = event.payload

        # GitHub events
        if event.source == "github":
            if event.event_type == "workflow_run":
                conclusion = payload.get("workflow_run", {}).get("conclusion", "")
                name = payload.get("workflow_run", {}).get("name", "unknown")
                repo_full = payload.get("repository", {}).get("full_name", "")
                if conclusion == "failure":
                    return (
                        "high",
                        f"GitHub Actions workflow '{name}' FAILED in {repo_full}",
                        ("backend/",),
                        self._repo_from_github(repo_full),
                    )
                return (
                    "low",
                    f"GitHub Actions workflow '{name}' {conclusion} in {repo_full}",
                    ("backend/",),
                    self._repo_from_github(repo_full),
                )

            if event.event_type == "issues":
                action = payload.get("action", "")
                title = payload.get("issue", {}).get("title", "")
                number = payload.get("issue", {}).get("number", 0)
                repo_full = payload.get("repository", {}).get("full_name", "")
                if action == "opened":
                    return (
                        "normal",
                        f"New issue #{number}: {title} in {repo_full}",
                        ("backend/",),
                        self._repo_from_github(repo_full),
                    )

            if event.event_type == "push":
                ref = payload.get("ref", "")
                commits = payload.get("commits", [])
                repo_full = payload.get("repository", {}).get("full_name", "")
                return (
                    "low",
                    f"Push to {ref} ({len(commits)} commits) in {repo_full}",
                    ("backend/",),
                    self._repo_from_github(repo_full),
                )

        # CI events
        if event.source == "ci":
            status = payload.get("status", payload.get("conclusion", "unknown"))
            name = payload.get("name", payload.get("job", "unknown"))
            if status in ("failure", "failed", "error"):
                return (
                    "high",
                    f"CI job '{name}' FAILED: {payload.get('message', '')}",
                    ("backend/",),
                    "jarvis",
                )
            return (
                "low",
                f"CI job '{name}': {status}",
                ("backend/",),
                "jarvis",
            )

        # Generic fallback
        return (
            "normal",
            f"{event.source} event: {event.event_type}",
            ("backend/",),
            "jarvis",
        )

    @staticmethod
    def _repo_from_github(full_name: str) -> str:
        """Map GitHub full_name to Trinity repo name."""
        mapping = {
            "drussell23/JARVIS": "jarvis",
            "drussell23/JARVIS-Prime": "jarvis-prime",
            "drussell23/JARVIS-Reactor": "reactor",
        }
        return mapping.get(full_name, "jarvis")

    # ------------------------------------------------------------------
    # Security (deterministic — HMAC, rate limiting)
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_github_signature(body: bytes, signature_header: str) -> bool:
        """Verify GitHub webhook HMAC-SHA256 signature."""
        if not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(
            _WEBHOOK_SECRET.encode(), body, hashlib.sha256,
        ).hexdigest()
        received = signature_header[7:]
        return hmac.compare_digest(expected, received)

    def _check_rate_limit(self, source: str) -> bool:
        """Check per-source rate limit. Deterministic sliding window."""
        now = time.time()
        window = self._rate_tracker.setdefault(source, [])
        # Prune events older than 60s
        self._rate_tracker[source] = [t for t in window if now - t < 60]
        if len(self._rate_tracker[source]) >= _MAX_EVENTS_PER_MINUTE:
            return False
        self._rate_tracker[source].append(now)
        return True

    async def _read_bounded_body(self, request: Any) -> Optional[bytes]:
        """Read request body with size limit."""
        try:
            return await request.content.read(_MAX_PAYLOAD_BYTES)
        except Exception:
            return None

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_events": self._stats.total_events,
            "events_by_source": dict(self._stats.events_by_source),
            "events_routed": self._stats.events_routed,
            "events_rejected": self._stats.events_rejected,
            "last_event_at": self._stats.last_event_at,
        }
