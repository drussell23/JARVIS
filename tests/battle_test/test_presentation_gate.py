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
    COCKPIT skips them, SOAK calls through. Single-flight is NOT in this
    helper — it is a FUNCTIONAL guard invoked unconditionally by main()
    (see the cockpit boot-path test below)."""
    calls = []
    monkeypatch.setattr(bt, "_reap_zombies", lambda: calls.append("reap") or set())
    monkeypatch.setattr(bt, "_print_preflight", lambda: calls.append("pf"))

    bt._run_gated_boot_banners(PresentationMode.COCKPIT, reap_enabled=True)
    assert calls == []             # all withheld at the source

    bt._run_gated_boot_banners(PresentationMode.SOAK, reap_enabled=True)
    assert calls == ["reap", "pf"]   # legacy order preserved


class _BootSentinel(Exception):
    """Raised by the single-flight spy to halt main() before stack boot."""


def test_single_flight_invoked_in_cockpit_boot_path(monkeypatch):
    """Mandate 1 invariant: single-flight is a FUNCTIONAL concurrent-launch
    guard (budget competition), not ceremony — main() must invoke it in
    COCKPIT mode too, with quiet=True (chatter gated, guard live)."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_BATTLE_TEST_SINGLETON_LOCK_ENABLED", raising=False)
    # Neutralize earlier boot steps (functional, but not under test here).
    monkeypatch.setattr(bt, "_load_env_files", lambda: None)
    monkeypatch.setattr(bt, "_reap_zombies", lambda quiet=False: set())
    monkeypatch.setattr(bt, "_cleanup_stale_router_lock", lambda reaped_pids=None: None)
    monkeypatch.setattr(bt, "_reap_stale_jarvis_locks", lambda *a, **k: 0)

    calls = []

    def _spy(*, quiet=False):
        calls.append(("sf", quiet))
        raise _BootSentinel  # stop main() before it boots the full stack

    monkeypatch.setattr(bt, "_single_flight_preflight", _spy)

    with pytest.raises(_BootSentinel):
        bt.main([])
    assert calls == [("sf", True)]   # invoked in COCKPIT, chatter-quiet


def test_single_flight_conflict_output_not_suppressed(monkeypatch, capsys):
    """The conflict path is ERROR-class telemetry: even with quiet=True
    (COCKPIT), a detected concurrent run prints the REJECTED block and
    exits 75 — the gate never carries fatal telemetry."""
    import subprocess

    class _FakePgrep:
        returncode = 0
        stdout = "99999999\n"   # a PID that is not this process

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakePgrep())
    with pytest.raises(SystemExit) as excinfo:
        bt._single_flight_preflight(quiet=True)
    assert excinfo.value.code == 75
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "99999999" in out


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
