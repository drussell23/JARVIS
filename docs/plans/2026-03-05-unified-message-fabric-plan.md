# Unified Message Fabric (UMF) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate dual communication split-brain (Trinity Event Bus + Reactor Bridge) by building one canonical messaging system (UMF) governed by the Unified Supervisor, across all 3 repos.

**Architecture:** Modular UMF under `backend/core/umf/` with canonical envelope types, dedup ledger (SQLite WAL), delivery engine, pluggable transport adapters, and heartbeat projection. Reuses Disease 2 types (`SubsystemState`, `ContractGate`, `ProcessIdentity`) directly. Each repo gets a thin UMF client SDK. Shadow mode validates parity before legacy path removal.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, sqlite3 (WAL mode), HMAC-SHA256 (from `managed_mode.py`), existing file/Redis transports.

**Design doc:** `docs/plans/2026-03-05-unified-message-fabric-design.md`

**Open questions resolved:**
1. **Dedup ledger store:** SQLite WAL — already stdlib, durable, bounded via TTL compaction
2. **Partition key strategy:** `{repo}.{component}` default, high-volume streams use `.{stream}` suffix
3. **Key rotation:** Key ID in signature block; accept current + previous key_id during rotation window
4. **Replay retention:** 1h for lifecycle/command, 15m for telemetry, 6h for heartbeat
5. **Parity soak threshold:** 4h shadow soak at >= 99.9% parity before promotion

---

## Wave 0 — Foundation Types (Tasks 1-4)

### Task 1: UMF Envelope Types Module

**Files:**
- Create: `backend/core/umf/__init__.py`
- Create: `backend/core/umf/types.py`
- Test: `tests/unit/core/umf/test_umf_types.py`

**Context:** This is the canonical envelope schema that ALL three repos will use. Zero imports from orchestrator/USP — stdlib only, same portability rule as `managed_mode.py`.

**Step 1: Create package and write the failing test**

Create `tests/unit/core/umf/__init__.py` (empty) and `tests/unit/core/umf/test_umf_types.py`:

```python
"""Tests for UMF canonical envelope types (Task 1)."""
import json
import time
import pytest


class TestUmfEnvelope:
    """Validate canonical envelope shape and serialization."""

    def test_envelope_has_all_required_fields(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="lifecycle",
            kind="heartbeat",
            source=MessageSource(repo="jarvis", component="supervisor",
                                 instance_id="i-1", session_id="s-1"),
            target=MessageTarget(repo="jarvis-prime", component="orchestrator"),
            payload={"state": "ready"},
        )
        assert msg.schema_version == "umf.v1"
        assert msg.message_id  # auto-generated UUID
        assert msg.stream == "lifecycle"
        assert msg.kind == "heartbeat"
        assert msg.source.repo == "jarvis"
        assert msg.target.repo == "jarvis-prime"
        assert msg.observed_at_unix_ms > 0

    def test_envelope_serialization_roundtrip(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command",
            kind="command",
            source=MessageSource(repo="jarvis", component="supervisor",
                                 instance_id="i-1", session_id="s-1"),
            target=MessageTarget(repo="reactor-core", component="trainer"),
            payload={"action": "start_training"},
        )
        d = msg.to_dict()
        restored = UmfMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.stream == msg.stream
        assert restored.payload == msg.payload

    def test_envelope_json_deterministic(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="event",
            kind="event",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="broadcast", component="*"),
            payload={"z": 1, "a": 2},
        )
        j1 = msg.to_json()
        j2 = msg.to_json()
        assert j1 == j2

    def test_default_routing_fields(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="telemetry",
            kind="event",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="broadcast", component="*"),
            payload={},
        )
        assert msg.routing_partition_key == "jarvis.a"
        assert msg.routing_priority == "normal"
        assert msg.routing_ttl_ms == 30000
        assert msg.routing_deadline_unix_ms == 0

    def test_causality_fields_default_none(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command",
            kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
        )
        assert msg.causality_trace_id  # auto-generated
        assert msg.causality_span_id   # auto-generated
        assert msg.causality_parent_message_id is None
        assert msg.causality_sequence == 0

    def test_signature_fields_default_empty(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command",
            kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
        )
        assert msg.signature_alg == ""
        assert msg.signature_key_id == ""
        assert msg.signature_value == ""

    def test_is_expired_respects_ttl(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command",
            kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
            routing_ttl_ms=1,  # 1ms TTL
            observed_at_unix_ms=int((time.time() - 10) * 1000),  # 10s ago
        )
        assert msg.is_expired() is True

    def test_not_expired_within_ttl(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command",
            kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
            routing_ttl_ms=60000,  # 60s TTL
        )
        assert msg.is_expired() is False


class TestReasonCode:
    """Validate deterministic reason taxonomy enum."""

    def test_all_reason_codes_exist(self):
        from backend.core.umf.types import RejectReason
        expected = {
            "schema_mismatch", "sig_invalid", "capability_mismatch",
            "ttl_expired", "deadline_expired", "dedup_duplicate",
            "route_unavailable", "backpressure_drop", "circuit_open",
            "handler_timeout",
        }
        actual = {r.value for r in RejectReason}
        assert expected == actual


class TestStreamAndKind:
    """Validate stream and kind enums."""

    def test_stream_values(self):
        from backend.core.umf.types import Stream
        expected = {"lifecycle", "command", "event", "heartbeat", "telemetry"}
        assert {s.value for s in Stream} == expected

    def test_kind_values(self):
        from backend.core.umf.types import Kind
        expected = {"command", "event", "heartbeat", "ack", "nack"}
        assert {k.value for k in Kind} == expected
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_types.py -v`
Expected: FAIL (ModuleNotFoundError: No module named 'backend.core.umf')

**Step 3: Write minimal implementation**

Create `backend/core/umf/__init__.py`:

```python
"""Unified Message Fabric (UMF) — canonical cross-repo messaging."""
```

Create `backend/core/umf/types.py`:

```python
"""UMF canonical envelope types.

Stdlib-only. No imports from orchestrator, USP, or any JARVIS module.
Designed to be importable by all three repos.

Schema version follows semver. Compatibility rule:
  * Major must match exactly.
  * Minor may differ by at most 1 (N / N-1).
  * Patch is ignored.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UMF_SCHEMA_VERSION: str = "umf.v1"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Stream(Enum):
    LIFECYCLE = "lifecycle"
    COMMAND = "command"
    EVENT = "event"
    HEARTBEAT = "heartbeat"
    TELEMETRY = "telemetry"


class Kind(Enum):
    COMMAND = "command"
    EVENT = "event"
    HEARTBEAT = "heartbeat"
    ACK = "ack"
    NACK = "nack"


class Priority(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class RejectReason(Enum):
    SCHEMA_MISMATCH = "schema_mismatch"
    SIG_INVALID = "sig_invalid"
    CAPABILITY_MISMATCH = "capability_mismatch"
    TTL_EXPIRED = "ttl_expired"
    DEADLINE_EXPIRED = "deadline_expired"
    DEDUP_DUPLICATE = "dedup_duplicate"
    ROUTE_UNAVAILABLE = "route_unavailable"
    BACKPRESSURE_DROP = "backpressure_drop"
    CIRCUIT_OPEN = "circuit_open"
    HANDLER_TIMEOUT = "handler_timeout"


class ReserveResult(Enum):
    RESERVED = "reserved"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


# ---------------------------------------------------------------------------
# Nested value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageSource:
    repo: str
    component: str
    instance_id: str
    session_id: str


@dataclass(frozen=True)
class MessageTarget:
    repo: str
    component: str


# ---------------------------------------------------------------------------
# Canonical envelope
# ---------------------------------------------------------------------------

def _uuid7_hex() -> str:
    """Generate a UUID v4 hex string (v7 not in stdlib; v4 is fine for IDs)."""
    return uuid.uuid4().hex


def _now_ms() -> int:
    return int(time.time() * 1000)


def _trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _span_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class UmfMessage:
    """Canonical UMF envelope — the single wire schema for all cross-repo messages."""

    # Required fields
    stream: str
    kind: str
    source: MessageSource
    target: MessageTarget
    payload: Dict[str, Any]

    # Identity (auto-generated)
    schema_version: str = field(default=UMF_SCHEMA_VERSION)
    message_id: str = field(default_factory=_uuid7_hex)
    idempotency_key: str = field(default="")

    # Routing
    routing_partition_key: str = field(default="")
    routing_priority: str = field(default="normal")
    routing_ttl_ms: int = field(default=30000)
    routing_deadline_unix_ms: int = field(default=0)

    # Causality
    causality_trace_id: str = field(default_factory=_trace_id)
    causality_span_id: str = field(default_factory=_span_id)
    causality_parent_message_id: Optional[str] = field(default=None)
    causality_sequence: int = field(default=0)

    # Contract
    contract_capability_hash: str = field(default="")
    contract_schema_hash: str = field(default="")
    contract_compat_window: str = field(default="N|N-1")

    # Timing
    observed_at_unix_ms: int = field(default_factory=_now_ms)

    # Signature (empty = unsigned)
    signature_alg: str = field(default="")
    signature_key_id: str = field(default="")
    signature_value: str = field(default="")

    def __post_init__(self) -> None:
        if not self.routing_partition_key:
            self.routing_partition_key = f"{self.source.repo}.{self.source.component}"
        if not self.idempotency_key:
            self.idempotency_key = self.message_id

    def is_expired(self) -> bool:
        """Check if message has exceeded its TTL."""
        if self.routing_ttl_ms <= 0:
            return False
        age_ms = _now_ms() - self.observed_at_unix_ms
        return age_ms > self.routing_ttl_ms

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON transport."""
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "idempotency_key": self.idempotency_key,
            "stream": self.stream,
            "kind": self.kind,
            "source": {
                "repo": self.source.repo,
                "component": self.source.component,
                "instance_id": self.source.instance_id,
                "session_id": self.source.session_id,
            },
            "target": {
                "repo": self.target.repo,
                "component": self.target.component,
            },
            "routing": {
                "partition_key": self.routing_partition_key,
                "priority": self.routing_priority,
                "ttl_ms": self.routing_ttl_ms,
                "deadline_unix_ms": self.routing_deadline_unix_ms,
            },
            "causality": {
                "trace_id": self.causality_trace_id,
                "span_id": self.causality_span_id,
                "parent_message_id": self.causality_parent_message_id,
                "sequence": self.causality_sequence,
            },
            "contract": {
                "capability_hash": self.contract_capability_hash,
                "schema_hash": self.contract_schema_hash,
                "compat_window": self.contract_compat_window,
            },
            "payload": self.payload,
            "observed_at_unix_ms": self.observed_at_unix_ms,
            "signature": {
                "alg": self.signature_alg,
                "key_id": self.signature_key_id,
                "value": self.signature_value,
            },
        }

    def to_json(self) -> str:
        """Deterministic JSON serialization."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UmfMessage":
        """Deserialize from dict."""
        src = d["source"]
        tgt = d["target"]
        routing = d.get("routing", {})
        causality = d.get("causality", {})
        contract = d.get("contract", {})
        sig = d.get("signature", {})
        return cls(
            schema_version=d.get("schema_version", UMF_SCHEMA_VERSION),
            message_id=d["message_id"],
            idempotency_key=d.get("idempotency_key", d["message_id"]),
            stream=d["stream"],
            kind=d["kind"],
            source=MessageSource(
                repo=src["repo"], component=src["component"],
                instance_id=src["instance_id"], session_id=src["session_id"],
            ),
            target=MessageTarget(repo=tgt["repo"], component=tgt["component"]),
            routing_partition_key=routing.get("partition_key", ""),
            routing_priority=routing.get("priority", "normal"),
            routing_ttl_ms=routing.get("ttl_ms", 30000),
            routing_deadline_unix_ms=routing.get("deadline_unix_ms", 0),
            causality_trace_id=causality.get("trace_id", ""),
            causality_span_id=causality.get("span_id", ""),
            causality_parent_message_id=causality.get("parent_message_id"),
            causality_sequence=causality.get("sequence", 0),
            contract_capability_hash=contract.get("capability_hash", ""),
            contract_schema_hash=contract.get("schema_hash", ""),
            contract_compat_window=contract.get("compat_window", "N|N-1"),
            payload=d.get("payload", {}),
            observed_at_unix_ms=d.get("observed_at_unix_ms", _now_ms()),
            signature_alg=sig.get("alg", ""),
            signature_key_id=sig.get("key_id", ""),
            signature_value=sig.get("value", ""),
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_types.py -v`
Expected: 12 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/__init__.py backend/core/umf/types.py \
       tests/unit/core/umf/__init__.py tests/unit/core/umf/test_umf_types.py
git commit -m "feat(umf): add canonical envelope types module (Task 1)"
```

---

### Task 2: UMF Contract Gate Integration

**Files:**
- Create: `backend/core/umf/contract_gate.py`
- Test: `tests/unit/core/umf/test_umf_contract_gate.py`

**Context:** Validates incoming UMF messages against compatibility rules. Reuses `ContractGate.is_schema_compatible()` from Disease 2's `root_authority_types.py` and `verify_hmac_auth()` from `managed_mode.py`.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_umf_contract_gate.py`:

```python
"""Tests for UMF contract gate (Task 2)."""
import time
import pytest


def _make_msg(**overrides):
    from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
    defaults = dict(
        stream="command", kind="command",
        source=MessageSource(repo="jarvis", component="sup",
                             instance_id="i", session_id="s"),
        target=MessageTarget(repo="jarvis-prime", component="orch"),
        payload={"x": 1},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestUmfContractGate:

    def test_valid_message_passes(self):
        from backend.core.umf.contract_gate import validate_message
        from backend.core.umf.types import UMF_SCHEMA_VERSION
        msg = _make_msg(schema_version=UMF_SCHEMA_VERSION)
        result = validate_message(msg)
        assert result.accepted is True
        assert result.reject_reason is None

    def test_unknown_schema_version_rejected(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(schema_version="umf.v99")
        result = validate_message(msg)
        assert result.accepted is False
        assert result.reject_reason == "schema_mismatch"

    def test_n_minus_1_schema_accepted(self):
        from backend.core.umf.contract_gate import validate_message
        # umf.v1 should accept umf.v1 (same version)
        msg = _make_msg(schema_version="umf.v1")
        result = validate_message(msg)
        assert result.accepted is True

    def test_expired_ttl_rejected(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(
            routing_ttl_ms=1,
            observed_at_unix_ms=int((time.time() - 60) * 1000),
        )
        result = validate_message(msg)
        assert result.accepted is False
        assert result.reject_reason == "ttl_expired"

    def test_expired_deadline_rejected(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(
            routing_deadline_unix_ms=int((time.time() - 60) * 1000),
        )
        result = validate_message(msg)
        assert result.accepted is False
        assert result.reject_reason == "deadline_expired"

    def test_capability_hash_mismatch_rejected(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(contract_capability_hash="abc123")
        result = validate_message(msg, expected_capability_hash="xyz789")
        assert result.accepted is False
        assert result.reject_reason == "capability_mismatch"

    def test_capability_hash_not_checked_when_not_required(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(contract_capability_hash="abc123")
        result = validate_message(msg)  # no expected hash
        assert result.accepted is True

    def test_hmac_invalid_rejected(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg(
            signature_alg="HMAC-SHA256",
            signature_key_id="k1",
            signature_value="bad_sig",
        )
        result = validate_message(msg, hmac_secret="my_secret", session_id="s1")
        assert result.accepted is False
        assert result.reject_reason == "sig_invalid"

    def test_unsigned_message_passes_when_no_secret(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg()  # no signature
        result = validate_message(msg)  # no secret required
        assert result.accepted is True

    def test_result_includes_message_id(self):
        from backend.core.umf.contract_gate import validate_message
        msg = _make_msg()
        result = validate_message(msg)
        assert result.message_id == msg.message_id
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_contract_gate.py -v`
Expected: FAIL (ModuleNotFoundError)

**Step 3: Write minimal implementation**

Create `backend/core/umf/contract_gate.py`:

```python
"""UMF message contract validation gate.

Validates incoming UMF messages against schema version, TTL/deadline,
capability hash, and HMAC signature rules. Returns structured results
with deterministic reason codes — never silently drops.

Reuses ContractGate compatibility logic from root_authority_types and
HMAC verification from managed_mode.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from backend.core.umf.types import UMF_SCHEMA_VERSION, UmfMessage

# Accepted schema versions (N and N-1)
_ACCEPTED_SCHEMAS = frozenset({UMF_SCHEMA_VERSION})


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a UMF message against the contract gate."""
    accepted: bool
    message_id: str
    reject_reason: Optional[str] = None


def validate_message(
    msg: UmfMessage,
    *,
    expected_capability_hash: Optional[str] = None,
    hmac_secret: Optional[str] = None,
    session_id: Optional[str] = None,
    accepted_schemas: Optional[frozenset] = None,
) -> ValidationResult:
    """Validate a UMF message. Returns ValidationResult (never raises)."""
    schemas = accepted_schemas or _ACCEPTED_SCHEMAS

    # 1. Schema version check
    if msg.schema_version not in schemas:
        return ValidationResult(
            accepted=False, message_id=msg.message_id,
            reject_reason="schema_mismatch",
        )

    # 2. TTL expiry check
    if msg.is_expired():
        return ValidationResult(
            accepted=False, message_id=msg.message_id,
            reject_reason="ttl_expired",
        )

    # 3. Absolute deadline check
    if msg.routing_deadline_unix_ms > 0:
        now_ms = int(time.time() * 1000)
        if now_ms > msg.routing_deadline_unix_ms:
            return ValidationResult(
                accepted=False, message_id=msg.message_id,
                reject_reason="deadline_expired",
            )

    # 4. Capability hash check (only if expected hash provided)
    if expected_capability_hash and msg.contract_capability_hash:
        if msg.contract_capability_hash != expected_capability_hash:
            return ValidationResult(
                accepted=False, message_id=msg.message_id,
                reject_reason="capability_mismatch",
            )

    # 5. HMAC signature check (only if message is signed AND secret provided)
    if msg.signature_alg and msg.signature_value and hmac_secret:
        if not _verify_signature(msg, hmac_secret, session_id or ""):
            return ValidationResult(
                accepted=False, message_id=msg.message_id,
                reject_reason="sig_invalid",
            )

    return ValidationResult(accepted=True, message_id=msg.message_id)


def _verify_signature(msg: UmfMessage, secret: str, session_id: str) -> bool:
    """Verify HMAC-SHA256 signature on a UMF message."""
    try:
        from backend.core.managed_mode import verify_hmac_auth
        return verify_hmac_auth(msg.signature_value, session_id, secret)
    except (ImportError, Exception):
        return False
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_contract_gate.py -v`
Expected: 10 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/contract_gate.py tests/unit/core/umf/test_umf_contract_gate.py
git commit -m "feat(umf): add contract gate with schema/TTL/HMAC validation (Task 2)"
```

---

### Task 3: Dedup Ledger (SQLite WAL)

**Files:**
- Create: `backend/core/umf/dedup_ledger.py`
- Test: `tests/unit/core/umf/test_dedup_ledger.py`

**Context:** The dedup ledger provides `reserve/commit/abort` semantics for effectively-once delivery. Uses SQLite in WAL mode for durability. TTL compaction prevents unbounded growth.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_dedup_ledger.py`:

```python
"""Tests for UMF dedup ledger (Task 3)."""
import asyncio
import time
import pytest


class TestDedupLedger:

    @pytest.mark.asyncio
    async def test_reserve_new_key_returns_reserved(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        result = await ledger.reserve("key-1", "msg-1", ttl_ms=30000)
        assert result.value == "reserved"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_duplicate_key_returns_duplicate(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        await ledger.reserve("key-1", "msg-1", ttl_ms=30000)
        result = await ledger.reserve("key-1", "msg-2", ttl_ms=30000)
        assert result.value == "duplicate"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_commit_marks_entry(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        await ledger.reserve("key-1", "msg-1", ttl_ms=30000)
        await ledger.commit("msg-1", "effect-hash-abc")
        entry = await ledger.get("msg-1")
        assert entry is not None
        assert entry["committed"] is True
        assert entry["effect_hash"] == "effect-hash-abc"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_abort_allows_replay(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        await ledger.reserve("key-1", "msg-1", ttl_ms=30000)
        await ledger.abort("msg-1", "test-abort")
        # After abort, same key can be reserved again
        result = await ledger.reserve("key-1", "msg-3", ttl_ms=30000)
        assert result.value == "reserved"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_ttl_expiration_allows_reuse(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        # Reserve with 1ms TTL
        await ledger.reserve("key-1", "msg-1", ttl_ms=1)
        await asyncio.sleep(0.01)  # wait for expiry
        await ledger.compact()
        result = await ledger.reserve("key-1", "msg-2", ttl_ms=30000)
        assert result.value == "reserved"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()
        entry = await ledger.get("nonexistent")
        assert entry is None
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_concurrent_reserves_one_winner(self, tmp_path):
        from backend.core.umf.dedup_ledger import SqliteDedupLedger, ReserveResult
        ledger = SqliteDedupLedger(db_path=tmp_path / "dedup.db")
        await ledger.start()

        results = []
        async def try_reserve(msg_id):
            r = await ledger.reserve("shared-key", msg_id, ttl_ms=30000)
            results.append(r)

        await asyncio.gather(
            try_reserve("msg-a"),
            try_reserve("msg-b"),
            try_reserve("msg-c"),
        )
        reserved_count = sum(1 for r in results if r == ReserveResult.RESERVED)
        assert reserved_count == 1
        await ledger.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_dedup_ledger.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/dedup_ledger.py`:

```python
"""UMF dedup ledger backed by SQLite WAL.

Provides reserve/commit/abort semantics for effectively-once delivery.
TTL compaction prevents unbounded growth.

Stdlib only — no external dependencies.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.umf.types import ReserveResult


class SqliteDedupLedger:
    """SQLite WAL-backed dedup ledger."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_ledger (
                idempotency_key TEXT NOT NULL,
                message_id TEXT PRIMARY KEY,
                reserved_at_ms INTEGER NOT NULL,
                ttl_ms INTEGER NOT NULL,
                committed INTEGER NOT NULL DEFAULT 0,
                effect_hash TEXT DEFAULT '',
                aborted INTEGER NOT NULL DEFAULT 0,
                abort_reason TEXT DEFAULT ''
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_idem_key
            ON dedup_ledger(idempotency_key)
        """)

    async def stop(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    async def reserve(
        self, idempotency_key: str, message_id: str, ttl_ms: int,
    ) -> ReserveResult:
        async with self._lock:
            assert self._conn is not None
            now_ms = int(time.time() * 1000)
            # Check existing entry for this idempotency key
            row = self._conn.execute(
                "SELECT message_id, reserved_at_ms, ttl_ms, aborted "
                "FROM dedup_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                existing_msg_id, reserved_at, existing_ttl, aborted = row
                if aborted:
                    # Aborted entries can be overwritten
                    self._conn.execute(
                        "DELETE FROM dedup_ledger WHERE message_id = ?",
                        (existing_msg_id,),
                    )
                else:
                    # Check if expired
                    if (now_ms - reserved_at) > existing_ttl:
                        self._conn.execute(
                            "DELETE FROM dedup_ledger WHERE message_id = ?",
                            (existing_msg_id,),
                        )
                    else:
                        return ReserveResult.DUPLICATE
            try:
                self._conn.execute(
                    "INSERT INTO dedup_ledger "
                    "(idempotency_key, message_id, reserved_at_ms, ttl_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (idempotency_key, message_id, now_ms, ttl_ms),
                )
                return ReserveResult.RESERVED
            except sqlite3.IntegrityError:
                return ReserveResult.CONFLICT

    async def commit(self, message_id: str, effect_hash: str) -> None:
        async with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "UPDATE dedup_ledger SET committed = 1, effect_hash = ? "
                "WHERE message_id = ?",
                (effect_hash, message_id),
            )

    async def abort(self, message_id: str, reason: str) -> None:
        async with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "UPDATE dedup_ledger SET aborted = 1, abort_reason = ? "
                "WHERE message_id = ?",
                (reason, message_id),
            )

    async def get(self, message_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT idempotency_key, message_id, reserved_at_ms, ttl_ms, "
                "committed, effect_hash, aborted, abort_reason "
                "FROM dedup_ledger WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "idempotency_key": row[0],
                "message_id": row[1],
                "reserved_at_ms": row[2],
                "ttl_ms": row[3],
                "committed": bool(row[4]),
                "effect_hash": row[5],
                "aborted": bool(row[6]),
                "abort_reason": row[7],
            }

    async def compact(self) -> int:
        """Remove expired entries. Returns count of removed rows."""
        async with self._lock:
            assert self._conn is not None
            now_ms = int(time.time() * 1000)
            cursor = self._conn.execute(
                "DELETE FROM dedup_ledger "
                "WHERE (? - reserved_at_ms) > ttl_ms",
                (now_ms,),
            )
            return cursor.rowcount
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_dedup_ledger.py -v`
Expected: 7 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/dedup_ledger.py tests/unit/core/umf/test_dedup_ledger.py
git commit -m "feat(umf): add SQLite WAL dedup ledger with reserve/commit/abort (Task 3)"
```

---

### Task 4: Heartbeat Projection Module

**Files:**
- Create: `backend/core/umf/heartbeat_projection.py`
- Test: `tests/unit/core/umf/test_heartbeat_projection.py`

**Context:** Derives single global health truth from UMF heartbeat stream. Maps `payload.state` to `SubsystemState.value` (Disease 2 reuse). Detects stale heartbeats and transitions readiness.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_heartbeat_projection.py`:

```python
"""Tests for UMF heartbeat projection (Task 4)."""
import time
import pytest


def _make_heartbeat_msg(subsystem="jarvis-prime", state="ready",
                        liveness=True, readiness=True, **extra):
    from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
    payload = {
        "liveness": liveness,
        "readiness": readiness,
        "subsystem_role": subsystem,
        "state": state,
        "last_error_code": "",
        "queue_depth": 0,
        "resource_pressure": 0.0,
    }
    payload.update(extra)
    return UmfMessage(
        stream="lifecycle", kind="heartbeat",
        source=MessageSource(repo="jarvis-prime", component=subsystem,
                             instance_id="i-1", session_id="s-1"),
        target=MessageTarget(repo="jarvis", component="supervisor"),
        payload=payload,
    )


class TestHeartbeatProjection:

    def test_ingest_heartbeat_updates_state(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        proj = HeartbeatProjection(stale_timeout_s=30.0)
        msg = _make_heartbeat_msg(state="ready")
        proj.ingest(msg)
        state = proj.get_state("jarvis-prime")
        assert state is not None
        assert state["state"] == "ready"
        assert state["liveness"] is True

    def test_subsystem_state_maps_to_enum(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        from backend.core.root_authority_types import SubsystemState
        proj = HeartbeatProjection(stale_timeout_s=30.0)
        msg = _make_heartbeat_msg(state="degraded")
        proj.ingest(msg)
        state = proj.get_state("jarvis-prime")
        assert state["state"] == SubsystemState.DEGRADED.value

    def test_stale_heartbeat_marks_degraded(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        proj = HeartbeatProjection(stale_timeout_s=0.01)  # 10ms timeout
        msg = _make_heartbeat_msg(state="ready",
            observed_at_unix_ms=int((time.time() - 60) * 1000))
        # Force old timestamp
        msg.observed_at_unix_ms = int((time.time() - 60) * 1000)
        proj.ingest(msg)
        import time as t; t.sleep(0.02)
        stale = proj.get_stale_subsystems()
        assert "jarvis-prime" in stale

    def test_unknown_subsystem_returns_none(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        proj = HeartbeatProjection(stale_timeout_s=30.0)
        assert proj.get_state("nonexistent") is None

    def test_get_all_states(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        proj = HeartbeatProjection(stale_timeout_s=30.0)
        proj.ingest(_make_heartbeat_msg(subsystem="prime", state="ready"))
        proj.ingest(_make_heartbeat_msg(subsystem="reactor", state="alive"))
        all_states = proj.get_all_states()
        assert len(all_states) == 2
        assert "prime" in all_states
        assert "reactor" in all_states

    def test_contradictory_reports_last_wins(self):
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        proj = HeartbeatProjection(stale_timeout_s=30.0)
        proj.ingest(_make_heartbeat_msg(subsystem="prime", state="ready"))
        proj.ingest(_make_heartbeat_msg(subsystem="prime", state="degraded"))
        state = proj.get_state("prime")
        assert state["state"] == "degraded"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_heartbeat_projection.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/heartbeat_projection.py`:

```python
"""UMF heartbeat projection — derives single global health truth.

Consumes heartbeat messages from UMF lifecycle stream and maintains
per-subsystem state. Maps payload.state to SubsystemState.value
(Disease 2 type reuse).

The Supervisor is the sole consumer; subsystems only report.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from backend.core.umf.types import UmfMessage


class HeartbeatProjection:
    """Maintains authoritative health truth from UMF heartbeat stream."""

    def __init__(self, stale_timeout_s: float = 30.0) -> None:
        self._stale_timeout_s = stale_timeout_s
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_seen: Dict[str, float] = {}  # subsystem -> monotonic time

    def ingest(self, msg: UmfMessage) -> None:
        """Ingest a heartbeat message and update projection."""
        payload = msg.payload
        subsystem = payload.get("subsystem_role", msg.source.component)
        self._states[subsystem] = {
            "liveness": payload.get("liveness", False),
            "readiness": payload.get("readiness", False),
            "state": payload.get("state", ""),
            "last_error_code": payload.get("last_error_code", ""),
            "queue_depth": payload.get("queue_depth", 0),
            "resource_pressure": payload.get("resource_pressure", 0.0),
            "observed_at_unix_ms": msg.observed_at_unix_ms,
            "source_repo": msg.source.repo,
            "session_id": msg.source.session_id,
        }
        self._last_seen[subsystem] = time.monotonic()

    def get_state(self, subsystem: str) -> Optional[Dict[str, Any]]:
        """Get current projected state for a subsystem."""
        return self._states.get(subsystem)

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get all projected states."""
        return dict(self._states)

    def get_stale_subsystems(self) -> List[str]:
        """Return subsystems whose last heartbeat exceeds stale timeout."""
        now = time.monotonic()
        stale = []
        for subsystem, last_seen in self._last_seen.items():
            if (now - last_seen) > self._stale_timeout_s:
                stale.append(subsystem)
        return stale
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_heartbeat_projection.py -v`
Expected: 6 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/heartbeat_projection.py \
       tests/unit/core/umf/test_heartbeat_projection.py
git commit -m "feat(umf): add heartbeat projection for global health truth (Task 4)"
```

---

## Wave 1 — Delivery Engine & Transport (Tasks 5-8)

### Task 5: Delivery Engine Core

**Files:**
- Create: `backend/core/umf/delivery_engine.py`
- Test: `tests/unit/core/umf/test_delivery_engine.py`

**Context:** Core publish/subscribe engine that routes UMF messages through contract gate and dedup ledger, then dispatches to registered handlers. Implements `MessageFabric` protocol from the design spec.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_delivery_engine.py`:

```python
"""Tests for UMF delivery engine (Task 5)."""
import asyncio
import pytest


def _make_msg(stream="command", kind="command", **overrides):
    from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
    defaults = dict(
        stream=stream, kind=kind,
        source=MessageSource(repo="jarvis", component="sup",
                             instance_id="i", session_id="s"),
        target=MessageTarget(repo="jarvis-prime", component="orch"),
        payload={"test": True},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestDeliveryEngine:

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self, tmp_path):
        from backend.core.umf.delivery_engine import DeliveryEngine
        engine = DeliveryEngine(dedup_db_path=tmp_path / "dedup.db")
        await engine.start()

        received = []
        await engine.subscribe("command", lambda msg: received.append(msg))

        msg = _make_msg()
        result = await engine.publish(msg)
        assert result.delivered is True
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].message_id == msg.message_id
        await engine.stop()

    @pytest.mark.asyncio
    async def test_duplicate_publish_deduped(self, tmp_path):
        from backend.core.umf.delivery_engine import DeliveryEngine
        engine = DeliveryEngine(dedup_db_path=tmp_path / "dedup.db")
        await engine.start()

        received = []
        await engine.subscribe("command", lambda msg: received.append(msg))

        msg = _make_msg()
        await engine.publish(msg)
        # Publish same message again (same idempotency key)
        await engine.publish(msg)
        await asyncio.sleep(0.05)
        assert len(received) == 1  # only delivered once
        await engine.stop()

    @pytest.mark.asyncio
    async def test_expired_message_rejected(self, tmp_path):
        import time
        from backend.core.umf.delivery_engine import DeliveryEngine
        engine = DeliveryEngine(dedup_db_path=tmp_path / "dedup.db")
        await engine.start()

        msg = _make_msg(
            routing_ttl_ms=1,
            observed_at_unix_ms=int((time.time() - 60) * 1000),
        )
        result = await engine.publish(msg)
        assert result.delivered is False
        assert result.reject_reason == "ttl_expired"
        await engine.stop()

    @pytest.mark.asyncio
    async def test_subscribe_filters_by_stream(self, tmp_path):
        from backend.core.umf.delivery_engine import DeliveryEngine
        engine = DeliveryEngine(dedup_db_path=tmp_path / "dedup.db")
        await engine.start()

        cmd_received = []
        event_received = []
        await engine.subscribe("command", lambda m: cmd_received.append(m))
        await engine.subscribe("event", lambda m: event_received.append(m))

        await engine.publish(_make_msg(stream="command"))
        await engine.publish(_make_msg(stream="event", kind="event"))
        await asyncio.sleep(0.05)
        assert len(cmd_received) == 1
        assert len(event_received) == 1
        await engine.stop()

    @pytest.mark.asyncio
    async def test_health_returns_status(self, tmp_path):
        from backend.core.umf.delivery_engine import DeliveryEngine
        engine = DeliveryEngine(dedup_db_path=tmp_path / "dedup.db")
        await engine.start()
        health = await engine.health()
        assert health["running"] is True
        assert "messages_published" in health
        await engine.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_delivery_engine.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/delivery_engine.py`:

```python
"""UMF delivery engine — core publish/subscribe routing.

Routes messages through contract gate and dedup ledger, then dispatches
to registered handlers. Implements the MessageFabric contract.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.umf.contract_gate import validate_message
from backend.core.umf.dedup_ledger import SqliteDedupLedger
from backend.core.umf.types import UmfMessage

logger = logging.getLogger(__name__)

Handler = Callable[[UmfMessage], Any]


@dataclass(frozen=True)
class PublishResult:
    delivered: bool
    message_id: str
    reject_reason: Optional[str] = None


class DeliveryEngine:
    """Core UMF publish/subscribe engine."""

    def __init__(
        self,
        dedup_db_path: Path,
        expected_capability_hash: Optional[str] = None,
    ) -> None:
        self._dedup = SqliteDedupLedger(db_path=dedup_db_path)
        self._expected_cap_hash = expected_capability_hash
        self._subscribers: Dict[str, List[Handler]] = {}
        self._running = False
        self._stats = {"messages_published": 0, "messages_rejected": 0,
                       "messages_delivered": 0, "messages_deduped": 0}

    async def start(self) -> None:
        await self._dedup.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self._dedup.stop()

    async def publish(self, msg: UmfMessage) -> PublishResult:
        """Validate, dedup, and deliver a message."""
        # 1. Contract gate
        validation = validate_message(
            msg, expected_capability_hash=self._expected_cap_hash,
        )
        if not validation.accepted:
            self._stats["messages_rejected"] += 1
            return PublishResult(
                delivered=False, message_id=msg.message_id,
                reject_reason=validation.reject_reason,
            )

        # 2. Dedup check
        reserve_result = await self._dedup.reserve(
            msg.idempotency_key, msg.message_id, msg.routing_ttl_ms,
        )
        if reserve_result.value != "reserved":
            self._stats["messages_deduped"] += 1
            return PublishResult(
                delivered=False, message_id=msg.message_id,
                reject_reason="dedup_duplicate",
            )

        # 3. Dispatch to subscribers
        self._stats["messages_published"] += 1
        handlers = self._subscribers.get(msg.stream, [])
        for handler in handlers:
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result
                self._stats["messages_delivered"] += 1
            except Exception as e:
                logger.error("UMF handler error on stream=%s: %s", msg.stream, e)

        # 4. Commit dedup
        await self._dedup.commit(msg.message_id, "delivered")

        return PublishResult(delivered=True, message_id=msg.message_id)

    async def subscribe(self, stream: str, handler: Handler) -> str:
        """Subscribe a handler to a stream. Returns subscription ID."""
        if stream not in self._subscribers:
            self._subscribers[stream] = []
        self._subscribers[stream].append(handler)
        return f"sub-{stream}-{len(self._subscribers[stream])}"

    async def health(self) -> Dict[str, Any]:
        return {"running": self._running, **self._stats}
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_delivery_engine.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/delivery_engine.py tests/unit/core/umf/test_delivery_engine.py
git commit -m "feat(umf): add delivery engine with pub/sub, dedup, and contract gate (Task 5)"
```

---

### Task 6: Circuit Breaker & Retry Policy

**Files:**
- Create: `backend/core/umf/retry_policy.py`
- Test: `tests/unit/core/umf/test_retry_policy.py`

**Context:** Single retry/failure policy for all UMF streams. Circuit breaker with CLOSED/OPEN/HALF_OPEN states. Exponential backoff with jitter. Bounded retry budgets.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_retry_policy.py`:

```python
"""Tests for UMF retry policy and circuit breaker (Task 6)."""
import pytest


class TestCircuitBreaker:

    def test_starts_closed(self):
        from backend.core.umf.retry_policy import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=30.0)
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_opens_after_threshold_failures(self):
        from backend.core.umf.retry_policy import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=30.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        assert cb.can_execute() is False

    def test_success_resets_failure_count(self):
        from backend.core.umf.retry_policy import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=30.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_half_open_after_recovery_timeout(self):
        from backend.core.umf.retry_policy import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        import time; time.sleep(0.02)
        assert cb.state == "half_open"
        assert cb.can_execute() is True

    def test_half_open_success_closes(self):
        from backend.core.umf.retry_policy import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.01)
        cb.record_failure()
        cb.record_failure()
        import time; time.sleep(0.02)
        cb.record_success()  # in half_open
        assert cb.state == "closed"


class TestRetryBudget:

    def test_allows_within_budget(self):
        from backend.core.umf.retry_policy import RetryBudget
        budget = RetryBudget(max_retries=3, base_delay_s=0.1, max_delay_s=5.0)
        assert budget.should_retry(attempt=0) is True
        assert budget.should_retry(attempt=2) is True

    def test_rejects_over_budget(self):
        from backend.core.umf.retry_policy import RetryBudget
        budget = RetryBudget(max_retries=3, base_delay_s=0.1, max_delay_s=5.0)
        assert budget.should_retry(attempt=3) is False

    def test_delay_increases_exponentially(self):
        from backend.core.umf.retry_policy import RetryBudget
        budget = RetryBudget(max_retries=5, base_delay_s=1.0, max_delay_s=30.0,
                             jitter_factor=0.0)
        d0 = budget.compute_delay(0)
        d1 = budget.compute_delay(1)
        d2 = budget.compute_delay(2)
        assert d0 == 1.0
        assert d1 == 2.0
        assert d2 == 4.0

    def test_delay_capped_at_max(self):
        from backend.core.umf.retry_policy import RetryBudget
        budget = RetryBudget(max_retries=10, base_delay_s=1.0, max_delay_s=5.0,
                             jitter_factor=0.0)
        assert budget.compute_delay(10) == 5.0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_retry_policy.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/retry_policy.py`:

```python
"""UMF retry policy and circuit breaker.

Single retry/failure policy for all UMF streams.
No component-local retry storms.
"""
from __future__ import annotations

import random
import time


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "closed"

    @property
    def state(self) -> str:
        if self._state == "open":
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._recovery_timeout_s:
                return "half_open"
        return self._state

    def can_execute(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._state = "open"


class RetryBudget:
    """Bounded retry budget with exponential backoff and jitter."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 30.0,
        jitter_factor: float = 0.3,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._jitter_factor = jitter_factor

    def should_retry(self, attempt: int) -> bool:
        return attempt < self._max_retries

    def compute_delay(self, attempt: int) -> float:
        raw = self._base_delay_s * (2 ** attempt)
        capped = min(raw, self._max_delay_s)
        if self._jitter_factor > 0:
            lo = capped * (1.0 - self._jitter_factor)
            hi = capped * (1.0 + self._jitter_factor)
            return random.uniform(lo, hi)
        return capped
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_retry_policy.py -v`
Expected: 9 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/retry_policy.py tests/unit/core/umf/test_retry_policy.py
git commit -m "feat(umf): add circuit breaker and retry budget (Task 6)"
```

---

### Task 7: File Transport Adapter

**Files:**
- Create: `backend/core/umf/transport_adapters/__init__.py`
- Create: `backend/core/umf/transport_adapters/file_transport.py`
- Test: `tests/unit/core/umf/test_file_transport.py`

**Context:** File-based transport adapter implementing the `TransportAdapter` protocol. Reuses `~/.jarvis/umf/` directory structure. Atomic writes (temp + rename). Sorted filename ordering for per-partition strict order.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_file_transport.py`:

```python
"""Tests for UMF file transport adapter (Task 7)."""
import asyncio
import json
import pytest


def _make_msg(**overrides):
    from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
    defaults = dict(
        stream="command", kind="command",
        source=MessageSource(repo="jarvis", component="sup",
                             instance_id="i", session_id="s"),
        target=MessageTarget(repo="jarvis-prime", component="orch"),
        payload={"test": True},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestFileTransport:

    @pytest.mark.asyncio
    async def test_send_creates_file(self, tmp_path):
        from backend.core.umf.transport_adapters.file_transport import FileTransport
        transport = FileTransport(base_dir=tmp_path / "umf")
        await transport.start()
        msg = _make_msg()
        result = await transport.send(msg)
        assert result is True
        files = list((tmp_path / "umf" / "command").glob("*.json"))
        assert len(files) == 1
        await transport.stop()

    @pytest.mark.asyncio
    async def test_receive_reads_files(self, tmp_path):
        from backend.core.umf.transport_adapters.file_transport import FileTransport
        transport = FileTransport(base_dir=tmp_path / "umf")
        await transport.start()

        msg = _make_msg()
        await transport.send(msg)

        received = []
        async for m in transport.receive("command", timeout_s=1.0):
            received.append(m)
            break  # just get one

        assert len(received) == 1
        assert received[0].message_id == msg.message_id
        await transport.stop()

    @pytest.mark.asyncio
    async def test_files_sorted_by_name_for_ordering(self, tmp_path):
        from backend.core.umf.transport_adapters.file_transport import FileTransport
        transport = FileTransport(base_dir=tmp_path / "umf")
        await transport.start()

        msg1 = _make_msg(payload={"seq": 1})
        msg2 = _make_msg(payload={"seq": 2})
        await transport.send(msg1)
        await asyncio.sleep(0.01)
        await transport.send(msg2)

        received = []
        async for m in transport.receive("command", timeout_s=1.0):
            received.append(m)
            if len(received) >= 2:
                break

        assert received[0].payload["seq"] == 1
        assert received[1].payload["seq"] == 2
        await transport.stop()

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_files(self, tmp_path):
        from backend.core.umf.transport_adapters.file_transport import FileTransport
        transport = FileTransport(base_dir=tmp_path / "umf", cleanup_age_s=0.01)
        await transport.start()
        await transport.send(_make_msg())
        await asyncio.sleep(0.02)
        removed = await transport.cleanup()
        assert removed >= 1
        await transport.stop()

    @pytest.mark.asyncio
    async def test_atomic_write_no_partial_files(self, tmp_path):
        from backend.core.umf.transport_adapters.file_transport import FileTransport
        transport = FileTransport(base_dir=tmp_path / "umf")
        await transport.start()
        await transport.send(_make_msg())
        files = list((tmp_path / "umf" / "command").glob("*.json"))
        # No temp files should remain
        tmp_files = list((tmp_path / "umf" / "command").glob("*.tmp"))
        assert len(files) == 1
        assert len(tmp_files) == 0
        await transport.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_file_transport.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/transport_adapters/__init__.py`:

```python
"""UMF transport adapter implementations."""
```

Create `backend/core/umf/transport_adapters/file_transport.py`:

```python
"""UMF file-based transport adapter.

Atomic writes (temp + rename). Sorted filename ordering for per-partition
strict order. Cleanup of old files to prevent unbounded growth.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional, Set

from backend.core.umf.types import UmfMessage


class FileTransport:
    """File-based transport adapter for UMF messages."""

    def __init__(
        self,
        base_dir: Path,
        cleanup_age_s: float = 86400.0,  # 24h default
    ) -> None:
        self._base_dir = Path(base_dir)
        self._cleanup_age_s = cleanup_age_s
        self._running = False
        self._processed: Set[str] = set()

    async def start(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._running = True

    async def stop(self) -> None:
        self._running = False

    def is_connected(self) -> bool:
        return self._running

    async def send(self, msg: UmfMessage) -> bool:
        """Write message to file atomically (temp + rename)."""
        stream_dir = self._base_dir / msg.stream
        stream_dir.mkdir(parents=True, exist_ok=True)

        timestamp_ms = msg.observed_at_unix_ms
        filename = f"{timestamp_ms:015d}_{msg.message_id}.json"
        final_path = stream_dir / filename
        tmp_path = stream_dir / f"{filename}.tmp"

        try:
            data = json.dumps(msg.to_dict(), sort_keys=True, separators=(",", ":"))
            tmp_path.write_text(data, encoding="utf-8")
            os.rename(str(tmp_path), str(final_path))
            return True
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return False

    async def receive(
        self, stream: str, timeout_s: float = 0.0, poll_interval_s: float = 0.1,
    ) -> AsyncIterator[UmfMessage]:
        """Yield messages from stream directory, sorted by filename."""
        import asyncio
        stream_dir = self._base_dir / stream
        if not stream_dir.exists():
            return

        deadline = time.monotonic() + timeout_s if timeout_s > 0 else 0
        while self._running:
            files = sorted(stream_dir.glob("*.json"))
            for filepath in files:
                if filepath.name in self._processed:
                    continue
                try:
                    data = json.loads(filepath.read_text(encoding="utf-8"))
                    msg = UmfMessage.from_dict(data)
                    self._processed.add(filepath.name)
                    yield msg
                except Exception:
                    self._processed.add(filepath.name)
                    continue

            if deadline and time.monotonic() >= deadline:
                return
            if not timeout_s:
                return
            await asyncio.sleep(poll_interval_s)

    async def cleanup(self) -> int:
        """Remove files older than cleanup_age_s. Returns count removed."""
        removed = 0
        now = time.time()
        for stream_dir in self._base_dir.iterdir():
            if not stream_dir.is_dir():
                continue
            for filepath in stream_dir.glob("*.json"):
                try:
                    age = now - filepath.stat().st_mtime
                    if age > self._cleanup_age_s:
                        filepath.unlink()
                        removed += 1
                except Exception:
                    continue
        return removed
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_file_transport.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/transport_adapters/__init__.py \
       backend/core/umf/transport_adapters/file_transport.py \
       tests/unit/core/umf/test_file_transport.py
git commit -m "feat(umf): add file transport adapter with atomic writes (Task 7)"
```

---

### Task 8: UMF Message Signing

**Files:**
- Create: `backend/core/umf/signing.py`
- Test: `tests/unit/core/umf/test_signing.py`

**Context:** Sign and verify UMF messages using HMAC-SHA256. Reuses `build_hmac_auth` / `verify_hmac_auth` patterns from `managed_mode.py`. Supports key rotation via `key_id` field.

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_signing.py`:

```python
"""Tests for UMF message signing (Task 8)."""
import pytest


def _make_msg(**overrides):
    from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
    defaults = dict(
        stream="command", kind="command",
        source=MessageSource(repo="jarvis", component="sup",
                             instance_id="i", session_id="s"),
        target=MessageTarget(repo="jarvis-prime", component="orch"),
        payload={"x": 1},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestUmfSigning:

    def test_sign_adds_signature_fields(self):
        from backend.core.umf.signing import sign_message
        msg = _make_msg()
        signed = sign_message(msg, secret="test-secret", key_id="k1")
        assert signed.signature_alg == "HMAC-SHA256"
        assert signed.signature_key_id == "k1"
        assert signed.signature_value != ""

    def test_verify_valid_signature(self):
        from backend.core.umf.signing import sign_message, verify_message
        msg = _make_msg()
        signed = sign_message(msg, secret="test-secret", key_id="k1")
        assert verify_message(signed, secret="test-secret") is True

    def test_verify_invalid_signature(self):
        from backend.core.umf.signing import sign_message, verify_message
        msg = _make_msg()
        signed = sign_message(msg, secret="test-secret", key_id="k1")
        assert verify_message(signed, secret="wrong-secret") is False

    def test_verify_tampered_payload_fails(self):
        from backend.core.umf.signing import sign_message, verify_message
        msg = _make_msg()
        signed = sign_message(msg, secret="test-secret", key_id="k1")
        signed.payload["x"] = 999  # tamper
        assert verify_message(signed, secret="test-secret") is False

    def test_unsigned_message_verify_returns_false(self):
        from backend.core.umf.signing import verify_message
        msg = _make_msg()
        assert verify_message(msg, secret="test-secret") is False

    def test_key_rotation_accepts_both_keys(self):
        from backend.core.umf.signing import sign_message, verify_message_multi_key
        msg = _make_msg()
        signed = sign_message(msg, secret="old-key", key_id="k1")
        keys = {"k1": "old-key", "k2": "new-key"}
        assert verify_message_multi_key(signed, keys) is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_signing.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/signing.py`:

```python
"""UMF message signing and verification.

HMAC-SHA256 over canonical JSON of signable fields.
Supports key rotation via key_id lookup.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from typing import Dict

from backend.core.umf.types import UmfMessage


def _signable_content(msg: UmfMessage) -> str:
    """Canonical JSON of fields included in signature."""
    d = msg.to_dict()
    # Exclude signature block itself
    d.pop("signature", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def sign_message(msg: UmfMessage, secret: str, key_id: str) -> UmfMessage:
    """Return a copy of msg with HMAC-SHA256 signature applied."""
    signed = copy.copy(msg)
    content = _signable_content(msg)
    sig = hmac.new(
        secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    signed.signature_alg = "HMAC-SHA256"
    signed.signature_key_id = key_id
    signed.signature_value = sig
    return signed


def verify_message(msg: UmfMessage, secret: str) -> bool:
    """Verify HMAC-SHA256 signature on a message."""
    if not msg.signature_alg or not msg.signature_value:
        return False
    content = _signable_content(msg)
    expected = hmac.new(
        secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(msg.signature_value, expected)


def verify_message_multi_key(msg: UmfMessage, keys: Dict[str, str]) -> bool:
    """Verify signature using key_id to look up the correct secret."""
    if not msg.signature_key_id or msg.signature_key_id not in keys:
        return False
    return verify_message(msg, keys[msg.signature_key_id])
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_signing.py -v`
Expected: 6 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/signing.py tests/unit/core/umf/test_signing.py
git commit -m "feat(umf): add HMAC-SHA256 message signing with key rotation (Task 8)"
```

---

## Wave 2 — Cross-Repo SDK & Golden Tests (Tasks 9-12)

### Task 9: UMF Client SDK Module

**Files:**
- Create: `backend/core/umf/client.py`
- Test: `tests/unit/core/umf/test_umf_client.py`

**Context:** Thin client SDK that all three repos use to publish/subscribe. Wraps DeliveryEngine with convenient helpers for common patterns (publish command, send heartbeat, ack/nack).

**Step 1: Write the failing test**

Create `tests/unit/core/umf/test_umf_client.py`:

```python
"""Tests for UMF client SDK (Task 9)."""
import asyncio
import pytest


class TestUmfClient:

    @pytest.mark.asyncio
    async def test_publish_command(self, tmp_path):
        from backend.core.umf.client import UmfClient
        client = UmfClient(
            repo="jarvis", component="supervisor",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        received = []
        await client.subscribe("command", lambda m: received.append(m))

        await client.publish_command(
            target_repo="jarvis-prime", target_component="orch",
            payload={"action": "start"},
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].kind == "command"
        assert received[0].stream == "command"
        await client.stop()

    @pytest.mark.asyncio
    async def test_send_heartbeat(self, tmp_path):
        from backend.core.umf.client import UmfClient
        client = UmfClient(
            repo="jarvis-prime", component="orchestrator",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        received = []
        await client.subscribe("lifecycle", lambda m: received.append(m))

        await client.send_heartbeat(
            state="ready", liveness=True, readiness=True,
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].kind == "heartbeat"
        assert received[0].payload["state"] == "ready"
        await client.stop()

    @pytest.mark.asyncio
    async def test_send_ack(self, tmp_path):
        from backend.core.umf.client import UmfClient
        client = UmfClient(
            repo="jarvis", component="supervisor",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        received = []
        await client.subscribe("command", lambda m: received.append(m))

        await client.send_ack(
            original_message_id="orig-123",
            target_repo="jarvis-prime", target_component="orch",
            success=True, message="done",
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].kind == "ack"
        assert received[0].causality_parent_message_id == "orig-123"
        await client.stop()

    @pytest.mark.asyncio
    async def test_client_health(self, tmp_path):
        from backend.core.umf.client import UmfClient
        client = UmfClient(
            repo="jarvis", component="supervisor",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()
        health = await client.health()
        assert health["running"] is True
        await client.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_client.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Create `backend/core/umf/client.py`:

```python
"""UMF client SDK — thin wrapper for publish/subscribe patterns.

Used by all three repos. Provides convenient helpers for common
messaging patterns: publish command, send heartbeat, ack/nack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from backend.core.umf.delivery_engine import DeliveryEngine, PublishResult
from backend.core.umf.types import MessageSource, MessageTarget, UmfMessage


class UmfClient:
    """Thin UMF client for cross-repo messaging."""

    def __init__(
        self,
        repo: str,
        component: str,
        instance_id: str,
        session_id: str,
        dedup_db_path: Path,
    ) -> None:
        self._source = MessageSource(
            repo=repo, component=component,
            instance_id=instance_id, session_id=session_id,
        )
        self._engine = DeliveryEngine(dedup_db_path=dedup_db_path)

    async def start(self) -> None:
        await self._engine.start()

    async def stop(self) -> None:
        await self._engine.stop()

    async def subscribe(self, stream: str, handler: Callable) -> str:
        return await self._engine.subscribe(stream, handler)

    async def health(self) -> Dict[str, Any]:
        return await self._engine.health()

    async def publish_command(
        self, target_repo: str, target_component: str,
        payload: Dict[str, Any],
        **kwargs: Any,
    ) -> PublishResult:
        msg = UmfMessage(
            stream="command", kind="command",
            source=self._source,
            target=MessageTarget(repo=target_repo, component=target_component),
            payload=payload,
            **kwargs,
        )
        return await self._engine.publish(msg)

    async def send_heartbeat(
        self,
        state: str,
        liveness: bool = True,
        readiness: bool = True,
        **extra_payload: Any,
    ) -> PublishResult:
        payload = {
            "liveness": liveness,
            "readiness": readiness,
            "subsystem_role": self._source.component,
            "state": state,
            "last_error_code": extra_payload.pop("last_error_code", ""),
            "queue_depth": extra_payload.pop("queue_depth", 0),
            "resource_pressure": extra_payload.pop("resource_pressure", 0.0),
        }
        payload.update(extra_payload)
        msg = UmfMessage(
            stream="lifecycle", kind="heartbeat",
            source=self._source,
            target=MessageTarget(repo="jarvis", component="supervisor"),
            payload=payload,
        )
        return await self._engine.publish(msg)

    async def send_ack(
        self,
        original_message_id: str,
        target_repo: str,
        target_component: str,
        success: bool = True,
        message: str = "",
    ) -> PublishResult:
        msg = UmfMessage(
            stream="command",
            kind="ack" if success else "nack",
            source=self._source,
            target=MessageTarget(repo=target_repo, component=target_component),
            payload={"success": success, "message": message},
            causality_parent_message_id=original_message_id,
        )
        return await self._engine.publish(msg)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_client.py -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add backend/core/umf/client.py tests/unit/core/umf/test_umf_client.py
git commit -m "feat(umf): add UMF client SDK with command/heartbeat/ack helpers (Task 9)"
```

---

### Task 10: Golden Contract Tests (Cross-Repo)

**Files:**
- Create: `tests/unit/core/umf/test_umf_contract_golden.py` (JARVIS)
- Create: `jarvis-prime/tests/test_umf_contract_golden.py` (Prime — copy)
- Create: `reactor-core/tests/test_umf_contract_golden.py` (Reactor — copy)

**Context:** Identical golden test file across all 3 repos. Validates UMF envelope shape, stream/kind enums, reason codes, and serialization. CI drift checking via hash comparison (same pattern as `test_managed_mode_contract.py`).

**Step 1: Write the golden test**

Create `tests/unit/core/umf/test_umf_contract_golden.py`:

```python
"""UMF Golden Contract Tests — IDENTICAL across all 3 repos.

CI must verify file hash parity to detect drift.
DO NOT modify without updating copies in jarvis-prime and reactor-core.
"""
import json
import pytest


class TestUmfEnvelopeShape:
    """Validate the canonical envelope has all required fields."""

    def test_required_top_level_keys(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command", kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
        )
        d = msg.to_dict()
        required = {
            "schema_version", "message_id", "idempotency_key",
            "stream", "kind", "source", "target", "routing",
            "causality", "contract", "payload", "observed_at_unix_ms",
            "signature",
        }
        assert required.issubset(set(d.keys()))

    def test_source_has_required_fields(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command", kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
        )
        src = msg.to_dict()["source"]
        assert {"repo", "component", "instance_id", "session_id"} == set(src.keys())

    def test_routing_has_required_fields(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="command", kind="command",
            source=MessageSource(repo="jarvis", component="a",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="b"),
            payload={},
        )
        routing = msg.to_dict()["routing"]
        assert {"partition_key", "priority", "ttl_ms", "deadline_unix_ms"} == set(routing.keys())

    def test_schema_version_is_umf_v1(self):
        from backend.core.umf.types import UMF_SCHEMA_VERSION
        assert UMF_SCHEMA_VERSION == "umf.v1"


class TestUmfStreamAndKindContract:

    def test_stream_enum_values(self):
        from backend.core.umf.types import Stream
        expected = {"lifecycle", "command", "event", "heartbeat", "telemetry"}
        assert {s.value for s in Stream} == expected

    def test_kind_enum_values(self):
        from backend.core.umf.types import Kind
        expected = {"command", "event", "heartbeat", "ack", "nack"}
        assert {k.value for k in Kind} == expected


class TestUmfReasonCodeContract:

    def test_all_reason_codes_present(self):
        from backend.core.umf.types import RejectReason
        required = {
            "schema_mismatch", "sig_invalid", "capability_mismatch",
            "ttl_expired", "deadline_expired", "dedup_duplicate",
            "route_unavailable", "backpressure_drop", "circuit_open",
            "handler_timeout",
        }
        actual = {r.value for r in RejectReason}
        assert required == actual

    def test_reason_codes_are_lowercase_snake(self):
        from backend.core.umf.types import RejectReason
        for r in RejectReason:
            assert r.value == r.value.lower()
            assert " " not in r.value


class TestUmfSerializationContract:

    def test_roundtrip_preserves_all_fields(self):
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget
        msg = UmfMessage(
            stream="lifecycle", kind="heartbeat",
            source=MessageSource(repo="reactor-core", component="trainer",
                                 instance_id="i-2", session_id="s-2"),
            target=MessageTarget(repo="jarvis", component="supervisor"),
            payload={"state": "ready", "liveness": True},
            routing_priority="high",
            routing_ttl_ms=60000,
            causality_parent_message_id="parent-123",
        )
        d = msg.to_dict()
        restored = UmfMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.routing_priority == "high"
        assert restored.routing_ttl_ms == 60000
        assert restored.causality_parent_message_id == "parent-123"
        assert restored.payload == {"state": "ready", "liveness": True}
```

**Step 2: Run test to verify it fails / passes**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_contract_golden.py -v`
Expected: PASS (types already implemented in Task 1)

**Step 3: Copy to other repos**

Copy `tests/unit/core/umf/test_umf_contract_golden.py` to:
- `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_umf_contract_golden.py`
- `/Users/djrussell23/Documents/repos/reactor-core/tests/test_umf_contract_golden.py`

Also copy `backend/core/umf/types.py` to both repos (adjusted for import paths):
- `/Users/djrussell23/Documents/repos/jarvis-prime/umf_types.py`
- `/Users/djrussell23/Documents/repos/reactor-core/umf_types.py`

**Note:** In the copies, change `from backend.core.umf.types import ...` to `from umf_types import ...`.

**Step 4: Run tests in all repos**

Run in each repo:
```bash
python3 -m pytest tests/test_umf_contract_golden.py -v  # Prime & Reactor
python3 -m pytest tests/unit/core/umf/test_umf_contract_golden.py -v  # JARVIS
```
Expected: PASS in all repos

**Step 5: Commit in each repo**

JARVIS:
```bash
git add tests/unit/core/umf/test_umf_contract_golden.py
git commit -m "test(umf): add golden contract tests for UMF envelope shape (Task 10)"
```

Prime:
```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add umf_types.py tests/test_umf_contract_golden.py
git commit -m "feat(umf): add UMF types and golden contract tests (Task 10)"
```

Reactor:
```bash
cd /Users/djrussell23/Documents/repos/reactor-core
git add umf_types.py tests/test_umf_contract_golden.py
git commit -m "feat(umf): add UMF types and golden contract tests (Task 10)"
```

---

### Task 11: Prime UMF Client Integration

**Files:**
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/umf_client.py`
- Test: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_umf_client_integration.py`

**Context:** jarvis-prime gets a copy of the UMF client SDK adapted for standalone import. Must import from local `umf_types.py` instead of `backend.core.umf.types`.

**Step 1: Write the failing test**

Create `jarvis-prime/tests/test_umf_client_integration.py`:

```python
"""Tests for Prime UMF client integration (Task 11)."""
import asyncio
import pytest


class TestPrimeUmfClient:

    @pytest.mark.asyncio
    async def test_prime_can_send_heartbeat(self, tmp_path):
        from umf_client import PrimeUmfClient
        client = PrimeUmfClient(
            session_id="test-s",
            instance_id="test-i",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()
        received = []
        await client.subscribe("lifecycle", lambda m: received.append(m))
        await client.send_heartbeat(state="ready")
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].source.repo == "jarvis-prime"
        await client.stop()

    @pytest.mark.asyncio
    async def test_prime_can_publish_event(self, tmp_path):
        from umf_client import PrimeUmfClient
        client = PrimeUmfClient(
            session_id="test-s",
            instance_id="test-i",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()
        received = []
        await client.subscribe("event", lambda m: received.append(m))
        await client.publish_event(
            target_repo="jarvis", target_component="supervisor",
            payload={"model_ready": True},
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].kind == "event"
        await client.stop()
```

**Step 2-5:** Create `umf_client.py` in jarvis-prime wrapping `umf_types.py` with Prime-specific defaults (repo="jarvis-prime", component="orchestrator"). Run tests, commit.

---

### Task 12: Reactor UMF Client Integration

**Files:**
- Create: `/Users/djrussell23/Documents/repos/reactor-core/umf_client.py`
- Test: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_umf_client_integration.py`

**Context:** Same pattern as Task 11 but for reactor-core. Repo="reactor-core", component="reactor".

(Same structure as Task 11, adapted for reactor-core imports and defaults.)

---

## Wave 3 — Supervisor Wiring (Tasks 13-15)

### Task 13: UMF Factory in unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py` (append ~30 lines near end, after existing `create_root_authority_watcher`)
- Test: `tests/unit/supervisor/test_umf_wiring.py`

**Context:** Factory function `create_umf_engine()` that creates and configures the UMF DeliveryEngine, wired into the supervisor's startup path. Respects `JARVIS_UMF_MODE` env var (shadow/active/disabled).

**Step 1: Write the failing test**

Create `tests/unit/supervisor/test_umf_wiring.py`:

```python
"""Tests for UMF wiring into unified_supervisor (Task 13)."""
import os
import pytest


class TestCreateUmfEngine:

    def test_returns_none_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        from unified_supervisor import create_umf_engine
        result = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert result is None

    def test_returns_engine_when_shadow(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from unified_supervisor import create_umf_engine
        engine = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert engine is not None

    def test_returns_engine_when_active(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        from unified_supervisor import create_umf_engine
        engine = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert engine is not None
```

**Step 2-5:** Append `create_umf_engine()` factory to end of `unified_supervisor.py`. Run tests, commit.

---

### Task 14: Heartbeat Projection Wiring

**Files:**
- Modify: `unified_supervisor.py` (append ~20 lines)
- Test: `tests/unit/supervisor/test_heartbeat_wiring.py`

**Context:** Wire `HeartbeatProjection` into supervisor so lifecycle heartbeats from UMF feed into the single global health truth. Connects to the Root Authority Watcher from Disease 2.

---

### Task 15: Shadow Mode Parity Logger

**Files:**
- Create: `backend/core/umf/shadow_parity.py`
- Test: `tests/unit/core/umf/test_shadow_parity.py`

**Context:** In shadow mode, both legacy paths and UMF process the same inputs. This module compares decisions and logs parity diffs with `trace_id` and reason codes. Blocks promotion if parity < 99.9%.

---

## Wave 4 — Heartbeat & Lifecycle Cutover (Tasks 16-17)

### Task 16: UMF Heartbeat Publisher in jarvis-prime

**Files:**
- Modify: `jarvis-prime/run_server.py` (add UMF heartbeat alongside /health endpoint)
- Test: `jarvis-prime/tests/test_umf_heartbeat.py`

**Context:** Prime's health endpoint already emits `build_health_envelope()`. Add UMF heartbeat publishing via `PrimeUmfClient.send_heartbeat()` on the same interval. Shadow mode: publish to UMF but don't consume from it yet.

---

### Task 17: UMF Heartbeat Publisher in reactor-core

**Files:**
- Modify: `reactor-core/reactor_core/api/server.py` (add UMF heartbeat)
- Test: `reactor-core/tests/test_umf_heartbeat.py`

**Context:** Same as Task 16 for reactor-core.

---

## Wave 5 — Command/Event Cutover (Tasks 18-19)

### Task 18: Legacy Guard Flag

**Files:**
- Create: `backend/core/umf/legacy_guard.py`
- Test: `tests/unit/core/umf/test_legacy_guard.py`

**Context:** Enforceable guard flag `JARVIS_UMF_LEGACY_ENABLED` that controls whether legacy paths (Trinity Event Bus, Reactor Bridge) are active. When UMF is authoritative (`JARVIS_UMF_MODE=active`), this flag defaults to `false`, disabling legacy paths.

**Step 1: Write the failing test**

```python
"""Tests for UMF legacy guard flag (Task 18)."""
import pytest


class TestLegacyGuard:

    def test_legacy_enabled_by_default_when_no_umf(self, monkeypatch):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_legacy_disabled_in_active_mode(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is False

    def test_legacy_enabled_in_shadow_mode(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.setenv("JARVIS_UMF_LEGACY_ENABLED", "true")
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_guard_check_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import assert_legacy_allowed
        with pytest.raises(RuntimeError, match="Legacy path disabled"):
            assert_legacy_allowed("test-caller")
```

**Step 2-5:** Implement, test, commit.

---

### Task 19: Guard Insertion in Reactor Bridge

**Files:**
- Modify: `backend/system/reactor_bridge.py` (add guard check at entry points)
- Test: `tests/unit/core/umf/test_legacy_bridge_guard.py`

**Context:** Insert `assert_legacy_allowed()` at the top of `ReactorCoreBridge.connect_async()` and `_publish_to_transports_async()`. When UMF is active mode, these methods raise `RuntimeError` preventing legacy usage.

---

## Wave 6 — Hardening & Operationalization (Tasks 20-21)

### Task 20: Dead Letter Queue

**Files:**
- Create: `backend/core/umf/dead_letter_queue.py`
- Test: `tests/unit/core/umf/test_dead_letter_queue.py`

**Context:** Centralized DLQ for messages that fail delivery after retry budget exhaustion. No oscillation (poison messages go to DLQ once). Bounded with TTL compaction.

**Step 1: Write the failing test**

```python
"""Tests for UMF dead letter queue (Task 20)."""
import pytest


class TestDeadLetterQueue:

    @pytest.mark.asyncio
    async def test_add_and_retrieve(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq")
        dlq.start()
        await dlq.add(message_id="msg-1", reason="handler_timeout",
                       payload={"test": True})
        entries = dlq.list_entries()
        assert len(entries) == 1
        assert entries[0]["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_no_oscillation(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq")
        dlq.start()
        await dlq.add(message_id="msg-1", reason="poison", payload={})
        await dlq.add(message_id="msg-1", reason="poison", payload={})
        entries = dlq.list_entries()
        assert len(entries) == 1  # same message_id only stored once

    @pytest.mark.asyncio
    async def test_cleanup_old_entries(self, tmp_path):
        import asyncio
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq", max_age_s=0.01)
        dlq.start()
        await dlq.add(message_id="msg-1", reason="test", payload={})
        await asyncio.sleep(0.02)
        removed = dlq.cleanup()
        assert removed >= 1
```

**Step 2-5:** Implement, test, commit.

---

### Task 21: Compact Integration Test Suite

**Files:**
- Create: `tests/unit/core/umf/test_umf_integration.py`

**Context:** End-to-end test that exercises the full UMF pipeline: client publishes command -> contract gate validates -> dedup ledger reserves -> delivery engine dispatches to subscriber -> heartbeat projection updates state -> ack flows back. All in-process, no external dependencies.

**Step 1: Write the integration test**

```python
"""UMF integration test — full pipeline (Task 21)."""
import asyncio
import pytest


class TestUmfFullPipeline:

    @pytest.mark.asyncio
    async def test_command_ack_roundtrip(self, tmp_path):
        from backend.core.umf.client import UmfClient

        # Supervisor client
        supervisor = UmfClient(
            repo="jarvis", component="supervisor",
            instance_id="i-sup", session_id="s-1",
            dedup_db_path=tmp_path / "sup-dedup.db",
        )
        await supervisor.start()

        # Track received commands
        commands = []
        await supervisor.subscribe("command", lambda m: commands.append(m))

        # Publish a command
        result = await supervisor.publish_command(
            target_repo="jarvis-prime", target_component="orch",
            payload={"action": "start_training"},
        )
        assert result.delivered is True
        await asyncio.sleep(0.05)
        assert len(commands) == 1

        # Duplicate should be deduped
        result2 = await supervisor.publish_command(
            target_repo="jarvis-prime", target_component="orch",
            payload={"action": "start_training"},
        )
        # Different message_id but may be delivered (different idempotency key)
        # This validates the dedup is by message_id not payload

        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_heartbeat_updates_projection(self, tmp_path):
        from backend.core.umf.client import UmfClient
        from backend.core.umf.heartbeat_projection import HeartbeatProjection

        client = UmfClient(
            repo="jarvis-prime", component="orchestrator",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        projection = HeartbeatProjection(stale_timeout_s=30.0)
        await client.subscribe("lifecycle", lambda m: projection.ingest(m))

        await client.send_heartbeat(state="ready", liveness=True, readiness=True)
        await asyncio.sleep(0.05)

        state = projection.get_state("orchestrator")
        assert state is not None
        assert state["state"] == "ready"
        assert state["liveness"] is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_expired_message_not_delivered(self, tmp_path):
        import time
        from backend.core.umf.client import UmfClient
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget

        client = UmfClient(
            repo="jarvis", component="sup",
            instance_id="i", session_id="s",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        received = []
        await client.subscribe("command", lambda m: received.append(m))

        # Manually create an expired message
        msg = UmfMessage(
            stream="command", kind="command",
            source=MessageSource(repo="jarvis", component="sup",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="orch"),
            payload={"expired": True},
            routing_ttl_ms=1,
            observed_at_unix_ms=int((time.time() - 60) * 1000),
        )
        result = await client._engine.publish(msg)
        assert result.delivered is False
        assert result.reject_reason == "ttl_expired"
        await client.stop()
```

**Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/umf/test_umf_integration.py -v`
Expected: 3 PASSED

**Step 3: Commit**

```bash
git add tests/unit/core/umf/test_umf_integration.py
git commit -m "test(umf): add full pipeline integration tests (Task 21)"
```

---

## Summary

| Wave | Tasks | Focus |
|------|-------|-------|
| 0 | 1-4 | Foundation types, contract gate, dedup ledger, heartbeat projection |
| 1 | 5-8 | Delivery engine, circuit breaker, file transport, signing |
| 2 | 9-12 | Client SDK, golden contract tests, Prime/Reactor integration |
| 3 | 13-15 | Supervisor wiring, heartbeat wiring, shadow parity |
| 4 | 16-17 | Heartbeat cutover in Prime and Reactor |
| 5 | 18-19 | Legacy guard flag, guard insertion in Reactor Bridge |
| 6 | 20-21 | Dead letter queue, integration test suite |

**Total: 21 tasks across 7 waves**

**Estimated test count: ~95 tests across all repos**

**Key files created:**
- `backend/core/umf/types.py` — Canonical envelope (stdlib-only, portable)
- `backend/core/umf/contract_gate.py` — Schema/TTL/HMAC validation
- `backend/core/umf/dedup_ledger.py` — SQLite WAL reserve/commit/abort
- `backend/core/umf/heartbeat_projection.py` — Global health truth
- `backend/core/umf/delivery_engine.py` — Pub/sub routing core
- `backend/core/umf/retry_policy.py` — Circuit breaker + retry budget
- `backend/core/umf/transport_adapters/file_transport.py` — File transport
- `backend/core/umf/signing.py` — HMAC-SHA256 with key rotation
- `backend/core/umf/client.py` — Thin client SDK
- `backend/core/umf/shadow_parity.py` — Shadow mode parity checking
- `backend/core/umf/legacy_guard.py` — Enforceable legacy disable
- `backend/core/umf/dead_letter_queue.py` — Centralized DLQ
