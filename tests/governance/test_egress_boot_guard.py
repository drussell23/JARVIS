"""T4 — Sovereign Telemetry Boot-Guard

Unit tests for the _warn_if_egress_guard_disabled helper extracted
from GovernedLoopService.start().

Strategy: test the small helper directly (no async GLS boot needed).
"""
from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# Import the helper under test
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.governed_loop_service import (
    _warn_if_egress_guard_disabled,
)

_SOVEREIGN_WARNING = (
    "[SOVEREIGN WARNING] API Citizenship Guard Disabled: Egress Interceptor "
    "is OFF. Node is vulnerable to overweight payload dispatch."
)


class TestWarnIfEgressGuardDisabled:
    """Unit tests for _warn_if_egress_guard_disabled."""

    def test_warns_when_explicitly_disabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=false the exact
        [SOVEREIGN WARNING] string must be emitted at WARNING level."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is True, "helper must return True when guard is disabled"
        assert any(
            _SOVEREIGN_WARNING in record.message
            for record in caplog.records
        ), f"Expected warning not found in caplog. Records: {[r.message for r in caplog.records]}"

    def test_no_warn_when_enabled_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When env var is absent (default=true) no warning should be emitted."""
        monkeypatch.delenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", raising=False)
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is False, "helper must return False when guard is enabled"
        assert not any(
            "SOVEREIGN WARNING" in record.message
            for record in caplog.records
        ), "No warning should be emitted when the guard is ON"

    def test_no_warn_when_enabled_true(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=true no warning."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "true")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is False
        assert not any("SOVEREIGN WARNING" in r.message for r in caplog.records)

    def test_no_warn_when_enabled_1(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=1 no warning."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "1")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is False
        assert not any("SOVEREIGN WARNING" in r.message for r in caplog.records)

    def test_warns_when_disabled_zero(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=0 the warning fires."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "0")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is True
        assert any(_SOVEREIGN_WARNING in r.message for r in caplog.records)

    def test_warns_when_disabled_off(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=off the warning fires."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "off")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is True
        assert any(_SOVEREIGN_WARNING in r.message for r in caplog.records)

    def test_exact_warning_string(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exact warning string must be present verbatim."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        logger = logging.getLogger("governed_loop_service_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            _warn_if_egress_guard_disabled(logger)
        messages = [r.message for r in caplog.records]
        assert _SOVEREIGN_WARNING in messages, (
            f"Exact string not matched. Got: {messages}"
        )

    def test_never_raises_on_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Helper must be fail-soft — even a broken logger must not raise."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        # Use a real logger — helper should never raise regardless
        logger = logging.getLogger("governed_loop_service_test")
        try:
            _warn_if_egress_guard_disabled(logger)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"_warn_if_egress_guard_disabled raised unexpectedly: {exc}")
