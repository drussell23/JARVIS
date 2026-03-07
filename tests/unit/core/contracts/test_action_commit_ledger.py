"""Tests for ActionCommitLedger -- SQLite WAL state machine.

Covers: reserve/commit/abort transitions, duplicate detection,
lease expiry, pre-exec invariant checks, and query filtering.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend")
)

import time

import pytest

from core.contracts.action_commit_ledger import (
    ActionCommitLedger,
    CommitRecord,
    CommitState,
)
from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionSource,
    DecisionType,
    IdempotencyKey,
    OriginComponent,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def ledger(tmp_path):
    db_path = tmp_path / "test_ledger.db"
    lg = ActionCommitLedger(db_path)
    await lg.start()
    yield lg
    await lg.stop()


def _make_envelope(
    envelope_id="env-1",
    trace_id="trace-1",
    decision_type=DecisionType.ACTION,
):
    return DecisionEnvelope(
        envelope_id=envelope_id,
        trace_id=trace_id,
        parent_envelope_id=None,
        decision_type=decision_type,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_RUNNER,
        payload={"message_id": "msg-1"},
        confidence=0.9,
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
        causal_seq=1,
        config_version="v1",
    )


def _make_idem_key(target_id="msg-1"):
    return IdempotencyKey.build(
        DecisionType.ACTION, target_id, "apply_label", "v1"
    )


# ---------------------------------------------------------------------------
# TestReserveCommitAbort
# ---------------------------------------------------------------------------


class TestReserveCommitAbort:
    @pytest.mark.asyncio
    async def test_reserve_returns_commit_id(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        assert isinstance(commit_id, str)
        assert len(commit_id) > 0

    @pytest.mark.asyncio
    async def test_commit_transitions_state(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.commit(commit_id, outcome="success")

        records = await ledger.query(since_epoch=0.0)
        assert len(records) == 1
        assert records[0].state == CommitState.COMMITTED
        assert records[0].outcome == "success"

    @pytest.mark.asyncio
    async def test_abort_transitions_state(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.abort(commit_id, reason="policy denied")

        records = await ledger.query(since_epoch=0.0)
        assert len(records) == 1
        assert records[0].state == CommitState.ABORTED
        assert records[0].abort_reason == "policy denied"

    @pytest.mark.asyncio
    async def test_commit_already_committed_raises(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.commit(commit_id, outcome="success")

        with pytest.raises(ValueError, match="not in RESERVED state"):
            await ledger.commit(commit_id, outcome="again")

    @pytest.mark.asyncio
    async def test_abort_already_committed_raises(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.commit(commit_id, outcome="success")

        with pytest.raises(ValueError, match="not in RESERVED state"):
            await ledger.abort(commit_id, reason="too late")


# ---------------------------------------------------------------------------
# TestDuplicateDetection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    @pytest.mark.asyncio
    async def test_is_duplicate_after_commit(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.commit(commit_id, outcome="done")
        assert await ledger.is_duplicate(idem) is True

    @pytest.mark.asyncio
    async def test_not_duplicate_when_only_reserved(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        assert await ledger.is_duplicate(idem) is False

    @pytest.mark.asyncio
    async def test_not_duplicate_after_abort(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.abort(commit_id, reason="changed mind")
        assert await ledger.is_duplicate(idem) is False


# ---------------------------------------------------------------------------
# TestLeaseExpiry
# ---------------------------------------------------------------------------


class TestLeaseExpiry:
    @pytest.mark.asyncio
    async def test_expire_stale_transitions(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=0.0,
        )
        count = await ledger.expire_stale()
        assert count == 1

        records = await ledger.query(since_epoch=0.0)
        assert len(records) == 1
        assert records[0].state == CommitState.EXPIRED


# ---------------------------------------------------------------------------
# TestPreExecInvariants
# ---------------------------------------------------------------------------


class TestPreExecInvariants:
    @pytest.mark.asyncio
    async def test_valid_invariants_pass(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=42,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(
            commit_id, current_fencing_token=42
        )
        assert ok is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_wrong_fencing_token_fails(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=42,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(
            commit_id, current_fencing_token=99
        )
        assert ok is False
        assert reason is not None
        assert "fencing" in reason.lower()

    @pytest.mark.asyncio
    async def test_expired_lease_fails(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=0.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(
            commit_id, current_fencing_token=1
        )
        assert ok is False
        assert reason is not None
        assert "expired" in reason.lower()

    @pytest.mark.asyncio
    async def test_already_committed_duplicate_fails(self, ledger):
        env1 = _make_envelope(envelope_id="env-1")
        idem = _make_idem_key()

        # First record: reserve and commit
        cid1 = await ledger.reserve(
            envelope=env1,
            action="apply_label",
            target_id="msg-1",
            fencing_token=1,
            lock_owner="worker-1",
            session_id="sess-1",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        await ledger.commit(cid1, outcome="done")

        # Second record: reserve with same idempotency key
        env2 = _make_envelope(envelope_id="env-2")
        cid2 = await ledger.reserve(
            envelope=env2,
            action="apply_label",
            target_id="msg-1",
            fencing_token=2,
            lock_owner="worker-2",
            session_id="sess-2",
            idempotency_key=idem,
            lease_duration_s=60.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(
            cid2, current_fencing_token=2
        )
        assert ok is False
        assert reason is not None
        assert "duplicate" in reason.lower()


# ---------------------------------------------------------------------------
# TestQueryFiltering
# ---------------------------------------------------------------------------


class TestQueryFiltering:
    @pytest.mark.asyncio
    async def test_query_by_decision_type(self, ledger):
        env_action = _make_envelope(
            envelope_id="env-a", decision_type=DecisionType.ACTION
        )
        env_policy = _make_envelope(
            envelope_id="env-p", decision_type=DecisionType.POLICY
        )
        idem_a = _make_idem_key(target_id="msg-a")
        idem_p = _make_idem_key(target_id="msg-p")

        await ledger.reserve(
            envelope=env_action,
            action="apply_label",
            target_id="msg-a",
            fencing_token=1,
            lock_owner="w1",
            session_id="s1",
            idempotency_key=idem_a,
            lease_duration_s=60.0,
        )
        await ledger.reserve(
            envelope=env_policy,
            action="evaluate_policy",
            target_id="msg-p",
            fencing_token=2,
            lock_owner="w2",
            session_id="s2",
            idempotency_key=idem_p,
            lease_duration_s=60.0,
        )

        results = await ledger.query(
            since_epoch=0.0, decision_type=DecisionType.ACTION
        )
        assert len(results) == 1
        assert results[0].decision_type == DecisionType.ACTION

    @pytest.mark.asyncio
    async def test_query_by_state(self, ledger):
        env1 = _make_envelope(envelope_id="env-q1")
        env2 = _make_envelope(envelope_id="env-q2")
        idem1 = _make_idem_key(target_id="msg-q1")
        idem2 = _make_idem_key(target_id="msg-q2")

        cid1 = await ledger.reserve(
            envelope=env1,
            action="a1",
            target_id="msg-q1",
            fencing_token=1,
            lock_owner="w1",
            session_id="s1",
            idempotency_key=idem1,
            lease_duration_s=60.0,
        )
        await ledger.commit(cid1, outcome="ok")

        await ledger.reserve(
            envelope=env2,
            action="a2",
            target_id="msg-q2",
            fencing_token=2,
            lock_owner="w2",
            session_id="s2",
            idempotency_key=idem2,
            lease_duration_s=60.0,
        )

        committed = await ledger.query(
            since_epoch=0.0, state=CommitState.COMMITTED
        )
        assert len(committed) == 1
        assert committed[0].state == CommitState.COMMITTED

        reserved = await ledger.query(
            since_epoch=0.0, state=CommitState.RESERVED
        )
        assert len(reserved) == 1
        assert reserved[0].state == CommitState.RESERVED
