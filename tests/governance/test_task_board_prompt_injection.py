"""Regression spine — Gap #5 Slice 3 advisory prompt injection.

Pins the Slice 3 additions:

  1. render_prompt_section() shape + content
  2. Env gating (master switch default ON; opt-out via "false")
  3. Authority invariant: authority-free, tier -1 sanitization
     inherited from the ConversationBridge pattern; does NOT gate
     anything downstream
  4. Configuration caps: max pending shown, title preview len
  5. Empty / closed / all-terminal states return None
  6. Orchestrator wiring pins (import-surface + finally-hook
     presence)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.task_board import (
    STATE_PENDING,
    STATE_IN_PROGRESS,
    TaskBoard,
    _prompt_injection_enabled,
    _prompt_max_tasks,
    _prompt_title_preview_len,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_taskboard_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_TASK_BOARD_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Env gates
# ---------------------------------------------------------------------------


def test_prompt_injection_defaults_to_true(monkeypatch):
    """Slice 3 test 1: default is ON for prompt injection. Authority-
    free, pure observability — contrast with the Slice 2 Venom tool
    flag which is deny-by-default. Operators opt OUT here if the
    subsection is noisy."""
    monkeypatch.delenv(
        "JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED", raising=False,
    )
    assert _prompt_injection_enabled() is True


def test_prompt_injection_explicit_false_opts_out(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED", "false",
    )
    assert _prompt_injection_enabled() is False


def test_prompt_injection_case_insensitive(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED", "TRUE",
    )
    assert _prompt_injection_enabled() is True


def test_prompt_max_tasks_cap_and_default(monkeypatch):
    assert _prompt_max_tasks() == 5
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_MAX_TASKS", "10")
    assert _prompt_max_tasks() == 10
    # Non-integer falls back to default.
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_MAX_TASKS", "not-a-number")
    assert _prompt_max_tasks() == 5
    # Negative clamped to 1.
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_MAX_TASKS", "-10")
    assert _prompt_max_tasks() == 1


def test_prompt_title_preview_len_cap_and_default(monkeypatch):
    assert _prompt_title_preview_len() == 120
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_TITLE_PREVIEW", "50")
    assert _prompt_title_preview_len() == 50


# ---------------------------------------------------------------------------
# 2. Empty / closed / all-terminal states return None
# ---------------------------------------------------------------------------


def test_render_returns_none_when_master_off(monkeypatch):
    """Slice 3 test 6: env opt-out suppresses the subsection entirely,
    regardless of board state."""
    monkeypatch.setenv(
        "JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED", "false",
    )
    board = TaskBoard(op_id="op-off")
    board.create(title="t")
    assert board.render_prompt_section() is None


def test_render_returns_none_on_empty_board():
    board = TaskBoard(op_id="op-empty")
    assert board.render_prompt_section() is None


def test_render_returns_none_on_closed_board():
    """Slice 3 test 8: closed board → None. No rendering post-close,
    even if tasks remain in memory. Matches Option A ephemeral
    lifetime."""
    board = TaskBoard(op_id="op-closed")
    board.create(title="t")
    board.close(reason="test")
    assert board.render_prompt_section() is None


def test_render_returns_none_when_all_terminal():
    """Slice 3 test 9: a board containing only completed / cancelled
    tasks renders as None — the advisory is about what's
    active/pending, not history. Completed tasks live in the §8
    audit log."""
    board = TaskBoard(op_id="op-all-terminal")
    t1 = board.create(title="done")
    board.complete(t1.task_id)
    t2 = board.create(title="gone")
    board.cancel(t2.task_id, reason="redirect")
    assert board.render_prompt_section() is None


# ---------------------------------------------------------------------------
# 3. Content shape — happy paths
# ---------------------------------------------------------------------------


def test_render_shows_single_active_task():
    board = TaskBoard(op_id="op-active")
    t = board.create(title="the work")
    board.start(t.task_id)
    prompt = board.render_prompt_section()
    assert prompt is not None
    assert "## Current tasks (advisory)" in prompt
    assert "### Active (in_progress)" in prompt
    assert "the work" in prompt
    assert t.task_id in prompt


def test_render_shows_pending_tasks_in_insertion_order():
    board = TaskBoard(op_id="op-pending")
    t1 = board.create(title="first")
    t2 = board.create(title="second")
    t3 = board.create(title="third")
    prompt = board.render_prompt_section()
    assert prompt is not None
    assert "### Pending" in prompt
    # Insertion order preserved.
    idx_first = prompt.index("first")
    idx_second = prompt.index("second")
    idx_third = prompt.index("third")
    assert idx_first < idx_second < idx_third


def test_render_caps_pending_tasks_at_env_limit(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_MAX_TASKS", "2")
    board = TaskBoard(op_id="op-cap")
    for i in range(5):
        board.create(title="t-" + str(i))
    prompt = board.render_prompt_section()
    assert prompt is not None
    # First 2 pending rendered + a "+N more pending" summary.
    assert "t-0" in prompt
    assert "t-1" in prompt
    assert "t-4" not in prompt
    assert "+3 more pending" in prompt


def test_render_shows_both_active_and_pending():
    board = TaskBoard(op_id="op-mixed")
    a = board.create(title="active-one")
    b = board.create(title="pending-one")
    board.create(title="pending-two")
    board.start(a.task_id)
    prompt = board.render_prompt_section()
    assert prompt is not None
    assert "### Active (in_progress)" in prompt
    assert "active-one" in prompt
    assert "### Pending" in prompt
    assert "pending-one" in prompt
    assert "pending-two" in prompt


def test_render_excludes_terminal_states():
    """Slice 3 test 14: completed + cancelled tasks are NOT shown.
    The advisory is "what am I working on right now", not history."""
    board = TaskBoard(op_id="op-exclude")
    t_done = board.create(title="already-done")
    board.complete(t_done.task_id)
    t_cancelled = board.create(title="gave-up")
    board.cancel(t_cancelled.task_id)
    t_pending = board.create(title="still-pending")
    prompt = board.render_prompt_section()
    assert prompt is not None
    assert "already-done" not in prompt
    assert "gave-up" not in prompt
    assert "still-pending" in prompt


# ---------------------------------------------------------------------------
# 4. Authority / sanitization contract
# ---------------------------------------------------------------------------


def test_render_carries_authority_disclaimer():
    """Slice 3 test 15 (CRITICAL): the rendered subsection carries the
    "Not authoritative" language so downstream consumers (the model,
    operators) cannot mistake it for a gating signal. Mirrors the
    ConversationBridge / SemanticIndex disclaimer discipline."""
    board = TaskBoard(op_id="op-auth")
    board.create(title="work")
    prompt = board.render_prompt_section()
    assert prompt is not None
    assert "Not authoritative" in prompt
    assert "Iron Gate" in prompt


def test_render_applies_tier_minus_one_sanitization():
    """Slice 3 test 16: control chars + secret-shape redaction via
    sanitize_for_log. If anyone strips the subsection via sanitizer
    regression (§ tier -1 change), we discover it here rather than
    fighting the sanitizer blindly."""
    board = TaskBoard(op_id="op-sanitize")
    # Title with control char + normal content.
    board.create(title="payment flow\x00refactor")
    prompt = board.render_prompt_section()
    assert prompt is not None
    # Control char stripped, visible content preserved.
    assert "\x00" not in prompt
    assert "payment flow" in prompt


def test_render_title_preview_length_capped(monkeypatch):
    """Slice 3 test 17: title preview capped at env-tunable length.
    Prevents a monster-title task from blowing out the prompt."""
    monkeypatch.setenv("JARVIS_TASK_BOARD_PROMPT_TITLE_PREVIEW", "32")
    board = TaskBoard(op_id="op-longtitle")
    long_title = "x" * 150
    board.create(title=long_title)
    prompt = board.render_prompt_section()
    assert prompt is not None
    # Preview truncated — original 150-char title does NOT appear
    # in full.
    assert "x" * 150 not in prompt


def test_render_fallback_to_redacted_on_empty_sanitized_title():
    """Slice 3 test 18: if sanitizer reduces a title to empty (all
    control chars, etc.), the task is rendered with ``<redacted>``
    instead of disappearing. Preserves the audit story — the ID
    + state remain visible."""
    board = TaskBoard(op_id="op-blank-sanitized")
    # All-control-char title — sanitize_for_log reduces to empty.
    # Board itself allows non-empty pre-sanitize, but after
    # sanitize_for_log these collapse.
    board.create(title="abc")
    # Manually clobber the in-memory title to force the sanitizer to
    # return empty. The render path uses sanitize_for_log which
    # aggressively strips — we simulate by injecting control-heavy
    # text at title time (but the board validator enforces non-empty
    # at create, so we test the sanitizer output path differently:
    # the sanitizer output itself is what render checks).
    # Easier: override the internal task's title via the internal
    # dict (test-only patch).
    task_id = list(board._tasks.keys())[0]  # noqa
    from dataclasses import replace as _replace
    board._tasks[task_id] = _replace(  # noqa
        board._tasks[task_id], title="\x00\x00\x00"  # noqa
    )
    prompt = board.render_prompt_section()
    assert prompt is not None
    # Redacted placeholder shown, not disappeared.
    assert "<redacted>" in prompt
    # Task ID still visible for audit coherence.
    assert task_id in prompt


# ---------------------------------------------------------------------------
# 5. Orchestrator wiring — grep-enforced presence pins
# ---------------------------------------------------------------------------


def test_orchestrator_imports_close_task_board_at_shutdown():
    """Slice 3 test 19 (CRITICAL): the orchestrator's run() finally
    block MUST call close_task_board(ctx.op_id, reason=...). Grep
    the orchestrator source for the expected import + call pattern
    so a future refactor that strips the hook fails loudly."""
    src = Path(
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text()
    assert "close_task_board" in src, (
        "Slice 3 contract violation: orchestrator no longer imports "
        "close_task_board — the ctx shutdown hook has been dropped."
    )
    # The reason kwarg is how we emit the phase name on close — pin it.
    assert "reason=" in src or "reason =" in src


def test_orchestrator_renders_task_board_at_context_expansion():
    """Slice 3 test 20: the orchestrator's CONTEXT_EXPANSION injection
    site calls render_prompt_section() for the ctx's op_id. Prevents
    a silent regression that strips the subsection."""
    src = Path(
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text()
    assert "TaskBoard" in src or "task_board" in src, (
        "Slice 3 contract violation: orchestrator no longer references "
        "TaskBoard for CONTEXT_EXPANSION injection."
    )
    assert "render_prompt_section" in src, (
        "Slice 3 contract violation: orchestrator does not call "
        "render_prompt_section — the advisory subsection has been "
        "disconnected from CONTEXT_EXPANSION."
    )


# ---------------------------------------------------------------------------
# 6. Authority invariant — render path doesn't leak into gates
# ---------------------------------------------------------------------------


def test_render_prompt_section_is_pure_read():
    """Slice 3 test 21 (CRITICAL): calling render_prompt_section
    MUST NOT mutate board state — no side effects. Can be called
    safely at any time (including from read-only paths)."""
    board = TaskBoard(op_id="op-purity")
    t = board.create(title="the work")
    snap_before = board.snapshot()
    _ = board.render_prompt_section()
    _ = board.render_prompt_section()
    snap_after = board.snapshot()
    assert snap_before == snap_after


def test_task_board_module_doc_mentions_authority_posture():
    """Slice 3 test 22 (bit-rot guard): the task_board module
    docstring still carries the "authority-free" / "observability
    only" language after Slice 3 additions. Bit-rot guard — future
    refactors that strip the §1/§6 mention fail loudly."""
    import backend.core.ouroboros.governance.task_board as module
    doc = (module.__doc__ or "").lower()
    # Must name one of the authority-safety anchors.
    assert "authority" in doc or "§1" in doc or "§6" in doc or "iron gate" in doc
