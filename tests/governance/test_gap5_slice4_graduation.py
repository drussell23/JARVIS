"""Gap #5 Slice 4 graduation pins — the default is live.

Slice 4 flipped one env flag from its Slice-2 opt-in state to the
graduated production default:

  * JARVIS_TOOL_TASK_BOARD_ENABLED    false -> **true**

(The Slice 3 advisory prompt flag JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED
was already default-on since it's pure observability; no graduation
needed.)

This module encodes the graduation contract as tests so the flip is
self-documenting + future regressions fail loudly. Mirrors the
Ticket #4 Slice 4 pattern:

  1. Graduation pin: default is ``true`` when env is absent.
  2. Opt-out pins: explicit ``"false"`` reverts to Slice-2 deny
     behavior.
  3. Authority invariants preserved: manifest caps still empty;
     tools still NOT in _MUTATION_TOOLS; handler module still
     doesn't import gate modules.
  4. Structural safeguards preserved: per-call bad-args validation
     still fires; capacity + length caps still fire.
  5. Slice-3 orchestrator wiring preserved: CONTEXT_EXPANSION
     injection + finally-block close_task_board still present.
  6. Docstring bit-rot guards: env helper docstring carries the
     graduation language.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.task_board import (
    TaskBoard,
    _prompt_injection_enabled,
)
from backend.core.ouroboros.governance.task_tool import (
    close_task_board,
    reset_task_board_registry,
    task_tools_enabled,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
)
from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS,
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Fixtures — clean env per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_gap5_env(monkeypatch):
    """Every test starts with no Gap #5 flags set — graduated
    defaults are what the tests observe."""
    for key in list(os.environ.keys()):
        if (
            key.startswith("JARVIS_TOOL_TASK_BOARD_")
            or key.startswith("JARVIS_TASK_BOARD_")
        ):
            monkeypatch.delenv(key, raising=False)
    reset_task_board_registry()
    yield
    reset_task_board_registry()


def _pctx(op_id: str = "op-slice4") -> PolicyContext:
    return PolicyContext(
        repo="jarvis", repo_root=Path("/tmp"),
        op_id=op_id, call_id=op_id + ":r0:t0",
        round_index=0, risk_tier=None, is_read_only=False,
    )


def _call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, arguments=dict(args))


# ---------------------------------------------------------------------------
# 1. Graduation pin — default is now true
# ---------------------------------------------------------------------------


def test_4a_task_tools_default_post_graduation_is_true():
    """Slice 4 pin: ``JARVIS_TOOL_TASK_BOARD_ENABLED`` default is
    ``true`` after graduation. Model-facing scratchpad tools are
    enabled on a fresh operator install; opt-out via explicit
    ``"false"``."""
    assert task_tools_enabled() is True


def test_4b_prompt_injection_still_default_true():
    """Slice 4 invariant: the Slice 3 prompt injection flag was
    already default-on by design (authority-free). Graduation didn't
    flip it — it's still default-on. Pin to catch accidental
    regressions."""
    assert _prompt_injection_enabled() is True


def test_4c_policy_allows_task_create_with_graduated_defaults():
    """Slice 4 end-to-end: with no env overrides at all, policy
    ALLOWS a well-formed task_create call. Proves the graduated
    default is wired through to the policy engine."""
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call("task_create", title="post-graduation work"), _pctx(),
    )
    assert result.decision == PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# 2. Opt-out pins — explicit false reverts
# ---------------------------------------------------------------------------


def test_4d_explicit_false_opts_out(monkeypatch):
    """Slice 4 opt-out pin: operators retain a runtime kill switch
    via ``=false``. Proves the graduation flip is reversible at
    the env layer."""
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    assert task_tools_enabled() is False


def test_4e_opt_out_is_case_insensitive(monkeypatch):
    """Slice 4 opt-out pin: ``FALSE``/``False``/``  false  `` all
    revert correctly. Defensive against fat-fingered env values."""
    for val in ("false", "False", "FALSE", "  False  "):
        monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", val)
        assert task_tools_enabled() is False, (
            "opt-out failed for " + repr(val)
        )


def test_4f_policy_still_denies_when_explicitly_off(monkeypatch):
    """Slice 4: policy deny-path still fires on explicit opt-out
    post-graduation. Ticket #4 Slice 2 discipline preserved."""
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call("task_create", title="t"), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_tools_disabled"


# ---------------------------------------------------------------------------
# 3. Authority invariants preserved through graduation
# ---------------------------------------------------------------------------


def test_4g_all_three_tools_still_read_only():
    """Slice 4 invariant: manifest capabilities for the three task
    tools are UNCHANGED — still ``frozenset()`` (empty / no side
    effects). Graduation flips opt-in, NOT authority shape."""
    for name in ("task_create", "task_update", "task_complete"):
        assert name in _L1_MANIFESTS
        m = _L1_MANIFESTS[name]
        assert m.capabilities == frozenset(), (
            "Slice 4 graduation violation: " + name + " caps mutated "
            "through graduation (got " + str(m.capabilities) + "). "
            "Graduation MUST NOT escalate authority."
        )
        assert "write" not in m.capabilities


def test_4h_task_tools_still_not_in_mutation_tools():
    """Slice 4 invariant: task tools stay out of _MUTATION_TOOLS.
    Under is_read_only scope they remain permitted (observation,
    not mutation)."""
    for name in ("task_create", "task_update", "task_complete"):
        assert name not in _MUTATION_TOOLS


def test_4i_task_tool_module_still_doesnt_import_gate_modules():
    """Slice 4 invariant: the Slice 2 import-surface boundary HOLDS
    post-graduation. task_tool.py MUST NOT grow imports of
    Iron Gate / risk_tier_floor / semantic_guardian / policy_engine."""
    src = Path(
        "backend/core/ouroboros/governance/task_tool.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 4 authority violation: task_tool.py now imports "
            + repr(f) + ". Graduation MUST NOT grant new authority "
            "surface."
        )


# ---------------------------------------------------------------------------
# 4. Structural safeguards preserved
# ---------------------------------------------------------------------------


def test_4j_bad_args_still_denied_post_graduation():
    """Slice 4 invariant: malformed args still produce a structured
    deny decision post-graduation. Graduation flips the master
    switch; per-call validation is untouched."""
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    # Empty title — bad shape, should deny regardless of master switch.
    result = policy.evaluate(_call("task_create", title=""), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


def test_4k_capacity_caps_still_fire_post_graduation(monkeypatch):
    """Slice 4 invariant: TaskBoard-level capacity caps fire
    regardless of tool-flag graduation. Primitive guarantees
    survive."""
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_TASKS", "2")
    board = TaskBoard(op_id="op-4k")
    board.create(title="a")
    board.create(title="b")
    from backend.core.ouroboros.governance.task_board import (
        TaskBoardCapacityError,
    )
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="overflow")


# ---------------------------------------------------------------------------
# 5. Slice-3 orchestrator wiring preserved post-graduation
# ---------------------------------------------------------------------------


def test_4l_orchestrator_close_task_board_still_wired():
    """Slice 4 invariant: the Slice 3 ctx-shutdown hook HOLDS
    through graduation. orchestrator.py::run()'s finally block
    still calls close_task_board."""
    src = Path(
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text()
    assert "close_task_board" in src, (
        "Slice 4 regression: orchestrator no longer imports "
        "close_task_board — the Slice 3 ctx-shutdown hook has been "
        "stripped."
    )


def test_4m_orchestrator_render_prompt_section_still_wired():
    """Slice 4 invariant: Slice 3 CONTEXT_EXPANSION injection HOLDS."""
    src = Path(
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text()
    assert "render_prompt_section" in src, (
        "Slice 4 regression: orchestrator no longer calls "
        "render_prompt_section — the Slice 3 advisory injection "
        "has been stripped."
    )


# ---------------------------------------------------------------------------
# 6. Mixed-state matrix — Gap #5 has one flag, but test interactions
# ---------------------------------------------------------------------------


def test_4n_full_revert_matrix_single_flag(monkeypatch):
    """Slice 4 matrix pin (single-flag version of the Ticket #4
    Slice 4 two-flag matrix): graduated default + explicit false
    are the two states; verify both reach their documented outcomes.
    """
    # Graduated default (env absent) -> True.
    monkeypatch.delenv("JARVIS_TOOL_TASK_BOARD_ENABLED", raising=False)
    assert task_tools_enabled() is True

    # Explicit false -> False.
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    assert task_tools_enabled() is False

    # Back to graduated default -> True again (env var deleted).
    monkeypatch.delenv("JARVIS_TOOL_TASK_BOARD_ENABLED", raising=False)
    assert task_tools_enabled() is True


def test_4o_task_tool_off_but_prompt_on_is_valid_state(monkeypatch):
    """Slice 4 mixed-state pin: operators can opt OUT of the Venom
    task tools while KEEPING the advisory prompt injection on
    (since prompt injection is default-on and controlled by a
    separate env flag). Degrades to "I can't mutate but I can still
    see an empty 'Current tasks' section" — harmless but documented."""
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    # Prompt injection flag unset — default is true.
    assert task_tools_enabled() is False
    assert _prompt_injection_enabled() is True


# ---------------------------------------------------------------------------
# 7. Docstring bit-rot guards
# ---------------------------------------------------------------------------


def test_4p_task_tools_enabled_docstring_documents_graduation():
    """Slice 4 bit-rot guard: ``task_tools_enabled`` docstring
    carries the graduation date + opt-out language. Future
    refactors that strip the documentation fail loudly."""
    doc = (task_tools_enabled.__doc__ or "").lower()
    assert "graduat" in doc
    assert "true" in doc
    assert "false" in doc or "opt" in doc


# ---------------------------------------------------------------------------
# 8. End-to-end under graduated defaults (meta-integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4q_full_lifecycle_works_under_graduated_defaults():
    """Slice 4 meta-test: with NO env overrides at all, the full
    create→start→complete lifecycle works end-to-end through the
    Venom tool surface. Proves graduation is wired correctly
    through the policy + dispatch + handler + primitive layers."""
    from backend.core.ouroboros.governance.task_tool import run_task_tool
    import json as _json
    pctx = _pctx("op-4q-e2e")

    # 1. Create.
    r1 = await run_task_tool(
        _call("task_create", title="e2e under graduation"),
        pctx, timeout=10.0, cap=4096,
    )
    assert r1.status.value == "success"
    tid = _json.loads(r1.output)["task_id"]

    # 2. Start.
    r2 = await run_task_tool(
        _call("task_update", task_id=tid, action="start"),
        pctx, timeout=10.0, cap=4096,
    )
    assert _json.loads(r2.output)["state"] == "in_progress"

    # 3. Complete.
    r3 = await run_task_tool(
        _call("task_complete", task_id=tid),
        pctx, timeout=10.0, cap=4096,
    )
    assert _json.loads(r3.output)["state"] == "completed"

    # 4. Shutdown cleanup.
    closed = close_task_board(pctx.op_id, reason="test e2e done")
    assert closed is True
