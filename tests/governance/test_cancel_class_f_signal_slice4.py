"""W3(7) Slice 4 — Class F system signal cancel hooks.

Operator resolutions binding (project_wave3_item7_mid_op_cancel_scope.md):

* Resolution-2: Default OFF for ALL new flags. ``JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED``
  defaults False even when master is on.
* Resolution-4: Class F coordinated with documented harness interactions
  but NO dependency on harness epic fixes for correctness — the existing
  harness signal-handler partial-summary write path is *unchanged*; Class F
  is additive observability only.

Coverage:

A. ``signal_enabled()`` flag composition.
B. ``CancelOriginEmitter.emit_class_f`` — gating, allowed-signal set,
   record shape (origin=F:<signal>), supersede semantics.
C. ``emit_signal_cancel`` convenience helper — fans out to all active ops,
   returns count, never raises.
D. Master-off invariant.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cancel_token import (
    CancelOriginEmitter,
    CancelRecord,
    CancelToken,
    CancelTokenRegistry,
    emit_signal_cancel,
    mid_op_cancel_enabled,
    signal_enabled,
)


# ---------------------------------------------------------------------------
# (A) signal_enabled flag composition
# ---------------------------------------------------------------------------


def test_signal_flag_default_off_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    assert signal_enabled() is False


def test_signal_flag_default_off_even_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator resolution-2: default OFF even with master on."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", raising=False)
    assert mid_op_cancel_enabled() is True
    assert signal_enabled() is False


def test_signal_flag_on_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    assert signal_enabled() is True


# ---------------------------------------------------------------------------
# (B) emit_class_f — gating + allowed-signal set + record shape
# ---------------------------------------------------------------------------


def test_emit_class_f_returns_none_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    result = emitter.emit_class_f(
        signal_name="sigterm",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert result is None
    assert token.is_cancelled is False


def test_emit_class_f_returns_none_when_subflag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", raising=False)
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    result = emitter.emit_class_f(
        signal_name="sigterm",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert result is None
    assert token.is_cancelled is False


def test_emit_class_f_writes_record_when_both_flags_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", raising=False)

    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    caplog.set_level("INFO", logger="Ouroboros.CancelToken")

    record = emitter.emit_class_f(
        signal_name="sigterm",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
        reason="container kill",
    )

    assert record is not None
    assert record.origin == "F:sigterm"
    assert record.phase_at_trigger == "GENERATE"
    assert "container kill" in record.reason
    assert token.is_cancelled is True

    msgs = [r.getMessage() for r in caplog.records]
    assert any("[CancelOrigin]" in m and "origin=F:sigterm" in m for m in msgs)


@pytest.mark.parametrize("signal_name", ["sigterm", "sigint", "sighup"])
def test_emit_class_f_accepts_canonical_signal_names(
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    token = CancelToken(f"op-{signal_name}-001")
    emitter = CancelOriginEmitter()
    record = emitter.emit_class_f(
        signal_name=signal_name,
        op_id=f"op-{signal_name}-001",
        token=token,
        phase_at_trigger="VALIDATE",
    )
    assert record is not None
    assert record.origin == f"F:{signal_name}"


def test_emit_class_f_rejects_unknown_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typo guard — unknown signal raises ValueError loudly."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    token = CancelToken("op-test-001")
    emitter = CancelOriginEmitter()
    with pytest.raises(ValueError, match="unknown signal_name"):
        emitter.emit_class_f(
            signal_name="SIGTERM",  # uppercase typo (canonical is lowercase)
            op_id="op-test-001",
            token=token,
            phase_at_trigger="GENERATE",
        )


def test_emit_class_f_supersede_log_when_token_already_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Class F losing to an earlier Class D operator cancel — supersede log fires."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")

    token = CancelToken("op-test-001")
    d_record = CancelRecord(
        schema_version="cancel.1",
        cancel_id="d-cid",
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

    second = emitter.emit_class_f(
        signal_name="sigterm",
        op_id="op-test-001",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert second is None
    assert token.get_record() is d_record  # original preserved

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "superseded" in m.lower()
        and "requested_origin=F:sigterm" in m
        and "winner_origin=D:repl_operator" in m
        for m in msgs
    )


# ---------------------------------------------------------------------------
# (C) emit_signal_cancel convenience helper — fan-out across active ops
# ---------------------------------------------------------------------------


def test_emit_signal_cancel_fans_out_to_all_active_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")

    reg = CancelTokenRegistry()
    op_ids = ["op-fanout-1", "op-fanout-2", "op-fanout-3"]
    for op_id in op_ids:
        reg.get_or_create(op_id)

    emitted = emit_signal_cancel(
        signal_name="sigterm",
        registry=reg,
    )

    assert emitted == 3
    for op_id in op_ids:
        tok = reg.get(op_id)
        assert tok is not None
        assert tok.is_cancelled is True
        rec = tok.get_record()
        assert rec is not None
        assert rec.origin == "F:sigterm"


def test_emit_signal_cancel_returns_zero_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    reg = CancelTokenRegistry()
    reg.get_or_create("op-master-off")

    emitted = emit_signal_cancel(signal_name="sigterm", registry=reg)
    assert emitted == 0
    tok = reg.get("op-master-off")
    assert tok is not None and tok.is_cancelled is False


def test_emit_signal_cancel_returns_zero_when_subflag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", raising=False)
    reg = CancelTokenRegistry()
    reg.get_or_create("op-subflag-off")

    emitted = emit_signal_cancel(signal_name="sigterm", registry=reg)
    assert emitted == 0


def test_emit_signal_cancel_returns_zero_when_no_active_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    reg = CancelTokenRegistry()
    emitted = emit_signal_cancel(signal_name="sigterm", registry=reg)
    assert emitted == 0


def test_emit_signal_cancel_persists_to_session_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", "true")

    reg = CancelTokenRegistry()
    reg.get_or_create("op-persist-1")
    reg.get_or_create("op-persist-2")

    emitted = emit_signal_cancel(
        signal_name="sighup",
        registry=reg,
        session_dir=tmp_path,
    )
    assert emitted == 2

    artifact = tmp_path / "cancel_records.jsonl"
    assert artifact.exists()
    lines = artifact.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    import json as _json
    for line in lines:
        rec = _json.loads(line)
        assert rec["origin"] == "F:sighup"


def test_emit_signal_cancel_skips_already_cancelled_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When some ops are already cancelled (e.g. by Class D before signal),
    emit_signal_cancel still tries them — they return None (supersede),
    not counted in the emitted total."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")

    reg = CancelTokenRegistry()
    tok_d = reg.get_or_create("op-pre-cancelled")
    tok_d.set(CancelRecord(
        schema_version="cancel.1",
        cancel_id="d",
        op_id="op-pre-cancelled",
        origin="D:repl_operator",
        phase_at_trigger="GENERATE",
        trigger_monotonic=0.0,
        trigger_wall_iso="2026-04-25T01:23:45Z",
        bounded_deadline_s=30.0,
        reason="op",
    ))
    reg.get_or_create("op-fresh-1")
    reg.get_or_create("op-fresh-2")

    emitted = emit_signal_cancel(signal_name="sigterm", registry=reg)
    # Only the 2 fresh ops counted; pre-cancelled supersede returned None
    assert emitted == 2

    # Pre-cancelled record preserved (Class D, not Class F)
    assert reg.get("op-pre-cancelled").get_record().origin == "D:repl_operator"


def test_emit_signal_cancel_never_raises_on_registry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt-safe: signal handler must never get a Python exception
    from emit_signal_cancel even when the registry is broken."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")

    class _BrokenRegistry:
        def active_op_ids(self):
            raise RuntimeError("registry broken")

        def get(self, op_id):
            return None

    # Must not raise
    emitted = emit_signal_cancel(
        signal_name="sigterm",
        registry=_BrokenRegistry(),
    )
    assert emitted == 0
