"""§3.6.2 vector #6 producer-loop wiring (Slice C).

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Closes the producer-side gap on §3.6.2 vector #6 engineering closure:
the Phase9Orchestrator interaction matrix (`.jarvis/graduation
_interaction_matrix.jsonl`) shipped earlier today is read by `/phase9
partners`, but had no canonical writer. After this slice,
`scripts/live_fire_graduation_soak.py cmd_run` calls `record_session_
flags(session_id=..., flags_enabled=...)` after a successful soak —
flag-set composed from the SAME `get_dependencies(target_flag)` source
the harness uses to build the subprocess env (single source of truth).

Operator-binding decision baked in: when
`JARVIS_PHASE9_ORCHESTRATOR_ENABLED` is OFF, the wiring surfaces a
structured operator-visible message rather than silently no-op-ing
(per binding: "do not silently no-op in production and wonder why
the matrix stays empty").

Coverage (~14 tests):
  * AST scan: cmd_run invokes _record_phase9_interaction_matrix
  * AST scan: _record_phase9_interaction_matrix lazy-imports both
    Phase9Orchestrator + get_dependencies (composition discipline)
  * AST scan: helper has master-flag-off branch with operator-visible
    message (the "do not silently no-op" binding)
  * AST scan: helper NEVER raises (catch-all defensive try/except)
  * Behavioral: helper writes to matrix when master flag on
  * Behavioral: helper prints diagnostic when master flag off
  * Behavioral: helper composes get_dependencies (subprocess env
    parity)
  * Cron entry point includes JARVIS_PHASE9_ORCHESTRATOR_ENABLED
  * Wrapper script exports JARVIS_PHASE9_ORCHESTRATOR_ENABLED
  * Crontab example includes JARVIS_PHASE9_ORCHESTRATOR_ENABLED
  * --once path includes JARVIS_PHASE9_ORCHESTRATOR_ENABLED
  * Single-source-of-truth pin (existing test) recognizes the
    new var
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _soak_script_path() -> Path:
    return _repo_root() / "scripts/live_fire_graduation_soak.py"


# ---------------------------------------------------------------------------
# AST: producer wiring discipline
# ---------------------------------------------------------------------------


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_cmd_run_invokes_record_helper():
    """`cmd_run` MUST invoke the producer-loop wiring helper
    after a successful soak."""
    src = _soak_script_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "cmd_run")
    assert fn is not None
    found = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Name)
                and func.id == "_record_phase9_interaction_matrix"
            ):
                found = True
                break
    assert found, (
        "cmd_run MUST invoke "
        "_record_phase9_interaction_matrix after a soak"
    )


def test_helper_lazy_imports_substrate():
    """The helper MUST lazy-import both Phase9Orchestrator
    (consumer) AND get_dependencies (so flag-set parity with
    the harness's subprocess env). Single source of truth."""
    src = _soak_script_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_record_phase9_interaction_matrix")
    assert fn is not None
    has_orchestrator = False
    has_get_deps = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.ImportFrom):
            module = sub.module or ""
            if "phase9_orchestrator" in module:
                names = {n.name for n in sub.names}
                if (
                    "get_default_orchestrator" in names
                    and "master_enabled" in names
                ):
                    has_orchestrator = True
            if "live_fire_soak" in module:
                names = {n.name for n in sub.names}
                if "get_dependencies" in names:
                    has_get_deps = True
    assert has_orchestrator, (
        "Helper MUST lazy-import get_default_orchestrator + "
        "master_enabled from phase9_orchestrator"
    )
    assert has_get_deps, (
        "Helper MUST lazy-import get_dependencies from "
        "live_fire_soak (single source of truth for flag-set)"
    )


def test_helper_master_flag_off_branch_present():
    """Operator binding 2026-05-07: must NOT silently no-op
    when the master flag is off. Helper must have an explicit
    branch that prints an operator-visible diagnostic."""
    src = _soak_script_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_record_phase9_interaction_matrix")
    assert fn is not None
    # Walk for an `if not phase9_master_enabled():` shape.
    found_master_check = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.UnaryOp) and isinstance(
            sub.op, ast.Not,
        ):
            inner = sub.operand
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "phase9_master_enabled"
            ):
                found_master_check = True
                break
    assert found_master_check, (
        "Helper MUST have `if not phase9_master_enabled():` "
        "branch with operator-visible diagnostic — operator "
        "binding 2026-05-07 forbids silent no-op."
    )


def test_helper_has_defensive_catch_all():
    """NEVER-raises discipline: helper must have a catch-all
    `except Exception` so failures surface as operator-visible
    notes rather than crashing the run path."""
    src = _soak_script_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_record_phase9_interaction_matrix")
    assert fn is not None
    has_catch_all = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.ExceptHandler):
            exc_type = sub.type
            if (
                isinstance(exc_type, ast.Name)
                and exc_type.id == "Exception"
            ):
                has_catch_all = True
                break
    assert has_catch_all, (
        "Helper MUST have an `except Exception` catch-all"
    )


# ---------------------------------------------------------------------------
# Behavioral: end-to-end record path
# ---------------------------------------------------------------------------


def test_helper_records_when_master_enabled(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PHASE9_INTERACTION_MATRIX_PATH",
        str(tmp_path / "matrix.jsonl"),
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        reset_default_orchestrator_for_tests,
    )
    reset_default_orchestrator_for_tests()
    # Stub get_dependencies to return a known set.
    import backend.core.ouroboros.governance.graduation.live_fire_soak as live_fire
    monkeypatch.setattr(
        live_fire, "get_dependencies",
        lambda flag: frozenset({"DEP_A", "DEP_B"}),
    )
    # Import after monkeypatching for clean state.
    import importlib
    import scripts.live_fire_graduation_soak as soak_module
    importlib.reload(soak_module)
    soak_module._record_phase9_interaction_matrix(
        session_id="bt-test-1",
        target_flag="JARVIS_TARGET_X",
    )
    # Verify matrix file was written.
    matrix_path = tmp_path / "matrix.jsonl"
    assert matrix_path.is_file()
    content = matrix_path.read_text(encoding="utf-8")
    import json
    record = json.loads(content.strip().split("\n")[0])
    assert record["session_id"] == "bt-test-1"
    flags = set(record["flags"])
    # Target flag + 2 deps.
    assert "JARVIS_TARGET_X" in flags
    assert "DEP_A" in flags
    assert "DEP_B" in flags
    # Operator-visible confirmation message.
    captured = capsys.readouterr()
    assert "phase9-matrix" in captured.out
    assert "recorded" in captured.out
    reset_default_orchestrator_for_tests()


def test_helper_prints_diagnostic_when_master_off(
    monkeypatch, capsys,
):
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    import importlib
    import scripts.live_fire_graduation_soak as soak_module
    importlib.reload(soak_module)
    soak_module._record_phase9_interaction_matrix(
        session_id="bt-test-2",
        target_flag="JARVIS_TARGET_Y",
    )
    captured = capsys.readouterr()
    # Must surface the off-state diagnostic — operator binding.
    assert "phase9-matrix" in captured.out
    # Either "ENABLED=false" or "false" appears.
    assert "false" in captured.out
    # Must mention how to fix it.
    assert "/phase9" in captured.out


def test_helper_never_raises_on_broken_substrate(
    monkeypatch, capsys,
):
    """When something internal breaks (broken
    record_session_flags), helper surfaces a non-fatal
    operator-visible note rather than crashing."""
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    import importlib
    import scripts.live_fire_graduation_soak as soak_module
    importlib.reload(soak_module)
    # Patch the orchestrator to raise.
    from backend.core.ouroboros.governance import (
        phase9_orchestrator as p9o,
    )
    with patch.object(
        p9o, "get_default_orchestrator",
        side_effect=RuntimeError("simulated"),
    ):
        # Must NOT raise.
        soak_module._record_phase9_interaction_matrix(
            session_id="bt-test-3",
            target_flag="JARVIS_TARGET_Z",
        )
    captured = capsys.readouterr()
    assert "non-fatal" in captured.out


# ---------------------------------------------------------------------------
# Cron + wrapper + crontab example must include the new var
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", [
    "scripts/run_live_fire_graduation_soak.sh",
    "scripts/install_live_fire_soak_cron.sh",
    "scripts/crontab-live-fire.example",
])
def test_phase9_orchestrator_env_var_present(rel_path):
    """Single-source-of-truth pin: every Phase 9 entry point
    MUST set JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true so the
    interaction matrix populates as cadence runs."""
    text = (_repo_root() / rel_path).read_text(encoding="utf-8")
    assert "JARVIS_PHASE9_ORCHESTRATOR_ENABLED" in text


def test_install_script_install_block_includes_phase9_var():
    """The cron generator's --install block MUST include the
    new env var."""
    text = (
        _repo_root() / "scripts/install_live_fire_soak_cron.sh"
    ).read_text(encoding="utf-8")
    # Find the cron-line definition.
    install_section = text.split("$BEGIN_MARKER")[1].split(
        "$END_MARKER",
    )[0]
    assert (
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true"
        in install_section
    )


def test_install_script_once_block_includes_phase9_var():
    """The --once path MUST also include the new env var
    (mirrors cron entry per existing single-source-of-truth
    pin discipline)."""
    text = (
        _repo_root() / "scripts/install_live_fire_soak_cron.sh"
    ).read_text(encoding="utf-8")
    # The --once block contains the literal env vars
    # backslash-continued.
    assert (
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true \\"
        in text
    )


def test_canonical_pin_recognizes_new_var():
    """The single-source-of-truth pin in
    test_install_live_fire_soak_cron MUST include the new
    JARVIS_PHASE9_ORCHESTRATOR_ENABLED — proves the test
    discipline propagates. Parses the test source via AST
    so we walk the tuple literal instead of relying on
    find(')') which lands inside comments."""
    import ast as _ast
    pin_path = (
        _repo_root()
        / "tests/governance/test_install_live_fire_soak_cron.py"
    )
    text = pin_path.read_text(encoding="utf-8")
    tree = _ast.parse(text)
    found = False
    for node in _ast.walk(tree):
        if (
            isinstance(node, _ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], _ast.Name)
            and node.targets[0].id == "_REQUIRED_PHASE9_ENV_VARS"
        ):
            value = node.value
            if isinstance(value, _ast.Tuple):
                for elt in value.elts:
                    if (
                        isinstance(elt, _ast.Constant)
                        and elt.value
                        == "JARVIS_PHASE9_ORCHESTRATOR_ENABLED"
                    ):
                        found = True
                        break
    assert found, (
        "_REQUIRED_PHASE9_ENV_VARS tuple MUST include "
        "'JARVIS_PHASE9_ORCHESTRATOR_ENABLED'"
    )
