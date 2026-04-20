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
    ) -> None:
        self._router = router
        self._port = port
        self._host = host
        self._batch_registry = batch_registry
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

            runner = web.AppRunner(app)
            await runner.setup()
            self._site = web.TCPSite(runner, self._host, self._port)
            await self._site.start()

            logger.info(
                "[EventChannel] Server started on %s:%d "
                "(endpoints: /webhook/github, /webhook/ci, /webhook/generic)",
                self._host, self._port,
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

        event = ChannelEvent(
            source="github",
            event_type=event_type,
            payload=payload,
        )

        await self._route_event(event)
        return web.Response(status=200, text="OK")

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
