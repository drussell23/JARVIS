"""Presentation gate: COCKPIT withholds banners at the SOURCE; fatal paths
structurally bypass (Mandate 1). SOAK is call-through (legacy regression)."""
from __future__ import annotations

import logging

import pytest

import scripts.ouroboros_battle_test as bt
from backend.core.ouroboros.ui.presentation_mode import PresentationMode


def test_check_api_keys_or_die_exists_and_is_fatal(monkeypatch):
    """The fatal check is its own function -- physically outside the gate."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        bt._check_api_keys_or_die()


def test_check_api_keys_passes_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bt._check_api_keys_or_die()   # no raise


def test_print_preflight_no_longer_contains_fatal_exit(monkeypatch, capsys):
    """_print_preflight is pure presentation now: with no keys it must NOT
    exit -- the fatal lives in _check_api_keys_or_die (bypass proof)."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bt._print_preflight()          # must not raise SystemExit


def test_gated_banner_helpers_skip_in_cockpit(monkeypatch):
    """The boot path calls banners through _run_gated_boot_banners(mode);
    COCKPIT skips them, SOAK calls through."""
    calls = []
    monkeypatch.setattr(bt, "_reap_zombies", lambda: calls.append("reap") or set())
    monkeypatch.setattr(bt, "_single_flight_preflight", lambda: calls.append("sf"))
    monkeypatch.setattr(bt, "_print_preflight", lambda: calls.append("pf"))

    bt._run_gated_boot_banners(PresentationMode.COCKPIT, single_flight_enabled=True,
                               reap_enabled=True)
    assert calls == []             # all withheld at the source

    bt._run_gated_boot_banners(PresentationMode.SOAK, single_flight_enabled=True,
                               reap_enabled=True)
    assert calls == ["reap", "sf", "pf"]   # legacy order preserved


def test_resolve_boot_log_level_cockpit_is_warning():
    assert bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=False) == logging.WARNING
    assert bt._resolve_boot_log_level(PresentationMode.SOAK, verbose=False) == logging.INFO
    # verbose ALWAYS wins -- an operator asking for -v is never silenced
    assert bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=True) == logging.DEBUG


def test_error_records_pass_in_cockpit(caplog):
    """WARNING root level still delivers ERROR/CRITICAL -- the bypass is
    structural: the gate only lowers verbosity, it filters nothing."""
    level = bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=False)
    logger = logging.getLogger("test.cockpit.fatal")
    with caplog.at_level(level, logger="test.cockpit.fatal"):
        logger.error("initialization collapse")
        logger.critical("fatal")
    messages = [r.message for r in caplog.records]
    assert "initialization collapse" in messages
    assert "fatal" in messages
