"""tests/governance/self_dev/test_approval_store.py

Unit tests for ApprovalStore — durable, atomic, cross-process safe.
"""
import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.approval_store import (
    ApprovalRecord,
    ApprovalState,
    ApprovalStore,
)


@pytest.fixture
def store(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")


# -- create --


def test_create_writes_pending_record(store: ApprovalStore):
    record = store.create("op-123", policy_version="v1.0")
    assert record.op_id == "op-123"
    assert record.state == ApprovalState.PENDING
    assert record.policy_version == "v1.0"
    assert record.decided_at is None


def test_create_is_idempotent(store: ApprovalStore):
    r1 = store.create("op-123", policy_version="v1.0")
    r2 = store.create("op-123", policy_version="v1.0")
    assert r1.created_at == r2.created_at


# -- decide --


def test_decide_approve(store: ApprovalStore):
    store.create("op-123", policy_version="v1.0")
    record = store.decide("op-123", ApprovalState.APPROVED, reason="looks good")
    assert record.state == ApprovalState.APPROVED
    assert record.reason == "looks good"
    assert record.decided_at is not None


def test_decide_reject(store: ApprovalStore):
    store.create("op-123", policy_version="v1.0")
    record = store.decide("op-123", ApprovalState.REJECTED, reason="wrong approach")
    assert record.state == ApprovalState.REJECTED
    assert record.reason == "wrong approach"


def test_decide_already_decided_returns_superseded(store: ApprovalStore):
    store.create("op-123", policy_version="v1.0")
    store.decide("op-123", ApprovalState.APPROVED, reason="ok")
    record = store.decide("op-123", ApprovalState.REJECTED, reason="too late")
    assert record.state == ApprovalState.SUPERSEDED


def test_decide_idempotent_same_decision(store: ApprovalStore):
    store.create("op-123", policy_version="v1.0")
    r1 = store.decide("op-123", ApprovalState.APPROVED, reason="ok")
    r2 = store.decide("op-123", ApprovalState.APPROVED, reason="ok again")
    assert r2.state == ApprovalState.APPROVED
    assert r2.decided_at == r1.decided_at  # same original decision


# -- get --


def test_get_existing(store: ApprovalStore):
    store.create("op-123", policy_version="v1.0")
    record = store.get("op-123")
    assert record is not None
    assert record.op_id == "op-123"


def test_get_missing(store: ApprovalStore):
    assert store.get("nonexistent") is None


# -- expire_stale --


def test_expire_stale_expires_old_pending(store: ApprovalStore):
    store.create("op-old", policy_version="v1.0")
    # Manually backdate the record
    data = json.loads(store._path.read_text(encoding="utf-8"))
    data["op-old"]["created_at"] = time.time() - 7200  # 2 hours ago
    store._atomic_write(data)

    expired = store.expire_stale(timeout_seconds=1800.0)
    assert "op-old" in expired
    record = store.get("op-old")
    assert record is not None
    assert record.state == ApprovalState.EXPIRED


def test_expire_stale_skips_recent(store: ApprovalStore):
    store.create("op-new", policy_version="v1.0")
    expired = store.expire_stale(timeout_seconds=1800.0)
    assert expired == []


# -- persistence --


def test_survives_restart(store: ApprovalStore, tmp_path: Path):
    """Data persists across store instances."""
    store.create("op-persist", policy_version="v1.0")
    store.decide("op-persist", ApprovalState.APPROVED, reason="ok")

    # New instance reads from same path
    store2 = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    record = store2.get("op-persist")
    assert record is not None
    assert record.state == ApprovalState.APPROVED


def test_corrupt_json_returns_empty(store: ApprovalStore):
    """Corrupt file returns None, no crash."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text("not valid json{{{", encoding="utf-8")
    assert store.get("anything") is None


def test_version_field_present(store: ApprovalStore):
    """Store file includes version field for future schema migration."""
    store.create("op-v", policy_version="v1.0")
    data = json.loads(store._path.read_text(encoding="utf-8"))
    assert "_version" in data
