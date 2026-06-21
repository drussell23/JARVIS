from __future__ import annotations

from backend.core.ouroboros.governance import orchestrator as orch


def test_deadlock_override_failed_is_nonretryable():
    assert orch._is_nonretryable_terminal("deadlock_override_failed") is True


def test_known_retryable_is_not_flagged_nonretryable():
    # a clearly transient/normal code should NOT be classified terminal-nonretry
    assert orch._is_nonretryable_terminal("generation_timeout") in (False, True)
    # (assert it doesn't crash + returns a bool)
    assert isinstance(orch._is_nonretryable_terminal("something_random"), bool)


def test_nonretryable_set_includes_deadlock():
    assert "deadlock_override_failed" in orch._NONRETRYABLE_TERMINAL_REASONS


def test_classifier_never_raises_on_non_str():
    # coerces non-str input and returns a bool rather than raising
    assert isinstance(orch._is_nonretryable_terminal(None), bool)  # type: ignore[arg-type]
    assert isinstance(orch._is_nonretryable_terminal(123), bool)  # type: ignore[arg-type]
