#!/usr/bin/env python3
"""Tests for SignalAuthority (Disease 5+6 MVP)."""
import asyncio
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.signal_authority import SignalAuthority
from backend.core.kernel_lifecycle_engine import LifecycleEngine, LifecycleEvent


class TestSignalAuthority:
    def test_install_is_idempotent(self):
        engine = LifecycleEngine()
        loop = MagicMock()
        loop.add_signal_handler = MagicMock(side_effect=NotImplementedError)
        auth = SignalAuthority(engine, loop)
        with patch("signal.signal"):
            auth.install()
            auth.install()  # second call should be no-op
        assert auth._installed

    def test_handle_signal_triggers_shutdown(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        assert engine.state.value == "shutting_down"

    def test_duplicate_signal_is_idempotent(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        # Second signal should NOT raise (duplicate shutdown is idempotent)
        auth._handle_signal(signal.SIGTERM.value)
        assert engine.state.value == "shutting_down"

    def test_repeated_signals_trigger_emergency_exit(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        with patch.object(auth, '_emergency_exit') as mock_exit:
            for _ in range(4):
                auth._handle_signal(signal.SIGTERM.value)
            mock_exit.assert_called_once()

    def test_signal_count_tracked(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        auth._handle_signal(signal.SIGTERM.value)
        assert auth._signal_count[signal.SIGTERM.value] == 2
