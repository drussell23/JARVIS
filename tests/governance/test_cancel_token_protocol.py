"""W3(7) Slice 1 — CancelToken primitive protocol tests.

Pins the contracts in `cancel_token.py` per scope doc §4 deterministic
guarantees:
    * idempotent ``set`` (only first commit wins),
    * sync ``is_cancelled`` readability,
    * async ``wait()`` blocks until set,
    * ``race(coro)`` semantics — coro vs cancel,
    * ``get_record()`` returns committed record or None.

Plus the registry's per-op uniqueness + prefix-match contract.

Slice 1 only — these tests do NOT require the orchestrator/PhaseDispatcher
integration (Slice 2). They exercise the primitive standalone.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.cancel_token import (
    CancelOriginEmitter,
    CancelRecord,
    CancelToken,
    CancelTokenRegistry,
    bounded_deadline_s,
    mid_op_cancel_enabled,
    record_persist_enabled,
    repl_immediate_enabled,
)


# ---------------------------------------------------------------------------
# (1) Flag defaults — master-off invariant (scope doc §8)
# ---------------------------------------------------------------------------


def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_MID_OP_CANCEL_ENABLED defaults to false."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    assert mid_op_cancel_enabled() is False


def test_repl_immediate_off_when_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-flag is force-False when master is off, regardless of its own value."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", "true")
    assert repl_immediate_enabled() is False


def test_repl_immediate_on_when_master_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-flag default true when master is on."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", raising=False)
    assert repl_immediate_enabled() is True


def test_record_persist_off_when_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persist sub-flag is force-False when master is off."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", "true")
    assert record_persist_enabled() is False


def test_bounded_deadline_default_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_CANCEL_BOUNDED_DEADLINE_S defaults to 30.0."""
    monkeypatch.delenv("JARVIS_CANCEL_BOUNDED_DEADLINE_S", raising=False)
    assert bounded_deadline_s() == 30.0


def test_bounded_deadline_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CANCEL_BOUNDED_DEADLINE_S", "5.5")
    assert bounded_deadline_s() == 5.5


def test_bounded_deadline_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed env value falls back to 30.0 — never raises."""
    monkeypatch.setenv("JARVIS_CANCEL_BOUNDED_DEADLINE_S", "not-a-number")
    assert bounded_deadline_s() == 30.0


# ---------------------------------------------------------------------------
# (2) CancelToken primitive — idempotent set + sync is_cancelled
# ---------------------------------------------------------------------------


def _make_record(op_id: str = "op-test-001", origin: str = "D:repl_operator") -> CancelRecord:
    return CancelRecord(
        schema_version="cancel.1",
        cancel_id="test-cancel-id",
        op_id=op_id,
        origin=origin,
        phase_at_trigger="GENERATE",
        trigger_monotonic=time.monotonic(),
        trigger_wall_iso="2026-04-25T01:23:45Z",
        bounded_deadline_s=30.0,
        reason="test",
    )


def test_token_starts_uncancelled():
    token = CancelToken("op-test-001")
    assert token.is_cancelled is False
    assert token.get_record() is None
    assert token.op_id == "op-test-001"


def test_token_set_commits_record():
    token = CancelToken("op-test-001")
    record = _make_record()
    assert token.set(record) is True
    assert token.is_cancelled is True
    assert token.get_record() is record


def test_token_set_idempotent_first_wins():
    """Second set() returns False and does NOT overwrite the first record."""
    token = CancelToken("op-test-001")
    first = _make_record(origin="D:repl_operator")
    second = CancelRecord(
        schema_version="cancel.1",
        cancel_id="second-cancel-id",
        op_id="op-test-001",
        origin="E:cost_watchdog",  # would-be racer
        phase_at_trigger="GENERATE",
        trigger_monotonic=time.monotonic(),
        trigger_wall_iso="2026-04-25T01:23:46Z",
        bounded_deadline_s=30.0,
        reason="cost burn",
    )
    assert token.set(first) is True
    assert token.set(second) is False
    # First wins — record identity unchanged
    assert token.get_record() is first
    assert token.get_record().origin == "D:repl_operator"


def test_token_set_rejects_mismatched_op_id():
    """Defensive: token is per-op; foreign records raise."""
    token = CancelToken("op-test-001")
    foreign = _make_record(op_id="op-OTHER-999")
    with pytest.raises(ValueError, match="does not match"):
        token.set(foreign)


# ---------------------------------------------------------------------------
# (3) CancelToken async — wait() + race()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_wait_blocks_until_set() -> None:
    token = CancelToken("op-test-001")
    record = _make_record()

    async def _setter():
        await asyncio.sleep(0.05)
        token.set(record)

    asyncio.create_task(_setter())
    got = await token.wait()
    assert got is record


@pytest.mark.asyncio
async def test_token_wait_returns_immediately_if_already_cancelled() -> None:
    token = CancelToken("op-test-001")
    record = _make_record()
    token.set(record)
    got = await asyncio.wait_for(token.wait(), timeout=0.5)
    assert got is record


@pytest.mark.asyncio
async def test_token_race_coro_wins() -> None:
    """When the racing coroutine completes first, race() returns its result."""
    token = CancelToken("op-test-001")

    async def _work():
        await asyncio.sleep(0.05)
        return "work_result"

    result = await token.race(_work())
    assert result == "work_result"
    assert token.is_cancelled is False


@pytest.mark.asyncio
async def test_token_race_cancel_wins() -> None:
    """When the cancel fires first, race() returns the CancelRecord."""
    token = CancelToken("op-test-001")
    record = _make_record()

    async def _slow_work():
        await asyncio.sleep(2.0)
        return "should_not_complete"

    async def _cancel_after_short_delay():
        await asyncio.sleep(0.05)
        token.set(record)

    asyncio.create_task(_cancel_after_short_delay())
    result = await token.race(_slow_work())
    assert result is record
    assert token.is_cancelled is True


# ---------------------------------------------------------------------------
# (4) CancelTokenRegistry — per-op uniqueness + prefix match
# ---------------------------------------------------------------------------


def test_registry_get_or_create_returns_same_token():
    reg = CancelTokenRegistry()
    a = reg.get_or_create("op-abc-123")
    b = reg.get_or_create("op-abc-123")
    assert a is b


def test_registry_get_returns_none_for_unknown():
    reg = CancelTokenRegistry()
    assert reg.get("op-never-registered") is None


def test_registry_find_by_prefix_unique_match():
    reg = CancelTokenRegistry()
    target = reg.get_or_create("op-019dc1b1-4baa")
    reg.get_or_create("op-019dc1c5-725b")  # distinct op
    assert reg.find_by_prefix("op-019dc1b1") is target


def test_registry_find_by_prefix_no_match_returns_none():
    reg = CancelTokenRegistry()
    reg.get_or_create("op-aaa-111")
    assert reg.find_by_prefix("op-zzz") is None


def test_registry_find_by_prefix_ambiguous_returns_none():
    """Multi-match → caller's UX problem; primitive returns None."""
    reg = CancelTokenRegistry()
    reg.get_or_create("op-abc-111")
    reg.get_or_create("op-abc-222")
    assert reg.find_by_prefix("op-abc") is None


def test_registry_discard_removes_active_keeps_known():
    reg = CancelTokenRegistry()
    reg.get_or_create("op-discard-test")
    assert "op-discard-test" in reg.active_op_ids()
    reg.discard("op-discard-test")
    assert "op-discard-test" not in reg.active_op_ids()
    # Re-create returns a fresh token (not the discarded one)
    fresh = reg.get_or_create("op-discard-test")
    assert fresh.is_cancelled is False


# ---------------------------------------------------------------------------
# (5) CancelOriginEmitter — Class D path (master flag gating + log + persist)
# ---------------------------------------------------------------------------


def test_emit_class_d_returns_none_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master flag off → emit is silent no-op (byte-for-byte pre-W3(7))."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    record = emitter.emit_class_d(
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert record is None
    assert token.is_cancelled is False  # CRITICAL — no record committed


def test_emit_class_d_writes_record_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Master on + sub-flag on → record committed, [CancelOrigin] log emitted."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", "true")
    monkeypatch.delenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", raising=False)

    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()  # no session_dir → log-only
    caplog.set_level("INFO", logger="Ouroboros.CancelToken")

    record = emitter.emit_class_d(
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
        reason="test reason",
    )

    assert record is not None
    assert token.is_cancelled is True
    assert token.get_record() is record
    assert record.origin == "D:repl_operator"
    assert record.phase_at_trigger == "GENERATE"

    msgs = [r.getMessage() for r in caplog.records]
    assert any("[CancelOrigin]" in m and "origin=D:repl_operator" in m for m in msgs)


def test_emit_class_d_persists_to_session_dir_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Persist sub-flag on + session_dir set → cancel_records.jsonl appended."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", "true")

    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter(session_dir=tmp_path)
    record = emitter.emit_class_d(
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert record is not None

    artifact = tmp_path / "cancel_records.jsonl"
    assert artifact.exists()
    line = artifact.read_text(encoding="utf-8").strip()
    import json as _json
    parsed = _json.loads(line)
    assert parsed["schema_version"] == "cancel.1"
    assert parsed["op_id"] == "op-test-001"
    assert parsed["origin"] == "D:repl_operator"


def test_emit_class_d_idempotent_on_double_trigger(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Second trigger on already-cancelled token returns None + emits supersede log."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", raising=False)

    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()

    first = emitter.emit_class_d(
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert first is not None

    caplog.clear()
    caplog.set_level("INFO", logger="Ouroboros.CancelToken")
    second = emitter.emit_class_d(
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert second is None  # idempotent loss
    assert token.get_record() is first  # original record preserved

    msgs = [r.getMessage() for r in caplog.records]
    assert any("superseded" in m.lower() for m in msgs)
