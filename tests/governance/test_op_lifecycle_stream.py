"""Regression spine — B.2.0.5 operation-FSM lifecycle SSE.

Closes the missing infrastructure piece exposed during SWE-Bench-Pro
Phase B.2.2 substrate scoping: the orchestrator did NOT publish
operation-FSM terminal events to the SSE broker. ``task_completed`` /
``task_cancelled`` are TaskBoard-scoped (tool-call boards owned by
``task_tool.py``), NOT operation-lifecycle terminals. Without this
substrate, the operator's "primary path: SSE subscribe by op_id, await
documented terminal event types" rendezvous pattern can't actually
work for op-lifecycle consumers — they would hang.

Structural fix (single-seam, composes existing surfaces only):
``orchestrator._record_ledger`` fans out a best-effort
``publish_operation_terminal(ctx, state)`` call AFTER a successful
``ledger.append()`` returns True. Idempotency rides on the ledger's
existing (op_id, state) dedup key — duplicate appends suppress the
publish naturally.

Spine invariants
----------------

  1. Master flag OFF (default-FALSE) → byte-identical pre-B.2.0.5; no
     ``operation_terminal`` events on the broker.
  2. Master flag ON + terminal state → exactly one event published
     with the full bounded payload (op_id / phase / state /
     terminal_reason_code / phase_entered_at / timestamp).
  3. Non-terminal state (sandboxing, gating, applying, ...) → no
     publish even with master flag ON.
  4. Idempotency: ledger.append's duplicate detection suppresses the
     follow-up publish call (no second event fires for the same
     (op_id, state) pair).
  5. Single-seam: AST pin — ``publish_operation_terminal`` is
     referenced exactly once in orchestrator.py, inside
     ``_record_ledger``. Drift here is a duplicate-publish bug.
  6. Never-raise discipline: AST pin — the lazy-import + call site in
     _record_ledger is wrapped in try/except.
  7. Ledger-before-publish: AST pin — within _record_ledger, the
     ``ledger.append`` call appears positionally BEFORE the
     ``publish_operation_terminal`` invocation.
  8. Closed taxonomy: TERMINAL_OPERATION_STATES = {applied,
     rolled_back, failed, blocked}. AST pin asserts the frozenset
     literal matches exactly — drift is a coverage bug (e.g. missing
     "blocked" would silently drop advisor_blocked terminals).
  9. Subscriber-side: a broker subscriber filtered by op_id receives
     the terminal event when published via the canonical path.
 10. FlagRegistry seed registered (default-FALSE, BOOL/SAFETY).
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_OPERATION_TERMINAL,
    OP_LIFECYCLE_SSE_ENABLED_ENV_VAR,
    TERMINAL_OPERATION_STATES,
    _VALID_EVENT_TYPES,
    get_default_broker,
    op_lifecycle_sse_enabled,
    publish_operation_terminal,
    register_flags,
    reset_default_broker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _DuckPhase:
    def __init__(self, name: str) -> None:
        self.name = name


class _DuckState:
    def __init__(self, value: str) -> None:
        self.value = value


class _DuckCtx:
    """Duck-typed OperationContext stand-in for substrate tests."""

    def __init__(
        self,
        op_id: str = "op-test-001",
        phase: str = "COMPLETE",
        terminal_reason_code: str = "",
    ) -> None:
        self.op_id = op_id
        self.phase = _DuckPhase(phase)
        self.phase_entered_at = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
        self.terminal_reason_code = terminal_reason_code


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, raising=False)


@pytest.fixture
def fresh_broker() -> Iterator[Any]:
    """Reset the singleton broker per-test so subscriber state doesn't
    leak across cases."""
    reset_default_broker()
    yield get_default_broker()
    reset_default_broker()


# ---------------------------------------------------------------------------
# 1. Master-flag-off byte-identical
# ---------------------------------------------------------------------------


def test_master_flag_default_false(clean_env: None) -> None:
    assert op_lifecycle_sse_enabled() is False


def test_master_flag_false_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "false")
    assert op_lifecycle_sse_enabled() is False


def test_publish_returns_none_when_master_off(
    clean_env: None, fresh_broker: Any,
) -> None:
    """No event reaches the broker when the master flag is unset."""
    result = publish_operation_terminal(_DuckCtx(), _DuckState("applied"))
    assert result is None
    # Broker history should not contain any operation_terminal events.
    history = fresh_broker.recent_history(
        limit=50, event_type=EVENT_TYPE_OPERATION_TERMINAL,
    )
    assert history == []


# ---------------------------------------------------------------------------
# 2. Terminal state with master on → exactly one event with bounded payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state_value", sorted(TERMINAL_OPERATION_STATES))
def test_publish_fires_for_each_terminal_state(
    state_value: str,
    monkeypatch: pytest.MonkeyPatch,
    fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    ctx = _DuckCtx(op_id=f"op-test-{state_value}", phase="COMPLETE",
                   terminal_reason_code=f"reason_for_{state_value}")
    event_id = publish_operation_terminal(ctx, _DuckState(state_value))
    assert event_id is not None
    history = fresh_broker.recent_history(
        limit=50, event_type=EVENT_TYPE_OPERATION_TERMINAL,
        op_id=f"op-test-{state_value}",
    )
    assert len(history) == 1
    payload = history[0].payload
    assert payload["op_id"] == f"op-test-{state_value}"
    assert payload["state"] == state_value
    assert payload["phase"] == "COMPLETE"
    assert payload["terminal_reason_code"] == f"reason_for_{state_value}"
    assert payload["phase_entered_at"].startswith("2026-05-12T")
    assert payload["timestamp"]  # ISO8601 non-empty


def test_payload_bounded_terminal_reason_code(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    """Operator binding: bounded payload — terminal_reason_code capped."""
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    long_reason = "x" * 1000
    ctx = _DuckCtx(terminal_reason_code=long_reason)
    publish_operation_terminal(ctx, _DuckState("failed"))
    history = fresh_broker.recent_history(
        limit=1, event_type=EVENT_TYPE_OPERATION_TERMINAL,
    )
    assert len(history) == 1
    assert len(history[0].payload["terminal_reason_code"]) <= 256


# ---------------------------------------------------------------------------
# 3. Non-terminal states do NOT publish
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_terminal", [
    "planned", "sandboxing", "validating", "gating", "applying",
    "budget_checkpoint", "iteration_outcome", "tier0_complete",
])
def test_non_terminal_state_does_not_publish(
    non_terminal: str,
    monkeypatch: pytest.MonkeyPatch,
    fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    result = publish_operation_terminal(_DuckCtx(), _DuckState(non_terminal))
    assert result is None
    history = fresh_broker.recent_history(
        limit=50, event_type=EVENT_TYPE_OPERATION_TERMINAL,
    )
    assert history == []


# ---------------------------------------------------------------------------
# 4. Malformed inputs handled gracefully (NEVER raises)
# ---------------------------------------------------------------------------


def test_publish_handles_missing_op_id(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    ctx = _DuckCtx(op_id="")
    assert publish_operation_terminal(ctx, _DuckState("applied")) is None


def test_publish_handles_non_string_state_value(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")

    class _BadState:
        value = 42

    assert publish_operation_terminal(_DuckCtx(), _BadState()) is None


def test_publish_handles_missing_state_attribute(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")

    class _NoValue:
        pass

    assert publish_operation_terminal(_DuckCtx(), _NoValue()) is None


def test_publish_handles_none_ctx_phase(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    """When ctx.phase is None / missing, publish proceeds with empty
    phase string rather than dropping the event entirely (preserves
    op terminal visibility even under degraded ctx)."""
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")

    class _DegradedCtx:
        op_id = "op-test-degraded"
        phase = None
        phase_entered_at = None
        terminal_reason_code = ""

    event_id = publish_operation_terminal(_DegradedCtx(), _DuckState("failed"))
    assert event_id is not None
    history = fresh_broker.recent_history(
        limit=1, event_type=EVENT_TYPE_OPERATION_TERMINAL,
        op_id="op-test-degraded",
    )
    assert len(history) == 1
    assert history[0].payload["phase"] == ""
    assert history[0].payload["phase_entered_at"] == ""


# ---------------------------------------------------------------------------
# 5. Subscriber filter by op_id receives terminal event
# ---------------------------------------------------------------------------


def test_subscriber_filtered_by_op_id_receives_terminal_event(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    """Operator-binding primary path: subscribe by op_id, receive
    terminal event. This is the rendezvous shape SWE-Bench-Pro B.2.2
    will compose."""
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    target_op = "op-target"
    other_op = "op-other"
    sub = fresh_broker.subscribe(op_id_filter=target_op)
    assert sub is not None
    try:
        publish_operation_terminal(_DuckCtx(op_id=other_op), _DuckState("failed"))
        publish_operation_terminal(_DuckCtx(op_id=target_op), _DuckState("applied"))
        # Drain subscriber queue (non-blocking peek up to 2 items).
        received = []
        for _ in range(2):
            try:
                ev = sub.queue.get_nowait()
                received.append(ev)
            except asyncio.QueueEmpty:
                break
        # Only the target op's terminal event should reach the filtered queue.
        op_targets = [
            ev for ev in received
            if ev.event_type == EVENT_TYPE_OPERATION_TERMINAL
            and ev.op_id == target_op
        ]
        assert len(op_targets) == 1
        other_received = [
            ev for ev in received
            if ev.op_id == other_op
        ]
        assert other_received == []
    finally:
        fresh_broker.unsubscribe(sub)


# ---------------------------------------------------------------------------
# 6. Closed taxonomy AST pin
# ---------------------------------------------------------------------------


def test_ast_pin_terminal_states_closed_taxonomy() -> None:
    """``TERMINAL_OPERATION_STATES`` MUST equal exactly the four ledger
    terminal values. Drift (missing one, adding one) is a coverage bug:
    missing "blocked" would silently drop advisor_blocked terminals;
    adding "applying" would fan-out duplicate events per APPLY pass.
    """
    expected = frozenset({"applied", "rolled_back", "failed", "blocked"})
    assert TERMINAL_OPERATION_STATES == expected


def test_ast_pin_terminal_states_match_ledger_enum() -> None:
    """The TERMINAL_OPERATION_STATES set MUST be a subset of the live
    OperationState enum values — verifies the substrate-independence
    comment in ide_observability_stream.py stays honest."""
    from backend.core.ouroboros.governance.ledger import OperationState
    live_values = {m.value for m in OperationState}
    assert TERMINAL_OPERATION_STATES.issubset(live_values)


def test_ast_pin_operation_terminal_in_valid_event_types() -> None:
    """The new event type MUST be present in _VALID_EVENT_TYPES, else
    the broker rejects every publish call (silent dead channel)."""
    assert EVENT_TYPE_OPERATION_TERMINAL in _VALID_EVENT_TYPES
    assert EVENT_TYPE_OPERATION_TERMINAL == "operation_terminal"


# ---------------------------------------------------------------------------
# 7. Naming-collision AST pin — operation_terminal distinct from task_*
# ---------------------------------------------------------------------------


def test_ast_pin_operation_terminal_distinct_from_task_events() -> None:
    """Operator binding (B.2.0.5 design note): IDE clients must not
    mis-parse TaskBoard traffic as op terminal. The event type string
    MUST NOT begin with ``task_`` (the TaskBoard prefix)."""
    assert not EVENT_TYPE_OPERATION_TERMINAL.startswith("task_")


# ---------------------------------------------------------------------------
# 8. Orchestrator wiring AST pins (single-seam / never-raise /
#    ledger-before-publish)
# ---------------------------------------------------------------------------


def _orchestrator_source() -> str:
    from backend.core.ouroboros.governance import orchestrator
    return Path(orchestrator.__file__).read_text()


def test_ast_pin_publish_called_exactly_once_in_orchestrator() -> None:
    """Single-seam invariant: ``publish_operation_terminal`` referenced
    by name EXACTLY once in orchestrator.py (the body of
    ``_record_ledger``). Drift here would mean a parallel call site
    issuing duplicate events for the same terminal."""
    src = _orchestrator_source()
    tree = ast.parse(src)
    call_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "publish_operation_terminal":
                call_sites.append(node)
    assert len(call_sites) == 1, (
        f"publish_operation_terminal must be called exactly once "
        f"in orchestrator.py; found {len(call_sites)} call sites"
    )


def test_ast_pin_publish_call_is_inside_record_ledger() -> None:
    """The single publish call MUST live inside ``_record_ledger``.
    Locating it anywhere else would mean the FSM is split across
    chokepoints (multiple authors of terminal events)."""
    src = _orchestrator_source()
    tree = ast.parse(src)
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for fn in ast.walk(cls):
                if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_record_ledger":
                    body_text = ast.unparse(fn)
                    if "publish_operation_terminal" in body_text:
                        return
    raise AssertionError(
        "_record_ledger body does not contain "
        "publish_operation_terminal — wiring missing"
    )


def test_ast_pin_publish_call_wrapped_in_try_except() -> None:
    """Operator binding "never raise into _record_ledger": the
    ``publish_operation_terminal`` call site MUST be inside a Try
    block within _record_ledger."""
    src = _orchestrator_source()
    tree = ast.parse(src)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in ast.walk(cls):
            if not (
                isinstance(fn, ast.AsyncFunctionDef)
                and fn.name == "_record_ledger"
            ):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try):
                    continue
                try_body_text = ast.unparse(ast.Module(
                    body=list(node.body), type_ignores=[],
                ))
                if "publish_operation_terminal" in try_body_text:
                    return
    raise AssertionError(
        "publish_operation_terminal call in _record_ledger is "
        "NOT wrapped in a try/except — operator binding violated"
    )


def test_ast_pin_ledger_append_called_before_publish() -> None:
    """Operator binding "publish_* cannot block ledger": within
    _record_ledger's body, ``self._stack.ledger.append`` MUST appear
    positionally BEFORE the ``publish_operation_terminal`` invocation.
    Using ast.unparse + index comparison guarantees source-order
    truth (line numbers can lie under formatter shuffles)."""
    src = _orchestrator_source()
    tree = ast.parse(src)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in ast.walk(cls):
            if not (
                isinstance(fn, ast.AsyncFunctionDef)
                and fn.name == "_record_ledger"
            ):
                continue
            fn_text = ast.unparse(fn)
            append_idx = fn_text.find("ledger.append")
            publish_idx = fn_text.find("publish_operation_terminal")
            assert append_idx >= 0, "ledger.append missing from _record_ledger"
            assert publish_idx >= 0, (
                "publish_operation_terminal missing from _record_ledger"
            )
            assert append_idx < publish_idx, (
                "publish_operation_terminal appears BEFORE ledger.append "
                "in _record_ledger — wiring inverted; publish must "
                "follow a successful append per operator binding"
            )
            return
    raise AssertionError("_record_ledger not found in orchestrator.py")


def test_ast_pin_no_publish_inside_data_class_advance() -> None:
    """The publish hook MUST NOT be inside OperationContext.advance() —
    advance() is a pure data-class transition and must remain side-
    effect-free. (Defensive pin against a future refactor temptation
    to hoist the publish "closer to" the FSM transition.)"""
    from backend.core.ouroboros.governance import op_context
    src = Path(op_context.__file__).read_text()
    assert "publish_operation_terminal" not in src, (
        "op_context.py contains publish_operation_terminal — the data "
        "class advance() must stay side-effect-free; publish belongs "
        "in _record_ledger only"
    )


# ---------------------------------------------------------------------------
# 9. FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_one_spec() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 1
    assert captured[0].name == OP_LIFECYCLE_SSE_ENABLED_ENV_VAR
    assert captured[0].default is False


def test_register_flags_never_raises_when_registry_register_throws() -> None:
    """The wrapper MUST swallow per-spec registration failures. This
    exercises the inner try/except in register_flags without polluting
    sys.modules (which could leak across tests via flag_registry
    identity stales). Verifies the fail-open contract."""

    class _ExplodingCapturer:
        def register(self, spec) -> None:
            raise RuntimeError("simulated registry failure")

    # Returns 0 because the only spec failed to register, but the call
    # itself doesn't raise.
    assert register_flags(_ExplodingCapturer()) == 0


# ---------------------------------------------------------------------------
# 10. End-to-end orchestrator integration — publish fires through real
#     _record_ledger when ledger.append succeeds; suppressed on duplicate
# ---------------------------------------------------------------------------


class _StubLedger:
    """In-memory ledger fixture that mirrors the dedup contract of
    OperationLedger.append (returns True on first write, False on
    duplicate). Used to exercise the orchestrator wiring without
    bringing up the full stack."""

    def __init__(self) -> None:
        self._seen: set = set()
        self.appended: list = []

    async def append(self, entry: Any) -> bool:
        key = (entry.op_id, entry.state.value)
        if key in self._seen:
            return False
        self._seen.add(key)
        self.appended.append(entry)
        return True


class _StubStack:
    def __init__(self) -> None:
        self.ledger = _StubLedger()


class _StubConfig:
    project_root = Path(".")


def test_orchestrator_record_ledger_publishes_on_terminal(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    """End-to-end through the real ``_record_ledger`` method —
    successful append + terminal state → exactly one event."""
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator,
    )
    from backend.core.ouroboros.governance.ledger import OperationState

    # Bypass full orchestrator construction — we only need
    # _record_ledger as a bound method against a minimal stub.
    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    orch._stack = _StubStack()  # type: ignore[attr-defined]
    orch._config = _StubConfig()  # type: ignore[attr-defined]

    ctx = _DuckCtx(op_id="op-record-001", phase="COMPLETE",
                   terminal_reason_code="clean")
    asyncio.run(orch._record_ledger(ctx, OperationState.APPLIED, {}))
    history = fresh_broker.recent_history(
        limit=10, event_type=EVENT_TYPE_OPERATION_TERMINAL,
        op_id="op-record-001",
    )
    assert len(history) == 1
    assert history[0].payload["state"] == "applied"


def test_orchestrator_record_ledger_does_not_publish_for_non_terminal(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator,
    )
    from backend.core.ouroboros.governance.ledger import OperationState

    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    orch._stack = _StubStack()  # type: ignore[attr-defined]
    orch._config = _StubConfig()  # type: ignore[attr-defined]

    ctx = _DuckCtx(op_id="op-record-002")
    asyncio.run(orch._record_ledger(ctx, OperationState.GATING, {}))
    history = fresh_broker.recent_history(
        limit=10, event_type=EVENT_TYPE_OPERATION_TERMINAL,
    )
    assert history == []


def test_orchestrator_record_ledger_idempotent_on_duplicate(
    monkeypatch: pytest.MonkeyPatch, fresh_broker: Any,
) -> None:
    """Idempotency contract: ledger.append returns False on duplicate,
    which MUST suppress the publish (exactly one event per (op_id,
    state) pair)."""
    monkeypatch.setenv(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "true")
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator,
    )
    from backend.core.ouroboros.governance.ledger import OperationState

    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    orch._stack = _StubStack()  # type: ignore[attr-defined]
    orch._config = _StubConfig()  # type: ignore[attr-defined]

    ctx = _DuckCtx(op_id="op-record-dup")
    asyncio.run(orch._record_ledger(ctx, OperationState.FAILED, {}))
    asyncio.run(orch._record_ledger(ctx, OperationState.FAILED, {}))
    asyncio.run(orch._record_ledger(ctx, OperationState.FAILED, {}))
    history = fresh_broker.recent_history(
        limit=10, event_type=EVENT_TYPE_OPERATION_TERMINAL,
        op_id="op-record-dup",
    )
    assert len(history) == 1


def test_orchestrator_record_ledger_master_off_byte_identical(
    clean_env: None, fresh_broker: Any,
) -> None:
    """With master flag unset, _record_ledger behavior is byte-
    identical: ledger.append still fires, but no SSE event is
    published. Defends the §33.1 graduation contract."""
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator,
    )
    from backend.core.ouroboros.governance.ledger import OperationState

    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    orch._stack = _StubStack()  # type: ignore[attr-defined]
    orch._config = _StubConfig()  # type: ignore[attr-defined]

    ctx = _DuckCtx()
    asyncio.run(orch._record_ledger(ctx, OperationState.APPLIED, {}))
    # Ledger append fired.
    assert len(orch._stack.ledger.appended) == 1
    # No SSE event.
    history = fresh_broker.recent_history(
        limit=10, event_type=EVENT_TYPE_OPERATION_TERMINAL,
    )
    assert history == []
