"""Regression spine for §41.3 Slice 3 #12 — inline `?` tooltip.

Substrate-level tests for `resolve_help_for_buffer` + the
`is_inline_help_enabled` gate. Wiring-level tests live in
test_serpent_flow_ux_wiring.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc
from backend.core.ouroboros.battle_test.repl_completion import (
    INLINE_HELP_ENABLED_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    VerbCategory,
    VerbDescriptor,
    VerbRegistry,
    discover_verbs,
    is_inline_help_enabled,
    resolve_help_for_buffer,
)


# --- is_inline_help_enabled gate -------------------------------------------


def test_is_inline_help_enabled_default_true(monkeypatch):
    monkeypatch.delenv(INLINE_HELP_ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    assert is_inline_help_enabled() is True


def test_is_inline_help_enabled_explicit_false(monkeypatch):
    monkeypatch.setenv(INLINE_HELP_ENABLED_ENV_VAR, "false")
    assert is_inline_help_enabled() is False


def test_is_inline_help_enabled_implicit_off_when_completion_off(monkeypatch):
    """When completion is disabled at the master flag, inline
    help is implicitly off — no verb registry available."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    monkeypatch.setenv(INLINE_HELP_ENABLED_ENV_VAR, "true")
    assert is_inline_help_enabled() is False


def test_is_inline_help_enabled_recognizes_off_aliases(monkeypatch):
    for off_value in ("0", "no", "off", "FALSE", "False"):
        monkeypatch.setenv(INLINE_HELP_ENABLED_ENV_VAR, off_value)
        assert is_inline_help_enabled() is False, off_value


# --- resolve_help_for_buffer — fixtures ------------------------------------


@pytest.fixture
def reg_sample(monkeypatch):
    """Sample registry + ensure master flags enabled."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(INLINE_HELP_ENABLED_ENV_VAR, raising=False)
    return VerbRegistry(verbs=(
        VerbDescriptor(
            slash_form="/cancel",
            handler_method="_handle_cancel",
            description="Cancel a pending op.",
            aliases=("/stop",),
            examples=("/cancel op-abc",),
            arg_spec="<op_id> [--immediate]",
            category=VerbCategory.LIFECYCLE,
        ),
        VerbDescriptor(
            slash_form="/budget",
            handler_method="_handle_budget",
            description="Show + adjust budget.",
            arg_spec="[set <amount>]",
        ),
        VerbDescriptor(
            slash_form="/posture",
            handler_method="",
            description="Show strategic posture.",
            category=VerbCategory.INTROSPECTION,
        ),
    ))


# --- resolve_help_for_buffer — happy paths --------------------------------


def test_exact_match_renders_help(reg_sample):
    out = resolve_help_for_buffer("/cancel", reg_sample)
    assert out is not None
    assert "/cancel" in out
    assert "Cancel a pending op." in out


def test_exact_match_with_trailing_args(reg_sample):
    """`/cancel op-abc` — verb word is `/cancel`; args are
    irrelevant to which help block surfaces."""
    out = resolve_help_for_buffer(
        "/cancel op-abc --immediate", reg_sample,
    )
    assert out is not None
    assert "/cancel" in out


def test_exact_match_via_alias(reg_sample):
    """`/stop` is an alias for `/cancel` — must route to the
    primary's help block."""
    out = resolve_help_for_buffer("/stop", reg_sample)
    assert out is not None
    assert "Cancel a pending op." in out


def test_fuzzy_unique_match_renders(reg_sample):
    """`/canc` is a unique fuzzy match for `/cancel` — surface it."""
    out = resolve_help_for_buffer("/canc", reg_sample)
    assert out is not None
    assert "/cancel" in out


def test_fuzzy_typo_within_distance_renders(reg_sample):
    """`/cancl` (missing 'e') is within edit distance 1 and unique."""
    out = resolve_help_for_buffer("/cancl", reg_sample)
    assert out is not None
    assert "/cancel" in out


def test_leading_whitespace_stripped(reg_sample):
    out = resolve_help_for_buffer("   /cancel", reg_sample)
    assert out is not None
    assert "/cancel" in out


# --- resolve_help_for_buffer — refuses ambiguous + invalid ---------------


def test_empty_buffer_returns_none(reg_sample):
    assert resolve_help_for_buffer("", reg_sample) is None


def test_none_buffer_returns_none(reg_sample):
    assert resolve_help_for_buffer(None, reg_sample) is None


def test_garbage_buffer_returns_none(reg_sample):
    assert resolve_help_for_buffer(42, reg_sample) is None
    assert resolve_help_for_buffer(object(), reg_sample) is None


def test_non_slash_returns_none(reg_sample):
    """Free-text input shouldn't trigger a verb tooltip."""
    assert resolve_help_for_buffer("hello world", reg_sample) is None
    assert resolve_help_for_buffer("? what is this", reg_sample) is None


def test_bare_slash_returns_none(reg_sample):
    """`/` alone has no verb word — refuse rather than guess."""
    assert resolve_help_for_buffer("/", reg_sample) is None


def test_slash_with_space_returns_none(reg_sample):
    """`/ something` has no slash-prefixed verb word."""
    assert resolve_help_for_buffer("/ something", reg_sample) is None


def test_single_char_after_slash_returns_none(reg_sample):
    """`/c` is too ambiguous to guess from — wait for more input."""
    assert resolve_help_for_buffer("/c", reg_sample) is None


def test_fuzzy_two_matches_returns_none(reg_sample):
    """`/b` could be `/budget` (distance 5) or none — but any
    case where 2+ candidates within range exist should NOT bias
    the operator toward an arbitrary pick.

    With our sample, `/p` has only `/posture` so should fire.
    Use a controlled fixture to verify the ambiguity refusal."""
    # Build a registry where fuzzy returns 2 within distance
    reg = VerbRegistry(verbs=(
        VerbDescriptor(slash_form="/run", handler_method="", description="run"),
        VerbDescriptor(slash_form="/sun", handler_method="", description="sun"),
        VerbDescriptor(slash_form="/fun", handler_method="", description="fun"),
    ))
    # `/zun` is 1-edit from each of /run, /sun, /fun (all change first char)
    out = resolve_help_for_buffer("/zun", reg)
    assert out is None  # ambiguous — declines


def test_fuzzy_distance_too_great_returns_none(reg_sample):
    """`/zzzzz` is too far from anything in the registry."""
    out = resolve_help_for_buffer("/zzzzz", reg_sample)
    assert out is None


# --- master-flag gating ----------------------------------------------------


def test_returns_none_when_inline_help_disabled(monkeypatch, reg_sample):
    monkeypatch.setenv(INLINE_HELP_ENABLED_ENV_VAR, "false")
    # Exact match would normally render — gate must override
    assert resolve_help_for_buffer("/cancel", reg_sample) is None


def test_returns_none_when_completion_disabled(monkeypatch, reg_sample):
    """Implicit gate: when completion is off, inline help is off."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert resolve_help_for_buffer("/cancel", reg_sample) is None


# --- never raises ---------------------------------------------------------


def test_never_raises_on_buggy_registry():
    """If the registry .find() raises, resolve_help_for_buffer
    must NOT propagate — degrade to None."""
    class _BuggyRegistry:
        @property
        def verbs(self):
            raise RuntimeError("boom")

        def find(self, _):
            raise RuntimeError("boom")

    # The substrate has a NEVER-raises contract — should return
    # None without exception.
    try:
        out = resolve_help_for_buffer("/cancel", _BuggyRegistry())
        assert out is None
    except Exception:
        pytest.fail("resolve_help_for_buffer raised")


# --- end-to-end via discover_verbs ----------------------------------------


def test_e2e_with_discovered_registry(monkeypatch):
    """Integration: discover_verbs from a fake REPL, then
    resolve_help_for_buffer on a real buffer string."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(INLINE_HELP_ENABLED_ENV_VAR, raising=False)

    class _FakeREPL:
        def _handle_cancel(self, op_id, immediate=False):
            """Cancel a pending op.

            @arg_spec: <op_id> [--immediate]
            @example: /cancel op-abc
            @category: lifecycle
            """

    reg = discover_verbs(_FakeREPL())
    out = resolve_help_for_buffer("/cancel op-abc", reg)
    assert out is not None
    assert "/cancel" in out
    assert "<op_id>" in out
    assert "/cancel op-abc" in out  # example surfaces


# --- AST pin: substrate helpers exported ----------------------------------


def test_ast_pin_inline_help_symbols_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    for name in (
        '"INLINE_HELP_ENABLED_ENV_VAR"',
        '"is_inline_help_enabled"',
        '"resolve_help_for_buffer"',
    ):
        assert name in src, f"{name} missing from __all__"


def test_ast_pin_is_inline_help_composes_completion_gate():
    """Bytes-pin: is_inline_help_enabled must compose
    is_completion_enabled — the slash registry isn't available
    without it, so inline help should be implicitly off."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    # Locate is_inline_help_enabled body
    idx = src.find("def is_inline_help_enabled")
    assert idx > 0
    body = src[idx:idx + 600]
    assert "is_completion_enabled" in body


def test_ast_pin_resolve_uses_find_and_fuzzy():
    """Bytes-pin: resolve_help_for_buffer must compose both the
    exact `.find(...)` path (primary + alias) and the
    `fuzzy_match` fallback — confirms the substrate stays
    canonical-only."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    idx = src.find("def resolve_help_for_buffer")
    assert idx > 0
    body = src[idx:idx + 2500]
    assert "registry.find(" in body
    assert "fuzzy_match(" in body
    assert "format_verb_help(" in body
