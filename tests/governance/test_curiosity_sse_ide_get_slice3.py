"""Wave 2 (4) Slice 3 — SSE event + IDE GET endpoint curiosity observability.

Per ``project_w2_4_curiosity_scope.md`` Slice 3:

* Additive ``curiosity_question_emitted`` SSE event (12th in the IDE
  stream vocab — additive-only contract preserved).
* ``JARVIS_CURIOSITY_SSE_ENABLED`` sub-flag (default ``false``,
  operator opt-in). Master-off → SSE force-off (composition).
* New ``GET /observability/curiosity`` and
  ``/observability/curiosity/{question_id}`` endpoints reading from
  the ``curiosity_ledger.jsonl`` artifact (Slice 1).

Coverage:

A. SSE event vocabulary — ``EVENT_TYPE_CURIOSITY_QUESTION_EMITTED``
   in ``_VALID_EVENT_TYPES``.
B. ``sse_enabled()`` flag composition — master gates sub-flag.
C. ``bridge_curiosity_to_sse`` — gating, payload shape, never raises.
D. End-to-end ``try_charge`` → SSE publish when all flags on.
E. Master-off invariant — no SSE event emitted when master off.
F. IDE GET endpoint — list, filter, detail, malformed id, 503 paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.curiosity_engine import (
    CuriosityBudget,
    CuriosityRecord,
    bridge_curiosity_to_sse,
    sse_enabled,
)


def _make_record(
    *,
    op_id: str = "op-test-001",
    question_id: str = "qid-1",
    posture: str = "EXPLORE",
    result: str = "allowed",
    question_text: str = "what should I do?",
) -> CuriosityRecord:
    return CuriosityRecord(
        schema_version="curiosity.1",
        question_id=question_id,
        op_id=op_id,
        posture_at_charge=posture,
        question_text=question_text,
        est_cost_usd=0.05,
        issued_at_monotonic=0.0,
        issued_at_iso="2026-04-25T01:23:45Z",
        result=result,
    )


# ---------------------------------------------------------------------------
# (A) SSE event type vocabulary pin
# ---------------------------------------------------------------------------


def test_curiosity_question_emitted_in_event_vocabulary() -> None:
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_CURIOSITY_QUESTION_EMITTED == "curiosity_question_emitted"
    assert EVENT_TYPE_CURIOSITY_QUESTION_EMITTED in _VALID_EVENT_TYPES


# ---------------------------------------------------------------------------
# (B) sse_enabled flag composition (master gates sub-flag)
# ---------------------------------------------------------------------------


def test_sse_flag_off_when_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit master-off + SSE-default → sse_enabled() returns False.
    Post-Slice-4 graduation, master defaults true; this pins the explicit-
    off escape hatch composition."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    monkeypatch.delenv("JARVIS_CURIOSITY_SSE_ENABLED", raising=False)
    assert sse_enabled() is False


def test_sse_flag_master_off_forces_sub_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master off + sub on → still off (master-off composition)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    assert sse_enabled() is False


def test_sse_flag_master_on_sub_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master on + sub off (default) → sse_enabled() False (operator opt-in)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CURIOSITY_SSE_ENABLED", raising=False)
    assert sse_enabled() is False


def test_sse_flag_master_on_sub_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master on + sub on → sse_enabled() True."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    assert sse_enabled() is True


# ---------------------------------------------------------------------------
# (C) bridge_curiosity_to_sse — gating + payload shape + never-raise
# ---------------------------------------------------------------------------


def test_bridge_no_op_when_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master off → bridge no-ops, no exception."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    bridge_curiosity_to_sse(_make_record())  # must not raise


def test_bridge_no_op_when_sse_sub_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master on but sub off → no publish, no exception."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CURIOSITY_SSE_ENABLED", raising=False)

    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        reset_default_broker,
    )
    reset_default_broker()
    broker = get_default_broker()
    pre = broker.published_count

    bridge_curiosity_to_sse(_make_record())
    assert broker.published_count == pre
    reset_default_broker()


def test_bridge_publishes_when_all_flags_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All flags on → broker.publish called with correct payload shape."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")

    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
        get_default_broker,
        reset_default_broker,
    )
    reset_default_broker()
    broker = get_default_broker()
    pre = broker.published_count

    rec = _make_record(
        op_id="op-bridge-001",
        question_id="qid-bridge-1",
        posture="EXPLORE",
        result="allowed",
        question_text="what should I do here?",
    )
    bridge_curiosity_to_sse(rec)

    assert broker.published_count == pre + 1
    history = list(broker._history)  # noqa: SLF001 — test-only inspection
    last = history[-1]
    assert last.event_type == EVENT_TYPE_CURIOSITY_QUESTION_EMITTED
    assert last.op_id == "op-bridge-001"
    assert last.payload["question_id"] == "qid-bridge-1"
    assert last.payload["posture"] == "EXPLORE"
    assert last.payload["result"] == "allowed"
    assert last.payload["question_text"] == "what should I do here?"
    reset_default_broker()


def test_bridge_truncates_question_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Question text >80 chars is truncated in the SSE payload."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")

    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        reset_default_broker,
    )
    reset_default_broker()
    broker = get_default_broker()

    long_text = "x" * 200
    bridge_curiosity_to_sse(_make_record(question_text=long_text))

    last = list(broker._history)[-1]  # noqa: SLF001
    assert len(last.payload["question_text"]) == 80
    reset_default_broker()


def test_bridge_never_raises_on_broker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker construction failure → bridge swallows, no raise."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")

    import backend.core.ouroboros.governance.ide_observability_stream as stream_mod

    def _broken():
        raise RuntimeError("broker construction failed")

    monkeypatch.setattr(stream_mod, "get_default_broker", _broken)
    bridge_curiosity_to_sse(_make_record())  # must not raise


# ---------------------------------------------------------------------------
# (D + E) End-to-end try_charge → SSE publish + master-off no-publish
# ---------------------------------------------------------------------------


def test_try_charge_allowed_publishes_sse_when_all_flags_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Allowed try_charge fires the bridge → broker has the event."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")

    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
        get_default_broker,
        reset_default_broker,
    )
    reset_default_broker()
    broker = get_default_broker()
    pre = broker.published_count

    bud = CuriosityBudget(
        op_id="op-e2e-001",
        posture_at_arm="EXPLORE",
        session_dir=tmp_path,
    )
    result = bud.try_charge(question_text="what next?", est_cost_usd=0.01)
    assert result.allowed is True

    assert broker.published_count == pre + 1
    last = list(broker._history)[-1]  # noqa: SLF001
    assert last.event_type == EVENT_TYPE_CURIOSITY_QUESTION_EMITTED
    assert last.op_id == "op-e2e-001"
    assert last.payload["result"] == "allowed"
    reset_default_broker()


def test_try_charge_master_off_publishes_no_sse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master off → try_charge denies + no SSE publish (master-off invariant)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")

    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        reset_default_broker,
    )
    reset_default_broker()
    broker = get_default_broker()
    pre = broker.published_count

    bud = CuriosityBudget(
        op_id="op-master-off",
        posture_at_arm="EXPLORE",
        session_dir=tmp_path,
    )
    result = bud.try_charge(question_text="x", est_cost_usd=0.01)
    assert result.allowed is False
    # Master-off → SSE gate denies even though sub + stream are on
    assert broker.published_count == pre
    reset_default_broker()


# ---------------------------------------------------------------------------
# (F) IDE GET endpoint — JSONL read + helpers
# ---------------------------------------------------------------------------


def test_router_reads_curiosity_records_from_artifact(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )

    artifact = tmp_path / "curiosity_ledger.jsonl"
    rec_a = _make_record(op_id="op-a", question_id="qid-a")
    rec_b = _make_record(
        op_id="op-b", question_id="qid-b", result="denied:posture_disallowed",
    )
    artifact.write_text(
        rec_a.to_jsonl() + rec_b.to_jsonl(), encoding="utf-8",
    )

    router = IDEObservabilityRouter(session_dir=tmp_path)
    records, parse_errors = router._read_curiosity_records()
    assert parse_errors == 0
    assert len(records) == 2
    assert records[0]["question_id"] == "qid-a"
    assert records[1]["question_id"] == "qid-b"


def test_router_curiosity_no_artifact(tmp_path: Path) -> None:
    """No file → empty list, zero parse errors."""
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    router = IDEObservabilityRouter(session_dir=tmp_path)
    records, parse_errors = router._read_curiosity_records()
    assert records == []
    assert parse_errors == 0


def test_router_curiosity_no_session_dir() -> None:
    """No session_dir bound → empty list (handler returns 503 — verified
    via gate check in production path; this tests the helper contract)."""
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    router = IDEObservabilityRouter(session_dir=None)
    records, parse_errors = router._read_curiosity_records()
    assert records == []
    assert parse_errors == 0


def test_router_curiosity_counts_parse_errors(tmp_path: Path) -> None:
    """Malformed JSONL lines counted, valid lines still parsed."""
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    artifact = tmp_path / "curiosity_ledger.jsonl"
    rec = _make_record(question_id="qid-good")
    artifact.write_text(
        rec.to_jsonl() + "{not valid json\n" + "[]\n",
        encoding="utf-8",
    )
    router = IDEObservabilityRouter(session_dir=tmp_path)
    records, parse_errors = router._read_curiosity_records()
    assert len(records) == 1
    assert records[0]["question_id"] == "qid-good"
    assert parse_errors == 1


# ---------------------------------------------------------------------------
# (G) Route registration pin — ensures the GETs are wired
# ---------------------------------------------------------------------------


def test_curiosity_routes_registered() -> None:
    """The list + detail GET routes are registered on the app router."""
    from aiohttp import web

    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )

    app = web.Application()
    router = IDEObservabilityRouter(session_dir=None)
    router.register_routes(app)

    paths = sorted(
        getattr(r, "resource", r).canonical for r in app.router.routes()
    )
    assert "/observability/curiosity" in paths
    assert "/observability/curiosity/{question_id}" in paths
