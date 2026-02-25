"""TraceEnvelope v1 schema — causal traceability primitives.

Provides:
- LamportClock: thread-safe logical clock for causal ordering
- TraceEnvelope: frozen dataclass carrying trace context across boundaries
- TraceEnvelopeFactory: creates root/child/event envelopes with auto-generated IDs
- validate_envelope: structural validation with clock-skew detection
- check_schema_compatibility: forward/backward version negotiation
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, fields as dc_fields
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACE_SCHEMA_VERSION: int = 1
TRACE_SCHEMA_MIN_SUPPORTED: int = 1
TRACE_SCHEMA_MAX_SUPPORTED: int = 1

KNOWN_REPOS: frozenset = frozenset({"jarvis", "jarvis-prime", "reactor-core"})

# Read once at import time.  Tests should patch the module attribute
# directly rather than setting the env var after import.
CLOCK_SKEW_TOLERANCE_S: float = float(
    os.environ.get("JARVIS_TRACE_CLOCK_SKEW_TOLERANCE", "300.0")
)


# ---------------------------------------------------------------------------
# BoundaryType
# ---------------------------------------------------------------------------

class BoundaryType(str, Enum):
    """Classification of the boundary a trace crosses."""

    http = "http"
    ipc = "ipc"
    file_rpc = "file_rpc"
    event_bus = "event_bus"
    subprocess = "subprocess"
    internal = "internal"


# ---------------------------------------------------------------------------
# LamportClock
# ---------------------------------------------------------------------------

class LamportClock:
    """Thread-safe Lamport logical clock.

    Every ``tick()`` atomically increments and returns the new value.
    ``receive(incoming)`` merges with an incoming counter.
    """

    __slots__ = ("_value", "_lock")

    def __init__(self) -> None:
        self._value: int = 0
        self._lock: threading.Lock = threading.Lock()

    def tick(self) -> int:
        """Advance the clock by one and return the new value."""
        with self._lock:
            self._value += 1
            return self._value

    def receive(self, incoming_seq: int) -> int:
        """Merge with *incoming_seq* and advance.

        Sets the internal counter to ``max(local, incoming) + 1``.
        """
        with self._lock:
            self._value = max(self._value, incoming_seq) + 1
            return self._value

    @property
    def current(self) -> int:
        """Return the current counter value (no side-effects)."""
        with self._lock:
            return self._value


# ---------------------------------------------------------------------------
# TraceEnvelope
# ---------------------------------------------------------------------------

# Header prefix used for HTTP propagation.
_HEADER_PREFIX = "X-Trace-"

# Mapping: field name -> header suffix (without prefix).
# ts_mono_local and extra are intentionally excluded:
# - ts_mono_local is process-local and meaningless across boundaries
# - extra requires structured serialisation (not a flat header)
_FIELD_TO_HEADER_SUFFIX: Dict[str, str] = {
    "trace_id": "ID",
    "span_id": "Span-ID",
    "event_id": "Event-ID",
    "parent_span_id": "Parent-Span-ID",
    "sequence": "Sequence",
    "boot_id": "Boot-ID",
    "runtime_epoch_id": "Epoch-ID",
    "process_id": "Process-ID",
    "node_id": "Node-ID",
    "ts_wall_utc": "Wall-UTC",
    "repo": "Repo",
    "component": "Component",
    "operation": "Operation",
    "boundary_type": "Boundary",
    "caused_by_event_id": "Caused-By",
    "idempotency_key": "Idempotency-Key",
    "producer_version": "Producer-Version",
    "schema_version": "Schema-Version",
}

# Reverse: header name -> field name.
_HEADER_TO_FIELD: Dict[str, str] = {
    f"{_HEADER_PREFIX}{suffix}": fname
    for fname, suffix in _FIELD_TO_HEADER_SUFFIX.items()
}

# Fields that should be parsed as int when coming from headers.
_INT_HEADER_FIELDS = {"sequence", "process_id", "schema_version"}

# Fields that should be parsed as float when coming from headers.
_FLOAT_HEADER_FIELDS = {"ts_wall_utc"}

# Set of known dataclass field names (populated after class definition).
_ENVELOPE_FIELD_NAMES: set = set()


@dataclass(frozen=True)
class TraceEnvelope:
    """Immutable trace context envelope.

    Carries identity, causality, timing, and boundary metadata across
    service / process / repo boundaries.
    """

    trace_id: str
    span_id: str
    event_id: str
    parent_span_id: Optional[str]
    sequence: int
    boot_id: str
    runtime_epoch_id: str
    process_id: int
    node_id: str
    ts_wall_utc: float
    ts_mono_local: float
    repo: str
    component: str
    operation: str
    boundary_type: BoundaryType
    caused_by_event_id: Optional[str]
    idempotency_key: Optional[str]
    producer_version: str
    schema_version: int
    extra: Dict[str, Any] = field(default_factory=dict)

    # -- Serialisation helpers ------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (JSON-safe)."""
        d: Dict[str, Any] = {}
        for f in dc_fields(self):
            val = getattr(self, f.name)
            if f.name == "boundary_type":
                val = val.value if isinstance(val, BoundaryType) else str(val)
            d[f.name] = val
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TraceEnvelope":
        """Deserialise from a plain dict.

        Unknown keys are folded into ``extra``.
        Raises ``ValueError`` if required fields are missing.
        """
        known = _ENVELOPE_FIELD_NAMES
        extra = dict(d.get("extra", {}))
        kwargs: Dict[str, Any] = {}

        for key, val in d.items():
            if key == "extra":
                continue
            if key in known:
                kwargs[key] = val
            else:
                extra[key] = val

        # Validate required fields before construction.
        required = _ENVELOPE_FIELD_NAMES - {"extra"}
        missing = required - set(kwargs.keys())
        if missing:
            raise ValueError(
                f"TraceEnvelope.from_dict missing required fields: {sorted(missing)}"
            )

        # Coerce boundary_type from string.
        bt = kwargs.get("boundary_type")
        if bt is not None and not isinstance(bt, BoundaryType):
            try:
                kwargs["boundary_type"] = BoundaryType(bt)
            except ValueError:
                kwargs["boundary_type"] = BoundaryType.internal

        kwargs["extra"] = extra
        return cls(**kwargs)

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "TraceEnvelope":
        """Deserialise from a JSON string."""
        return cls.from_dict(json.loads(s))

    # -- HTTP header propagation ----------------------------------------------

    def to_headers(self) -> Dict[str, str]:
        """Emit trace context as ``X-Trace-*`` HTTP headers."""
        headers: Dict[str, str] = {}
        for fname, suffix in _FIELD_TO_HEADER_SUFFIX.items():
            val = getattr(self, fname)
            if val is None:
                val = ""
            if fname == "boundary_type":
                val = val.value if isinstance(val, BoundaryType) else str(val)
            headers[f"{_HEADER_PREFIX}{suffix}"] = str(val)
        return headers

    @classmethod
    def from_headers(cls, h: Dict[str, str]) -> Optional["TraceEnvelope"]:
        """Reconstruct a TraceEnvelope from ``X-Trace-*`` headers.

        Returns ``None`` if the mandatory ``X-Trace-ID`` header is absent.
        """
        if f"{_HEADER_PREFIX}ID" not in h:
            return None

        kwargs: Dict[str, Any] = {}
        for header_name, field_name in _HEADER_TO_FIELD.items():
            raw = h.get(header_name, "")
            if raw == "" and field_name in (
                "parent_span_id",
                "caused_by_event_id",
                "idempotency_key",
            ):
                kwargs[field_name] = None
                continue
            if field_name in _INT_HEADER_FIELDS:
                try:
                    kwargs[field_name] = int(raw) if raw else 0
                except (ValueError, TypeError):
                    kwargs[field_name] = 0
            elif field_name in _FLOAT_HEADER_FIELDS:
                try:
                    kwargs[field_name] = float(raw) if raw else 0.0
                except (ValueError, TypeError):
                    kwargs[field_name] = 0.0
            elif field_name == "boundary_type":
                try:
                    kwargs[field_name] = BoundaryType(raw) if raw else BoundaryType.internal
                except ValueError:
                    kwargs[field_name] = BoundaryType.internal
            else:
                kwargs[field_name] = raw

        # Fields not transported via headers get sensible defaults.
        # ts_mono_local is process-local; 0.0 is a sentinel meaning
        # "received from wire, no local monotonic available".
        kwargs.setdefault("ts_mono_local", 0.0)
        kwargs.setdefault("extra", {})

        return cls(**kwargs)


# Populate known field names now that the class is defined.
_ENVELOPE_FIELD_NAMES = {f.name for f in dc_fields(TraceEnvelope)}


# ---------------------------------------------------------------------------
# TraceEnvelopeFactory
# ---------------------------------------------------------------------------

def _new_id() -> str:
    """Generate a 16-char hex identifier from uuid4."""
    return uuid.uuid4().hex[:16]


class TraceEnvelopeFactory:
    """Factory for creating related :class:`TraceEnvelope` instances.

    Holds per-process configuration (repo, boot_id, etc.) and an internal
    :class:`LamportClock` so callers don't need to manage IDs or sequencing.
    """

    __slots__ = (
        "_repo",
        "_boot_id",
        "_runtime_epoch_id",
        "_node_id",
        "_producer_version",
        "_clock",
    )

    def __init__(
        self,
        repo: str,
        boot_id: str,
        runtime_epoch_id: str,
        node_id: str,
        producer_version: str,
    ) -> None:
        self._repo = repo
        self._boot_id = boot_id
        self._runtime_epoch_id = runtime_epoch_id
        self._node_id = node_id
        self._producer_version = producer_version
        self._clock = LamportClock()

    @property
    def runtime_epoch_id(self) -> str:
        return self._runtime_epoch_id

    def _base_kwargs(self, component: str, operation: str, boundary_type: BoundaryType) -> Dict[str, Any]:
        """Common fields shared across all envelope creation methods."""
        return {
            "boot_id": self._boot_id,
            "runtime_epoch_id": self._runtime_epoch_id,
            "process_id": os.getpid(),
            "node_id": self._node_id,
            "ts_wall_utc": time.time(),
            "ts_mono_local": time.monotonic(),
            "repo": self._repo,
            "component": component,
            "operation": operation,
            "boundary_type": boundary_type,
            "producer_version": self._producer_version,
            "schema_version": TRACE_SCHEMA_VERSION,
            "extra": {},
        }

    def create_root(
        self,
        component: str,
        operation: str,
        boundary_type: BoundaryType = BoundaryType.internal,
        idempotency_key: Optional[str] = None,
    ) -> TraceEnvelope:
        """Create a root envelope (new trace, no parent)."""
        kw = self._base_kwargs(component, operation, boundary_type)
        kw.update(
            trace_id=_new_id(),
            span_id=_new_id(),
            event_id=_new_id(),
            parent_span_id=None,
            sequence=self._clock.tick(),
            caused_by_event_id=None,
            idempotency_key=idempotency_key,
        )
        return TraceEnvelope(**kw)

    def create_child(
        self,
        parent: TraceEnvelope,
        component: str,
        operation: str,
        boundary_type: BoundaryType = BoundaryType.internal,
        caused_by_event_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> TraceEnvelope:
        """Create a child envelope inheriting the parent's trace_id."""
        kw = self._base_kwargs(component, operation, boundary_type)
        kw.update(
            trace_id=parent.trace_id,
            span_id=_new_id(),
            event_id=_new_id(),
            parent_span_id=parent.span_id,
            sequence=self._clock.tick(),
            caused_by_event_id=caused_by_event_id,
            idempotency_key=idempotency_key,
        )
        return TraceEnvelope(**kw)

    def create_event_from(self, envelope: TraceEnvelope) -> TraceEnvelope:
        """Create a new event within the same span (same trace_id + span_id)."""
        kw = self._base_kwargs(envelope.component, envelope.operation, envelope.boundary_type)
        kw.update(
            trace_id=envelope.trace_id,
            span_id=envelope.span_id,
            event_id=_new_id(),
            parent_span_id=envelope.parent_span_id,
            sequence=self._clock.tick(),
            caused_by_event_id=envelope.caused_by_event_id,
            idempotency_key=envelope.idempotency_key,
        )
        return TraceEnvelope(**kw)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_envelope(env: TraceEnvelope) -> List[str]:
    """Validate structural integrity of *env*.

    Returns a list of human-readable error strings. An empty list means valid.
    """
    errors: List[str] = []

    # ID fields: non-empty, max 64 chars.
    for id_field in ("trace_id", "span_id", "event_id"):
        val = getattr(env, id_field)
        if not val:
            errors.append(f"{id_field} must be non-empty")
        elif len(val) > 64:
            errors.append(f"{id_field} exceeds 64 character limit (got {len(val)})")

    # Sequence must be positive.
    if env.sequence <= 0:
        errors.append(f"sequence must be > 0 (got {env.sequence})")

    # Repo must be recognised.
    if env.repo not in KNOWN_REPOS:
        errors.append(f"repo '{env.repo}' not in KNOWN_REPOS {KNOWN_REPOS}")

    # Wall-clock skew check.
    now = time.time()
    skew = abs(env.ts_wall_utc - now)
    if skew > CLOCK_SKEW_TOLERANCE_S:
        errors.append(
            f"ts_wall_utc clock skew {skew:.1f}s exceeds tolerance "
            f"{CLOCK_SKEW_TOLERANCE_S:.1f}s"
        )

    # Monotonic must be positive for locally-created envelopes.
    # 0.0 is a valid sentinel for cross-boundary envelopes received via
    # headers (from_headers defaults to 0.0 since monotonic time is
    # process-local and cannot be transmitted).  Negative is always invalid.
    if env.ts_mono_local < 0:
        errors.append(f"ts_mono_local must be >= 0 (got {env.ts_mono_local})")

    # Schema version in supported range.
    if env.schema_version < TRACE_SCHEMA_MIN_SUPPORTED:
        errors.append(
            f"schema_version {env.schema_version} below minimum "
            f"{TRACE_SCHEMA_MIN_SUPPORTED}"
        )
    elif env.schema_version > TRACE_SCHEMA_MAX_SUPPORTED:
        errors.append(
            f"schema_version {env.schema_version} above maximum "
            f"{TRACE_SCHEMA_MAX_SUPPORTED}"
        )

    return errors


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------

@dataclass
class CompatibilityResult:
    """Outcome of a schema version compatibility check."""

    accepted: bool
    warning: Optional[str]


def check_schema_compatibility(
    schema_version: int,
    boundary_critical: bool,
) -> CompatibilityResult:
    """Determine whether *schema_version* is compatible.

    - Below minimum: always rejected.
    - Above maximum on a critical boundary: rejected.
    - Above maximum on a non-critical boundary: accepted with warning.
    - Current version: accepted, no warning.
    """
    if schema_version < TRACE_SCHEMA_MIN_SUPPORTED:
        return CompatibilityResult(
            accepted=False,
            warning=f"Schema version {schema_version} is below minimum "
            f"supported {TRACE_SCHEMA_MIN_SUPPORTED}",
        )

    if schema_version > TRACE_SCHEMA_MAX_SUPPORTED:
        if boundary_critical:
            return CompatibilityResult(
                accepted=False,
                warning=f"Schema version {schema_version} exceeds maximum "
                f"supported {TRACE_SCHEMA_MAX_SUPPORTED} on critical boundary",
            )
        return CompatibilityResult(
            accepted=True,
            warning=f"Schema version {schema_version} exceeds maximum "
            f"supported {TRACE_SCHEMA_MAX_SUPPORTED}; proceeding on "
            f"non-critical boundary",
        )

    # Within supported range — fully compatible.
    return CompatibilityResult(accepted=True, warning=None)
