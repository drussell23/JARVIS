"""Tests for the Chaos Readiness Handshake (chaos_injector_ast).

THE BUG (A1 live soak): the ChaosInjector mutated a file BEFORE O+V's
TrinityEventBus + TestWatcher were initialized and listening -> the live
``fs.changed`` event fired into a dead bus (or fired before any subscriber
existed) and the mutation was lost. Boot hydration covers the offline case
from ground truth; the readiness handshake covers the live case by REFUSING to
mutate until the bus + TestWatcher are demonstrably ready.

``await_chaos_readiness`` is an ASYNC, bounded-deadline, exponential-backoff
probe of a readiness surface:

* HTTP readiness surface (``/channel/health`` / ``/observability/health``)
  parsed for a ``testwatcher_ready`` truthy field, OR
* a stdout/log BOOT-MARKER (``[TestWatcher] READY subscribed=fs.changed.*``)

On timeout it returns ``(False, "CHAOS_READINESS_TIMEOUT")`` -- a graceful
inject failure with a clear locus, never a blind mutate-into-a-dead-bus.

Gated ``JARVIS_CHAOS_READINESS_PROBE_ENABLED`` (default true). NO ``time.sleep``,
NO fixed wait constant as the sync mechanism (env-tuned bounded deadlines only).
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, List, Optional, Tuple

import pytest

import scripts.chaos_injector_ast as ci


# ---------------------------------------------------------------------------
# 1. Probe waits for the ready signal then returns True
# ---------------------------------------------------------------------------


class TestProbeBecomesReady:
    def test_returns_true_when_marker_appears(self, monkeypatch: Any) -> None:
        # The readiness check flips ready on the 3rd poll -> proves the probe
        # waits (does not assume ready) and converges.
        calls: List[int] = []

        async def _check() -> bool:
            calls.append(1)
            return len(calls) >= 3

        ok, locus = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=5.0, initial_backoff_s=0.001,
            )
        )
        assert ok is True
        assert locus == ""
        assert len(calls) >= 3

    def test_returns_true_immediately_if_ready(self) -> None:
        async def _check() -> bool:
            return True

        ok, locus = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=5.0, initial_backoff_s=0.001,
            )
        )
        assert ok is True
        assert locus == ""


# ---------------------------------------------------------------------------
# 2. Times out gracefully with CHAOS_READINESS_TIMEOUT when never ready
# ---------------------------------------------------------------------------


class TestProbeTimesOut:
    def test_never_ready_times_out_gracefully(self) -> None:
        async def _check() -> bool:
            return False  # never ready

        ok, locus = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=0.05, initial_backoff_s=0.001,
            )
        )
        assert ok is False
        assert locus == "CHAOS_READINESS_TIMEOUT"

    def test_check_errors_are_swallowed_then_timeout(self) -> None:
        async def _check() -> bool:
            raise RuntimeError("surface unreachable")

        # A failing surface must NOT crash the probe -- it degrades to timeout.
        ok, locus = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=0.05, initial_backoff_s=0.001,
            )
        )
        assert ok is False
        assert locus == "CHAOS_READINESS_TIMEOUT"


# ---------------------------------------------------------------------------
# 3. Backoff (not fixed sleep): it polls multiple times + uses asyncio.sleep
# ---------------------------------------------------------------------------


class TestBackoffNotFixedSleep:
    def test_uses_asyncio_sleep_with_growing_backoff(
        self, monkeypatch: Any
    ) -> None:
        sleeps: List[float] = []

        real_sleep = asyncio.sleep

        async def _spy_sleep(delay: float, *a: Any, **k: Any) -> None:
            sleeps.append(delay)
            await real_sleep(0)  # yield, but don't actually wait

        monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

        attempts: List[int] = []

        async def _check() -> bool:
            attempts.append(1)
            return len(attempts) >= 4

        ok, _ = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=10.0,
                initial_backoff_s=0.01, backoff_factor=2.0,
            )
        )
        assert ok is True
        # Polled more than once (it waited) and used asyncio.sleep between polls.
        assert len(sleeps) >= 2
        # Exponential growth: a later backoff is strictly larger than the first.
        assert sleeps[-1] > sleeps[0]

    def test_source_has_no_time_sleep(self) -> None:
        # Structural guard: the probe must not use the blocking time.sleep as
        # its synchronization mechanism.
        src = inspect.getsource(ci.await_chaos_readiness)
        assert "time.sleep" not in src
        assert "asyncio.sleep" in src


# ---------------------------------------------------------------------------
# 4. HTTP readiness check parses testwatcher_ready
# ---------------------------------------------------------------------------


class TestHttpReadinessCheck:
    def test_http_check_ready_true(self, monkeypatch: Any) -> None:
        # A fake HTTP fetch returns a health JSON with testwatcher_ready=true.
        async def _fake_fetch(url: str, timeout_s: float) -> Optional[dict]:
            return {"testwatcher_ready": True, "enabled": True}

        monkeypatch.setattr(ci, "_fetch_health_json", _fake_fetch)
        check = ci.make_http_readiness_check("http://127.0.0.1:8123/channel/health")
        assert asyncio.run(check()) is True

    def test_http_check_not_ready(self, monkeypatch: Any) -> None:
        async def _fake_fetch(url: str, timeout_s: float) -> Optional[dict]:
            return {"testwatcher_ready": False}

        monkeypatch.setattr(ci, "_fetch_health_json", _fake_fetch)
        check = ci.make_http_readiness_check("http://127.0.0.1:8123/channel/health")
        assert asyncio.run(check()) is False

    def test_http_check_unreachable_returns_false(
        self, monkeypatch: Any
    ) -> None:
        async def _fake_fetch(url: str, timeout_s: float) -> Optional[dict]:
            return None  # connection refused / not up yet

        monkeypatch.setattr(ci, "_fetch_health_json", _fake_fetch)
        check = ci.make_http_readiness_check("http://127.0.0.1:8123/channel/health")
        assert asyncio.run(check()) is False


# ---------------------------------------------------------------------------
# 5. Log boot-marker readiness check
# ---------------------------------------------------------------------------


class TestLogMarkerReadinessCheck:
    def test_marker_present(self, tmp_path: Any) -> None:
        log = tmp_path / "soak_stdout.log"
        log.write_text(
            "boot...\n[TestWatcher] READY subscribed=fs.changed.*\nmore\n",
            encoding="utf-8",
        )
        check = ci.make_log_marker_readiness_check(str(log))
        assert asyncio.run(check()) is True

    def test_marker_absent(self, tmp_path: Any) -> None:
        log = tmp_path / "soak_stdout.log"
        log.write_text("boot...\nstill warming up\n", encoding="utf-8")
        check = ci.make_log_marker_readiness_check(str(log))
        assert asyncio.run(check()) is False

    def test_missing_log_is_not_ready(self, tmp_path: Any) -> None:
        check = ci.make_log_marker_readiness_check(str(tmp_path / "nope.log"))
        assert asyncio.run(check()) is False


# ---------------------------------------------------------------------------
# 6. Gated OFF -> probe is a no-op that reports ready (legacy blind inject)
# ---------------------------------------------------------------------------


class TestGatedOff:
    def test_probe_disabled_returns_ready_immediately(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("JARVIS_CHAOS_READINESS_PROBE_ENABLED", "false")

        calls: List[int] = []

        async def _check() -> bool:
            calls.append(1)
            return False

        ok, locus = asyncio.run(
            ci.await_chaos_readiness(
                check=_check, deadline_s=5.0, initial_backoff_s=0.001,
            )
        )
        # OFF == legacy: do not block, do not consult the surface.
        assert ok is True
        assert locus == ""
        assert calls == []

    def test_probe_enabled_default_true(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("JARVIS_CHAOS_READINESS_PROBE_ENABLED", raising=False)
        assert ci.readiness_probe_enabled() is True
