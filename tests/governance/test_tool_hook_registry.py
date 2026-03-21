# tests/governance/test_tool_hook_registry.py
"""ToolCallHookRegistry: per-tool pre/post interception hooks (GAP 1)."""
from __future__ import annotations

import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Convenience: run coroutine synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# test_empty_registry_allows_all
# ---------------------------------------------------------------------------

def test_empty_registry_allows_all():
    """No hooks registered — run_pre returns ALLOW for any tool/input."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    registry = ToolCallHookRegistry()
    decision = _run(registry.run_pre("edit", {"file": "backend/foo.py"}))
    assert decision == HookDecision.ALLOW


# ---------------------------------------------------------------------------
# test_register_pre_hook_called
# ---------------------------------------------------------------------------

def test_register_pre_hook_called():
    """Registered pre-hook gets called with tool_name and tool_input."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    calls = []

    async def my_hook(tool_name: str, tool_input: dict) -> HookDecision:
        calls.append((tool_name, tool_input))
        return HookDecision.ALLOW

    registry = ToolCallHookRegistry()
    registry.register_pre("edit", None, my_hook)

    decision = _run(registry.run_pre("edit", {"file": "backend/foo.py"}))
    assert decision == HookDecision.ALLOW
    assert len(calls) == 1
    assert calls[0][0] == "edit"
    assert calls[0][1]["file"] == "backend/foo.py"


# ---------------------------------------------------------------------------
# test_pre_hook_block_stops_execution
# ---------------------------------------------------------------------------

def test_pre_hook_block_stops_execution():
    """BLOCK from first hook stops subsequent hooks from running."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    second_called = []

    async def blocker(tool_name: str, tool_input: dict) -> HookDecision:
        return HookDecision.BLOCK

    async def second(tool_name: str, tool_input: dict) -> HookDecision:
        second_called.append(True)
        return HookDecision.ALLOW

    registry = ToolCallHookRegistry()
    registry.register_pre("edit", None, blocker)
    registry.register_pre("edit", None, second)

    decision = _run(registry.run_pre("edit", {"file": "backend/foo.py"}))
    assert decision == HookDecision.BLOCK
    assert len(second_called) == 0, "second hook must NOT be called after BLOCK"


# ---------------------------------------------------------------------------
# test_post_hook_called_with_result
# ---------------------------------------------------------------------------

def test_post_hook_called_with_result():
    """Post-hook is called with tool_name, tool_input, and result."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        ToolCallHookRegistry,
    )

    calls = []

    async def audit(tool_name: str, tool_input: dict, result: object) -> None:
        calls.append((tool_name, tool_input, result))

    registry = ToolCallHookRegistry()
    registry.register_post("edit", None, audit)

    _run(registry.run_post("edit", {"file": "backend/foo.py"}, result="ok"))
    assert len(calls) == 1
    assert calls[0][0] == "edit"
    assert calls[0][2] == "ok"


# ---------------------------------------------------------------------------
# test_pattern_filtering
# ---------------------------------------------------------------------------

def test_pattern_filtering():
    """Hook with **/.env* pattern matches .env files but not other files."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    blocked_files = []

    async def env_guard(tool_name: str, tool_input: dict) -> HookDecision:
        blocked_files.append(tool_input.get("file"))
        return HookDecision.BLOCK

    registry = ToolCallHookRegistry()
    registry.register_pre("edit", "**/.env*", env_guard)

    # Should BLOCK .env files
    d1 = _run(registry.run_pre("edit", {"file": "/project/.env"}))
    assert d1 == HookDecision.BLOCK

    d2 = _run(registry.run_pre("edit", {"file": "/project/.env.local"}))
    assert d2 == HookDecision.BLOCK

    # Should ALLOW non-.env files
    d3 = _run(registry.run_pre("edit", {"file": "/project/backend/foo.py"}))
    assert d3 == HookDecision.ALLOW

    assert len(blocked_files) == 2


# ---------------------------------------------------------------------------
# test_hook_exception_is_swallowed
# ---------------------------------------------------------------------------

def test_hook_exception_is_swallowed():
    """Broken pre-hook doesn't crash; registry fails-open returning ALLOW."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    async def broken_hook(tool_name: str, tool_input: dict) -> HookDecision:
        raise RuntimeError("hook exploded")

    registry = ToolCallHookRegistry()
    registry.register_pre("edit", None, broken_hook)

    # Must not raise; must return ALLOW (fail-open)
    decision = _run(registry.run_pre("edit", {"file": "backend/foo.py"}))
    assert decision == HookDecision.ALLOW


# ---------------------------------------------------------------------------
# test_load_from_yaml
# ---------------------------------------------------------------------------

def test_load_from_yaml(tmp_path: Path):
    """from_yaml() loads pre/post hooks from YAML config; pre block works."""
    from backend.core.ouroboros.governance.tool_hook_registry import (
        HookDecision,
        ToolCallHookRegistry,
    )

    # Write a minimal YAML config with one blocking pre-hook for .env files
    config = {
        "hooks": [
            {
                "tool": "edit",
                "event": "pre",
                "pattern": "**/.env*",
                "action": "block",
                "reason": "protect env files",
            }
        ]
    }
    config_path = tmp_path / "hooks.yaml"
    config_path.write_text(yaml.dump(config))

    registry = ToolCallHookRegistry.from_yaml(str(config_path))

    # .env file must be blocked
    d1 = _run(registry.run_pre("edit", {"file": "/project/.env"}))
    assert d1 == HookDecision.BLOCK

    # other files pass through
    d2 = _run(registry.run_pre("edit", {"file": "/project/main.py"}))
    assert d2 == HookDecision.ALLOW


# ---------------------------------------------------------------------------
# test_change_engine_references_hook_registry
# ---------------------------------------------------------------------------

def test_change_engine_references_hook_registry():
    """Structural: ChangeEngine.__init__ accepts tool_hook_registry param
    and ChangeEngine.execute references it (hook_registry wired in)."""
    import inspect
    from backend.core.ouroboros.governance.change_engine import ChangeEngine

    sig = inspect.signature(ChangeEngine.__init__)
    assert "tool_hook_registry" in sig.parameters, (
        "ChangeEngine.__init__ must accept tool_hook_registry parameter"
    )

    # Verify the attribute is stored and accessible
    # (we can't instantiate without a real ledger, but we can check the source)
    import inspect as _inspect
    src = _inspect.getsource(ChangeEngine.execute)
    assert "tool_hook_registry" in src or "_tool_hook_registry" in src, (
        "ChangeEngine.execute must reference the hook registry"
    )
