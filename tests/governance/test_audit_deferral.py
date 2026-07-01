"""Activity-Gated Audit Deferral -- cure the impatient grader.

Live evidence (Window-2, bt-iso-1782944904): `run_watch`'s ceiling expired and the
partial FAILED verdict was rendered while the resumed ops were actively streaming at
31% progress on the 32B. The assessor must not render a blind verdict over a live
organism: on ceiling expiry it consults an activity probe (the stream_heartbeat
cross-process FILE mirror) and defers assessment in bounded slices, up to an
absolute deferral ceiling. The harness wall-clock hard cap stays completely blind
(Slice-47 Watchdog Isolation Invariant) -- this gates only the AUDITOR's verdict.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import time

import pytest


def _load_auditor():
    import sys
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "a1_graduation_auditor.py"
    spec = importlib.util.spec_from_file_location("_aud_defer_test", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_aud_defer_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def aud_mod():
    return _load_auditor()


def _run(coro):
    return asyncio.run(coro)


def _watch(mod, tmp_path, *, timeout_s, probe, logs):
    auditor = mod.A1GraduationAuditor(chaos_manifest_path=None)
    log_file = tmp_path / "debug.log"
    log_file.write_text("")
    return mod.run_watch(
        auditor, base=None, log_file=str(log_file),
        timeout_s=timeout_s, log=logs.append, activity_probe=probe,
    )


class TestDeferral:
    def test_defers_while_active_then_concludes(self, aud_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_SLICE_S", "0.2")
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_ABSOLUTE_S", "10")
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return calls["n"] <= 2          # active for two slices, then idle

        logs = []
        start = time.monotonic()
        verdict = _run(_watch(aud_mod, tmp_path, timeout_s=0.3, probe=probe, logs=logs))
        elapsed = time.monotonic() - start
        assert elapsed >= 0.3 + 2 * 0.2 - 0.05      # ceiling + two deferral slices
        assert verdict.proven is False
        assert any("defer" in ln.lower() for ln in logs)

    def test_absolute_ceiling_bounds_deferral(self, aud_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_SLICE_S", "0.2")
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_ABSOLUTE_S", "0.5")
        logs = []
        start = time.monotonic()
        _run(_watch(aud_mod, tmp_path, timeout_s=0.2, probe=lambda: True, logs=logs))
        elapsed = time.monotonic() - start
        assert elapsed < 0.2 + 0.5 + 1.0            # bounded: never runs away

    def test_disabled_master_reverts_to_legacy(self, aud_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_ENABLED", "false")
        monkeypatch.setenv("JARVIS_A1_AUDIT_DEFER_SLICE_S", "5")
        logs = []
        start = time.monotonic()
        _run(_watch(aud_mod, tmp_path, timeout_s=0.2, probe=lambda: True, logs=logs))
        elapsed = time.monotonic() - start
        assert elapsed < 1.0                        # no deferral: legacy ceiling only

    def test_no_probe_and_no_default_is_legacy(self, aud_mod, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_STREAM_HEARTBEAT_FILE", raising=False)
        logs = []
        start = time.monotonic()
        _run(_watch(aud_mod, tmp_path, timeout_s=0.2, probe=None, logs=logs))
        elapsed = time.monotonic() - start
        assert elapsed < 1.0


class TestDefaultActivityProbe:
    def test_fresh_heartbeat_file_is_active(self, aud_mod, tmp_path, monkeypatch):
        hb = tmp_path / "hb.txt"
        hb.write_text(str(time.time()))
        monkeypatch.setenv("JARVIS_STREAM_HEARTBEAT_FILE", str(hb))
        monkeypatch.setenv("JARVIS_A1_AUDIT_ACTIVITY_WINDOW_S", "90")
        assert aud_mod.default_activity_probe() is True

    def test_stale_heartbeat_file_is_inactive(self, aud_mod, tmp_path, monkeypatch):
        hb = tmp_path / "hb.txt"
        hb.write_text(str(time.time() - 3600))
        monkeypatch.setenv("JARVIS_STREAM_HEARTBEAT_FILE", str(hb))
        monkeypatch.setenv("JARVIS_A1_AUDIT_ACTIVITY_WINDOW_S", "90")
        assert aud_mod.default_activity_probe() is False

    def test_missing_file_or_env_is_inactive(self, aud_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_STREAM_HEARTBEAT_FILE", str(tmp_path / "nope.txt"))
        assert aud_mod.default_activity_probe() is False
        monkeypatch.delenv("JARVIS_STREAM_HEARTBEAT_FILE", raising=False)
        assert aud_mod.default_activity_probe() is False
