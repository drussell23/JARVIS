"""Tests for orchestrator tool-execution ledger emission (Task 9).

Verifies that:
1. GenerationResult.with_tool_records() returns a new frozen copy carrying
   the supplied ToolExecutionRecord tuple, leaving the original unchanged.
2. ToolExecutionRecord is dataclasses.asdict()-serialisable and that
   ToolExecStatus.SUCCESS serialises to the string "success".
3. Multiple SANDBOXING records with distinct entry_ids are all written
   (no dedup collision).
4. Without entry_id, the original op+state dedup is preserved.
"""
from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from pathlib import Path

from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.tool_executor import ToolExecStatus, ToolExecutionRecord


def _make_record(op_id: str) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        schema_version="tool.exec.v1",
        op_id=op_id,
        call_id=f"{op_id}:r0:read_file",
        round_index=0,
        tool_name="read_file",
        tool_version="1.0",
        arguments_hash="abc123",
        repo="jarvis",
        policy_decision="allow",
        policy_reason_code="",
        started_at_ns=1_000_000,
        ended_at_ns=2_000_000,
        duration_ms=1.0,
        output_bytes=42,
        error_class=None,
        status=ToolExecStatus.SUCCESS,
    )


def test_generation_result_carries_tool_records() -> None:
    from backend.core.ouroboros.governance.op_context import GenerationResult

    gen = GenerationResult(
        candidates=(
            {
                "candidate_id": "c1",
                "file_path": "f.py",
                "full_content": "x=1\n",
                "rationale": "t",
            },
        ),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
    )
    record = _make_record("op-ledger-001")
    gen2 = gen.with_tool_records((record,))

    assert len(gen2.tool_execution_records) == 1
    assert gen2.tool_execution_records[0].schema_version == "tool.exec.v1"
    # Original must be unchanged (frozen dataclass / replace semantics)
    assert gen.tool_execution_records == ()


def test_tool_exec_record_is_asdict_serializable() -> None:
    record = _make_record("op-serial-test")
    d = dataclasses.asdict(record)

    assert d["schema_version"] == "tool.exec.v1"
    # ToolExecStatus.SUCCESS is a str-enum; asdict preserves the value string
    assert d["status"] == "success"  # ToolExecStatus.SUCCESS.value


def test_multiple_tool_records_have_unique_dedup_keys() -> None:
    """Multiple SANDBOXING records with distinct entry_ids must all be written."""
    async def _run(tmpdir: Path) -> list:
        ledger = OperationLedger(storage_dir=tmpdir)
        entries = [
            LedgerEntry(
                op_id="op-1",
                state=OperationState.SANDBOXING,
                entry_id=f"op-1:r0:tool_{i}",
            )
            for i in range(3)
        ]
        return list(await asyncio.gather(*[ledger.append(e) for e in entries]))

    with tempfile.TemporaryDirectory() as tmpdir:
        results = asyncio.run(_run(Path(tmpdir)))
        assert all(results), "All 3 records should be written (no dedup collision)"


def test_dedup_without_entry_id_still_works() -> None:
    """Without entry_id the original op+state dedup behaviour is preserved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = OperationLedger(storage_dir=Path(tmpdir))
        e = LedgerEntry(op_id="op-2", state=OperationState.SANDBOXING)
        r1 = asyncio.run(ledger.append(e))
        r2 = asyncio.run(ledger.append(e))
        assert r1 is True
        assert r2 is False  # duplicate — same op+state, no entry_id
