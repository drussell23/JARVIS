"""Tests for Ticket B — SIGHUP handler + partial-summary v1.1b.

Ticket: memory/project_followup_partial_summary_on_interrupt.md (2026-04-23).

Background: #7 GENERATE S2 (``bt-2026-04-23-070317``) was killed via
Claude Code's ``TaskStop`` on the parent bash pipeline. The Python
harness received SIGHUP — Python's default action is to terminate
without running ``atexit`` handlers — and left a session dir with only
``debug.log`` and no ``summary.json``. Ticket B routes SIGHUP through
the same sync-partial-write path as SIGTERM/SIGINT and stamps
``session_outcome="incomplete_kill"`` plus a signal-specific
``stop_reason`` so audit tooling can distinguish parent-death from
operator-interrupt from container-kill.

Coverage:
- v1.1b schema: ``session_outcome`` + ``last_activity_ts`` fields
  present when non-None, absent when None (back-compat with v1.1a
  consumers).
- ``_atexit_fallback_write(session_outcome=...)`` passes the field
  through to SessionRecorder.save_summary.
- ``_handle_shutdown_signal(signal_name)`` stamps the per-signal
  ``_stop_reason`` before calling the sync write.
- ``register_signal_handlers`` installs SIGHUP alongside SIGTERM/SIGINT
  (getattr-guarded for Windows).
- SIGPIPE is ignored at the process level so broken-pipe writes during
  shutdown do not crash the interpreter.
"""
from __future__ import annotations

import atexit
import json
import signal
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-sighup-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    harness._started_at = 1_700_000_000.0  # deterministic anchor
    yield harness
    atexit.unregister(harness._atexit_fallback_write)


# ---------------------------------------------------------------------------
# (1) Schema v1.1b: additive fields
# ---------------------------------------------------------------------------


def test_save_summary_omits_session_outcome_when_none(tmp_harness):
    """Back-compat: when session_outcome is None the field is absent
    from summary.json — v1.1a consumers stay happy."""
    tmp_harness._session_recorder.save_summary(
        output_dir=tmp_harness._session_dir,
        stop_reason="idle_timeout",
        duration_s=10.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0},
        convergence_state="INSUFFICIENT_DATA",
        convergence_slope=0.0,
        convergence_r2=0.0,
    )
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert "session_outcome" not in payload
    assert "last_activity_ts" not in payload


def test_save_summary_emits_session_outcome_when_set(tmp_harness):
    """When session_outcome is passed, it lands in summary.json."""
    tmp_harness._session_recorder.save_summary(
        output_dir=tmp_harness._session_dir,
        stop_reason="sighup",
        duration_s=10.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0},
        convergence_state="INSUFFICIENT_DATA",
        convergence_slope=0.0,
        convergence_r2=0.0,
        session_outcome="incomplete_kill",
        last_activity_ts=1_700_000_050.0,
    )
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "incomplete_kill"
    assert payload["last_activity_ts"] == 1_700_000_050.0
    assert payload["stop_reason"] == "sighup"
    # Schema version unchanged (additive fields, not a breaking bump).
    assert payload["schema_version"] == 2


# ---------------------------------------------------------------------------
# (2) _atexit_fallback_write propagates session_outcome
# ---------------------------------------------------------------------------


def test_atexit_fallback_write_passes_session_outcome(tmp_harness):
    """Calling _atexit_fallback_write(session_outcome="incomplete_kill")
    must land that value in summary.json via SessionRecorder."""
    tmp_harness._atexit_fallback_write(session_outcome="incomplete_kill")
    assert tmp_harness._summary_written is True
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "incomplete_kill"


def test_atexit_fallback_write_without_kwarg_omits_field(tmp_harness):
    """Calling _atexit_fallback_write() with no kwarg leaves
    session_outcome absent — atexit-only path doesn't know it was
    "incomplete_kill"; distinguishes from signal-driven path."""
    tmp_harness._atexit_fallback_write()
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert "session_outcome" not in payload


def test_atexit_fallback_write_idempotent_on_second_call(tmp_harness):
    """_summary_written flag prevents double-writes. Second call with
    different args is a no-op — first write wins."""
    tmp_harness._atexit_fallback_write(session_outcome="incomplete_kill")
    # Second call should not overwrite.
    tmp_harness._atexit_fallback_write(session_outcome="different_value")
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "incomplete_kill"


# ---------------------------------------------------------------------------
# (3) _handle_shutdown_signal stamps per-signal stop_reason
# ---------------------------------------------------------------------------


def test_handle_sighup_stamps_sighup_stop_reason(tmp_harness):
    """SIGHUP handler → stop_reason="sighup" + session_outcome="incomplete_kill"."""
    import asyncio
    tmp_harness._shutdown_event = asyncio.Event()
    tmp_harness._stop_reason = "unknown"
    tmp_harness._handle_shutdown_signal("sighup")
    # _stop_reason should be stamped to the signal name.
    assert tmp_harness._stop_reason == "sighup"
    # Summary should be written with incomplete_kill outcome.
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "incomplete_kill"
    assert "sighup" in payload["stop_reason"]


def test_handle_sigterm_stamps_sigterm_stop_reason(tmp_harness):
    import asyncio
    tmp_harness._shutdown_event = asyncio.Event()
    tmp_harness._stop_reason = "unknown"
    tmp_harness._handle_shutdown_signal("sigterm")
    assert tmp_harness._stop_reason == "sigterm"
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "incomplete_kill"


def test_handle_sigint_stamps_sigint_stop_reason(tmp_harness):
    import asyncio
    tmp_harness._shutdown_event = asyncio.Event()
    tmp_harness._stop_reason = "unknown"
    tmp_harness._handle_shutdown_signal("sigint")
    assert tmp_harness._stop_reason == "sigint"


def test_handle_shutdown_preserves_prior_informative_stop_reason(tmp_harness):
    """If something already stamped a more informative reason (e.g.
    wall_clock_cap raced ahead), the signal handler should not overwrite
    it — only replaces 'unknown' or empty strings."""
    import asyncio
    tmp_harness._shutdown_event = asyncio.Event()
    tmp_harness._stop_reason = "wall_clock_cap"
    tmp_harness._handle_shutdown_signal("sighup")
    assert tmp_harness._stop_reason == "wall_clock_cap"


# ---------------------------------------------------------------------------
# (4) register_signal_handlers installs SIGHUP when available
# ---------------------------------------------------------------------------


def test_register_signal_handlers_installs_sighup(tmp_harness):
    """Loop receives add_signal_handler(SIGHUP) when the platform has it."""
    import asyncio
    handlers_installed = []

    class _FakeLoop:
        def add_signal_handler(self, sig, cb):
            handlers_installed.append(sig)

    tmp_harness.register_signal_handlers(_FakeLoop())  # type: ignore[arg-type]
    # Ticket B: SIGHUP must be present on macOS/Linux.
    _sighup = getattr(signal, "SIGHUP", None)
    if _sighup is not None:
        assert _sighup in handlers_installed
    assert signal.SIGINT in handlers_installed
    assert signal.SIGTERM in handlers_installed


def test_register_signal_handlers_ignores_sigpipe():
    """SIGPIPE is set to SIG_IGN at process level so broken-pipe writes
    during shutdown don't crash the interpreter."""
    _sigpipe: int
    _probed = getattr(signal, "SIGPIPE", None)
    if _probed is None:
        pytest.skip("SIGPIPE not available on this platform")
    _sigpipe = int(_probed)
    # Reset to default first so we see the transition.
    _original = signal.getsignal(_sigpipe)
    try:
        signal.signal(_sigpipe, signal.SIG_DFL)
        from backend.core.ouroboros.battle_test.harness import BattleTestHarness
        h = BattleTestHarness.__new__(BattleTestHarness)  # skip __init__
        # Call through the register path — need a fake loop.

        class _FakeLoop:
            def add_signal_handler(self, sig, cb):
                pass

        h.register_signal_handlers(_FakeLoop())  # type: ignore[arg-type]
        assert signal.getsignal(_sigpipe) == signal.SIG_IGN
    finally:
        signal.signal(_sigpipe, _original)


# ---------------------------------------------------------------------------
# (5) Clean path stamps session_outcome="complete"
# ---------------------------------------------------------------------------


def test_clean_path_can_stamp_complete_outcome(tmp_harness):
    """Via direct save_summary call (simulating the _generate_report clean
    path), session_outcome="complete" lands in summary.json. This proves
    the enum has two values: complete | incomplete_kill."""
    tmp_harness._session_recorder.save_summary(
        output_dir=tmp_harness._session_dir,
        stop_reason="idle_timeout",
        duration_s=900.0,
        cost_total=0.50,
        cost_breakdown={"claude": 0.50},
        branch_stats={"commits": 2, "files_changed": 1, "insertions": 3, "deletions": 0},
        convergence_state="IMPROVING",
        convergence_slope=0.1,
        convergence_r2=0.9,
        session_outcome="complete",
        last_activity_ts=1_700_000_890.0,
    )
    payload = json.loads((tmp_harness._session_dir / "summary.json").read_text())
    assert payload["session_outcome"] == "complete"
    assert payload["last_activity_ts"] == 1_700_000_890.0
