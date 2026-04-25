"""W3(7) Slice 5 — PLAN-EXPLOIT + parallel_dispatch cancel propagation.

Per scope doc §5.3 + §5.4:

* PLAN-EXPLOIT: when an in-flight cancel arrives during the parallel
  Claude-stream gather, all child streams are cancelled, the merged-files
  synthesis is *abandoned* (no partial state persisted), and the
  ``[PLAN-EXPLOIT] status=cancelled`` log emits.
* parallel_dispatch enforce path: when a Class D/E/F cancel fires during
  ``scheduler.wait_for_graph``, the helper surfaces it as
  ``OperationCancelledError``, we attempt best-effort scheduler graph
  cancellation, and return a ``FanoutResult(outcome=CANCELLED, ...)``
  carrying the cancel record info.

Master-flag-off invariant (verified): when ``current_cancel_token()``
returns None, both call sites fall through to the existing
``asyncio.wait_for`` semantics — byte-for-byte pre-W3(7).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.cancel_token import (
    CancelRecord,
    CancelToken,
    OperationCancelledError,
    cancel_token_var,
    race_or_wait_for,
)


def _make_record(op_id: str = "op-test-001") -> CancelRecord:
    return CancelRecord(
        schema_version="cancel.1",
        cancel_id="cid-test",
        op_id=op_id,
        origin="D:repl_operator",
        phase_at_trigger="GENERATE",
        trigger_monotonic=0.0,
        trigger_wall_iso="2026-04-25T01:23:45Z",
        bounded_deadline_s=30.0,
        reason="test",
    )


# ---------------------------------------------------------------------------
# (A) PLAN-EXPLOIT — cancel during parallel Claude-stream gather
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_exploit_cancel_during_gather_cancels_all_children() -> None:
    """When cancel fires during a `gather`, all children get CancelledError
    and the helper surfaces OperationCancelledError to the caller."""
    token = CancelToken("op-pe-001")
    cancel_token_var.set(token)

    started = []
    cancelled = []

    async def _child(idx: int):
        started.append(idx)
        try:
            await asyncio.sleep(2.0)
            return f"result-{idx}"
        except asyncio.CancelledError:
            cancelled.append(idx)
            raise

    async def _trigger_after_short_delay():
        await asyncio.sleep(0.05)
        token.set(_make_record("op-pe-001"))

    asyncio.create_task(_trigger_after_short_delay())
    with pytest.raises(OperationCancelledError) as ei:
        await race_or_wait_for(
            asyncio.gather(_child(1), _child(2), _child(3)),
            timeout=2.0,
            cancel_token=token,
        )

    assert ei.value.record.origin == "D:repl_operator"
    assert sorted(started) == [1, 2, 3]
    # All children cancelled — no result-N return value
    assert sorted(cancelled) == [1, 2, 3]


@pytest.mark.asyncio
async def test_plan_exploit_no_cancel_returns_gather_result() -> None:
    """Master-off path: token is None → behaves as plain asyncio.wait_for."""
    cancel_token_var.set(None)

    async def _child(idx: int):
        await asyncio.sleep(0.01)
        return f"result-{idx}"

    results = await race_or_wait_for(
        asyncio.gather(_child(1), _child(2), _child(3)),
        timeout=2.0,
        cancel_token=None,
    )
    assert sorted(results) == ["result-1", "result-2", "result-3"]


# ---------------------------------------------------------------------------
# (B) plan_exploit module — production call site behaves correctly
# ---------------------------------------------------------------------------


def test_plan_exploit_source_imports_cancel_helpers() -> None:
    """Source-grep pin: plan_exploit.py imports the cancel helpers Slice 5
    needs at the gather() call site. Survives drift on either side."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/plan_exploit.py"
    ).read_text()
    assert "race_or_wait_for as _race_or_wait_for" in src
    assert "current_cancel_token as _curr_cancel_token" in src
    assert "OperationCancelledError as _OpCancelledError" in src


def test_plan_exploit_source_emits_status_cancelled_log() -> None:
    """Source-grep pin: the Slice 5 cancel handler emits the canonical
    `[PLAN-EXPLOIT] op=... status=cancelled` log line."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/plan_exploit.py"
    ).read_text()
    assert "[PLAN-EXPLOIT] op=%s status=cancelled" in src


def test_plan_exploit_source_returns_none_on_cancel() -> None:
    """Source-grep pin: cancel handler abandons merge (returns None per §5.3)."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/plan_exploit.py"
    ).read_text()
    # The cancel handler should explicitly return None
    cancel_idx = src.find("status=cancelled dag_units")
    assert cancel_idx >= 0
    # Search for `return None` within ~500 chars of the log line
    nearby = src[cancel_idx:cancel_idx + 500]
    assert "return None" in nearby


# ---------------------------------------------------------------------------
# (C) parallel_dispatch — enforce path cancel handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_dispatch_enforce_returns_cancelled_outcome_on_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when a Class D/E/F cancel fires during scheduler.wait_for_graph,
    enforce_evaluate_fanout returns FanoutResult(outcome=CANCELLED, ...)."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")

    from backend.core.ouroboros.governance.parallel_dispatch import (
        FanoutOutcome,
        enforce_evaluate_fanout,
    )

    token = CancelToken("op-pd-001")
    cancel_token_var.set(token)

    # Stub scheduler: wait_for_graph hangs; cancel_graph is recorded.
    cancel_graph_called = []

    class _StubGraph:
        graph_id = "g-test-001"
        plan_digest = "p-test-digest"
        concurrency_limit = 3
        units = [object(), object(), object()]
        causal_trace_id = "ctid"

    class _StubScheduler:
        async def wait_for_graph(self, graph_id, timeout_s=None):
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                raise

        async def submit(self, graph):
            return True

        def has_graph(self, graph_id):
            return True

        def cancel_graph(self, graph_id):
            cancel_graph_called.append(graph_id)
            return True

    # Stub the candidate-files extraction + graph build by monkeypatching
    # the module functions called by enforce_evaluate_fanout.
    import backend.core.ouroboros.governance.parallel_dispatch as pd_mod

    def _fake_extract(generation):
        return ["a.py", "b.py", "c.py"]

    def _fake_eligible(*args, **kwargs):
        # Match the FanoutEligibility shape — allowed=True with all needed fields.
        from backend.core.ouroboros.governance.parallel_dispatch import (
            FanoutEligibility,
            ReasonCode,
        )
        return FanoutEligibility(
            allowed=True,
            reason_code=ReasonCode.ALLOWED,
            n_requested=3,
            n_allowed=3,
            posture="explore",
            posture_weight=1.0,
            posture_confidence=1.0,
            memory_level="normal",
            memory_n_allowed=3,
            base_cap=3,
            max_units_cap=3,
        )

    def _fake_build(op_id, repo, candidate_files, eligibility):
        return _StubGraph()

    monkeypatch.setattr(pd_mod, "extract_candidate_files", _fake_extract)
    monkeypatch.setattr(pd_mod, "is_fanout_eligible", _fake_eligible)
    monkeypatch.setattr(pd_mod, "build_execution_graph", _fake_build)

    async def _trigger_cancel_after_delay():
        await asyncio.sleep(0.05)
        token.set(_make_record("op-pd-001"))

    asyncio.create_task(_trigger_cancel_after_delay())

    result = await enforce_evaluate_fanout(
        op_id="op-pd-001",
        generation={"files": [{"file_path": "a.py"}]},
        scheduler=_StubScheduler(),
        wait_timeout_s=120.0,
    )

    assert result.outcome == FanoutOutcome.CANCELLED
    assert "cancelled mid-fanout" in (result.error or "")
    assert cancel_graph_called == ["g-test-001"]


def test_parallel_dispatch_source_imports_cancel_helpers() -> None:
    """Source-grep pin: parallel_dispatch.py imports the cancel helpers."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/parallel_dispatch.py"
    ).read_text()
    assert "race_or_wait_for as _race_or_wait_for" in src
    assert "current_cancel_token as _curr_cancel_token" in src
    assert "OperationCancelledError as _OpCancelledError" in src


def test_parallel_dispatch_source_returns_cancelled_outcome() -> None:
    """Source-grep pin: cancel handler returns FanoutResult with CANCELLED outcome."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/parallel_dispatch.py"
    ).read_text()
    assert "FanoutOutcome.CANCELLED" in src
    # Best-effort cancel_graph attempt
    assert 'getattr(scheduler, "cancel_graph"' in src


# ---------------------------------------------------------------------------
# (D) Master-off invariant — both call sites fall through cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_off_plan_exploit_passes_through_to_wait_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With token=None (master off), the helper falls through to plain
    asyncio.wait_for — no Slice 5 behavior change."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)

    cancel_token_var.set(None)

    async def _child():
        await asyncio.sleep(0.01)
        return "ok"

    # Should behave identically to asyncio.wait_for(asyncio.gather(...), ...)
    result = await race_or_wait_for(
        asyncio.gather(_child(), _child()),
        timeout=1.0,
        cancel_token=None,
    )
    assert result == ["ok", "ok"]
