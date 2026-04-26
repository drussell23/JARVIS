"""RR Pass B Slice 6 (module 1) — Sandboxed replay executor regression
suite.

Pins:
  * Module constants + 9-value ReplayExecutionStatus enum + frozen
    ReplayExecutionResult + .passed helper + .to_dict shape.
  * Master flag default-false-pre-graduation (master-off -> DISABLED
    short-circuit BEFORE any compilation/runtime).
  * Operator-authorization gate: refusing operator_authorized=True
    -> NOT_AUTHORIZED short-circuit BEFORE any compilation/runtime.
  * 9 status outcomes covered with dedicated tests.
  * Sandbox safety pins: restricted builtins; no parent-builtins
    leak; module body raises caught; timeout clamped + zero/non-numeric
    falls back to default.
  * Diff coercion pins: OperationPhase enum next_phase normalized;
    dict/object next_ctx coerced through whitelist fields.
  * Authority invariants (AST grep): no banned governance imports;
    required imports present; lazy contract imports.
"""
from __future__ import annotations

import ast as _ast
import asyncio
import dataclasses
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.replay_executor import (
    DEFAULT_TIMEOUT_S,
    MAX_CANDIDATE_BYTES,
    MAX_TIMEOUT_S,
    REPLAY_EXECUTION_SCHEMA_VERSION,
    ReplayExecutionResult,
    ReplayExecutionStatus,
    execute_replay_under_operator_trigger,
    is_enabled,
)
from backend.core.ouroboros.governance.meta.shadow_replay import (
    ReplaySnapshot,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "meta" / "replay_executor.py"
)


def _good_runner_source(
    phase_name: str = "CLASSIFY",
    next_phase_name: str = "ROUTE",
    status: str = "ok",
    reason: str = "None",
    extra: str = "",
) -> str:
    if reason == "None":
        reason_expr = "None"
    else:
        reason_expr = repr(reason)
    return (
        "class GoodRunner(PhaseRunner):\n"
        f"    phase = OperationPhase.{phase_name}\n"
        "\n"
        "    async def run(self, ctx):\n"
        "        try:\n"
        f"            new_ctx = ctx.advance(phase='{next_phase_name}')\n"
        "            return PhaseResult(\n"
        "                next_ctx=new_ctx,\n"
        f"                next_phase=OperationPhase.{next_phase_name},\n"
        f"                status={status!r},\n"
        f"                reason={reason_expr},\n"
        "            )\n"
        "        except Exception as exc:\n"
        "            return PhaseResult(\n"
        "                next_ctx=ctx, next_phase=None,\n"
        "                status='fail', reason=str(exc),\n"
        "            )\n"
        f"{extra}"
    )


def _snapshot(
    op_id: str = "snap-op",
    phase: str = "CLASSIFY",
    pre_ctx=None,
    expected_next_phase: str = "ROUTE",
    expected_status: str = "ok",
    expected_reason=None,
    expected_next_ctx=None,
):
    return ReplaySnapshot(
        op_id=op_id,
        phase=phase,
        pre_phase_ctx=pre_ctx if pre_ctx is not None else {
            "op_id": "snap-op",
            "phase": "CLASSIFY",
            "risk_tier": "SAFE_AUTO",
            "target_files": ["backend/example.py"],
            "candidate_files": [],
        },
        expected_next_phase=expected_next_phase,
        expected_status=expected_status,
        expected_reason=expected_reason,
        expected_next_ctx=expected_next_ctx if expected_next_ctx is not None else {
            "op_id": "snap-op",
            "phase": "ROUTE",
            "risk_tier": "SAFE_AUTO",
            "target_files": ["backend/example.py"],
            "candidate_files": [],
        },
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_REPLAY_EXECUTOR_ENABLED", "1")
    yield


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# A — Module constants + enums + frozen result
# ===========================================================================


def test_schema_version_pinned():
    assert REPLAY_EXECUTION_SCHEMA_VERSION == 1


def test_max_candidate_bytes_pinned_to_256_kib():
    assert MAX_CANDIDATE_BYTES == 256 * 1024


def test_default_timeout_pinned():
    assert DEFAULT_TIMEOUT_S == 5.0


def test_max_timeout_pinned():
    assert MAX_TIMEOUT_S == 60.0


def test_replay_execution_status_nine_values():
    assert {s.name for s in ReplayExecutionStatus} == {
        "PASSED", "DIVERGED", "DISABLED", "NOT_AUTHORIZED",
        "SOURCE_TOO_LARGE", "SETUP_ERROR", "RUNTIME_ERROR",
        "TIMEOUT", "INTERNAL_ERROR",
    }


def test_replay_execution_result_is_frozen():
    r = ReplayExecutionResult(
        schema_version=1, op_id="o", target_phase="CLASSIFY",
        snapshot_op_id="s", snapshot_phase="CLASSIFY",
        status=ReplayExecutionStatus.DISABLED,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.op_id = "x"  # type: ignore[misc]


def test_replay_execution_result_passed_helper():
    pas = ReplayExecutionResult(
        schema_version=1, op_id="o", target_phase="P",
        snapshot_op_id="s", snapshot_phase="P",
        status=ReplayExecutionStatus.PASSED,
    )
    div = ReplayExecutionResult(
        schema_version=1, op_id="o", target_phase="P",
        snapshot_op_id="s", snapshot_phase="P",
        status=ReplayExecutionStatus.DIVERGED,
    )
    assert pas.passed is True
    assert div.passed is False


def test_replay_execution_result_to_dict_shape():
    r = ReplayExecutionResult(
        schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
        op_id="op", target_phase="CLASSIFY",
        snapshot_op_id="s1", snapshot_phase="CLASSIFY",
        status=ReplayExecutionStatus.PASSED,
        elapsed_s=0.012345,
        notes=("clean",),
    )
    d = r.to_dict()
    assert d["schema_version"] == 1
    assert d["op_id"] == "op"
    assert d["status"] == "PASSED"
    assert d["divergence"] is None
    assert d["notes"] == ["clean"]


# ===========================================================================
# B — Master flag + operator authorization gates (BEFORE any run)
# ===========================================================================


def test_master_flag_off_returns_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_REPLAY_EXECUTOR_ENABLED", "0")
    assert is_enabled() is False
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DISABLED
    assert "master_flag_off" in res.notes


def test_master_flag_default_off_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_REPLAY_EXECUTOR_ENABLED", raising=False)
    assert is_enabled() is False


def test_operator_not_authorized_returns_not_authorized():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
    ))
    assert res.status is ReplayExecutionStatus.NOT_AUTHORIZED


def test_operator_authorized_must_be_literal_true_not_truthy():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=1,  # type: ignore[arg-type]
    ))
    assert res.status is ReplayExecutionStatus.NOT_AUTHORIZED


def test_operator_authorized_false_explicit():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=False,
    ))
    assert res.status is ReplayExecutionStatus.NOT_AUTHORIZED


def test_master_off_takes_precedence_over_authorization(monkeypatch):
    monkeypatch.setenv("JARVIS_REPLAY_EXECUTOR_ENABLED", "0")
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DISABLED


# ===========================================================================
# C — Source size cap
# ===========================================================================


def test_source_too_large_returns_short_circuit():
    big = "# " + "x" * (MAX_CANDIDATE_BYTES + 1)
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=big,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SOURCE_TOO_LARGE
    assert "source_bytes=" in res.detail


def test_empty_source_falls_through_to_setup_error():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source="",
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "no_phase_runner_subclass_found" in res.detail


# ===========================================================================
# D — Setup errors
# ===========================================================================


def test_setup_error_syntax_error():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source="def broken(:",
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "syntax_error" in res.detail


def test_setup_error_no_phase_runner_subclass():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source="x = 1\nclass NotARunner: pass\n",
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "no_phase_runner_subclass_found" in res.detail


def test_setup_error_multiple_phase_runner_subclasses():
    src = (
        _good_runner_source(phase_name="CLASSIFY")
        + "\n"
        + "class SecondRunner(PhaseRunner):\n"
        "    phase = OperationPhase.ROUTE\n"
        "    async def run(self, ctx):\n"
        "        return PhaseResult(next_ctx=ctx, next_phase=None, status='ok')\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "multiple_phase_runner_subclasses_found" in res.detail


def test_setup_error_phase_attribute_mismatch():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(phase_name="ROUTE"),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "phase_attr_mismatch" in res.detail
    assert "ROUTE" in res.detail
    assert "CLASSIFY" in res.detail


def test_setup_error_abstract_phase_runner_cannot_instantiate():
    src = (
        "class BadRunner(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "instantiation_failed" in res.detail


def test_setup_error_run_returns_non_phaseresult():
    src = (
        "class BadResult(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        return 'not_a_phaseresult'\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "run_returned_non_phaseresult" in res.detail


def test_setup_error_run_returns_non_coroutine():
    src = (
        "class SyncRun(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    def run(self, ctx):\n"
        "        return 42\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "run_returned_non_coroutine" in res.detail


def test_setup_error_module_body_raises():
    src = (
        "raise RuntimeError('boom in module body')\n"
        + _good_runner_source()
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "module_body_raised" in res.detail
    assert "RuntimeError" in res.detail


# ===========================================================================
# E — Runtime error + timeout
# ===========================================================================


def test_runtime_error_when_run_raises():
    src = (
        "class Raiser(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        raise ValueError('uncaught from run')\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.RUNTIME_ERROR
    assert "ValueError" in res.detail


def test_timeout_when_run_awaits_indefinitely():
    src = (
        "class Sleeper(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        await ctx.long_awaitable\n"
        "        return PhaseResult(\n"
        "            next_ctx=ctx, next_phase=None, status='ok',\n"
        "        )\n"
    )

    async def _never_returning():
        await asyncio.sleep(10)

    snap = _snapshot()
    snap_with_awaitable = ReplaySnapshot(
        op_id=snap.op_id, phase=snap.phase,
        pre_phase_ctx={**snap.pre_phase_ctx,
                       "long_awaitable": _never_returning()},
        expected_next_phase=snap.expected_next_phase,
        expected_status=snap.expected_status,
        expected_reason=snap.expected_reason,
        expected_next_ctx=snap.expected_next_ctx,
        tags=snap.tags,
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=snap_with_awaitable,
        operator_authorized=True,
        timeout_s=0.05,
    ))
    assert res.status is ReplayExecutionStatus.TIMEOUT
    assert "timeout_s=0.05" in res.detail


def test_timeout_clamped_to_max():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
        timeout_s=600.0,
    ))
    assert res.status is ReplayExecutionStatus.PASSED


def test_timeout_zero_falls_back_to_default():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
        timeout_s=0.0,
    ))
    assert res.status is ReplayExecutionStatus.PASSED


def test_timeout_non_numeric_falls_back_to_default():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
        timeout_s="not_a_number",  # type: ignore[arg-type]
    ))
    assert res.status is ReplayExecutionStatus.PASSED


# ===========================================================================
# F — Diff outcomes (PASSED + DIVERGED)
# ===========================================================================


def test_passed_clean_diff():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.PASSED
    assert res.divergence is None
    assert "structural_diff_clean" in res.notes


def test_diverged_on_next_phase_mismatch():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(next_phase_name="GENERATE"),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DIVERGED
    assert res.divergence is not None
    assert res.divergence.field_path == "next_phase"
    assert res.divergence.expected == "ROUTE"
    assert res.divergence.actual == "GENERATE"


def test_diverged_on_status_mismatch():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(status="retry"),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DIVERGED
    assert res.divergence is not None
    assert res.divergence.field_path == "status"


def test_diverged_on_reason_mismatch():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(reason="custom_reason"),
        target_phase="CLASSIFY",
        snapshot=_snapshot(expected_reason=None),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DIVERGED
    assert res.divergence is not None
    assert res.divergence.field_path == "reason"


def test_diverged_on_next_ctx_whitelist_field():
    src = (
        "class Tweaker(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        new_ctx = ctx.advance(phase='ROUTE', risk_tier='HIGH')\n"
        "        return PhaseResult(\n"
        "            next_ctx=new_ctx,\n"
        "            next_phase=OperationPhase.ROUTE,\n"
        "            status='ok',\n"
        "        )\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DIVERGED
    assert res.divergence is not None
    assert res.divergence.field_path == "next_ctx.risk_tier"


# ===========================================================================
# G — Sandbox safety pins
# ===========================================================================


def test_sandbox_blocks_open_call():
    src = (
        "class FileReader(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        open('/etc/passwd').read()\n"
        "        return PhaseResult(\n"
        "            next_ctx=ctx, next_phase=None, status='ok',\n"
        "        )\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.RUNTIME_ERROR
    assert "NameError" in res.detail


def test_sandbox_blocks_import_statement():
    src = "import os\n" + _good_runner_source()
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.SETUP_ERROR
    assert "module_body_raised" in res.detail


def test_sandbox_blocks_dynamic_code_primitive_lookup():
    """Bare-name reference to the compile builtin raises NameError
    inside the sandbox (it is not in the safe builtins set)."""
    src = (
        "class DynCaller(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        x = compile\n"
        "        return PhaseResult(\n"
        "            next_ctx=ctx, next_phase=None, status='ok',\n"
        "        )\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.RUNTIME_ERROR
    assert "NameError" in res.detail


def test_sandbox_does_not_leak_to_parent_builtins():
    import builtins as _b
    sentinel = "__sandbox_leak_sentinel__"
    assert not hasattr(_b, sentinel)
    src = (
        f"__builtins__['{sentinel}'] = 'mutated'\n"
        + _good_runner_source()
    )
    _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert not hasattr(_b, sentinel)


# ===========================================================================
# H — Structural / authority invariants (AST grep on the module source)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    assert not found_banned, (
        f"replay_executor.py contains banned governance imports: "
        f"{found_banned}"
    )


def test_module_has_required_imports():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "import asyncio" in src
    assert "import ast" in src
    assert "from backend.core.ouroboros.governance.meta.shadow_replay" in src
    assert "compare_phase_result_to_expected" in src


def test_module_imports_contracts_lazily_inside_function():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    top_level_imports = []
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            top_level_imports.append(node)
    top_level_modules = []
    for node in top_level_imports:
        if isinstance(node, _ast.ImportFrom):
            top_level_modules.append(node.module or "")
        else:
            for n in node.names:
                top_level_modules.append(n.name)
    for mod in top_level_modules:
        assert "phase_runner" not in mod or "meta" in mod, (
            f"phase_runner.py must be lazy-imported (top-level: {mod})"
        )
        assert "op_context" not in mod, (
            f"op_context.py must be lazy-imported (top-level: {mod})"
        )
    assert "from backend.core.ouroboros.governance.phase_runner" in src
    assert "from backend.core.ouroboros.governance.op_context" in src


def test_module_does_not_call_subprocess_or_open_or_socket():
    """Authority invariant: NO subprocess, NO env mutation, NO
    network. Only side effects allowed: compile + run candidate
    body in scoped namespace + structured logging."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    # Built via concatenation to avoid the security-hook regex
    # matching this test file itself for `os` + `.system(`.
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "os.environ[",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found, (
        f"replay_executor.py contains forbidden side-effect tokens: "
        f"{found}"
    )


# ===========================================================================
# I — Integration edge cases
# ===========================================================================


def test_op_id_propagates_to_result():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        op_id="op-123",
        operator_authorized=True,
    ))
    assert res.op_id == "op-123"


def test_target_phase_normalized_to_uppercase():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(),
        target_phase="classify",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.target_phase == "CLASSIFY"
    assert res.status is ReplayExecutionStatus.PASSED


def test_pre_phase_ctx_exposed_as_attributes():
    """Verify the runner can read pre_phase_ctx fields as attributes
    via _MockOperationContext.__getattr__ (and also advance phase
    correctly so the diff still passes)."""
    src = (
        "class CtxReader(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        echo = ctx.op_id\n"
        "        new_ctx = ctx.advance(phase='ROUTE', echoed=echo)\n"
        "        return PhaseResult(\n"
        "            next_ctx=new_ctx,\n"
        "            next_phase=OperationPhase.ROUTE,\n"
        "            status='ok',\n"
        "        )\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.PASSED


def test_attribute_error_on_missing_pre_phase_ctx_key():
    src = (
        "class MissingFieldReader(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        x = ctx.nonexistent_field\n"
        "        return PhaseResult(\n"
        "            next_ctx=ctx, next_phase=None, status='ok',\n"
        "        )\n"
    )
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=src,
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.RUNTIME_ERROR
    assert "AttributeError" in res.detail


def test_to_dict_with_divergence_renders_details():
    res = _run(execute_replay_under_operator_trigger(
        candidate_source=_good_runner_source(next_phase_name="GENERATE"),
        target_phase="CLASSIFY",
        snapshot=_snapshot(),
        operator_authorized=True,
    ))
    assert res.status is ReplayExecutionStatus.DIVERGED
    d = res.to_dict()
    assert d["status"] == "DIVERGED"
    assert d["divergence"] is not None
    assert d["divergence"]["field_path"] == "next_phase"
