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


# ---------------------------------------------------------------------------
# ov cockpit silence (Slice 2, Task 1) -- named raw-print sites gated on
# PresentationMode. Spy pattern: exercise the extracted free functions
# directly (no need to boot the full stack).
# ---------------------------------------------------------------------------


def _fake_caps_result(*, ok: bool, detail: str = "detail-x"):
    from backend.core.ouroboros.aegis.battle_test_defaults import CapsResult
    return CapsResult(
        ok=ok, session_cap_source="default",
        hourly_burn_cap_source="default", detail=detail,
    )


def _fake_hygiene_result(*, ok: bool, skipped: bool = False, detail: str = "detail-y"):
    from backend.core.ouroboros.aegis.ledger_hygiene import HygieneResult
    return HygieneResult(ok=ok, skipped=skipped, detail=detail)


class TestBattleTestDefaultsBannerGate:
    def test_ok_banner_skipped_in_cockpit(self, capsys):
        bt._print_battle_test_defaults_banner(
            _fake_caps_result(ok=True), PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[BattleTestDefaults]" not in captured.out
        assert "[BattleTestDefaults]" not in captured.err

    def test_ok_banner_prints_in_soak(self, capsys):
        bt._print_battle_test_defaults_banner(
            _fake_caps_result(ok=True), PresentationMode.SOAK,
        )
        captured = capsys.readouterr()
        assert "[BattleTestDefaults]" in captured.out

    def test_warning_banner_unconditional_in_cockpit(self, capsys):
        """The failure path is fatal-adjacent telemetry (Mandate 1) --
        it must print in COCKPIT too."""
        bt._print_battle_test_defaults_banner(
            _fake_caps_result(ok=False), PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[BattleTestDefaults] WARNING" in captured.err

    def test_warning_banner_unconditional_in_soak(self, capsys):
        bt._print_battle_test_defaults_banner(
            _fake_caps_result(ok=False), PresentationMode.SOAK,
        )
        captured = capsys.readouterr()
        assert "[BattleTestDefaults] WARNING" in captured.err


class TestLedgerHygieneBannerGate:
    def test_ok_banner_skipped_in_cockpit(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=True), PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene]" not in captured.out
        assert "[LedgerHygiene]" not in captured.err

    def test_ok_banner_prints_in_soak(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=True), PresentationMode.SOAK,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene]" in captured.out

    def test_skipped_banner_skipped_in_cockpit(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=True, skipped=True),
            PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene]" not in captured.out

    def test_skipped_banner_prints_in_soak(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=True, skipped=True),
            PresentationMode.SOAK,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene] skipped" in captured.out

    def test_warning_banner_unconditional_in_cockpit(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=False), PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene] WARNING" in captured.err

    def test_warning_banner_unconditional_in_soak(self, capsys):
        bt._print_ledger_hygiene_banner(
            _fake_hygiene_result(ok=False), PresentationMode.SOAK,
        )
        captured = capsys.readouterr()
        assert "[LedgerHygiene] WARNING" in captured.err


class _FakeAegisReadyResult:
    def __init__(self, *, aegis_url: str = "http://127.0.0.1:55555", subprocess_pid: int = 4242):
        self.aegis_url = aegis_url
        self.subprocess_pid = subprocess_pid


class TestAegisDaemonReadyBannerGate:
    """``[Aegis] daemon ready`` -- ov cockpit silence (Slice 2 Task 2).
    Same ceremony-gate shape as BattleTestDefaults/LedgerHygiene above:
    COCKPIT withholds the READY-path success line at the source; SOAK
    prints it unchanged. Aegis preflight FAILURE prints live at a
    separate, unconditional call site a few lines above in main() and
    are not covered by this gate (Mandate 1)."""

    def test_ready_banner_skipped_in_cockpit(self, capsys):
        bt._print_aegis_daemon_ready(
            _FakeAegisReadyResult(), PresentationMode.COCKPIT,
        )
        captured = capsys.readouterr()
        assert "[Aegis]" not in captured.out
        assert "[Aegis]" not in captured.err

    def test_ready_banner_prints_in_soak(self, capsys):
        result = _FakeAegisReadyResult(
            aegis_url="http://127.0.0.1:61234", subprocess_pid=9911,
        )
        bt._print_aegis_daemon_ready(result, PresentationMode.SOAK)
        captured = capsys.readouterr()
        assert "[Aegis] daemon ready at http://127.0.0.1:61234" in captured.out
        assert "pid=9911" in captured.out


class TestBootExorcismMarkerGate:
    """The script-top ``[Slice12X.BootExorcism]`` marker is pure ceremony
    and gated via a raw env check (only os/sys are importable that early).
    Verified via the same subprocess-driver technique as
    test_slice12x_boot_supremacy.py::TestPhase1ScriptTopRuntime."""

    def _run_script_top(self, tmp_path, extra_env: dict):
        import os
        import subprocess
        import sys

        driver = tmp_path / "driver.py"
        driver.write_text(
            "with open('scripts/ouroboros_battle_test.py') as f:\n"
            "    src = f.read()\n"
            "cut = src.find('import argparse')\n"
            "assert cut > 0\n"
            "exec(src[:cut + len('import argparse')])\n"
        )
        env = dict(os.environ)
        env.update(extra_env)
        env["PYTHONPATH"] = os.getcwd()
        return subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, timeout=30,
            env=env, cwd=os.getcwd(),
        )

    def test_marker_withheld_in_cockpit(self, tmp_path):
        result = self._run_script_top(
            tmp_path, {"JARVIS_OV_PRESENTATION": "cockpit"},
        )
        assert result.returncode == 0, result.stderr
        assert "Slice12X.BootExorcism" not in result.stderr

    def test_marker_printed_in_soak(self, tmp_path):
        result = self._run_script_top(
            tmp_path, {"JARVIS_OV_PRESENTATION": "soak"},
        )
        assert result.returncode == 0, result.stderr
        assert "Slice12X.BootExorcism" in result.stderr

    def test_marker_printed_when_unset(self, tmp_path):
        import os as _os
        env = {"JARVIS_OV_PRESENTATION": ""}
        result = self._run_script_top(tmp_path, env)
        assert result.returncode == 0, result.stderr
        assert "Slice12X.BootExorcism" in result.stderr


class TestDiscordBridgeBannerGate:
    def test_banner_skipped_in_cockpit(self, monkeypatch, capsys):
        from backend.core.ouroboros.battle_test import harness as bt_harness

        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        bt_harness._print_discord_bridge_boot_banner(True)
        captured = capsys.readouterr()
        assert "[DiscordBridge] boot:" not in captured.err

    def test_banner_prints_in_soak(self, monkeypatch, capsys):
        from backend.core.ouroboros.battle_test import harness as bt_harness

        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
        bt_harness._print_discord_bridge_boot_banner(True)
        captured = capsys.readouterr()
        assert "[DiscordBridge] boot: enabled=True" in captured.err
