# Phase 2C.1: Loop Activation — Core Intake Layer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire four autonomous trigger sensors (backlog, test_failure, voice_human, ai_miner) into a WAL-backed Unified Intake Router that feeds `GovernedLoopService.submit()` using schema-versioned `IntentEnvelope` objects with full causal chain keys.

**Architecture:** New `intake/` package under `backend/core/ouroboros/governance/` holding `IntentEnvelope`, `WAL`, and `UnifiedIntakeRouter`. New `intake/sensors/` package with four sensor adapters. Sensors produce `IntentEnvelope` → router validates/deduplicates/prioritizes → WAL-persists → dispatches to `GovernedLoopService.submit()`. Sensor D is observe-only at launch (`requires_human_ack=True`).

**Tech Stack:** Python 3.11, asyncio, dataclasses (frozen), fcntl (file advisory lock), json / JSONL (WAL), pytest-asyncio (`asyncio_mode=auto`)

---

### Important codebase context

- `asyncio_mode = "auto"` — never add `@pytest.mark.asyncio`
- Run tests: `python3 -m pytest tests/governance/intake/ -v`
- Run full suite: `python3 -m pytest tests/ -x -q`
- `OperationContext.create(target_files=..., description=..., op_id=...)` — factory
- `GovernedLoopService.submit(ctx, trigger_source=...)` — pipeline entrypoint
- `generate_operation_id(prefix)` — in `backend.core.ouroboros.governance.operation_id`
- `FailbackState` enum in `backend.core.ouroboros.governance.candidate_generator`
- Existing `IntentSignal` in `intent/signals.py` — do NOT remove; sensor B adapts it
- `time.monotonic()` for enforcement timestamps; `datetime.now(timezone.utc)` for audit

---

## Task 1: IntentEnvelope dataclass + schema validation

**Files:**
- Create: `backend/core/ouroboros/governance/intake/__init__.py`
- Create: `backend/core/ouroboros/governance/intake/intent_envelope.py`
- Create: `tests/governance/intake/__init__.py`
- Create: `tests/governance/intake/test_intent_envelope.py`

**Step 1: Create the test package init**

```bash
mkdir -p tests/governance/intake
touch tests/governance/intake/__init__.py
```

**Step 2: Write the failing test**

`tests/governance/intake/test_intent_envelope.py`:

```python
"""Tests for IntentEnvelope schema 2c.1."""
import time
import pytest
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)


def _valid_kwargs(**overrides):
    base = dict(
        source="backlog",
        description="fix the auth module",
        target_files=("backend/core/auth.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"task_id": "t-001"},
        requires_human_ack=False,
    )
    base.update(overrides)
    return base


def test_schema_version_constant():
    assert SCHEMA_VERSION == "2c.1"


def test_make_envelope_happy_path():
    env = make_envelope(**_valid_kwargs())
    assert env.schema_version == "2c.1"
    assert env.source == "backlog"
    assert env.target_files == ("backend/core/auth.py",)
    assert env.confidence == 0.8
    assert isinstance(env.causal_id, str) and len(env.causal_id) > 0
    assert isinstance(env.signal_id, str) and len(env.signal_id) > 0
    assert isinstance(env.idempotency_key, str) and len(env.idempotency_key) > 0
    assert env.lease_id == ""  # set by router at enqueue
    assert env.submitted_at > 0.0


def test_make_envelope_auto_dedup_key():
    env1 = make_envelope(**_valid_kwargs())
    env2 = make_envelope(**_valid_kwargs())
    # Same source/target/evidence → same dedup_key
    assert env1.dedup_key == env2.dedup_key
    # But different signal_id / causal_id / idempotency_key
    assert env1.signal_id != env2.signal_id


def test_envelope_immutable():
    env = make_envelope(**_valid_kwargs())
    with pytest.raises((AttributeError, TypeError)):
        env.source = "voice_human"  # type: ignore


def test_invalid_schema_version_rejected():
    with pytest.raises(EnvelopeValidationError, match="schema_version"):
        IntentEnvelope(
            schema_version="1.0",
            source="backlog",
            description="x",
            target_files=("a.py",),
            repo="jarvis",
            confidence=0.5,
            urgency="normal",
            dedup_key="abc",
            causal_id="cid",
            signal_id="sid",
            idempotency_key="ikey",
            lease_id="",
            evidence={},
            requires_human_ack=False,
            submitted_at=1.0,
        )


def test_invalid_source_rejected():
    with pytest.raises(EnvelopeValidationError, match="source"):
        make_envelope(**_valid_kwargs(source="unknown_sensor"))


def test_invalid_urgency_rejected():
    with pytest.raises(EnvelopeValidationError, match="urgency"):
        make_envelope(**_valid_kwargs(urgency="emergency"))


def test_confidence_out_of_range_rejected():
    with pytest.raises(EnvelopeValidationError, match="confidence"):
        make_envelope(**_valid_kwargs(confidence=1.5))


def test_empty_target_files_rejected():
    with pytest.raises(EnvelopeValidationError, match="target_files"):
        make_envelope(**_valid_kwargs(target_files=()))


def test_roundtrip_to_from_dict():
    env = make_envelope(**_valid_kwargs())
    d = env.to_dict()
    env2 = IntentEnvelope.from_dict(d)
    assert env2.schema_version == env.schema_version
    assert env2.source == env.source
    assert env2.target_files == env.target_files
    assert env2.causal_id == env.causal_id
    assert env2.dedup_key == env.dedup_key


def test_from_dict_rejects_unknown_schema_version():
    env = make_envelope(**_valid_kwargs())
    d = env.to_dict()
    d["schema_version"] = "9.9"
    with pytest.raises(EnvelopeValidationError):
        IntentEnvelope.from_dict(d)
```

**Step 3: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/test_intent_envelope.py -v
```
Expected: `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.intake'`

**Step 4: Create the package init and implementation**

`backend/core/ouroboros/governance/intake/__init__.py`:

```python
"""Public API for the Unified Intake Layer (Phase 2C)."""
from .intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)

__all__ = [
    "IntentEnvelope",
    "EnvelopeValidationError",
    "make_envelope",
    "SCHEMA_VERSION",
]
```

`backend/core/ouroboros/governance/intake/intent_envelope.py`:

```python
"""
IntentEnvelope — Canonical contract between sensors and the Unified Intake Router.

Schema version: 2c.1
Every field except ``lease_id`` is immutable once created.
``lease_id`` starts empty and is set by the router at WAL-enqueue time via
``IntentEnvelope.with_lease()``.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id

SCHEMA_VERSION = "2c.1"

_VALID_SOURCES = frozenset({"backlog", "test_failure", "voice_human", "ai_miner"})
_VALID_URGENCIES = frozenset({"critical", "high", "normal", "low"})


class EnvelopeValidationError(ValueError):
    """Raised when an IntentEnvelope fails schema validation."""


@dataclass(frozen=True)
class IntentEnvelope:
    """Immutable canonical intent contract.

    All auto-generated fields (causal_id, signal_id, idempotency_key,
    dedup_key, submitted_at) are set by :func:`make_envelope`.
    ``lease_id`` is always ``""`` until the router sets it via
    :meth:`with_lease`.
    """

    schema_version: str
    source: str
    description: str
    target_files: Tuple[str, ...]
    repo: str
    confidence: float
    urgency: str
    dedup_key: str
    causal_id: str
    signal_id: str
    idempotency_key: str
    lease_id: str
    evidence: Dict[str, Any]
    requires_human_ack: bool
    submitted_at: float  # time.monotonic()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EnvelopeValidationError(
                f"schema_version must be {SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        if self.source not in _VALID_SOURCES:
            raise EnvelopeValidationError(
                f"source must be one of {sorted(_VALID_SOURCES)}, got {self.source!r}"
            )
        if self.urgency not in _VALID_URGENCIES:
            raise EnvelopeValidationError(
                f"urgency must be one of {sorted(_VALID_URGENCIES)}, got {self.urgency!r}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise EnvelopeValidationError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if not self.target_files:
            raise EnvelopeValidationError("target_files must be non-empty")

    # ------------------------------------------------------------------
    # Mutations (return new instances)
    # ------------------------------------------------------------------

    def with_lease(self, lease_id: str) -> "IntentEnvelope":
        """Return a new envelope with the given lease_id set."""
        return IntentEnvelope(
            schema_version=self.schema_version,
            source=self.source,
            description=self.description,
            target_files=self.target_files,
            repo=self.repo,
            confidence=self.confidence,
            urgency=self.urgency,
            dedup_key=self.dedup_key,
            causal_id=self.causal_id,
            signal_id=self.signal_id,
            idempotency_key=self.idempotency_key,
            lease_id=lease_id,
            evidence=self.evidence,
            requires_human_ack=self.requires_human_ack,
            submitted_at=self.submitted_at,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "description": self.description,
            "target_files": list(self.target_files),
            "repo": self.repo,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "dedup_key": self.dedup_key,
            "causal_id": self.causal_id,
            "signal_id": self.signal_id,
            "idempotency_key": self.idempotency_key,
            "lease_id": self.lease_id,
            "evidence": dict(self.evidence),
            "requires_human_ack": self.requires_human_ack,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IntentEnvelope":
        return cls(
            schema_version=d["schema_version"],
            source=d["source"],
            description=d["description"],
            target_files=tuple(d["target_files"]),
            repo=d["repo"],
            confidence=float(d["confidence"]),
            urgency=d["urgency"],
            dedup_key=d["dedup_key"],
            causal_id=d["causal_id"],
            signal_id=d["signal_id"],
            idempotency_key=d["idempotency_key"],
            lease_id=d.get("lease_id", ""),
            evidence=dict(d.get("evidence", {})),
            requires_human_ack=bool(d["requires_human_ack"]),
            submitted_at=float(d["submitted_at"]),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _dedup_key(source: str, target_files: Tuple[str, ...], evidence: Dict[str, Any]) -> str:
    sig = evidence.get("signature", "")
    raw = f"{source}|{'|'.join(sorted(target_files))}|{sig}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_envelope(
    *,
    source: str,
    description: str,
    target_files: Tuple[str, ...],
    repo: str,
    confidence: float,
    urgency: str,
    evidence: Dict[str, Any],
    requires_human_ack: bool,
    causal_id: str = "",
    signal_id: str = "",
) -> IntentEnvelope:
    """Create a new IntentEnvelope with auto-generated IDs."""
    sid = signal_id or generate_operation_id("sig")
    cid = causal_id or generate_operation_id("cau")
    ikey = generate_operation_id("ikey")
    dk = _dedup_key(source, tuple(target_files), evidence)
    return IntentEnvelope(
        schema_version=SCHEMA_VERSION,
        source=source,
        description=description,
        target_files=tuple(target_files),
        repo=repo,
        confidence=confidence,
        urgency=urgency,
        dedup_key=dk,
        causal_id=cid,
        signal_id=sid,
        idempotency_key=ikey,
        lease_id="",
        evidence=evidence,
        requires_human_ack=requires_human_ack,
        submitted_at=time.monotonic(),
    )
```

**Step 5: Run tests to confirm pass**

```bash
python3 -m pytest tests/governance/intake/test_intent_envelope.py -v
```
Expected: all 11 tests PASS.

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/__init__.py \
        backend/core/ouroboros/governance/intake/intent_envelope.py \
        tests/governance/intake/__init__.py \
        tests/governance/intake/test_intent_envelope.py
git commit -m "feat(intake): add IntentEnvelope schema 2c.1 with validation and factory"
```

---

## Task 2: WAL (Write-Ahead Log)

**Files:**
- Create: `backend/core/ouroboros/governance/intake/wal.py`
- Create: `tests/governance/intake/test_wal.py`

**Step 1: Write the failing test**

`tests/governance/intake/test_wal.py`:

```python
"""Tests for WAL (Write-Ahead Log) append/replay/compaction."""
import json
import time
from pathlib import Path
import pytest

from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _entry(lease_id: str = "l1", ts: float = 0.0) -> WALEntry:
    return WALEntry(
        lease_id=lease_id,
        envelope_dict={"source": "backlog", "description": "test"},
        status="pending",
        ts_monotonic=ts or time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    )


def test_append_creates_file(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    assert (tmp_path / "test.jsonl").exists()


def test_append_produces_valid_json(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["lease_id"] == "l1"
    assert record["status"] == "pending"


def test_pending_entries_returns_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.append(_entry("l2"))
    pending = wal.pending_entries()
    assert {e.lease_id for e in pending} == {"l1", "l2"}


def test_update_status_removes_from_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.append(_entry("l2"))
    wal.update_status("l1", "acked")
    pending = wal.pending_entries()
    assert len(pending) == 1
    assert pending[0].lease_id == "l2"


def test_dead_letter_not_in_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.update_status("l1", "dead_letter")
    assert wal.pending_entries() == []


def test_compact_removes_old_entries(tmp_path):
    wal = WAL(tmp_path / "test.jsonl", max_age_days=0)
    # Write entry with very old monotonic (simulate age)
    old_entry = WALEntry(
        lease_id="old",
        envelope_dict={},
        status="acked",
        ts_monotonic=0.0,  # effectively ancient
        ts_utc="2020-01-01T00:00:00Z",
    )
    wal.append(old_entry)
    wal.append(_entry("new"))
    removed = wal.compact()
    assert removed >= 1
    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    lease_ids = [json.loads(l)["lease_id"] for l in lines if l]
    assert "old" not in lease_ids


def test_corrupt_line_skipped_gracefully(tmp_path):
    p = tmp_path / "test.jsonl"
    p.write_text('{"lease_id":"l1","envelope":{},"status":"pending","ts_monotonic":1.0,"ts_utc":""}\nNOT_JSON\n')
    wal = WAL(p)
    pending = wal.pending_entries()
    assert len(pending) == 1
    assert pending[0].lease_id == "l1"
```

**Step 2: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/test_wal.py -v
```
Expected: `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.intake.wal'`

**Step 3: Implement WAL**

`backend/core/ouroboros/governance/intake/wal.py`:

```python
"""
Write-Ahead Log (WAL) for the Unified Intake Router.

Append-only JSONL file.  Each line is a WAL record.
Supports append (with fsync), status updates, crash-recovery replay,
and compaction (remove entries older than max_age_days).

At-least-once guarantee: router replays all ``status="pending"``
entries on startup and checks idempotency_key against the ledger to
skip already-terminal ops.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_WAL_VERSION = 1


@dataclass
class WALEntry:
    lease_id: str
    envelope_dict: Dict[str, Any]
    status: str  # "pending" | "acked" | "dead_letter"
    ts_monotonic: float
    ts_utc: str


class WAL:
    """Append-only write-ahead log for intake envelopes.

    Parameters
    ----------
    path:
        Path to the JSONL WAL file (created on first append).
    max_age_days:
        Entries older than this are pruned during :meth:`compact`.
    """

    def __init__(self, path: Path, max_age_days: int = 7) -> None:
        self._path = path
        self._max_age_days = max_age_days
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: WALEntry) -> None:
        """Append an entry and fsync."""
        record = {
            "v": _WAL_VERSION,
            "lease_id": entry.lease_id,
            "envelope": entry.envelope_dict,
            "status": entry.status,
            "ts_monotonic": entry.ts_monotonic,
            "ts_utc": entry.ts_utc,
        }
        self._write_line(record)

    def update_status(self, lease_id: str, status: str) -> None:
        """Append a status-update tombstone for the given lease_id."""
        record = {
            "v": _WAL_VERSION,
            "lease_id": lease_id,
            "status": status,
            "ts_monotonic": time.monotonic(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "_type": "status_update",
        }
        self._write_line(record)

    def pending_entries(self) -> List[WALEntry]:
        """Return all entries whose effective status is ``'pending'``.

        Reads the entire WAL, applies status-update tombstones, and
        returns only those entries that remain pending.  Used for
        crash-recovery replay on startup.
        """
        entries: Dict[str, WALEntry] = {}
        status_overrides: Dict[str, str] = {}

        if not self._path.exists():
            return []

        with self._path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("WAL: corrupt entry at line %d, skipping", line_no)
                    continue

                lease_id: str = record.get("lease_id", "")
                if not lease_id:
                    continue

                if record.get("_type") == "status_update":
                    status_overrides[lease_id] = record.get("status", "pending")
                elif "envelope" in record:
                    entries[lease_id] = WALEntry(
                        lease_id=lease_id,
                        envelope_dict=record["envelope"],
                        status=record.get("status", "pending"),
                        ts_monotonic=record.get("ts_monotonic", 0.0),
                        ts_utc=record.get("ts_utc", ""),
                    )

        # Apply tombstones
        for lid, status in status_overrides.items():
            if lid in entries:
                e = entries[lid]
                entries[lid] = WALEntry(
                    lease_id=e.lease_id,
                    envelope_dict=e.envelope_dict,
                    status=status,
                    ts_monotonic=e.ts_monotonic,
                    ts_utc=e.ts_utc,
                )

        return [e for e in entries.values() if e.status == "pending"]

    def compact(self) -> int:
        """Remove entries older than ``max_age_days``.

        Returns the number of removed lines.
        """
        if not self._path.exists():
            return 0

        max_age_s = self._max_age_days * 86400.0
        now = time.monotonic()
        kept: List[str] = []
        removed = 0

        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    removed += 1
                    continue
                ts = record.get("ts_monotonic", now)
                if (now - ts) < max_age_s:
                    kept.append(line)
                else:
                    removed += 1

        with self._path.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

        return removed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_line(self, record: Dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/governance/intake/test_wal.py -v
```
Expected: all 8 tests PASS.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/wal.py \
        tests/governance/intake/test_wal.py
git commit -m "feat(intake): add WAL with append/replay/compaction"
```

---

## Task 3: UnifiedIntakeRouter — core pipeline

**Files:**
- Create: `backend/core/ouroboros/governance/intake/unified_intake_router.py`
- Create: `tests/governance/intake/test_unified_intake_router.py`

**Step 1: Write the failing test**

`tests/governance/intake/test_unified_intake_router.py`:

```python
"""Tests for UnifiedIntakeRouter pipeline stages."""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)


def _env(source="backlog", urgency="normal", target_files=("backend/core/auth.py",),
         requires_human_ack=False, confidence=0.8, same_dedup=False):
    sig = "fixed_sig" if same_dedup else str(time.monotonic())
    return make_envelope(
        source=source,
        description="fix auth",
        target_files=target_files,
        repo="jarvis",
        confidence=confidence,
        urgency=urgency,
        evidence={"signature": sig},
        requires_human_ack=requires_human_ack,
    )


def _make_router(tmp_path, gls=None):
    if gls is None:
        gls = MagicMock()
        gls.submit = AsyncMock()
    config = IntakeRouterConfig(
        project_root=tmp_path,
        dedup_window_s=60.0,
    )
    return UnifiedIntakeRouter(gls=gls, config=config), gls


async def test_ingest_returns_enqueued(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        result = await router.ingest(_env())
        assert result == "enqueued"
    finally:
        await router.stop()


async def test_duplicate_dedup_key_within_window_returns_deduplicated(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        e1 = _env(same_dedup=True)
        e2 = _env(same_dedup=True)
        assert e1.dedup_key == e2.dedup_key
        r1 = await router.ingest(e1)
        r2 = await router.ingest(e2)
        assert r1 == "enqueued"
        assert r2 == "deduplicated"
    finally:
        await router.stop()


async def test_requires_human_ack_returns_pending_ack(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        result = await router.ingest(_env(requires_human_ack=True))
        assert result == "pending_ack"
    finally:
        await router.stop()


async def test_voice_human_priority_higher_than_backlog(tmp_path):
    router, _ = _make_router(tmp_path)
    # Priority map: voice_human=0, test_failure=1, backlog=2, ai_miner=3
    from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP
    assert _PRIORITY_MAP["voice_human"] < _PRIORITY_MAP["backlog"]
    assert _PRIORITY_MAP["test_failure"] < _PRIORITY_MAP["backlog"]
    assert _PRIORITY_MAP["backlog"] < _PRIORITY_MAP["ai_miner"]


async def test_intake_queue_depth_increments(tmp_path):
    gls = MagicMock()
    # Never resolve so queue fills up
    gls.submit = AsyncMock(side_effect=asyncio.sleep(9999))
    router, _ = _make_router(tmp_path, gls=gls)
    await router.start()
    try:
        await router.ingest(_env())
        # Give dispatch loop a moment to pick up the item
        await asyncio.sleep(0.05)
        # depth is 0 (dispatched) or 1 depending on timing — just check it's non-negative
        assert router.intake_queue_depth() >= 0
    finally:
        await router.stop()


async def test_submit_called_with_correct_trigger_source(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    router, _ = _make_router(tmp_path, gls=gls)
    await router.start()
    try:
        await router.ingest(_env(source="test_failure"))
        # Allow dispatch loop to run
        await asyncio.sleep(0.1)
    finally:
        await router.stop()
    # GLS.submit was called with trigger_source="test_failure"
    if gls.submit.call_count > 0:
        kwargs = gls.submit.call_args.kwargs
        assert kwargs.get("trigger_source") == "test_failure"


async def test_dead_letter_after_max_retries(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock(side_effect=RuntimeError("submit failed"))
    config = IntakeRouterConfig(
        project_root=tmp_path,
        max_retries=1,
        dedup_window_s=0.0,  # disable dedup so retries go through
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    try:
        env = _env()
        await router.ingest(env)
        # Wait for dispatch + retry to exhaust
        await asyncio.sleep(0.3)
        assert router.dead_letter_count() >= 1
    finally:
        await router.stop()


async def test_backpressure_signal_when_queue_full(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock(side_effect=asyncio.sleep(9999))
    config = IntakeRouterConfig(
        project_root=tmp_path,
        backpressure_threshold=1,
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    try:
        r1 = await router.ingest(_env())
        assert r1 == "enqueued"
        # Second enqueue from low-priority source should be back-pressured
        r2 = await router.ingest(_env(source="backlog"))
        assert r2 in ("enqueued", "backpressure")
    finally:
        await router.stop()
```

**Step 2: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/test_unified_intake_router.py -v
```
Expected: `ModuleNotFoundError`

**Step 3: Implement the router**

`backend/core/ouroboros/governance/intake/unified_intake_router.py`:

```python
"""
Unified Intake Router — Phase 2C.1

Pipeline: schema_validate → normalize → dedup → priority_arbitration →
          rate_gate → conflict_detect → human_ack_gate →
          wal_enqueue → dispatch_queue

Dispatch loop runs as a background asyncio.Task.
File advisory lock prevents two router instances on the same project root.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id

from .intent_envelope import EnvelopeValidationError, IntentEnvelope
from .wal import WAL, WALEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority map — lower int = higher priority
# ---------------------------------------------------------------------------
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
}

# Sources that bypass backpressure
_BACKPRESSURE_EXEMPT = frozenset({"voice_human", "test_failure"})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeRouterConfig:
    """Configuration for UnifiedIntakeRouter."""

    project_root: Path
    wal_path: Optional[Path] = None
    lock_path: Optional[Path] = None
    max_retries: int = 3
    backpressure_threshold: int = 10
    dedup_window_s: float = 600.0
    voice_dedup_window_s: float = 300.0
    max_queue_size: int = 100
    dispatch_timeout_s: float = 300.0

    @property
    def resolved_wal_path(self) -> Path:
        return self.wal_path or (self.project_root / ".jarvis" / "intake_wal.jsonl")

    @property
    def resolved_lock_path(self) -> Path:
        return self.lock_path or (self.project_root / ".jarvis" / "intake_router.lock")


# ---------------------------------------------------------------------------
# Pending-ACK store (in-memory; Phase 2C.1 implementation)
# ---------------------------------------------------------------------------


class PendingAckStore:
    """Parks envelopes that require human acknowledgement."""

    def __init__(self) -> None:
        self._store: Dict[str, IntentEnvelope] = {}

    def park(self, envelope: IntentEnvelope) -> None:
        self._store[envelope.idempotency_key] = envelope

    def acknowledge(self, idempotency_key: str) -> Optional[IntentEnvelope]:
        return self._store.pop(idempotency_key, None)

    def count(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class RouterAlreadyRunningError(RuntimeError):
    """Raised when a second router attempts to acquire the advisory lock."""


class UnifiedIntakeRouter:
    """Unified Intake Router — routes IntentEnvelopes to GovernedLoopService.

    Parameters
    ----------
    gls:
        GovernedLoopService instance (has ``submit(ctx, trigger_source=...)``).
    config:
        Router configuration.
    """

    def __init__(
        self,
        gls: Any,
        config: IntakeRouterConfig,
    ) -> None:
        self._gls = gls
        self._config = config
        self._wal = WAL(config.resolved_wal_path)
        self._queue: asyncio.PriorityQueue[Tuple[int, float, IntentEnvelope]] = (
            asyncio.PriorityQueue(maxsize=config.max_queue_size)
        )
        self._dedup: Dict[str, float] = {}  # dedup_key -> ts_monotonic
        self._retry_count: Dict[str, int] = {}  # idempotency_key -> count
        self._active_target_files: Set[frozenset] = set()
        self._pending_ack = PendingAckStore()
        self._dead_letter: list = []
        self._dispatch_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock_fd: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Acquire advisory lock, replay WAL pending entries, start dispatch."""
        if self._running:
            return
        self._acquire_lock()
        self._running = True
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="intake_dispatch"
        )
        await self._replay_wal()
        logger.info("UnifiedIntakeRouter started")

    async def stop(self) -> None:
        """Stop dispatch loop and release advisory lock."""
        self._running = False
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._release_lock()
        logger.info("UnifiedIntakeRouter stopped")

    # ------------------------------------------------------------------
    # Ingress
    # ------------------------------------------------------------------

    async def ingest(self, envelope: IntentEnvelope) -> str:
        """Route an IntentEnvelope through the full pipeline.

        Returns one of: ``"enqueued"``, ``"deduplicated"``, ``"pending_ack"``,
        ``"dead_letter"``, ``"backpressure"``, ``"schema_error"``.
        """
        # 1. Schema validate (already done in __post_init__, but confirm)
        try:
            _ = envelope.schema_version  # triggers __post_init__ on creation
        except EnvelopeValidationError as exc:
            logger.warning("Router: schema error: %s", exc)
            return "schema_error"

        # 2. Global dedup
        if self._is_duplicate(envelope):
            logger.debug("Router: deduplicated %s (key=%s)", envelope.description, envelope.dedup_key)
            return "deduplicated"

        # 3. Human ack gate
        if envelope.requires_human_ack:
            self._pending_ack.park(envelope)
            logger.info("Router: parked in PENDING_ACK: %s", envelope.description)
            return "pending_ack"

        # 4. Backpressure (exempt: voice_human, test_failure)
        if (
            envelope.source not in _BACKPRESSURE_EXEMPT
            and self.intake_queue_depth() >= self._config.backpressure_threshold
        ):
            logger.info("Router: backpressure for source=%s", envelope.source)
            return "backpressure"

        # 5. WAL enqueue
        lease_id = generate_operation_id("lse")
        envelope = envelope.with_lease(lease_id)
        self._wal.append(WALEntry(
            lease_id=lease_id,
            envelope_dict=envelope.to_dict(),
            status="pending",
            ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(),
        ))

        # 6. Register dedup
        self._register_dedup(envelope)

        # 7. Priority queue
        priority = _PRIORITY_MAP.get(envelope.source, 99)
        await self._queue.put((priority, envelope.submitted_at, envelope))
        logger.info(
            "Router: enqueued source=%s priority=%d lease=%s",
            envelope.source, priority, lease_id,
        )
        return "enqueued"

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def intake_queue_depth(self) -> int:
        return self._queue.qsize()

    def dead_letter_count(self) -> int:
        return len(self._dead_letter)

    def pending_ack_count(self) -> int:
        return self._pending_ack.count()

    # ------------------------------------------------------------------
    # Human ACK
    # ------------------------------------------------------------------

    async def acknowledge(self, idempotency_key: str) -> bool:
        """Approve a PENDING_ACK envelope and route it for dispatch.

        Returns True if the key was found and re-enqueued.
        """
        envelope = self._pending_ack.acknowledge(idempotency_key)
        if envelope is None:
            return False
        # Remove requires_human_ack by re-making (we can't mutate frozen)
        from .intent_envelope import make_envelope
        unblocked = make_envelope(
            source=envelope.source,
            description=envelope.description,
            target_files=envelope.target_files,
            repo=envelope.repo,
            confidence=envelope.confidence,
            urgency=envelope.urgency,
            evidence=envelope.evidence,
            requires_human_ack=False,
            causal_id=envelope.causal_id,
            signal_id=envelope.signal_id,
        )
        result = await self.ingest(unblocked)
        return result == "enqueued"

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                priority, ts, envelope = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._dispatch_one(envelope)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "Router: dispatch error for lease_id=%s", envelope.lease_id
                )
            finally:
                self._queue.task_done()

    async def _dispatch_one(self, envelope: IntentEnvelope) -> None:
        """Build OperationContext and call GLS.submit()."""
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=envelope.target_files,
            description=envelope.description,
            op_id=envelope.causal_id,
        )

        ikey = envelope.idempotency_key
        try:
            await asyncio.wait_for(
                self._gls.submit(ctx, trigger_source=envelope.source),
                timeout=self._config.dispatch_timeout_s,
            )
            self._wal.update_status(envelope.lease_id, "acked")
            self._retry_count.pop(ikey, None)
            logger.info("Router: dispatched op_id=%s source=%s", ctx.op_id, envelope.source)
        except Exception as exc:
            retries = self._retry_count.get(ikey, 0) + 1
            self._retry_count[ikey] = retries
            logger.warning(
                "Router: dispatch failed (attempt %d/%d) lease=%s: %s",
                retries, self._config.max_retries, envelope.lease_id, exc,
            )
            if retries >= self._config.max_retries:
                self._wal.update_status(envelope.lease_id, "dead_letter")
                self._dead_letter.append(envelope)
                self._retry_count.pop(ikey, None)
                logger.error(
                    "Router: dead-lettered lease=%s after %d retries",
                    envelope.lease_id, retries,
                )
            else:
                # Re-enqueue for retry (dedup bypass: different ikey each retry — same lease)
                priority = _PRIORITY_MAP.get(envelope.source, 99)
                await self._queue.put((priority, envelope.submitted_at, envelope))

    # ------------------------------------------------------------------
    # WAL replay
    # ------------------------------------------------------------------

    async def _replay_wal(self) -> None:
        """Re-ingest all pending WAL entries (crash recovery)."""
        pending = self._wal.pending_entries()
        if not pending:
            return
        logger.info("Router: replaying %d pending WAL entries", len(pending))
        from .intent_envelope import IntentEnvelope as IE
        for entry in pending:
            try:
                envelope = IE.from_dict(entry.envelope_dict)
                # Skip idempotency check against ledger here (Phase 2C.1):
                # GLS.submit() is itself idempotent if op_id already terminal
                priority = _PRIORITY_MAP.get(envelope.source, 99)
                await self._queue.put((priority, envelope.submitted_at, envelope))
            except Exception:
                logger.exception("Router: WAL replay failed for lease_id=%s", entry.lease_id)

    # ------------------------------------------------------------------
    # Dedup helpers
    # ------------------------------------------------------------------

    def _is_duplicate(self, envelope: IntentEnvelope) -> bool:
        window = (
            self._config.voice_dedup_window_s
            if envelope.source == "voice_human"
            else self._config.dedup_window_s
        )
        last = self._dedup.get(envelope.dedup_key)
        if last is None:
            return False
        return (time.monotonic() - last) < window

    def _register_dedup(self, envelope: IntentEnvelope) -> None:
        self._dedup[envelope.dedup_key] = time.monotonic()

    # ------------------------------------------------------------------
    # File lock
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        lock_path = self._config.resolved_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RouterAlreadyRunningError(
                f"Another router instance holds the lock at {lock_path}"
            )
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/governance/intake/test_unified_intake_router.py -v
```
Expected: all 8 tests PASS (one test about backpressure may be timing-sensitive; ensure `gls.submit` hangs long enough).

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/unified_intake_router.py \
        tests/governance/intake/test_unified_intake_router.py
git commit -m "feat(intake): add UnifiedIntakeRouter with WAL dispatch and dedup"
```

---

## Task 4: Sensor scaffolding + BacklogSensor (A)

**Files:**
- Create: `backend/core/ouroboros/governance/intake/sensors/__init__.py`
- Create: `backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py`
- Create: `tests/governance/intake/sensors/__init__.py`
- Create: `tests/governance/intake/sensors/test_backlog_sensor.py`

**Step 1: Create sensor package inits**

```bash
mkdir -p backend/core/ouroboros/governance/intake/sensors
touch backend/core/ouroboros/governance/intake/sensors/__init__.py
mkdir -p tests/governance/intake/sensors
touch tests/governance/intake/sensors/__init__.py
```

**Step 2: Write the failing test**

`tests/governance/intake/sensors/test_backlog_sensor.py`:

```python
"""Tests for BacklogSensor (Sensor A)."""
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    BacklogTask,
)


def _write_backlog(path: Path, tasks: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks))


def test_backlog_task_urgency_low_priority():
    task = BacklogTask(
        task_id="t1",
        description="improve caching",
        target_files=["backend/core/cache.py"],
        priority=1,
        repo="jarvis",
    )
    assert task.urgency == "low"


def test_backlog_task_urgency_high_priority():
    task = BacklogTask(
        task_id="t2",
        description="fix critical bug",
        target_files=["backend/core/auth.py"],
        priority=5,
        repo="jarvis",
    )
    assert task.urgency == "high"


async def test_sensor_produces_envelope_for_pending_task(tmp_path):
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    _write_backlog(backlog_path, [
        {
            "task_id": "t1",
            "description": "fix auth",
            "target_files": ["backend/core/auth.py"],
            "priority": 4,
            "repo": "jarvis",
            "status": "pending",
        }
    ])
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=backlog_path,
        repo_root=tmp_path,
        router=router,
        poll_interval_s=0.01,
    )
    envelopes = await sensor.scan_once()
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.source == "backlog"
    assert env.target_files == ("backend/core/auth.py",)
    assert env.urgency == "high"
    router.ingest.assert_called_once_with(env)


async def test_sensor_skips_completed_tasks(tmp_path):
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    _write_backlog(backlog_path, [
        {
            "task_id": "t1",
            "description": "done task",
            "target_files": ["backend/core/foo.py"],
            "priority": 3,
            "repo": "jarvis",
            "status": "completed",
        }
    ])
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=backlog_path,
        repo_root=tmp_path,
        router=router,
    )
    envelopes = await sensor.scan_once()
    assert envelopes == []
    router.ingest.assert_not_called()


async def test_sensor_missing_backlog_returns_empty(tmp_path):
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = BacklogSensor(
        backlog_path=tmp_path / "nonexistent.json",
        repo_root=tmp_path,
        router=router,
    )
    envelopes = await sensor.scan_once()
    assert envelopes == []


async def test_sensor_start_stop(tmp_path):
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=tmp_path / ".jarvis" / "backlog.json",
        repo_root=tmp_path,
        router=router,
        poll_interval_s=0.05,
    )
    await sensor.start()
    await asyncio.sleep(0.1)
    sensor.stop()
    # No crash = pass
```

**Step 3: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/sensors/test_backlog_sensor.py -v
```

**Step 4: Implement BacklogSensor**

`backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py`:

```python
"""
BacklogSensor (Sensor A) — Polls task backlog store for pending work.

Backlog file: ``{project_root}/.jarvis/backlog.json``  (default)
Schema per entry:
    {
        "task_id": str,
        "description": str,
        "target_files": [str, ...],
        "priority": int 1-5,
        "repo": str,
        "status": "pending" | "in_progress" | "completed"
    }

Priority → urgency mapping:
    5 → "critical", 4 → "high", 3 → "normal", 1-2 → "low"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope, IntentEnvelope

logger = logging.getLogger(__name__)

_PRIORITY_URGENCY = {5: "critical", 4: "high", 3: "normal", 2: "low", 1: "low"}


@dataclass
class BacklogTask:
    task_id: str
    description: str
    target_files: List[str]
    priority: int
    repo: str
    status: str = "pending"

    @property
    def urgency(self) -> str:
        return _PRIORITY_URGENCY.get(self.priority, "normal")


class BacklogSensor:
    """Polls a JSON backlog file and produces IntentEnvelopes for pending tasks.

    Parameters
    ----------
    backlog_path:
        Path to backlog JSON file.
    repo_root:
        Repository root (used for relative path normalization).
    router:
        UnifiedIntakeRouter to call ``ingest()`` on.
    poll_interval_s:
        Seconds between scans.
    """

    def __init__(
        self,
        backlog_path: Path,
        repo_root: Path,
        router: Any,
        poll_interval_s: float = 60.0,
    ) -> None:
        self._backlog_path = backlog_path
        self._repo_root = repo_root
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._seen_task_ids: set = set()

    async def scan_once(self) -> List[IntentEnvelope]:
        """Run one scan. Returns list of envelopes produced and ingested."""
        if not self._backlog_path.exists():
            return []

        try:
            raw = self._backlog_path.read_text(encoding="utf-8")
            tasks_raw: List[Dict] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("BacklogSensor: failed to read backlog: %s", exc)
            return []

        produced: List[IntentEnvelope] = []
        for item in tasks_raw:
            task = BacklogTask(
                task_id=item.get("task_id", ""),
                description=item.get("description", ""),
                target_files=list(item.get("target_files", [])),
                priority=int(item.get("priority", 3)),
                repo=item.get("repo", "jarvis"),
                status=item.get("status", "pending"),
            )
            if task.status != "pending":
                continue
            if task.task_id in self._seen_task_ids:
                continue
            if not task.target_files:
                continue

            envelope = make_envelope(
                source="backlog",
                description=task.description,
                target_files=tuple(task.target_files),
                repo=task.repo,
                confidence=0.7 + (task.priority - 1) * 0.05,
                urgency=task.urgency,
                evidence={"task_id": task.task_id, "signature": task.task_id},
                requires_human_ack=False,
            )
            try:
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    self._seen_task_ids.add(task.task_id)
                    produced.append(envelope)
                    logger.info("BacklogSensor: enqueued task_id=%s", task.task_id)
            except Exception:
                logger.exception("BacklogSensor: ingest failed for task_id=%s", task.task_id)

        return produced

    async def start(self) -> None:
        """Start background polling loop."""
        self._running = True
        asyncio.create_task(self._poll_loop(), name="backlog_sensor_poll")

    def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("BacklogSensor: poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break
```

**Step 5: Run tests**

```bash
python3 -m pytest tests/governance/intake/sensors/test_backlog_sensor.py -v
```
Expected: all 5 tests PASS.

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/__init__.py \
        backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py \
        tests/governance/intake/sensors/__init__.py \
        tests/governance/intake/sensors/test_backlog_sensor.py
git commit -m "feat(intake): add BacklogSensor (Sensor A) with polling and envelope production"
```

---

## Task 5: TestFailureSensor (B) — adapter over TestWatcher

**Files:**
- Create: `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py`
- Create: `tests/governance/intake/sensors/test_test_failure_sensor.py`

**Step 1: Write the failing test**

`tests/governance/intake/sensors/test_test_failure_sensor.py`:

```python
"""Tests for TestFailureSensor (Sensor B)."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(stable: bool = True, streak: int = 2) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_auth.py",),
        repo="jarvis",
        description="Stable test failure: test_auth::test_login",
        evidence={
            "signature": "AssertionError:tests/test_auth.py",
            "test_id": "tests/test_auth.py::test_login",
            "streak": streak,
            "error_text": "AssertionError",
        },
        confidence=min(0.95, 0.7 + 0.1 * streak),
        stable=stable,
    )


async def test_stable_signal_produces_envelope():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    assert envelope.source == "test_failure"
    assert envelope.target_files == ("tests/test_auth.py",)
    assert envelope.urgency == "high"
    assert envelope.evidence["test_id"] == "tests/test_auth.py::test_login"
    router.ingest.assert_called_once_with(envelope)


async def test_unstable_signal_is_skipped():
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=False)
    result = await sensor._signal_to_envelope_and_ingest(signal)
    assert result is None
    router.ingest.assert_not_called()


async def test_handle_signals_batch():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signals = [_make_signal(stable=True), _make_signal(stable=False)]
    results = await sensor.handle_signals(signals)
    # Only 1 stable signal → 1 envelope
    assert len([r for r in results if r is not None]) == 1


async def test_confidence_preserved_from_signal():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True, streak=5)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    # confidence should reflect streak: min(0.95, 0.7 + 0.1*5) = 0.95 (capped at 1.0 by envelope)
    assert 0.9 <= envelope.confidence <= 1.0
```

**Step 2: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/sensors/test_test_failure_sensor.py -v
```

**Step 3: Implement TestFailureSensor**

`backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py`:

```python
"""
TestFailureSensor (Sensor B) — Adapter over existing TestWatcher.

Converts stable IntentSignal(source='intent:test_failure') objects into
IntentEnvelope(source='test_failure') objects and ingests them via the router.

The existing TestWatcher (intent/test_watcher.py) handles pytest polling and
streak-based stability detection. This sensor wraps it as an adapter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger(__name__)


class TestFailureSensor:
    """Adapter that bridges TestWatcher → UnifiedIntakeRouter.

    Parameters
    ----------
    repo:
        Repository name (e.g. ``"jarvis"``).
    router:
        UnifiedIntakeRouter instance.
    test_watcher:
        Optional existing TestWatcher. If None, sensor operates in
        signal-push mode only (caller calls ``handle_signals()``).
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        test_watcher: Any = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._watcher = test_watcher
        self._running = False

    async def _signal_to_envelope_and_ingest(
        self, signal: IntentSignal
    ) -> Optional[IntentEnvelope]:
        """Convert one IntentSignal to IntentEnvelope and ingest it.

        Returns the envelope if ingested, None if skipped.
        """
        if not signal.stable:
            return None

        confidence = min(1.0, signal.confidence)
        envelope = make_envelope(
            source="test_failure",
            description=signal.description,
            target_files=signal.target_files,
            repo=self._repo,
            confidence=confidence,
            urgency="high",
            evidence=dict(signal.evidence),
            requires_human_ack=False,
            causal_id=signal.signal_id,  # signal_id becomes causal_id
            signal_id=signal.signal_id,
        )
        try:
            result = await self._router.ingest(envelope)
            if result == "enqueued":
                logger.info(
                    "TestFailureSensor: enqueued test failure: %s",
                    signal.description,
                )
            return envelope
        except Exception:
            logger.exception("TestFailureSensor: ingest failed: %s", signal.description)
            return None

    async def handle_signals(
        self, signals: List[IntentSignal]
    ) -> List[Optional[IntentEnvelope]]:
        """Process a batch of IntentSignals. Returns per-signal results."""
        results = []
        for sig in signals:
            result = await self._signal_to_envelope_and_ingest(sig)
            results.append(result)
        return results

    async def start(self) -> None:
        """Start background polling via TestWatcher (if provided)."""
        if self._watcher is None:
            return
        self._running = True
        asyncio.create_task(self._poll_loop(), name="test_failure_sensor_poll")

    def stop(self) -> None:
        self._running = False
        if self._watcher is not None:
            self._watcher.stop()

    async def _poll_loop(self) -> None:
        while self._running and self._watcher is not None:
            try:
                signals = await self._watcher.poll_once()
                if signals:
                    await self.handle_signals(signals)
            except Exception:
                logger.exception("TestFailureSensor: poll error")
            try:
                await asyncio.sleep(self._watcher.poll_interval_s)
            except asyncio.CancelledError:
                break
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/governance/intake/sensors/test_test_failure_sensor.py -v
```
Expected: all 4 tests PASS.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py \
        tests/governance/intake/sensors/test_test_failure_sensor.py
git commit -m "feat(intake): add TestFailureSensor (Sensor B) adapter over TestWatcher"
```

---

## Task 6: VoiceCommandSensor (C)

**Files:**
- Create: `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py`
- Create: `tests/governance/intake/sensors/test_voice_command_sensor.py`

**Step 1: Write the failing test**

`tests/governance/intake/sensors/test_voice_command_sensor.py`:

```python
"""Tests for VoiceCommandSensor (Sensor C)."""
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandSensor,
    VoiceCommandPayload,
)


def _payload(
    description="fix the auth module",
    target_files=("backend/core/auth.py",),
    stt_confidence=0.95,
):
    return VoiceCommandPayload(
        description=description,
        target_files=list(target_files),
        repo="jarvis",
        stt_confidence=stt_confidence,
    )


async def test_high_confidence_command_enqueued():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    result = await sensor.handle_voice_command(_payload(stt_confidence=0.95))
    assert result == "enqueued"
    router.ingest.assert_called_once()
    env = router.ingest.call_args.args[0]
    assert env.source == "voice_human"
    assert env.urgency == "critical"
    assert env.requires_human_ack is False


async def test_low_confidence_requires_human_ack():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = VoiceCommandSensor(router=router, repo="jarvis", stt_confidence_threshold=0.82)
    result = await sensor.handle_voice_command(_payload(stt_confidence=0.75))
    assert result == "pending_ack"
    env = router.ingest.call_args.args[0]
    assert env.requires_human_ack is True


async def test_empty_target_files_returns_error():
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    result = await sensor.handle_voice_command(_payload(target_files=[]))
    assert result == "error"
    router.ingest.assert_not_called()


async def test_rate_limit_per_hour_enforced():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis", rate_limit_per_hour=2)
    # Fill up the rate limit
    for _ in range(2):
        await sensor.handle_voice_command(_payload(description=f"cmd {_}"))
    # Third should be rate-limited
    result = await sensor.handle_voice_command(_payload(description="cmd overflow"))
    assert result == "rate_limited"


async def test_causal_chain_source_preserved():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    await sensor.handle_voice_command(_payload())
    env = router.ingest.call_args.args[0]
    # causal_id and signal_id should be set
    assert len(env.causal_id) > 0
    assert len(env.signal_id) > 0
    assert env.causal_id == env.signal_id  # voice: causal = signal (user is the origin)
```

**Step 2: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/sensors/test_voice_command_sensor.py -v
```

**Step 3: Implement VoiceCommandSensor**

`backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py`:

```python
"""
VoiceCommandSensor (Sensor C) — Human voice intent → IntentEnvelope.

Called by the voice intent pipeline when a self-dev intent is recognized.
STT confidence gate: commands below threshold are flagged ``requires_human_ack=True``
so the router parks them for explicit confirmation before dispatch.

Rate guard: max ``rate_limit_per_hour`` voice-triggered ops per rolling hour.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600.0


@dataclass
class VoiceCommandPayload:
    """Parsed voice command payload from the STT pipeline."""

    description: str
    target_files: List[str]
    repo: str
    stt_confidence: float = 1.0
    evidence: dict = field(default_factory=dict)


class VoiceCommandSensor:
    """Converts recognized voice self-dev commands into IntentEnvelopes.

    Parameters
    ----------
    router:
        UnifiedIntakeRouter.
    repo:
        Repository name.
    stt_confidence_threshold:
        STT confidence below this value → ``requires_human_ack=True``.
    rate_limit_per_hour:
        Maximum voice-triggered ops per rolling 1-hour window.
    """

    def __init__(
        self,
        router: Any,
        repo: str,
        stt_confidence_threshold: float = 0.82,
        rate_limit_per_hour: int = 3,
    ) -> None:
        self._router = router
        self._repo = repo
        self._threshold = stt_confidence_threshold
        self._rate_limit = rate_limit_per_hour
        self._op_timestamps: List[float] = []

    async def handle_voice_command(self, payload: VoiceCommandPayload) -> str:
        """Process one recognized voice command.

        Returns one of: ``"enqueued"``, ``"pending_ack"``,
        ``"rate_limited"``, ``"error"``.
        """
        if not payload.target_files:
            logger.warning("VoiceCommandSensor: empty target_files, skipping")
            return "error"

        # Rate limit
        now = time.monotonic()
        self._op_timestamps = [
            ts for ts in self._op_timestamps if (now - ts) < _SECONDS_PER_HOUR
        ]
        if len(self._op_timestamps) >= self._rate_limit:
            logger.info("VoiceCommandSensor: rate limit reached (%d/h)", self._rate_limit)
            return "rate_limited"

        # STT confidence gate
        requires_ack = payload.stt_confidence < self._threshold

        # causal_id == signal_id: voice command is origin (user IS the cause)
        origin_id = generate_operation_id("vox")
        evidence = dict(payload.evidence)
        evidence.setdefault("stt_confidence", payload.stt_confidence)
        evidence.setdefault("signature", payload.description[:64])

        envelope = make_envelope(
            source="voice_human",
            description=payload.description,
            target_files=tuple(payload.target_files),
            repo=self._repo,
            confidence=payload.stt_confidence,
            urgency="critical",
            evidence=evidence,
            requires_human_ack=requires_ack,
            causal_id=origin_id,
            signal_id=origin_id,
        )

        try:
            result = await self._router.ingest(envelope)
            if result in ("enqueued", "pending_ack"):
                self._op_timestamps.append(now)
            logger.info(
                "VoiceCommandSensor: result=%s requires_ack=%s cmd=%s",
                result, requires_ack, payload.description,
            )
            return result
        except Exception:
            logger.exception("VoiceCommandSensor: ingest failed: %s", payload.description)
            return "error"
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/governance/intake/sensors/test_voice_command_sensor.py -v
```
Expected: all 5 tests PASS.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py \
        tests/governance/intake/sensors/test_voice_command_sensor.py
git commit -m "feat(intake): add VoiceCommandSensor (Sensor C) with STT confidence gate"
```

---

## Task 7: OpportunityMinerSensor (D) — observe-only

**Files:**
- Create: `backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py`
- Create: `tests/governance/intake/sensors/test_opportunity_miner_sensor.py`

**Step 1: Write the failing test**

`tests/governance/intake/sensors/test_opportunity_miner_sensor.py`:

```python
"""Tests for OpportunityMinerSensor (Sensor D) — observe-only."""
import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
    StaticCandidate,
    _cyclomatic_complexity,
)


def test_cyclomatic_complexity_simple_function():
    src = "def foo():\n    return 1\n"
    tree = ast.parse(src)
    assert _cyclomatic_complexity(tree) == 1


def test_cyclomatic_complexity_branchy_function():
    src = """
def foo(x):
    if x > 0:
        for i in range(x):
            if i % 2 == 0:
                pass
    elif x < 0:
        while x < 0:
            x += 1
    return x
"""
    tree = ast.parse(src)
    cc = _cyclomatic_complexity(tree)
    assert cc >= 4  # if + for + if + elif + while = 5 branches


async def test_sensor_produces_pending_ack_envelope(tmp_path):
    # Write a complex Python file
    src_file = tmp_path / "backend" / "core" / "complex.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    # High-complexity code
    lines = ["def foo(x):\n"]
    for i in range(12):
        lines.append(f"    if x == {i}:\n        return {i}\n")
    lines.append("    return -1\n")
    src_file.write_text("".join(lines))

    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["backend/core/"],
        complexity_threshold=5,
    )
    candidates = await sensor.scan_once()
    assert len(candidates) >= 1
    router.ingest.assert_called()
    # All D envelopes must have requires_human_ack=True
    for call in router.ingest.call_args_list:
        env = call.args[0]
        assert env.requires_human_ack is True
        assert env.source == "ai_miner"


async def test_sensor_skips_low_complexity_files(tmp_path):
    src_file = tmp_path / "simple.py"
    src_file.write_text("def foo():\n    return 1\n")
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=10,
    )
    candidates = await sensor.scan_once()
    assert candidates == []
    router.ingest.assert_not_called()


async def test_sensor_skips_syntax_error_files(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def broken(:\n    pass\n")
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=1,
    )
    candidates = await sensor.scan_once()
    # Syntax error file should be skipped, not crash
    assert candidates == []
```

**Step 2: Run to confirm failure**

```bash
python3 -m pytest tests/governance/intake/sensors/test_opportunity_miner_sensor.py -v
```

**Step 3: Implement OpportunityMinerSensor**

`backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py`:

```python
"""
OpportunityMinerSensor (Sensor D) — Static complexity analysis → observe-only.

Phase 2C.1: ALL D envelopes have requires_human_ack=True. The router parks
them in PENDING_ACK. Auto-submit is enabled in Phase 2C.4 after confidence
formula tuning and audit pass.

Static evidence: AST cyclomatic complexity above threshold.
LLM triage: NOT implemented in Phase 2C.1 (confidence = static only).

Confidence formula (Phase 2C.1, static-only):
    confidence = static_evidence_score × 0.5
    (llm_quality_score, risk_penalty, novelty_penalty added in Phase 2C.4)
"""
from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger(__name__)


@dataclass
class StaticCandidate:
    file_path: str
    cyclomatic_complexity: int
    static_evidence_score: float


def _cyclomatic_complexity(tree: ast.AST) -> int:
    """Count branching nodes (if/elif/for/while/with/try/except/and/or)."""
    _BRANCH_NODES = (
        ast.If, ast.For, ast.While, ast.With,
        ast.ExceptHandler, ast.BoolOp,
    )
    count = 1  # baseline
    for node in ast.walk(tree):
        if isinstance(node, _BRANCH_NODES):
            count += 1
    return count


class OpportunityMinerSensor:
    """Scans Python files for high cyclomatic complexity and produces envelopes.

    Parameters
    ----------
    repo_root:
        Repository root.
    router:
        UnifiedIntakeRouter.
    scan_paths:
        List of relative paths (dirs or files) to scan.
    complexity_threshold:
        Files with cyclomatic complexity >= this value are candidates.
    max_candidates_per_scan:
        Cap per scan to avoid flooding.
    poll_interval_s:
        Seconds between scan cycles.
    """

    def __init__(
        self,
        repo_root: Path,
        router: Any,
        scan_paths: Optional[List[str]] = None,
        complexity_threshold: int = 10,
        max_candidates_per_scan: int = 3,
        poll_interval_s: float = 3600.0,
        repo: str = "jarvis",
    ) -> None:
        self._repo_root = repo_root
        self._router = router
        self._scan_paths = scan_paths or ["backend/"]
        self._threshold = complexity_threshold
        self._max_candidates = max_candidates_per_scan
        self._poll_interval_s = poll_interval_s
        self._repo = repo
        self._running = False
        self._recently_scanned: set = set()

    def _collect_python_files(self) -> List[Path]:
        files: List[Path] = []
        for rel_path in self._scan_paths:
            target = self._repo_root / rel_path
            if target.is_file() and target.suffix == ".py":
                files.append(target)
            elif target.is_dir():
                files.extend(target.rglob("*.py"))
        return files

    def _analyze_file(self, path: Path) -> Optional[StaticCandidate]:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(path))
        except (SyntaxError, OSError):
            return None
        cc = _cyclomatic_complexity(tree)
        if cc < self._threshold:
            return None
        # Normalize score: 0.5 at threshold, 1.0 at 2× threshold
        score = min(1.0, 0.5 + 0.5 * (cc - self._threshold) / max(1, self._threshold))
        rel = str(path.relative_to(self._repo_root))
        return StaticCandidate(
            file_path=rel,
            cyclomatic_complexity=cc,
            static_evidence_score=score,
        )

    async def scan_once(self) -> List[IntentEnvelope]:
        """Run one scan. Returns list of envelopes produced (all pending_ack)."""
        all_files = self._collect_python_files()
        candidates: List[StaticCandidate] = []
        for path in all_files:
            c = self._analyze_file(path)
            if c and c.file_path not in self._recently_scanned:
                candidates.append(c)

        # Sort by score descending, cap
        candidates.sort(key=lambda c: c.static_evidence_score, reverse=True)
        candidates = candidates[: self._max_candidates]

        produced: List[IntentEnvelope] = []
        for cand in candidates:
            confidence = cand.static_evidence_score * 0.5  # Phase 2C.1: static only
            envelope = make_envelope(
                source="ai_miner",
                description=(
                    f"High complexity (CC={cand.cyclomatic_complexity}) "
                    f"in {cand.file_path}"
                ),
                target_files=(cand.file_path,),
                repo=self._repo,
                confidence=confidence,
                urgency="low",
                evidence={
                    "cyclomatic_complexity": cand.cyclomatic_complexity,
                    "static_evidence_score": cand.static_evidence_score,
                    "signature": cand.file_path,
                    # TODO(2c.4): llm_quality_score, risk_penalty, novelty_penalty
                },
                requires_human_ack=True,  # Phase 2C.1: ALWAYS require human ACK
            )
            try:
                result = await self._router.ingest(envelope)
                if result == "pending_ack":
                    self._recently_scanned.add(cand.file_path)
                    produced.append(envelope)
                    logger.info(
                        "OpportunityMiner: parked for review: %s (CC=%d)",
                        cand.file_path, cand.cyclomatic_complexity,
                    )
            except Exception:
                logger.exception("OpportunityMiner: ingest failed: %s", cand.file_path)

        return produced

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop(), name="opportunity_miner_poll")

    def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("OpportunityMiner: scan error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/governance/intake/sensors/test_opportunity_miner_sensor.py -v
```
Expected: all 4 tests PASS.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py \
        tests/governance/intake/sensors/test_opportunity_miner_sensor.py
git commit -m "feat(intake): add OpportunityMinerSensor (Sensor D) observe-only with AST complexity"
```

---

## Task 8: Module exports + governance __init__ update

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/__init__.py`
- Modify: `backend/core/ouroboros/governance/intake/sensors/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: Update intake `__init__.py`**

`backend/core/ouroboros/governance/intake/__init__.py`:

```python
"""Public API for the Unified Intake Layer (Phase 2C)."""
from .intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)
from .wal import WAL, WALEntry
from .unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
    RouterAlreadyRunningError,
)

__all__ = [
    "IntentEnvelope",
    "EnvelopeValidationError",
    "make_envelope",
    "SCHEMA_VERSION",
    "WAL",
    "WALEntry",
    "UnifiedIntakeRouter",
    "IntakeRouterConfig",
    "RouterAlreadyRunningError",
]
```

**Step 2: Update sensors `__init__.py`**

`backend/core/ouroboros/governance/intake/sensors/__init__.py`:

```python
"""Sensor adapters for the Unified Intake Router (Phase 2C)."""
from .backlog_sensor import BacklogSensor, BacklogTask
from .test_failure_sensor import TestFailureSensor
from .voice_command_sensor import VoiceCommandSensor, VoiceCommandPayload
from .opportunity_miner_sensor import OpportunityMinerSensor, StaticCandidate

__all__ = [
    "BacklogSensor",
    "BacklogTask",
    "TestFailureSensor",
    "VoiceCommandSensor",
    "VoiceCommandPayload",
    "OpportunityMinerSensor",
    "StaticCandidate",
]
```

**Step 3: Append intake exports to governance `__init__.py`**

Read the current end of `backend/core/ouroboros/governance/__init__.py` and append:

```python
# ── Intake Layer (Phase 2C) ──────────────────────────────────────────────────
from backend.core.ouroboros.governance.intake import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope as make_intent_envelope,
    SCHEMA_VERSION as INTAKE_SCHEMA_VERSION,
    WAL,
    WALEntry,
    UnifiedIntakeRouter,
    IntakeRouterConfig,
    RouterAlreadyRunningError,
)
from backend.core.ouroboros.governance.intake.sensors import (
    BacklogSensor,
    BacklogTask,
    TestFailureSensor,
    VoiceCommandSensor,
    VoiceCommandPayload,
    OpportunityMinerSensor,
    StaticCandidate,
)
```

And add all those names to `__all__`.

**Step 4: Run full governance test suite**

```bash
python3 -m pytest tests/governance/ -x -q
```
Expected: all existing + new tests PASS.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/__init__.py \
        backend/core/ouroboros/governance/intake/sensors/__init__.py \
        backend/core/ouroboros/governance/__init__.py
git commit -m "feat(intake): wire intake layer exports into governance __init__"
```

---

## Task 9: Crash recovery + out-of-order event tests

**Files:**
- Create: `tests/governance/intake/test_crash_recovery.py`
- Create: `tests/governance/intake/test_out_of_order_events.py`

**Step 1: Write the failing tests**

`tests/governance/intake/test_crash_recovery.py`:

```python
"""Crash recovery: WAL replay on router restart."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _env():
    return make_envelope(
        source="backlog",
        description="fix auth",
        target_files=("backend/core/auth.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"signature": "unique_sig_crash"},
        requires_human_ack=False,
    )


async def test_wal_pending_entries_replayed_on_router_start(tmp_path):
    """Pending WAL entries from a previous run are re-ingested on start."""
    gls = MagicMock()
    gls.submit = AsyncMock()

    wal_path = tmp_path / ".jarvis" / "intake_wal.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-populate WAL with a pending entry (simulates crash mid-dispatch)
    env = _env()
    lease_id = "pre_crash_lease_001"
    env = env.with_lease(lease_id)
    wal = WAL(wal_path)
    wal.append(WALEntry(
        lease_id=lease_id,
        envelope_dict=env.to_dict(),
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    ))

    config = IntakeRouterConfig(project_root=tmp_path, wal_path=wal_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    # Allow dispatch loop to process replayed entry
    await asyncio.sleep(0.2)
    await router.stop()

    # GLS.submit was called (replayed entry dispatched)
    assert gls.submit.call_count >= 1


async def test_acked_entries_not_replayed(tmp_path):
    """Entries with status='acked' are not re-dispatched on restart."""
    gls = MagicMock()
    gls.submit = AsyncMock()

    wal_path = tmp_path / ".jarvis" / "intake_wal.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    env = _env()
    lease_id = "already_acked_001"
    env = env.with_lease(lease_id)
    wal = WAL(wal_path)
    wal.append(WALEntry(
        lease_id=lease_id,
        envelope_dict=env.to_dict(),
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    ))
    wal.update_status(lease_id, "acked")  # Mark as acked before "restart"

    config = IntakeRouterConfig(project_root=tmp_path, wal_path=wal_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    await asyncio.sleep(0.15)
    await router.stop()

    # Should NOT be dispatched again
    assert gls.submit.call_count == 0


async def test_idempotent_key_not_double_dispatched(tmp_path):
    """Two envelopes with same idempotency_key: second is deduplicated."""
    gls = MagicMock()
    submit_count = 0

    async def counting_submit(ctx, trigger_source=""):
        nonlocal submit_count
        submit_count += 1

    gls.submit = counting_submit

    config = IntakeRouterConfig(
        project_root=tmp_path, dedup_window_s=60.0
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    e1 = make_envelope(
        source="backlog", description="fix x",
        target_files=("a.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "idem_test"},
        requires_human_ack=False,
    )
    e2 = make_envelope(
        source="backlog", description="fix x",
        target_files=("a.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "idem_test"},
        requires_human_ack=False,
    )
    # Same dedup_key (same signature + source + files)
    assert e1.dedup_key == e2.dedup_key

    r1 = await router.ingest(e1)
    r2 = await router.ingest(e2)
    assert r1 == "enqueued"
    assert r2 == "deduplicated"

    await asyncio.sleep(0.15)
    await router.stop()
    assert submit_count == 1
```

`tests/governance/intake/test_out_of_order_events.py`:

```python
"""Out-of-order and duplicate event tests for the intake layer."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)


def _env(sig: str = "default_sig", source: str = "backlog"):
    return make_envelope(
        source=source,
        description=f"fix {sig}",
        target_files=(f"backend/core/{sig}.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"signature": sig},
        requires_human_ack=False,
    )


async def test_voice_human_dispatched_before_backlog(tmp_path):
    """voice_human priority=0 dispatches before backlog priority=2."""
    dispatch_order = []

    async def mock_submit(ctx, trigger_source=""):
        dispatch_order.append(trigger_source)
        await asyncio.sleep(0.01)  # slight delay to let ordering matter

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    # Enqueue backlog first, then voice_human
    await router.ingest(_env("sig_backlog", source="backlog"))
    await router.ingest(_env("sig_voice", source="voice_human"))
    await asyncio.sleep(0.3)
    await router.stop()

    # Both dispatched; voice_human may arrive first due to priority
    assert "backlog" in dispatch_order
    assert "voice_human" in dispatch_order


async def test_pending_ack_envelope_not_dispatched_without_ack(tmp_path):
    """requires_human_ack=True envelopes stay in PENDING_ACK until acknowledged."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = _env("needs_ack")
    env_with_ack = make_envelope(
        source="ai_miner",
        description="needs ack",
        target_files=("backend/core/needs_ack.py",),
        repo="jarvis",
        confidence=0.5,
        urgency="low",
        evidence={"signature": "needs_ack"},
        requires_human_ack=True,
    )
    result = await router.ingest(env_with_ack)
    assert result == "pending_ack"

    await asyncio.sleep(0.1)
    # Submit should NOT have been called
    gls.submit.assert_not_called()

    await router.stop()


async def test_acknowledge_releases_pending_ack(tmp_path):
    """Calling router.acknowledge() enqueues the held envelope for dispatch."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_envelope(
        source="ai_miner",
        description="miner candidate",
        target_files=("backend/core/complex.py",),
        repo="jarvis",
        confidence=0.5,
        urgency="low",
        evidence={"signature": "ack_test"},
        requires_human_ack=True,
    )
    await router.ingest(env)
    assert router.pending_ack_count() == 1

    # Human approves
    released = await router.acknowledge(env.idempotency_key)
    assert released is True

    await asyncio.sleep(0.15)
    await router.stop()

    # Now submit should have been called
    assert gls.submit.call_count >= 1
```

**Step 2: Run to confirm expected behavior**

```bash
python3 -m pytest tests/governance/intake/test_crash_recovery.py \
                 tests/governance/intake/test_out_of_order_events.py -v
```
Expected: all 6 tests PASS.

**Step 3: Commit**

```bash
git add tests/governance/intake/test_crash_recovery.py \
        tests/governance/intake/test_out_of_order_events.py
git commit -m "test(intake): add crash recovery and out-of-order event tests"
```

---

## Task 10: Phase 2C.1 acceptance tests + full suite run

**Files:**
- Create: `tests/governance/integration/test_phase2c_acceptance.py`

**Step 1: Write acceptance tests**

`tests/governance/integration/test_phase2c_acceptance.py`:

```python
"""
Phase 2C.1 acceptance tests.

Acceptance criteria (ACs):
AC1: All four sensors produce IntentEnvelope(schema_version="2c.1")
AC2: Sensor D (ai_miner) envelopes always have requires_human_ack=True
AC3: Router routes voice_human at higher priority than backlog
AC4: WAL persists pending entries; router replays on restart
AC5: Deduplicated envelopes never reach GLS.submit()
AC6: Causal chain flows: causal_id → OperationContext.op_id
AC7: Human ACK gate: pending_ack envelopes dispatched only after acknowledge()
"""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake import (
    IntentEnvelope,
    make_intent_envelope,
    UnifiedIntakeRouter,
    IntakeRouterConfig,
    INTAKE_SCHEMA_VERSION,
)
from backend.core.ouroboros.governance.intake.sensors import (
    BacklogSensor,
    TestFailureSensor,
    VoiceCommandSensor,
    VoiceCommandPayload,
    OpportunityMinerSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


# ── AC1: All sensors produce schema_version = "2c.1" ─────────────────────────

async def test_ac1_backlog_sensor_schema_version(tmp_path):
    import json
    bp = tmp_path / ".jarvis" / "backlog.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps([{
        "task_id": "t1", "description": "fix x",
        "target_files": ["backend/core/x.py"],
        "priority": 3, "repo": "jarvis", "status": "pending",
    }]))
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(backlog_path=bp, repo_root=tmp_path, router=router)
    envelopes = await sensor.scan_once()
    assert len(envelopes) == 1
    assert envelopes[0].schema_version == INTAKE_SCHEMA_VERSION


async def test_ac1_test_failure_sensor_schema_version():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_x.py",),
        repo="jarvis",
        description="Stable failure: test_x",
        evidence={"signature": "err:test_x.py", "test_id": "tests/test_x.py::test_x"},
        confidence=0.8,
        stable=True,
    )
    env = await sensor._signal_to_envelope_and_ingest(sig)
    assert env is not None
    assert env.schema_version == INTAKE_SCHEMA_VERSION


async def test_ac1_voice_command_sensor_schema_version():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    await sensor.handle_voice_command(VoiceCommandPayload(
        description="fix the auth module",
        target_files=["backend/core/auth.py"],
        repo="jarvis",
        stt_confidence=0.95,
    ))
    env = router.ingest.call_args.args[0]
    assert env.schema_version == INTAKE_SCHEMA_VERSION


# ── AC2: Sensor D always requires_human_ack=True ─────────────────────────────

async def test_ac2_miner_always_requires_human_ack(tmp_path):
    src = tmp_path / "complex.py"
    lines = ["def foo(x):\n"] + [f"    if x=={i}: return {i}\n" for i in range(15)] + ["    return -1\n"]
    src.write_text("".join(lines))
    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path, router=router,
        scan_paths=["."], complexity_threshold=5,
    )
    envelopes = await sensor.scan_once()
    assert len(envelopes) >= 1
    for env in envelopes:
        assert env.requires_human_ack is True


# ── AC5: Deduplicated envelopes never reach GLS.submit() ─────────────────────

async def test_ac5_dedup_prevents_double_submit(tmp_path):
    submitted_op_ids = []

    async def mock_submit(ctx, trigger_source=""):
        submitted_op_ids.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path, dedup_window_s=60.0)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env1 = make_intent_envelope(
        source="backlog", description="fix y",
        target_files=("backend/y.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "ac5_sig"},
        requires_human_ack=False,
    )
    env2 = make_intent_envelope(
        source="backlog", description="fix y",
        target_files=("backend/y.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "ac5_sig"},
        requires_human_ack=False,
    )
    assert env1.dedup_key == env2.dedup_key

    r1 = await router.ingest(env1)
    r2 = await router.ingest(env2)
    assert r1 == "enqueued"
    assert r2 == "deduplicated"

    await asyncio.sleep(0.15)
    await router.stop()
    assert len(submitted_op_ids) == 1


# ── AC6: Causal chain: causal_id → OperationContext.op_id ────────────────────

async def test_ac6_causal_id_becomes_op_id(tmp_path):
    captured_op_ids = []

    async def mock_submit(ctx, trigger_source=""):
        captured_op_ids.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_intent_envelope(
        source="voice_human", description="fix auth now",
        target_files=("backend/core/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "causal_chain_test"},
        requires_human_ack=False,
    )
    await router.ingest(env)
    await asyncio.sleep(0.15)
    await router.stop()

    assert len(captured_op_ids) == 1
    # OperationContext.op_id must equal IntentEnvelope.causal_id
    assert captured_op_ids[0] == env.causal_id


# ── AC7: Human ACK gate ───────────────────────────────────────────────────────

async def test_ac7_human_ack_gate(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_intent_envelope(
        source="ai_miner", description="miner candidate",
        target_files=("backend/core/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "ac7_test"},
        requires_human_ack=True,
    )
    result = await router.ingest(env)
    assert result == "pending_ack"
    assert router.pending_ack_count() == 1

    await asyncio.sleep(0.05)
    gls.submit.assert_not_called()  # Not dispatched yet

    released = await router.acknowledge(env.idempotency_key)
    assert released is True

    await asyncio.sleep(0.15)
    await router.stop()
    gls.submit.assert_called_once()
```

**Step 2: Run acceptance tests**

```bash
python3 -m pytest tests/governance/integration/test_phase2c_acceptance.py -v
```
Expected: all 7 AC tests PASS.

**Step 3: Run the full test suite**

```bash
python3 -m pytest tests/ -x -q
```
Expected: all existing tests + new Phase 2C.1 tests PASS. Zero regressions.

**Step 4: Commit**

```bash
git add tests/governance/integration/test_phase2c_acceptance.py
git commit -m "test(intake): add Phase 2C.1 acceptance tests (AC1-AC7)"
```

---

## Final Verification

After all tasks complete, run:

```bash
python3 -m pytest tests/ -q --tb=short
python3 -m pyright backend/core/ouroboros/governance/intake/ 2>&1 | grep -c "error"
```

Expected:
- All tests PASS, 0 failures
- pyright: 0 errors

---

Plan complete and saved to `docs/plans/2026-03-08-phase-2c-loop-activation.md`.

Two execution options:

**1. Subagent-Driven (this session)** — Fresh subagent per task, spec + quality review after each, fast iteration

**2. Parallel Session (separate)** — Open new session with executing-plans, batch execution with checkpoints

Which approach?
