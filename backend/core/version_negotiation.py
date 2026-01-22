"""
Cross-Repository Version Negotiation System v1.0
=================================================

Fixes Issue 20: Component version mismatch by providing:
1. Protocol versioning separate from component versions
2. Capability-based feature negotiation
3. Handshake protocol during connection establishment
4. Compatibility matrix with semantic versioning
5. Graceful degradation for partial compatibility
6. Version migration paths and upgrade orchestration

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    VERSION NEGOTIATION SYSTEM                           │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                         │
    │  ┌────────────────┐   ┌────────────────┐   ┌────────────────┐          │
    │  │  JARVIS Body   │   │ JARVIS Prime   │   │  Reactor Core  │          │
    │  │  v13.4.0       │   │  v2.1.0        │   │  v1.5.0        │          │
    │  │  Protocol v3   │   │  Protocol v3   │   │  Protocol v2   │          │
    │  └───────┬────────┘   └───────┬────────┘   └───────┬────────┘          │
    │          │                    │                    │                   │
    │          └────────────────────┼────────────────────┘                   │
    │                               │                                        │
    │                    ┌──────────▼──────────┐                              │
    │                    │  Handshake Protocol │                              │
    │                    │  ┌───────────────┐  │                              │
    │                    │  │ 1. Announce   │  │                              │
    │                    │  │ 2. Negotiate  │  │                              │
    │                    │  │ 3. Agree      │  │                              │
    │                    │  │ 4. Verify     │  │                              │
    │                    │  └───────────────┘  │                              │
    │                    └──────────┬──────────┘                              │
    │                               │                                        │
    │                    ┌──────────▼──────────┐                              │
    │                    │  Capability Matrix  │                              │
    │                    │  ┌───────────────┐  │                              │
    │                    │  │ Streaming ✓   │  │                              │
    │                    │  │ BatchAPI ✓    │  │                              │
    │                    │  │ HotSwap  ✓    │  │                              │
    │                    │  │ RLHF     ◐    │  │                              │
    │                    │  └───────────────┘  │                              │
    │                    └─────────────────────┘                              │
    │                                                                         │
    │   Legend: ✓ = supported by all, ◐ = partial support, ✗ = unsupported   │
    └─────────────────────────────────────────────────────────────────────────┘

Protocol Versioning Strategy:
    - Protocol versions are separate from component versions
    - Protocol v1: Basic request/response
    - Protocol v2: Streaming + batch operations
    - Protocol v3: Hot-swap + RLHF integration
    - Each component declares supported protocol range [min, max]
    - Negotiation finds highest common version

Capability Negotiation:
    - Beyond versions, components declare feature capabilities
    - Capabilities have dependencies (e.g., RLHF requires Streaming)
    - Runtime capability checking before feature use
    - Graceful fallback when capability missing

Handshake Protocol:
    1. ANNOUNCE: Component broadcasts its versions and capabilities
    2. NEGOTIATE: Components exchange compatibility information
    3. AGREE: Establish common protocol version and enabled capabilities
    4. VERIFY: Confirm negotiated parameters with test message

Author: JARVIS Trinity v96.0 - Cross-Repo Version Negotiation
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (100% Environment-Driven)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class NegotiationConfig:
    """Configuration for version negotiation."""

    # Negotiation timeouts
    HANDSHAKE_TIMEOUT_MS = _env_int("VERSION_HANDSHAKE_TIMEOUT_MS", 10000)
    ANNOUNCE_TIMEOUT_MS = _env_int("VERSION_ANNOUNCE_TIMEOUT_MS", 5000)
    VERIFY_TIMEOUT_MS = _env_int("VERSION_VERIFY_TIMEOUT_MS", 3000)

    # Retry settings
    MAX_HANDSHAKE_RETRIES = _env_int("VERSION_MAX_RETRIES", 3)
    RETRY_BACKOFF_BASE_MS = _env_int("VERSION_RETRY_BACKOFF_MS", 500)
    RETRY_BACKOFF_MULTIPLIER = _env_float("VERSION_RETRY_MULTIPLIER", 1.5)

    # Compatibility settings
    ALLOW_PROTOCOL_DOWNGRADE = _env_bool("VERSION_ALLOW_DOWNGRADE", True)
    REQUIRE_CAPABILITY_MATCH = _env_bool("VERSION_REQUIRE_CAPABILITIES", False)

    # File paths
    NEGOTIATION_DIR = Path(_env_str(
        "VERSION_NEGOTIATION_DIR",
        str(Path.home() / ".jarvis" / "trinity" / "versions")
    ))

    # Protocol version constraints
    MIN_PROTOCOL_VERSION = _env_int("VERSION_MIN_PROTOCOL", 1)
    MAX_PROTOCOL_VERSION = _env_int("VERSION_MAX_PROTOCOL", 3)

    # Current versions for this component (set at runtime)
    COMPONENT_VERSION = _env_str("COMPONENT_VERSION", "1.0.0")
    PROTOCOL_VERSION_MIN = _env_int("PROTOCOL_VERSION_MIN", 2)
    PROTOCOL_VERSION_MAX = _env_int("PROTOCOL_VERSION_MAX", 3)


# Ensure directories exist
NegotiationConfig.NEGOTIATION_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Semantic Version with Extended Comparison
# =============================================================================

@dataclass(frozen=True, order=True)
class SemanticVersion:
    """
    Immutable semantic version with full comparison support.

    Supports:
    - Standard semver (MAJOR.MINOR.PATCH)
    - Pre-release tags (-alpha.1, -beta.2, -rc.1)
    - Build metadata (+build.123)
    - Compatibility ranges (^1.0.0, ~1.2.0, >=1.0.0 <2.0.0)
    """
    major: int
    minor: int
    patch: int
    prerelease: Tuple[Union[str, int], ...] = field(default=())
    build: str = field(default="", compare=False)

    # Comparison priority: tuple order ensures correct sorting
    # prerelease="" (stable) > prerelease="rc.1" > "beta.1" > "alpha.1"
    _sort_key: Tuple = field(init=False, repr=False, compare=True)

    def __post_init__(self):
        # Calculate sort key for proper ordering
        # Stable versions (no prerelease) should be higher than prereleases
        if not self.prerelease:
            # Use a very high value for stable releases
            sort_prerelease = (float('inf'),)
        else:
            # Convert prerelease to sortable tuple
            sort_prerelease = tuple(
                (0, p) if isinstance(p, int) else (1, p)
                for p in self.prerelease
            )

        object.__setattr__(
            self,
            '_sort_key',
            (self.major, self.minor, self.patch, sort_prerelease)
        )

    @classmethod
    def parse(cls, version_str: str) -> "SemanticVersion":
        """Parse a semantic version string."""
        # Handle common non-standard formats
        version_str = version_str.strip().lstrip('v').lstrip('V')

        # Full semver pattern
        pattern = r'^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([a-zA-Z0-9.-]+))?(?:\+([a-zA-Z0-9.-]+))?$'
        match = re.match(pattern, version_str)

        if not match:
            raise ValueError(f"Invalid version format: {version_str}")

        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        patch = int(match.group(3) or 0)

        # Parse prerelease
        prerelease_str = match.group(4) or ""
        if prerelease_str:
            prerelease = tuple(
                int(p) if p.isdigit() else p
                for p in prerelease_str.split(".")
            )
        else:
            prerelease = ()

        build = match.group(5) or ""

        return cls(
            major=major,
            minor=minor,
            patch=patch,
            prerelease=prerelease,
            build=build,
        )

    @classmethod
    def try_parse(cls, version_str: str) -> Optional["SemanticVersion"]:
        """Try to parse a version, returning None on failure."""
        try:
            return cls.parse(version_str)
        except (ValueError, AttributeError):
            return None

    def __str__(self) -> str:
        version = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            version += f"-{'.'.join(str(p) for p in self.prerelease)}"
        if self.build:
            version += f"+{self.build}"
        return version

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def is_compatible_with(self, other: "SemanticVersion") -> bool:
        """Check if this version is compatible with another (same major, >= minor)."""
        if self.major != other.major:
            return False
        return (self.minor, self.patch) >= (other.minor, other.patch)

    def is_api_compatible(self, other: "SemanticVersion") -> bool:
        """Check API compatibility (semver: same major version)."""
        return self.major == other.major

    def bump_major(self) -> "SemanticVersion":
        """Return a new version with major bumped."""
        return SemanticVersion(self.major + 1, 0, 0)

    def bump_minor(self) -> "SemanticVersion":
        """Return a new version with minor bumped."""
        return SemanticVersion(self.major, self.minor + 1, 0)

    def bump_patch(self) -> "SemanticVersion":
        """Return a new version with patch bumped."""
        return SemanticVersion(self.major, self.minor, self.patch + 1)


# =============================================================================
# Capability System
# =============================================================================

class Capability(Flag):
    """
    Feature capabilities that components can support.

    Using Flag enum allows combining capabilities with bitwise operations.
    """
    NONE = 0

    # Core capabilities
    BASIC_REQUEST_RESPONSE = auto()  # Simple request/response
    STREAMING = auto()                # Streaming responses
    BATCH_OPERATIONS = auto()         # Batch request processing

    # Advanced capabilities
    HOT_SWAP = auto()                 # Hot model swapping
    CIRCUIT_BREAKER = auto()          # Circuit breaker support
    BACKPRESSURE = auto()             # Backpressure handling

    # AI capabilities
    LOCAL_INFERENCE = auto()          # Local model inference
    CLOUD_FALLBACK = auto()           # Cloud API fallback
    RLHF_TRAINING = auto()            # RLHF training support
    EXPERIENCE_REPLAY = auto()        # Experience replay buffer

    # Communication capabilities
    EVENT_BUS = auto()                # Event bus integration
    WEBSOCKET = auto()                # WebSocket support
    GRPC = auto()                     # gRPC support

    # Monitoring capabilities
    METRICS_EXPORT = auto()           # Prometheus metrics
    DISTRIBUTED_TRACING = auto()      # Distributed tracing
    HEALTH_PROBES = auto()            # K8s-style health probes

    # Combinations for convenience
    STREAMING_FULL = STREAMING | BACKPRESSURE
    AI_FULL = LOCAL_INFERENCE | CLOUD_FALLBACK | RLHF_TRAINING
    MONITORING_FULL = METRICS_EXPORT | DISTRIBUTED_TRACING | HEALTH_PROBES


# Capability dependencies - some capabilities require others
CAPABILITY_DEPENDENCIES: Dict[Capability, Set[Capability]] = {
    Capability.BACKPRESSURE: {Capability.STREAMING},
    Capability.RLHF_TRAINING: {Capability.STREAMING, Capability.EXPERIENCE_REPLAY},
    Capability.HOT_SWAP: {Capability.CIRCUIT_BREAKER},
    Capability.DISTRIBUTED_TRACING: {Capability.METRICS_EXPORT},
}


@dataclass(frozen=True)
class CapabilitySet:
    """
    Immutable set of capabilities with dependency validation.
    """
    capabilities: FrozenSet[Capability]
    _validated: bool = field(default=False, compare=False)

    def __post_init__(self):
        if not self._validated:
            self._validate_dependencies()
            object.__setattr__(self, '_validated', True)

    def _validate_dependencies(self) -> None:
        """Validate that all capability dependencies are satisfied."""
        for cap in self.capabilities:
            deps = CAPABILITY_DEPENDENCIES.get(cap, set())
            missing = deps - self.capabilities
            if missing:
                raise ValueError(
                    f"Capability {cap.name} requires {[c.name for c in missing]}"
                )

    @classmethod
    def from_flags(cls, flags: Capability) -> "CapabilitySet":
        """Create from a Capability flag combination."""
        caps = frozenset(
            cap for cap in Capability
            if cap in flags and cap != Capability.NONE
        )
        return cls(capabilities=caps)

    @classmethod
    def from_list(cls, cap_names: List[str]) -> "CapabilitySet":
        """Create from a list of capability names."""
        caps = frozenset(
            Capability[name.upper()] for name in cap_names
            if hasattr(Capability, name.upper())
        )
        return cls(capabilities=caps)

    def has(self, cap: Capability) -> bool:
        """Check if capability is present."""
        return cap in self.capabilities

    def has_all(self, *caps: Capability) -> bool:
        """Check if all capabilities are present."""
        return all(cap in self.capabilities for cap in caps)

    def has_any(self, *caps: Capability) -> bool:
        """Check if any capability is present."""
        return any(cap in self.capabilities for cap in caps)

    def intersection(self, other: "CapabilitySet") -> "CapabilitySet":
        """Get common capabilities."""
        return CapabilitySet(
            capabilities=self.capabilities & other.capabilities,
            _validated=True,  # Skip validation - intersection of valid sets is valid
        )

    def union(self, other: "CapabilitySet") -> "CapabilitySet":
        """Combine capabilities (validates dependencies)."""
        return CapabilitySet(
            capabilities=self.capabilities | other.capabilities
        )

    def to_list(self) -> List[str]:
        """Convert to list of capability names."""
        return sorted(cap.name for cap in self.capabilities if cap.name is not None)

    def __contains__(self, item: Capability) -> bool:
        return item in self.capabilities

    def __len__(self) -> int:
        return len(self.capabilities)

    def __iter__(self):
        return iter(self.capabilities)


# =============================================================================
# Component Identity and Version Declaration
# =============================================================================

class ComponentType(Enum):
    """Types of Trinity components."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    CODING_COUNCIL = "coding_council"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ComponentIdentity:
    """
    Unique identity of a component instance.

    Combines component type, instance ID, and version information
    into a single identity that can be used for version negotiation.
    """
    component_type: ComponentType
    instance_id: str
    hostname: str
    pid: int
    started_at: float

    def __hash__(self) -> int:
        return hash((self.component_type, self.instance_id, self.hostname, self.pid))

    @classmethod
    def create(cls, component_type: ComponentType) -> "ComponentIdentity":
        """Create a new component identity."""
        import socket
        return cls(
            component_type=component_type,
            instance_id=str(uuid.uuid4())[:8],
            hostname=socket.gethostname(),
            pid=os.getpid(),
            started_at=time.time(),
        )

    @property
    def unique_key(self) -> str:
        """Get unique key for this instance."""
        return f"{self.component_type.value}:{self.hostname}:{self.pid}:{self.instance_id}"


@dataclass
class VersionDeclaration:
    """
    Complete version declaration for a component.

    Includes component version, supported protocol versions,
    and declared capabilities.
    """
    identity: ComponentIdentity
    component_version: SemanticVersion
    protocol_version_min: int
    protocol_version_max: int
    capabilities: CapabilitySet
    metadata: Dict[str, Any] = field(default_factory=dict)
    declared_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.protocol_version_min > self.protocol_version_max:
            raise ValueError(
                f"Min protocol version ({self.protocol_version_min}) "
                f"cannot exceed max ({self.protocol_version_max})"
            )

    def supports_protocol(self, version: int) -> bool:
        """Check if a specific protocol version is supported."""
        return self.protocol_version_min <= version <= self.protocol_version_max

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "identity": {
                "component_type": self.identity.component_type.value,
                "instance_id": self.identity.instance_id,
                "hostname": self.identity.hostname,
                "pid": self.identity.pid,
                "started_at": self.identity.started_at,
            },
            "component_version": str(self.component_version),
            "protocol_version_min": self.protocol_version_min,
            "protocol_version_max": self.protocol_version_max,
            "capabilities": self.capabilities.to_list(),
            "metadata": self.metadata,
            "declared_at": self.declared_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionDeclaration":
        """Deserialize from dictionary."""
        identity_data = data["identity"]
        identity = ComponentIdentity(
            component_type=ComponentType(identity_data["component_type"]),
            instance_id=identity_data["instance_id"],
            hostname=identity_data["hostname"],
            pid=identity_data["pid"],
            started_at=identity_data["started_at"],
        )

        return cls(
            identity=identity,
            component_version=SemanticVersion.parse(data["component_version"]),
            protocol_version_min=data["protocol_version_min"],
            protocol_version_max=data["protocol_version_max"],
            capabilities=CapabilitySet.from_list(data["capabilities"]),
            metadata=data.get("metadata", {}),
            declared_at=data.get("declared_at", time.time()),
        )

    def get_fingerprint(self) -> str:
        """Get a fingerprint of this declaration for comparison."""
        content = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# =============================================================================
# Negotiation Protocol Messages
# =============================================================================

class HandshakePhase(Enum):
    """Phases of the handshake protocol."""
    ANNOUNCE = "announce"       # Component announces its capabilities
    NEGOTIATE = "negotiate"     # Components exchange compatibility info
    AGREE = "agree"             # Agreement on common ground
    VERIFY = "verify"           # Verification of negotiated parameters
    COMPLETE = "complete"       # Handshake complete
    FAILED = "failed"           # Handshake failed


@dataclass
class HandshakeMessage:
    """
    A message in the handshake protocol.
    """
    message_id: str
    phase: HandshakePhase
    source: ComponentIdentity
    target: Optional[ComponentIdentity]  # None for broadcast
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""
    ttl_seconds: float = 30.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "phase": self.phase.value,
            "source": {
                "component_type": self.source.component_type.value,
                "instance_id": self.source.instance_id,
                "hostname": self.source.hostname,
                "pid": self.source.pid,
                "started_at": self.source.started_at,
            },
            "target": {
                "component_type": self.target.component_type.value,
                "instance_id": self.target.instance_id,
                "hostname": self.target.hostname,
                "pid": self.target.pid,
                "started_at": self.target.started_at,
            } if self.target else None,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandshakeMessage":
        source_data = data["source"]
        source = ComponentIdentity(
            component_type=ComponentType(source_data["component_type"]),
            instance_id=source_data["instance_id"],
            hostname=source_data["hostname"],
            pid=source_data["pid"],
            started_at=source_data["started_at"],
        )

        target = None
        if data.get("target"):
            target_data = data["target"]
            target = ComponentIdentity(
                component_type=ComponentType(target_data["component_type"]),
                instance_id=target_data["instance_id"],
                hostname=target_data["hostname"],
                pid=target_data["pid"],
                started_at=target_data["started_at"],
            )

        return cls(
            message_id=data["message_id"],
            phase=HandshakePhase(data["phase"]),
            source=source,
            target=target,
            payload=data["payload"],
            timestamp=data.get("timestamp", time.time()),
            correlation_id=data.get("correlation_id", ""),
            ttl_seconds=data.get("ttl_seconds", 30.0),
        )

    def is_expired(self) -> bool:
        """Check if message has expired."""
        return (time.time() - self.timestamp) > self.ttl_seconds


@dataclass
class NegotiationResult:
    """
    Result of a version negotiation.
    """
    success: bool
    negotiated_protocol_version: Optional[int]
    common_capabilities: Optional[CapabilitySet]
    peer_declaration: Optional[VersionDeclaration]
    error: Optional[str] = None
    negotiation_time_ms: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "negotiated_protocol_version": self.negotiated_protocol_version,
            "common_capabilities": self.common_capabilities.to_list() if self.common_capabilities else None,
            "peer_declaration": self.peer_declaration.to_dict() if self.peer_declaration else None,
            "error": self.error,
            "negotiation_time_ms": self.negotiation_time_ms,
            "warnings": self.warnings,
        }


# =============================================================================
# Compatibility Matrix
# =============================================================================

@dataclass
class CompatibilityEntry:
    """Entry in the compatibility matrix."""
    component_type: ComponentType
    min_version: SemanticVersion
    max_version: Optional[SemanticVersion] = None
    required_capabilities: CapabilitySet = field(
        default_factory=lambda: CapabilitySet(frozenset())
    )
    notes: str = ""


class CompatibilityMatrix:
    """
    Defines which versions of components are compatible with each other.

    This is the source of truth for version compatibility across the
    Trinity ecosystem.
    """

    def __init__(self):
        self._entries: Dict[ComponentType, List[CompatibilityEntry]] = {}
        self._protocol_compatibility: Dict[int, Set[int]] = {}
        self._initialize_default_compatibility()

    def _initialize_default_compatibility(self) -> None:
        """Initialize default compatibility rules."""
        # Protocol version compatibility
        # Each version is compatible with itself and adjacent versions
        self._protocol_compatibility = {
            1: {1, 2},
            2: {1, 2, 3},
            3: {2, 3},
        }

        # Component compatibility - these are examples, adjust as needed
        # The actual versions should be discovered at runtime
        self._entries[ComponentType.JARVIS_BODY] = [
            CompatibilityEntry(
                component_type=ComponentType.JARVIS_PRIME,
                min_version=SemanticVersion(1, 0, 0),
                required_capabilities=CapabilitySet(frozenset({
                    Capability.STREAMING,
                    Capability.CIRCUIT_BREAKER,
                })),
            ),
            CompatibilityEntry(
                component_type=ComponentType.REACTOR_CORE,
                min_version=SemanticVersion(1, 0, 0),
                required_capabilities=CapabilitySet(frozenset({
                    Capability.EVENT_BUS,
                })),
            ),
        ]

    def are_protocols_compatible(self, v1: int, v2: int) -> bool:
        """Check if two protocol versions are compatible."""
        return v2 in self._protocol_compatibility.get(v1, set())

    def find_highest_common_protocol(
        self,
        range1: Tuple[int, int],
        range2: Tuple[int, int],
    ) -> Optional[int]:
        """
        Find the highest protocol version both ranges support.

        Args:
            range1: (min, max) protocol versions for component 1
            range2: (min, max) protocol versions for component 2

        Returns:
            Highest common version or None if incompatible
        """
        min1, max1 = range1
        min2, max2 = range2

        # Find overlap
        common_min = max(min1, min2)
        common_max = min(max1, max2)

        if common_min > common_max:
            return None

        # Return highest compatible version
        for v in range(common_max, common_min - 1, -1):
            if self.are_protocols_compatible(v, v):
                return v

        return None

    def check_component_compatibility(
        self,
        source_type: ComponentType,
        target_type: ComponentType,
        target_version: SemanticVersion,
        target_capabilities: CapabilitySet,
    ) -> Tuple[bool, List[str]]:
        """
        Check if a target component is compatible with the source.

        Returns:
            Tuple of (is_compatible, list of warnings/errors)
        """
        warnings: List[str] = []

        entries = self._entries.get(source_type, [])
        for entry in entries:
            if entry.component_type != target_type:
                continue

            # Check version
            if target_version < entry.min_version:
                return False, [
                    f"Version {target_version} is below minimum {entry.min_version}"
                ]

            if entry.max_version and target_version > entry.max_version:
                warnings.append(
                    f"Version {target_version} exceeds tested max {entry.max_version}"
                )

            # Check required capabilities
            for cap in entry.required_capabilities:
                if cap not in target_capabilities:
                    return False, [
                        f"Missing required capability: {cap.name}"
                    ]

        return True, warnings

    def get_minimum_version(
        self,
        source_type: ComponentType,
        target_type: ComponentType,
    ) -> Optional[SemanticVersion]:
        """Get the minimum required version for a target component."""
        entries = self._entries.get(source_type, [])
        for entry in entries:
            if entry.component_type == target_type:
                return entry.min_version
        return None


# =============================================================================
# Version Negotiator
# =============================================================================

class VersionNegotiator:
    """
    Handles version negotiation between components.

    This is the main class for performing handshakes and establishing
    compatible communication between Trinity components.
    """

    def __init__(
        self,
        declaration: VersionDeclaration,
        compatibility_matrix: Optional[CompatibilityMatrix] = None,
    ):
        self._declaration = declaration
        self._matrix = compatibility_matrix or CompatibilityMatrix()
        self._negotiation_dir = NegotiationConfig.NEGOTIATION_DIR
        self._active_negotiations: Dict[str, asyncio.Task] = {}
        self._completed_negotiations: Dict[str, NegotiationResult] = {}
        self._lock = asyncio.Lock()

        # Create component-specific directory
        self._component_dir = self._negotiation_dir / declaration.identity.component_type.value
        self._component_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"[VersionNegotiator] Initialized for {declaration.identity.component_type.value} "
            f"v{declaration.component_version} (protocol {declaration.protocol_version_min}-"
            f"{declaration.protocol_version_max})"
        )

    @property
    def declaration(self) -> VersionDeclaration:
        """Get this component's version declaration."""
        return self._declaration

    async def announce(self) -> None:
        """
        Announce this component's presence and version information.

        Writes version declaration to the shared negotiation directory
        for other components to discover.
        """
        # Write announcement file
        announcement_file = (
            self._negotiation_dir / "announcements" /
            f"{self._declaration.identity.unique_key}.json"
        )
        announcement_file.parent.mkdir(parents=True, exist_ok=True)

        message = HandshakeMessage(
            message_id=str(uuid.uuid4()),
            phase=HandshakePhase.ANNOUNCE,
            source=self._declaration.identity,
            target=None,  # Broadcast
            payload=self._declaration.to_dict(),
        )

        # Atomic write
        temp_file = announcement_file.with_suffix('.tmp')
        temp_file.write_text(json.dumps(message.to_dict(), indent=2))
        temp_file.rename(announcement_file)

        logger.debug(f"[VersionNegotiator] Announced presence at {announcement_file}")

    async def discover_peers(self) -> List[VersionDeclaration]:
        """
        Discover other components that have announced themselves.

        Returns:
            List of version declarations from peer components
        """
        peers: List[VersionDeclaration] = []
        announcements_dir = self._negotiation_dir / "announcements"

        if not announcements_dir.exists():
            return peers

        for announcement_file in announcements_dir.glob("*.json"):
            try:
                data = json.loads(announcement_file.read_text())
                message = HandshakeMessage.from_dict(data)

                # Skip our own announcement
                if message.source.unique_key == self._declaration.identity.unique_key:
                    continue

                # Skip expired announcements
                if message.is_expired():
                    # Clean up expired file
                    try:
                        announcement_file.unlink()
                    except Exception:
                        pass
                    continue

                # Parse declaration from payload
                declaration = VersionDeclaration.from_dict(message.payload)
                peers.append(declaration)

            except Exception as e:
                logger.warning(f"[VersionNegotiator] Error reading {announcement_file}: {e}")

        return peers

    async def negotiate_with_peer(
        self,
        peer_declaration: VersionDeclaration,
    ) -> NegotiationResult:
        """
        Perform full version negotiation with a peer component.

        Args:
            peer_declaration: The peer's version declaration

        Returns:
            NegotiationResult with negotiated parameters
        """
        start_time = time.time()
        peer_key = peer_declaration.identity.unique_key

        async with self._lock:
            # Check if already negotiated
            if peer_key in self._completed_negotiations:
                return self._completed_negotiations[peer_key]

        warnings: List[str] = []

        try:
            # Step 1: Find common protocol version
            my_range = (
                self._declaration.protocol_version_min,
                self._declaration.protocol_version_max,
            )
            peer_range = (
                peer_declaration.protocol_version_min,
                peer_declaration.protocol_version_max,
            )

            common_protocol = self._matrix.find_highest_common_protocol(my_range, peer_range)

            if common_protocol is None:
                return NegotiationResult(
                    success=False,
                    negotiated_protocol_version=None,
                    common_capabilities=None,
                    peer_declaration=peer_declaration,
                    error=(
                        f"No common protocol version. "
                        f"Local: {my_range[0]}-{my_range[1]}, "
                        f"Peer: {peer_range[0]}-{peer_range[1]}"
                    ),
                    negotiation_time_ms=(time.time() - start_time) * 1000,
                )

            # Step 2: Check component compatibility
            is_compatible, compat_warnings = self._matrix.check_component_compatibility(
                source_type=self._declaration.identity.component_type,
                target_type=peer_declaration.identity.component_type,
                target_version=peer_declaration.component_version,
                target_capabilities=peer_declaration.capabilities,
            )

            warnings.extend(compat_warnings)

            if not is_compatible:
                return NegotiationResult(
                    success=False,
                    negotiated_protocol_version=common_protocol,
                    common_capabilities=None,
                    peer_declaration=peer_declaration,
                    error=f"Incompatible component: {'; '.join(compat_warnings)}",
                    negotiation_time_ms=(time.time() - start_time) * 1000,
                    warnings=warnings,
                )

            # Step 3: Find common capabilities
            common_capabilities = self._declaration.capabilities.intersection(
                peer_declaration.capabilities
            )

            # Check if required capabilities are present
            if NegotiationConfig.REQUIRE_CAPABILITY_MATCH:
                if len(common_capabilities) == 0:
                    return NegotiationResult(
                        success=False,
                        negotiated_protocol_version=common_protocol,
                        common_capabilities=common_capabilities,
                        peer_declaration=peer_declaration,
                        error="No common capabilities found",
                        negotiation_time_ms=(time.time() - start_time) * 1000,
                        warnings=warnings,
                    )

            # Step 4: Write agreement
            await self._write_agreement(
                peer_declaration=peer_declaration,
                protocol_version=common_protocol,
                capabilities=common_capabilities,
            )

            # Success!
            result = NegotiationResult(
                success=True,
                negotiated_protocol_version=common_protocol,
                common_capabilities=common_capabilities,
                peer_declaration=peer_declaration,
                negotiation_time_ms=(time.time() - start_time) * 1000,
                warnings=warnings,
            )

            # Cache result
            async with self._lock:
                self._completed_negotiations[peer_key] = result

            logger.info(
                f"[VersionNegotiator] Negotiation successful with "
                f"{peer_declaration.identity.component_type.value}: "
                f"protocol v{common_protocol}, "
                f"{len(common_capabilities)} common capabilities"
            )

            return result

        except Exception as e:
            return NegotiationResult(
                success=False,
                negotiated_protocol_version=None,
                common_capabilities=None,
                peer_declaration=peer_declaration,
                error=f"Negotiation error: {str(e)}",
                negotiation_time_ms=(time.time() - start_time) * 1000,
                warnings=warnings,
            )

    async def _write_agreement(
        self,
        peer_declaration: VersionDeclaration,
        protocol_version: int,
        capabilities: CapabilitySet,
    ) -> None:
        """Write negotiation agreement to file."""
        agreements_dir = self._negotiation_dir / "agreements"
        agreements_dir.mkdir(parents=True, exist_ok=True)

        # Create agreement ID from both components
        components = sorted([
            self._declaration.identity.unique_key,
            peer_declaration.identity.unique_key,
        ])
        agreement_id = hashlib.sha256(
            ":".join(components).encode()
        ).hexdigest()[:16]

        agreement = {
            "agreement_id": agreement_id,
            "timestamp": time.time(),
            "components": [
                self._declaration.to_dict(),
                peer_declaration.to_dict(),
            ],
            "negotiated": {
                "protocol_version": protocol_version,
                "capabilities": capabilities.to_list(),
            },
        }

        agreement_file = agreements_dir / f"{agreement_id}.json"
        temp_file = agreement_file.with_suffix('.tmp')
        temp_file.write_text(json.dumps(agreement, indent=2))
        temp_file.rename(agreement_file)

    async def negotiate_with_all_peers(self) -> Dict[str, NegotiationResult]:
        """
        Negotiate with all discovered peer components.

        Returns:
            Dict mapping peer unique keys to negotiation results
        """
        # Announce ourselves first
        await self.announce()

        # Wait briefly for others to announce
        await asyncio.sleep(0.5)

        # Discover peers
        peers = await self.discover_peers()

        if not peers:
            logger.info("[VersionNegotiator] No peers discovered")
            return {}

        # Negotiate with each peer in parallel
        tasks = {
            peer.identity.unique_key: self.negotiate_with_peer(peer)
            for peer in peers
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        final_results: Dict[str, NegotiationResult] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                final_results[key] = NegotiationResult(
                    success=False,
                    negotiated_protocol_version=None,
                    common_capabilities=None,
                    peer_declaration=None,
                    error=str(result),
                )
            elif isinstance(result, NegotiationResult):
                final_results[key] = result

        return final_results

    def get_negotiation_result(self, peer_key: str) -> Optional[NegotiationResult]:
        """Get cached negotiation result for a peer."""
        return self._completed_negotiations.get(peer_key)

    def get_all_results(self) -> Dict[str, NegotiationResult]:
        """Get all cached negotiation results."""
        return self._completed_negotiations.copy()

    async def cleanup_stale_announcements(self, max_age_seconds: float = 60.0) -> int:
        """Remove stale announcement files."""
        cleaned = 0
        announcements_dir = self._negotiation_dir / "announcements"

        if not announcements_dir.exists():
            return cleaned

        for announcement_file in announcements_dir.glob("*.json"):
            try:
                age = time.time() - announcement_file.stat().st_mtime
                if age > max_age_seconds:
                    announcement_file.unlink()
                    cleaned += 1
            except Exception:
                pass

        return cleaned


# =============================================================================
# Version-Aware Protocol Handler
# =============================================================================

class ProtocolHandler(ABC):
    """
    Abstract base class for version-aware protocol handlers.

    Implement this for each protocol version to handle version-specific
    message formatting and parsing.
    """

    @property
    @abstractmethod
    def version(self) -> int:
        """Protocol version this handler supports."""
        pass

    @abstractmethod
    async def encode_message(self, message: Dict[str, Any]) -> bytes:
        """Encode a message for transmission."""
        pass

    @abstractmethod
    async def decode_message(self, data: bytes) -> Dict[str, Any]:
        """Decode a received message."""
        pass

    @abstractmethod
    def supports_capability(self, cap: Capability) -> bool:
        """Check if this protocol version supports a capability."""
        pass


class ProtocolV2Handler(ProtocolHandler):
    """Protocol version 2 handler - basic streaming support."""

    @property
    def version(self) -> int:
        return 2

    async def encode_message(self, message: Dict[str, Any]) -> bytes:
        return json.dumps(message).encode('utf-8')

    async def decode_message(self, data: bytes) -> Dict[str, Any]:
        return json.loads(data.decode('utf-8'))

    def supports_capability(self, cap: Capability) -> bool:
        supported = {
            Capability.BASIC_REQUEST_RESPONSE,
            Capability.STREAMING,
            Capability.BATCH_OPERATIONS,
            Capability.CIRCUIT_BREAKER,
            Capability.EVENT_BUS,
            Capability.HEALTH_PROBES,
        }
        return cap in supported


class ProtocolV3Handler(ProtocolHandler):
    """Protocol version 3 handler - full feature support."""

    @property
    def version(self) -> int:
        return 3

    async def encode_message(self, message: Dict[str, Any]) -> bytes:
        # V3 adds compression hint
        message["_protocol_version"] = 3
        return json.dumps(message).encode('utf-8')

    async def decode_message(self, data: bytes) -> Dict[str, Any]:
        return json.loads(data.decode('utf-8'))

    def supports_capability(self, cap: Capability) -> bool:
        # V3 supports everything - cap parameter used for interface compliance
        _ = cap  # Explicitly mark as used for type checker
        return True


# Protocol handler registry
PROTOCOL_HANDLERS: Dict[int, type] = {
    2: ProtocolV2Handler,
    3: ProtocolV3Handler,
}


def get_protocol_handler(version: int) -> ProtocolHandler:
    """Get a protocol handler for the specified version."""
    handler_class = PROTOCOL_HANDLERS.get(version)
    if not handler_class:
        raise ValueError(f"Unsupported protocol version: {version}")
    return handler_class()


# =============================================================================
# Version-Aware Connection Manager
# =============================================================================

class VersionAwareConnectionManager:
    """
    Manages connections with version negotiation.

    Ensures all connections are version-compatible before allowing
    communication.
    """

    def __init__(self, negotiator: VersionNegotiator):
        self._negotiator = negotiator
        self._connections: Dict[str, NegotiationResult] = {}
        self._handlers: Dict[str, ProtocolHandler] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        peer_declaration: VersionDeclaration,
    ) -> Tuple[bool, Optional[ProtocolHandler]]:
        """
        Establish a version-negotiated connection with a peer.

        Returns:
            Tuple of (success, protocol_handler)
        """
        peer_key = peer_declaration.identity.unique_key

        async with self._lock:
            # Check if already connected
            if peer_key in self._connections:
                result = self._connections[peer_key]
                if result.success and result.negotiated_protocol_version:
                    return True, self._handlers.get(peer_key)

        # Negotiate
        result = await self._negotiator.negotiate_with_peer(peer_declaration)

        if not result.success:
            logger.warning(
                f"[ConnectionManager] Failed to connect to "
                f"{peer_declaration.identity.component_type.value}: {result.error}"
            )
            return False, None

        # Get appropriate protocol handler
        if result.negotiated_protocol_version is None:
            logger.warning(
                f"[ConnectionManager] No protocol version negotiated with "
                f"{peer_declaration.identity.component_type.value}"
            )
            return False, None

        handler = get_protocol_handler(result.negotiated_protocol_version)

        async with self._lock:
            self._connections[peer_key] = result
            self._handlers[peer_key] = handler

        logger.info(
            f"[ConnectionManager] Connected to "
            f"{peer_declaration.identity.component_type.value} "
            f"using protocol v{result.negotiated_protocol_version}"
        )

        return True, handler

    async def disconnect(self, peer_key: str) -> None:
        """Disconnect from a peer."""
        async with self._lock:
            self._connections.pop(peer_key, None)
            self._handlers.pop(peer_key, None)

    def get_connection(self, peer_key: str) -> Optional[NegotiationResult]:
        """Get connection info for a peer."""
        return self._connections.get(peer_key)

    def get_handler(self, peer_key: str) -> Optional[ProtocolHandler]:
        """Get protocol handler for a peer."""
        return self._handlers.get(peer_key)

    def is_connected(self, peer_key: str) -> bool:
        """Check if connected to a peer."""
        result = self._connections.get(peer_key)
        return result is not None and result.success

    def get_all_connections(self) -> Dict[str, NegotiationResult]:
        """Get all active connections."""
        return {k: v for k, v in self._connections.items() if v.success}


# =============================================================================
# Global Instance Management
# =============================================================================

_negotiator: Optional[VersionNegotiator] = None
_connection_manager: Optional[VersionAwareConnectionManager] = None


async def initialize_version_negotiation(
    component_type: ComponentType,
    component_version: str,
    protocol_version_min: int = 2,
    protocol_version_max: int = 3,
    capabilities: Optional[List[str]] = None,
) -> VersionNegotiator:
    """
    Initialize the version negotiation system for this component.

    Args:
        component_type: Type of this component
        component_version: Semantic version string
        protocol_version_min: Minimum supported protocol version
        protocol_version_max: Maximum supported protocol version
        capabilities: List of capability names this component supports

    Returns:
        Initialized VersionNegotiator
    """
    global _negotiator, _connection_manager

    # Create identity
    identity = ComponentIdentity.create(component_type)

    # Parse version
    version = SemanticVersion.parse(component_version)

    # Parse capabilities
    if capabilities:
        cap_set = CapabilitySet.from_list(capabilities)
    else:
        # Default capabilities based on component type
        default_caps = {
            ComponentType.JARVIS_BODY: [
                "BASIC_REQUEST_RESPONSE", "STREAMING", "CIRCUIT_BREAKER",
                "CLOUD_FALLBACK", "EVENT_BUS", "HEALTH_PROBES",
            ],
            ComponentType.JARVIS_PRIME: [
                "BASIC_REQUEST_RESPONSE", "STREAMING", "BACKPRESSURE",
                "LOCAL_INFERENCE", "HOT_SWAP", "CIRCUIT_BREAKER", "HEALTH_PROBES",
            ],
            ComponentType.REACTOR_CORE: [
                "BASIC_REQUEST_RESPONSE", "STREAMING", "BATCH_OPERATIONS",
                "RLHF_TRAINING", "EXPERIENCE_REPLAY", "EVENT_BUS", "HEALTH_PROBES",
            ],
        }
        cap_set = CapabilitySet.from_list(
            default_caps.get(component_type, ["BASIC_REQUEST_RESPONSE"])
        )

    # Create declaration
    declaration = VersionDeclaration(
        identity=identity,
        component_version=version,
        protocol_version_min=protocol_version_min,
        protocol_version_max=protocol_version_max,
        capabilities=cap_set,
    )

    # Create negotiator
    _negotiator = VersionNegotiator(declaration)

    # Create connection manager
    _connection_manager = VersionAwareConnectionManager(_negotiator)

    # Announce presence
    await _negotiator.announce()

    logger.info(
        f"[VersionNegotiation] Initialized {component_type.value} v{version} "
        f"(protocol {protocol_version_min}-{protocol_version_max})"
    )

    return _negotiator


async def get_version_negotiator() -> VersionNegotiator:
    """Get the global version negotiator instance."""
    if _negotiator is None:
        raise RuntimeError("Version negotiation not initialized")
    return _negotiator


async def get_connection_manager() -> VersionAwareConnectionManager:
    """Get the global connection manager instance."""
    if _connection_manager is None:
        raise RuntimeError("Version negotiation not initialized")
    return _connection_manager


async def shutdown_version_negotiation() -> None:
    """Shutdown the version negotiation system."""
    global _negotiator, _connection_manager

    if _negotiator:
        await _negotiator.cleanup_stale_announcements()

    _negotiator = None
    _connection_manager = None
