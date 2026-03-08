"""Tests for ErrorInterceptor — captures ERROR/CRITICAL log records as IntentSignals.

Validates that the interceptor:
1. Emits signals for ERROR-level logs
2. Ignores WARNING and INFO logs
3. Emits higher-confidence signals for CRITICAL logs
4. Extracts source file information from log records
5. Extracts traceback information from exception records

Each test uses a unique logger name to avoid cross-test interference.
"""
from __future__ import annotations

import logging

from backend.core.ouroboros.governance.intent.error_interceptor import (
    ErrorInterceptor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interceptor_with_logger(
    logger_name: str,
) -> tuple[ErrorInterceptor, logging.Logger, list[IntentSignal]]:
    """Create an ErrorInterceptor installed on a fresh logger, collecting signals."""
    interceptor = ErrorInterceptor(repo="jarvis")
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)  # Ensure all levels reach handlers
    signals: list[IntentSignal] = []
    interceptor.on_signal = signals.append
    interceptor.install(logger)
    return interceptor, logger, signals


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInterceptorCapturesErrorLog:
    """test_interceptor_captures_error_log"""

    def test_error_log_emits_signal(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.capture"
        )
        try:
            logger.error("Something went wrong in the engine")

            assert len(signals) == 1
            sig = signals[0]
            assert sig.source == "intent:stack_trace"
            assert sig.stable is False
            assert sig.repo == "jarvis"
            assert "Something went wrong" in sig.evidence.get("signature", "")
        finally:
            interceptor.uninstall(logger)


class TestInterceptorIgnoresWarningAndInfo:
    """test_interceptor_ignores_warning_and_info"""

    def test_warning_produces_no_signal(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.ignore.warning"
        )
        try:
            logger.warning("This is a warning")
            assert len(signals) == 0
        finally:
            interceptor.uninstall(logger)

    def test_info_produces_no_signal(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.ignore.info"
        )
        try:
            logger.info("This is informational")
            assert len(signals) == 0
        finally:
            interceptor.uninstall(logger)


class TestInterceptorCapturesCritical:
    """test_interceptor_captures_critical"""

    def test_critical_log_has_high_confidence(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.critical"
        )
        try:
            logger.critical("Fatal database connection failure")

            assert len(signals) == 1
            sig = signals[0]
            assert sig.confidence > 0.8
            assert sig.source == "intent:stack_trace"
            assert sig.stable is False
        finally:
            interceptor.uninstall(logger)


class TestInterceptorExtractsFileFromRecord:
    """test_interceptor_extracts_file_from_record"""

    def test_target_files_has_at_least_one_entry(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.file_extract"
        )
        try:
            logger.error("File-related error")

            assert len(signals) == 1
            sig = signals[0]
            assert len(sig.target_files) >= 1
            # The source file should be this test file (since logger.error is called here)
            assert any("test_error_interceptor" in f for f in sig.target_files)
        finally:
            interceptor.uninstall(logger)


class TestInterceptorExtractsTraceback:
    """test_interceptor_extracts_traceback"""

    def test_exception_traceback_in_evidence(self):
        interceptor, logger, signals = _make_interceptor_with_logger(
            "test.interceptor.traceback"
        )
        try:
            try:
                raise ValueError("broken widget")
            except ValueError:
                logger.exception("Caught an exception")

            assert len(signals) == 1
            sig = signals[0]
            assert "traceback" in sig.evidence
            tb_text = sig.evidence["traceback"]
            assert "ValueError" in tb_text
            assert "broken widget" in tb_text
        finally:
            interceptor.uninstall(logger)
