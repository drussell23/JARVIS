"""Production ``AgentTurnFn`` adapter — the Swarm drone's real production brain.

Mandated bulletproof (Step 1): a "Rogue Agent" simulation. The sub-agent, on its
first turn, emits an ``edit_file`` tool-call that targets a line range OUTSIDE
its assigned ``ChunkTarget`` bounds. Assert:

  1. The ``LineRangeJail`` CATCHES it before it reaches the inner backend.
  2. It receives a ``PERMISSION_DENIED`` observation carrying
     ``PermissionError(Out of Bounds: Stick to assigned AST node)``.
  3. On the next turn the agent SELF-CORRECTS its tool-call to target the
     correct line range — which is authorized and delegates to the inner backend.
  4. The final returned node is the verified in-bounds repair.

Plus: wrong-file jailing, in-bounds passthrough, real-brain wiring (the injected
client is driven verbatim), rolling-compaction firing, and never-raise isolation.
The jail is exercised against the REAL ``ScopedToolBackend`` + REAL
``ToolCall``/``ToolResult`` types — no mock of the cage itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.agent_turn_adapter import (
    LineRangeJail,
    ProductionAgentTurnFn,
)
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    ScopedToolGate,
    ToolScope,
)
from backend.core.ouroboros.governance.scoped_tool_backend import ScopedToolBackend
from backend.core.ouroboros.governance.tool_executor import (
    PolicyContext,
    ToolCall,
    ToolExecStatus,
    ToolResult,
)

# A file where `beta` sits at a KNOWN line range so the jail has real bounds.
_FILE = '''"""Module."""


def alpha(a, b):
    return a - b


def beta(a, b):
    return a * a


def gamma(xs):
    return xs[0]
'''

_FIX_BETA = "def beta(a, b):\n    return a * b"


class _RecordingBackend:
    """The inner 'real' backend the ScopedToolBackend delegates to. Records
    every call that survives BOTH the scope gate and the line-range jail — i.e.
    every AUTHORIZED, in-bounds mutation. A jailed call must never reach here."""

    def __init__(self) -> None:
        self.executed: List[ToolCall] = []

    async def execute_async(self, call: ToolCall, policy_ctx: Any, deadline: float) -> ToolResult:
        self.executed.append(call)
        return ToolResult(tool_call=call, output="edit applied", status=ToolExecStatus.SUCCESS)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _ScriptedClient:
    """Stands in for the live DoubleWord provider client. Returns a scripted
    sequence of raw responses — proving the adapter drives the injected client
    verbatim (the real-brain seam) without a network call."""

    def __init__(self, script: List[str]) -> None:
        self._script = list(script)
        self.prompts: List[str] = []
        self.kwargs: List[Dict[str, Any]] = []

    async def generate(self, *, prompt: str, **kwargs: Any) -> _FakeResp:
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        nxt = self._script.pop(0) if self._script else ""
        return _FakeResp(nxt)


def _beta_target() -> ChunkTarget:
    chunk = extract_target_chunk(_FILE, "m.py", "beta")
    assert chunk is not None
    return ChunkTarget(symbol="beta", chunk=chunk, instruction="fix beta")


def _jail_for(target: ChunkTarget, inner: Any) -> LineRangeJail:
    scope = ToolScope(allowed_tools=frozenset({"read_file", "edit_file"}), read_only=False)
    gate = ScopedToolGate(scope)
    scoped = ScopedToolBackend(inner, gate, max_mutations=8)
    chunk = target.chunk
    return LineRangeJail(
        scoped,
        file_path=getattr(chunk, "file_path", "m.py") or "m.py",
        start_line=int(getattr(chunk, "start_line")),
        end_line=int(getattr(chunk, "end_line")),
    )


def _pctx(repo_root: str = ".") -> PolicyContext:
    return PolicyContext(
        repo="jarvis", repo_root=Path(repo_root), op_id="op-1",
        call_id="op-1:r1:edit_file:0", round_index=1, is_read_only=False,
    )


# ---------------------------------------------------------------------------
# LineRangeJail — the net-new mechanism, tested against the REAL cage.
# ---------------------------------------------------------------------------


async def test_jail_denies_out_of_bounds_line_range() -> None:
    target = _beta_target()
    inner = _RecordingBackend()
    jail = _jail_for(target, inner)
    start = int(getattr(target.chunk, "start_line"))

    # Rogue: edit a line ABOVE the node (alpha's territory).
    rogue = ToolCall(name="edit_file", arguments={
        "file_path": "m.py", "start_line": 1, "end_line": 2,
        "new_content": "def alpha(a, b):\n    return 999",
    })
    result = await jail.execute_async(rogue, _pctx(), deadline=1e18)

    assert result.status == ToolExecStatus.PERMISSION_DENIED
    assert "Out of Bounds: Stick to assigned AST node" in (result.error or "")
    assert inner.executed == []              # never reached the real backend
    assert jail.denials and jail.denials[0][0] == "edit_file"
    # Sanity: the assigned range is quoted back for self-correction.
    assert f"lines {start}-" in (result.error or "")


async def test_jail_denies_wrong_file() -> None:
    target = _beta_target()
    inner = _RecordingBackend()
    jail = _jail_for(target, inner)
    rogue = ToolCall(name="edit_file", arguments={
        "file_path": "OTHER.py", "start_line": int(getattr(target.chunk, "start_line")),
        "end_line": int(getattr(target.chunk, "end_line")),
    })
    result = await jail.execute_async(rogue, _pctx(), deadline=1e18)
    assert result.status == ToolExecStatus.PERMISSION_DENIED
    assert "wrong file" in (result.error or "")
    assert inner.executed == []


async def test_jail_allows_in_bounds_edit_and_reads() -> None:
    target = _beta_target()
    inner = _RecordingBackend()
    jail = _jail_for(target, inner)
    lo = int(getattr(target.chunk, "start_line"))
    hi = int(getattr(target.chunk, "end_line"))

    good = ToolCall(name="edit_file", arguments={
        "file_path": "m.py", "start_line": lo, "end_line": hi, "new_content": _FIX_BETA,
    })
    res = await jail.execute_async(good, _pctx(), deadline=1e18)
    assert res.status == ToolExecStatus.SUCCESS
    assert inner.executed and inner.executed[0].name == "edit_file"

    # A read tool with no bounds is never jailed.
    read = ToolCall(name="read_file", arguments={"file_path": "anything.py"})
    res2 = await jail.execute_async(read, _pctx(), deadline=1e18)
    assert res2.status == ToolExecStatus.SUCCESS


# ---------------------------------------------------------------------------
# The Rogue Agent: catch → PermissionError observation → self-correct.
# ---------------------------------------------------------------------------


async def test_rogue_agent_jailed_then_self_corrects() -> None:
    target = _beta_target()
    inner = _RecordingBackend()
    lo = int(getattr(target.chunk, "start_line"))
    hi = int(getattr(target.chunk, "end_line"))

    # A parse_fn that turns our scripted raw strings into tool-calls / final.
    #  - "OOB"  → an out-of-bounds edit_file (rogue turn 1)
    #  - "GOOD" → an in-bounds edit_file (self-corrected turn 2)
    #  - anything else → final answer (no tool calls)
    def parse_fn(raw: str) -> Optional[List[ToolCall]]:
        if raw == "OOB":
            return [ToolCall(name="edit_file", arguments={
                "file_path": "m.py", "start_line": 1, "end_line": 2,
                "new_content": "def alpha(a, b):\n    return 0",
            })]
        if raw == "GOOD":
            return [ToolCall(name="edit_file", arguments={
                "file_path": "m.py", "start_line": lo, "end_line": hi,
                "new_content": _FIX_BETA,
            })]
        return None  # final answer

    # The agent's brain: turn 1 → rogue OOB edit; after it SEES the
    # PermissionError observation in the prompt, turn 2 → in-bounds edit; turn 3
    # → emits the final corrected node.
    class _RogueClient:
        def __init__(self) -> None:
            self.turn = 0
            self.saw_permission_error = False

        async def generate(self, *, prompt: str, **kwargs: Any) -> _FakeResp:
            self.turn += 1
            if "Out of Bounds: Stick to assigned AST node" in prompt:
                self.saw_permission_error = True
            if self.turn == 1:
                return _FakeResp("OOB")
            if self.turn == 2:
                # Only self-correct once the denial observation is visible.
                assert self.saw_permission_error, "must see the jail denial first"
                return _FakeResp("GOOD")
            return _FakeResp(_FIX_BETA)  # final node

    client = _RogueClient()
    adapter = ProductionAgentTurnFn(
        client=client, tool_backend=inner, repo_root=".", op_id="op-1",
        allowed_tools=("read_file", "edit_file"), max_turns=5, parse_fn=parse_fn,
    )

    node = await adapter(target, feedback="")

    # (2) The agent saw the PermissionError observation ...
    assert client.saw_permission_error is True
    # (1)+(3) ... exactly ONE in-bounds edit reached the real backend (the rogue
    # one was jailed), proving catch-then-self-correct.
    assert len(inner.executed) == 1
    ex = inner.executed[0].arguments
    assert ex["start_line"] == lo and ex["end_line"] == hi
    # (4) The final returned node is the verified in-bounds repair.
    assert node.strip() == _FIX_BETA
    import ast as _ast
    tree = _ast.parse(node)
    assert tree.body[0].name == "beta"


# ---------------------------------------------------------------------------
# Real-brain wiring, rolling compaction, and never-raise isolation.
# ---------------------------------------------------------------------------


async def test_adapter_drives_injected_client_verbatim() -> None:
    """The real-brain seam: the adapter calls the injected client's generate,
    and a single final answer with the node returns it verified."""
    target = _beta_target()
    inner = _RecordingBackend()
    client = _ScriptedClient([_FIX_BETA])  # one shot, no tool calls
    adapter = ProductionAgentTurnFn(
        client=client, tool_backend=inner, op_id="op-2", parse_fn=lambda raw: None,
        model_name="Qwen/Qwen3.5-397B-A17B-FP8", max_turns=3,
    )
    node = await adapter(target, feedback="make beta multiply")
    assert node.strip() == _FIX_BETA
    assert client.prompts, "the injected client was actually driven"
    # The node prompt + system/model kwargs were forwarded to the brain.
    assert "beta" in client.prompts[0]
    assert client.kwargs[0]["model_name"] == "Qwen/Qwen3.5-397B-A17B-FP8"


async def test_rolling_compaction_fires_on_deep_loop() -> None:
    """Deep ReAct loop → the existing ContextCompactor is invoked before the
    next brain prompt, folding older turns into a summary."""
    target = _beta_target()
    inner = _RecordingBackend()

    compacted = {"n": 0}

    class _FakeCompactor:
        def should_compact(self, entries: List[Dict[str, Any]], config: Any = None) -> bool:
            return len(entries) > 2

        async def compact(self, entries: List[Dict[str, Any]], config: Any = None, *, op_id: Any = None) -> Any:
            compacted["n"] += 1
            class _R:
                summary = "compacted earlier turns"
                entries_before = len(entries)
            return _R()

    # parse_fn keeps requesting an in-bounds read (extends the transcript) until
    # the brain finally answers with the node on a late turn.
    lo = int(getattr(target.chunk, "start_line"))

    def parse_fn(raw: str) -> Optional[List[ToolCall]]:
        if raw == "READ":
            return [ToolCall(name="read_file", arguments={"file_path": "m.py", "line": lo})]
        return None

    class _LoopClient:
        def __init__(self) -> None:
            self.turn = 0

        async def generate(self, *, prompt: str, **kwargs: Any) -> _FakeResp:
            self.turn += 1
            if self.turn < 4:
                return _FakeResp("READ")
            return _FakeResp(_FIX_BETA)

    adapter = ProductionAgentTurnFn(
        client=_LoopClient(), tool_backend=inner, op_id="op-3", parse_fn=parse_fn,
        compactor=_FakeCompactor(), max_turns=8,
    )
    node = await adapter(target, feedback="")
    assert node.strip() == _FIX_BETA
    assert compacted["n"] >= 1, "rolling compaction must have fired on the deep loop"


async def test_adapter_never_raises_returns_empty_on_brain_fault() -> None:
    target = _beta_target()

    class _BoomClient:
        async def generate(self, *, prompt: str, **kwargs: Any) -> _FakeResp:
            raise RuntimeError("DW upstream_error")

    adapter = ProductionAgentTurnFn(
        client=_BoomClient(), tool_backend=_RecordingBackend(), op_id="op-4",
        parse_fn=lambda raw: None, max_turns=2,
    )
    node = await adapter(target, feedback="")
    assert node == ""  # isolated → swarm marks failed → interceptor RAG fallback


async def test_adapter_unconverged_returns_empty() -> None:
    """A brain that only ever emits an unparseable non-node → verified-empty."""
    target = _beta_target()
    adapter = ProductionAgentTurnFn(
        client=_ScriptedClient(["this is not a function at all"]),
        tool_backend=_RecordingBackend(), op_id="op-5", parse_fn=lambda raw: None,
        max_turns=2,
    )
    node = await adapter(target, feedback="")
    assert node == ""
