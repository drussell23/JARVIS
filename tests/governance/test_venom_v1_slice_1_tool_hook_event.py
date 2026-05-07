"""Venom V1 Slice 1 — ToolHookEvent enum + registry widen
regression spine.

Pins per operator binding 2026-05-06:

  * 6-value ToolHookEvent closed taxonomy bytes-pinned
  * LifecycleEvent (5 values) untouched — no taxonomy drift
  * Registry register() accepts BOTH LifecycleEvent and
    ToolHookEvent (single registry, two event taxonomies)
  * count_for_event / for_event accept either taxonomy
  * Garbage event types still raise InvalidHookError
  * Master flag JARVIS_VENOM_TOOL_HOOKS_ENABLED default-FALSE
    per §33.1 graduation contract
  * Phase-boundary hooks (LifecycleEvent.PRE_GENERATE etc.) and
    tool-boundary hooks (ToolHookEvent.PRE_TOOL_USE etc.) coexist
    without storage drift
  * AST: ToolHookEvent class declares exactly 6 members

Verifies (24 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_tool_hook_event_has_6_values():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    assert len(list(ToolHookEvent)) == 6


def test_tool_hook_event_values_bytes_pinned():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    assert {e.value for e in ToolHookEvent} == {
        "pre_tool_use",
        "post_tool_use",
        "pre_tool_use_failure",
        "post_tool_use_failure",
        "subagent_start",
        "subagent_stop",
    }


def test_lifecycle_event_unchanged():
    """V1 must not modify the existing LifecycleEvent taxonomy
    — phase-boundary surface stays at 5 values."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        LifecycleEvent,
    )
    assert len(list(LifecycleEvent)) == 5
    assert {e.value for e in LifecycleEvent} == {
        "pre_generate", "pre_apply", "post_apply",
        "post_verify", "on_operator_action",
    }


def test_hook_event_types_union():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookEventTypes, LifecycleEvent, ToolHookEvent,
    )
    assert isinstance(HookEventTypes, tuple)
    assert LifecycleEvent in HookEventTypes
    assert ToolHookEvent in HookEventTypes


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        venom_tool_hooks_enabled,
    )
    assert venom_tool_hooks_enabled() is False


def test_master_flag_truthy_values(monkeypatch):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        venom_tool_hooks_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(
            "JARVIS_VENOM_TOOL_HOOKS_ENABLED", v,
        )
        assert venom_tool_hooks_enabled() is True


def test_master_flag_falsy_values(monkeypatch):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        venom_tool_hooks_enabled,
    )
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_VENOM_TOOL_HOOKS_ENABLED", v,
        )
        assert venom_tool_hooks_enabled() is False


def test_master_flag_distinct_from_lifecycle_flag(monkeypatch):
    """The two surfaces have separate master flags so operators
    can adopt phase hooks without enabling tool hooks (or vice
    versa)."""
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_LIFECYCLE_HOOKS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        lifecycle_hooks_enabled, venom_tool_hooks_enabled,
    )
    # Lifecycle hooks default-true (graduated 2026-05-02);
    # Venom tool hooks default-false (§33.1).
    assert lifecycle_hooks_enabled() is True
    assert venom_tool_hooks_enabled() is False


# ---------------------------------------------------------------------------
# Registry widen — accepts both taxonomies
# ---------------------------------------------------------------------------


def _make_hook(name="test"):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, make_hook_result,
    )

    def _hook(ctx):
        return make_hook_result(
            name=name, outcome=HookOutcome.CONTINUE,
        )
    return _hook


def test_registry_accepts_lifecycle_event():
    """Existing path — phase-boundary registration still works."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        LifecycleEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    rec = reg.register(
        event=LifecycleEvent.PRE_GENERATE,
        hook=_make_hook("phase_hook"),
        name="phase_hook",
    )
    assert rec.event == LifecycleEvent.PRE_GENERATE
    assert reg.count_for_event(
        LifecycleEvent.PRE_GENERATE,
    ) == 1


def test_registry_accepts_tool_hook_event():
    """V1 path — per-tool-boundary registration."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    rec = reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("tool_hook"),
        name="tool_hook",
    )
    assert rec.event == ToolHookEvent.PRE_TOOL_USE
    assert reg.count_for_event(
        ToolHookEvent.PRE_TOOL_USE,
    ) == 1


def test_registry_two_taxonomies_coexist():
    """A registry holds both phase hooks AND tool hooks
    simultaneously without storage drift."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        LifecycleEvent, ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=LifecycleEvent.PRE_GENERATE,
        hook=_make_hook("phase"),
        name="phase",
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("tool"),
        name="tool",
    )
    assert reg.total_count() == 2
    assert reg.count_for_event(
        LifecycleEvent.PRE_GENERATE,
    ) == 1
    assert reg.count_for_event(
        ToolHookEvent.PRE_TOOL_USE,
    ) == 1


def test_registry_phase_and_tool_buckets_independent():
    """Lookups are scoped to the event the hook registered for —
    a phase hook is NOT returned for a tool event lookup."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        LifecycleEvent, ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=LifecycleEvent.PRE_GENERATE,
        hook=_make_hook("phase"),
        name="phase",
    )
    # Same name token but different event: registration error
    # by name uniqueness (not bucket)
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("tool"),
        name="tool",
    )
    phase_bucket = reg.for_event(LifecycleEvent.PRE_GENERATE)
    tool_bucket = reg.for_event(ToolHookEvent.PRE_TOOL_USE)
    assert len(phase_bucket) == 1 and len(tool_bucket) == 1
    assert phase_bucket[0].name == "phase"
    assert tool_bucket[0].name == "tool"


def test_registry_for_event_returns_empty_on_garbage():
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    # Pass garbage that's neither LifecycleEvent nor
    # ToolHookEvent → empty tuple, no raise
    assert reg.for_event("not_an_event") == ()  # type: ignore
    assert reg.for_event(None) == ()  # type: ignore
    assert reg.for_event(42) == ()  # type: ignore


def test_registry_register_rejects_garbage_event():
    """Non-event types still raise — operator misconfig stays
    visible at boot."""
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        InvalidHookError, LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    with pytest.raises(InvalidHookError):
        reg.register(
            event="not_an_event",  # type: ignore
            hook=_make_hook(),
            name="x",
        )
    with pytest.raises(InvalidHookError):
        reg.register(
            event=42,  # type: ignore
            hook=_make_hook(),
            name="x",
        )


def test_registry_register_rejects_non_callable():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        InvalidHookError, LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    with pytest.raises(InvalidHookError):
        reg.register(
            event=ToolHookEvent.PRE_TOOL_USE,
            hook="not_callable",  # type: ignore
            name="x",
        )


def test_registry_duplicate_name_rejection():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        DuplicateHookNameError, LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("a"),
        name="dup",
    )
    with pytest.raises(DuplicateHookNameError):
        reg.register(
            event=ToolHookEvent.POST_TOOL_USE,
            hook=_make_hook("b"),
            name="dup",
        )


def test_registry_priority_ordering_per_event_bucket():
    """Priority sort happens per-event-bucket — high-priority
    tool hook fires before low-priority tool hook."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("high"),
        name="high", priority=10,
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_hook("low"),
        name="low", priority=200,
    )
    bucket = reg.for_event(ToolHookEvent.PRE_TOOL_USE)
    assert [r.name for r in bucket] == ["high", "low"]


# ---------------------------------------------------------------------------
# AST pins on ToolHookEvent taxonomy
# ---------------------------------------------------------------------------


def test_tool_hook_event_class_declares_6_members():
    """Source-level pin: the ToolHookEvent class body declares
    EXACTLY the 6 expected enum members. AST parse so a
    refactor that drifts the taxonomy fails CI before it
    reaches production."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/lifecycle_hook.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found_class = False
    found_values = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ToolHookEvent"
        ):
            found_class = True
            for sub in node.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, str)
                ):
                    found_values.add(sub.value.value)
            break
    assert found_class
    assert found_values == {
        "pre_tool_use",
        "post_tool_use",
        "pre_tool_use_failure",
        "post_tool_use_failure",
        "subagent_start",
        "subagent_stop",
    }


def test_lifecycle_event_class_declares_5_members():
    """LifecycleEvent untouched — V1 Slice 1 must NOT modify
    the existing 5-value taxonomy."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/lifecycle_hook.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found_values = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "LifecycleEvent"
        ):
            for sub in node.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, str)
                ):
                    found_values.add(sub.value.value)
            break
    assert found_values == {
        "pre_generate", "pre_apply", "post_apply",
        "post_verify", "on_operator_action",
    }


def test_master_flag_distinct_in_source():
    """The two flags must read distinct env var names."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/lifecycle_hook.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "JARVIS_VENOM_TOOL_HOOKS_ENABLED" in source
    assert "JARVIS_LIFECYCLE_HOOKS_ENABLED" in source


def test_registry_register_signature_widened():
    """Registry register() must accept either taxonomy. AST
    scan asserts the isinstance check uses HookEventTypes
    (the union), not LifecycleEvent alone."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "lifecycle_hook_registry.py"
    )
    source = target.read_text(encoding="utf-8")
    # Register body must isinstance against the union, not
    # against LifecycleEvent alone (which would silently lock
    # out tool hooks)
    register_idx = source.find("def register(")
    assert register_idx >= 0
    register_body = source[register_idx:register_idx + 2000]
    assert "HookEventTypes" in register_body, (
        "register() must isinstance against HookEventTypes "
        "(the union of LifecycleEvent + ToolHookEvent)"
    )


def test_for_event_uses_union_isinstance():
    """for_event() must accept either taxonomy via the union
    isinstance check."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "lifecycle_hook_registry.py"
    )
    source = target.read_text(encoding="utf-8")
    for_event_idx = source.find("def for_event(")
    assert for_event_idx >= 0
    body = source[for_event_idx:for_event_idx + 800]
    assert "HookEventTypes" in body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_lifecycle_hook_module_exports_tool_hook_event():
    from backend.core.ouroboros.governance import lifecycle_hook
    assert hasattr(lifecycle_hook, "ToolHookEvent")
    assert hasattr(lifecycle_hook, "HookEventTypes")
    assert hasattr(lifecycle_hook, "venom_tool_hooks_enabled")


def test_registry_imports_tool_hook_event():
    """The registry module must import ToolHookEvent so
    callers don't need a separate import path."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "lifecycle_hook_registry.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "ToolHookEvent" in source
    assert "HookEventTypes" in source
