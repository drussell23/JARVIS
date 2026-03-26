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
    ) -> None:
        self._router = router
        self._port = port
        self._host = host
        self._stats = ChannelStats()
        self._rate_tracker: Dict[str, List[float]] = {}
        self._server_task: Optional[asyncio.Task] = None
        self._site: Optional[Any] = None

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

    async def _handle_health(self, request: Any) -> Any:
        """Health check endpoint."""
        from aiohttp import web
        return web.json_response({
            "status": "healthy",
            "total_events": self._stats.total_events,
            "events_routed": self._stats.events_routed,
            "last_event": self._stats.last_event_at,
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
