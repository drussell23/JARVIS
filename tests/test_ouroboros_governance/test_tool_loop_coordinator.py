from __future__ import annotations
import asyncio, json, time
from pathlib import Path
from typing import List, Optional
import pytest
from backend.core.ouroboros.governance.tool_executor import (
    AsyncProcessToolBackend, GoverningToolPolicy, PolicyContext, PolicyDecision,
    ToolCall, ToolExecStatus, ToolLoopCoordinator, ToolResult, _format_tool_result,
    _MAX_PROMPT_CHARS,
)
from backend.core.ouroboros.governance.candidate_generator import (
    FailbackStateMachine,
    FailureMode,
)

_SCHEMA = "2b.2-tool"

def _tool_resp(name="read_file", args=None):
    return json.dumps({"schema_version": _SCHEMA,
        "tool_call": {"name": name, "arguments": args or {"path": "src/foo.py"}}})

def _patch_resp():
    return json.dumps({"schema_version": "2b.1",
        "candidates": [{"candidate_id": "c1", "file_path": "src/foo.py",
                         "full_content": "x = 1\n", "rationale": "t"}]})

def _parse_fn(raw: str) -> Optional[List[ToolCall]]:
    """Parse a provider response into a list of tool calls or None.

    Contract update: ``ToolLoopCoordinator.run`` passes ``parse_fn`` a
    ``List[ToolCall]`` now (parallel-execution aware), not a single
    ``ToolCall``. Returning a single-element list keeps these tests
    exercising the sequential path while matching the new signature.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if data.get("schema_version") != _SCHEMA:
        return None
    tc = data.get("tool_call", {})
    name = tc.get("name")
    if not name:
        return None
    return [ToolCall(name=name, arguments=tc.get("arguments", {}))]

def _allow_policy(repo_root):
    return GoverningToolPolicy(repo_roots={"jarvis": repo_root})

def _coordinator(repo_root, max_rounds=5):
    return ToolLoopCoordinator(
        backend=AsyncProcessToolBackend(semaphore=asyncio.Semaphore(2)),
        policy=_allow_policy(repo_root), max_rounds=max_rounds, tool_timeout_s=30.0)

@pytest.mark.asyncio
async def test_max_rounds_exceeded(tmp_path):
    coordinator = _coordinator(tmp_path, max_rounds=3)
    call_count = [0]
    async def generate_fn(prompt):
        call_count[0] += 1
        return _tool_resp()
    with pytest.raises(RuntimeError, match="tool_loop_max_rounds_exceeded"):
        await coordinator.run(prompt="init", generate_fn=generate_fn,
            parse_fn=_parse_fn, repo="jarvis", op_id="op-max", deadline=time.monotonic() + 30)
    assert call_count[0] == 3

@pytest.mark.asyncio
async def test_context_overflow_raises_and_classifies_as_context_overflow(tmp_path):
    """Task #96 regression fence: force the actual raise and verify the
    full wiring from ToolLoopCoordinator.run() → FailbackStateMachine.

    Previously this test was named ``test_budget_exceeded`` and used a
    120K base prompt. After commit ff3d2f841b added a force-truncate
    fallback, that configuration no longer hit the raise path — it was
    silently saved by truncation, and the test passed vacuously on
    `DID NOT RAISE`. This version sizes the base prompt at
    ``_MAX_PROMPT_CHARS - 72`` so ``appendix ≤ overflow`` holds, which
    is the only condition that actually reaches line 3452 in
    tool_executor.py (``raise RuntimeError("tool_loop_context_overflow:...")``).

    Without this, a battle test that reports "0 CONTEXT_OVERFLOW hits"
    leaves the question open: is the path dead, or just unexercised?
    This test answers it — the raise path IS wired, and classification
    lands on CONTEXT_OVERFLOW (not TIMEOUT, which would trigger the
    wrong backoff policy).
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("y" * 80_000)

    base_prompt = "x" * (_MAX_PROMPT_CHARS - 72)
    assert len(base_prompt) == _MAX_PROMPT_CHARS - 72

    coordinator = _coordinator(tmp_path)
    responses = [_tool_resp(), _patch_resp()]
    idx = [0]

    async def generate_fn(prompt):
        i = min(idx[0], len(responses) - 1)
        idx[0] += 1
        return responses[i]

    with pytest.raises(RuntimeError) as exc_info:
        await coordinator.run(
            prompt=base_prompt,
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-context-overflow",
            deadline=time.monotonic() + 30,
        )

    # 1. The raise path is wired and uses the post-ff3d2f841b message.
    assert "tool_loop_context_overflow" in str(exc_info.value), (
        f"expected 'tool_loop_context_overflow' in error, got {exc_info.value!r}"
    )
    assert "tool_loop_budget_exceeded" not in str(exc_info.value), (
        "old error name must not appear — commit ff3d2f841b renamed it"
    )

    # 2. Classification lands on CONTEXT_OVERFLOW, not TIMEOUT.
    mode = FailbackStateMachine.classify_exception(exc_info.value)
    assert mode is FailureMode.CONTEXT_OVERFLOW, (
        f"expected CONTEXT_OVERFLOW, got {mode} — string classifier drift "
        f"would silently reroute overflows to TIMEOUT backoff"
    )
    assert mode is not FailureMode.TIMEOUT

    # 3. The FSM must NOT apply any backoff for this mode (zero-eta policy).
    fsm = FailbackStateMachine()
    fsm.record_primary_failure(mode=FailureMode.CONTEXT_OVERFLOW)
    assert fsm.recovery_eta() <= time.monotonic(), (
        "CONTEXT_OVERFLOW must have zero backoff — otherwise a single "
        "huge tool result stalls the provider for seconds"
    )

@pytest.mark.asyncio
async def test_deadline_exceeded(tmp_path):
    coordinator = _coordinator(tmp_path)
    async def generate_fn(prompt): return _patch_resp()
    with pytest.raises(RuntimeError, match="tool_loop_deadline_exceeded"):
        await coordinator.run(prompt="init", generate_fn=generate_fn,
            parse_fn=_parse_fn, repo="jarvis", op_id="op-dl",
            deadline=time.monotonic() - 1.0)  # already expired

@pytest.mark.asyncio
async def test_tool_timeout(tmp_path):
    # Use a stub backend that returns ToolResult(status=TIMEOUT) when the deadline
    # has expired — this is the contract the coordinator relies on after removing
    # the asyncio.wait_for wrapper (backend owns deadline enforcement).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("pass\n")

    class TimeoutBackend:
        async def execute_async(self, call, policy_ctx, deadline):
            # Simulate backend enforcing an already-expired deadline.
            return ToolResult(tool_call=call, output="", error="TIMEOUT",
                status=ToolExecStatus.TIMEOUT)

    coordinator = ToolLoopCoordinator(
        backend=TimeoutBackend(),
        policy=_allow_policy(tmp_path), max_rounds=5, tool_timeout_s=0.001)
    responses = [_tool_resp(), _patch_resp()]
    idx = [0]
    async def generate_fn(prompt):
        i = min(idx[0], len(responses)-1); idx[0] += 1; return responses[i]
    raw, records = await coordinator.run(prompt="init", generate_fn=generate_fn,
        parse_fn=_parse_fn, repo="jarvis", op_id="op-to", deadline=time.monotonic()+30)
    assert any(r.status == ToolExecStatus.TIMEOUT for r in records)

@pytest.mark.asyncio
async def test_cancellation_propagates(tmp_path):
    test_file = tmp_path / "tests" / "test_slow.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("import time\ndef test_slow(): time.sleep(60)\n")
    policy = GoverningToolPolicy(repo_roots={"jarvis": tmp_path}, run_tests_allowed=True)
    coordinator = ToolLoopCoordinator(
        backend=AsyncProcessToolBackend(semaphore=asyncio.Semaphore(2)),
        policy=policy, max_rounds=5, tool_timeout_s=30.0)
    responses = [json.dumps({"schema_version": _SCHEMA,
        "tool_call": {"name": "run_tests", "arguments": {"paths": [str(test_file)]}}}),
        _patch_resp()]
    idx = [0]
    async def generate_fn(prompt):
        i = min(idx[0], len(responses)-1); idx[0] += 1; return responses[i]
    task = asyncio.create_task(coordinator.run(prompt="init", generate_fn=generate_fn,
        parse_fn=_parse_fn, repo="jarvis", op_id="op-cancel", deadline=time.monotonic()+60))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

@pytest.mark.asyncio
async def test_deadline_inversion(tmp_path):
    # per_tool_deadline = min(tool_timeout_s, max(1.0, deadline - monotonic()))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("pass\n")
    observed: list[float] = []

    class TrackingBackend:
        async def execute_async(self, call, policy_ctx, deadline):
            observed.append(deadline)
            return ToolResult(tool_call=call, output="ok", status=ToolExecStatus.SUCCESS)

    coordinator = ToolLoopCoordinator(
        backend=TrackingBackend(), policy=_allow_policy(tmp_path),
        max_rounds=5, tool_timeout_s=5.0)
    responses = [_tool_resp(), _patch_resp()]
    idx = [0]
    async def generate_fn(prompt):
        i = min(idx[0], len(responses)-1); idx[0] += 1; return responses[i]
    outer_deadline = time.monotonic() + 3.0
    await coordinator.run(prompt="init", generate_fn=generate_fn,
        parse_fn=_parse_fn, repo="jarvis", op_id="op-inv", deadline=outer_deadline)
    assert len(observed) == 1
    # per_tool_deadline <= outer_deadline (tool_timeout=5s > remaining~3s, so min picks ~3s from now)
    assert observed[0] <= outer_deadline + 0.2

@pytest.mark.asyncio
async def test_cancelled_op_records_cancellation_event(tmp_path):
    """When task is cancelled during execute_async, coordinator appends CANCELLED record then re-raises."""
    policy = GoverningToolPolicy(repo_roots={"jarvis": tmp_path}, run_tests_allowed=True)
    started = asyncio.Event()
    execute_count = [0]
    cancel_count = [0]

    class CountingBackend:
        async def execute_async(self, call, policy_ctx, deadline):
            execute_count[0] += 1
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancel_count[0] += 1
                raise
            return ToolResult(tool_call=call, output="done", status=ToolExecStatus.SUCCESS)

    coordinator = ToolLoopCoordinator(
        backend=CountingBackend(), policy=policy, max_rounds=5, tool_timeout_s=30.0)
    test_file = tmp_path / "tests" / "test_run.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_x(): pass\n")
    responses = [json.dumps({"schema_version": _SCHEMA,
        "tool_call": {"name": "run_tests", "arguments": {"paths": [str(test_file)]}}}),
        _patch_resp()]
    idx = [0]
    async def generate_fn(prompt):
        i = min(idx[0], len(responses)-1); idx[0] += 1; return responses[i]
    task = asyncio.create_task(coordinator.run(
        prompt="init", generate_fn=generate_fn,
        parse_fn=_parse_fn, repo="jarvis", op_id="op-cr", deadline=time.monotonic()+60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # execute_async was called once and received CancelledError →
    # coordinator's except CancelledError branch ran → CANCELLED record was appended before re-raise.
    assert execute_count[0] == 1
    assert cancel_count[0] == 1
    # Spec: coordinator must append ToolExecutionRecord(status=CANCELLED) before re-raising.
    assert any(r.status == ToolExecStatus.CANCELLED for r in coordinator._last_records)


# ---------------------------------------------------------------------------
# get_last_edit_history — Venom mutation audit surfacing
# ---------------------------------------------------------------------------


def _edit_tool_resp():
    """Provider emits edit_file tool call that mutates src/foo.py."""
    return json.dumps({
        "schema_version": _SCHEMA,
        "tool_call": {
            "name": "edit_file",
            "arguments": {
                "path": "src/foo.py",
                "old_text": "x = 1",
                "new_text": "x = 2",
            },
        },
    })


def _read_foo_tool_resp():
    return json.dumps({
        "schema_version": _SCHEMA,
        "tool_call": {"name": "read_file", "arguments": {"path": "src/foo.py"}},
    })


@pytest.mark.asyncio
async def test_get_last_edit_history_empty_when_no_mutations(tmp_path, monkeypatch):
    """A run that only reads files must surface an empty edit history."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")

    coordinator = _coordinator(tmp_path)
    responses = [_read_foo_tool_resp(), _patch_resp()]
    idx = [0]

    async def generate_fn(_prompt):
        i = min(idx[0], len(responses) - 1)
        idx[0] += 1
        return responses[i]

    raw, _records = await coordinator.run(
        prompt="init", generate_fn=generate_fn, parse_fn=_parse_fn,
        repo="jarvis", op_id="op-noedit", deadline=time.monotonic() + 30,
    )
    assert raw  # final answer returned
    assert coordinator.get_last_edit_history() == []


@pytest.mark.asyncio
async def test_get_last_edit_history_captures_edit_file(tmp_path, monkeypatch):
    """After an edit_file tool call, get_last_edit_history() must return
    an audit entry with the expected keys. This is the observability hook
    providers.py uses to populate GenerationResult.venom_edit_history."""
    monkeypatch.setenv("JARVIS_TOOL_EDIT_ALLOWED", "true")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")

    coordinator = _coordinator(tmp_path)
    # Round 1: read_file (satisfies must-have-read), Round 2: edit_file,
    # Round 3: final patch response.
    responses = [_read_foo_tool_resp(), _edit_tool_resp(), _patch_resp()]
    idx = [0]

    async def generate_fn(_prompt):
        i = min(idx[0], len(responses) - 1)
        idx[0] += 1
        return responses[i]

    raw, _records = await coordinator.run(
        prompt="init", generate_fn=generate_fn, parse_fn=_parse_fn,
        repo="jarvis", op_id="op-edit", deadline=time.monotonic() + 30,
    )
    assert raw

    history = coordinator.get_last_edit_history()
    assert len(history) == 1, f"expected 1 edit entry, got {history!r}"
    entry = history[0]
    assert entry.get("tool") == "edit_file"
    assert entry.get("path") == "src/foo.py"
    # Mutation actually landed on disk.
    assert (tmp_path / "src" / "foo.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_get_last_edit_history_resets_between_runs(tmp_path, monkeypatch):
    """A subsequent run() must not carry over edit history from the prior run."""
    monkeypatch.setenv("JARVIS_TOOL_EDIT_ALLOWED", "true")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")

    coordinator = _coordinator(tmp_path)

    # First run: performs an edit.
    responses1 = [_read_foo_tool_resp(), _edit_tool_resp(), _patch_resp()]
    idx1 = [0]

    async def gen1(_p):
        i = min(idx1[0], len(responses1) - 1)
        idx1[0] += 1
        return responses1[i]

    await coordinator.run(
        prompt="x", generate_fn=gen1, parse_fn=_parse_fn,
        repo="jarvis", op_id="op-r1", deadline=time.monotonic() + 30,
    )
    assert len(coordinator.get_last_edit_history()) == 1

    # Second run: only reads. Must show empty history, not stale from run 1.
    responses2 = [_read_foo_tool_resp(), _patch_resp()]
    idx2 = [0]

    async def gen2(_p):
        i = min(idx2[0], len(responses2) - 1)
        idx2[0] += 1
        return responses2[i]

    await coordinator.run(
        prompt="x", generate_fn=gen2, parse_fn=_parse_fn,
        repo="jarvis", op_id="op-r2", deadline=time.monotonic() + 30,
    )
    # Note: _last_edit_history is updated at _finalize_run. For a run with
    # no mutations, the per-op executor's get_edit_history() returns [],
    # so _last_edit_history must be [] too.
    assert coordinator.get_last_edit_history() == []
