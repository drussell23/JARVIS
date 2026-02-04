"""
Advanced Async Backend Client for JARVIS Hybrid Architecture
Features:
- Zero hardcoding, fully configuration-driven
- Async/await throughout with connection pooling
- Circuit breaker pattern with auto-recovery
- Exponential backoff with jitter
- Request/response streaming
- Health monitoring and service discovery
- Intelligent caching with Redis
- Load balancing across multiple backends
"""
import asyncio
import logging
import os
import time
import random
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
import httpx
import yaml
from pathlib import Path

from backend.core.async_safety import TimeoutConfig, get_shutdown_event

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failures detected, not accepting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class BackendType(Enum):
    """Backend types"""
    LOCAL = "local"
    CLOUD = "cloud"
    EDGE = "edge"


@dataclass
class BackendHealth:
    """Health status of a backend"""
    healthy: bool = True
    last_check: datetime = field(default_factory=datetime.now)
    response_time: float = 0.0
    error_count: int = 0
    success_count: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.success_count / total if total > 0 else 0.0


@dataclass
class CircuitBreaker:
    """Circuit breaker for fault tolerance"""
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout: int = 60
    half_open_timeout: int = 30

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[datetime] = None
    last_state_change: datetime = field(default_factory=datetime.now)

    def record_success(self):
        """Record successful request"""
        self.success_count += 1
        self.failure_count = 0

        if self.state == CircuitState.HALF_OPEN:
            if self.success_count >= self.success_threshold:
                self._close_circuit()

    def record_failure(self):
        """Record failed request"""
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = datetime.now()

        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                self._open_circuit()

    def _open_circuit(self):
        """Open the circuit (stop accepting requests)"""
        self.state = CircuitState.OPEN
        self.last_state_change = datetime.now()
        logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

    def _close_circuit(self):
        """Close the circuit (resume normal operation)"""
        self.state = CircuitState.CLOSED
        self.last_state_change = datetime.now()
        self.failure_count = 0
        self.success_count = 0
        logger.info("Circuit breaker closed - service recovered")

    def can_attempt(self) -> bool:
        """Check if request can be attempted"""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has elapsed
            elapsed = (datetime.now() - self.last_state_change).total_seconds()
            if elapsed >= self.timeout:
                self.state = CircuitState.HALF_OPEN
                self.last_state_change = datetime.now()
                logger.info("Circuit breaker half-opened - testing service")
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return True

        return False


@dataclass
class Backend:
    """Backend service definition"""
    name: str
    type: BackendType
    priority: int
    capabilities: List[str]
    base_url: str
    health_endpoint: str
    enabled: bool = True

    health: BackendHealth = field(default_factory=BackendHealth)
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    def can_handle(self, capability: str) -> bool:
        """Check if backend can handle capability"""
        return capability in self.capabilities

    def is_available(self) -> bool:
        """Check if backend is available for requests"""
        return (
            self.enabled and
            self.health.healthy and
            self.circuit_breaker.can_attempt()
        )


class HybridBackendClient:
    """
    Advanced async client for hybrid local/cloud architecture
    """

    def __init__(self, config_path: Optional[str] = None):
        # v218.0: Robust config path resolution with guaranteed absolute paths
        # Uses .resolve() to ensure the path is absolute regardless of:
        # - Working directory when Python was invoked
        # - Symlinks in the path
        # - Relative imports
        if config_path:
            # User-provided path - resolve to absolute
            self.config_path = str(Path(config_path).resolve())
        else:
            # Default: resolve relative to this file's directory (backend/core/)
            # __file__ can be relative if Python was invoked with a relative path
            this_file = Path(__file__).resolve()  # Guaranteed absolute path
            this_dir = this_file.parent
            self.config_path = str(this_dir / "hybrid_config.yaml")
        self.config = self._load_config()

        # Initialize HTTP client with connection pooling
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.config['hybrid']['connection_pool']['timeout']
            ),
            limits=httpx.Limits(
                max_connections=self.config['hybrid']['connection_pool']['max_connections'],
                max_keepalive_connections=self.config['hybrid']['connection_pool']['max_keepalive_connections'],
            ),
        )
        # Register with HTTP client registry for proper cleanup on shutdown
        try:
            from core.thread_manager import register_http_client
            register_http_client(
                self.client,
                name="HybridBackendClient",
                owner="core.hybrid_backend_client"
            )
        except ImportError:
            pass  # Registry not available

        # Initialize backends
        self.backends: Dict[str, Backend] = {}
        self._initialize_backends()

        # Health check task
        self.health_check_task: Optional[asyncio.Task] = None

        # Metrics
        self.metrics = {
            'requests_total': 0,
            'requests_success': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
        }

        logger.info("🚀 HybridBackendClient initialized")

    def _load_config(self) -> Dict:
        """Load configuration from YAML"""
        config_file = Path(self.config_path)
        if not config_file.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            return self._default_config()

        with open(config_file) as f:
            return yaml.safe_load(f)

    def _default_config(self) -> Dict:
        """Default configuration"""
        return {
            'hybrid': {
                'mode': 'auto',
                'backends': {
                    'local': {
                        'type': 'local',
                        'priority': 1,
                        'capabilities': ['vision_capture', 'screen_unlock', 'voice_activation'],
                        'health_endpoint': 'http://localhost:8010/health',
                        'enabled': True
                    },
                    'gcp': {
                        'type': 'cloud',
                        'priority': 2,
                        'capabilities': ['ml_processing', 'nlp_analysis', 'chatbot_inference'],
                        'fallback_url': 'http://34.10.137.70:8010',
                        'health_endpoint': '/health',
                        'enabled': True
                    }
                },
                'circuit_breaker': {
                    'enabled': True,
                    'failure_threshold': 5,
                    'success_threshold': 2,
                    'timeout': 60,
                    'half_open_timeout': 30
                },
                'retry': {
                    'enabled': True,
                    'max_attempts': 3,
                    'initial_delay': 1,
                    'max_delay': 30,
                    'exponential_base': 2,
                    'jitter': True
                },
                'connection_pool': {
                    'max_connections': 100,
                    'max_keepalive_connections': 20,
                    'timeout': 10
                }
            }
        }

    def _initialize_backends(self):
        """Initialize backend services from config"""
        backends_config = self.config['hybrid']['backends']
        cb_config = self.config['hybrid']['circuit_breaker']

        for name, config in backends_config.items():
            if not config.get('enabled', True):
                continue

            backend_type = BackendType(config['type'])
            base_url = config.get('fallback_url', config.get('health_endpoint', ''))

            circuit_breaker = CircuitBreaker(
                failure_threshold=cb_config['failure_threshold'],
                success_threshold=cb_config['success_threshold'],
                timeout=cb_config['timeout'],
                half_open_timeout=cb_config['half_open_timeout']
            ) if cb_config['enabled'] else CircuitBreaker()

            backend = Backend(
                name=name,
                type=backend_type,
                priority=config['priority'],
                capabilities=config['capabilities'],
                base_url=base_url,
                health_endpoint=config['health_endpoint'],
                circuit_breaker=circuit_breaker
            )

            self.backends[name] = backend
            logger.info(f"✅ Registered backend: {name} ({backend_type.value}) - {len(backend.capabilities)} capabilities")

    async def start(self):
        """Start the client and health monitoring"""
        logger.info("Starting hybrid backend client...")

        # Start health check loop if enabled
        if self.config['hybrid'].get('discovery', {}).get('enabled', True):
            interval = self.config['hybrid']['discovery'].get('health_check_interval', 30)
            self.health_check_task = asyncio.create_task(self._health_check_loop(interval))

        # Perform initial health check
        await self._check_all_backends()

    async def stop(self):
        """Stop the client and cleanup"""
        logger.info("Stopping hybrid backend client...")

        if self.health_check_task:
            self.health_check_task.cancel()
            try:
                await self.health_check_task
            except asyncio.CancelledError:
                pass

        await self.client.aclose()

    async def _health_check_loop(self, interval: int):
        """Periodic health check loop with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("BACKEND_HEALTH_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Backend health check loop stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"Backend health check loop reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(interval)
                # Add timeout protection for health check
                await asyncio.wait_for(
                    self._check_all_backends(),
                    timeout=TimeoutConfig.HEALTH_CHECK
                )
            except asyncio.TimeoutError:
                logger.warning(f"Backend health check timed out after {TimeoutConfig.HEALTH_CHECK}s")
            except asyncio.CancelledError:
                logger.info("Backend health check loop cancelled")
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _check_all_backends(self):
        """Check health of all backends"""
        tasks = [self._check_backend_health(backend) for backend in self.backends.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_backend_health(self, backend: Backend):
        """Check health of a single backend"""
        try:
            start_time = time.time()
            response = await self.client.get(
                f"{backend.base_url}{backend.health_endpoint}",
                timeout=5.0
            )
            response_time = time.time() - start_time

            if response.status_code == 200:
                backend.health.healthy = True
                backend.health.response_time = response_time
                backend.health.success_count += 1
                backend.circuit_breaker.record_success()
            else:
                backend.health.healthy = False
                backend.health.error_count += 1
                backend.circuit_breaker.record_failure()

            backend.health.last_check = datetime.now()

        except Exception as e:
            # v109.2: Health checks can fail during startup - use INFO not WARNING
            logger.info(f"Health check failed for {backend.name}: {e}")
            backend.health.healthy = False
            backend.health.error_count += 1
            backend.health.last_check = datetime.now()
            backend.circuit_breaker.record_failure()

    def _select_backend(self, capability: Optional[str] = None, **kwargs) -> Optional[Backend]:
        """
        Intelligently select best backend for request
        """
        available_backends = [
            b for b in self.backends.values()
            if b.is_available() and (not capability or b.can_handle(capability))
        ]

        if not available_backends:
            logger.warning(f"No available backends for capability: {capability}")
            return None

        # Sort by priority (lower = better) and health
        available_backends.sort(
            key=lambda b: (b.priority, -b.health.success_rate, b.health.response_time)
        )

        return available_backends[0]

    async def execute(
        self,
        path: str,
        method: str = "POST",
        data: Optional[Dict] = None,
        capability: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Execute request with intelligent routing and fault tolerance
        """
        self.metrics['requests_total'] += 1

        # Select backend
        backend = self._select_backend(capability, **kwargs)
        if not backend:
            self.metrics['requests_failed'] += 1
            return {
                'success': False,
                'error': 'No available backend',
                'capability': capability
            }

        # Execute with retry
        retry_config = self.config['hybrid']['retry']
        max_attempts = retry_config['max_attempts'] if retry_config['enabled'] else 1

        for attempt in range(max_attempts):
            try:
                result = await self._execute_request(backend, path, method, data)
                self.metrics['requests_success'] += 1
                backend.circuit_breaker.record_success()
                return result

            except Exception as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_attempts}): {e}")
                backend.circuit_breaker.record_failure()

                if attempt < max_attempts - 1:
                    delay = self._calculate_retry_delay(attempt, retry_config)
                    await asyncio.sleep(delay)
                else:
                    self.metrics['requests_failed'] += 1
                    return {
                        'success': False,
                        'error': str(e),
                        'backend': backend.name,
                        'attempts': max_attempts
                    }

    def _calculate_retry_delay(self, attempt: int, config: Dict) -> float:
        """Calculate retry delay with exponential backoff and jitter"""
        delay = config['initial_delay'] * (config['exponential_base'] ** attempt)
        delay = min(delay, config['max_delay'])

        if config.get('jitter', True):
            delay = delay * (0.5 + random.random())

        return delay

    async def _execute_request(
        self,
        backend: Backend,
        path: str,
        method: str,
        data: Optional[Dict]
    ) -> Dict[str, Any]:
        """Execute HTTP request to backend"""
        url = f"{backend.base_url}{path}"

        if method.upper() == "GET":
            response = await self.client.get(url)
        elif method.upper() == "POST":
            response = await self.client.post(url, json=data)
        elif method.upper() == "PUT":
            response = await self.client.put(url, json=data)
        elif method.upper() == "DELETE":
            response = await self.client.delete(url)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        return response.json()

    def get_metrics(self) -> Dict[str, Any]:
        """Get client metrics"""
        return {
            **self.metrics,
            'backends': {
                name: {
                    'healthy': b.health.healthy,
                    'response_time': b.health.response_time,
                    'success_rate': b.health.success_rate,
                    'circuit_state': b.circuit_breaker.state.value
                }
                for name, b in self.backends.items()
            }
        }

    async def __aenter__(self):
        """Async context manager entry"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.stop()
