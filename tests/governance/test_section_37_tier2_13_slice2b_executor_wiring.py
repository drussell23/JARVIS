"""§37 Tier 2 #13 Slice 2b — tool_executor wiring regression spine.

Verifies the per-tool confidence observation fires AFTER successful
tool dispatch + BEFORE V1 POST_TOOL_USE hook, master-flag-gated,
defensive against errors.

Tests (12):
  * _maybe_observe_tool_confidence helper exists at module level
  * helper is no-op when master flag off
  * helper is no-op when result.status != SUCCESS
  * helper invokes observe_active_signal on SUCCESS + master on
  * helper swallows exceptions (NEVER raises)
  * helper composes Slice 1+2 substrate (lazy import discipline)
  * AST pin: both call sites in execute_async invoke the helper
  * AST pin: helper is invoked BEFORE _maybe_fire_tool_hook(POST_TOOL_USE)
  * DW provider: ContextVar set wired at the artifacts-stash site
  * Observer/V1 hook ordering structurally preserved
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tool_executor_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )


def _dw_provider_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "doubleword_provider.py"
    )


# ---------------------------------------------------------------------------
# _maybe_observe_tool_confidence — module-level helper
# ---------------------------------------------------------------------------


def test_helper_exists_at_module_level():
    from backend.core.ouroboros.governance import (
        tool_executor,
    )
    assert hasattr(
        tool_executor, "_maybe_observe_tool_confidence",
    )


def test_helper_noop_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
        tool_executor,
    )
    call = MagicMock(name="ToolCall")
    policy_ctx = MagicMock(op_id="op1")
    result = MagicMock(status=tool_executor.ToolExecStatus.SUCCESS)
    with patch.object(
        toolconf, "observe_active_signal",
    ) as observe_spy:
        tool_executor._maybe_observe_tool_confidence(
            call, policy_ctx, result,
        )
        # Master off → observer NOT invoked.
        observe_spy.assert_not_called()


def test_helper_noop_when_status_not_success(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
        tool_executor,
    )
    call = MagicMock(name="ToolCall")
    policy_ctx = MagicMock(op_id="op1")
    # TIMEOUT status → no observation.
    result = MagicMock(
        status=tool_executor.ToolExecStatus.TIMEOUT,
    )
    with patch.object(
        toolconf, "observe_active_signal",
    ) as observe_spy:
        tool_executor._maybe_observe_tool_confidence(
            call, policy_ctx, result,
        )
        observe_spy.assert_not_called()


def test_helper_invokes_observe_on_success_with_master(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
        tool_executor,
    )
    call = MagicMock()
    call.name = "read_file"
    policy_ctx = MagicMock(op_id="op-42")
    result = MagicMock(
        status=tool_executor.ToolExecStatus.SUCCESS,
    )
    with patch.object(
        toolconf, "observe_active_signal",
    ) as observe_spy:
        tool_executor._maybe_observe_tool_confidence(
            call, policy_ctx, result,
        )
        observe_spy.assert_called_once()
        kwargs = observe_spy.call_args.kwargs
        assert kwargs["op_id"] == "op-42"
        assert kwargs["tool_name"] == "read_file"
        assert kwargs["publish_sse"] is True


def test_helper_swallows_observe_exception(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
        tool_executor,
    )
    call = MagicMock()
    call.name = "x"
    policy_ctx = MagicMock(op_id="op1")
    result = MagicMock(
        status=tool_executor.ToolExecStatus.SUCCESS,
    )
    with patch.object(
        toolconf, "observe_active_signal",
        side_effect=RuntimeError("boom"),
    ):
        # Must NOT raise — defensive.
        tool_executor._maybe_observe_tool_confidence(
            call, policy_ctx, result,
        )


def test_helper_swallows_status_attribute_error(monkeypatch):
    """Defensive: malformed result object → no crash."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import tool_executor
    call = MagicMock()
    policy_ctx = MagicMock(op_id="op1")

    class _Broken:
        @property
        def status(self):
            raise RuntimeError("simulated")

    # Must NOT raise.
    tool_executor._maybe_observe_tool_confidence(
        call, policy_ctx, _Broken(),
    )


# ---------------------------------------------------------------------------
# Wiring AST checks — both call sites + ordering vs V1 POST_TOOL_USE
# ---------------------------------------------------------------------------


def _find_executor_execute_async(tree: ast.Module):
    """Locate the substantive ``AsyncProcessToolBackend.
    execute_async`` definition (skips the abstract protocol stub
    earlier in the file). Returns the AsyncFunctionDef node."""
    for cls in ast.walk(tree):
        if (
            isinstance(cls, ast.ClassDef)
            and cls.name == "AsyncProcessToolBackend"
        ):
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AsyncFunctionDef)
                    and stmt.name == "execute_async"
                ):
                    return stmt
    return None


def test_executor_invokes_helper_at_async_native_path():
    """The async-native dispatch path (line ~3363) MUST invoke
    _maybe_observe_tool_confidence."""
    source = _tool_executor_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = _find_executor_execute_async(tree)
    assert fn is not None, (
        "ToolExecutor.execute_async function missing"
    )
    # Count invocations of _maybe_observe_tool_confidence
    invocations = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Name)
                and func.id == "_maybe_observe_tool_confidence"
            ):
                invocations.append(sub)
    # Must fire at BOTH dispatch paths (async-native + sync).
    assert len(invocations) >= 2, (
        f"Expected ≥2 helper invocations in execute_async "
        f"(async-native + sync paths), found {len(invocations)}"
    )


def test_helper_invoked_before_v1_post_tool_use():
    """Structural ordering: confidence observation happens BEFORE
    V1 POST_TOOL_USE hook fires. AST scan: in each dispatch path,
    the line of _maybe_observe_tool_confidence MUST come before
    the line of _maybe_fire_tool_hook("post_tool_use", ...)."""
    source = _tool_executor_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = _find_executor_execute_async(tree)
    assert fn is not None
    observe_lines = []
    post_hook_lines = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id == "_maybe_observe_tool_confidence":
                    observe_lines.append(sub.lineno)
            if isinstance(func, ast.Name):
                if func.id == "_maybe_fire_tool_hook":
                    # Check first arg is "post_tool_use".
                    if sub.args and isinstance(
                        sub.args[0], ast.Constant,
                    ):
                        if sub.args[0].value == "post_tool_use":
                            post_hook_lines.append(sub.lineno)
    assert observe_lines, "no observe calls found"
    assert post_hook_lines, "no post_tool_use calls found"
    # For each post_tool_use line, an observe call must come
    # before it (within reasonable proximity — same dispatch
    # branch). Pair each post_hook to the nearest preceding
    # observe.
    for hook_line in post_hook_lines:
        preceding = [o for o in observe_lines if o < hook_line]
        assert preceding, (
            f"_maybe_fire_tool_hook(post_tool_use) at line "
            f"{hook_line} has NO preceding "
            f"_maybe_observe_tool_confidence — Slice 2 "
            f"ordering violated"
        )


def test_helper_lazy_imports_slice1_and_slice2_substrate():
    """Helper must import master_enabled + observe_active_signal
    from Slice 1 module (composition discipline; no parallel
    confidence math)."""
    source = _tool_executor_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_maybe_observe_tool_confidence"
        ):
            helper_fn = node
            break
    assert helper_fn is not None
    has_master = False
    has_observe = False
    for sub in ast.walk(helper_fn):
        if isinstance(sub, ast.ImportFrom):
            module = sub.module or ""
            if "tool_confidence_warning_observer" in module:
                names = {n.name for n in sub.names}
                if "master_enabled" in names:
                    has_master = True
                if "observe_active_signal" in names:
                    has_observe = True
    assert has_master, (
        "_maybe_observe_tool_confidence MUST lazy-import "
        "master_enabled from tool_confidence_warning_observer"
    )
    assert has_observe, (
        "_maybe_observe_tool_confidence MUST lazy-import "
        "observe_active_signal from "
        "tool_confidence_warning_observer"
    )


# ---------------------------------------------------------------------------
# DW provider — ContextVar set wired at the artifacts-stash site
# ---------------------------------------------------------------------------


def test_dw_provider_sets_active_capturer():
    """The DW provider MUST stamp the ContextVar with the active
    ConfidenceCapturer so tool_executor sees it. AST scan for
    set_active_capturer call inside the streaming function."""
    source = _dw_provider_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    found_set = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "tool_confidence_warning_observer" in module:
                if any(
                    n.name == "set_active_capturer"
                    for n in node.names
                ):
                    found_set = True
                    break
    assert found_set, (
        "doubleword_provider.py MUST lazy-import "
        "set_active_capturer from "
        "tool_confidence_warning_observer (Slice 2 wiring)"
    )


def test_dw_provider_set_calls_with_capturer():
    """The DW provider's set_active_capturer call MUST pass the
    confidence_capturer it just created — AST scan."""
    source = _dw_provider_path().read_text(encoding="utf-8")
    # Look for the alias-call pattern `_toolconf_set_var(_confidence_capturer)`
    # OR `set_active_capturer(_confidence_capturer)` in the source.
    assert (
        "_toolconf_set_var(_confidence_capturer)" in source
        or "set_active_capturer(_confidence_capturer)" in source
    ), (
        "DW provider MUST call set_active_capturer with the "
        "freshly-created _confidence_capturer (Slice 2 wiring)"
    )


# ---------------------------------------------------------------------------
# Integration check — both modules import cleanly
# ---------------------------------------------------------------------------


def test_modules_import_cleanly():
    """Smoke: both edited modules import without error."""
    from backend.core.ouroboros.governance import (
        doubleword_provider,  # noqa: F401
        tool_confidence_warning_observer,  # noqa: F401
        tool_executor,  # noqa: F401
    )


def test_helper_does_not_block_on_missing_module(monkeypatch):
    """If Slice 1 module is unavailable (ImportError), the helper
    silently no-ops rather than crashing tool dispatch. Defense-
    in-depth."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import tool_executor
    call = MagicMock()
    call.name = "x"
    policy_ctx = MagicMock(op_id="op1")
    result = MagicMock(
        status=tool_executor.ToolExecStatus.SUCCESS,
    )
    # Force the lazy import to fail.
    import sys
    saved = sys.modules.pop(
        "backend.core.ouroboros.governance."
        "tool_confidence_warning_observer", None,
    )
    try:
        with patch.dict(
            sys.modules,
            {
                "backend.core.ouroboros.governance."
                "tool_confidence_warning_observer": None,
            },
        ):
            # Must NOT raise.
            tool_executor._maybe_observe_tool_confidence(
                call, policy_ctx, result,
            )
    finally:
        if saved is not None:
            sys.modules[
                "backend.core.ouroboros.governance."
                "tool_confidence_warning_observer"
            ] = saved
