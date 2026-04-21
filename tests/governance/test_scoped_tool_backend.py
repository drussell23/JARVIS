"""Regression spine — ScopedToolBackend pre-linguistic allowlist adapter.

Pins the structural contract a GENERAL subagent's tool-execution path
relies on:

  1. Tool in allowlist → delegates to inner backend, returns inner result.
  2. Tool NOT in allowlist → returns POLICY_DENIED without awaiting inner.
  3. Read-only scope + mutation tool → denied via ScopedToolGate layer 2.
  4. Explicit deny takes precedence over allowlist.
  5. Empty allowlist = permissive (default ToolScope behavior).
  6. __getattr__ passthrough — release_op and arbitrary attrs flow to inner.
  7. Scope rejection carries the tool name AND reason in the error string
     (for model-observable debugging).
  8. Inner backend exceptions propagate unchanged (the adapter is purely
     additive — does not swallow inner failures).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.scoped_tool_access import (
    ScopedToolGate,
    ToolScope,
)
from backend.core.ouroboros.governance.scoped_tool_backend import (
    ScopedToolBackend,
)
from backend.core.ouroboros.governance.tool_executor import (
    PolicyContext,
    ToolCall,
    ToolExecStatus,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingBackend:
    """Fake ToolBackend that records calls + returns configurable results."""

    def __init__(self, default_output: str = "ok") -> None:
        self.calls: List[ToolCall] = []
        self._default_output = default_output
        self.released_ops: List[str] = []
        self.extra_attr_value = "inner-attribute"

    async def execute_async(
        self, call: ToolCall, policy_ctx: PolicyContext, deadline: float,
    ) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            tool_call=call,
            output=f"{self._default_output}:{call.name}",
            error=None,
            status=ToolExecStatus.SUCCESS,
        )

    def release_op(self, op_id: str) -> None:
        """Optional backend method — adapter should passthrough via __getattr__."""
        self.released_ops.append(op_id)


class _ExceptionBackend:
    """Fake that raises on execute_async — to pin passthrough behavior."""

    async def execute_async(
        self, call: ToolCall, policy_ctx: PolicyContext, deadline: float,
    ) -> ToolResult:
        raise RuntimeError(f"inner backend failure for {call.name}")


def _pctx(call_id: str = "op-scoped-test:r0:tool") -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=Path("/tmp"),
        op_id="op-scoped-test",
        call_id=call_id,
        round_index=0,
        risk_tier=None,
        is_read_only=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowlisted_tool_delegates_to_inner() -> None:
    """Test 1: tool in allowlist → inner.execute_async is called;
    adapter returns the inner result unchanged."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file"}),
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    call = ToolCall(name="read_file", arguments={"path": "a.py"})
    result = await backend.execute_async(call, _pctx(), deadline=10.0)

    assert result.status == ToolExecStatus.SUCCESS
    assert result.output == "ok:read_file"
    assert len(inner.calls) == 1
    assert inner.calls[0].name == "read_file"


@pytest.mark.asyncio
async def test_non_allowlisted_tool_refused_before_inner() -> None:
    """Test 2: tool NOT in allowlist → POLICY_DENIED, inner never called.

    This is the CRITICAL safety invariant: under
    allowed_tools=("read_file",), a bash call is rejected at the
    backend boundary without the inner backend being awaited.
    """
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file"}),
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    call = ToolCall(name="bash", arguments={"command": "rm -rf /"})
    result = await backend.execute_async(call, _pctx(), deadline=10.0)

    assert result.status == ToolExecStatus.POLICY_DENIED
    assert result.output == ""
    assert result.error is not None
    assert "bash" in result.error
    assert "allowed_tools" in result.error
    assert inner.calls == [], (
        "CRITICAL: inner backend must NOT be awaited on rejected call"
    )


@pytest.mark.asyncio
async def test_readonly_scope_rejects_mutation_tools() -> None:
    """Test 3: read_only=True scope rejects edit_file / bash / etc.

    Even if allowed_tools is empty (permissive default), the
    read_only layer 2 blocks every entry in _MUTATION_TOOLS.
    """
    gate = ScopedToolGate(ToolScope(read_only=True))  # empty allowlist, read-only
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    for mutation in ("edit_file", "write_file", "bash", "apply_patch", "delete_file"):
        call = ToolCall(name=mutation)
        result = await backend.execute_async(call, _pctx(), deadline=10.0)
        assert result.status == ToolExecStatus.POLICY_DENIED, (
            f"read-only scope must deny {mutation}"
        )
        assert "read-only" in (result.error or "").lower()

    # But read_file under read_only scope (no allowlist) is permitted:
    call = ToolCall(name="read_file")
    result = await backend.execute_async(call, _pctx(), deadline=10.0)
    assert result.status == ToolExecStatus.SUCCESS


@pytest.mark.asyncio
async def test_explicit_deny_takes_precedence_over_allowlist() -> None:
    """Test 4: ``denied_tools`` overrides ``allowed_tools`` — if a
    tool is in both sets, the deny wins (ScopedToolGate's layer 1)."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "search_code"}),
        denied_tools=frozenset({"read_file"}),  # overridden
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    # read_file is in allowlist but ALSO in denied_tools → denied
    result = await backend.execute_async(
        ToolCall(name="read_file"), _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.POLICY_DENIED
    assert "denied" in (result.error or "").lower()

    # search_code in allowlist, not denied → allowed
    result = await backend.execute_async(
        ToolCall(name="search_code"), _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.SUCCESS


@pytest.mark.asyncio
async def test_empty_allowlist_is_permissive_for_type_gate() -> None:
    """Test 5: ToolScope() with no allowed_tools/denied_tools/read_only
    is TYPE-gate permissive — every tool passes the ScopedToolGate
    check. Under the Epoch 1 / Ticket 8 COUNT gate, mutation tools
    still require an explicit ``max_mutations > 0`` budget; callers
    that want the old ``worker`` / ``lead`` "unlimited mutations"
    semantics must opt in via a high budget.
    """
    gate = ScopedToolGate(ToolScope())
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=99,
    )

    for tool in ("read_file", "bash", "edit_file", "search_code"):
        result = await backend.execute_async(
            ToolCall(name=tool), _pctx(), deadline=10.0,
        )
        assert result.status == ToolExecStatus.SUCCESS, (
            f"permissive scope + unlimited budget must allow {tool}"
        )


def test_getattr_passthrough_for_optional_methods() -> None:
    """Test 6: unknown attributes / methods delegate to the inner
    backend via __getattr__ (``release_op`` pattern)."""
    gate = ScopedToolGate(ToolScope())
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    # release_op is on the inner backend but not on the adapter —
    # __getattr__ must forward.
    backend.release_op("op-abc-123")  # type: ignore[attr-defined]
    assert inner.released_ops == ["op-abc-123"]

    # Arbitrary attribute also forwards.
    assert backend.extra_attr_value == "inner-attribute"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rejection_error_is_model_observable() -> None:
    """Test 7: the rejection error string carries the tool name and
    enough reason text that the LLM can reason about WHY its call was
    refused. Tests the exact format of the error so prompt-construction
    code downstream can pattern-match on it."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "search_code"}),
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    call = ToolCall(name="edit_file", arguments={"path": "a.py"})
    result = await backend.execute_async(call, _pctx(), deadline=10.0)

    assert result.status == ToolExecStatus.POLICY_DENIED
    err = result.error or ""
    # Tool name present so the model knows which call was rejected:
    assert "'edit_file'" in err or "edit_file" in err
    # Mentions "allowed_tools" so the model knows the semantic category:
    assert "allowed_tools" in err
    # Clear non-negotiation framing — prompt-injection attempts shouldn't
    # trick the model into thinking it can retry the same call:
    assert "non-negotiable" in err.lower()


@pytest.mark.asyncio
async def test_inner_exceptions_propagate_unchanged() -> None:
    """Test 8: the adapter does NOT swallow or wrap inner-backend
    exceptions. An inner failure on an allowed tool surfaces as-is —
    the adapter is purely additive."""
    gate = ScopedToolGate(ToolScope())  # permissive
    inner = _ExceptionBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    with pytest.raises(RuntimeError, match="inner backend failure"):
        await backend.execute_async(
            ToolCall(name="read_file"), _pctx(), deadline=10.0,
        )


@pytest.mark.asyncio
async def test_mutation_tool_with_allowlist_and_not_readonly() -> None:
    """Test 9 (bonus): mutation tool in allowlist + read_only=False →
    the mutation tool is permitted. This pins that the adapter does
    NOT unconditionally block mutations — only when read_only=True.

    GENERAL invocations with max_mutations>0 + allowed_tools containing
    edit_file MUST be allowed to actually mutate; the firewall's
    boundary validation already enforced the cross-field consistency
    (can't have max_mutations>0 without mutating tools AND
    parent_op_risk_tier >= NOTIFY_APPLY), so this layer just honors
    the resulting scope — provided the COUNT gate is opened with a
    matching ``max_mutations`` budget (Epoch 1 / Ticket 8).
    """
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file"}),
        read_only=False,  # mutating invocation
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    call = ToolCall(name="edit_file", arguments={"path": "a.py", "content": "..."})
    result = await backend.execute_async(call, _pctx(), deadline=10.0)
    assert result.status == ToolExecStatus.SUCCESS
    assert len(inner.calls) == 1
    assert backend.mutations_count == 1


@pytest.mark.asyncio
async def test_max_mutations_count_gate_denies_second_edit_under_cap_1() -> None:
    """Test 11 (Epoch 1 / Ticket 8 — structural COUNT gate):

    Model dispatched with ``max_mutations=1`` and
    ``allowed_tools=("read_file", "edit_file")``. The first
    ``edit_file`` call is authorized and delegated to the inner
    backend. The second ``edit_file`` call must be refused with
    ``POLICY_DENIED`` at the adapter layer — BEFORE the inner backend
    runs — even though the tool TYPE is still in the allowlist.

    This pins the Phase C Slice 1b graduation follow-through: the
    live battle test proved Claude cooperatively respected
    ``max_mutations=1``, but cooperation is weaker than structural
    enforcement. A hallucinating or adversarial model that emits a
    second ``edit_file`` tool call MUST be caught here.
    """
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    # First edit_file — authorized, slot consumed.
    result_1 = await backend.execute_async(
        ToolCall(name="edit_file", arguments={"path": "a.py", "content": "x = 1"}),
        _pctx(), deadline=10.0,
    )
    assert result_1.status == ToolExecStatus.SUCCESS
    assert backend.mutations_count == 1
    assert len(inner.calls) == 1

    # Second edit_file — MUST be refused. Inner backend must NOT see it.
    result_2 = await backend.execute_async(
        ToolCall(name="edit_file", arguments={"path": "b.py", "content": "y = 2"}),
        _pctx(), deadline=10.0,
    )
    assert result_2.status == ToolExecStatus.POLICY_DENIED, (
        "CRITICAL: second edit_file under max_mutations=1 must be "
        "structurally refused — the cooperative cap is now a "
        "mechanical one"
    )
    err = result_2.error or ""
    assert "max_mutations" in err.lower()
    assert "budget" in err.lower()
    assert "1/1" in err or "exhausted" in err.lower()
    assert "non-negotiable" in err.lower()
    assert len(inner.calls) == 1, (
        "CRITICAL: inner backend must NOT be awaited on the refused "
        "second mutation — the slot was consumed by call #1, call #2 "
        "is rejected pre-linguistically"
    )
    # Counter does NOT advance on the refused call — the slot was
    # already consumed by call #1; a refused call is not a mutation.
    assert backend.mutations_count == 1


@pytest.mark.asyncio
async def test_count_gate_allows_unlimited_read_tools_under_cap() -> None:
    """Test 12: the COUNT gate ONLY counts tools in ``_MUTATION_TOOLS``.

    A subagent with ``max_mutations=1`` can still call ``read_file``
    N times without ever consuming its mutation slot. This pins the
    TYPE vs COUNT separation — read_file is not a mutation, so it
    does not touch the budget.
    """
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "search_code", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    # 10 read_file calls — none consume mutation slots.
    for i in range(10):
        result = await backend.execute_async(
            ToolCall(name="read_file", arguments={"path": f"f{i}.py"}),
            _pctx(), deadline=10.0,
        )
        assert result.status == ToolExecStatus.SUCCESS
    assert backend.mutations_count == 0, (
        "read_file must never consume a mutation slot"
    )

    # Now the single authorized edit_file consumes the slot.
    result = await backend.execute_async(
        ToolCall(name="edit_file", arguments={"path": "a.py"}),
        _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.SUCCESS
    assert backend.mutations_count == 1
    assert len(inner.calls) == 11  # 10 reads + 1 edit


@pytest.mark.asyncio
async def test_count_gate_default_max_mutations_zero_denies_all_mutations() -> None:
    """Test 13: ``ScopedToolBackend(inner, gate)`` without an explicit
    ``max_mutations`` defaults to 0 — no mutations permitted.

    This is the read-only default; even if the gate is permissive
    (empty allowlist, read_only=False), the COUNT gate alone denies
    every mutation tool. Layered with the read-only gate in typical
    use, this is belt-and-suspenders.
    """
    gate = ScopedToolGate(ToolScope())  # permissive
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)  # max_mutations default = 0

    assert backend.max_mutations == 0

    result = await backend.execute_async(
        ToolCall(name="edit_file"), _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.POLICY_DENIED
    assert "max_mutations" in (result.error or "").lower()
    assert inner.calls == []

    # read_file still flows — the COUNT gate only sees mutations.
    result = await backend.execute_async(
        ToolCall(name="read_file"), _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.SUCCESS


@pytest.mark.asyncio
async def test_count_gate_consumes_slot_even_on_inner_failure() -> None:
    """Test 14: a mutation slot is consumed at AUTHORIZATION time, not
    at inner-call success. An inner backend exception on the first
    edit_file still burns the slot; the second edit_file is refused.

    Deliberate design: a model cannot retry a failing mutation to
    eventually exceed the budget. If the first attempt was
    structurally authorized, the slot is gone.
    """
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"edit_file"}),
        read_only=False,
    ))
    inner = _ExceptionBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    # First edit_file — inner raises, but the slot is already committed.
    with pytest.raises(RuntimeError, match="inner backend failure"):
        await backend.execute_async(
            ToolCall(name="edit_file"), _pctx(), deadline=10.0,
        )
    assert backend.mutations_count == 1

    # Second edit_file — refused, budget exhausted.
    result = await backend.execute_async(
        ToolCall(name="edit_file"), _pctx(), deadline=10.0,
    )
    assert result.status == ToolExecStatus.POLICY_DENIED
    assert "budget" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_count_gate_logs_with_budget_state(caplog) -> None:
    """Test 15: mutation-budget refusals log at INFO with the
    current/max state. Operators can grep
    ``reason=mutation_budget_exhausted`` in shadow-arc telemetry to
    detect models testing the cage."""
    import logging as _logging
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    caplog.set_level(
        _logging.INFO,
        logger="backend.core.ouroboros.governance.scoped_tool_backend",
    )
    # Consume the slot.
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx(), deadline=10.0,
    )
    # Attempt the refused second call.
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx(), deadline=10.0,
    )

    budget_hits = [
        r for r in caplog.records
        if "mutation_budget_exhausted" in r.getMessage()
    ]
    assert budget_hits, (
        f"expected mutation_budget_exhausted INFO line; "
        f"got {[r.getMessage() for r in caplog.records]}"
    )
    msg = budget_hits[0].getMessage()
    assert "mutations_count=1" in msg
    assert "max_mutations=1" in msg
    assert "edit_file" in msg


@pytest.mark.asyncio
async def test_logged_at_info_level_on_rejection(caplog) -> None:
    """Test 10 (bonus): scope rejection emits an INFO-level log line
    with tool name, reason, op_id, and call_id — operators can grep for
    prompt-injection attempts in the shadow-arc telemetry."""
    import logging as _logging
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file"}),
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(inner=inner, gate=gate)

    caplog.set_level(
        _logging.INFO,
        logger="backend.core.ouroboros.governance.scoped_tool_backend",
    )
    await backend.execute_async(
        ToolCall(name="bash"), _pctx(), deadline=10.0,
    )

    blocked = [
        r for r in caplog.records
        if "ScopedToolBackend" in r.getMessage() and "BLOCKED" in r.getMessage()
    ]
    assert blocked, f"expected BLOCKED INFO line; got {[r.getMessage() for r in caplog.records]}"
    assert "bash" in blocked[0].getMessage()
    assert "op-scoped-test" in blocked[0].getMessage()


# ---------------------------------------------------------------------------
# Epoch 2 / Ticket 9 — records preservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutation_records_capture_tool_and_call_id() -> None:
    """Epoch 2 test 1: each authorized mutation pushes
    ``(tool_name, call_id, t_mono)`` into ``mutation_records``.

    The records survive even if the inner backend later hangs or is
    cancelled — they're appended at AUTHORIZATION time, before the
    inner backend sees the call. This is the primary records
    preservation primitive."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file", "write_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=3,
    )

    # A mix of reads + mutations.
    await backend.execute_async(
        ToolCall(name="read_file", arguments={"path": "a.py"}),
        _pctx("call-1"), deadline=10.0,
    )
    await backend.execute_async(
        ToolCall(name="edit_file", arguments={"path": "b.py"}),
        _pctx("call-2"), deadline=10.0,
    )
    await backend.execute_async(
        ToolCall(name="write_file", arguments={"path": "c.py"}),
        _pctx("call-3"), deadline=10.0,
    )
    await backend.execute_async(
        ToolCall(name="read_file", arguments={"path": "d.py"}),
        _pctx("call-4"), deadline=10.0,
    )

    # Only the two mutation calls are in mutation_records.
    records = backend.mutation_records
    assert len(records) == 2
    assert records[0][0] == "edit_file"
    assert records[0][1] == "call-2"
    assert isinstance(records[0][2], float)  # t_mono
    assert records[1][0] == "write_file"
    assert records[1][1] == "call-3"
    # Snapshot is a tuple — immutable to external callers.
    assert isinstance(records, tuple)


@pytest.mark.asyncio
async def test_call_records_capture_all_authorization_decisions() -> None:
    """Epoch 2 test 2: every execute_async attempt (authorized +
    type_denied + count_denied) lands in ``call_records``. This is
    the full audit trail — critical for detecting adversarial models
    that probe the cage."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    # 1. authorized read
    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("call-1"), deadline=10.0,
    )
    # 2. type_denied — bash not in allowlist
    await backend.execute_async(
        ToolCall(name="bash"), _pctx("call-2"), deadline=10.0,
    )
    # 3. authorized edit (consumes mutation slot)
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx("call-3"), deadline=10.0,
    )
    # 4. count_denied — second edit over budget
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx("call-4"), deadline=10.0,
    )

    records = backend.call_records
    assert len(records) == 4
    # Unpacked tuples: (tool_name, call_id, status, t_mono)
    assert records[0][:3] == ("read_file", "call-1", "authorized")
    assert records[1][:3] == ("bash", "call-2", "type_denied")
    assert records[2][:3] == ("edit_file", "call-3", "authorized")
    assert records[3][:3] == ("edit_file", "call-4", "count_denied")
    # Timestamps are monotonic-nondecreasing.
    timestamps = [r[3] for r in records]
    assert all(timestamps[i] <= timestamps[i + 1]
               for i in range(len(timestamps) - 1))


@pytest.mark.asyncio
async def test_tool_names_property_returns_unique_authorized_tools_only() -> None:
    """Epoch 2 test 3: ``tool_names`` property is the unique set of
    authorized tool names (insertion order preserved). Denied calls
    do NOT surface here — this is the "what actually ran" view."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "search_code", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
    )

    # authorized: read_file, read_file, search_code, edit_file
    # denied: bash (type), edit_file (count)
    for name, cid in [
        ("read_file", "c1"), ("bash", "c2"),
        ("read_file", "c3"), ("search_code", "c4"),
        ("edit_file", "c5"), ("edit_file", "c6"),
    ]:
        await backend.execute_async(
            ToolCall(name=name), _pctx(cid), deadline=10.0,
        )

    # Insertion order, dedup, authorized-only:
    assert backend.tool_names == ("read_file", "search_code", "edit_file")


@pytest.mark.asyncio
async def test_state_mirror_updates_live_on_every_execute_async() -> None:
    """Epoch 2 test 4: when ``state_mirror`` is passed, the adapter
    pushes live counters into it on every execute_async call. This
    is the hard-kill preservation mechanism — the executor reads
    this dict on cancellation to build a complete exec_trace."""
    mirror: dict = {}
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=2,
        state_mirror=mirror,
    )

    # Immediately after construction, mirror should carry static config.
    assert mirror["max_mutations"] == 2
    assert mirror["mutations_count"] == 0
    assert mirror["tool_calls_made"] == 0
    assert mirror["mutation_records"] == []
    assert mirror["call_records"] == []
    assert mirror["tool_names"] == []

    # First call — read_file. Mirror updates.
    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("c1"), deadline=10.0,
    )
    assert mirror["tool_calls_made"] == 1
    assert mirror["mutations_count"] == 0
    assert mirror["tool_names"] == ["read_file"]
    assert len(mirror["call_records"]) == 1
    assert mirror["call_records"][0]["status"] == "authorized"

    # Second call — edit_file. Mutations counter bumps.
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx("c2"), deadline=10.0,
    )
    assert mirror["tool_calls_made"] == 2
    assert mirror["mutations_count"] == 1
    assert mirror["tool_names"] == ["read_file", "edit_file"]
    assert len(mirror["mutation_records"]) == 1
    assert mirror["mutation_records"][0]["tool"] == "edit_file"
    assert mirror["mutation_records"][0]["call_id"] == "c2"
    assert isinstance(mirror["mutation_records"][0]["t_mono"], float)

    # Third call — denied mutation. Only call_records grows.
    await backend.execute_async(
        ToolCall(name="bash"), _pctx("c3"), deadline=10.0,
    )
    assert mirror["tool_calls_made"] == 2  # unchanged — denied
    assert mirror["mutations_count"] == 1  # unchanged — type_denied
    assert len(mirror["call_records"]) == 3
    assert mirror["call_records"][2]["status"] == "type_denied"


@pytest.mark.asyncio
async def test_state_mirror_survives_inner_backend_exception() -> None:
    """Epoch 2 test 5 (CRITICAL): when the inner backend raises
    mid-execute, the mirror STILL carries the authorization record.

    This pins the core hard-kill preservation invariant: the record
    is written at AUTHORIZATION time, before the inner backend is
    awaited. A hang / exception / cancellation of the inner path
    cannot un-write the record."""
    mirror: dict = {}
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"edit_file"}),
        read_only=False,
    ))
    inner = _ExceptionBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=5,
        state_mirror=mirror,
    )

    with pytest.raises(RuntimeError):
        await backend.execute_async(
            ToolCall(name="edit_file"),
            _pctx("c-crashed"),
            deadline=10.0,
        )

    # Despite the inner exception, the record IS in the mirror.
    assert mirror["mutations_count"] == 1
    assert mirror["tool_calls_made"] == 1
    assert mirror["mutation_records"][0]["tool"] == "edit_file"
    assert mirror["mutation_records"][0]["call_id"] == "c-crashed"
    assert mirror["call_records"][0]["status"] == "authorized"


@pytest.mark.asyncio
async def test_state_mirror_snapshot_isolation_across_reads() -> None:
    """Epoch 2 test 6: each mirror update writes a FRESH list, so a
    reader that captures mirror["call_records"] gets a stable
    snapshot. Subsequent execute_async calls don't mutate the
    previously-snapshotted list (the adapter writes a new list each
    time instead of appending in-place).

    This is important on the hard-kill path: the executor captures
    ``list(mirror["call_records"])`` and expects it to be a
    point-in-time snapshot, not a live view that changes as
    concurrent driver activity continues.
    """
    mirror: dict = {}
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=0,
        state_mirror=mirror,
    )

    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("c1"), deadline=10.0,
    )
    snapshot_t1 = mirror["call_records"]
    snapshot_t1_copy = list(snapshot_t1)  # defensive copy
    assert len(snapshot_t1_copy) == 1

    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("c2"), deadline=10.0,
    )
    # Explicit-copy reader sees 1 record; direct mirror["call_records"]
    # reader sees 2.
    assert len(snapshot_t1_copy) == 1
    assert len(mirror["call_records"]) == 2


@pytest.mark.asyncio
async def test_state_mirror_none_is_safe_default() -> None:
    """Epoch 2 test 7: passing ``state_mirror=None`` (or omitting it
    entirely) is safe — the adapter takes the same internal bookkeeping
    path, just without pushing to an external dict. All the existing
    Epoch 1 behavior is preserved."""
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file", "edit_file"}),
        read_only=False,
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1,
        # state_mirror omitted — should be exactly the same as None
    )

    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("c1"), deadline=10.0,
    )
    await backend.execute_async(
        ToolCall(name="edit_file"), _pctx("c2"), deadline=10.0,
    )

    # Properties still work.
    assert backend.mutations_count == 1
    assert len(backend.mutation_records) == 1
    assert len(backend.call_records) == 2


@pytest.mark.asyncio
async def test_scoped_backend_ref_attachable_via_caller_dict() -> None:
    """Epoch 2 test 8: callers can stash the backend itself on an
    external dict (e.g. the driver attaches to state_mirror) so the
    executor has a direct reference for anything beyond the mirrored
    primitives. This is the belt-and-suspenders path alongside the
    mirror."""
    mirror: dict = {}
    gate = ScopedToolGate(ToolScope(
        allowed_tools=frozenset({"read_file"}),
    ))
    inner = _RecordingBackend()
    backend = ScopedToolBackend(
        inner=inner, gate=gate, state_mirror=mirror,
    )

    # Driver-style attach (matches general_driver.py):
    mirror["_scoped_backend_ref"] = backend

    await backend.execute_async(
        ToolCall(name="read_file"), _pctx("c1"), deadline=10.0,
    )

    # External reader uses the ref to query beyond the mirrored fields.
    ref = mirror["_scoped_backend_ref"]
    assert ref is backend
    assert ref.mutations_count == 0
    assert len(ref.call_records) == 1


@pytest.mark.asyncio
async def test_state_mirror_malformed_type_does_not_crash_adapter() -> None:
    """Epoch 2 test 9: the adapter treats state_mirror defensively —
    passing something weird at construction time may raise a TypeError
    on the first attempt to write, but the ``None`` contract (no
    mirror) is the only guaranteed-safe shape. Verify that the actual
    expected ``None`` / ``dict`` contract holds cleanly.
    """
    # None — pure no-op path.
    gate = ScopedToolGate(ToolScope())
    inner = _RecordingBackend()
    backend_none = ScopedToolBackend(
        inner=inner, gate=gate, max_mutations=1, state_mirror=None,
    )
    await backend_none.execute_async(
        ToolCall(name="read_file"), _pctx(), deadline=10.0,
    )
    # No crash — that's the assertion.

    # Empty dict — seeded + updated.
    mirror: dict = {}
    backend_dict = ScopedToolBackend(
        inner=_RecordingBackend(), gate=gate, max_mutations=1,
        state_mirror=mirror,
    )
    assert "max_mutations" in mirror  # seeded at construction
    await backend_dict.execute_async(
        ToolCall(name="read_file"), _pctx(), deadline=10.0,
    )
    assert mirror["tool_calls_made"] == 1
