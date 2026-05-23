"""Harness integration — aegis_preflight() end-to-end.

Spawns a real Aegis subprocess via `python -m`, reads the bootstrap
payload, scrubs creds, asserts daemon health endpoint reachable.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import aiohttp
import pytest

from backend.core.ouroboros.aegis.env_scrub import (
    assert_no_upstream_credentials,
)
from backend.core.ouroboros.aegis.flags import ENV_AEGIS_ENABLED
from backend.core.ouroboros.aegis.preflight import (
    PreflightOutcome,
    aegis_preflight,
)


# ---------------------------------------------------------------------------
# Skipped-when-disabled
# ---------------------------------------------------------------------------


async def test_preflight_skipped_when_disabled(monkeypatch, tmp_path):
    """JARVIS_AEGIS_ENABLED=false → SKIPPED_DISABLED, zero subprocess."""
    monkeypatch.setenv(ENV_AEGIS_ENABLED, "false")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(tmp_path / "spend.jsonl"))

    fake_env = {
        ENV_AEGIS_ENABLED: "false",
        "ANTHROPIC_API_KEY": "should-survive",
    }
    result = await aegis_preflight(env=fake_env)
    assert result.outcome is PreflightOutcome.SKIPPED_DISABLED
    # When disabled, creds are NOT scrubbed.
    assert fake_env.get("ANTHROPIC_API_KEY") == "should-survive"


# ---------------------------------------------------------------------------
# Full ready cycle — spawn + read + scrub + health
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_preflight_ready_full_cycle(monkeypatch, tmp_path):
    """JARVIS_AEGIS_ENABLED=true with caps → daemon spawns, payload
    landed, creds scrubbed, /health reachable."""
    monkeypatch.setenv(ENV_AEGIS_ENABLED, "true")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_DIR", str(tmp_path / "bootstrap"))
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(tmp_path / "spend.jsonl"))
    # Loosen caps so the daemon can construct without underflow.
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "10.0")
    monkeypatch.setenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", "5.0")
    for route in ("IMMEDIATE", "STANDARD", "COMPLEX", "BACKGROUND", "SPECULATIVE"):
        monkeypatch.setenv(f"JARVIS_AEGIS_ROUTE_CAP_{route}_USD", "1.0")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_TIMEOUT_S", "20")

    # Synthetic env we'll hand to preflight so we don't mutate the real os.environ.
    fake_env = dict(os.environ)
    fake_env["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    fake_env["DOUBLEWORD_API_KEY"] = "test-doubleword-key"

    result = await aegis_preflight(env=fake_env)
    pid = result.subprocess_pid

    try:
        assert result.outcome is PreflightOutcome.READY, (
            f"preflight failed: {result.outcome.value} — {result.detail}"
        )
        assert result.aegis_url is not None
        assert result.bootstrap_psk is not None
        assert result.aegis_url.startswith("http://127.0.0.1:")
        # Credentials MUST be absent post-scrub.
        assert "ANTHROPIC_API_KEY" not in fake_env
        assert "DOUBLEWORD_API_KEY" not in fake_env
        assert_no_upstream_credentials(fake_env)
        # Aegis URL + PSK exposed.
        assert fake_env.get("JARVIS_AEGIS_URL") == result.aegis_url
        assert fake_env.get("JARVIS_AEGIS_BOOTSTRAP_PSK") == result.bootstrap_psk

        # /health must be reachable on the bound URL.
        async with aiohttp.ClientSession() as sess:
            # Poll briefly — daemon writes payload BEFORE it starts the
            # server, so there can be a few ms gap.
            deadline = time.monotonic() + 5.0
            last_exc = None
            while time.monotonic() < deadline:
                try:
                    async with sess.get(f"{result.aegis_url}/health") as resp:
                        assert resp.status == 200
                        body = await resp.json()
                        assert body["ok"] is True
                        assert body["psk_consumed"] is False
                        return
                except (aiohttp.ClientError, AssertionError) as exc:
                    last_exc = exc
                    await asyncio.sleep(0.05)
            pytest.fail(f"/health not reachable within 5s: {last_exc}")
    finally:
        # Clean up the daemon subprocess so the test isolates.
        if pid is not None:
            try:
                os.kill(pid, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass


# ---------------------------------------------------------------------------
# Failure mode — bootstrap timeout
# ---------------------------------------------------------------------------


async def test_preflight_failed_bootstrap_timeout(monkeypatch, tmp_path):
    """Force a bootstrap timeout by making the timeout absurdly short."""
    monkeypatch.setenv(ENV_AEGIS_ENABLED, "true")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(tmp_path / "wal.jsonl"))
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "1.0")
    monkeypatch.setenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", "1.0")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_TIMEOUT_S", "1")

    # Replace _spawn_daemon with a stub that spawns a process which
    # NEVER writes the payload (just `sleep 60`).
    import backend.core.ouroboros.aegis.preflight as preflight_mod

    real_spawn = preflight_mod._spawn_daemon

    def fake_spawn(*, bootstrap_out, credentials, bind_host_override=None):
        del bootstrap_out, credentials, bind_host_override
        return subprocess.Popen(
            ["sleep", "60"], stdin=subprocess.DEVNULL, close_fds=True,
        )

    monkeypatch.setattr(preflight_mod, "_spawn_daemon", fake_spawn)
    result = None
    try:
        result = await aegis_preflight(env=dict(os.environ))
        assert result.outcome is PreflightOutcome.FAILED_BOOTSTRAP_TIMEOUT
    finally:
        monkeypatch.setattr(preflight_mod, "_spawn_daemon", real_spawn)
        if result is not None and result.subprocess_pid:
            try:
                os.kill(result.subprocess_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
