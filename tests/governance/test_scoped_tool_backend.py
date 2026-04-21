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


def _pctx() -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=Path("/tmp"),
        op_id="op-scoped-test",
        call_id="op-scoped-test:r0:tool",
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
