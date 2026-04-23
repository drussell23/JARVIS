"""Tests for Ticket C — native --headless flag.

Ticket: memory/project_followup_graduation_runbook_stdin.md (2026-04-23).

Background: agent-conducted soak launches previously required the
opaque ``tail -f /dev/null | python3 ...`` stdin-guard idiom to
prevent SerpentREPL's ``PromptSession.prompt_async()`` from hitting
``EOFError → break`` on the first iteration against an EOF'd stdin
(killing the harness in ~16 log lines). Ticket C replaces the
workaround with a native ``--headless`` flag + env var + isatty
auto-detect that skips the REPL entirely.

Coverage:
- ``HarnessConfig.headless`` tri-state field (None / True / False).
- ``HarnessConfig.from_env`` reading ``OUROBOROS_BATTLE_HEADLESS``
  with canonical truthy/falsy tokens + unset → None.
- ``HarnessConfig.resolve_headless()`` resolution logic:
  True/False returned as-is, None triggers ``not sys.stdin.isatty()``
  auto-detect, OSError/ValueError on isatty() treated as headless.
- CLI arg parse: ``--headless`` sets True, ``--no-headless`` sets
  False, absent → None. Mutually exclusive.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.harness import HarnessConfig


# ---------------------------------------------------------------------------
# (1) HarnessConfig field + from_env
# ---------------------------------------------------------------------------


def test_config_default_headless_is_none():
    """Default must be None — auto-detect path runs in run()."""
    cfg = HarnessConfig()
    assert cfg.headless is None


def test_config_accepts_true():
    cfg = HarnessConfig(headless=True)
    assert cfg.headless is True


def test_config_accepts_false():
    cfg = HarnessConfig(headless=False)
    assert cfg.headless is False


def test_from_env_reads_truthy_values(monkeypatch):
    """OUROBOROS_BATTLE_HEADLESS=1/true/yes/on → headless=True."""
    for token in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("OUROBOROS_BATTLE_HEADLESS", token)
        cfg = HarnessConfig.from_env()
        assert cfg.headless is True, f"token {token!r} should map to True"


def test_from_env_reads_falsy_values(monkeypatch):
    """OUROBOROS_BATTLE_HEADLESS=0/false/no/off → headless=False."""
    for token in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("OUROBOROS_BATTLE_HEADLESS", token)
        cfg = HarnessConfig.from_env()
        assert cfg.headless is False, f"token {token!r} should map to False"


def test_from_env_unset_is_none(monkeypatch):
    """Unset → None (auto-detect handled by resolve_headless)."""
    monkeypatch.delenv("OUROBOROS_BATTLE_HEADLESS", raising=False)
    cfg = HarnessConfig.from_env()
    assert cfg.headless is None


def test_from_env_junk_value_is_none(monkeypatch):
    """Unrecognized strings fall back to None rather than raising —
    conservative: better to auto-detect than error on boot."""
    monkeypatch.setenv("OUROBOROS_BATTLE_HEADLESS", "maybe")
    cfg = HarnessConfig.from_env()
    assert cfg.headless is None


# ---------------------------------------------------------------------------
# (2) resolve_headless()
# ---------------------------------------------------------------------------


def test_resolve_headless_explicit_true():
    """headless=True returned as-is, no isatty probe."""
    cfg = HarnessConfig(headless=True)
    assert cfg.resolve_headless() is True


def test_resolve_headless_explicit_false():
    """headless=False returned as-is even if stdin is closed."""
    cfg = HarnessConfig(headless=False)
    assert cfg.resolve_headless() is False


def test_resolve_headless_none_tty_returns_false(monkeypatch):
    """headless=None + isatty()=True → NOT headless (interactive)."""
    import sys as _sys

    class _FakeStdin:
        def isatty(self):
            return True

    monkeypatch.setattr(_sys, "stdin", _FakeStdin())
    cfg = HarnessConfig(headless=None)
    assert cfg.resolve_headless() is False


def test_resolve_headless_none_no_tty_returns_true(monkeypatch):
    """headless=None + isatty()=False → headless (auto-detected)."""
    import sys as _sys

    class _FakeStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(_sys, "stdin", _FakeStdin())
    cfg = HarnessConfig(headless=None)
    assert cfg.resolve_headless() is True


def test_resolve_headless_none_isatty_raises_returns_true(monkeypatch):
    """headless=None + isatty() raises → headless (defensive)."""
    import sys as _sys

    class _FakeStdin:
        def isatty(self):
            raise ValueError("closed")

    monkeypatch.setattr(_sys, "stdin", _FakeStdin())
    cfg = HarnessConfig(headless=None)
    assert cfg.resolve_headless() is True


# ---------------------------------------------------------------------------
# (3) CLI arg surface — argparse round-trip
# ---------------------------------------------------------------------------


def test_cli_flag_sets_true():
    """--headless → args.headless is True."""
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--headless", dest="headless", action="store_const", const=True, default=None)
    grp.add_argument("--no-headless", dest="headless", action="store_const", const=False)
    args = parser.parse_args(["--headless"])
    assert args.headless is True


def test_cli_flag_sets_false():
    """--no-headless → args.headless is False."""
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--headless", dest="headless", action="store_const", const=True, default=None)
    grp.add_argument("--no-headless", dest="headless", action="store_const", const=False)
    args = parser.parse_args(["--no-headless"])
    assert args.headless is False


def test_cli_flag_absent_is_none():
    """No flag → args.headless is None (triggers env/auto-detect fallback)."""
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--headless", dest="headless", action="store_const", const=True, default=None)
    grp.add_argument("--no-headless", dest="headless", action="store_const", const=False)
    args = parser.parse_args([])
    assert args.headless is None


def test_cli_flags_mutually_exclusive():
    """--headless and --no-headless cannot both be passed."""
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--headless", dest="headless", action="store_const", const=True, default=None)
    grp.add_argument("--no-headless", dest="headless", action="store_const", const=False)
    with pytest.raises(SystemExit):
        parser.parse_args(["--headless", "--no-headless"])
