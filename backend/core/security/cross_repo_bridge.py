"""
Cross-Repository Security Bridge v1.0
======================================

Provides secure cross-repo communication and coordination for the Trinity ecosystem:
- JARVIS (Body) - Primary interface and execution
- JARVIS Prime (Mind) - Intelligence and decision making
- Reactor Core (Learning) - Training and model updates

Features:
- Mutual authentication between repos
- Secure token exchange
- Security policy synchronization
- Intrusion detection
- Security event propagation

Author: Trinity Security System
Version: 1.0.0
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)
import uuid

from backend.core.security.unified_engine import (
    SecurityConfig,
    AuthMethod,
    TokenType,
    Permission,
    Role,
    ComponentIdentity,
    AuditAction,
    AuthToken,
    Principal,
    AuditEntry,
    EncryptedMessage,
    JWTTokenManager,
    APIKeyManager,
    RBACManager,
    EncryptionManager,
    AuditLogger,
    UnifiedSecurityEngine,
    get_security_engine,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class SecurityEventType(Enum):
    """Types of security events for cross-repo communication."""
    # Authentication events
    AUTH_REQUEST = auto()
    AUTH_RESPONSE = auto()
    TOKEN_REFRESH = auto()
    TOKEN_REVOKE = auto()
    SESSION_START = auto()
    SESSION_END = auto()

    # Authorization events
    PERMISSION_GRANT = auto()
    PERMISSION_REVOKE = auto()
    ROLE_CHANGE = auto()

    # Security incidents
    INTRUSION_DETECTED = auto()
    BRUTE_FORCE_DETECTED = auto()
    ANOMALY_DETECTED = auto()
    POLICY_VIOLATION = auto()

    # Sync events
    POLICY_SYNC = auto()
    KEY_ROTATION = auto()
    CERT_ROTATION = auto()

    # Heartbeat
    HEARTBEAT = auto()


class TrustLevel(Enum):
    """Trust levels for cross-repo communication."""
    UNTRUSTED = 0
    LIMITED = 1
    STANDARD = 2
    ELEVATED = 3
    FULL = 4


class SecurityPolicyType(Enum):
    """Types of security policies."""
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    ENCRYPTION = "encryption"
    AUDIT = "audit"
    RATE_LIMIT = "rate_limit"


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class CrossRepoSecurityConfig:
    """Configuration for cross-repo security."""

    # Trust settings
    default_trust_level: str = os.getenv("CROSS_REPO_TRUST_LEVEL", "STANDARD")
    require_mutual_auth: bool = os.getenv("REQUIRE_MUTUAL_AUTH", "true").lower() == "true"

    # Sync settings
    policy_sync_interval: float = float(os.getenv("SECURITY_POLICY_SYNC_INTERVAL", "60.0"))
    heartbeat_interval: float = float(os.getenv("SECURITY_HEARTBEAT_INTERVAL", "10.0"))
    heartbeat_timeout: float = float(os.getenv("SECURITY_HEARTBEAT_TIMEOUT", "30.0"))

    # Intrusion detection
    max_failed_auths: int = int(os.getenv("MAX_FAILED_AUTHS", "5"))
    anomaly_detection_enabled: bool = os.getenv("ANOMALY_DETECTION", "true").lower() == "true"
    block_duration_minutes: int = int(os.getenv("BLOCK_DURATION_MINUTES", "30"))

    # Event settings
    event_queue_size: int = int(os.getenv("SECURITY_EVENT_QUEUE_SIZE", "10000"))
    event_retention_hours: float = float(os.getenv("SECURITY_EVENT_RETENTION_HOURS", "24.0"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class SecurityEvent:
    """A security event for cross-repo communication."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: SecurityEventType = SecurityEventType.HEARTBEAT
    source: ComponentIdentity = ComponentIdentity.JARVIS_BODY
    target: Optional[ComponentIdentity] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    priority: int = 1
    requires_ack: bool = False
    signature: str = ""


@dataclass
class RepoTrustInfo:
    """Trust information for a repository."""
    repo: ComponentIdentity
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    authenticated: bool = False
    last_auth: Optional[datetime] = None
    auth_token: Optional[AuthToken] = None
    failed_auth_count: int = 0
    blocked_until: Optional[datetime] = None
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    permissions: Set[Permission] = field(default_factory=set)


@dataclass
class SecurityPolicy:
    """A security policy for cross-repo enforcement."""
    policy_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    policy_type: SecurityPolicyType = SecurityPolicyType.AUTHENTICATION
    name: str = ""
    rules: Dict[str, Any] = field(default_factory=dict)
    applies_to: List[ComponentIdentity] = field(default_factory=list)
    priority: int = 1
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class IntrusionAlert:
    """An intrusion detection alert."""
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    severity: str = "warning"
    source: Optional[ComponentIdentity] = None
    description: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False


# =============================================================================
# SECURITY EVENT BUS
# =============================================================================


class SecurityEventBus:
    """
    Event bus for cross-repo security communication.

    Features:
    - Secure event publishing with signatures
    - Event filtering and routing
    - Priority-based delivery
    - Event acknowledgment
    """

    def __init__(self, config: CrossRepoSecurityConfig):
        self.config = config
        self.logger = logging.getLogger("SecurityEventBus")
        self._subscribers: Dict[SecurityEventType, List[Callable]] = defaultdict(list)
        self._global_subscribers: List[Callable] = []
        self._event_history: List[SecurityEvent] = []
        self._pending_acks: Dict[str, asyncio.Event] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=config.event_queue_size)
        self._lock = asyncio.Lock()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the event bus."""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())
        self.logger.info("Security event bus started")

    async def stop(self):
        """Stop the event bus."""
        if not self._running:
            return

        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Security event bus stopped")

    async def publish(
        self,
        event: SecurityEvent,
        wait_for_ack: bool = False,
        timeout: float = 30.0,
    ) -> bool:
        """Publish a security event."""
        # Sign the event
        event.signature = await self._sign_event(event)

        try:
            await self._queue.put(event)

            if wait_for_ack and event.requires_ack:
                ack_event = asyncio.Event()
                self._pending_acks[event.event_id] = ack_event

                try:
                    await asyncio.wait_for(ack_event.wait(), timeout=timeout)
                    return True
                except asyncio.TimeoutError:
                    self.logger.warning(f"Event ack timeout: {event.event_id}")
                    return False
                finally:
                    self._pending_acks.pop(event.event_id, None)

            return True

        except asyncio.QueueFull:
            self.logger.warning("Security event queue full")
            return False

    async def acknowledge(self, event_id: str):
        """Acknowledge an event."""
        if event_id in self._pending_acks:
            self._pending_acks[event_id].set()

    def subscribe(
        self,
        event_type: Optional[SecurityEventType] = None,
        callback: Callable = None,
    ):
        """Subscribe to security events."""
        if callback is None:
            return

        if event_type is None:
            self._global_subscribers.append(callback)
        else:
            self._subscribers[event_type].append(callback)

    def unsubscribe(
        self,
        event_type: Optional[SecurityEventType] = None,
        callback: Callable = None,
    ):
        """Unsubscribe from security events."""
        if callback is None:
            return

        if event_type is None:
            if callback in self._global_subscribers:
                self._global_subscribers.remove(callback)
        else:
            if callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    async def _process_events(self):
        """Process events from the queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )

                # Verify signature
                if not await self._verify_event(event):
                    self.logger.warning(f"Event signature verification failed: {event.event_id}")
                    continue

                # Store in history
                async with self._lock:
                    self._event_history.append(event)

                    # Prune old events
                    cutoff = datetime.utcnow() - timedelta(hours=self.config.event_retention_hours)
                    self._event_history = [
                        e for e in self._event_history if e.timestamp > cutoff
                    ]

                # Deliver to subscribers
                await self._deliver_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Event processing error: {e}")

    async def _deliver_event(self, event: SecurityEvent):
        """Deliver event to subscribers."""
        # Type-specific subscribers
        for callback in self._subscribers.get(event.event_type, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Subscriber callback error: {e}")

        # Global subscribers
        for callback in self._global_subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Global subscriber callback error: {e}")

    async def _sign_event(self, event: SecurityEvent) -> str:
        """Sign an event for integrity verification."""
        data = json.dumps({
            "event_id": event.event_id,
            "event_type": event.event_type.name,
            "source": event.source.value,
            "target": event.target.value if event.target else None,
            "payload": event.payload,
            "timestamp": event.timestamp.isoformat(),
        }, sort_keys=True)

        secret = os.getenv("SECURITY_EVENT_SECRET", "jarvis-security-events").encode()
        import hmac
        return hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()

    async def _verify_event(self, event: SecurityEvent) -> bool:
        """Verify event signature."""
        expected = await self._sign_event(event)
        import hmac
        return hmac.compare_digest(event.signature, expected)


# =============================================================================
# INTRUSION DETECTION
# =============================================================================


class IntrusionDetector:
    """
    Detects and responds to security intrusions.

    Features:
    - Failed authentication tracking
    - Anomaly detection
    - Automatic blocking
    - Alert generation
    """

    def __init__(self, config: CrossRepoSecurityConfig):
        self.config = config
        self.logger = logging.getLogger("IntrusionDetector")
        self._failed_auths: Dict[str, List[float]] = defaultdict(list)
        self._blocked: Dict[str, float] = {}
        self._alerts: List[IntrusionAlert] = []
        self._anomaly_baseline: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []

    async def record_auth_attempt(
        self,
        identity: str,
        source: ComponentIdentity,
        success: bool,
    ):
        """Record an authentication attempt."""
        async with self._lock:
            now = time.time()

            if not success:
                self._failed_auths[identity].append(now)

                # Clean old attempts
                cutoff = now - 300  # 5 minutes
                self._failed_auths[identity] = [
                    t for t in self._failed_auths[identity] if t > cutoff
                ]

                # Check for brute force
                if len(self._failed_auths[identity]) >= self.config.max_failed_auths:
                    await self._handle_brute_force(identity, source)

    async def _handle_brute_force(self, identity: str, source: ComponentIdentity):
        """Handle detected brute force attack."""
        # Block the identity
        block_until = time.time() + (self.config.block_duration_minutes * 60)
        self._blocked[identity] = block_until

        # Create alert
        alert = IntrusionAlert(
            severity="critical",
            source=source,
            description=f"Brute force attack detected from {identity}",
            evidence={
                "failed_attempts": len(self._failed_auths[identity]),
                "blocked_until": datetime.utcfromtimestamp(block_until).isoformat(),
            },
        )
        self._alerts.append(alert)

        # Notify callbacks
        await self._emit_alert(alert)

        self.logger.warning(f"Brute force detected - blocked {identity}")

    async def is_blocked(self, identity: str) -> bool:
        """Check if an identity is blocked."""
        async with self._lock:
            if identity not in self._blocked:
                return False

            if time.time() > self._blocked[identity]:
                del self._blocked[identity]
                return False

            return True

    async def detect_anomaly(
        self,
        metric_name: str,
        value: float,
        source: ComponentIdentity,
    ) -> bool:
        """Detect anomalies in security metrics."""
        if not self.config.anomaly_detection_enabled:
            return False

        async with self._lock:
            key = f"{source.value}:{metric_name}"

            # Get baseline
            if key not in self._anomaly_baseline:
                self._anomaly_baseline[key] = value
                return False

            baseline = self._anomaly_baseline[key]

            # Simple threshold-based detection
            deviation = abs(value - baseline) / max(baseline, 1)

            if deviation > 0.5:  # 50% deviation
                alert = IntrusionAlert(
                    severity="warning",
                    source=source,
                    description=f"Anomaly detected in {metric_name}",
                    evidence={
                        "metric": metric_name,
                        "value": value,
                        "baseline": baseline,
                        "deviation": deviation,
                    },
                )
                self._alerts.append(alert)
                await self._emit_alert(alert)
                return True

            # Update baseline (moving average)
            self._anomaly_baseline[key] = 0.9 * baseline + 0.1 * value
            return False

    def register_callback(self, callback: Callable):
        """Register alert callback."""
        self._callbacks.append(callback)

    async def _emit_alert(self, alert: IntrusionAlert):
        """Emit an intrusion alert."""
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(alert)
                else:
                    callback(alert)
            except Exception as e:
                self.logger.error(f"Alert callback error: {e}")

    async def get_alerts(
        self,
        severity: Optional[str] = None,
        resolved: Optional[bool] = None,
        limit: int = 100,
    ) -> List[IntrusionAlert]:
        """Get intrusion alerts."""
        async with self._lock:
            alerts = self._alerts.copy()

        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if resolved is not None:
            alerts = [a for a in alerts if a.resolved == resolved]

        return alerts[-limit:]


# =============================================================================
# CROSS-REPO SECURITY BRIDGE
# =============================================================================


class CrossRepoSecurityBridge:
    """
    Bridge for cross-repository security coordination.

    Manages:
    - Mutual authentication between repos
    - Token exchange and refresh
    - Security policy synchronization
    - Intrusion detection coordination
    """

    def __init__(self, config: Optional[CrossRepoSecurityConfig] = None):
        self.config = config or CrossRepoSecurityConfig()
        self.logger = logging.getLogger("CrossRepoSecurityBridge")

        # Components
        self.event_bus = SecurityEventBus(self.config)
        self.intrusion_detector = IntrusionDetector(self.config)

        # State
        self._running = False
        self._trust_info: Dict[ComponentIdentity, RepoTrustInfo] = {}
        self._policies: Dict[str, SecurityPolicy] = {}
        self._security_engine: Optional[UnifiedSecurityEngine] = None

        # Tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._policy_sync_task: Optional[asyncio.Task] = None

        # Locks
        self._lock = asyncio.Lock()

        # Register event handlers
        self.event_bus.subscribe(SecurityEventType.AUTH_REQUEST, self._handle_auth_request)
        self.event_bus.subscribe(SecurityEventType.AUTH_RESPONSE, self._handle_auth_response)
        self.event_bus.subscribe(SecurityEventType.HEARTBEAT, self._handle_heartbeat)
        self.event_bus.subscribe(SecurityEventType.POLICY_SYNC, self._handle_policy_sync)
        self.event_bus.subscribe(SecurityEventType.INTRUSION_DETECTED, self._handle_intrusion)

    async def initialize(self) -> bool:
        """Initialize the security bridge."""
        try:
            # Get security engine
            self._security_engine = await get_security_engine()

            # Initialize trust info for known repos
            for component in [
                ComponentIdentity.JARVIS_BODY,
                ComponentIdentity.JARVIS_PRIME,
                ComponentIdentity.REACTOR_CORE,
            ]:
                self._trust_info[component] = RepoTrustInfo(
                    repo=component,
                    trust_level=TrustLevel(
                        TrustLevel[self.config.default_trust_level].value
                    ),
                )

            self.logger.info("CrossRepoSecurityBridge initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start the security bridge."""
        if self._running:
            return

        self._running = True

        # Start event bus
        await self.event_bus.start()

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._policy_sync_task = asyncio.create_task(self._policy_sync_loop())

        self.logger.info("CrossRepoSecurityBridge started")

    async def stop(self):
        """Stop the security bridge."""
        if not self._running:
            return

        self._running = False

        # Cancel tasks
        for task in [self._heartbeat_task, self._policy_sync_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop event bus
        await self.event_bus.stop()

        self.logger.info("CrossRepoSecurityBridge stopped")

    async def shutdown(self):
        """Complete shutdown."""
        await self.stop()

    # =========================================================================
    # Authentication
    # =========================================================================

    async def authenticate_repo(
        self,
        target: ComponentIdentity,
    ) -> Optional[AuthToken]:
        """Authenticate with another repository."""
        if not self._security_engine:
            return None

        # Check if blocked
        if await self.intrusion_detector.is_blocked(target.value):
            self.logger.warning(f"Repo {target.value} is blocked")
            return None

        # Generate service token
        token = await self._security_engine.jwt.generate_token(
            subject=ComponentIdentity.JARVIS_BODY.value,
            component=ComponentIdentity.JARVIS_BODY,
            roles=[Role.SERVICE],
            permissions=[
                Permission.READ_DATA,
                Permission.WRITE_DATA,
                Permission.CROSS_REPO_SYNC,
            ],
            token_type=TokenType.SERVICE,
        )

        # Send auth request
        event = SecurityEvent(
            event_type=SecurityEventType.AUTH_REQUEST,
            source=ComponentIdentity.JARVIS_BODY,
            target=target,
            payload={
                "token": token.token,
                "requested_permissions": [p.value for p in [
                    Permission.READ_DATA,
                    Permission.WRITE_DATA,
                    Permission.CROSS_REPO_SYNC,
                ]],
            },
            requires_ack=True,
        )

        success = await self.event_bus.publish(event, wait_for_ack=True, timeout=30.0)

        if success:
            async with self._lock:
                self._trust_info[target].authenticated = True
                self._trust_info[target].last_auth = datetime.utcnow()
                self._trust_info[target].auth_token = token
                self._trust_info[target].failed_auth_count = 0

            await self.intrusion_detector.record_auth_attempt(
                target.value, target, True
            )

            return token
        else:
            async with self._lock:
                self._trust_info[target].failed_auth_count += 1

            await self.intrusion_detector.record_auth_attempt(
                target.value, target, False
            )

            return None

    async def verify_repo_auth(
        self,
        source: ComponentIdentity,
        token: str,
    ) -> Optional[Principal]:
        """Verify authentication from another repository."""
        if not self._security_engine:
            return None

        # Check if blocked
        if await self.intrusion_detector.is_blocked(source.value):
            self.logger.warning(f"Blocked repo attempted auth: {source.value}")
            return None

        # Verify token
        principal = await self._security_engine.authenticate_jwt(token)

        if principal:
            async with self._lock:
                self._trust_info[source].authenticated = True
                self._trust_info[source].last_auth = datetime.utcnow()
                self._trust_info[source].permissions = principal.permissions

            await self.intrusion_detector.record_auth_attempt(
                source.value, source, True
            )
        else:
            await self.intrusion_detector.record_auth_attempt(
                source.value, source, False
            )

        return principal

    async def get_trust_level(self, repo: ComponentIdentity) -> TrustLevel:
        """Get trust level for a repository."""
        async with self._lock:
            if repo in self._trust_info:
                return self._trust_info[repo].trust_level
            return TrustLevel.UNTRUSTED

    async def is_authenticated(self, repo: ComponentIdentity) -> bool:
        """Check if a repository is authenticated."""
        async with self._lock:
            if repo in self._trust_info:
                return self._trust_info[repo].authenticated
            return False

    # =========================================================================
    # Policy Management
    # =========================================================================

    async def add_policy(self, policy: SecurityPolicy):
        """Add or update a security policy."""
        async with self._lock:
            self._policies[policy.policy_id] = policy

        # Sync to other repos
        event = SecurityEvent(
            event_type=SecurityEventType.POLICY_SYNC,
            source=ComponentIdentity.JARVIS_BODY,
            payload={
                "action": "add",
                "policy": {
                    "policy_id": policy.policy_id,
                    "policy_type": policy.policy_type.value,
                    "name": policy.name,
                    "rules": policy.rules,
                    "priority": policy.priority,
                    "enabled": policy.enabled,
                },
            },
        )
        await self.event_bus.publish(event)

    async def remove_policy(self, policy_id: str):
        """Remove a security policy."""
        async with self._lock:
            if policy_id in self._policies:
                del self._policies[policy_id]

        event = SecurityEvent(
            event_type=SecurityEventType.POLICY_SYNC,
            source=ComponentIdentity.JARVIS_BODY,
            payload={
                "action": "remove",
                "policy_id": policy_id,
            },
        )
        await self.event_bus.publish(event)

    async def get_policies(
        self,
        policy_type: Optional[SecurityPolicyType] = None,
    ) -> List[SecurityPolicy]:
        """Get security policies."""
        async with self._lock:
            policies = list(self._policies.values())

        if policy_type:
            policies = [p for p in policies if p.policy_type == policy_type]

        return sorted(policies, key=lambda p: p.priority, reverse=True)

    # =========================================================================
    # Status
    # =========================================================================

    async def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        async with self._lock:
            repo_status = {}
            for repo, info in self._trust_info.items():
                repo_status[repo.value] = {
                    "trust_level": info.trust_level.name,
                    "authenticated": info.authenticated,
                    "last_auth": info.last_auth.isoformat() if info.last_auth else None,
                    "failed_auth_count": info.failed_auth_count,
                    "last_heartbeat": info.last_heartbeat.isoformat(),
                }

            return {
                "running": self._running,
                "repos": repo_status,
                "policies_count": len(self._policies),
                "intrusion_alerts": len(
                    await self.intrusion_detector.get_alerts(resolved=False)
                ),
            }

    async def get_health(self) -> Dict[ComponentIdentity, RepoTrustInfo]:
        """Get health status of all repos."""
        async with self._lock:
            return self._trust_info.copy()

    # =========================================================================
    # Event Handlers
    # =========================================================================

    async def _handle_auth_request(self, event: SecurityEvent):
        """Handle authentication request from another repo."""
        token = event.payload.get("token")
        if not token:
            return

        principal = await self.verify_repo_auth(event.source, token)

        # Send response
        response = SecurityEvent(
            event_type=SecurityEventType.AUTH_RESPONSE,
            source=ComponentIdentity.JARVIS_BODY,
            target=event.source,
            payload={
                "success": principal is not None,
                "request_id": event.event_id,
            },
        )
        await self.event_bus.publish(response)

        # Acknowledge original request
        if principal:
            await self.event_bus.acknowledge(event.event_id)

    async def _handle_auth_response(self, event: SecurityEvent):
        """Handle authentication response."""
        if event.payload.get("success"):
            request_id = event.payload.get("request_id")
            if request_id:
                await self.event_bus.acknowledge(request_id)

    async def _handle_heartbeat(self, event: SecurityEvent):
        """Handle heartbeat from another repo."""
        async with self._lock:
            if event.source in self._trust_info:
                self._trust_info[event.source].last_heartbeat = datetime.utcnow()

    async def _handle_policy_sync(self, event: SecurityEvent):
        """Handle policy sync from another repo."""
        action = event.payload.get("action")

        if action == "add":
            policy_data = event.payload.get("policy", {})
            policy = SecurityPolicy(
                policy_id=policy_data.get("policy_id", str(uuid.uuid4())),
                policy_type=SecurityPolicyType(policy_data.get("policy_type", "authentication")),
                name=policy_data.get("name", ""),
                rules=policy_data.get("rules", {}),
                priority=policy_data.get("priority", 1),
                enabled=policy_data.get("enabled", True),
            )
            async with self._lock:
                self._policies[policy.policy_id] = policy

        elif action == "remove":
            policy_id = event.payload.get("policy_id")
            if policy_id:
                async with self._lock:
                    self._policies.pop(policy_id, None)

    async def _handle_intrusion(self, event: SecurityEvent):
        """Handle intrusion event from another repo."""
        self.logger.warning(
            f"Intrusion reported from {event.source.value}: {event.payload}"
        )

        # Record in intrusion detector
        alert = IntrusionAlert(
            severity=event.payload.get("severity", "warning"),
            source=event.source,
            description=event.payload.get("description", ""),
            evidence=event.payload.get("evidence", {}),
        )

        await self.intrusion_detector._emit_alert(alert)

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _heartbeat_loop(self):
        """Periodic heartbeat to other repos."""
        while self._running:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)

                # Send heartbeat
                event = SecurityEvent(
                    event_type=SecurityEventType.HEARTBEAT,
                    source=ComponentIdentity.JARVIS_BODY,
                    payload={"timestamp": time.time()},
                )
                await self.event_bus.publish(event)

                # Check for stale repos
                await self._check_stale_repos()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat loop error: {e}")

    async def _policy_sync_loop(self):
        """Periodic policy synchronization."""
        while self._running:
            try:
                await asyncio.sleep(self.config.policy_sync_interval)

                # Sync all policies
                async with self._lock:
                    for policy in self._policies.values():
                        event = SecurityEvent(
                            event_type=SecurityEventType.POLICY_SYNC,
                            source=ComponentIdentity.JARVIS_BODY,
                            payload={
                                "action": "add",
                                "policy": {
                                    "policy_id": policy.policy_id,
                                    "policy_type": policy.policy_type.value,
                                    "name": policy.name,
                                    "rules": policy.rules,
                                    "priority": policy.priority,
                                    "enabled": policy.enabled,
                                },
                            },
                        )
                        await self.event_bus.publish(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Policy sync loop error: {e}")

    async def _check_stale_repos(self):
        """Check for repos that haven't sent heartbeat."""
        now = datetime.utcnow()
        timeout = timedelta(seconds=self.config.heartbeat_timeout)

        async with self._lock:
            for repo, info in self._trust_info.items():
                if info.authenticated and now - info.last_heartbeat > timeout:
                    info.authenticated = False
                    info.trust_level = TrustLevel.LIMITED
                    self.logger.warning(f"Repo {repo.value} heartbeat timeout")


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_bridge: Optional[CrossRepoSecurityBridge] = None
_bridge_lock = asyncio.Lock()


async def get_cross_repo_security_bridge() -> CrossRepoSecurityBridge:
    """Get or create the global security bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is None:
            _bridge = CrossRepoSecurityBridge()
            await _bridge.initialize()
        return _bridge


async def initialize_cross_repo_security() -> bool:
    """Initialize the global security bridge."""
    bridge = await get_cross_repo_security_bridge()
    await bridge.start()
    return True


async def shutdown_cross_repo_security():
    """Shutdown the global security bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is not None:
            await _bridge.shutdown()
            _bridge = None
            logger.info("Cross-repo security bridge shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "CrossRepoSecurityConfig",
    # Enums
    "SecurityEventType",
    "TrustLevel",
    "SecurityPolicyType",
    # Data Structures
    "SecurityEvent",
    "RepoTrustInfo",
    "SecurityPolicy",
    "IntrusionAlert",
    # Components
    "SecurityEventBus",
    "IntrusionDetector",
    # Bridge
    "CrossRepoSecurityBridge",
    # Global Functions
    "get_cross_repo_security_bridge",
    "initialize_cross_repo_security",
    "shutdown_cross_repo_security",
]
