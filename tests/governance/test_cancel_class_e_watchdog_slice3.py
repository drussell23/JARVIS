"""W3(7) Slice 3 — Class E watchdog cancel hooks.

Operator-binding resolutions in scope (project_wave3_item7_mid_op_cancel_scope.md):

* Resolution-2: Default OFF for ALL new flags. ``JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED``
  defaults False even when master is on.
* Resolution-3: Class E must respect single-terminal + precedence
  (operator/safety > idle watchdog). The Slice 1 idempotent-set semantics
  enforce single-terminal; Slice 3 adds the named watchdog set + supersede log.

Coverage:

A. ``watchdog_enabled()`` flag composition.
B. ``CancelOriginEmitter.emit_class_e`` — gating, allowed-watchdog set,
   record shape (origin=E:<watchdog>), supersede semantics.
C. ``emit_watchdog_cancel`` convenience helper — registry lookup, master-off
   no-op, sub-flag-off no-op.
D. ``CostGovernor`` reference integration — cap-exceeded path emits Class E:cost
   when surface attached + flags on; silent no-op when surface missing.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.cancel_token import (
    CancelOriginEmitter,
    CancelRecord,
    CancelToken,
    CancelTokenRegistry,
    emit_watchdog_cancel,
    mid_op_cancel_enabled,
    watchdog_enabled,
)


# ---------------------------------------------------------------------------
# (A) watchdog_enabled flag composition
# ---------------------------------------------------------------------------


def test_watchdog_flag_default_off_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    # Master off forces sub-flag off regardless
    assert watchdog_enabled() is False


def test_watchdog_flag_default_off_even_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator resolution-2: default OFF even with master on."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", raising=False)
    assert mid_op_cancel_enabled() is True
    assert watchdog_enabled() is False


def test_watchdog_flag_on_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    assert watchdog_enabled() is True


# ---------------------------------------------------------------------------
# (B) emit_class_e — gating + allowed-watchdog set + record shape
# ---------------------------------------------------------------------------


def test_emit_class_e_returns_none_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    result = emitter.emit_class_e(
        watchdog="cost",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert result is None
    assert token.is_cancelled is False


def test_emit_class_e_returns_none_when_subflag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master on but watchdog sub-flag off → no-op."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", raising=False)
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    result = emitter.emit_class_e(
        watchdog="cost",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert result is None
    assert token.is_cancelled is False


def test_emit_class_e_writes_record_when_both_flags_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", raising=False)

    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    caplog.set_level("INFO", logger="Ouroboros.CancelToken")

    record = emitter.emit_class_e(
        watchdog="cost",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
        reason="cap exceeded",
    )

    assert record is not None
    assert record.origin == "E:cost"
    assert record.phase_at_trigger == "GENERATE"
    assert "cap exceeded" in record.reason
    assert token.is_cancelled is True

    msgs = [r.getMessage() for r in caplog.records]
    assert any("[CancelOrigin]" in m and "origin=E:cost" in m for m in msgs)


@pytest.mark.parametrize("watchdog", ["cost", "wall", "productivity", "idle"])
def test_emit_class_e_accepts_canonical_watchdog_names(
    monkeypatch: pytest.MonkeyPatch,
    watchdog: str,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    token = CancelToken(f"op-{watchdog}-001")
    emitter = CancelOriginEmitter()
    record = emitter.emit_class_e(
        watchdog=watchdog,
        op_id=f"op-{watchdog}-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert record is not None
    assert record.origin == f"E:{watchdog}"


def test_emit_class_e_rejects_unknown_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typo guard — unknown watchdog raises ValueError loudly."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    with pytest.raises(ValueError, match="unknown watchdog"):
        emitter.emit_class_e(
            watchdog="cosst",  # typo
            op_id="op-test-001",
            token=token,
            phase_at_trigger="GENERATE",
        )


def test_emit_class_e_supersede_log_when_token_already_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Class E loses to a previous Class D — supersede log emits, no overwrite."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")

    token = CancelToken("op-test-001")
    # Pre-set a Class D record (operator was first)
    d_record = CancelRecord(
        schema_version="cancel.1",
        cancel_id="d-cancel-id",
        op_id="op-test-001",
        origin="D:repl_operator",
        phase_at_trigger="GENERATE",
        trigger_monotonic=0.0,
        trigger_wall_iso="2026-04-25T01:23:45Z",
        bounded_deadline_s=30.0,
        reason="operator first",
    )
    token.set(d_record)

    emitter = CancelOriginEmitter()
    caplog.set_level("INFO", logger="Ouroboros.CancelToken")

    second = emitter.emit_class_e(
        watchdog="idle",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert second is None
    # First-arrival record preserved
    assert token.get_record() is d_record

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "superseded" in m.lower()
        and "requested_origin=E:idle" in m
        and "winner_origin=D:repl_operator" in m
        for m in msgs
    )


# ---------------------------------------------------------------------------
# (C) emit_watchdog_cancel convenience helper
# ---------------------------------------------------------------------------


def test_emit_watchdog_cancel_master_off_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    reg = CancelTokenRegistry()
    result = emit_watchdog_cancel(
        watchdog="cost",
        op_id="op-test-001",
        registry=reg,
    )
    assert result is None
    # Token may have been created by registry but never set
    tok = reg.get("op-test-001")
    if tok is not None:
        assert tok.is_cancelled is False


def test_emit_watchdog_cancel_subflag_off_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", raising=False)
    reg = CancelTokenRegistry()
    result = emit_watchdog_cancel(
        watchdog="cost",
        op_id="op-test-001",
        registry=reg,
    )
    assert result is None


def test_emit_watchdog_cancel_returns_record_with_both_flags_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    reg = CancelTokenRegistry()
    record = emit_watchdog_cancel(
        watchdog="cost",
        op_id="op-helper-001",
        registry=reg,
        phase_at_trigger="VALIDATE",
        reason="test reason",
    )
    assert record is not None
    assert record.origin == "E:cost"
    tok = reg.get("op-helper-001")
    assert tok is not None and tok.is_cancelled


def test_emit_watchdog_cancel_persists_to_session_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", "true")

    reg = CancelTokenRegistry()
    record = emit_watchdog_cancel(
        watchdog="wall",
        op_id="op-persist-001",
        registry=reg,
        session_dir=tmp_path,
    )
    assert record is not None

    artifact = tmp_path / "cancel_records.jsonl"
    assert artifact.exists()
    import json as _json
    parsed = _json.loads(artifact.read_text(encoding="utf-8").strip())
    assert parsed["origin"] == "E:wall"


# ---------------------------------------------------------------------------
# (D) CostGovernor — Class E reference integration
# ---------------------------------------------------------------------------


def test_cost_governor_attach_cancel_surface_idempotent():
    """CostGovernor.attach_cancel_surface is idempotent — re-attach overwrites."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )

    cg = CostGovernor(CostGovernorConfig())
    reg1 = CancelTokenRegistry()
    reg2 = CancelTokenRegistry()
    cg.attach_cancel_surface(registry=reg1)
    assert cg._cancel_token_registry is reg1
    cg.attach_cancel_surface(registry=reg2)
    assert cg._cancel_token_registry is reg2


def test_cost_governor_cap_exceeded_emits_class_e_when_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: charge() trips cap → CostGovernor emits Class E:cost record
    when the cancel surface is attached AND both flags are on."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")

    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )

    cg = CostGovernor(CostGovernorConfig())
    reg = CancelTokenRegistry()
    cg.attach_cancel_surface(registry=reg)

    cg.start("op-cap-test-001", route="standard", complexity="moderate")
    # Force a charge that exceeds cap (cap defaults to ~$0.45 for STANDARD/moderate)
    cg.charge("op-cap-test-001", cost_usd=10.0, provider="claude")

    tok = reg.get("op-cap-test-001")
    assert tok is not None and tok.is_cancelled
    record = tok.get_record()
    assert record is not None
    assert record.origin == "E:cost"
    assert "cost cap exceeded" in record.reason


def test_cost_governor_cap_exceeded_no_op_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master flag off → cap exceeded doesn't emit a cancel record (byte-for-byte
    pre-W3(7) — cost_governor still flips entry.exceeded=True for the
    orchestrator's existing cap-check at line ~3402)."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)

    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )

    cg = CostGovernor(CostGovernorConfig())
    reg = CancelTokenRegistry()
    cg.attach_cancel_surface(registry=reg)

    cg.start("op-master-off-001", route="standard", complexity="moderate")
    cg.charge("op-master-off-001", cost_usd=10.0, provider="claude")

    # Existing exceeded flag still flips
    assert cg.is_exceeded("op-master-off-001") is True
    # But NO Class E cancel record was emitted
    tok = reg.get("op-master-off-001")
    if tok is not None:
        assert tok.is_cancelled is False


def test_cost_governor_cap_exceeded_no_op_when_no_registry_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with both flags on, missing registry → no-op (silent, never raises)."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")

    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )

    cg = CostGovernor(CostGovernorConfig())
    # No attach_cancel_surface — registry stays None

    cg.start("op-noreg-001", route="standard", complexity="moderate")
    # Must NOT raise even with flags on
    cg.charge("op-noreg-001", cost_usd=10.0, provider="claude")
    assert cg.is_exceeded("op-noreg-001") is True
