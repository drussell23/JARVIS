"""Arc #1 — Conversation Ledger Observer tests.

Covers:
  * Observer registered → turns auto-persisted to ledger
  * Observer exception swallowed → bridge unaffected
  * Flag off → no writes
  * Session ID resolution from SessionManager
  * Singleton / idempotent registration
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _observer_env(tmp_path, monkeypatch):
    """Ledger dir in tmp, enabled, reset singletons between tests."""
    monkeypatch.setenv(
        "JARVIS_CONVERSATION_LEDGER_DIR", str(tmp_path / "sessions"),
    )
    monkeypatch.setenv("JARVIS_CONVERSATION_LEDGER_ENABLED", "true")
    from backend.core.ouroboros.governance.conversation_ledger import (
        reset_seq_cache_for_tests,
    )
    reset_seq_cache_for_tests()
    from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield


# ---------------------------------------------------------------------------
# Observer callback
# ---------------------------------------------------------------------------


class TestObserverCallback:

    def test_observer_persists_turn(self, tmp_path, monkeypatch):
        """Direct call to observer persists to JSONL."""
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ConversationLedgerObserver,
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir, read_tail,
        )

        observer = ConversationLedgerObserver()

        # Mock session resolution to a stable id.
        with mock.patch.object(
            ConversationLedgerObserver,
            "_resolve_session_id",
            return_value="test-obs-session",
        ):
            turn = SimpleNamespace(
                role="user",
                text="hello from observer",
                source="tui_user",
                op_id="obs-op-1",
                ts=1000.0,
            )
            observer(turn)

        tail = read_tail("test-obs-session")
        assert len(tail) == 1
        assert tail[0].text == "hello from observer"

    def test_observer_never_raises(self, monkeypatch):
        """Observer swallows exceptions — no propagation."""
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ConversationLedgerObserver,
        )

        observer = ConversationLedgerObserver()

        # Force append_turn to raise.
        with mock.patch(
            "backend.core.ouroboros.governance."
            "conversation_ledger.append_turn",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise.
            observer(SimpleNamespace(
                role="user", text="crash", source="tui_user",
                op_id="", ts=0.0,
            ))

    def test_observer_noop_when_disabled(self, monkeypatch, tmp_path):
        """When ledger is disabled, observer does nothing."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ConversationLedgerObserver,
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir,
        )

        observer = ConversationLedgerObserver()
        with mock.patch.object(
            ConversationLedgerObserver,
            "_resolve_session_id",
            return_value="disabled-test",
        ):
            observer(SimpleNamespace(
                role="user", text="ignored", source="tui_user",
                op_id="", ts=0.0,
            ))

        d = ledger_dir()
        if d.exists():
            assert list(d.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# Session ID resolution
# ---------------------------------------------------------------------------


class TestSessionIdResolution:

    def test_fallback_to_epoch_id(self):
        """When SessionManager is unavailable, use epoch id."""
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ConversationLedgerObserver,
            _PROCESS_EPOCH_SESSION_ID,
        )

        with mock.patch(
            "backend.core.ouroboros.governance."
            "conversation_ledger_observer.get_session_manager",
            side_effect=ImportError("no manager"),
        ):
            sid = ConversationLedgerObserver._resolve_session_id()

        assert sid == _PROCESS_EPOCH_SESSION_ID
        assert sid.startswith("ephemeral-")

    def test_resolves_from_session_manager(self):
        """When SessionManager has an active session, use its id."""
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ConversationLedgerObserver,
        )

        fake_session = SimpleNamespace(session_id="mgr-uuid-123")
        fake_mgr = SimpleNamespace(
            list_active=lambda: [fake_session],
        )

        with mock.patch(
            "backend.core.ouroboros.governance."
            "conversation_ledger_observer.get_session_manager",
            return_value=fake_mgr,
        ):
            sid = ConversationLedgerObserver._resolve_session_id()

        assert sid == "mgr-uuid-123"


# ---------------------------------------------------------------------------
# Singleton and registration
# ---------------------------------------------------------------------------


class TestSingleton:

    def test_get_default_observer_is_singleton(self):
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            get_default_observer,
        )
        obs1 = get_default_observer()
        obs2 = get_default_observer()
        assert obs1 is obs2

    def test_reset_clears_singleton(self):
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            get_default_observer,
            reset_default_observer_for_tests,
        )
        obs1 = get_default_observer()
        reset_default_observer_for_tests()
        obs2 = get_default_observer()
        assert obs1 is not obs2

    def test_ensure_registered_disabled_returns_false(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            ensure_registered,
        )
        assert ensure_registered() is False
