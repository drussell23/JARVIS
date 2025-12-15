#!/usr/bin/env python3
"""
JARVIS Loading Server v3.0 - Production-Grade Startup Progress Server
======================================================================

Serves the loading page independently from frontend/backend during restart.
Provides real-time progress updates via WebSocket and HTTP polling.

Features:
- Monotonic progress enforcement (never decreases)
- CORS support for cross-origin requests
- WebSocket for real-time updates with heartbeat
- HTTP polling fallback with caching
- Parallel health checks (backend + frontend)
- Connection pooling and rate limiting
- Metrics and telemetry collection
- Dynamic configuration from environment
- Graceful degradation and recovery
- Request queuing for burst handling

Port: 3001 (separate from frontend:3000 and backend:8010)
"""

import asyncio
import logging
import json
import os
import sys
import time
import weakref
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List, Any, Callable
from collections import deque
from contextlib import asynccontextmanager
import hashlib

import aiohttp
from aiohttp import web, WSCloseCode

# Configure logging with structured format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('loading_server')


# =============================================================================
# Dynamic Configuration
# =============================================================================

@dataclass
class ServerConfig:
    """Dynamic server configuration from environment variables"""

    # Server ports
    loading_port: int = field(default_factory=lambda: int(os.getenv('LOADING_SERVER_PORT', '3001')))
    backend_port: int = field(default_factory=lambda: int(os.getenv('BACKEND_PORT', '8010')))
    frontend_port: int = field(default_factory=lambda: int(os.getenv('FRONTEND_PORT', '3000')))

    # Health check settings
    health_check_timeout: float = field(default_factory=lambda: float(os.getenv('HEALTH_CHECK_TIMEOUT', '3.0')))
    health_check_interval: float = field(default_factory=lambda: float(os.getenv('HEALTH_CHECK_INTERVAL', '5.0')))

    # Watchdog settings
    watchdog_silence_threshold: int = field(default_factory=lambda: int(os.getenv('WATCHDOG_SILENCE_THRESHOLD', '60')))
    watchdog_startup_delay: int = field(default_factory=lambda: int(os.getenv('WATCHDOG_STARTUP_DELAY', '30')))

    # WebSocket settings
    ws_heartbeat_interval: float = field(default_factory=lambda: float(os.getenv('WS_HEARTBEAT_INTERVAL', '15.0')))
    ws_heartbeat_timeout: float = field(default_factory=lambda: float(os.getenv('WS_HEARTBEAT_TIMEOUT', '30.0')))
    ws_max_connections: int = field(default_factory=lambda: int(os.getenv('WS_MAX_CONNECTIONS', '100')))

    # Rate limiting
    rate_limit_requests: int = field(default_factory=lambda: int(os.getenv('RATE_LIMIT_REQUESTS', '100')))
    rate_limit_window: float = field(default_factory=lambda: float(os.getenv('RATE_LIMIT_WINDOW', '60.0')))

    # Paths
    frontend_path: Path = field(default_factory=lambda: Path(os.getenv('FRONTEND_PATH', Path(__file__).parent / 'landing-page')))

    def __post_init__(self):
        self.frontend_path = Path(self.frontend_path)


config = ServerConfig()


# =============================================================================
# Metrics Collection
# =============================================================================

@dataclass
class ServerMetrics:
    """Server metrics for monitoring and debugging"""

    start_time: datetime = field(default_factory=datetime.now)
    total_requests: int = 0
    websocket_connections: int = 0
    websocket_messages_sent: int = 0
    progress_updates_received: int = 0
    health_checks_performed: int = 0
    errors: int = 0
    last_error: Optional[str] = None
    last_progress_update: Optional[datetime] = None

    # Request latencies (last 100)
    request_latencies: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_request(self, latency_ms: float):
        self.total_requests += 1
        self.request_latencies.append(latency_ms)

    def record_error(self, error: str):
        self.errors += 1
        self.last_error = error

    @property
    def avg_latency_ms(self) -> float:
        if not self.request_latencies:
            return 0.0
        return sum(self.request_latencies) / len(self.request_latencies)

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "total_requests": self.total_requests,
            "websocket_connections": self.websocket_connections,
            "websocket_messages_sent": self.websocket_messages_sent,
            "progress_updates_received": self.progress_updates_received,
            "health_checks_performed": self.health_checks_performed,
            "errors": self.errors,
            "last_error": self.last_error,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_progress_update": self.last_progress_update.isoformat() if self.last_progress_update else None
        }


metrics = ServerMetrics()


# =============================================================================
# Progress State Management
# =============================================================================

@dataclass
class ProgressState:
    """Thread-safe progress state with history tracking"""

    stage: str = "init"
    message: str = "Initializing JARVIS..."
    progress: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    backend_ready: bool = False
    frontend_ready: bool = False
    websocket_ready: bool = False

    # Monotonic enforcement
    max_progress_seen: float = 0.0

    # History tracking (last 50 updates)
    history: deque = field(default_factory=lambda: deque(maxlen=50))

    # ETag for caching
    _etag: Optional[str] = None

    def update(self, stage: str, message: str, progress: float, metadata: Optional[Dict] = None) -> bool:
        """Update progress with monotonic enforcement. Returns True if progress changed."""

        # Monotonic enforcement
        if stage == 'complete':
            effective_progress = 100.0
            self.max_progress_seen = 100.0
        elif progress > self.max_progress_seen:
            effective_progress = progress
            self.max_progress_seen = progress
        else:
            effective_progress = self.max_progress_seen

        # Check if anything changed
        changed = (
            self.stage != stage or
            self.message != message or
            self.progress != effective_progress
        )

        # Update state
        self.stage = stage
        self.message = message
        self.progress = effective_progress
        self.timestamp = datetime.now()

        if metadata:
            self.metadata = metadata

        # Track history
        self.history.append({
            "stage": stage,
            "message": message,
            "progress": effective_progress,
            "timestamp": self.timestamp.isoformat()
        })

        # Invalidate ETag
        self._etag = None

        return changed

    @property
    def etag(self) -> str:
        """Generate ETag for HTTP caching"""
        if self._etag is None:
            content = f"{self.stage}:{self.message}:{self.progress}:{self.timestamp.isoformat()}"
            self._etag = hashlib.md5(content.encode()).hexdigest()[:16]
        return self._etag

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "backend_ready": self.backend_ready,
            "frontend_ready": self.frontend_ready,
            "websocket_ready": self.websocket_ready
        }


progress_state = ProgressState()


# =============================================================================
# Connection Management
# =============================================================================

class ConnectionManager:
    """Manages WebSocket connections with heartbeat and cleanup"""

    def __init__(self, max_connections: int = 100):
        self.max_connections = max_connections
        self._connections: Set[web.WebSocketResponse] = set()
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def count(self) -> int:
        return len(self._connections)

    async def add(self, ws: web.WebSocketResponse) -> bool:
        """Add a WebSocket connection. Returns False if at capacity."""
        async with self._lock:
            if len(self._connections) >= self.max_connections:
                return False
            self._connections.add(ws)
            metrics.websocket_connections = len(self._connections)
            return True

    async def remove(self, ws: web.WebSocketResponse):
        """Remove a WebSocket connection."""
        async with self._lock:
            self._connections.discard(ws)
            metrics.websocket_connections = len(self._connections)

    async def broadcast(self, data: Dict[str, Any]):
        """Broadcast to all connections, removing dead ones."""
        if not self._connections:
            return

        disconnected = set()
        message = json.dumps(data)

        async with self._lock:
            for ws in self._connections:
                try:
                    if not ws.closed:
                        await ws.send_str(message)
                        metrics.websocket_messages_sent += 1
                except Exception as e:
                    logger.debug(f"Broadcast failed to client: {e}")
                    disconnected.add(ws)

            # Clean up disconnected
            for ws in disconnected:
                self._connections.discard(ws)

            metrics.websocket_connections = len(self._connections)

    async def start_heartbeat(self):
        """Start heartbeat task to keep connections alive."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self):
        """Stop heartbeat task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to all connections."""
        while True:
            try:
                await asyncio.sleep(config.ws_heartbeat_interval)
                await self.broadcast({"type": "heartbeat", "timestamp": datetime.now().isoformat()})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

    async def close_all(self):
        """Close all connections gracefully."""
        async with self._lock:
            for ws in list(self._connections):
                try:
                    await ws.close(code=WSCloseCode.GOING_AWAY, message=b'Server shutting down')
                except Exception:
                    pass
            self._connections.clear()


connection_manager = ConnectionManager(max_connections=config.ws_max_connections)


# =============================================================================
# Rate Limiting
# =============================================================================

class RateLimiter:
    """Simple sliding window rate limiter"""

    def __init__(self, requests: int, window_seconds: float):
        self.requests = requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, deque] = {}

    def is_allowed(self, client_id: str) -> bool:
        """Check if request is allowed for client."""
        now = time.time()
        cutoff = now - self.window_seconds

        if client_id not in self._requests:
            self._requests[client_id] = deque()

        # Clean old requests
        while self._requests[client_id] and self._requests[client_id][0] < cutoff:
            self._requests[client_id].popleft()

        # Check limit
        if len(self._requests[client_id]) >= self.requests:
            return False

        # Record request
        self._requests[client_id].append(now)
        return True


rate_limiter = RateLimiter(config.rate_limit_requests, config.rate_limit_window)


# =============================================================================
# Health Check System
# =============================================================================

class HealthChecker:
    """Parallel health checking with connection pooling"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None

    async def start(self):
        """Initialize connection pool."""
        self._connector = aiohttp.TCPConnector(
            limit=10,
            limit_per_host=5,
            ttl_dns_cache=300,
            enable_cleanup_closed=True
        )
        self._session = aiohttp.ClientSession(
            connector=self._connector,
            timeout=aiohttp.ClientTimeout(total=config.health_check_timeout)
        )

    async def stop(self):
        """Close connection pool."""
        if self._session:
            await self._session.close()
        if self._connector:
            await self._connector.close()

    async def check_backend(self) -> tuple[bool, str]:
        """Check if backend is healthy."""
        if not self._session:
            return False, "Health checker not initialized"

        try:
            async with self._session.get(
                f'http://localhost:{config.backend_port}/health'
            ) as resp:
                if resp.status == 200:
                    progress_state.backend_ready = True
                    progress_state.websocket_ready = True
                    return True, "Backend ready"
                return False, f"Backend returned {resp.status}"
        except asyncio.TimeoutError:
            return False, "Backend timeout"
        except aiohttp.ClientError as e:
            return False, f"Backend connection error: {e}"
        except Exception as e:
            return False, f"Backend check error: {e}"

    async def check_frontend(self) -> tuple[bool, str]:
        """Check if frontend is healthy."""
        if not self._session:
            return False, "Health checker not initialized"

        try:
            async with self._session.get(
                f'http://localhost:{config.frontend_port}'
            ) as resp:
                if resp.status in [200, 304]:
                    progress_state.frontend_ready = True
                    return True, "Frontend ready"
                return False, f"Frontend returned {resp.status}"
        except asyncio.TimeoutError:
            return False, "Frontend timeout"
        except aiohttp.ClientError as e:
            return False, f"Frontend connection error: {e}"
        except Exception as e:
            return False, f"Frontend check error: {e}"

    async def check_all_parallel(self) -> tuple[bool, str]:
        """Check both backend and frontend in parallel."""
        metrics.health_checks_performed += 1

        backend_task = asyncio.create_task(self.check_backend())
        frontend_task = asyncio.create_task(self.check_frontend())

        backend_ok, backend_reason = await backend_task
        frontend_ok, frontend_reason = await frontend_task

        if not backend_ok:
            return False, backend_reason
        if not frontend_ok:
            return False, f"Backend ready but {frontend_reason}"

        return True, "Full system ready"


health_checker = HealthChecker()


# =============================================================================
# Middleware
# =============================================================================

@web.middleware
async def cors_middleware(request: web.Request, handler: Callable) -> web.Response:
    """Add CORS headers and handle preflight requests."""
    if request.method == 'OPTIONS':
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e

    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, If-None-Match'
    response.headers['Access-Control-Expose-Headers'] = 'ETag, X-Progress'
    response.headers['Access-Control-Max-Age'] = '3600'

    return response


@web.middleware
async def metrics_middleware(request: web.Request, handler: Callable) -> web.Response:
    """Track request metrics and latency."""
    start_time = time.time()
    try:
        response = await handler(request)
        return response
    finally:
        latency_ms = (time.time() - start_time) * 1000
        metrics.record_request(latency_ms)


@web.middleware
async def rate_limit_middleware(request: web.Request, handler: Callable) -> web.Response:
    """Apply rate limiting."""
    client_id = request.remote or 'unknown'

    if not rate_limiter.is_allowed(client_id):
        return web.json_response(
            {"error": "Rate limit exceeded"},
            status=429,
            headers={"Retry-After": str(int(config.rate_limit_window))}
        )

    return await handler(request)


# =============================================================================
# Route Handlers
# =============================================================================

async def serve_loading_page(request: web.Request) -> web.Response:
    """Serve the main loading page."""
    loading_html = config.frontend_path / "loading.html"

    if not loading_html.exists():
        return web.Response(
            text="Loading page not found. Please ensure frontend/public/loading.html exists.",
            status=404
        )

    return web.FileResponse(loading_html)


async def serve_preview_page(request: web.Request) -> web.Response:
    """Serve the preview loading page."""
    preview_html = config.frontend_path / "loading_preview.html"

    if not preview_html.exists():
        return web.Response(
            text="Preview page not found.",
            status=404
        )

    return web.FileResponse(preview_html)


async def serve_loading_manager(request: web.Request) -> web.Response:
    """Serve the loading manager JavaScript."""
    loading_js = config.frontend_path / "loading-manager.js"

    if not loading_js.exists():
        return web.Response(
            text="Loading manager not found.",
            status=404
        )

    return web.FileResponse(loading_js)


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint with detailed status."""
    system_ready, reason = await health_checker.check_all_parallel()

    return web.json_response({
        "status": "ok" if system_ready else "degraded",
        "message": reason,
        "service": "jarvis_loading_server",
        "version": "3.0.0",
        "progress": progress_state.progress,
        "backend_ready": progress_state.backend_ready,
        "frontend_ready": progress_state.frontend_ready,
        "metrics": metrics.to_dict()
    })


async def get_metrics(request: web.Request) -> web.Response:
    """Get server metrics."""
    return web.json_response(metrics.to_dict())


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler for real-time progress updates."""
    ws = web.WebSocketResponse(
        heartbeat=config.ws_heartbeat_interval,
        receive_timeout=config.ws_heartbeat_timeout
    )
    await ws.prepare(request)

    # Check connection limit
    if not await connection_manager.add(ws):
        await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b'Server at capacity')
        return ws

    logger.info(f"[WebSocket] Client connected (total: {connection_manager.count})")

    try:
        # Send current progress immediately
        await ws.send_json(progress_state.to_dict())

        # Handle messages
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong', 'timestamp': datetime.now().isoformat()})
                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.debug(f'[WebSocket] Error: {ws.exception()}')

    except Exception as e:
        logger.debug(f"[WebSocket] Connection error: {e}")
    finally:
        await connection_manager.remove(ws)
        logger.info(f"[WebSocket] Client disconnected (total: {connection_manager.count})")

    return ws


async def get_progress(request: web.Request) -> web.Response:
    """HTTP endpoint for progress with ETag caching."""
    # Check If-None-Match for caching
    if_none_match = request.headers.get('If-None-Match')
    current_etag = progress_state.etag

    if if_none_match == current_etag:
        return web.Response(status=304)

    response = web.json_response(
        progress_state.to_dict(),
        headers={
            'ETag': current_etag,
            'Cache-Control': 'no-cache',
            'X-Progress': str(int(progress_state.progress))
        }
    )
    return response


async def get_progress_history(request: web.Request) -> web.Response:
    """Get progress history."""
    return web.json_response({
        "history": list(progress_state.history),
        "current": progress_state.to_dict()
    })


async def update_progress_endpoint(request: web.Request) -> web.Response:
    """HTTP endpoint for receiving progress updates from start_system.py."""
    try:
        data = await request.json()
        stage = data.get('stage', 'unknown')
        message = data.get('message', '')
        progress = float(data.get('progress', 0))
        metadata = data.get('metadata')

        # Update state
        changed = progress_state.update(stage, message, progress, metadata)

        # Track metrics
        metrics.progress_updates_received += 1
        metrics.last_progress_update = datetime.now()

        # Log significant changes
        if changed:
            logger.info(f"[Progress] {progress_state.progress:.0f}% - {stage}: {message}")

        # Broadcast to WebSocket clients
        await connection_manager.broadcast(progress_state.to_dict())

        return web.json_response({
            "status": "ok",
            "effective_progress": progress_state.progress,
            "changed": changed
        })

    except json.JSONDecodeError:
        metrics.record_error("Invalid JSON in progress update")
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        metrics.record_error(str(e))
        logger.error(f"[Update] Error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_options(request: web.Request) -> web.Response:
    """Handle OPTIONS preflight requests."""
    return web.Response(status=204)


# =============================================================================
# Watchdog System
# =============================================================================

async def system_health_watchdog():
    """
    WATCHDOG (not authority): Only kicks in if start_system.py fails to send updates.

    This watchdog does NOT compete with start_system.py. It only acts if:
    1. No progress updates received for 60+ seconds AND
    2. System appears to be ready (both backend + frontend responding)
    """
    logger.info("[Watchdog] Started (fallback only - start_system.py is authority)")

    # Wait before starting
    await asyncio.sleep(config.watchdog_startup_delay)

    last_update_time = metrics.last_progress_update or datetime.now()

    while True:
        try:
            # Check if we've received updates recently
            current_update = metrics.last_progress_update
            if current_update and current_update > last_update_time:
                last_update_time = current_update

            # Calculate silence duration
            silence_seconds = (datetime.now() - last_update_time).total_seconds()

            # Only intervene if extended silence and not complete
            if silence_seconds > config.watchdog_silence_threshold and progress_state.progress < 100:
                logger.warning(f"[Watchdog] No updates for {int(silence_seconds)}s, checking system...")

                # Check if system is ready
                system_ready, reason = await health_checker.check_all_parallel()

                if system_ready:
                    logger.info("[Watchdog] System ready but start_system.py silent - triggering completion")
                    progress_state.update(
                        "complete",
                        "JARVIS is online (watchdog recovery)",
                        100,
                        {
                            "success": True,
                            "redirect_url": f"http://localhost:{config.frontend_port}",
                            "backend_ready": True,
                            "frontend_ready": True,
                            "watchdog_triggered": True
                        }
                    )
                    await connection_manager.broadcast(progress_state.to_dict())
                    break
                else:
                    logger.debug(f"[Watchdog] System not ready: {reason}")

        except Exception as e:
            logger.debug(f"[Watchdog] Check failed: {e}")

        await asyncio.sleep(config.health_check_interval)

    logger.info("[Watchdog] Stopped")


# =============================================================================
# Application Lifecycle
# =============================================================================

async def on_startup(app: web.Application):
    """Initialize services on startup."""
    logger.info("Starting loading server services...")

    # Start health checker
    await health_checker.start()

    # Start WebSocket heartbeat
    await connection_manager.start_heartbeat()

    # Start watchdog
    app['watchdog_task'] = asyncio.create_task(system_health_watchdog())

    logger.info("All services started")


async def on_shutdown(app: web.Application):
    """Cleanup on shutdown."""
    logger.info("Shutting down loading server...")

    # Stop watchdog
    if 'watchdog_task' in app:
        app['watchdog_task'].cancel()
        try:
            await app['watchdog_task']
        except asyncio.CancelledError:
            pass

    # Stop heartbeat
    await connection_manager.stop_heartbeat()

    # Close all WebSocket connections
    await connection_manager.close_all()

    # Stop health checker
    await health_checker.stop()

    logger.info("Shutdown complete")


def create_app() -> web.Application:
    """Create and configure the application."""
    app = web.Application(
        middlewares=[
            cors_middleware,
            metrics_middleware,
            rate_limit_middleware
        ]
    )

    # Routes
    app.router.add_get('/', serve_loading_page)
    app.router.add_get('/loading.html', serve_loading_page)
    app.router.add_get('/preview', serve_preview_page)
    app.router.add_get('/loading-manager.js', serve_loading_manager)

    # Health and metrics
    app.router.add_get('/health', health_check)
    app.router.add_get('/health/ping', health_check)
    app.router.add_get('/metrics', get_metrics)

    # Progress endpoints
    app.router.add_get('/ws/startup-progress', websocket_handler)
    app.router.add_get('/api/startup-progress', get_progress)
    app.router.add_get('/api/progress-history', get_progress_history)
    app.router.add_post('/api/update-progress', update_progress_endpoint)

    # CORS preflight
    app.router.add_route('OPTIONS', '/{path:.*}', handle_options)

    # Lifecycle handlers
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Store references for external access
    app['progress_state'] = progress_state
    app['connection_manager'] = connection_manager
    app['metrics'] = metrics
    app['config'] = config

    return app


async def start_server(host: str = '0.0.0.0', port: Optional[int] = None):
    """Start the standalone loading server."""
    port = port or config.loading_port
    app = create_app()

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"{'='*60}")
    logger.info(f" JARVIS Loading Server v3.0 - Production Ready")
    logger.info(f"{'='*60}")
    logger.info(f" Server:      http://{host}:{port}")
    logger.info(f" WebSocket:   ws://{host}:{port}/ws/startup-progress")
    logger.info(f" HTTP API:    http://{host}:{port}/api/startup-progress")
    logger.info(f" Preview:     http://{host}:{port}/preview")
    logger.info(f" Metrics:     http://{host}:{port}/metrics")
    logger.info(f"{'='*60}")
    logger.info(f" CORS:        Enabled for all origins")
    logger.info(f" Rate Limit:  {config.rate_limit_requests} req/{config.rate_limit_window}s")
    logger.info(f" Max WS:      {config.ws_max_connections} connections")
    logger.info(f" Mode:        RELAY (start_system.py is authority)")
    logger.info(f" Watchdog:    Fallback after {config.watchdog_silence_threshold}s silence")
    logger.info(f"{'='*60}")

    return runner


async def shutdown_server(runner: web.AppRunner):
    """Gracefully shutdown the server."""
    await runner.cleanup()
    logger.info("Server stopped")


async def main():
    """Main entry point."""
    runner = await start_server()

    try:
        # Keep server running
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal...")
    finally:
        await shutdown_server(runner)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
