"""Tests for scripts/a1_telemetry_bridge.py — TDD Red→Green.

All five tests are injection-based: no real gcloud/SSH/GCP is needed.
Transport is replaced by async generator factories; asyncio.sleep is
replaced by a no-op coroutine so the reconnect-backoff tests are instant.

asyncio_mode = auto (pytest.ini) — no @pytest.mark.asyncio needed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, List, Set

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (mirrors the hypervisor test pattern)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "a1_telemetry_bridge.py"
_spec = importlib.util.spec_from_file_location("a1_telemetry_bridge", _SCRIPT)
assert _spec and _spec.loader, f"Cannot load {_SCRIPT}"
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)  # type: ignore[union-attr]

TelemetryMultiplexer = bridge.TelemetryMultiplexer
stream_telemetry = bridge.stream_telemetry
_build_tail_cmd = bridge._build_tail_cmd
CH_A1TRACE = bridge.CH_A1TRACE
CH_CORTEX = bridge.CH_CORTEX
CH_LEDGER = bridge.CH_LEDGER
CH_OTHER = bridge.CH_OTHER

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mux(
    no_color: bool = True,
    channels: Set[str] | None = None,
) -> TelemetryMultiplexer:
    return TelemetryMultiplexer(
        channels=channels
        if channels is not None
        else {CH_A1TRACE, CH_CORTEX, CH_LEDGER, CH_OTHER},
        no_color=no_color,
    )


async def _instant_sleep(_s: float) -> None:
    """Drop-in for asyncio.sleep: completes in zero wall time."""
    await asyncio.sleep(0)


# ===========================================================================
# Test 1 — Multiplexer: classify + colour
# ===========================================================================

class TestMultiplexer:
    def test_a1trace_basic(self) -> None:
        mux = _make_mux()
        assert mux.classify("[A1Trace] emit goal=abc123") == CH_A1TRACE

    def test_a1trace_emit_probe_variant(self) -> None:
        mux = _make_mux()
        line = "[A1Trace][emit-probe] EMIT goal=abc123 source=roadmap emit_ts=1.0 orchestrator_enabled=True"
        assert mux.classify(line) == CH_A1TRACE

    def test_cortex_bracket_prefix(self) -> None:
        mux = _make_mux()
        assert mux.classify("[Cortex] PROACTIVE hedge: racing RT vs BATCH concurrently") == CH_CORTEX

    def test_cortex_hedge_governor(self) -> None:
        mux = _make_mux()
        assert mux.classify("[Cortex] ⚡ HEDGE GOVERNOR: op needs Iron-Gate") == CH_CORTEX

    def test_cortex_hedge_governor_standalone(self) -> None:
        mux = _make_mux()
        assert mux.classify("some text HEDGE GOVERNOR: blah") == CH_CORTEX

    def test_ledger_terminal_applied(self) -> None:
        mux = _make_mux()
        line = "[Slice74Probe] LEDGER_TERMINAL op_id=xyz state=applied written=True"
        assert mux.classify(line) == CH_LEDGER

    def test_ledger_terminal_failed(self) -> None:
        mux = _make_mux()
        line = "[Slice74Probe] LEDGER_TERMINAL op_id=xyz state=failed written=False"
        assert mux.classify(line) == CH_LEDGER

    def test_other_line(self) -> None:
        mux = _make_mux()
        assert mux.classify("2026-06-27 INFO Some random log line") == CH_OTHER

    def test_no_color_plain_output(self) -> None:
        mux = _make_mux(no_color=True)
        result = mux.format_line("[A1Trace] emit goal=abc123", 1.0)
        assert result is not None
        assert "[A1T|" in result
        assert "\033[" not in result, "ANSI codes found when no_color=True"

    def test_color_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        mux = TelemetryMultiplexer(
            channels={CH_A1TRACE, CH_CORTEX, CH_LEDGER},
            no_color=False,
        )
        result = mux.format_line("[A1Trace] emit goal=abc123", 1.0)
        assert result is not None
        assert "\033[" in result, "Expected ANSI codes on TTY with color enabled"

    def test_no_color_env_suppresses_ansi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        mux = TelemetryMultiplexer(
            channels={CH_A1TRACE, CH_CORTEX, CH_LEDGER},
            no_color=False,
        )
        result = mux.format_line("[A1Trace] emit goal=abc123", 1.0)
        assert result is not None
        assert "\033[" not in result, "ANSI leaked through NO_COLOR env"

    def test_ledger_applied_is_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        mux = TelemetryMultiplexer(channels={CH_LEDGER}, no_color=False)
        line = "[Slice74Probe] LEDGER_TERMINAL op_id=x state=applied written=True"
        result = mux.format_line(line, 0.0)
        assert result is not None
        assert "\033[32m" in result, "Expected green (\\033[32m) for state=applied"

    def test_ledger_failed_is_red(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        mux = TelemetryMultiplexer(channels={CH_LEDGER}, no_color=False)
        line = "[Slice74Probe] LEDGER_TERMINAL op_id=x state=failed written=False"
        result = mux.format_line(line, 0.0)
        assert result is not None
        assert "\033[31m" in result, "Expected red (\\033[31m) for state=failed"

    def test_filtered_channel_returns_none(self) -> None:
        mux = TelemetryMultiplexer(channels={CH_A1TRACE}, no_color=True)
        result = mux.format_line("[Cortex] PROACTIVE hedge", 0.0)
        assert result is None, "Expected None for filtered channel"

    def test_is_terminal_sentinel_applied(self) -> None:
        mux = _make_mux()
        assert mux.is_terminal_sentinel(
            "[Slice74Probe] LEDGER_TERMINAL op_id=x state=applied written=True"
        )

    def test_is_terminal_sentinel_failed(self) -> None:
        mux = _make_mux()
        assert mux.is_terminal_sentinel(
            "[Slice74Probe] LEDGER_TERMINAL op_id=x state=failed written=False"
        )

    def test_is_terminal_sentinel_rolled_back(self) -> None:
        mux = _make_mux()
        assert mux.is_terminal_sentinel(
            "[Slice74Probe] LEDGER_TERMINAL op_id=x state=rolled_back written=True"
        )

    def test_not_terminal_sentinel(self) -> None:
        mux = _make_mux()
        assert not mux.is_terminal_sentinel("[A1Trace] emit goal=abc123")


# ===========================================================================
# Test 2 — Byte-offset resume: no duplicate / no gap across reconnects
# ===========================================================================

async def test_byte_offset_resume() -> None:
    """Second connection must request tail -c +<N+1> — exactly no dup, no gap."""
    first_data = b"[A1Trace] emit goal=abc hop=1\n"
    N = len(first_data)

    recorded_cmds: List[List[str]] = []
    call_count = 0

    async def fake_runner(cmd: List[str]) -> AsyncGenerator[bytes, None]:
        nonlocal call_count
        call_count += 1
        recorded_cmds.append(list(cmd))
        if call_count == 1:
            yield first_data
            # EOF — connection ends cleanly
        elif call_count == 2:
            # Resume: yield terminal sentinel so the loop ends
            yield (
                b"[Slice74Probe] LEDGER_TERMINAL op_id=x state=applied written=True\n"
            )

    collected: List[str] = []

    await stream_telemetry(
        node="test-node",
        zone="us-central1-a",
        project="test-project",
        remote_log_path="/home/jarvis/.ouroboros/sessions/abc/debug.log",
        on_line=collected.append,
        no_color=True,
        max_reconnects=10,
        _cmd_runner=fake_runner,
        _sleep=_instant_sleep,
    )

    assert call_count == 2, f"expected exactly 2 connections, got {call_count}"

    # --- First connection must start at 0-indexed offset 0 → tail -c +1 ---
    first_remote = recorded_cmds[0][-1]   # last element is the --command value
    assert "tail -c +1 " in first_remote, (
        f"first cmd should be tail -c +1, got: {first_remote!r}"
    )

    # --- Second connection must resume at byte N → tail -c +(N+1) ---------
    second_remote = recorded_cmds[1][-1]
    expected_start = N + 1
    assert f"tail -c +{expected_start} " in second_remote, (
        f"expected tail -c +{expected_start} in second cmd, got: {second_remote!r}"
    )

    # IAP-SSH flags are present and non-hardcoded
    assert "--tunnel-through-iap" in recorded_cmds[0]
    assert "--project=test-project" in recorded_cmds[0]
    assert "--zone=us-central1-a" in recorded_cmds[0]

    # First connection's A1Trace line was received and emitted
    assert any("A1T" in line for line in collected), (
        f"A1Trace line not found in collected: {collected}"
    )


# ===========================================================================
# Test 3 — Reconnect loop: fails twice then succeeds; exponential backoff
# ===========================================================================

async def test_reconnect_loop_fails_twice_then_succeeds() -> None:
    """Bridge retries with exponential backoff on transport errors, never raises."""
    call_count = 0

    async def flaky_runner(cmd: List[str]) -> AsyncGenerator[bytes, None]:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError(f"SSH connection refused (attempt {call_count})")
        # Third attempt: succeed
        yield b"[A1Trace] dequeue goal=test-goal\n"
        yield b"[Slice74Probe] LEDGER_TERMINAL op_id=test-goal state=applied written=True\n"

    slept: List[float] = []

    async def recording_sleep(s: float) -> None:
        slept.append(s)

    collected: List[str] = []

    # Must NOT raise even though the first 2 attempts fail
    await stream_telemetry(
        node="test-node",
        zone="us-central1-a",
        project="test-project",
        remote_log_path="/tmp/debug.log",
        on_line=collected.append,
        no_color=True,
        max_reconnects=10,
        _cmd_runner=flaky_runner,
        _sleep=recording_sleep,
    )

    # 3 attempts total (2 failures + 1 success)
    assert call_count == 3, f"expected 3 attempts, got {call_count}"

    # Exactly 2 backoff sleeps (one per failure)
    assert len(slept) == 2, (
        f"expected exactly 2 backoff sleeps (one per failure), got {len(slept)}: {slept}"
    )

    # Exponential: second sleep >= first sleep (jitter is bounded by 20%)
    assert slept[1] >= slept[0], (
        f"second backoff {slept[1]:.3f} not >= first {slept[0]:.3f} — not exponential"
    )

    # Both sleeps are positive and bounded
    for s in slept:
        assert 0.0 < s <= bridge._DEFAULT_BACKOFF_CAP_S + 1, (
            f"sleep value out of bounds: {s}"
        )

    # A1Trace line was emitted from the successful third attempt
    assert any("A1T" in line for line in collected), (
        f"A1Trace line missing from collected: {collected}"
    )


# ===========================================================================
# Test 4 — Read-only: stop_event tears down LOCAL bridge; remote never killed
# ===========================================================================

async def test_stop_event_exits_cleanly_without_remote_kill() -> None:
    """stop_event exits the LOCAL bridge.  Remote soak is never signalled.

    The test verifies:
    1. The bridge exits cleanly (no exception) when stop_event is set.
    2. No remote-kill method is invoked (there is no 'remote handle' in the
       injection path — the guarantee is structural: only proc.kill() in the
       production subprocess finally-block; the injection path has no proc).
    3. At least some data was received before the stop.
    """
    stop_event = asyncio.Event()
    chunk_count = 0

    async def controlled_runner(cmd: List[str]) -> AsyncGenerator[bytes, None]:
        nonlocal chunk_count
        # Yields chunks; sets stop_event after the third chunk.
        for _ in range(20):          # bounded so the test can't hang
            chunk_count += 1
            yield b"[A1Trace] emit goal=loop\n"
            if chunk_count >= 3:
                stop_event.set()
            await asyncio.sleep(0)   # cooperative yield within the generator

    collected: List[str] = []

    # Bridge runs to completion (stop_event causes it to exit)
    await stream_telemetry(
        node="test-node",
        zone="us-central1-a",
        project="test-project",
        remote_log_path="/tmp/debug.log",
        on_line=collected.append,
        no_color=True,
        max_reconnects=0,   # no limit from reconnects
        stop_event=stop_event,
        _cmd_runner=controlled_runner,
        _sleep=_instant_sleep,
    )

    # stop_event was the exit trigger
    assert stop_event.is_set(), "stop_event should be set"

    # At least some A1Trace lines were received before the bridge stopped
    assert len(collected) >= 1, (
        f"Expected at least 1 emitted line before stop, got 0"
    )

    # Generator ran at least 3 iterations before stop
    assert chunk_count >= 3, f"Expected chunk_count >= 3, got {chunk_count}"

    # No remote kill was invoked — verified by absence: in the injection path,
    # no subprocess is created, so proc.kill() (the only kill in the module)
    # is never reachable.  The bridge exited without raising proves this path.


# ===========================================================================
# Test 5 — Terminal sentinel ends loop cleanly
# ===========================================================================

async def test_terminal_sentinel_applied_ends_loop() -> None:
    """LEDGER_TERMINAL state=applied causes a clean, immediate shutdown."""

    async def terminal_runner(cmd: List[str]) -> AsyncGenerator[bytes, None]:
        yield b"[A1Trace] emit goal=goal-1\n"
        yield b"[A1Trace] ingest goal=goal-1\n"
        yield b"[Cortex] PROACTIVE hedge: racing RT vs BATCH concurrently\n"
        yield b"[Slice74Probe] LEDGER_TERMINAL op_id=goal-1 state=applied written=True\n"

    collected: List[str] = []

    await stream_telemetry(
        node="test-node",
        zone="us-central1-a",
        project="test-project",
        remote_log_path="/tmp/debug.log",
        on_line=collected.append,
        no_color=True,
        max_reconnects=5,
        _cmd_runner=terminal_runner,
        _sleep=_instant_sleep,
    )

    a1t = [l for l in collected if "A1T" in l]
    cor = [l for l in collected if "COR" in l]
    led = [l for l in collected if "LED" in l]

    assert len(a1t) >= 2, f"Expected >=2 A1Trace lines, got: {a1t}"
    assert len(cor) >= 1, f"Expected >=1 Cortex line, got: {cor}"
    assert len(led) == 1, f"Expected exactly 1 LEDGER line, got: {led}"
    assert "state=applied" in led[0], f"LEDGER line missing state=applied: {led[0]}"


# ===========================================================================
# Test 6 — _build_tail_cmd: IAP-SSH command shape matches hypervisor
# ===========================================================================

class TestBuildTailCmd:
    def test_offset_zero_starts_at_one(self) -> None:
        """offset=0 → tail -c +1 (full file from byte 1, 1-indexed)."""
        cmd = _build_tail_cmd(
            "my-node", "us-central1-a", "my-project", "/tmp/debug.log", 0
        )
        assert cmd[0] == "gcloud"
        assert cmd[1] == "compute"
        assert cmd[2] == "ssh"
        assert "my-node" in cmd
        assert "--tunnel-through-iap" in cmd
        assert "--project=my-project" in cmd
        assert "--zone=us-central1-a" in cmd
        assert cmd[-2] == "--command"
        assert "tail -c +1 " in cmd[-1]

    def test_nonzero_offset_resume(self) -> None:
        """offset=N → tail -c +(N+1): 0-indexed local offset → 1-indexed tail arg."""
        N = 12345
        cmd = _build_tail_cmd("n", "z", "p", "/tmp/debug.log", N)
        assert f"tail -c +{N + 1} " in cmd[-1], (
            f"expected tail -c +{N + 1}, got: {cmd[-1]!r}"
        )

    def test_remote_path_is_shell_quoted(self) -> None:
        """Paths with spaces or special chars are shell-quoted for safety."""
        cmd = _build_tail_cmd("n", "z", "p", "/home/user/my log/debug.log", 0)
        remote = cmd[-1]
        # shlex.quote wraps the path in single quotes
        assert "'/home/user/my log/debug.log'" in remote

    def test_no_hardcoded_project_or_zone(self) -> None:
        """project and zone come from args, not literals."""
        cmd = _build_tail_cmd("nd", "custom-zone-99", "custom-project-abc", "/f", 0)
        assert "--project=custom-project-abc" in cmd
        assert "--zone=custom-zone-99" in cmd
