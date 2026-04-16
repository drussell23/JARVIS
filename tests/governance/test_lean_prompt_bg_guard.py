"""Test the BG/SPEC guard on ``_should_use_lean_prompt``.

When a BG or SPECULATIVE op cascades to Claude (via
``JARVIS_TOPOLOGY_BG_CASCADE_ENABLED``), the tool loop is still skipped
for cost reasons — but before v1.1a the lean prompt was still selected,
giving Claude tool instructions that nobody would execute. The model
then emits a ``2b.2-tool`` tool-call that fails schema validation with
``tool_call_without_tool_loop``.

The guard fixes this by making ``_should_use_lean_prompt`` return False
on BG/SPEC routes — falling back to the full codegen prompt which asks
directly for a patch without tool instructions.

Env override ``JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED=true`` restores
the pre-guard behavior for controlled experiments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from backend.core.ouroboros.governance.providers import _should_use_lean_prompt


@dataclass
class _FakeCtx:
    """Minimal context shape — only the getattr fields the guard reads."""

    provider_route: str = ""
    task_complexity: str = ""
    cross_repo: bool = False


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in ("JARVIS_LEAN_PROMPT", "JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Baseline: standard/complex/immediate routes get the lean prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["standard", "complex", "immediate", ""])
def test_non_bg_routes_still_use_lean_prompt(route):
    ctx = _FakeCtx(provider_route=route)
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is True


# ---------------------------------------------------------------------------
# Fix: BG / SPEC routes skip the lean prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["background", "speculative"])
def test_bg_and_spec_routes_skip_lean_prompt(route):
    """The core fix — no more tool instructions on routes that skip the loop."""
    ctx = _FakeCtx(provider_route=route)
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is False


# ---------------------------------------------------------------------------
# Escape hatch: env override restores pre-guard behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "TRUE"])
def test_env_override_restores_lean_on_bg(monkeypatch, truthy):
    monkeypatch.setenv("JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED", truthy)
    ctx = _FakeCtx(provider_route="background")
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is True


@pytest.mark.parametrize("falsey", ["false", "0", "no", "off", ""])
def test_env_override_falsey_leaves_guard_on(monkeypatch, falsey):
    monkeypatch.setenv("JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED", falsey)
    ctx = _FakeCtx(provider_route="background")
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is False


# ---------------------------------------------------------------------------
# Interaction with pre-existing exclusion rules
# ---------------------------------------------------------------------------


def test_bg_trivial_still_skips(monkeypatch):
    """Both guards fire — trivial + BG — result still False (well-defined)."""
    ctx = _FakeCtx(provider_route="background", task_complexity="trivial")
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is False


def test_tools_disabled_dominates_bg_override(monkeypatch):
    """tools_enabled=False always wins, even with the BG override on."""
    monkeypatch.setenv("JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED", "true")
    ctx = _FakeCtx(provider_route="background")
    assert _should_use_lean_prompt(ctx, tools_enabled=False) is False


def test_force_full_dominates_everything():
    ctx = _FakeCtx(provider_route="complex")  # lean-eligible baseline
    assert _should_use_lean_prompt(
        ctx, tools_enabled=True, force_full=True,
    ) is False


def test_jarvis_lean_prompt_env_false_disables_globally(monkeypatch):
    monkeypatch.setenv("JARVIS_LEAN_PROMPT", "false")
    ctx = _FakeCtx(provider_route="complex")
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is False


# ---------------------------------------------------------------------------
# Cross-repo guard (pre-existing, unchanged) — regression test
# ---------------------------------------------------------------------------


def test_cross_repo_still_skips():
    ctx = _FakeCtx(provider_route="complex", cross_repo=True)
    assert _should_use_lean_prompt(ctx, tools_enabled=True) is False
