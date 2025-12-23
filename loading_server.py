#!/usr/bin/env python3
"""
JARVIS Loading Server v4.0 - Zero-Touch Edition
================================================

Serves the loading page independently from frontend/backend during restart.
Provides real-time progress updates via WebSocket and HTTP polling.

Core Features:
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

v4.0 Zero-Touch Features:
- Zero-Touch autonomous update stage tracking
- Dead Man's Switch (DMS) status broadcasting
- Update classification awareness (security/critical/minor/major)
- Validation progress and results display
- Supervisor event integration
- AGI OS status relay

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
# Dynamic Path Resolver - No Hardcoding
# =============================================================================

class DynamicPathResolver:
    """
    Dynamically resolves paths for static files without hardcoding.
    Uses multiple strategies to find the correct path at runtime.
    """

    def __init__(self):
        self._cache: Dict[str, Path] = {}
        self._base_paths: List[Path] = []
        self._initialized = False

    def initialize(self):
        """Initialize base paths dynamically based on runtime context."""
        if self._initialized:
            return

        # Strategy 1: Environment variable (highest priority)
        env_path = os.getenv('FRONTEND_PATH')
        if env_path:
            self._base_paths.append(Path(env_path))

        # Strategy 2: Relative to this script file
        script_dir = Path(__file__).resolve().parent
        self._base_paths.append(script_dir / 'frontend' / 'public')

        # Strategy 3: Relative to current working directory
        cwd = Path.cwd()
        self._base_paths.append(cwd / 'frontend' / 'public')

        # Strategy 4: Walk up directory tree to find project root
        project_root = self._find_project_root(script_dir)
        if project_root:
            self._base_paths.append(project_root / 'frontend' / 'public')

        # Strategy 5: Check common development paths relative to home
        home = Path.home()
        for subdir in ['Documents/repos', 'Projects', 'Development', 'code', 'dev']:
            potential = home / subdir
            if potential.exists():
                # Find any JARVIS-AI-Agent directory
                for item in potential.iterdir():
                    if item.is_dir() and 'jarvis' in item.name.lower():
                        self._base_paths.append(item / 'frontend' / 'public')

        # Deduplicate while preserving order
        seen = set()
        unique_paths = []
        for p in self._base_paths:
            p_str = str(p.resolve()) if p.exists() else str(p)
            if p_str not in seen:
                seen.add(p_str)
                unique_paths.append(p)
        self._base_paths = unique_paths

        self._initialized = True
        logger.info(f"[PathResolver] Initialized with {len(self._base_paths)} search paths")

    def _find_project_root(self, start_path: Path, max_depth: int = 10) -> Optional[Path]:
        """Walk up directory tree to find project root (contains .git or package.json)."""
        current = start_path
        for _ in range(max_depth):
            if (current / '.git').exists() or (current / 'package.json').exists():
                return current
            if current.parent == current:
                break
            current = current.parent
        return None

    def resolve(self, filename: str) -> Optional[Path]:
        """
        Resolve a filename to its full path, checking cache first.
        Returns None if file not found in any search path.
        """
        # Check cache
        if filename in self._cache:
            cached = self._cache[filename]
            if cached.exists():
                return cached
            # Cache invalidated, remove it
            del self._cache[filename]

        self.initialize()

        # Search all base paths
        for base in self._base_paths:
            full_path = base / filename
            if full_path.exists() and full_path.is_file():
                self._cache[filename] = full_path
                logger.debug(f"[PathResolver] Resolved {filename} -> {full_path}")
                return full_path

        logger.warning(f"[PathResolver] Could not resolve: {filename}")
        logger.debug(f"[PathResolver] Searched paths: {self._base_paths}")
        return None

    def get_base_path(self) -> Optional[Path]:
        """Get the first valid base path that contains loading.html."""
        self.initialize()
        for base in self._base_paths:
            if (base / 'loading.html').exists():
                return base
        return self._base_paths[0] if self._base_paths else None

    @property
    def search_paths(self) -> List[Path]:
        """Get all search paths for debugging."""
        self.initialize()
        return self._base_paths.copy()


# Global path resolver instance
path_resolver = DynamicPathResolver()


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

    # Paths - Dynamically resolved, no hardcoding
    @property
    def frontend_path(self) -> Path:
        """Dynamically resolve frontend path."""
        base = path_resolver.get_base_path()
        if base:
            return base
        # Fallback to env or script-relative path
        return Path(os.getenv('FRONTEND_PATH', Path(__file__).resolve().parent / 'frontend' / 'public'))


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
class ZeroTouchState:
    """
    v4.0: Zero-Touch autonomous update state tracking.
    """
    active: bool = False
    state: str = "idle"  # initiated, staging, validating, applying, dms_monitoring, complete, failed
    classification: Optional[str] = None  # security, critical, minor, major, patch
    message: str = ""
    validation_progress: float = 0.0
    files_validated: int = 0
    total_files: int = 0
    commits: int = 0
    files_changed: int = 0
    validation_report: Optional[Dict] = None
    started_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "state": self.state,
            "classification": self.classification,
            "message": self.message,
            "validationProgress": self.validation_progress,
            "filesValidated": self.files_validated,
            "totalFiles": self.total_files,
            "commits": self.commits,
            "filesChanged": self.files_changed,
            "validationReport": self.validation_report,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class DMSState:
    """
    v4.0: Dead Man's Switch state tracking.
    """
    active: bool = False
    state: str = "idle"  # monitoring, passed, rolling_back
    health_score: float = 1.0
    probation_remaining: float = 0.0
    probation_total: float = 30.0
    consecutive_failures: int = 0
    started_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "state": self.state,
            "healthScore": self.health_score,
            "probationRemaining": self.probation_remaining,
            "probationTotal": self.probation_total,
            "consecutiveFailures": self.consecutive_failures,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class TwoTierSecurityState:
    """
    v5.0: Two-Tier Agentic Security System state tracking.

    Tracks the initialization and status of:
    - AgenticWatchdog (safety monitoring)
    - TieredCommandRouter (Tier 1/2 routing)
    - TieredVBIAAdapter (voice biometric authentication)
    """
    # Component readiness
    watchdog_ready: bool = False
    router_ready: bool = False
    vbia_adapter_ready: bool = False

    # Tier operational status
    tier1_operational: bool = False
    tier2_operational: bool = False

    # Watchdog status
    watchdog_status: str = "initializing"  # initializing, running, idle, critical
    watchdog_mode: str = "idle"  # idle, monitoring, armed

    # Router stats
    tier1_routes: int = 0
    tier2_routes: int = 0
    blocked_routes: int = 0

    # VBIA details
    vbia_tier1_threshold: float = 0.70
    vbia_tier2_threshold: float = 0.85
    vbia_liveness_enabled: bool = True
    vbia_anti_spoofing_ready: bool = False

    # Overall status
    overall_status: str = "initializing"  # initializing, partial, ready, error
    message: str = "Initializing Two-Tier Security..."

    def to_dict(self) -> Dict[str, Any]:
        return {
            "watchdogReady": self.watchdog_ready,
            "routerReady": self.router_ready,
            "vbiaAdapterReady": self.vbia_adapter_ready,
            "tier1Operational": self.tier1_operational,
            "tier2Operational": self.tier2_operational,
            "watchdogStatus": self.watchdog_status,
            "watchdogMode": self.watchdog_mode,
            "tier1Routes": self.tier1_routes,
            "tier2Routes": self.tier2_routes,
            "blockedRoutes": self.blocked_routes,
            "vbiaTier1Threshold": self.vbia_tier1_threshold,
            "vbiaTier2Threshold": self.vbia_tier2_threshold,
            "vbiaLivenessEnabled": self.vbia_liveness_enabled,
            "vbiaAntiSpoofingReady": self.vbia_anti_spoofing_ready,
            "overallStatus": self.overall_status,
            "message": self.message,
        }

    def update_overall_status(self):
        """Update overall status based on component states."""
        if self.watchdog_ready and self.router_ready and self.vbia_adapter_ready:
            self.overall_status = "ready"
            self.message = "Two-Tier Security System operational"
        elif self.watchdog_ready or self.router_ready or self.vbia_adapter_ready:
            self.overall_status = "partial"
            components = []
            if self.watchdog_ready:
                components.append("Watchdog")
            if self.router_ready:
                components.append("Router")
            if self.vbia_adapter_ready:
                components.append("VBIA")
            self.message = f"Partial: {', '.join(components)} ready"
        else:
            self.overall_status = "initializing"
            self.message = "Initializing Two-Tier Security..."


@dataclass
class ProgressState:
    """
    Thread-safe progress state with history tracking.

    v2.0 - Enhanced to sync with UnifiedStartupProgressHub.
    v4.0 - Added Zero-Touch and DMS state tracking.
    All state is now received from the hub as the single source of truth.
    """

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

    # Hub sync fields
    is_ready: bool = False
    components_ready: int = 0
    total_components: int = 0
    phase: str = "initializing"
    
    # v4.0: Zero-Touch state
    zero_touch: ZeroTouchState = field(default_factory=ZeroTouchState)
    dms: DMSState = field(default_factory=DMSState)

    # v5.0: Two-Tier Agentic Security
    two_tier: TwoTierSecurityState = field(default_factory=TwoTierSecurityState)

    # v4.0: Supervisor integration
    supervisor_connected: bool = False
    agi_os_enabled: bool = False

    def update(self, stage: str, message: str, progress: float, metadata: Optional[Dict] = None) -> bool:
        """Update progress with monotonic enforcement. Returns True if progress changed."""

        # Monotonic enforcement
        if stage == 'complete':
            effective_progress = 100.0
            self.max_progress_seen = 100.0
            self.is_ready = True
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
        self.phase = stage  # Alias for hub compatibility
        self.message = message
        self.progress = effective_progress
        self.timestamp = datetime.now()

        # Process metadata from hub
        if metadata:
            self.metadata = metadata
            # Extract hub-specific fields
            self.is_ready = metadata.get('is_ready', self.is_ready)
            self.components_ready = metadata.get('components_ready', self.components_ready)
            self.total_components = metadata.get('total_components', self.total_components)

            # Update ready flags from metadata
            if 'backend_ready' in metadata:
                self.backend_ready = metadata['backend_ready']
            if 'frontend_ready' in metadata:
                self.frontend_ready = metadata['frontend_ready']
            
            # v4.0: Zero-Touch state processing
            if 'zero_touch' in metadata:
                zt = metadata['zero_touch']
                self.zero_touch.active = zt.get('active', False)
                self.zero_touch.state = zt.get('state', self.zero_touch.state)
                self.zero_touch.classification = zt.get('classification')
                self.zero_touch.message = zt.get('message', '')
                self.zero_touch.validation_progress = zt.get('validation_progress', 0.0)
                self.zero_touch.files_validated = zt.get('files_validated', 0)
                self.zero_touch.total_files = zt.get('total_files', 0)
                self.zero_touch.commits = zt.get('commits', 0)
                self.zero_touch.files_changed = zt.get('files_changed', 0)
                self.zero_touch.validation_report = zt.get('validation_report')
                if zt.get('started_at'):
                    self.zero_touch.started_at = datetime.fromisoformat(zt['started_at'])
            
            # v4.0: DMS state processing
            if 'dms' in metadata:
                dms = metadata['dms']
                self.dms.active = dms.get('active', False)
                self.dms.state = dms.get('state', self.dms.state)
                self.dms.health_score = dms.get('health_score', 1.0)
                self.dms.probation_remaining = dms.get('probation_remaining', 0.0)
                self.dms.probation_total = dms.get('probation_total', 30.0)
                self.dms.consecutive_failures = dms.get('consecutive_failures', 0)

            # v5.0: Two-Tier Agentic Security state processing
            if 'two_tier' in metadata:
                tt = metadata['two_tier']
                self.two_tier.watchdog_ready = tt.get('watchdog_ready', False)
                self.two_tier.router_ready = tt.get('router_ready', False)
                self.two_tier.vbia_adapter_ready = tt.get('vbia_adapter_ready', False)
                self.two_tier.tier1_operational = tt.get('tier1_operational', False)
                self.two_tier.tier2_operational = tt.get('tier2_operational', False)
                self.two_tier.watchdog_status = tt.get('watchdog_status', 'initializing')
                self.two_tier.watchdog_mode = tt.get('watchdog_mode', 'idle')
                self.two_tier.tier1_routes = tt.get('tier1_routes', 0)
                self.two_tier.tier2_routes = tt.get('tier2_routes', 0)
                self.two_tier.blocked_routes = tt.get('blocked_routes', 0)
                self.two_tier.vbia_tier1_threshold = tt.get('vbia_tier1_threshold', 0.70)
                self.two_tier.vbia_tier2_threshold = tt.get('vbia_tier2_threshold', 0.85)
                self.two_tier.vbia_liveness_enabled = tt.get('vbia_liveness_enabled', True)
                self.two_tier.vbia_anti_spoofing_ready = tt.get('vbia_anti_spoofing_ready', False)
                self.two_tier.update_overall_status()

            # v4.0: Supervisor integration
            if 'supervisor_connected' in metadata:
                self.supervisor_connected = metadata['supervisor_connected']
            if 'agi_os_enabled' in metadata:
                self.agi_os_enabled = metadata['agi_os_enabled']

        # Track history
        self.history.append({
            "stage": stage,
            "message": message,
            "progress": effective_progress,
            "timestamp": self.timestamp.isoformat(),
            "components_ready": self.components_ready,
            "total_components": self.total_components
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
            "phase": self.phase,
            "message": self.message,
            "progress": self.progress,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "backend_ready": self.backend_ready,
            "frontend_ready": self.frontend_ready,
            "websocket_ready": self.websocket_ready,
            "is_ready": self.is_ready,
            "components_ready": self.components_ready,
            "total_components": self.total_components,
            # v4.0: Zero-Touch state
            "zero_touch": self.zero_touch.to_dict(),
            "dms": self.dms.to_dict(),
            # v5.0: Two-Tier Agentic Security
            "two_tier": self.two_tier.to_dict(),
            "supervisor_connected": self.supervisor_connected,
            "agi_os_enabled": self.agi_os_enabled,
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
    """Serve the main loading page using dynamic path resolution."""
    resolved = path_resolver.resolve("loading.html")

    if resolved:
        return web.FileResponse(resolved)

    # Log detailed error for debugging
    logger.error(f"[Static] Loading page not found")
    logger.error(f"[Static] Search paths: {path_resolver.search_paths}")
    return web.Response(
        text=f"Loading page not found. Searched {len(path_resolver.search_paths)} locations.",
        status=404
    )


async def serve_preview_page(request: web.Request) -> web.Response:
    """Serve the preview loading page using dynamic path resolution."""
    resolved = path_resolver.resolve("loading_preview.html")

    if resolved:
        return web.FileResponse(resolved)

    return web.Response(text="Preview page not found.", status=404)


async def serve_loading_manager(request: web.Request) -> web.Response:
    """Serve the loading manager JavaScript using dynamic path resolution."""
    resolved = path_resolver.resolve("loading-manager.js")

    if resolved:
        return web.FileResponse(resolved)

    logger.error(f"[Static] Loading manager not found")
    return web.Response(text="Loading manager not found.", status=404)


async def serve_static_file(request: web.Request) -> web.Response:
    """Serve static files using dynamic path resolution."""
    filename = request.match_info.get('filename', '')

    # Security: prevent path traversal
    if '..' in filename or filename.startswith('/'):
        return web.Response(text="Invalid path", status=400)

    resolved = path_resolver.resolve(filename)

    if resolved:
        return web.FileResponse(resolved)

    return web.Response(text=f"File not found: {filename}", status=404)


async def health_check(request: web.Request) -> web.Response:
    """
    Lightweight health check endpoint.
    
    This endpoint is called frequently by the supervisor to check if the
    loading server is running. It should return IMMEDIATELY without doing
    any heavy work like checking backend/frontend status.
    
    For detailed system status, use /api/status instead.
    """
    # Fast response - just confirm the server is running
    uptime = (datetime.now() - metrics.start_time).total_seconds()
    return web.json_response({
        "status": "ok",
        "message": "Loading server running",
        "service": "jarvis_loading_server",
        "version": "4.0.0",
        "uptime_seconds": round(uptime, 2),
    })


async def detailed_status(request: web.Request) -> web.Response:
    """
    Detailed status endpoint with full system health check.
    
    This does the heavy work of checking backend and frontend.
    Use /health for quick liveness checks.
    """
    system_ready, reason = await health_checker.check_all_parallel()

    return web.json_response({
        "status": "ok" if system_ready else "degraded",
        "message": reason,
        "service": "jarvis_loading_server",
        "version": "4.0.0",
        "progress": progress_state.progress,
        "backend_ready": progress_state.backend_ready,
        "frontend_ready": progress_state.frontend_ready,
        "is_ready": progress_state.is_ready,
        "components_ready": progress_state.components_ready,
        "total_components": progress_state.total_components,
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


async def get_ready_status(request: web.Request) -> web.Response:
    """
    Quick endpoint to check if system is truly ready.
    Use this before announcing 'ready' via voice or UI.
    """
    return web.json_response({
        "is_ready": progress_state.is_ready,
        "progress": progress_state.progress,
        "phase": progress_state.phase,
        "stage": progress_state.stage,
        "message": progress_state.message,
        "components_ready": progress_state.components_ready,
        "total_components": progress_state.total_components,
        "backend_ready": progress_state.backend_ready,
        "frontend_ready": progress_state.frontend_ready
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
# v4.0: Zero-Touch Update Endpoints
# =============================================================================

async def get_zero_touch_status(request: web.Request) -> web.Response:
    """
    v4.0: Get Zero-Touch autonomous update status.
    
    Returns the current state of any Zero-Touch update in progress,
    including validation status and DMS monitoring.
    """
    return web.json_response({
        "zero_touch": progress_state.zero_touch.to_dict(),
        "dms": progress_state.dms.to_dict(),
        "supervisor_connected": progress_state.supervisor_connected,
        "agi_os_enabled": progress_state.agi_os_enabled,
    })


async def update_zero_touch_status(request: web.Request) -> web.Response:
    """
    v4.0: Update Zero-Touch state from supervisor.
    
    Called by the JARVISSupervisor to broadcast Zero-Touch update progress.
    """
    try:
        data = await request.json()
        event_type = data.get('event', 'unknown')
        
        # Map event to state
        event_state_map = {
            'zero_touch_initiated': 'initiated',
            'zero_touch_staging': 'staging',
            'zero_touch_validating': 'validating',
            'zero_touch_validation_complete': 'applying' if data.get('passed') else 'validation_failed',
            'zero_touch_applying': 'applying',
            'zero_touch_complete': 'dms_monitoring',
            'zero_touch_failed': 'failed',
        }
        
        new_state = event_state_map.get(event_type, progress_state.zero_touch.state)
        
        # Update Zero-Touch state
        progress_state.zero_touch.active = data.get('active', event_type != 'zero_touch_failed')
        progress_state.zero_touch.state = new_state
        progress_state.zero_touch.classification = data.get('classification', progress_state.zero_touch.classification)
        progress_state.zero_touch.message = data.get('message', '')
        progress_state.zero_touch.validation_progress = data.get('validation_progress', progress_state.zero_touch.validation_progress)
        progress_state.zero_touch.files_validated = data.get('files_validated', progress_state.zero_touch.files_validated)
        progress_state.zero_touch.total_files = data.get('total_files', progress_state.zero_touch.total_files)
        progress_state.zero_touch.commits = data.get('commits', progress_state.zero_touch.commits)
        progress_state.zero_touch.files_changed = data.get('files_changed', progress_state.zero_touch.files_changed)
        
        if event_type == 'zero_touch_initiated':
            progress_state.zero_touch.started_at = datetime.now()
        
        if data.get('validation_report'):
            progress_state.zero_touch.validation_report = data['validation_report']
        
        # Broadcast to all WebSocket clients
        broadcast_data = {
            "type": event_type,
            **data,
            "zero_touch": progress_state.zero_touch.to_dict(),
        }
        await connection_manager.broadcast(broadcast_data)
        
        logger.info(f"[Zero-Touch] {event_type}: {progress_state.zero_touch.message}")
        
        return web.json_response({
            "status": "ok",
            "event": event_type,
            "zero_touch": progress_state.zero_touch.to_dict(),
        })
        
    except Exception as e:
        metrics.record_error(str(e))
        logger.error(f"[Zero-Touch] Update error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def update_dms_status(request: web.Request) -> web.Response:
    """
    v4.0: Update Dead Man's Switch status from supervisor.
    
    Called by the JARVISSupervisor to broadcast DMS heartbeats and state changes.
    """
    try:
        data = await request.json()
        event_type = data.get('event', 'dms_heartbeat')
        
        # Update DMS state
        if event_type == 'dms_probation_start':
            progress_state.dms.active = True
            progress_state.dms.state = 'monitoring'
            progress_state.dms.probation_total = data.get('probation_seconds', 30.0)
            progress_state.dms.probation_remaining = progress_state.dms.probation_total
            progress_state.dms.started_at = datetime.now()
            progress_state.dms.health_score = 1.0
            progress_state.dms.consecutive_failures = 0
            
        elif event_type == 'dms_heartbeat':
            progress_state.dms.health_score = data.get('health_score', progress_state.dms.health_score)
            progress_state.dms.probation_remaining = data.get('remaining_seconds', progress_state.dms.probation_remaining)
            progress_state.dms.consecutive_failures = data.get('consecutive_failures', progress_state.dms.consecutive_failures)
            
        elif event_type == 'dms_probation_passed':
            progress_state.dms.active = False
            progress_state.dms.state = 'passed'
            progress_state.dms.probation_remaining = 0.0
            progress_state.zero_touch.active = False
            progress_state.zero_touch.state = 'complete'
            
        elif event_type == 'dms_rollback_triggered':
            progress_state.dms.state = 'rolling_back'
            
        elif event_type == 'dms_rollback_complete':
            progress_state.dms.active = False
            progress_state.dms.state = 'idle'
            progress_state.zero_touch.active = False
            progress_state.zero_touch.state = 'idle'
        
        # Broadcast to all WebSocket clients
        broadcast_data = {
            "type": event_type,
            **data,
            "dms": progress_state.dms.to_dict(),
        }
        await connection_manager.broadcast(broadcast_data)
        
        if event_type != 'dms_heartbeat':
            logger.info(f"[DMS] {event_type}: {data.get('message', '')}")
        
        return web.json_response({
            "status": "ok",
            "event": event_type,
            "dms": progress_state.dms.to_dict(),
        })
        
    except Exception as e:
        metrics.record_error(str(e))
        logger.error(f"[DMS] Update error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# =============================================================================
# v5.0: Two-Tier Agentic Security Endpoints
# =============================================================================

async def get_two_tier_status(request: web.Request) -> web.Response:
    """
    v5.0: Get Two-Tier Agentic Security System status.

    Returns the current state of the Two-Tier Security System:
    - Watchdog status and mode
    - Router initialization and stats
    - VBIA adapter status and thresholds
    - Overall system readiness
    """
    return web.json_response({
        "two_tier": progress_state.two_tier.to_dict(),
        "supervisor_connected": progress_state.supervisor_connected,
        "agi_os_enabled": progress_state.agi_os_enabled,
    })


async def update_two_tier_status(request: web.Request) -> web.Response:
    """
    v5.0: Update Two-Tier Security status from supervisor.

    Called by the JARVISSupervisor to broadcast Two-Tier Security component
    initialization progress and status changes.

    Event types:
    - two_tier_watchdog_init: Watchdog starting initialization
    - two_tier_watchdog_ready: Watchdog initialized and ready
    - two_tier_router_init: Router starting initialization
    - two_tier_router_ready: Router initialized with tier routing
    - two_tier_vbia_init: VBIA adapter starting initialization
    - two_tier_vbia_ready: VBIA adapter ready with thresholds
    - two_tier_ready: All components ready
    - two_tier_error: Component initialization error
    """
    try:
        data = await request.json()
        event_type = data.get('event', 'unknown')

        # Update component states based on event type
        if event_type == 'two_tier_watchdog_init':
            progress_state.two_tier.watchdog_status = 'initializing'

        elif event_type == 'two_tier_watchdog_ready':
            progress_state.two_tier.watchdog_ready = True
            progress_state.two_tier.watchdog_status = data.get('status', 'running')
            progress_state.two_tier.watchdog_mode = data.get('mode', 'idle')

        elif event_type == 'two_tier_router_init':
            progress_state.two_tier.tier1_operational = False
            progress_state.two_tier.tier2_operational = False

        elif event_type == 'two_tier_router_ready':
            progress_state.two_tier.router_ready = True
            progress_state.two_tier.tier1_operational = data.get('tier1_ready', True)
            progress_state.two_tier.tier2_operational = data.get('tier2_ready', True)
            progress_state.two_tier.tier1_routes = data.get('tier1_routes', 0)
            progress_state.two_tier.tier2_routes = data.get('tier2_routes', 0)
            progress_state.two_tier.blocked_routes = data.get('blocked_routes', 0)

        elif event_type == 'two_tier_vbia_init':
            progress_state.two_tier.vbia_anti_spoofing_ready = False

        elif event_type == 'two_tier_vbia_ready':
            progress_state.two_tier.vbia_adapter_ready = True
            progress_state.two_tier.vbia_tier1_threshold = data.get('tier1_threshold', 0.70)
            progress_state.two_tier.vbia_tier2_threshold = data.get('tier2_threshold', 0.85)
            progress_state.two_tier.vbia_liveness_enabled = data.get('liveness_enabled', True)
            progress_state.two_tier.vbia_anti_spoofing_ready = data.get('anti_spoofing_ready', True)

        elif event_type == 'two_tier_ready':
            progress_state.two_tier.watchdog_ready = True
            progress_state.two_tier.router_ready = True
            progress_state.two_tier.vbia_adapter_ready = True
            progress_state.two_tier.tier1_operational = True
            progress_state.two_tier.tier2_operational = True

        elif event_type == 'two_tier_error':
            component = data.get('component', 'unknown')
            if component == 'watchdog':
                progress_state.two_tier.watchdog_status = 'error'
            elif component == 'router':
                progress_state.two_tier.tier1_operational = False
                progress_state.two_tier.tier2_operational = False
            elif component == 'vbia':
                progress_state.two_tier.vbia_anti_spoofing_ready = False

        # Update watchdog stats if provided
        if 'watchdog_mode' in data:
            progress_state.two_tier.watchdog_mode = data['watchdog_mode']

        # Update overall status
        progress_state.two_tier.update_overall_status()

        # Broadcast to all WebSocket clients
        broadcast_data = {
            "type": event_type,
            **data,
            "two_tier": progress_state.two_tier.to_dict(),
        }
        await connection_manager.broadcast(broadcast_data)

        logger.info(f"[Two-Tier] {event_type}: {progress_state.two_tier.message}")

        return web.json_response({
            "status": "ok",
            "event": event_type,
            "two_tier": progress_state.two_tier.to_dict(),
        })

    except Exception as e:
        metrics.record_error(str(e))
        logger.error(f"[Two-Tier] Update error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def supervisor_event_handler(request: web.Request) -> web.Response:
    """
    v4.0: Unified supervisor event handler.
    
    Handles all supervisor events including:
    - Zero-Touch updates
    - DMS status
    - Maintenance mode
    - System online/offline
    - Prime directive violations
    
    This is the main integration point between JARVISSupervisor and the loading server.
    """
    try:
        data = await request.json()
        event_type = data.get('type', data.get('event', 'unknown'))
        
        # Mark supervisor as connected
        progress_state.supervisor_connected = True
        
        # Route to appropriate handler based on event type
        if event_type.startswith('zero_touch_'):
            # Zero-Touch events
            progress_state.zero_touch.active = event_type != 'zero_touch_failed'
            if event_type == 'zero_touch_initiated':
                progress_state.zero_touch.state = 'initiated'
                progress_state.zero_touch.started_at = datetime.now()
            elif event_type == 'zero_touch_staging':
                progress_state.zero_touch.state = 'staging'
            elif event_type == 'zero_touch_validating':
                progress_state.zero_touch.state = 'validating'
                progress_state.zero_touch.validation_progress = data.get('progress', 0)
                progress_state.zero_touch.files_validated = data.get('files_validated', 0)
                progress_state.zero_touch.total_files = data.get('total_files', 0)
            elif event_type == 'zero_touch_applying':
                progress_state.zero_touch.state = 'applying'
            elif event_type == 'zero_touch_complete':
                progress_state.zero_touch.state = 'dms_monitoring'
            elif event_type == 'zero_touch_failed':
                progress_state.zero_touch.state = 'failed'
                progress_state.zero_touch.active = False
            
            progress_state.zero_touch.message = data.get('message', '')
            progress_state.zero_touch.classification = data.get('classification', progress_state.zero_touch.classification)
            
        elif event_type.startswith('dms_'):
            # DMS events
            if event_type == 'dms_probation_start':
                progress_state.dms.active = True
                progress_state.dms.state = 'monitoring'
                progress_state.dms.probation_total = data.get('probation_seconds', 30)
                progress_state.dms.probation_remaining = progress_state.dms.probation_total
                progress_state.dms.started_at = datetime.now()
            elif event_type == 'dms_heartbeat':
                progress_state.dms.health_score = data.get('health_score', 1.0)
                progress_state.dms.probation_remaining = data.get('remaining_seconds', 0)
                progress_state.dms.consecutive_failures = data.get('consecutive_failures', 0)
            elif event_type == 'dms_probation_passed':
                progress_state.dms.active = False
                progress_state.dms.state = 'passed'
                progress_state.zero_touch.state = 'complete'
                progress_state.zero_touch.active = False
            elif event_type == 'dms_rollback_triggered':
                progress_state.dms.state = 'rolling_back'
            elif event_type == 'dms_rollback_complete':
                progress_state.dms.active = False
                progress_state.dms.state = 'idle'
                progress_state.zero_touch.active = False
                
        elif event_type == 'system_updating':
            # Enter maintenance mode for update
            progress_state.phase = 'maintenance'
            progress_state.message = data.get('message', 'Updating JARVIS...')
            
        elif event_type == 'system_online':
            # System back online
            progress_state.phase = 'ready'
            progress_state.is_ready = True
            
        elif event_type == 'agi_os_status':
            # AGI OS status update
            progress_state.agi_os_enabled = data.get('enabled', False)
        
        # Broadcast to all WebSocket clients
        await connection_manager.broadcast({
            "type": event_type,
            **data,
            "zero_touch": progress_state.zero_touch.to_dict(),
            "dms": progress_state.dms.to_dict(),
        })
        
        return web.json_response({
            "status": "ok",
            "event": event_type,
            "processed": True,
        })
        
    except Exception as e:
        metrics.record_error(str(e))
        logger.error(f"[Supervisor Event] Error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


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

    # Static files (favicon, images, etc.) - catch-all for static assets
    app.router.add_get('/{filename:.*\\.(svg|png|jpg|jpeg|ico|css|woff|woff2|ttf)}', serve_static_file)

    # Health and metrics
    app.router.add_get('/health', health_check)         # Fast liveness check
    app.router.add_get('/health/ping', health_check)    # Alias for liveness
    app.router.add_get('/api/status', detailed_status)  # Detailed system status
    app.router.add_get('/metrics', get_metrics)

    # Progress endpoints
    app.router.add_get('/ws/startup-progress', websocket_handler)
    app.router.add_get('/api/startup-progress', get_progress)
    app.router.add_get('/api/startup-progress/ready', get_ready_status)
    app.router.add_get('/api/progress-history', get_progress_history)
    app.router.add_post('/api/update-progress', update_progress_endpoint)
    
    # v4.0: Zero-Touch and supervisor endpoints
    app.router.add_get('/api/zero-touch/status', get_zero_touch_status)
    app.router.add_post('/api/zero-touch/update', update_zero_touch_status)
    app.router.add_post('/api/dms/update', update_dms_status)
    app.router.add_post('/api/supervisor/event', supervisor_event_handler)

    # v5.0: Two-Tier Agentic Security endpoints
    app.router.add_get('/api/two-tier/status', get_two_tier_status)
    app.router.add_post('/api/two-tier/update', update_two_tier_status)

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

    # Initialize path resolver and verify paths
    path_resolver.initialize()
    resolved_loading = path_resolver.resolve("loading.html")
    resolved_manager = path_resolver.resolve("loading-manager.js")

    logger.info(f"{'='*60}")
    logger.info(f" JARVIS Loading Server v4.0 - Zero-Touch Edition")
    logger.info(f"{'='*60}")
    logger.info(f" Server:      http://{host}:{port}")
    logger.info(f" WebSocket:   ws://{host}:{port}/ws/startup-progress")
    logger.info(f" HTTP API:    http://{host}:{port}/api/startup-progress")
    logger.info(f"{'='*60}")
    logger.info(f" Path Resolution:")
    logger.info(f"   loading.html:      {'✓' if resolved_loading else '✗'} {resolved_loading or 'NOT FOUND'}")
    logger.info(f"   loading-manager.js: {'✓' if resolved_manager else '✗'} {resolved_manager or 'NOT FOUND'}")
    logger.info(f"   Search paths:      {len(path_resolver.search_paths)}")
    logger.info(f"{'='*60}")
    logger.info(f" v4.0 Zero-Touch Endpoints:")
    logger.info(f"   GET  /api/zero-touch/status")
    logger.info(f"   POST /api/zero-touch/update")
    logger.info(f"   POST /api/dms/update")
    logger.info(f"   POST /api/supervisor/event")
    logger.info(f"{'='*60}")
    logger.info(f" CORS:        Enabled for all origins")
    logger.info(f" Rate Limit:  {config.rate_limit_requests} req/{config.rate_limit_window}s")
    logger.info(f" Max WS:      {config.ws_max_connections} connections")
    logger.info(f" Mode:        RELAY (start_system.py + supervisor as authority)")
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


# =============================================================================
# StartupProgressReporter - For start_system.py to import and use
# =============================================================================

class StartupProgressReporter:
    """
    Robust progress reporter for use by start_system.py
    
    Features:
    - Async and fire-and-forget modes
    - Automatic retries with backoff
    - Never blocks main startup
    - Connection pooling
    
    Usage in start_system.py:
        from loading_server import StartupProgressReporter, start_loading_server_background
        
        await start_loading_server_background()
        reporter = StartupProgressReporter()
        await reporter.report("init", "Initializing JARVIS...", 5)
        await reporter.complete("JARVIS is online!")
    """
    
    def __init__(self, host: str = None, port: int = None):
        self.host = host or os.getenv('LOADING_SERVER_HOST', 'localhost')
        self.port = port or int(os.getenv('LOADING_SERVER_PORT', '3001'))
        self.timeout = float(os.getenv('PROGRESS_REQUEST_TIMEOUT', '2.0'))
        self.retry_count = int(os.getenv('PROGRESS_RETRY_COUNT', '3'))
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_progress = 0.0
        self._enabled = True
    
    @property
    def update_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/update-progress"
    
    async def _get_session(self) -> Optional[aiohttp.ClientSession]:
        if self._session is None or self._session.closed:
            try:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                )
            except Exception as e:
                logger.debug(f"Failed to create session: {e}")
                self._enabled = False
        return self._session
    
    async def report(
        self,
        stage: str,
        message: str,
        progress: float,
        metadata: Optional[Dict[str, Any]] = None,
        fire_and_forget: bool = True,
        log_entry: Optional[str] = None,
        log_source: Optional[str] = None,
        log_type: str = "info"
    ) -> bool:
        """
        Report progress to loading server with optional operation log.
        
        Args:
            stage: Current stage name (e.g., "init", "backend", "models")
            message: Human-readable status message
            progress: Percentage complete (0-100)
            metadata: Optional additional data
            fire_and_forget: If True, don't wait for response
            log_entry: Optional single log entry to add to operations log
            log_source: Source of the log entry (supervisor, backend, system)
            log_type: Type of log (info, success, error, warning)
        """
        if not self._enabled:
            return False
        
        # Monotonic enforcement
        if progress > self._last_progress:
            self._last_progress = progress
        else:
            progress = self._last_progress
        
        # Build metadata with log entry if provided
        meta = metadata.copy() if metadata else {}
        if log_entry:
            meta["log_entry"] = log_entry
            meta["log_source"] = log_source or "system"
            meta["log_type"] = log_type
        
        payload = {
            "stage": stage,
            "message": message,
            "progress": progress,
            "metadata": meta
        }
        
        if fire_and_forget:
            asyncio.create_task(self._send_with_retry(payload))
            return True
        return await self._send_with_retry(payload)
    
    async def _send_with_retry(self, payload: Dict[str, Any]) -> bool:
        session = await self._get_session()
        if not session:
            return False
        
        for attempt in range(self.retry_count):
            try:
                async with session.post(self.update_url, json=payload) as resp:
                    if resp.status in (200, 201):
                        return True
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.debug(f"Progress update failed: {e}")
            
            if attempt < self.retry_count - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
        return False
    
    async def complete(
        self,
        message: str = "JARVIS is online!",
        redirect_url: str = None,
        success: bool = True
    ) -> bool:
        """Report startup complete"""
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
        metadata = {
            "success": success,
            "redirect_url": redirect_url or frontend_url,
            "backend_ready": True,
            "frontend_ready": True
        }
        return await self.report("complete", message, 100.0, metadata, fire_and_forget=False)
    
    async def fail(self, message: str, error: str = None) -> bool:
        """Report startup failure"""
        return await self.report(
            "failed", message, self._last_progress,
            {"success": False, "error": error or message},
            fire_and_forget=False
        )
    
    async def log(
        self,
        source: str,
        message: str,
        log_type: str = "info"
    ) -> bool:
        """
        Add an entry to the live operations log without changing progress.
        
        Args:
            source: Source of the operation (supervisor, backend, system, etc.)
            message: Log message to display
            log_type: Type of log (info, success, error, warning)
            
        Example:
            await reporter.log("supervisor", "Checking for existing instances...", "info")
            await reporter.log("backend", "FastAPI application started", "success")
        """
        if not self._enabled:
            return False
        
        # Send a progress update at current level with just the log entry
        payload = {
            "stage": "_log",  # Special stage that doesn't change visual progress
            "message": "",
            "progress": self._last_progress,
            "metadata": {
                "log_entry": message,
                "log_source": source,
                "log_type": log_type,
                "is_log_only": True
            }
        }
        
        # Fire and forget for logs
        asyncio.create_task(self._send_with_retry(payload))
        return True
    
    async def log_batch(
        self,
        operations: list
    ) -> bool:
        """
        Add multiple entries to the operations log at once.
        
        Args:
            operations: List of dicts with {source, message, type}
            
        Example:
            await reporter.log_batch([
                {"source": "supervisor", "message": "Started cleanup", "type": "info"},
                {"source": "supervisor", "message": "Killed 2 processes", "type": "success"}
            ])
        """
        if not self._enabled or not operations:
            return False
        
        payload = {
            "stage": "_log_batch",
            "message": "",
            "progress": self._last_progress,
            "metadata": {
                "operations": operations,
                "is_log_only": True
            }
        }
        
        asyncio.create_task(self._send_with_retry(payload))
        return True
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    # =========================================================================
    # v4.0: Zero-Touch Update Reporting
    # =========================================================================
    
    async def zero_touch_event(
        self,
        event_type: str,
        message: str = "",
        classification: Optional[str] = None,
        validation_progress: float = 0.0,
        files_validated: int = 0,
        total_files: int = 0,
        commits: int = 0,
        files_changed: int = 0,
        validation_report: Optional[Dict] = None,
        **kwargs
    ) -> bool:
        """
        v4.0: Report Zero-Touch autonomous update event.
        
        Args:
            event_type: One of: zero_touch_initiated, zero_touch_staging, 
                       zero_touch_validating, zero_touch_applying, 
                       zero_touch_complete, zero_touch_failed
            message: Human-readable status message
            classification: Update type (security/critical/minor/major/patch)
            validation_progress: Validation progress (0-100)
            files_validated: Number of files validated
            total_files: Total files to validate
            commits: Number of commits in update
            files_changed: Number of files changed
            validation_report: Validation results dict
        """
        if not self._enabled:
            return False
        
        payload = {
            "event": event_type,
            "message": message,
            "classification": classification,
            "validation_progress": validation_progress,
            "files_validated": files_validated,
            "total_files": total_files,
            "commits": commits,
            "files_changed": files_changed,
            "validation_report": validation_report,
            "active": event_type != 'zero_touch_failed',
            **kwargs
        }
        
        try:
            session = await self._get_session()
            if session:
                url = f"http://{self.host}:{self.port}/api/zero-touch/update"
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug(f"Zero-Touch event failed: {e}")
        return False
    
    async def dms_event(
        self,
        event_type: str,
        health_score: float = 1.0,
        remaining_seconds: float = 0.0,
        probation_seconds: float = 30.0,
        consecutive_failures: int = 0,
        message: str = "",
        **kwargs
    ) -> bool:
        """
        v4.0: Report Dead Man's Switch event.
        
        Args:
            event_type: One of: dms_probation_start, dms_heartbeat, 
                       dms_probation_passed, dms_rollback_triggered, 
                       dms_rollback_complete
            health_score: Current health score (0.0-1.0)
            remaining_seconds: Remaining probation time
            probation_seconds: Total probation period
            consecutive_failures: Number of consecutive health check failures
            message: Human-readable status message
        """
        if not self._enabled:
            return False
        
        payload = {
            "event": event_type,
            "health_score": health_score,
            "remaining_seconds": remaining_seconds,
            "probation_seconds": probation_seconds,
            "consecutive_failures": consecutive_failures,
            "message": message,
            **kwargs
        }
        
        try:
            session = await self._get_session()
            if session:
                url = f"http://{self.host}:{self.port}/api/dms/update"
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug(f"DMS event failed: {e}")
        return False
    
    async def supervisor_event(
        self,
        event_type: str,
        **kwargs
    ) -> bool:
        """
        v4.0: Report generic supervisor event.
        
        Args:
            event_type: Event type identifier
            **kwargs: Additional event data
        """
        if not self._enabled:
            return False
        
        payload = {
            "type": event_type,
            **kwargs
        }
        
        try:
            session = await self._get_session()
            if session:
                url = f"http://{self.host}:{self.port}/api/supervisor/event"
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug(f"Supervisor event failed: {e}")
        return False


# Global reporter instance
_reporter: Optional[StartupProgressReporter] = None


def get_progress_reporter() -> StartupProgressReporter:
    """Get or create global reporter instance"""
    global _reporter
    if _reporter is None:
        _reporter = StartupProgressReporter()
    return _reporter


async def report_progress(stage: str, message: str, progress: float, metadata: Dict = None) -> bool:
    """Convenience function for quick progress reports"""
    return await get_progress_reporter().report(stage, message, progress, metadata)


async def report_complete(message: str = "JARVIS is online!", redirect_url: str = None) -> bool:
    """Convenience function for completion"""
    return await get_progress_reporter().complete(message, redirect_url)


async def report_failure(message: str, error: str = None) -> bool:
    """Convenience function for failure"""
    return await get_progress_reporter().fail(message, error)


# =============================================================================
# Loading Server Launcher - Start server in background for start_system.py
# =============================================================================

_loading_server_process = None


async def start_loading_server_background() -> bool:
    """
    Start loading server as a background subprocess.
    Call this at the very beginning of start_system.py.
    
    Returns True if server is running (either started or already running).
    """
    global _loading_server_process
    
    import subprocess
    
    host = os.getenv('LOADING_SERVER_HOST', 'localhost')
    port = int(os.getenv('LOADING_SERVER_PORT', '3001'))
    
    # Check if already running
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
            async with session.get(f"http://{host}:{port}/health") as resp:
                if resp.status == 200:
                    logger.info(f"Loading server already running on port {port}")
                    return True
    except:
        pass  # Not running, start it
    
    # Start the server
    try:
        logger.info(f"Starting loading server on port {port}...")
        script_path = Path(__file__).resolve()
        
        _loading_server_process = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        
        # Wait for server to be ready
        for _ in range(10):
            await asyncio.sleep(0.3)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
                    async with session.get(f"http://{host}:{port}/health") as resp:
                        if resp.status == 200:
                            logger.info("Loading server started successfully")
                            return True
            except:
                continue
        
        logger.warning("Loading server started but health check failed")
        return False
        
    except Exception as e:
        logger.error(f"Failed to start loading server: {e}")
        return False


async def stop_loading_server_background():
    """Stop the loading server subprocess"""
    global _loading_server_process
    if _loading_server_process:
        try:
            _loading_server_process.terminate()
            _loading_server_process.wait(timeout=5)
        except:
            try:
                _loading_server_process.kill()
            except:
                pass
        _loading_server_process = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

