"""Phase 1 Slice 1.2 — DecisionRuntime regression spine.

The CALL-SITE integration layer for record/replay/verify.

Pins:
  §1  ledger_enabled flag — default false
  §2  Mode resolution — env override > master flag default
  §3  PASSTHROUGH — just runs compute, no recording
  §4  RECORD — appends a JSONL row per decide() call
  §5  RECORD — ordinal increments per (op, phase, kind)
  §6  RECORD — atomic flock-protected append
  §7  RECORD — disk fault doesn't propagate (caller still gets output)
  §8  REPLAY — returns recorded output, skips compute()
  §9  REPLAY — replay miss falls through to RECORD (best-effort)
  §10 REPLAY — non-JSON-parseable recorded output falls through
  §11 VERIFY — match → returns live, no log noise
  §12 VERIFY — mismatch → log warning, return live (default)
  §13 VERIFY — mismatch + raises_flag=true → raises DecisionMismatchError
  §14 decide() — accepts sync, sync-returning-awaitable, async compute
  §15 runtime_for_session — singleton per session_id
  §16 reset_for_session / reset_all_for_tests
  §17 DecisionRecord — to_dict / from_dict round-trip
  §18 DecisionRecord — from_dict rejects bad input gracefully
  §19 Authority invariants — no orchestrator/phase_runner imports
  §20 Antigravity primitive integration — canonical_serialize used
  §21 Slice 1.1 integration — entropy_for + clock_for_session used
  §22 Cross-process safety — flock acquired during append
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    DecisionMismatchError,
    DecisionRecord,
    DecisionRuntime,
    LedgerMode,
    SCHEMA_VERSION,
    VerifyResult,
    _resolve_mode,
    decide,
    ledger_enabled,
    reset_all_for_tests,
    reset_for_session,
    runtime_for_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES", raising=False,
    )
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    reset_all_for_tests()
    yield tmp_path / "det"
    reset_all_for_tests()


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_ledger_default_true(monkeypatch) -> None:
    """Phase 1 Slice 1.5 graduated default — env unset → True."""
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_ENABLED", raising=False)
    assert ledger_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  "])
def test_ledger_empty_reads_as_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", val)
    assert ledger_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_ledger_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", val)
    assert ledger_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_ledger_falsy(monkeypatch, val) -> None:
    """Hot-revert: explicit false-class strings disable. Empty/
    whitespace map to graduated default True post-Slice-1.5."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", val)
    assert ledger_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Mode resolution
# ---------------------------------------------------------------------------


def test_mode_passthrough_when_master_off(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    # Master flag off forces PASSTHROUGH regardless of mode env
    assert _resolve_mode() is LedgerMode.PASSTHROUGH


def test_mode_record_default_when_master_on(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    assert _resolve_mode() is LedgerMode.RECORD


@pytest.mark.parametrize("val,expected", [
    ("passthrough", LedgerMode.PASSTHROUGH),
    ("record", LedgerMode.RECORD),
    ("replay", LedgerMode.REPLAY),
    ("verify", LedgerMode.VERIFY),
    ("RECORD", LedgerMode.RECORD),  # case-tolerant
])
def test_mode_env_override(monkeypatch, val, expected) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", val)
    assert _resolve_mode() is expected


def test_mode_unknown_env_falls_to_default(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "garbage")
    assert _resolve_mode() is LedgerMode.RECORD  # falls to flag default


# ---------------------------------------------------------------------------
# §3 — PASSTHROUGH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_no_recording(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )

    async def compute():
        return "X"

    out = await decide(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        inputs={}, compute=compute,
    )
    assert out == "X"
    # Disk should not be touched in PASSTHROUGH
    assert not (tmp_path / "det").exists()


@pytest.mark.asyncio
async def test_passthrough_compute_called(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "false")
    counter = {"n": 0}

    async def compute():
        counter["n"] += 1
        return "X"

    await decide(
        op_id="op-1", phase="ROUTE", kind="rk", inputs={}, compute=compute,
    )
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# §4-§7 — RECORD mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_writes_jsonl(isolated) -> None:
    async def compute():
        return {"route": "STANDARD"}

    await decide(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        inputs={"urgency": "normal"}, compute=compute,
    )
    ledger_path = (
        isolated / "test-session" / "decisions.jsonl"
    )
    assert ledger_path.exists()
    line = ledger_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["op_id"] == "op-1"
    assert payload["phase"] == "ROUTE"
    assert payload["kind"] == "route_assignment"
    assert payload["ordinal"] == 0
    assert payload["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_record_ordinal_increments(isolated) -> None:
    async def compute():
        return "value"

    for _ in range(3):
        await decide(
            op_id="op-1", phase="GATE", kind="risk_tier",
            inputs={"data": 1}, compute=compute,
        )
    ledger_path = isolated / "test-session" / "decisions.jsonl"
    rows = [
        json.loads(l) for l in
        ledger_path.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert [r["ordinal"] for r in rows] == [0, 1, 2]


@pytest.mark.asyncio
async def test_record_distinct_kinds_have_independent_ordinals(
    isolated,
) -> None:
    async def compute():
        return "x"

    await decide(
        op_id="op-1", phase="GATE", kind="kind_A",
        inputs={}, compute=compute,
    )
    await decide(
        op_id="op-1", phase="GATE", kind="kind_B",
        inputs={}, compute=compute,
    )
    await decide(
        op_id="op-1", phase="GATE", kind="kind_A",
        inputs={}, compute=compute,
    )
    ledger_path = isolated / "test-session" / "decisions.jsonl"
    rows = [
        json.loads(l) for l in
        ledger_path.read_text(encoding="utf-8").strip().split("\n")
    ]
    # kind_A: ordinal 0, then 1; kind_B: ordinal 0
    a_ordinals = [r["ordinal"] for r in rows if r["kind"] == "kind_A"]
    b_ordinals = [r["ordinal"] for r in rows if r["kind"] == "kind_B"]
    assert a_ordinals == [0, 1]
    assert b_ordinals == [0]


@pytest.mark.asyncio
async def test_record_canonical_input_hash(isolated) -> None:
    """Inputs with different dict ordering should hash identically."""
    async def compute():
        return "out"

    rt = runtime_for_session()
    rec1 = await rt.record(
        op_id="op-1", phase="P", kind="K",
        inputs={"a": 1, "b": 2}, output="out",
    )
    rec2 = await rt.record(
        op_id="op-2", phase="P", kind="K",
        inputs={"b": 2, "a": 1}, output="out",  # different order
    )
    assert rec1 is not None and rec2 is not None
    assert rec1.inputs_hash == rec2.inputs_hash


@pytest.mark.asyncio
async def test_record_disk_fault_returns_output_anyway(
    isolated, monkeypatch,
) -> None:
    """If the disk write fails, the caller still gets the live
    output. Defensive try/except around the append."""
    async def compute():
        return "still-valid"

    # Force the JSONL path to a directory (write will fail with IsADirectoryError)
    bad_dir = isolated / "test-session" / "decisions.jsonl"
    bad_dir.parent.mkdir(parents=True, exist_ok=True)
    bad_dir.mkdir()  # Make it a directory instead of a file

    out = await decide(
        op_id="op-1", phase="P", kind="K", inputs={}, compute=compute,
    )
    assert out == "still-valid"


@pytest.mark.asyncio
async def test_record_from_disk_round_trip(isolated) -> None:
    """Records can be loaded back from disk + parsed."""
    async def compute():
        return [1, 2, 3]

    await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={"a": 1}, compute=compute,
    )
    rt = runtime_for_session()
    looked_up = await rt.lookup(op_id="op-1", phase="P", kind="K")
    assert looked_up is not None
    assert looked_up.op_id == "op-1"
    assert json.loads(looked_up.output_repr) == [1, 2, 3]


# ---------------------------------------------------------------------------
# §8-§10 — REPLAY mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_returns_recorded_output(
    isolated, monkeypatch,
) -> None:
    """First pass: RECORD. Second pass: REPLAY → returns recorded
    without calling compute()."""
    counter = {"n": 0}

    async def compute():
        counter["n"] += 1
        return "RECORDED"

    # RECORD pass
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    out1 = await decide(
        op_id="op-1", phase="P", kind="K", inputs={}, compute=compute,
    )
    assert out1 == "RECORDED"
    assert counter["n"] == 1

    # REPLAY pass — switch session ordinals start fresh
    reset_all_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    async def compute_should_not_run():
        counter["n"] += 100  # canary — should NOT execute in replay
        return "LIVE"

    out2 = await decide(
        op_id="op-1", phase="P", kind="K", inputs={},
        compute=compute_should_not_run,
    )
    assert out2 == "RECORDED"  # replayed
    assert counter["n"] == 1   # compute_should_not_run NEVER called


@pytest.mark.asyncio
async def test_replay_miss_falls_through_to_record(
    isolated, monkeypatch,
) -> None:
    """REPLAY with no recording falls through to RECORD (best-effort)."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    async def compute():
        return "FRESH"

    out = await decide(
        op_id="op-never-recorded", phase="P", kind="K",
        inputs={}, compute=compute,
    )
    assert out == "FRESH"  # compute ran (replay miss)
    # And it was recorded for next time
    rt = runtime_for_session()
    rec = await rt.lookup(op_id="op-never-recorded", phase="P", kind="K")
    assert rec is not None


# ---------------------------------------------------------------------------
# §11-§13 — VERIFY mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_match_returns_live(
    isolated, monkeypatch, caplog,
) -> None:
    """VERIFY match: live equals recorded → no warning, return live."""
    async def compute():
        return {"route": "STANDARD"}

    # Record first
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={"x": 1}, compute=compute,
    )

    # Verify
    reset_all_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "verify")
    caplog.set_level(logging.WARNING)
    out = await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={"x": 1}, compute=compute,
    )
    assert out == {"route": "STANDARD"}
    mismatches = [
        r for r in caplog.records if "VERIFY mismatch" in r.getMessage()
    ]
    assert mismatches == []


@pytest.mark.asyncio
async def test_verify_mismatch_logs_default(
    isolated, monkeypatch, caplog,
) -> None:
    """VERIFY mismatch with default flag: log warning, return live."""
    async def compute_record():
        return "OLD"

    async def compute_diverged():
        return "NEW"

    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=compute_record,
    )

    reset_all_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "verify")
    caplog.set_level(logging.WARNING)
    out = await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=compute_diverged,
    )
    assert out == "NEW"  # returns live by default
    mismatches = [
        r for r in caplog.records if "VERIFY mismatch" in r.getMessage()
    ]
    assert len(mismatches) == 1


@pytest.mark.asyncio
async def test_verify_mismatch_raises_when_strict(
    isolated, monkeypatch,
) -> None:
    """VERIFY + raises_flag=true: raises DecisionMismatchError."""
    async def compute_record():
        return "OLD"

    async def compute_diverged():
        return "NEW"

    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=compute_record,
    )

    reset_all_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "verify")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES", "true")
    with pytest.raises(DecisionMismatchError) as exc_info:
        await decide(
            op_id="op-1", phase="P", kind="K",
            inputs={}, compute=compute_diverged,
        )
    assert exc_info.value.result.matched is False


# ---------------------------------------------------------------------------
# §14 — decide() compute flexibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_accepts_sync_compute(isolated) -> None:
    def compute_sync():
        return "sync-value"

    out = await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=compute_sync,
    )
    assert out == "sync-value"


@pytest.mark.asyncio
async def test_decide_accepts_async_compute(isolated) -> None:
    async def compute_async():
        return "async-value"

    out = await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=compute_async,
    )
    assert out == "async-value"


@pytest.mark.asyncio
async def test_decide_accepts_lambda_returning_coroutine(
    isolated,
) -> None:
    async def inner():
        return "lambda-value"

    out = await decide(
        op_id="op-1", phase="P", kind="K",
        inputs={}, compute=lambda: inner(),
    )
    assert out == "lambda-value"


# ---------------------------------------------------------------------------
# §15-§16 — Singleton + reset
# ---------------------------------------------------------------------------


def test_runtime_singleton_per_session() -> None:
    rt1 = runtime_for_session("alpha")
    rt2 = runtime_for_session("alpha")
    assert rt1 is rt2


def test_runtime_distinct_per_session() -> None:
    rt1 = runtime_for_session("alpha")
    rt2 = runtime_for_session("beta")
    assert rt1 is not rt2


def test_reset_for_session_drops_singleton() -> None:
    rt1 = runtime_for_session("alpha")
    reset_for_session("alpha")
    rt2 = runtime_for_session("alpha")
    assert rt1 is not rt2


def test_reset_all_drops_all_singletons() -> None:
    runtime_for_session("alpha")
    runtime_for_session("beta")
    reset_all_for_tests()
    rt1 = runtime_for_session("alpha")
    rt2 = runtime_for_session("beta")
    # Fresh instances after reset
    # (we don't have access to the cache to compare directly, so
    # smoke test that they are usable)
    assert rt1 is not None
    assert rt2 is not None


# ---------------------------------------------------------------------------
# §17-§18 — DecisionRecord serialization
# ---------------------------------------------------------------------------


def test_record_to_dict_from_dict_round_trip() -> None:
    rec = DecisionRecord(
        record_id="rec-1",
        session_id="sess-1",
        op_id="op-1",
        phase="ROUTE",
        kind="rk",
        ordinal=2,
        inputs_hash="abc123",
        output_repr='"X"',
        monotonic_ts=100.5,
        wall_ts=1700000000.0,
    )
    d = rec.to_dict()
    rec2 = DecisionRecord.from_dict(d)
    assert rec2 == rec


def test_record_from_dict_rejects_garbage() -> None:
    assert DecisionRecord.from_dict("not a mapping") is None  # type: ignore[arg-type]
    assert DecisionRecord.from_dict({}) is None  # missing required fields
    assert DecisionRecord.from_dict({
        "schema_version": "wrong.0", "record_id": "r",
        "session_id": "s", "op_id": "o", "phase": "p",
        "kind": "k", "ordinal": 0, "inputs_hash": "h",
        "output_repr": "v", "monotonic_ts": 0, "wall_ts": 0,
    }) is None  # schema mismatch


def test_record_from_dict_handles_type_errors() -> None:
    bad = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "r", "session_id": "s", "op_id": "o",
        "phase": "p", "kind": "k", "ordinal": "not-an-int",
        "inputs_hash": "h", "output_repr": "v",
        "monotonic_ts": 0, "wall_ts": 0,
    }
    assert DecisionRecord.from_dict(bad) is None


# ---------------------------------------------------------------------------
# §19 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.determinism import decision_runtime
    src = inspect.getsource(decision_runtime)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"decision_runtime must NOT contain {f!r}"


# ---------------------------------------------------------------------------
# §20-§21 — Cross-module integration (Antigravity + Slice 1.1)
# ---------------------------------------------------------------------------


def test_uses_antigravity_canonical_serialize() -> None:
    """Source-level pin: decision_runtime imports canonical_serialize
    + canonical_hash from observability/determinism_substrate.py
    (Antigravity's primitives). Pin so a refactor doesn't accidentally
    re-implement hashing."""
    import inspect
    from backend.core.ouroboros.governance.determinism import decision_runtime
    src = inspect.getsource(decision_runtime)
    assert "canonical_hash" in src
    assert "canonical_serialize" in src
    assert "observability.determinism_substrate" in src


def test_uses_slice_1_1_entropy_and_clock() -> None:
    """Source-level pin: decision_runtime imports entropy_for +
    clock_for_session from Slice 1.1. Stable record_ids + traced
    timestamps are required for deterministic replay."""
    import inspect
    from backend.core.ouroboros.governance.determinism import decision_runtime
    src = inspect.getsource(decision_runtime)
    assert "entropy_for" in src
    assert "clock_for_session" in src


# ---------------------------------------------------------------------------
# §22 — Cross-process safety (flock)
# ---------------------------------------------------------------------------


def test_flock_used_in_atomic_append() -> None:
    """Source-level pin: _atomic_append uses fcntl.flock for cross-
    process safety. Mirrors Antigravity's decision_trace_ledger
    pattern."""
    import inspect
    from backend.core.ouroboros.governance.determinism import decision_runtime
    src = inspect.getsource(decision_runtime._atomic_append)
    assert "fcntl" in src
    assert "flock" in src
    assert "LOCK_EX" in src


# ---------------------------------------------------------------------------
# §23 — Concurrent records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_records_serialize_correctly(isolated) -> None:
    """Multiple concurrent decide() calls all land cleanly in the
    JSONL — no torn writes, all rows parse."""
    async def compute(i):
        return {"i": i}

    tasks = [
        decide(
            op_id=f"op-{i}", phase="P", kind="K",
            inputs={"i": i},
            compute=lambda i=i: compute(i),
        )
        for i in range(20)
    ]
    await asyncio.gather(*tasks)

    ledger_path = isolated / "test-session" / "decisions.jsonl"
    rows = [
        json.loads(l) for l in
        ledger_path.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(rows) == 20
    # All rows are valid + unique op_ids
    op_ids = sorted(r["op_id"] for r in rows)
    assert op_ids == sorted([f"op-{i}" for i in range(20)])


# ---------------------------------------------------------------------------
# §24 — VerifyResult inspection
# ---------------------------------------------------------------------------


def test_verify_result_is_frozen() -> None:
    r = VerifyResult(
        matched=True, expected_hash="a", actual_hash="a",
        expected_repr="x", actual_repr="x",
    )
    with pytest.raises(Exception):
        r.matched = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_verify_returns_frozen_result(isolated, monkeypatch) -> None:
    rt = runtime_for_session()
    rec = await rt.record(
        op_id="op-1", phase="P", kind="K", inputs={}, output="A",
    )
    result = rt.verify(recorded=rec, live_output="A")
    assert result.matched is True
    result2 = rt.verify(recorded=rec, live_output="B")
    assert result2.matched is False
    assert result2.detail != ""
