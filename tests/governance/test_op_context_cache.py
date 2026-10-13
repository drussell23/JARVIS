"""An op's retries open with what it already read, while it is still true.

Up to ~13 stateless retry layers fire after a diff fails to place, and each
re-entered the tool loop at round 0 and re-read the same files before it was
allowed to write. These pin the cache's validity rules, the pinned region
around a failed hunk, and the real loop opening with (and crediting) what the
op already gathered.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import op_context_cache as occ
from backend.core.ouroboros.governance.tool_executor import (
    PolicyDecision, PolicyResult, ToolCall, ToolExecStatus, ToolLoopCoordinator,
    ToolResult, _compute_args_hash,
)

OP = "op-cache-1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    occ.reset_for_tests()
    monkeypatch.delenv("JARVIS_OP_CONTEXT_CACHE_ENABLED", raising=False)
    yield
    occ.reset_for_tests()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"idx")
    (tmp_path / "a.py").write_text("".join(f"line {i}\n" for i in range(1, 101)))
    return tmp_path


def _read(repo: Path, path: str = "a.py", out: str = "contents of a") -> None:
    call = ToolCall(name="read_file", arguments={"path": path})
    occ.record(OP, repo, call, ToolResult(tool_call=call, output=out),
               args_hash=_compute_args_hash(call.arguments))


def _bump_mtime(p: Path) -> None:
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


# --------------------------------------------------------------------------
# 1. validity
# --------------------------------------------------------------------------

def test_a_current_read_opens_the_next_attempt(repo) -> None:
    _read(repo)
    block, shown = occ.opening(OP, repo, budget_chars=10_000)
    assert "Already gathered" in block and "contents of a" in block
    assert [c.name for c, _ in shown] == ["read_file"]


def test_a_changed_file_is_never_shown(repo) -> None:
    _read(repo)
    _bump_mtime(repo / "a.py")
    assert occ.opening(OP, repo, budget_chars=10_000) == ("", [])


def test_a_landing_elsewhere_invalidates_by_the_index(repo) -> None:
    _read(repo)
    _bump_mtime(repo / ".git" / "index")
    assert occ.opening(OP, repo, budget_chars=10_000)[1] == []


def test_an_edit_by_the_op_invalidates_everything(repo) -> None:
    _read(repo)
    edit = ToolCall(name="edit_file", arguments={"path": "a.py"})
    occ.record(OP, repo, edit, ToolResult(tool_call=edit, output="ok"),
               args_hash=_compute_args_hash(edit.arguments))
    assert occ.opening(OP, repo, budget_chars=10_000)[1] == []


def test_failures_are_not_cached(repo) -> None:
    call = ToolCall(name="read_file", arguments={"path": "a.py"})
    occ.record(OP, repo, call, ToolResult(tool_call=call, output="", error="boom",
                                          status=ToolExecStatus.EXEC_ERROR),
               args_hash=_compute_args_hash(call.arguments))
    assert occ.opening(OP, repo, budget_chars=10_000)[1] == []


def test_the_budget_is_honoured_and_says_what_it_left_out(repo) -> None:
    for i in range(5):
        (repo / f"f{i}.py").write_text("x")
        _read(repo, f"f{i}.py", out="y" * 400)
    block, shown = occ.opening(OP, repo, budget_chars=1200)
    assert 0 < len(shown) < 5 and "omitted for space" in block


def test_an_ended_op_releases_its_cache(repo) -> None:
    from backend.core.ouroboros.governance.op_context import _notify_phase_transition
    _read(repo)
    _notify_phase_transition(OP, "COMPLETE")
    assert occ.opening(OP, repo, budget_chars=10_000) == ("", [])


# --------------------------------------------------------------------------
# 2. the failed hunk's region goes first, read fresh
# --------------------------------------------------------------------------

def test_a_rejected_hunk_pins_the_current_source_where_it_was_aimed(repo) -> None:
    rejection = "context diverges after 2 matching line(s): expected 'foo', file line 50 is 'line 50'"
    assert occ.pin_failed_hunk(OP, "a.py", rejection)
    _read(repo)
    block, _ = occ.opening(OP, repo, budget_chars=10_000)
    assert block.index("[CURRENT SOURCE] a.py") < block.index("contents of a")
    assert "50│line 50" in block  # source_lines format


def test_the_realignment_hint_pins_the_region_and_points_at_it(repo) -> None:
    from backend.core.ouroboros.governance.providers import _arm_diff_context_realignment
    ctx = SimpleNamespace(op_id=OP, strategic_memory_prompt="")
    _arm_diff_context_realignment(ctx, "a.py", "context line 'x' not found within ±8 lines of line 12")
    assert "opens your next attempt" in ctx.strategic_memory_prompt
    block, _ = occ.opening(OP, repo, budget_chars=10_000)
    assert "12│line 12" in block  # source_lines format


# --------------------------------------------------------------------------
# 3. the real loop: a retry opens with it and is credited for it
# --------------------------------------------------------------------------

def _coord(repo: Path) -> ToolLoopCoordinator:
    class _Policy:
        def evaluate(self, call: Any, ctx: Any) -> PolicyResult:
            return PolicyResult(decision=PolicyDecision.ALLOW, reason_code="ok")

        def repo_root_for(self, r: str) -> Path:
            return repo

    class _Backend:
        async def execute_async(self, call: Any, ctx: Any, deadline: Any) -> ToolResult:
            return ToolResult(tool_call=call, output=(repo / call.arguments["path"]).read_text())

    return ToolLoopCoordinator(backend=_Backend(), policy=_Policy(),  # type: ignore[arg-type]
                               max_rounds=3, tool_timeout_s=5.0)


async def _attempt(coord: ToolLoopCoordinator, script: List[str]) -> tuple:
    prompts: List[str] = []

    async def gen(prompt: str) -> str:
        prompts.append(prompt)
        return script[min(len(prompts) - 1, len(script) - 1)]

    def parse(raw: str) -> Any:
        return [ToolCall(name="read_file", arguments={"path": "a.py"})] if raw == "READ" else None

    # Records reach the orchestrator the two ways it harvests them: the
    # return value, or stamped on the exception an outer retry catches.
    try:
        _final, records = await coord.run("fix a.py", gen, parse, repo="jarvis", op_id=OP,
                                          deadline=time.monotonic() + 120)
    except RuntimeError as exc:
        records = getattr(exc, "tool_execution_records", None) or []
    return prompts, list(records)


@pytest.mark.asyncio
async def test_a_retry_opens_with_what_the_first_attempt_read(repo) -> None:
    coord = _coord(repo)
    await _attempt(coord, ["READ", "DONE"])                       # attempt 1 reads a.py
    prompts, records = await _attempt(coord, ["DONE"])             # the retry
    assert "Already gathered" in prompts[0] and "line 77" in prompts[0]
    cached = [r for r in records if r.policy_reason_code == "op_context_cache"]
    assert cached and cached[0].arguments_hash == _compute_args_hash({"path": "a.py"})
    assert cached[0].status == ToolExecStatus.SUCCESS


@pytest.mark.asyncio
async def test_the_kill_switch_restores_a_blank_retry(repo, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_OP_CONTEXT_CACHE_ENABLED", "false")
    coord = _coord(repo)
    await _attempt(coord, ["READ", "DONE"])
    prompts, _ = await _attempt(coord, ["DONE"])
    assert "Already gathered" not in prompts[0]
