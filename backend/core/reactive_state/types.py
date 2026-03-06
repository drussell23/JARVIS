"""Reactive state store core data types -- stdlib only, zero JARVIS imports.

Defines the frozen dataclasses and enums used by the reactive state store,
its journal, and the ownership / schema registries.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only.
* All record types are ``@dataclass(frozen=True)`` (immutable value objects).
* ``WriteStatus`` is a ``str`` enum so it serializes naturally to JSON.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Optional


# ── Enums ───────────────────────────────────────────────────────────────


class WriteStatus(str, enum.Enum):
    """Outcome of a state-store write attempt."""

    OK = "OK"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    OWNERSHIP_REJECTED = "OWNERSHIP_REJECTED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    EPOCH_STALE = "EPOCH_STALE"
    POLICY_REJECTED = "POLICY_REJECTED"


# ── Frozen value objects ────────────────────────────────────────────────


@dataclass(frozen=True)
class StateEntry:
    """A single key-value pair in the reactive state store.

    Attributes
    ----------
    key:
        Dotted key name (e.g. ``"gcp.vm_ready"``).
    value:
        Arbitrary payload -- any JSON-serializable value.
    version:
        Monotonically increasing per-key version (starts at 1).
    epoch:
        Store-wide epoch; bumped on ownership / topology changes.
    writer:
        Logical identity of the component that last wrote this key.
    origin:
        How the value was produced: ``"explicit"``, ``"default"``,
        or ``"derived"``.
    updated_at_mono:
        ``time.monotonic()`` timestamp of the last write.
    updated_at_unix_ms:
        Wall-clock time in milliseconds since epoch.
    """

    key: str
    value: Any
    version: int
    epoch: int
    writer: str
    origin: str  # "explicit" | "default" | "derived"
    updated_at_mono: float
    updated_at_unix_ms: int


@dataclass(frozen=True)
class JournalEntry:
    """An immutable record in the append-only mutation journal.

    Attributes
    ----------
    global_revision:
        Store-wide monotonic revision number.
    key:
        The key that was mutated.
    value:
        New value after mutation.
    previous_value:
        Value before mutation (``None`` for first write).
    version:
        Per-key version after this mutation.
    epoch:
        Store epoch at the time of mutation.
    writer:
        Logical writer identity.
    writer_session_id:
        Session-scoped identifier for the writer instance.
    origin:
        How the value was produced.
    consistency_group:
        Optional group tag for multi-key atomic writes.
    timestamp_unix_ms:
        Wall-clock time in milliseconds since epoch.
    checksum:
        Integrity checksum of the journal entry.
    """

    global_revision: int
    key: str
    value: Any
    previous_value: Any
    version: int
    epoch: int
    writer: str
    writer_session_id: str
    origin: str
    consistency_group: Optional[str]
    timestamp_unix_ms: int
    checksum: str


@dataclass(frozen=True)
class WriteRejection:
    """Diagnostic record produced when a write is rejected.

    Attributes
    ----------
    key:
        The key the writer attempted to mutate.
    writer:
        Logical writer identity.
    writer_session_id:
        Session-scoped identifier for the writer instance.
    reason:
        The ``WriteStatus`` code explaining the rejection.
    epoch:
        Store epoch at the time of rejection.
    attempted_version:
        The version the writer expected to write.
    current_version:
        The actual current version of the key.
    global_revision_at_reject:
        Store-wide revision at rejection time.
    timestamp_mono:
        ``time.monotonic()`` timestamp of the rejection.
    """

    key: str
    writer: str
    writer_session_id: str
    reason: WriteStatus
    epoch: int
    attempted_version: int
    current_version: int
    global_revision_at_reject: int
    timestamp_mono: float


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a state-store write attempt.

    For ``WriteStatus.OK``, ``entry`` holds the new ``StateEntry`` and
    ``rejection`` is ``None``.  For any failure status, ``rejection``
    holds the diagnostic ``WriteRejection`` and ``entry`` is ``None``.
    """

    status: WriteStatus
    entry: Optional[StateEntry] = None
    rejection: Optional[WriteRejection] = None
