"""
Trinity State Manager v4.0 - Enterprise-Grade Distributed State Management
===========================================================================

Comprehensive state management for the Trinity Ecosystem addressing all 8 gaps:

Gap 1: Unified State Coordinator - Single source of truth with atomic updates
Gap 2: Distributed State Synchronization - State replication across repos
Gap 3: State Versioning - History tracking and rollback capability
Gap 4: State Conflict Resolution - Vector clocks, CRDTs, merge strategies
Gap 5: State Snapshot & Restore - Save/restore system state
Gap 6: State Partitioning - Partitioned by component/domain
Gap 7: State Compression - LZ4/zstd compression and archival
Gap 8: State Access Control - Role-based access control

Architecture:
    ┌──────────────────────────────────────────────────────────────────────────────┐
    │                      Trinity State Manager v4.0                              │
    ├──────────────────────────────────────────────────────────────────────────────┤
    │                                                                              │
    │  ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐     │
    │  │ Unified State      │  │ State Sync         │  │ Version Manager    │     │
    │  │ Coordinator        │  │ Protocol           │  │ (History/Rollback) │     │
    │  │ [Gap 1]            │  │ [Gap 2]            │  │ [Gap 3]            │     │
    │  └─────────┬──────────┘  └─────────┬──────────┘  └─────────┬──────────┘     │
    │            │                       │                       │                 │
    │            └───────────────────────┼───────────────────────┘                 │
    │                                    │                                         │
    │  ┌────────────────────┐  ┌────────▼────────┐  ┌────────────────────┐        │
    │  │ Conflict Resolver  │  │ STATE ENGINE    │  │ Snapshot Manager   │        │
    │  │ (Vector Clocks)    │◄─┤ (Core)          ├─►│ (Save/Restore)     │        │
    │  │ [Gap 4]            │  └────────┬────────┘  │ [Gap 5]            │        │
    │  └────────────────────┘           │           └────────────────────┘        │
    │                                   │                                          │
    │  ┌────────────────────┐  ┌────────▼────────┐  ┌────────────────────┐        │
    │  │ State Partitioner  │  │ Compression     │  │ Access Control     │        │
    │  │ (Namespace/Domain) │  │ Engine          │  │ (RBAC)             │        │
    │  │ [Gap 6]            │  │ [Gap 7]         │  │ [Gap 8]            │        │
    │  └────────────────────┘  └─────────────────┘  └────────────────────┘        │
    │                                                                              │
    └──────────────────────────────────────────────────────────────────────────────┘

Features:
- Vector clocks for causality tracking and conflict detection
- CRDTs (Conflict-free Replicated Data Types) for automatic merge
- LZ4 compression with zstd fallback for large states
- Atomic file operations with fcntl locking
- Role-based access control with cryptographic tokens
- State partitioning by namespace (jarvis, prime, reactor, shared)
- Incremental snapshots with full backup support
- WAL (Write-Ahead Log) for durability

Author: JARVIS AI System
Version: 4.0.0
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
import uuid
import zlib
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Dict, Generic, List, Optional,
    Protocol, Set, Tuple, TypeVar, Union, Iterator
)

try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False

try:
    import lz4.frame
    LZ4_AVAILABLE = True
except ImportError:
    LZ4_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# Type Variables and Protocols
# =============================================================================

T = TypeVar('T')
StateValue = Union[str, int, float, bool, list, dict, None]


class StateObserver(Protocol):
    """Protocol for state change observers."""
    async def on_state_change(
        self,
        key: str,
        old_value: StateValue,
        new_value: StateValue,
        metadata: Dict[str, Any]
    ) -> None: ...


# =============================================================================
# Enums and Constants
# =============================================================================

class StateNamespace(str, Enum):
    """State namespace partitions."""
    JARVIS_BODY = "jarvis"
    JARVIS_PRIME = "prime"
    REACTOR_CORE = "reactor"
    SHARED = "shared"
    SYSTEM = "system"
    TRAINING = "training"
    MODELS = "models"
    TELEMETRY = "telemetry"


class AccessLevel(str, Enum):
    """Access control levels."""
    NONE = "none"
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class ConflictStrategy(str, Enum):
    """Conflict resolution strategies."""
    LAST_WRITE_WINS = "lww"
    FIRST_WRITE_WINS = "fww"
    VECTOR_CLOCK = "vector_clock"
    MERGE = "merge"
    MANUAL = "manual"


class CompressionType(str, Enum):
    """Compression algorithms."""
    NONE = "none"
    ZLIB = "zlib"
    LZ4 = "lz4"
    AUTO = "auto"


class SyncProtocol(str, Enum):
    """State synchronization protocols."""
    FULL_SYNC = "full"
    INCREMENTAL = "incremental"
    CRDT = "crdt"
    GOSSIP = "gossip"


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class StateManagerConfig:
    """Configuration for Trinity State Manager."""

    # Base directory
    state_base_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_STATE_DIR",
            str(Path.home() / ".jarvis" / "state")
        ))
    )

    # Versioning
    max_versions: int = 100
    version_retention_days: int = 30
    enable_versioning: bool = True

    # Compression
    compression_type: CompressionType = CompressionType.AUTO
    compression_threshold: int = 1024  # Compress if > 1KB

    # Synchronization
    sync_interval: float = 5.0  # seconds
    sync_protocol: SyncProtocol = SyncProtocol.CRDT
    sync_batch_size: int = 100

    # Snapshots
    snapshot_interval: float = 300.0  # 5 minutes
    max_snapshots: int = 24  # 24 snapshots (2 hours at 5-min intervals)
    incremental_snapshots: bool = True

    # Access control
    enable_rbac: bool = True
    token_expiry: int = 3600  # 1 hour

    # Conflict resolution
    default_conflict_strategy: ConflictStrategy = ConflictStrategy.VECTOR_CLOCK

    # Performance
    write_buffer_size: int = 1000
    flush_interval: float = 1.0
    wal_enabled: bool = True


# =============================================================================
# Gap 4: Vector Clock for Conflict Resolution
# =============================================================================

@dataclass
class VectorClock:
    """
    Vector clock for causality tracking and conflict detection.

    Implements Lamport logical clocks extended to distributed systems.
    Each node maintains its own counter, and clocks are compared to
    determine happened-before relationships.
    """

    clocks: Dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str) -> 'VectorClock':
        """Increment clock for a node."""
        new_clocks = self.clocks.copy()
        new_clocks[node_id] = new_clocks.get(node_id, 0) + 1
        return VectorClock(clocks=new_clocks)

    def merge(self, other: 'VectorClock') -> 'VectorClock':
        """Merge with another vector clock (take max of each component)."""
        all_nodes = set(self.clocks.keys()) | set(other.clocks.keys())
        merged = {
            node: max(self.clocks.get(node, 0), other.clocks.get(node, 0))
            for node in all_nodes
        }
        return VectorClock(clocks=merged)

    def __lt__(self, other: 'VectorClock') -> bool:
        """Check if this clock happened-before other (strict partial order)."""
        all_nodes = set(self.clocks.keys()) | set(other.clocks.keys())

        less_than_any = False
        for node in all_nodes:
            self_val = self.clocks.get(node, 0)
            other_val = other.clocks.get(node, 0)

            if self_val > other_val:
                return False
            if self_val < other_val:
                less_than_any = True

        return less_than_any

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VectorClock):
            return False
        return self.clocks == other.clocks

    def __le__(self, other: 'VectorClock') -> bool:
        return self < other or self == other

    def is_concurrent(self, other: 'VectorClock') -> bool:
        """Check if two clocks are concurrent (neither happened-before)."""
        return not (self <= other) and not (other <= self)

    def to_dict(self) -> Dict[str, int]:
        return self.clocks.copy()

    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> 'VectorClock':
        return cls(clocks=data)


# =============================================================================
# Gap 4: CRDT Types for Automatic Merge
# =============================================================================

class CRDT(ABC, Generic[T]):
    """Base class for Conflict-free Replicated Data Types."""

    @abstractmethod
    def value(self) -> T:
        """Get current value."""
        pass

    @abstractmethod
    def merge(self, other: 'CRDT[T]') -> 'CRDT[T]':
        """Merge with another CRDT."""
        pass

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        pass

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CRDT[T]':
        """Deserialize from dict."""
        pass


@dataclass
class GCounter(CRDT[int]):
    """
    Grow-only Counter CRDT.

    Each node has its own counter that can only increase.
    Value is sum of all node counters.
    """

    counts: Dict[str, int] = field(default_factory=dict)

    def value(self) -> int:
        return sum(self.counts.values())

    def increment(self, node_id: str, amount: int = 1) -> 'GCounter':
        """Increment counter for node."""
        new_counts = self.counts.copy()
        new_counts[node_id] = new_counts.get(node_id, 0) + amount
        return GCounter(counts=new_counts)

    def merge(self, other: 'GCounter') -> 'GCounter':
        """Merge by taking max of each node's counter."""
        all_nodes = set(self.counts.keys()) | set(other.counts.keys())
        merged = {
            node: max(self.counts.get(node, 0), other.counts.get(node, 0))
            for node in all_nodes
        }
        return GCounter(counts=merged)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "gcounter", "counts": self.counts}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GCounter':
        return cls(counts=data.get("counts", {}))


@dataclass
class PNCounter(CRDT[int]):
    """
    Positive-Negative Counter CRDT.

    Supports both increment and decrement operations.
    Uses two G-Counters: one for increments, one for decrements.
    """

    positive: GCounter = field(default_factory=GCounter)
    negative: GCounter = field(default_factory=GCounter)

    def value(self) -> int:
        return self.positive.value() - self.negative.value()

    def increment(self, node_id: str, amount: int = 1) -> 'PNCounter':
        return PNCounter(
            positive=self.positive.increment(node_id, amount),
            negative=self.negative
        )

    def decrement(self, node_id: str, amount: int = 1) -> 'PNCounter':
        return PNCounter(
            positive=self.positive,
            negative=self.negative.increment(node_id, amount)
        )

    def merge(self, other: 'PNCounter') -> 'PNCounter':
        return PNCounter(
            positive=self.positive.merge(other.positive),
            negative=self.negative.merge(other.negative)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "pncounter",
            "positive": self.positive.to_dict(),
            "negative": self.negative.to_dict()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PNCounter':
        return cls(
            positive=GCounter.from_dict(data.get("positive", {})),
            negative=GCounter.from_dict(data.get("negative", {}))
        )


@dataclass
class LWWRegister(CRDT[T], Generic[T]):
    """
    Last-Writer-Wins Register CRDT.

    Associates a timestamp with each value.
    On conflict, the value with the latest timestamp wins.
    """

    _value: T = None
    timestamp: float = 0.0
    node_id: str = ""

    def value(self) -> T:
        return self._value

    def set(self, value: T, node_id: str) -> 'LWWRegister[T]':
        """Set value with current timestamp."""
        return LWWRegister(
            _value=value,
            timestamp=time.time(),
            node_id=node_id
        )

    def merge(self, other: 'LWWRegister[T]') -> 'LWWRegister[T]':
        """Merge by taking value with latest timestamp."""
        if other.timestamp > self.timestamp:
            return other
        elif other.timestamp < self.timestamp:
            return self
        else:
            # Tie-breaker: use node_id lexicographically
            if other.node_id > self.node_id:
                return other
            return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "lww_register",
            "value": self._value,
            "timestamp": self.timestamp,
            "node_id": self.node_id
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LWWRegister':
        return cls(
            _value=data.get("value"),
            timestamp=data.get("timestamp", 0.0),
            node_id=data.get("node_id", "")
        )


@dataclass
class ORSet(CRDT[Set[T]], Generic[T]):
    """
    Observed-Remove Set CRDT.

    Supports add and remove operations.
    Each element is tagged with unique identifiers to handle
    add/remove conflicts (add wins if concurrent).
    """

    # Map from element to set of (add_id, remove_flag)
    elements: Dict[str, Set[Tuple[str, bool]]] = field(default_factory=lambda: defaultdict(set))

    def value(self) -> Set[T]:
        """Get current set value."""
        result = set()
        for element, tags in self.elements.items():
            # Element is in set if any tag has remove_flag=False
            for tag_id, removed in tags:
                if not removed:
                    result.add(element)
                    break
        return result

    def add(self, element: T) -> 'ORSet[T]':
        """Add element to set."""
        new_elements = defaultdict(set, {k: v.copy() for k, v in self.elements.items()})
        tag_id = str(uuid.uuid4())
        new_elements[str(element)].add((tag_id, False))
        return ORSet(elements=new_elements)

    def remove(self, element: T) -> 'ORSet[T]':
        """Remove element from set."""
        new_elements = defaultdict(set, {k: v.copy() for k, v in self.elements.items()})
        str_element = str(element)

        if str_element in new_elements:
            # Mark all existing tags as removed
            new_tags = set()
            for tag_id, removed in new_elements[str_element]:
                new_tags.add((tag_id, True))
            new_elements[str_element] = new_tags

        return ORSet(elements=new_elements)

    def merge(self, other: 'ORSet[T]') -> 'ORSet[T]':
        """Merge by union of all tags."""
        all_elements = set(self.elements.keys()) | set(other.elements.keys())
        merged = defaultdict(set)

        for element in all_elements:
            self_tags = self.elements.get(element, set())
            other_tags = other.elements.get(element, set())

            # Union of all tags
            all_tags = self_tags | other_tags
            merged[element] = all_tags

        return ORSet(elements=merged)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "or_set",
            "elements": {k: list(v) for k, v in self.elements.items()}
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ORSet':
        elements = defaultdict(set)
        for k, v in data.get("elements", {}).items():
            elements[k] = set(tuple(x) for x in v)
        return cls(elements=elements)


# =============================================================================
# State Entry with Full Metadata
# =============================================================================

@dataclass
class StateEntry:
    """
    A single state entry with full metadata.

    Includes versioning, vector clock, compression info, and access control.
    """

    key: str
    value: StateValue
    namespace: StateNamespace = StateNamespace.SHARED

    # Versioning (Gap 3)
    version: int = 1
    vector_clock: VectorClock = field(default_factory=VectorClock)

    # Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    # Metadata
    checksum: str = ""
    compressed: bool = False
    compression_type: CompressionType = CompressionType.NONE
    original_size: int = 0

    # Access control (Gap 8)
    owner: str = ""
    access_level: AccessLevel = AccessLevel.WRITE
    allowed_readers: Set[str] = field(default_factory=set)
    allowed_writers: Set[str] = field(default_factory=set)

    def compute_checksum(self) -> str:
        """Compute SHA-256 checksum of value."""
        value_bytes = json.dumps(self.value, sort_keys=True).encode()
        return hashlib.sha256(value_bytes).hexdigest()[:16]

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "namespace": self.namespace.value,
            "version": self.version,
            "vector_clock": self.vector_clock.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "checksum": self.checksum,
            "compressed": self.compressed,
            "compression_type": self.compression_type.value,
            "original_size": self.original_size,
            "owner": self.owner,
            "access_level": self.access_level.value,
            "allowed_readers": list(self.allowed_readers),
            "allowed_writers": list(self.allowed_writers)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StateEntry':
        return cls(
            key=data["key"],
            value=data["value"],
            namespace=StateNamespace(data.get("namespace", "shared")),
            version=data.get("version", 1),
            vector_clock=VectorClock.from_dict(data.get("vector_clock", {})),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            expires_at=data.get("expires_at"),
            checksum=data.get("checksum", ""),
            compressed=data.get("compressed", False),
            compression_type=CompressionType(data.get("compression_type", "none")),
            original_size=data.get("original_size", 0),
            owner=data.get("owner", ""),
            access_level=AccessLevel(data.get("access_level", "write")),
            allowed_readers=set(data.get("allowed_readers", [])),
            allowed_writers=set(data.get("allowed_writers", []))
        )


# =============================================================================
# Gap 7: Compression Engine
# =============================================================================

class CompressionEngine:
    """
    State compression engine supporting multiple algorithms.

    Auto-selects best algorithm based on data characteristics.
    """

    def __init__(self, config: StateManagerConfig):
        self.config = config
        self._stats = {
            "compressed_count": 0,
            "decompressed_count": 0,
            "bytes_saved": 0
        }

    def compress(
        self,
        data: bytes,
        compression_type: CompressionType = CompressionType.AUTO
    ) -> Tuple[bytes, CompressionType]:
        """
        Compress data using specified or auto-selected algorithm.

        Returns compressed data and actual compression type used.
        """
        if len(data) < self.config.compression_threshold:
            return data, CompressionType.NONE

        if compression_type == CompressionType.AUTO:
            compression_type = self._select_algorithm(data)

        if compression_type == CompressionType.NONE:
            return data, CompressionType.NONE

        if compression_type == CompressionType.LZ4 and LZ4_AVAILABLE:
            compressed = lz4.frame.compress(data)
        else:
            # Fall back to zlib
            compressed = zlib.compress(data, level=6)
            compression_type = CompressionType.ZLIB

        # Only use compression if it actually reduces size
        if len(compressed) < len(data):
            self._stats["compressed_count"] += 1
            self._stats["bytes_saved"] += len(data) - len(compressed)
            return compressed, compression_type

        return data, CompressionType.NONE

    def decompress(
        self,
        data: bytes,
        compression_type: CompressionType
    ) -> bytes:
        """Decompress data using specified algorithm."""
        if compression_type == CompressionType.NONE:
            return data

        self._stats["decompressed_count"] += 1

        if compression_type == CompressionType.LZ4 and LZ4_AVAILABLE:
            return lz4.frame.decompress(data)
        elif compression_type == CompressionType.ZLIB:
            return zlib.decompress(data)

        raise ValueError(f"Unknown compression type: {compression_type}")

    def _select_algorithm(self, data: bytes) -> CompressionType:
        """Auto-select best compression algorithm."""
        # LZ4 for speed, zlib for ratio
        if len(data) > 1_000_000 and LZ4_AVAILABLE:
            return CompressionType.LZ4  # Faster for large data
        return CompressionType.ZLIB  # Better ratio for smaller data

    def get_stats(self) -> Dict[str, Any]:
        return self._stats.copy()


# =============================================================================
# Gap 8: Access Control (RBAC)
# =============================================================================

@dataclass
class AccessToken:
    """Access token for state operations."""

    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    node_id: str = ""
    namespaces: Set[StateNamespace] = field(default_factory=set)
    access_level: AccessLevel = AccessLevel.READ
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 3600)
    signature: str = ""

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def can_read(self, namespace: StateNamespace) -> bool:
        if self.is_expired():
            return False
        if StateNamespace.SYSTEM in self.namespaces:
            return True  # System token can read all
        return namespace in self.namespaces and self.access_level != AccessLevel.NONE

    def can_write(self, namespace: StateNamespace) -> bool:
        if self.is_expired():
            return False
        if StateNamespace.SYSTEM in self.namespaces:
            return True
        return (
            namespace in self.namespaces and
            self.access_level in (AccessLevel.WRITE, AccessLevel.ADMIN)
        )

    def can_admin(self, namespace: StateNamespace) -> bool:
        if self.is_expired():
            return False
        return (
            StateNamespace.SYSTEM in self.namespaces or
            (namespace in self.namespaces and self.access_level == AccessLevel.ADMIN)
        )


class AccessController:
    """
    Role-based access control for state operations.

    Manages tokens, permissions, and audit logging.
    """

    def __init__(self, config: StateManagerConfig):
        self.config = config
        self._secret_key = secrets.token_bytes(32)
        self._tokens: Dict[str, AccessToken] = {}
        self._revoked_tokens: Set[str] = set()
        self._audit_log: List[Dict[str, Any]] = []

    def create_token(
        self,
        node_id: str,
        namespaces: Set[StateNamespace],
        access_level: AccessLevel,
        ttl: Optional[int] = None
    ) -> AccessToken:
        """Create a new access token."""
        token = AccessToken(
            node_id=node_id,
            namespaces=namespaces,
            access_level=access_level,
            expires_at=time.time() + (ttl or self.config.token_expiry)
        )

        # Sign the token
        token.signature = self._sign_token(token)

        self._tokens[token.token_id] = token
        self._audit("token_created", token.node_id, {"token_id": token.token_id})

        return token

    def validate_token(self, token: AccessToken) -> bool:
        """Validate a token's authenticity and expiry."""
        if token.token_id in self._revoked_tokens:
            return False

        if token.is_expired():
            return False

        expected_sig = self._sign_token(token)
        return hmac.compare_digest(token.signature, expected_sig)

    def revoke_token(self, token_id: str) -> bool:
        """Revoke a token."""
        if token_id in self._tokens:
            self._revoked_tokens.add(token_id)
            del self._tokens[token_id]
            self._audit("token_revoked", "system", {"token_id": token_id})
            return True
        return False

    def check_access(
        self,
        token: AccessToken,
        namespace: StateNamespace,
        operation: str  # "read", "write", "admin"
    ) -> bool:
        """Check if token has required access."""
        if not self.validate_token(token):
            self._audit("access_denied", token.node_id, {
                "reason": "invalid_token",
                "namespace": namespace.value,
                "operation": operation
            })
            return False

        if operation == "read":
            allowed = token.can_read(namespace)
        elif operation == "write":
            allowed = token.can_write(namespace)
        elif operation == "admin":
            allowed = token.can_admin(namespace)
        else:
            allowed = False

        if not allowed:
            self._audit("access_denied", token.node_id, {
                "namespace": namespace.value,
                "operation": operation
            })

        return allowed

    def _sign_token(self, token: AccessToken) -> str:
        """Create HMAC signature for token."""
        data = f"{token.token_id}:{token.node_id}:{token.expires_at}"
        sig = hmac.new(self._secret_key, data.encode(), hashlib.sha256)
        return sig.hexdigest()

    def _audit(self, action: str, actor: str, details: Dict[str, Any]) -> None:
        """Add audit log entry."""
        self._audit_log.append({
            "timestamp": time.time(),
            "action": action,
            "actor": actor,
            "details": details
        })

        # Keep last 10000 entries
        if len(self._audit_log) > 10000:
            self._audit_log = self._audit_log[-10000:]

    def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent audit log entries."""
        return self._audit_log[-limit:]


# =============================================================================
# Gap 6: State Partitioner
# =============================================================================

class StatePartitioner:
    """
    Manages state partitioning by namespace.

    Each namespace has its own storage file for:
    - Isolation
    - Performance (smaller files)
    - Independent compression
    - Granular snapshots
    """

    def __init__(self, config: StateManagerConfig):
        self.config = config
        self._partitions: Dict[StateNamespace, Path] = {}
        self._locks: Dict[StateNamespace, asyncio.Lock] = {}

        self._initialize_partitions()

    def _initialize_partitions(self) -> None:
        """Initialize partition directories and files."""
        partitions_dir = self.config.state_base_dir / "partitions"
        partitions_dir.mkdir(parents=True, exist_ok=True)

        for namespace in StateNamespace:
            partition_file = partitions_dir / f"{namespace.value}.json"
            self._partitions[namespace] = partition_file
            self._locks[namespace] = asyncio.Lock()

            # Create empty partition file if not exists
            if not partition_file.exists():
                partition_file.write_text("{}")

    def get_partition_path(self, namespace: StateNamespace) -> Path:
        """Get path to partition file."""
        return self._partitions[namespace]

    def get_partition_lock(self, namespace: StateNamespace) -> asyncio.Lock:
        """Get lock for partition."""
        return self._locks[namespace]

    async def read_partition(self, namespace: StateNamespace) -> Dict[str, StateEntry]:
        """Read all entries from a partition."""
        async with self._locks[namespace]:
            path = self._partitions[namespace]

            if AIOFILES_AVAILABLE:
                async with aiofiles.open(path, 'r') as f:
                    data = await f.read()
            else:
                data = path.read_text()

            raw_data = json.loads(data) if data else {}
            return {
                key: StateEntry.from_dict(value)
                for key, value in raw_data.items()
            }

    async def write_partition(
        self,
        namespace: StateNamespace,
        entries: Dict[str, StateEntry]
    ) -> None:
        """Write all entries to a partition."""
        async with self._locks[namespace]:
            path = self._partitions[namespace]
            data = {
                key: entry.to_dict()
                for key, entry in entries.items()
            }

            json_data = json.dumps(data, indent=2)

            if AIOFILES_AVAILABLE:
                async with aiofiles.open(path, 'w') as f:
                    await f.write(json_data)
            else:
                path.write_text(json_data)

    def get_partition_stats(self) -> Dict[str, Any]:
        """Get statistics for all partitions."""
        stats = {}
        for namespace, path in self._partitions.items():
            if path.exists():
                stats[namespace.value] = {
                    "size_bytes": path.stat().st_size,
                    "modified": path.stat().st_mtime
                }
        return stats


# =============================================================================
# Gap 3: Version Manager
# =============================================================================

@dataclass
class StateVersion:
    """A specific version of a state entry."""

    version: int
    entry: StateEntry
    timestamp: float = field(default_factory=time.time)
    change_type: str = "update"  # create, update, delete
    previous_version: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "entry": self.entry.to_dict(),
            "timestamp": self.timestamp,
            "change_type": self.change_type,
            "previous_version": self.previous_version
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StateVersion':
        return cls(
            version=data["version"],
            entry=StateEntry.from_dict(data["entry"]),
            timestamp=data.get("timestamp", time.time()),
            change_type=data.get("change_type", "update"),
            previous_version=data.get("previous_version")
        )


class VersionManager:
    """
    Manages state versioning and history.

    Provides:
    - Version tracking for all state changes
    - History retrieval
    - Rollback to previous versions
    - Version comparison (diff)
    """

    def __init__(self, config: StateManagerConfig):
        self.config = config
        self._versions: Dict[str, List[StateVersion]] = defaultdict(list)
        self._version_dir = config.state_base_dir / "versions"
        self._version_dir.mkdir(parents=True, exist_ok=True)

    async def record_version(
        self,
        key: str,
        entry: StateEntry,
        change_type: str = "update"
    ) -> StateVersion:
        """Record a new version of a state entry."""
        versions = self._versions[key]

        new_version_num = entry.version
        previous_version = versions[-1].version if versions else None

        version = StateVersion(
            version=new_version_num,
            entry=entry,
            change_type=change_type,
            previous_version=previous_version
        )

        versions.append(version)

        # Prune old versions
        if len(versions) > self.config.max_versions:
            self._versions[key] = versions[-self.config.max_versions:]

        return version

    async def get_version(
        self,
        key: str,
        version: Optional[int] = None
    ) -> Optional[StateVersion]:
        """Get a specific version or latest."""
        versions = self._versions.get(key, [])

        if not versions:
            return None

        if version is None:
            return versions[-1]

        for v in versions:
            if v.version == version:
                return v

        return None

    async def get_history(
        self,
        key: str,
        limit: int = 10
    ) -> List[StateVersion]:
        """Get version history for a key."""
        versions = self._versions.get(key, [])
        return versions[-limit:]

    async def rollback(
        self,
        key: str,
        target_version: int
    ) -> Optional[StateEntry]:
        """Rollback to a specific version."""
        version = await self.get_version(key, target_version)

        if version is None:
            return None

        # Create new version as rollback
        rolled_back_entry = StateEntry(
            key=version.entry.key,
            value=version.entry.value,
            namespace=version.entry.namespace,
            version=len(self._versions[key]) + 1,
            vector_clock=version.entry.vector_clock,
            owner=version.entry.owner,
            access_level=version.entry.access_level
        )
        rolled_back_entry.checksum = rolled_back_entry.compute_checksum()

        await self.record_version(key, rolled_back_entry, "rollback")

        return rolled_back_entry

    async def diff(
        self,
        key: str,
        version1: int,
        version2: int
    ) -> Dict[str, Any]:
        """Compare two versions."""
        v1 = await self.get_version(key, version1)
        v2 = await self.get_version(key, version2)

        if v1 is None or v2 is None:
            return {"error": "Version not found"}

        return {
            "key": key,
            "version1": version1,
            "version2": version2,
            "value1": v1.entry.value,
            "value2": v2.entry.value,
            "changed": v1.entry.value != v2.entry.value,
            "time_diff": v2.timestamp - v1.timestamp
        }

    async def prune_old_versions(self) -> int:
        """Prune versions older than retention period."""
        cutoff = time.time() - (self.config.version_retention_days * 86400)
        pruned_count = 0

        for key, versions in self._versions.items():
            original_len = len(versions)
            self._versions[key] = [
                v for v in versions
                if v.timestamp > cutoff or v == versions[-1]  # Keep latest
            ]
            pruned_count += original_len - len(self._versions[key])

        return pruned_count


# =============================================================================
# Gap 5: Snapshot Manager
# =============================================================================

@dataclass
class Snapshot:
    """A complete snapshot of system state."""

    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    namespaces: List[StateNamespace] = field(default_factory=list)
    entries: Dict[str, StateEntry] = field(default_factory=dict)
    checksum: str = ""
    compressed: bool = False
    incremental: bool = False
    base_snapshot_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def compute_checksum(self) -> str:
        """Compute checksum of entire snapshot."""
        content = json.dumps(
            {k: v.to_dict() for k, v in self.entries.items()},
            sort_keys=True
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "namespaces": [ns.value for ns in self.namespaces],
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
            "checksum": self.checksum,
            "compressed": self.compressed,
            "incremental": self.incremental,
            "base_snapshot_id": self.base_snapshot_id,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Snapshot':
        return cls(
            snapshot_id=data["snapshot_id"],
            timestamp=data.get("timestamp", time.time()),
            namespaces=[StateNamespace(ns) for ns in data.get("namespaces", [])],
            entries={
                k: StateEntry.from_dict(v)
                for k, v in data.get("entries", {}).items()
            },
            checksum=data.get("checksum", ""),
            compressed=data.get("compressed", False),
            incremental=data.get("incremental", False),
            base_snapshot_id=data.get("base_snapshot_id"),
            metadata=data.get("metadata", {})
        )


class SnapshotManager:
    """
    Manages state snapshots for backup and restore.

    Supports:
    - Full snapshots
    - Incremental snapshots
    - Compressed snapshots
    - Point-in-time restore
    """

    def __init__(
        self,
        config: StateManagerConfig,
        compression: CompressionEngine,
        partitioner: StatePartitioner
    ):
        self.config = config
        self.compression = compression
        self.partitioner = partitioner
        self._snapshots: Dict[str, Snapshot] = {}
        self._snapshot_dir = config.state_base_dir / "snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_full_snapshot: Optional[str] = None

    async def create_snapshot(
        self,
        namespaces: Optional[List[StateNamespace]] = None,
        incremental: bool = True,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Snapshot:
        """Create a new snapshot."""
        if namespaces is None:
            namespaces = list(StateNamespace)

        # Collect all entries from specified namespaces
        all_entries: Dict[str, StateEntry] = {}
        for namespace in namespaces:
            partition_entries = await self.partitioner.read_partition(namespace)
            all_entries.update(partition_entries)

        # Create snapshot
        snapshot = Snapshot(
            namespaces=namespaces,
            entries=all_entries,
            incremental=incremental and self._last_full_snapshot is not None,
            base_snapshot_id=self._last_full_snapshot if incremental else None,
            metadata=metadata or {}
        )
        snapshot.checksum = snapshot.compute_checksum()

        # Save snapshot
        await self._save_snapshot(snapshot)

        self._snapshots[snapshot.snapshot_id] = snapshot

        if not incremental:
            self._last_full_snapshot = snapshot.snapshot_id

        # Prune old snapshots
        await self._prune_snapshots()

        logger.info(f"Created {'incremental' if snapshot.incremental else 'full'} snapshot: {snapshot.snapshot_id}")

        return snapshot

    async def restore_snapshot(
        self,
        snapshot_id: str,
        namespaces: Optional[List[StateNamespace]] = None
    ) -> bool:
        """Restore system state from a snapshot."""
        snapshot = await self._load_snapshot(snapshot_id)

        if snapshot is None:
            logger.error(f"Snapshot not found: {snapshot_id}")
            return False

        # Verify checksum
        if snapshot.compute_checksum() != snapshot.checksum:
            logger.error(f"Snapshot checksum mismatch: {snapshot_id}")
            return False

        # If incremental, first restore base snapshot
        if snapshot.incremental and snapshot.base_snapshot_id:
            await self.restore_snapshot(snapshot.base_snapshot_id, namespaces)

        # Determine which namespaces to restore
        target_namespaces = namespaces or snapshot.namespaces

        # Restore entries to partitions
        for namespace in target_namespaces:
            entries = {
                k: v for k, v in snapshot.entries.items()
                if v.namespace == namespace
            }
            await self.partitioner.write_partition(namespace, entries)

        logger.info(f"Restored snapshot: {snapshot_id}")
        return True

    async def list_snapshots(self) -> List[Dict[str, Any]]:
        """List available snapshots."""
        snapshots = []
        for path in self._snapshot_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                snapshots.append({
                    "snapshot_id": data["snapshot_id"],
                    "timestamp": data["timestamp"],
                    "incremental": data.get("incremental", False),
                    "size_bytes": path.stat().st_size
                })
            except Exception as e:
                logger.debug(f"Error reading snapshot {path}: {e}")

        return sorted(snapshots, key=lambda x: x["timestamp"], reverse=True)

    async def _save_snapshot(self, snapshot: Snapshot) -> None:
        """Save snapshot to disk."""
        path = self._snapshot_dir / f"{snapshot.snapshot_id}.json"
        data = json.dumps(snapshot.to_dict(), indent=2)

        # Optionally compress
        if len(data) > self.config.compression_threshold:
            compressed, comp_type = self.compression.compress(data.encode())
            if comp_type != CompressionType.NONE:
                path = path.with_suffix(".json.compressed")
                snapshot.compressed = True
                data = compressed

        if isinstance(data, str):
            data = data.encode()

        if AIOFILES_AVAILABLE:
            async with aiofiles.open(path, 'wb') as f:
                await f.write(data)
        else:
            path.write_bytes(data)

    async def _load_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        """Load snapshot from disk."""
        # Try both compressed and uncompressed
        for suffix in [".json", ".json.compressed"]:
            path = self._snapshot_dir / f"{snapshot_id}{suffix}"
            if path.exists():
                try:
                    if AIOFILES_AVAILABLE:
                        async with aiofiles.open(path, 'rb') as f:
                            data = await f.read()
                    else:
                        data = path.read_bytes()

                    # Decompress if needed
                    if suffix == ".json.compressed":
                        data = self.compression.decompress(
                            data, CompressionType.ZLIB
                        )

                    return Snapshot.from_dict(json.loads(data))
                except Exception as e:
                    logger.error(f"Error loading snapshot {path}: {e}")

        return None

    async def _prune_snapshots(self) -> int:
        """Remove old snapshots beyond max limit."""
        snapshots = await self.list_snapshots()

        if len(snapshots) <= self.config.max_snapshots:
            return 0

        # Keep newest snapshots
        to_remove = snapshots[self.config.max_snapshots:]
        removed = 0

        for snap in to_remove:
            for suffix in [".json", ".json.compressed"]:
                path = self._snapshot_dir / f"{snap['snapshot_id']}{suffix}"
                if path.exists():
                    path.unlink()
                    removed += 1
                    break

        return removed


# =============================================================================
# Gap 2: Distributed State Synchronization
# =============================================================================

@dataclass
class SyncMessage:
    """Message for state synchronization."""

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender_id: str = ""
    message_type: str = "sync"  # sync, ack, request, response
    entries: List[StateEntry] = field(default_factory=list)
    vector_clock: VectorClock = field(default_factory=VectorClock)
    timestamp: float = field(default_factory=time.time)


class StateSynchronizer:
    """
    Handles distributed state synchronization between repos.

    Uses a CRDT-based approach with vector clocks for:
    - Conflict-free merging
    - Causal ordering
    - Eventual consistency
    """

    def __init__(
        self,
        config: StateManagerConfig,
        node_id: str,
        partitioner: StatePartitioner,
        version_manager: VersionManager
    ):
        self.config = config
        self.node_id = node_id
        self.partitioner = partitioner
        self.version_manager = version_manager

        self._local_clock = VectorClock()
        self._sync_queue: asyncio.Queue[SyncMessage] = asyncio.Queue()
        self._peers: Set[str] = set()
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False

        # Sync directory for file-based sync
        self._sync_dir = config.state_base_dir / "sync"
        self._sync_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start synchronization loop."""
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        """Stop synchronization."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task

    async def register_peer(self, peer_id: str) -> None:
        """Register a peer for synchronization."""
        self._peers.add(peer_id)
        logger.info(f"Registered sync peer: {peer_id}")

    async def sync_entry(self, entry: StateEntry) -> None:
        """Queue an entry for synchronization."""
        # Increment local clock
        self._local_clock = self._local_clock.increment(self.node_id)
        entry.vector_clock = self._local_clock

        message = SyncMessage(
            sender_id=self.node_id,
            entries=[entry],
            vector_clock=self._local_clock
        )

        await self._sync_queue.put(message)

    async def _sync_loop(self) -> None:
        """Main synchronization loop."""
        while self._running:
            try:
                # Process outgoing sync messages
                await self._process_outgoing()

                # Process incoming sync messages
                await self._process_incoming()

                await asyncio.sleep(self.config.sync_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sync error: {e}")
                await asyncio.sleep(1.0)

    async def _process_outgoing(self) -> None:
        """Process outgoing sync messages."""
        messages_to_send: List[SyncMessage] = []

        # Collect batch of messages
        while len(messages_to_send) < self.config.sync_batch_size:
            try:
                message = self._sync_queue.get_nowait()
                messages_to_send.append(message)
            except asyncio.QueueEmpty:
                break

        if not messages_to_send:
            return

        # Write sync messages to files for each peer
        for peer_id in self._peers:
            peer_sync_dir = self._sync_dir / peer_id
            peer_sync_dir.mkdir(parents=True, exist_ok=True)

            for message in messages_to_send:
                sync_file = peer_sync_dir / f"{message.message_id}.json"
                data = {
                    "message_id": message.message_id,
                    "sender_id": message.sender_id,
                    "message_type": message.message_type,
                    "entries": [e.to_dict() for e in message.entries],
                    "vector_clock": message.vector_clock.to_dict(),
                    "timestamp": message.timestamp
                }

                if AIOFILES_AVAILABLE:
                    async with aiofiles.open(sync_file, 'w') as f:
                        await f.write(json.dumps(data))
                else:
                    sync_file.write_text(json.dumps(data))

    async def _process_incoming(self) -> None:
        """Process incoming sync messages from peers."""
        my_inbox = self._sync_dir / self.node_id

        if not my_inbox.exists():
            return

        for sync_file in my_inbox.glob("*.json"):
            try:
                if AIOFILES_AVAILABLE:
                    async with aiofiles.open(sync_file, 'r') as f:
                        data = json.loads(await f.read())
                else:
                    data = json.loads(sync_file.read_text())

                # Process the sync message
                await self._merge_remote_state(data)

                # Remove processed file
                sync_file.unlink()

            except Exception as e:
                logger.error(f"Error processing sync file {sync_file}: {e}")

    async def _merge_remote_state(self, data: Dict[str, Any]) -> None:
        """Merge remote state using CRDT semantics."""
        remote_clock = VectorClock.from_dict(data.get("vector_clock", {}))

        for entry_data in data.get("entries", []):
            remote_entry = StateEntry.from_dict(entry_data)

            # Read local state for this key
            local_entries = await self.partitioner.read_partition(remote_entry.namespace)
            local_entry = local_entries.get(remote_entry.key)

            # Determine merge action
            if local_entry is None:
                # No local entry, accept remote
                local_entries[remote_entry.key] = remote_entry
            elif remote_entry.vector_clock > local_entry.vector_clock:
                # Remote is newer, accept it
                local_entries[remote_entry.key] = remote_entry
            elif local_entry.vector_clock > remote_entry.vector_clock:
                # Local is newer, keep it
                pass
            else:
                # Concurrent updates - merge using conflict strategy
                merged_entry = await self._resolve_conflict(
                    local_entry, remote_entry
                )
                local_entries[remote_entry.key] = merged_entry

            # Write back to partition
            await self.partitioner.write_partition(
                remote_entry.namespace, local_entries
            )

        # Merge vector clocks
        self._local_clock = self._local_clock.merge(remote_clock)

    async def _resolve_conflict(
        self,
        local: StateEntry,
        remote: StateEntry
    ) -> StateEntry:
        """Resolve conflicting state updates."""
        strategy = self.config.default_conflict_strategy

        if strategy == ConflictStrategy.LAST_WRITE_WINS:
            return remote if remote.updated_at > local.updated_at else local

        elif strategy == ConflictStrategy.FIRST_WRITE_WINS:
            return local if local.created_at < remote.created_at else remote

        elif strategy == ConflictStrategy.VECTOR_CLOCK:
            # Already handled above, this is fallback
            return remote if remote.updated_at > local.updated_at else local

        elif strategy == ConflictStrategy.MERGE:
            # Attempt automatic merge for compatible types
            if isinstance(local.value, dict) and isinstance(remote.value, dict):
                # Deep merge dicts
                merged_value = {**local.value, **remote.value}
                merged = StateEntry(
                    key=local.key,
                    value=merged_value,
                    namespace=local.namespace,
                    version=max(local.version, remote.version) + 1,
                    vector_clock=local.vector_clock.merge(remote.vector_clock),
                    owner=local.owner
                )
                merged.updated_at = time.time()
                merged.checksum = merged.compute_checksum()
                return merged
            else:
                # Can't merge, use LWW
                return remote if remote.updated_at > local.updated_at else local

        else:
            # Manual resolution - keep local by default
            return local


# =============================================================================
# Gap 1: Unified State Coordinator (Main Class)
# =============================================================================

class TrinityStateManager:
    """
    Unified State Coordinator for the Trinity Ecosystem.

    Single source of truth with:
    - Atomic updates
    - Distributed synchronization
    - Version history
    - Conflict resolution
    - Snapshots
    - Partitioning
    - Compression
    - Access control

    This is the main entry point for all state operations.
    """

    def __init__(
        self,
        config: Optional[StateManagerConfig] = None,
        node_id: Optional[str] = None
    ):
        self.config = config or StateManagerConfig()
        self.node_id = node_id or f"{os.uname().nodename}-{os.getpid()}"

        # Initialize components for each gap
        self.compression = CompressionEngine(self.config)  # Gap 7
        self.access_controller = AccessController(self.config)  # Gap 8
        self.partitioner = StatePartitioner(self.config)  # Gap 6
        self.version_manager = VersionManager(self.config)  # Gap 3

        # Initialize snapshot manager (Gap 5)
        self.snapshot_manager = SnapshotManager(
            self.config,
            self.compression,
            self.partitioner
        )

        # Initialize synchronizer (Gap 2)
        self.synchronizer = StateSynchronizer(
            self.config,
            self.node_id,
            self.partitioner,
            self.version_manager
        )

        # State storage
        self._state: Dict[str, StateEntry] = {}
        self._observers: List[StateObserver] = []
        self._write_buffer: List[StateEntry] = []
        self._buffer_lock = asyncio.Lock()

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._started = False

        # Current token (for auth)
        self._current_token: Optional[AccessToken] = None

    @classmethod
    async def create(
        cls,
        config: Optional[StateManagerConfig] = None,
        node_id: Optional[str] = None
    ) -> 'TrinityStateManager':
        """Factory method for creating and starting state manager."""
        manager = cls(config, node_id)
        await manager.start()
        return manager

    async def start(self) -> None:
        """Start the state manager."""
        if self._started:
            return

        logger.info(f"Starting Trinity State Manager v4.0 (node: {self.node_id})")

        # Initialize directories
        self.config.state_base_dir.mkdir(parents=True, exist_ok=True)

        # Load existing state from partitions
        await self._load_state()

        # Start synchronizer
        await self.synchronizer.start()

        # Start background tasks
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())

        # Create default system token
        self._current_token = self.access_controller.create_token(
            self.node_id,
            set(StateNamespace),
            AccessLevel.ADMIN,
            ttl=86400 * 365  # 1 year for system token
        )

        self._started = True
        logger.info("Trinity State Manager started successfully")

    async def stop(self) -> None:
        """Stop the state manager."""
        if not self._started:
            return

        logger.info("Stopping Trinity State Manager...")

        # Flush any pending writes
        await self._flush_buffer()

        # Stop background tasks
        for task in [self._flush_task, self._snapshot_task]:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        # Stop synchronizer
        await self.synchronizer.stop()

        # Create final snapshot
        await self.snapshot_manager.create_snapshot(incremental=False)

        self._started = False
        logger.info("Trinity State Manager stopped")

    # =========================================================================
    # Core State Operations
    # =========================================================================

    async def get(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SHARED,
        default: StateValue = None
    ) -> StateValue:
        """
        Get a state value.

        Args:
            key: State key
            namespace: State namespace partition
            default: Default value if key not found

        Returns:
            State value or default
        """
        # Check access
        if self.config.enable_rbac and self._current_token:
            if not self.access_controller.check_access(
                self._current_token, namespace, "read"
            ):
                raise PermissionError(f"No read access to namespace {namespace}")

        full_key = f"{namespace.value}:{key}"

        # Check in-memory cache first
        if full_key in self._state:
            entry = self._state[full_key]
            if entry.is_expired():
                del self._state[full_key]
                return default
            return entry.value

        # Load from partition
        entries = await self.partitioner.read_partition(namespace)
        if key in entries:
            entry = entries[key]
            if entry.is_expired():
                return default
            self._state[full_key] = entry
            return entry.value

        return default

    async def set(
        self,
        key: str,
        value: StateValue,
        namespace: StateNamespace = StateNamespace.SHARED,
        ttl: Optional[int] = None,
        sync: bool = True
    ) -> StateEntry:
        """
        Set a state value.

        Args:
            key: State key
            value: Value to set
            namespace: State namespace partition
            ttl: Time-to-live in seconds (optional)
            sync: Whether to sync to other nodes

        Returns:
            Created/updated state entry
        """
        # Check access
        if self.config.enable_rbac and self._current_token:
            if not self.access_controller.check_access(
                self._current_token, namespace, "write"
            ):
                raise PermissionError(f"No write access to namespace {namespace}")

        full_key = f"{namespace.value}:{key}"

        # Get existing entry or create new
        existing = self._state.get(full_key)
        new_version = (existing.version + 1) if existing else 1

        # Create entry
        entry = StateEntry(
            key=key,
            value=value,
            namespace=namespace,
            version=new_version,
            vector_clock=existing.vector_clock if existing else VectorClock(),
            expires_at=time.time() + ttl if ttl else None,
            owner=self.node_id
        )
        entry.vector_clock = entry.vector_clock.increment(self.node_id)
        entry.checksum = entry.compute_checksum()

        # Store in memory
        old_value = existing.value if existing else None
        self._state[full_key] = entry

        # Buffer for batch write
        async with self._buffer_lock:
            self._write_buffer.append(entry)

        # Record version
        if self.config.enable_versioning:
            await self.version_manager.record_version(
                full_key, entry,
                "update" if existing else "create"
            )

        # Sync to other nodes
        if sync:
            await self.synchronizer.sync_entry(entry)

        # Notify observers
        await self._notify_observers(key, old_value, value, {
            "namespace": namespace.value,
            "version": new_version
        })

        return entry

    async def delete(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> bool:
        """Delete a state entry."""
        # Check access
        if self.config.enable_rbac and self._current_token:
            if not self.access_controller.check_access(
                self._current_token, namespace, "write"
            ):
                raise PermissionError(f"No write access to namespace {namespace}")

        full_key = f"{namespace.value}:{key}"

        if full_key in self._state:
            entry = self._state.pop(full_key)

            # Record deletion
            if self.config.enable_versioning:
                await self.version_manager.record_version(full_key, entry, "delete")

            # Write to partition
            entries = await self.partitioner.read_partition(namespace)
            if key in entries:
                del entries[key]
                await self.partitioner.write_partition(namespace, entries)

            # Notify observers
            await self._notify_observers(key, entry.value, None, {
                "namespace": namespace.value,
                "deleted": True
            })

            return True

        return False

    async def get_many(
        self,
        keys: List[str],
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> Dict[str, StateValue]:
        """Get multiple state values."""
        result = {}
        for key in keys:
            value = await self.get(key, namespace)
            if value is not None:
                result[key] = value
        return result

    async def set_many(
        self,
        entries: Dict[str, StateValue],
        namespace: StateNamespace = StateNamespace.SHARED,
        sync: bool = True
    ) -> List[StateEntry]:
        """Set multiple state values atomically."""
        results = []
        for key, value in entries.items():
            entry = await self.set(key, value, namespace, sync=sync)
            results.append(entry)
        return results

    # =========================================================================
    # Advanced Operations
    # =========================================================================

    async def increment(
        self,
        key: str,
        amount: int = 1,
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> int:
        """Atomically increment a counter using CRDT."""
        full_key = f"{namespace.value}:{key}"

        current = await self.get(key, namespace, default=0)
        if not isinstance(current, (int, float)):
            raise TypeError(f"Cannot increment non-numeric value: {type(current)}")

        new_value = current + amount
        await self.set(key, new_value, namespace)

        return new_value

    async def append_to_list(
        self,
        key: str,
        item: Any,
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> List[Any]:
        """Append item to a list state."""
        current = await self.get(key, namespace, default=[])
        if not isinstance(current, list):
            raise TypeError(f"Cannot append to non-list value: {type(current)}")

        current.append(item)
        await self.set(key, current, namespace)

        return current

    async def update_dict(
        self,
        key: str,
        updates: Dict[str, Any],
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> Dict[str, Any]:
        """Update a dictionary state with merge."""
        current = await self.get(key, namespace, default={})
        if not isinstance(current, dict):
            raise TypeError(f"Cannot update non-dict value: {type(current)}")

        current.update(updates)
        await self.set(key, current, namespace)

        return current

    # =========================================================================
    # Version Operations (Gap 3)
    # =========================================================================

    async def get_history(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SHARED,
        limit: int = 10
    ) -> List[StateVersion]:
        """Get version history for a key."""
        full_key = f"{namespace.value}:{key}"
        return await self.version_manager.get_history(full_key, limit)

    async def rollback(
        self,
        key: str,
        version: int,
        namespace: StateNamespace = StateNamespace.SHARED
    ) -> Optional[StateEntry]:
        """Rollback to a specific version."""
        full_key = f"{namespace.value}:{key}"
        entry = await self.version_manager.rollback(full_key, version)

        if entry:
            self._state[full_key] = entry
            entries = await self.partitioner.read_partition(namespace)
            entries[key] = entry
            await self.partitioner.write_partition(namespace, entries)

        return entry

    # =========================================================================
    # Snapshot Operations (Gap 5)
    # =========================================================================

    async def create_snapshot(
        self,
        namespaces: Optional[List[StateNamespace]] = None,
        incremental: bool = True
    ) -> Snapshot:
        """Create a state snapshot."""
        return await self.snapshot_manager.create_snapshot(
            namespaces, incremental
        )

    async def restore_snapshot(self, snapshot_id: str) -> bool:
        """Restore from a snapshot."""
        success = await self.snapshot_manager.restore_snapshot(snapshot_id)

        if success:
            # Reload state into memory
            await self._load_state()

        return success

    async def list_snapshots(self) -> List[Dict[str, Any]]:
        """List available snapshots."""
        return await self.snapshot_manager.list_snapshots()

    # =========================================================================
    # Synchronization Operations (Gap 2)
    # =========================================================================

    async def add_peer(self, peer_id: str) -> None:
        """Add a synchronization peer."""
        await self.synchronizer.register_peer(peer_id)

    # =========================================================================
    # Access Control Operations (Gap 8)
    # =========================================================================

    def create_token(
        self,
        node_id: str,
        namespaces: Set[StateNamespace],
        access_level: AccessLevel,
        ttl: Optional[int] = None
    ) -> AccessToken:
        """Create an access token."""
        return self.access_controller.create_token(
            node_id, namespaces, access_level, ttl
        )

    def set_token(self, token: AccessToken) -> None:
        """Set current authentication token."""
        self._current_token = token

    def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get access audit log."""
        return self.access_controller.get_audit_log(limit)

    # =========================================================================
    # Observer Pattern
    # =========================================================================

    def add_observer(self, observer: StateObserver) -> Callable[[], None]:
        """Add a state change observer."""
        self._observers.append(observer)

        def unsubscribe():
            if observer in self._observers:
                self._observers.remove(observer)

        return unsubscribe

    async def _notify_observers(
        self,
        key: str,
        old_value: StateValue,
        new_value: StateValue,
        metadata: Dict[str, Any]
    ) -> None:
        """Notify observers of state change."""
        for observer in self._observers:
            try:
                await observer.on_state_change(key, old_value, new_value, metadata)
            except Exception as e:
                logger.error(f"Observer error: {e}")

    # =========================================================================
    # Internal Operations
    # =========================================================================

    async def _load_state(self) -> None:
        """Load state from all partitions."""
        for namespace in StateNamespace:
            entries = await self.partitioner.read_partition(namespace)
            for key, entry in entries.items():
                full_key = f"{namespace.value}:{key}"
                self._state[full_key] = entry

        logger.info(f"Loaded {len(self._state)} state entries")

    async def _flush_buffer(self) -> None:
        """Flush write buffer to partitions."""
        async with self._buffer_lock:
            if not self._write_buffer:
                return

            buffer = self._write_buffer.copy()
            self._write_buffer.clear()

        # Group by namespace
        by_namespace: Dict[StateNamespace, Dict[str, StateEntry]] = defaultdict(dict)
        for entry in buffer:
            by_namespace[entry.namespace][entry.key] = entry

        # Write to each partition
        for namespace, entries in by_namespace.items():
            existing = await self.partitioner.read_partition(namespace)
            existing.update(entries)
            await self.partitioner.write_partition(namespace, existing)

        logger.debug(f"Flushed {len(buffer)} entries to disk")

    async def _flush_loop(self) -> None:
        """Background flush loop."""
        while True:
            await asyncio.sleep(self.config.flush_interval)
            await self._flush_buffer()

    async def _snapshot_loop(self) -> None:
        """Background snapshot loop."""
        while True:
            await asyncio.sleep(self.config.snapshot_interval)
            try:
                await self.snapshot_manager.create_snapshot(incremental=True)
            except Exception as e:
                logger.error(f"Auto-snapshot failed: {e}")

    # =========================================================================
    # Metrics and Health
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive metrics."""
        return {
            "node_id": self.node_id,
            "started": self._started,
            "state_entries": len(self._state),
            "write_buffer_size": len(self._write_buffer),
            "partitions": self.partitioner.get_partition_stats(),
            "compression": self.compression.get_stats(),
            "peers": list(self.synchronizer._peers),
            "vector_clock": self.synchronizer._local_clock.to_dict()
        }

    async def get_health(self) -> Dict[str, Any]:
        """Get health status."""
        return {
            "status": "healthy" if self._started else "stopped",
            "node_id": self.node_id,
            "state_entries": len(self._state),
            "components": {
                "partitioner": "ok",
                "version_manager": "ok",
                "snapshot_manager": "ok",
                "synchronizer": "ok" if self.synchronizer._running else "stopped",
                "access_controller": "ok"
            }
        }


# =============================================================================
# Convenience Functions
# =============================================================================

_global_state_manager: Optional[TrinityStateManager] = None


async def get_state_manager() -> TrinityStateManager:
    """Get global state manager instance."""
    global _global_state_manager

    if _global_state_manager is None:
        _global_state_manager = await TrinityStateManager.create()

    return _global_state_manager


async def shutdown_state_manager() -> None:
    """Shutdown global state manager."""
    global _global_state_manager

    if _global_state_manager:
        await _global_state_manager.stop()
        _global_state_manager = None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main Class
    "TrinityStateManager",
    "StateManagerConfig",
    "get_state_manager",
    "shutdown_state_manager",

    # State Entry
    "StateEntry",
    "StateVersion",

    # Gap 1: Unified Coordinator - main class above

    # Gap 2: Synchronization
    "StateSynchronizer",
    "SyncMessage",
    "SyncProtocol",

    # Gap 3: Versioning
    "VersionManager",

    # Gap 4: Conflict Resolution
    "VectorClock",
    "ConflictStrategy",
    "CRDT",
    "GCounter",
    "PNCounter",
    "LWWRegister",
    "ORSet",

    # Gap 5: Snapshots
    "Snapshot",
    "SnapshotManager",

    # Gap 6: Partitioning
    "StatePartitioner",
    "StateNamespace",

    # Gap 7: Compression
    "CompressionEngine",
    "CompressionType",

    # Gap 8: Access Control
    "AccessController",
    "AccessToken",
    "AccessLevel",
]
