"""Slice 1b Step 0 — GovernedLoopService GENERAL factory wire-in.

Pins the integration contract: GLS must attach ``build_llm_general_factory``
(not the bare default stub factory) and must provide a provider-registry
callable that maps canonical provider names to live provider instances.

This is NOT a full GLS boot test — that surface is covered by
``tests/governance/integration/test_governed_loop_startup.py`` and would
multiply the runtime by 10× for no added signal. These tests exercise
the narrow surface the wire-in touches: the resolver method on GLS,
and the factory's flag-gated behavior.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.agentic_general_subagent import (
    AgenticGeneralSubagent,
    build_llm_general_factory,
)


# ---------------------------------------------------------------------------
# _resolve_provider_for_subagent (the new method on GLS)
# ---------------------------------------------------------------------------

def _make_gls_surface(
    claude_ref=None, doubleword_ref=None,
) -> SimpleNamespace:
    """Minimal GLS-like surface — the _resolve_provider_for_subagent
    method is bound to this namespace so we test the resolver without
    spinning up the full governed loop."""
    import types
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopService,
    )
    ns = SimpleNamespace()
    ns._claude_ref = claude_ref
    ns._doubleword_ref = doubleword_ref
    ns._resolve_provider_for_subagent = types.MethodType(
        GovernedLoopService._resolve_provider_for_subagent, ns,
    )
    return ns


def test_resolver_returns_claude_for_claude_name() -> None:
    claude = SimpleNamespace(name="claude-api-stub")
    dw = SimpleNamespace(name="dw-stub")
    gls = _make_gls_surface(claude_ref=claude, doubleword_ref=dw)
    assert gls._resolve_provider_for_subagent("claude-api") is claude
    assert gls._resolve_provider_for_subagent("Claude-Sonnet-4-6") is claude


def test_resolver_returns_dw_for_doubleword_name() -> None:
    claude = SimpleNamespace(name="claude")
    dw = SimpleNamespace(name="dw")
    gls = _make_gls_surface(claude_ref=claude, doubleword_ref=dw)
    assert gls._resolve_provider_for_subagent("doubleword-397b") is dw
    # Also handles "dw-" prefix + "qwen" substring (DW's model families)
    assert gls._resolve_provider_for_subagent("dw-batch") is dw
    assert gls._resolve_provider_for_subagent("Qwen3-VL-235B") is dw


def test_resolver_unknown_name_falls_back_to_claude() -> None:
    """Per the resolver's docstring: unrecognized name defaults to the
    Claude reference (prefrontal cortex for GENERAL's NOTIFY_APPLY
    tier). None return only when claude_ref itself is None."""
    claude = SimpleNamespace(name="claude")
    gls = _make_gls_surface(claude_ref=claude, doubleword_ref=None)
    assert gls._resolve_provider_for_subagent("mystery-brand") is claude
    assert gls._resolve_provider_for_subagent("") is claude


def test_resolver_returns_none_when_claude_not_wired() -> None:
    """If Claude isn't configured (API key absent), the resolver
    surfaces None cleanly — the driver's no_provider_wired path
    handles it as a structured failure."""
    gls = _make_gls_surface(claude_ref=None, doubleword_ref=None)
    assert gls._resolve_provider_for_subagent("claude-api") is None
    assert gls._resolve_provider_for_subagent("anything") is None


# ---------------------------------------------------------------------------
# build_llm_general_factory integration behavior
# ---------------------------------------------------------------------------

def test_factory_flag_off_returns_stub_subagent(
    monkeypatch, tmp_path,
) -> None:
    """Flag off → factory returns stub subagent (llm_driver=None). This
    is the byte-identical-to-Phase-B-default behavior that makes the
    wire-in safe to attach unconditionally. Post-graduation (2026-04-20)
    the default is ``true``, so opt-out now requires an explicit
    ``setenv("false")`` — a bare ``delenv`` would enable the driver."""
    monkeypatch.setenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", "false")

    claude = SimpleNamespace(name="claude")
    gls = _make_gls_surface(claude_ref=claude)
    factory = build_llm_general_factory(
        tmp_path,
        provider_registry=gls._resolve_provider_for_subagent,
    )
    sub = factory()
    assert isinstance(sub, AgenticGeneralSubagent)
    assert sub._llm_driver is None, (
        "flag-off must produce a stub subagent — Phase B byte-identical"
    )


def test_factory_flag_on_returns_llm_driven_subagent(
    monkeypatch, tmp_path,
) -> None:
    """Flag on → factory wires an LLM driver closure over the provider
    registry. Each factory() call produces a fresh subagent — no shared
    mutable state across dispatches."""
    monkeypatch.setenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", "true")

    claude = SimpleNamespace(name="claude")
    gls = _make_gls_surface(claude_ref=claude)
    factory = build_llm_general_factory(
        tmp_path,
        provider_registry=gls._resolve_provider_for_subagent,
    )

    sub_a = factory()
    sub_b = factory()
    # Fresh instance per call.
    assert sub_a is not sub_b
    assert isinstance(sub_a, AgenticGeneralSubagent)
    assert sub_a._llm_driver is not None
    assert callable(sub_a._llm_driver)


@pytest.mark.asyncio
async def test_factory_driver_resolves_via_gls_registry(
    monkeypatch, tmp_path,
) -> None:
    """End-to-end integration: factory → driver closure → provider
    registry → resolved provider. When registry returns None, the
    driver emits no_provider_wired (same behavior test_general_driver
    covered in isolation). Here we prove the GLS registry is the one
    consulted."""
    monkeypatch.setenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", "true")

    # Record registry calls so we can assert the resolver was called.
    resolver_calls: list = []

    gls = _make_gls_surface(claude_ref=None, doubleword_ref=None)
    def _recording_registry(name: str):
        resolver_calls.append(name)
        return gls._resolve_provider_for_subagent(name)

    factory = build_llm_general_factory(
        tmp_path,
        provider_registry=_recording_registry,
    )
    sub = factory()

    # Invoke the driver directly with a minimal payload.
    trace = await sub._llm_driver({
        "sub_id": "sub-wire-in-test",
        "invocation": {
            "operation_scope": ["src/"],
            "allowed_tools": ["read_file"],
            "max_mutations": 0,
            "parent_op_risk_tier": "NOTIFY_APPLY",
            "invocation_reason": "wire-in integration test",
            "goal": "test",
            "primary_repo": "jarvis",
        },
        "project_root": str(tmp_path),
        "primary_provider_name": "claude-api",
        "fallback_provider_name": "",
        "deadline": None,
        "max_rounds": 2,
        "tool_timeout_s": 5.0,
    })

    assert resolver_calls == ["claude-api"], (
        f"registry should have been called once with 'claude-api'; "
        f"got {resolver_calls!r}"
    )
    # No Claude wired → driver produces structured no_provider_wired trace.
    assert trace["status"] == "no_provider_wired"
