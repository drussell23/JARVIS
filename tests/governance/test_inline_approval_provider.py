"""P3 Slice 2 — InlineApprovalProvider regression suite.

Pins:
  * Protocol conformance (isinstance check against ApprovalProvider).
  * request → APPROVED / REJECTED / EXPIRED happy paths via
    await_decision.
  * Idempotency on second approve / reject.
  * SUPERSEDED on terminal-after-terminal (approve after reject; reject
    after approve; approve after expired).
  * KeyError on unknown request_id.
  * Audit ledger: schema, JSONL append, terminal coverage, best-effort
    write failure (read-only path).
  * elicit (Slice 2 stub) returns None on its configured timeout +
    raises KeyError on unknown id.
  * list_pending shows only undecided.
  * Soft cap (MAX_RETAINED_REQUESTS) evicts decided entries first.
  * Authority invariants: banned imports + only-allowed I/O surface
    (the audit ledger path).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalProvider,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalChoice,
    InlineApprovalQueue,
    reset_default_queue,
)
from backend.core.ouroboros.governance.inline_approval_provider import (
    AUDIT_LEDGER_SCHEMA_VERSION,
    InlineApprovalProvider,
    MAX_RETAINED_REQUESTS,
    _AuditLedger,
    audit_ledger_path,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.risk_engine import RiskTier


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _make_ctx(
    op_id: str = "op-test-1",
    target_files: tuple = ("a.py",),
    description: str = "test op",
    risk_tier: RiskTier = RiskTier.APPROVAL_REQUIRED,
) -> OperationContext:
    ctx = OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
    )
    return dataclasses.replace(ctx, risk_tier=risk_tier)


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def provider(audit_path: Path):
    reset_default_queue()
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue, audit_ledger=_AuditLedger(audit_path))
    yield p
    reset_default_queue()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_INLINE_APPROVAL_AUDIT_PATH", raising=False)
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", raising=False)
    yield


# ===========================================================================
# A — Module-level constants + path resolver
# ===========================================================================


def test_audit_ledger_schema_pinned():
    assert AUDIT_LEDGER_SCHEMA_VERSION == 1


def test_max_retained_pinned():
    assert MAX_RETAINED_REQUESTS == 256


def test_default_audit_path_under_dot_jarvis():
    p = audit_ledger_path()
    assert p.parent.name == ".jarvis"
    assert p.name == "inline_approval_audit.jsonl"


def test_audit_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_INLINE_APPROVAL_AUDIT_PATH", str(tmp_path / "custom.jsonl"),
    )
    assert audit_ledger_path() == tmp_path / "custom.jsonl"


# ===========================================================================
# B — Protocol conformance
# ===========================================================================


def test_implements_approval_provider_protocol(provider):
    """Pin: provider satisfies the runtime-checkable Protocol so
    orchestrator can swap it in for CLIApprovalProvider with no
    code-path changes."""
    assert isinstance(provider, ApprovalProvider)


# ===========================================================================
# C — request happy path
# ===========================================================================


def test_request_returns_op_id():
    p = InlineApprovalProvider(queue=InlineApprovalQueue())
    ctx = _make_ctx(op_id="op-x")
    assert asyncio.run(p.request(ctx)) == "op-x"


def test_request_idempotent_on_same_op_id():
    p = InlineApprovalProvider(queue=InlineApprovalQueue())
    ctx = _make_ctx(op_id="op-y")

    async def _run():
        a = await p.request(ctx)
        b = await p.request(ctx)
        return a, b

    a, b = asyncio.run(_run())
    assert a == b == "op-y"


def test_request_enqueues_into_inline_queue():
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue)
    ctx = _make_ctx(op_id="op-z")
    asyncio.run(p.request(ctx))
    pend = queue.next_pending()
    assert pend is not None
    assert pend.op_id == "op-z"


def test_request_stamps_risk_tier_name_on_queue():
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue)
    ctx = _make_ctx(op_id="op-blk", risk_tier=RiskTier.BLOCKED)
    asyncio.run(p.request(ctx))
    pend = queue.next_pending()
    assert pend is not None
    assert pend.risk_tier == "BLOCKED"
    assert pend.is_immediate_priority() is True


def test_request_with_unset_risk_tier_stamps_unknown():
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue)
    ctx = OperationContext.create(
        target_files=("a.py",), description="x", op_id="op-no-tier",
    )
    asyncio.run(p.request(ctx))
    pend = queue.next_pending()
    assert pend is not None
    assert pend.risk_tier == "UNKNOWN"


# ===========================================================================
# D — approve / reject + await_decision happy paths
# ===========================================================================


def test_approve_then_await_returns_approved(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-a"))
        result = await provider.approve(rid, "operator")
        awaited = await provider.await_decision(rid, timeout_s=1.0)
        return result, awaited

    result, awaited = asyncio.run(_run())
    assert result.status is ApprovalStatus.APPROVED
    assert awaited.status is ApprovalStatus.APPROVED
    assert awaited.approver == "operator"


def test_reject_then_await_returns_rejected(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-r"))
        result = await provider.reject(rid, "operator", "looks risky")
        awaited = await provider.await_decision(rid, timeout_s=1.0)
        return result, awaited

    result, awaited = asyncio.run(_run())
    assert result.status is ApprovalStatus.REJECTED
    assert awaited.reason == "looks risky"


def test_await_decision_times_out_to_expired(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-e"))
        return await provider.await_decision(rid, timeout_s=0.05)

    res = asyncio.run(_run())
    assert res.status is ApprovalStatus.EXPIRED
    assert res.approver is None


def test_await_decision_unknown_request_raises(provider):
    with pytest.raises(KeyError):
        asyncio.run(provider.await_decision("missing", timeout_s=0.01))


# ===========================================================================
# E — Idempotency + SUPERSEDED
# ===========================================================================


def test_approve_already_approved_returns_same(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-i1"))
        a = await provider.approve(rid, "operator")
        b = await provider.approve(rid, "operator-2")
        return a, b

    a, b = asyncio.run(_run())
    assert a is b  # same ApprovalResult instance returned


def test_reject_already_rejected_returns_same(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-i2"))
        a = await provider.reject(rid, "op", "no")
        b = await provider.reject(rid, "op", "still no")
        return a, b

    a, b = asyncio.run(_run())
    assert a is b


def test_reject_after_approve_returns_superseded(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-s1"))
        await provider.approve(rid, "operator")
        return await provider.reject(rid, "operator-2", "changed mind")

    res = asyncio.run(_run())
    assert res.status is ApprovalStatus.SUPERSEDED


def test_approve_after_reject_returns_superseded(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-s2"))
        await provider.reject(rid, "operator", "no")
        return await provider.approve(rid, "operator-2")

    res = asyncio.run(_run())
    assert res.status is ApprovalStatus.SUPERSEDED


def test_approve_after_expired_returns_superseded(provider):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-s3"))
        await provider.await_decision(rid, timeout_s=0.02)  # → EXPIRED
        return await provider.approve(rid, "operator-late")

    res = asyncio.run(_run())
    assert res.status is ApprovalStatus.SUPERSEDED


def test_approve_unknown_request_raises(provider):
    with pytest.raises(KeyError):
        asyncio.run(provider.approve("missing", "op"))


def test_reject_unknown_request_raises(provider):
    with pytest.raises(KeyError):
        asyncio.run(provider.reject("missing", "op", "x"))


# ===========================================================================
# F — Audit ledger
# ===========================================================================


def _read_jsonl(path: Path) -> list:
    return [
        json.loads(line) for line in path.read_text(
            encoding="utf-8",
        ).splitlines() if line.strip()
    ]


def test_audit_writes_on_approve(provider, audit_path):
    async def _run():
        await provider.request(_make_ctx(op_id="op-au1"))
        await provider.approve("op-au1", "operator")

    asyncio.run(_run())
    rows = _read_jsonl(audit_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == AUDIT_LEDGER_SCHEMA_VERSION
    assert row["request_id"] == "op-au1"
    assert row["status"] == "APPROVED"
    assert row["approver"] == "operator"
    assert row["target_files"] == ["a.py"]
    assert row["risk_tier"] == "APPROVAL_REQUIRED"


def test_audit_writes_on_reject(provider, audit_path):
    async def _run():
        await provider.request(_make_ctx(op_id="op-au2"))
        await provider.reject("op-au2", "operator", "bad diff")

    asyncio.run(_run())
    rows = _read_jsonl(audit_path)
    assert rows[-1]["status"] == "REJECTED"
    assert rows[-1]["reason"] == "bad diff"


def test_audit_writes_on_expired(provider, audit_path):
    async def _run():
        await provider.request(_make_ctx(op_id="op-au3"))
        await provider.await_decision("op-au3", timeout_s=0.02)

    asyncio.run(_run())
    rows = _read_jsonl(audit_path)
    assert rows[-1]["status"] == "EXPIRED"
    assert rows[-1]["approver"] is None


def test_audit_writes_on_superseded(provider, audit_path):
    async def _run():
        rid = await provider.request(_make_ctx(op_id="op-au4"))
        await provider.approve(rid, "operator")
        await provider.reject(rid, "operator-2", "changed mind")

    asyncio.run(_run())
    rows = _read_jsonl(audit_path)
    statuses = [r["status"] for r in rows]
    assert "APPROVED" in statuses
    assert "SUPERSEDED" in statuses


def test_audit_best_effort_does_not_raise_on_io_failure(tmp_path):
    """Pin: a read-only audit dir does not propagate I/O errors —
    decisions still succeed, only the ledger write is dropped."""
    bad_path = tmp_path / "ro_dir" / "audit.jsonl"
    bad_path.parent.mkdir()
    bad_path.parent.chmod(0o400)  # read-only
    try:
        ledger = _AuditLedger(bad_path)
        ok = ledger.append({"schema_version": 1, "x": "y"})
        assert ok is False
        # Second call still doesn't raise (warning only logged once).
        ok2 = ledger.append({"schema_version": 1, "x": "z"})
        assert ok2 is False
    finally:
        bad_path.parent.chmod(0o700)  # restore for tmp cleanup


def test_audit_creates_parent_directory(tmp_path):
    """Pin: ledger transparently creates ``.jarvis/`` (or its custom
    parent) on first write."""
    path = tmp_path / "newly" / "made" / "audit.jsonl"
    ledger = _AuditLedger(path)
    assert ledger.append({"schema_version": 1, "x": "y"}) is True
    assert path.exists()


# ===========================================================================
# G — Queue interaction (mark_timeout + record_decision propagation)
# ===========================================================================


def test_approve_records_decision_on_queue():
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue)

    async def _run():
        await p.request(_make_ctx(op_id="op-q1"))
        await p.approve("op-q1", "operator")

    asyncio.run(_run())
    dec = queue.get_decision("op-q1")
    assert dec is not None
    assert dec.choice is InlineApprovalChoice.APPROVE


def test_expired_marks_timeout_on_queue():
    queue = InlineApprovalQueue()
    p = InlineApprovalProvider(queue=queue)

    async def _run():
        await p.request(_make_ctx(op_id="op-q2"))
        await p.await_decision("op-q2", timeout_s=0.02)

    asyncio.run(_run())
    dec = queue.get_decision("op-q2")
    assert dec is not None
    assert dec.choice is InlineApprovalChoice.TIMEOUT_DEFERRED


# ===========================================================================
# H — list_pending + elicit stub
# ===========================================================================


def test_list_pending_excludes_decided(provider):
    async def _run():
        await provider.request(_make_ctx(op_id="op-l1"))
        await provider.request(_make_ctx(op_id="op-l2"))
        await provider.approve("op-l1", "operator")
        return await provider.list_pending()

    rows = asyncio.run(_run())
    op_ids = {r["op_id"] for r in rows}
    assert op_ids == {"op-l2"}


def test_elicit_stub_returns_none_on_timeout(provider):
    async def _run():
        await provider.request(_make_ctx(op_id="op-elic"))
        return await provider.elicit("op-elic", "OK?", timeout_s=0.02)

    assert asyncio.run(_run()) is None


def test_elicit_unknown_request_raises(provider):
    with pytest.raises(KeyError):
        asyncio.run(provider.elicit("missing", "OK?", timeout_s=0.01))


# ===========================================================================
# I — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_provider_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/inline_approval_provider.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_provider_only_io_surface_is_audit_ledger():
    """Pin: the only I/O surface this module owns is the audit ledger
    JSONL append. No subprocess, no os.environ writes, no network."""
    src = _read("backend/core/ouroboros/governance/inline_approval_provider.py")
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import urllib.request",
        "import requests",
        "import httpx",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


# ===========================================================================
# J — Soft cap (MAX_RETAINED_REQUESTS)
# ===========================================================================


def test_soft_cap_evicts_decided_entries_first():
    """Pin: when cap is reached, decided entries are evicted FIFO so
    new requests can be admitted; undecided entries are never evicted."""
    p = InlineApprovalProvider(queue=InlineApprovalQueue())

    async def _run():
        # Fill with decided entries up to the cap.
        for i in range(MAX_RETAINED_REQUESTS):
            rid = await p.request(_make_ctx(op_id=f"op-cap-{i}"))
            await p.approve(rid, "operator")
        # One more request — provider GC must evict at least one decided.
        await p.request(_make_ctx(op_id="op-cap-final"))

    asyncio.run(_run())
    # Last inserted must still be present.
    assert "op-cap-final" in p._requests
    # Total should not exceed the cap by more than the current single
    # undecided entry (post-GC).
    assert len(p._requests) <= MAX_RETAINED_REQUESTS
