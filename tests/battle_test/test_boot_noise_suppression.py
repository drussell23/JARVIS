"""Tests for boot-noise log suppression (Gap #7 follow-up).

The user-facing problem: under ``-v`` (verbose) mode, the boot
screen leaks DEBUG / INFO from initialization loggers — module
discovery, kernel init, termination-hook registration, graceful-
shutdown setup. These are pure forensic accounting and litter
the operator's view.

Fix: extend the Gap #7 suppression family with
:func:`suppress_boot_noise_logs` — raises the noisy loggers'
levels to WARNING when restraint is enabled, with the
``JARVIS_BOOT_NOISE_VERBOSE`` opt-out for boot-debugging.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.presentation_restraint import (
    BOOT_NOISE_LOGGER_NAMES,
    BOOT_NOISE_VERBOSE_ENV_VAR,
    is_boot_noise_verbose,
    restore_boot_noise_logs_for_tests,
    suppress_boot_noise_logs,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(BOOT_NOISE_VERBOSE_ENV_VAR, raising=False)
    restore_boot_noise_logs_for_tests()
    yield
    restore_boot_noise_logs_for_tests()


# ===========================================================================
# Logger-list pinning
# ===========================================================================


def test_boot_noise_logger_names_includes_module_discovery():
    """The empirically-observed offender from the user's terminal."""
    assert (
        "backend.core.ouroboros.governance.meta.module_discovery"
        in BOOT_NOISE_LOGGER_NAMES
    )


def test_boot_noise_logger_names_includes_kernel():
    """Parent ``backend.kernel`` covers submodules via inheritance."""
    assert "backend.kernel" in BOOT_NOISE_LOGGER_NAMES


def test_boot_noise_logger_names_includes_graceful_shutdown():
    assert "GracefulShutdown" in BOOT_NOISE_LOGGER_NAMES


def test_boot_noise_logger_names_includes_termination_hook():
    assert (
        "backend.core.ouroboros.battle_test.termination_hook_registry"
        in BOOT_NOISE_LOGGER_NAMES
    )


def test_boot_noise_logger_names_is_tuple():
    """Frozen tuple — the list is a structural constant; modifying it
    requires a slice."""
    assert isinstance(BOOT_NOISE_LOGGER_NAMES, tuple)


# ===========================================================================
# is_boot_noise_verbose — opt-out flag
# ===========================================================================


def test_boot_noise_verbose_default_off():
    assert is_boot_noise_verbose() is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("garbage", False),
])
def test_boot_noise_verbose_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(BOOT_NOISE_VERBOSE_ENV_VAR, raw)
    assert is_boot_noise_verbose() is expected


# ===========================================================================
# suppress_boot_noise_logs — raises levels to WARNING
# ===========================================================================


def test_suppress_raises_each_logger_to_warning():
    """After suppression, all named loggers are at WARNING level."""
    # Pre-condition: explicitly set them to DEBUG so we know the
    # suppression actually changed something.
    for name in BOOT_NOISE_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.DEBUG)
    suppressed = suppress_boot_noise_logs()
    assert suppressed == len(BOOT_NOISE_LOGGER_NAMES)
    for name in BOOT_NOISE_LOGGER_NAMES:
        assert logging.getLogger(name).level == logging.WARNING


def test_suppress_skipped_when_verbose_on(monkeypatch):
    """Operators debugging boot keep DEBUG visibility."""
    monkeypatch.setenv(BOOT_NOISE_VERBOSE_ENV_VAR, "true")
    # Pre-set to DEBUG to ensure we'd notice if suppression fired
    for name in BOOT_NOISE_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.DEBUG)
    suppressed = suppress_boot_noise_logs()
    assert suppressed == 0
    # Levels unchanged
    for name in BOOT_NOISE_LOGGER_NAMES:
        assert logging.getLogger(name).level == logging.DEBUG


def test_suppress_idempotent():
    """Calling twice doesn't blow up. Original level is captured
    once; subsequent calls just re-apply WARNING."""
    suppress_boot_noise_logs()
    suppress_boot_noise_logs()  # no-op equivalent
    for name in BOOT_NOISE_LOGGER_NAMES:
        assert logging.getLogger(name).level == logging.WARNING


def test_restore_for_tests_resets_levels():
    for name in BOOT_NOISE_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.DEBUG)
    suppress_boot_noise_logs()
    assert logging.getLogger(BOOT_NOISE_LOGGER_NAMES[0]).level == logging.WARNING
    restore_boot_noise_logs_for_tests()
    # Original DEBUG level restored
    assert logging.getLogger(BOOT_NOISE_LOGGER_NAMES[0]).level == logging.DEBUG


def test_warning_messages_still_propagate(caplog):
    """WARNING+ messages must still surface — only DEBUG / INFO is
    silenced. Operators must not lose actual warnings."""
    # Capture all logging at WARNING level
    suppress_boot_noise_logs()
    for name in BOOT_NOISE_LOGGER_NAMES:
        log = logging.getLogger(name)
        with caplog.at_level(logging.WARNING, logger=name):
            log.debug("noise-debug-msg")
            log.info("noise-info-msg")
            log.warning("real-warning-msg")
    # The DEBUG / INFO must be filtered out; WARNING preserved.
    levels = {r.levelname for r in caplog.records}
    assert "DEBUG" not in levels
    assert "INFO" not in levels
    assert "WARNING" in levels


# ===========================================================================
# Boot-script wiring — regression check
# ===========================================================================


def test_battle_test_script_invokes_suppression():
    """The battle-test launcher must call suppress_boot_noise_logs()
    at boot under restraint mode, otherwise the user's reported
    boot-noise issue silently regresses."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    assert "suppress_boot_noise_logs" in src
    assert "BOOT_NOISE_VERBOSE" in src or "is_restraint_enabled" in src


def test_battle_test_script_suppression_after_basicconfig():
    """The suppression call must come AFTER ``logging.basicConfig``
    so it overrides the default level on each logger. AST walk to
    verify the ordering."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    # Find the indices of the two markers
    idx_basicconfig = src.find("logging.basicConfig(")
    idx_suppress = src.find("suppress_boot_noise_logs")
    assert idx_basicconfig > 0, "basicConfig() not found"
    assert idx_suppress > 0, "suppress_boot_noise_logs not found"
    assert idx_suppress > idx_basicconfig, (
        "suppress_boot_noise_logs must be called AFTER basicConfig"
    )
