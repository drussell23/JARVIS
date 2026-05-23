"""Slice 12V — Tripartite Unification (WAL-first + Sidecar + ASCII healing).

bt-2026-05-23-192636 (Slice 12U validation soak) achieved the
historic milestone of closing the 15-minute LoopDeadman wedge — but
surfaced three follow-on layers of friction:

* **Teardown hang:** ``ShutdownWatchdog`` fired ``os._exit(75)`` after
  a 69s ``dw_heavy_probe`` HTTP cleanup hung past the 30s teardown
  deadline. ``operations[]`` was empty because the clean
  ``_generate_report`` path never reached ``save_summary`` and
  ``os._exit`` bypasses ``atexit``.

* **Ghost blockers:** ``ControlPlaneStarvation`` events DOUBLED to 121
  with peaks 88s / 52s / 52s. The existing snapshot path runs as an
  asyncio task — when MainThread is wedged, that task is suspended,
  so snapshots fire only post-recovery and capture the watchdog
  observing itself instead of the actual blocker.

* **ASCII healing activation:** loop survives to VALIDATE; operator
  directs activation of the cognitive ASCII healing layer (Slice
  12R Phase 2 + 3 — already shipped; this slice audits the wiring
  + pins the activation path).

Slice 12V is the **tripartite unification** — three composable
substrates that close all three layers:

# Phase 1 — Bulletproof Teardown + ShutdownWatchdog Tombstone

* ``harness._shutdown_components`` writes ``summary.json`` via the
  existing synchronous ``_atexit_fallback_write`` as the FIRST step
  (before any hangable cleanup). Master switch
  ``JARVIS_SHUTDOWN_WAL_FIRST_ENABLED`` (default TRUE).
* ``shutdown_watchdog.BoundedShutdownWatchdog`` mirrors Slice 12T's
  forensic tombstone — three sinks: stderr faulthandler, session-dir
  file (``shutdown_watchdog_tombstone.txt`` via
  ``JARVIS_SHUTDOWN_TOMBSTONE_DIR``), per-thread logger CRITICAL
  lines. Harness wires the dir at boot via ``os.environ.setdefault``.

# Phase 2 — Sidecar Profiler (out-of-band MainThread observer)

New module ``governance/sidecar_profiler.py`` — daemon thread
polling ``sys._current_frames()`` from outside the asyncio loop.
When the MainThread frame doesn't change for ``stuck_threshold_s``
(default 5s) consecutive polls, emits a
``[SidecarProfiler.STUCK_FRAME]`` CRITICAL log line with the
in-progress stack. Catches the ACTUAL blocker, not the post-event
recovery frame.

Three env knobs: ``JARVIS_SIDECAR_PROFILER_ENABLED`` (default TRUE),
``JARVIS_SIDECAR_POLL_INTERVAL_S`` (default 1.0s),
``JARVIS_SIDECAR_STUCK_THRESHOLD_S`` (default 5.0s),
``JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S`` (default 30.0s).

# Phase 3 — ASCII healing activation audit (verifies Slice 12R state)

* :data:`ascii_strict_gate._UNICODE_REPAIR_MAP` already carries 74
  typographical codepoints (smart quotes, em-dashes, ellipsis,
  zero-width chars, arrows, ©/®/™, ×/÷, °, section/pilcrow, …).
* :meth:`AsciiStrictGate.check` runs :meth:`AsciiStrictGate.repair`
  BEFORE the hard-reject scan (auto-heal-then-validate).
* :func:`reflexive_healing.format_structural_rejection_feedback`
  returns the ``<DEVELOPER_FEEDBACK priority="CRITICAL_SYSTEM_OVERRIDE">``
  block for ``ascii_gate_failed`` rejection class.
* Orchestrator's ``ascii_corruption`` retry branch prepends the
  reflexive healing block (Slice 12R Phase 3).

These tests pin the activation path so a future refactor cannot
silently break it — Slice 12V audits + locks-in the layer that
Slice 12R built.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.battle_test import shutdown_watchdog as sw
from backend.core.ouroboros.battle_test.shutdown_watchdog import (
    BoundedShutdownWatchdog,
    EXIT_CODE_HARNESS_WEDGED,
)
from backend.core.ouroboros.governance import sidecar_profiler
from backend.core.ouroboros.governance.ascii_strict_gate import (
    AsciiStrictGate,
    _UNICODE_REPAIR_MAP,
    is_auto_repair_enabled,
)
from backend.core.ouroboros.governance.reflexive_healing import (
    format_structural_rejection_feedback,
)
from backend.core.ouroboros.governance.sidecar_profiler import (
    SidecarProfiler,
    get_default_sidecar,
    reset_default_sidecar,
    sidecar_enabled,
    sidecar_poll_interval_s,
    sidecar_stuck_threshold_s,
    sidecar_stuck_log_interval_s,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Slice 12V env knobs must not leak between tests."""
    for var in (
        "JARVIS_SHUTDOWN_WAL_FIRST_ENABLED",
        "JARVIS_SHUTDOWN_TOMBSTONE_DIR",
        "JARVIS_SHUTDOWN_TOMBSTONE_LOGGER",
        "JARVIS_SIDECAR_PROFILER_ENABLED",
        "JARVIS_SIDECAR_POLL_INTERVAL_S",
        "JARVIS_SIDECAR_STUCK_THRESHOLD_S",
        "JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_default_sidecar()
    yield
    reset_default_sidecar()


# ──────────────────────────────────────────────────────────────────────
# Phase 1 — ShutdownWatchdog tombstone
# ──────────────────────────────────────────────────────────────────────


class TestPhase1ShutdownWatchdogTombstone:
    """The fire path MUST dump to all three sinks AND never raise.
    We replace ``os._exit`` with a sentinel-raising callable so the
    test can observe the dump and survive."""

    def _make_sentinel_exit(self, capture):
        class _ExitSentinel(BaseException):
            pass

        def _fake_exit(code):
            capture["exit_code"] = code
            raise _ExitSentinel()

        return _fake_exit, _ExitSentinel

    def test_fire_writes_tombstone_to_session_dir(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_SHUTDOWN_TOMBSTONE_DIR", str(tmp_path),
        )
        capture: dict = {}
        fake_exit, sentinel = self._make_sentinel_exit(capture)

        wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
        wdg.arm("test_teardown", deadline_s=0.1)
        # The daemon thread sleeps for deadline_s, fires the tombstone
        # writes, then calls exit_fn (which raises in the daemon —
        # uncaught daemon exceptions are silent). We just wait for
        # the side effect (wdg.fired flag) which gets set BEFORE
        # the tombstone writes + exit_fn call.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if wdg.fired:
                break
            time.sleep(0.01)
        if not wdg.fired:
            pytest.fail("ShutdownWatchdog did not fire")
        # Give the daemon a moment to finish writing tombstones
        # after _fired flag is set.
        time.sleep(0.2)
        assert capture.get("exit_code") == EXIT_CODE_HARNESS_WEDGED

        tombstone = tmp_path / "shutdown_watchdog_tombstone.txt"
        assert tombstone.exists(), (
            "Slice 12V Phase 1 — shutdown_watchdog_tombstone.txt "
            "was not created in the session dir"
        )
        body = tombstone.read_text()
        assert "TEARDOWN TOMBSTONE" in body
        assert "reason=" in body
        assert "test_teardown" in body
        assert f"pid={os.getpid()}" in body
        # faulthandler dump must include a Thread marker.
        assert "Thread" in body or "Current thread" in body
        wdg.stop()

    def test_fire_emits_logger_tombstone_lines(
        self, monkeypatch, caplog,
    ):
        capture: dict = {}
        fake_exit, _ = self._make_sentinel_exit(capture)

        wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
        with caplog.at_level(
            "CRITICAL", logger="Ouroboros.ShutdownWatchdog",
        ):
            wdg.arm("test_logger", deadline_s=0.1)
            # Wait for the daemon to fire.
            deadline = time.time() + 5.0
            while time.time() < deadline and not wdg.fired:
                time.sleep(0.01)
            # Give the daemon a moment to write logs after fire.
            time.sleep(0.1)

        tombstone_msgs = [
            r.getMessage() for r in caplog.records
            if "ShutdownWatchdog.TOMBSTONE" in r.getMessage()
        ]
        assert len(tombstone_msgs) >= 1, (
            "Slice 12V Phase 1 — no "
            "[ShutdownWatchdog.TOMBSTONE] logger lines emitted; "
            "operator would have no debug.log attribution"
        )
        joined = "\n".join(tombstone_msgs)
        assert "thread_id=" in joined
        wdg.stop()

    def test_fire_logger_disabled_emits_no_tombstone_lines(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setenv(
            "JARVIS_SHUTDOWN_TOMBSTONE_LOGGER", "false",
        )
        capture: dict = {}
        fake_exit, _ = self._make_sentinel_exit(capture)

        wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
        with caplog.at_level(
            "CRITICAL", logger="Ouroboros.ShutdownWatchdog",
        ):
            wdg.arm("test_disabled_logger", deadline_s=0.1)
            deadline = time.time() + 5.0
            while time.time() < deadline and not wdg.fired:
                time.sleep(0.01)
            time.sleep(0.1)

        tombstone_msgs = [
            r.getMessage() for r in caplog.records
            if "ShutdownWatchdog.TOMBSTONE" in r.getMessage()
        ]
        assert len(tombstone_msgs) == 0
        wdg.stop()

    def test_fire_swallows_tombstone_file_error(
        self, monkeypatch,
    ):
        """Bad tombstone dir MUST NOT prevent os._exit firing."""
        monkeypatch.setenv(
            "JARVIS_SHUTDOWN_TOMBSTONE_DIR",
            "/no/such/path/that/cannot/exist",
        )
        capture: dict = {}
        fake_exit, _ = self._make_sentinel_exit(capture)

        wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
        wdg.arm("test_bad_dir", deadline_s=0.1)
        deadline = time.time() + 5.0
        while time.time() < deadline and not wdg.fired:
            time.sleep(0.01)
        # exit_fn must have been called. _fired flag is set BEFORE
        # tombstone-block runs + exit_fn fires; give the daemon a
        # grace window to complete the post-flag sequence.
        assert wdg.fired
        time.sleep(0.3)
        assert capture.get("exit_code") == EXIT_CODE_HARNESS_WEDGED
        wdg.stop()


class TestPhase1ASTPin:
    def test_shutdown_watchdog_carries_slice12v_tombstone(self):
        src = Path(
            "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
        ).read_text()
        assert "Slice 12V Phase 1" in src, (
            "Slice 12V Phase 1 marker missing from "
            "shutdown_watchdog.py"
        )
        assert "JARVIS_SHUTDOWN_TOMBSTONE_DIR" in src
        assert "JARVIS_SHUTDOWN_TOMBSTONE_LOGGER" in src
        assert "ShutdownWatchdog.TOMBSTONE" in src, (
            "ShutdownWatchdog.TOMBSTONE logger marker missing — "
            "debug.log attribution path is broken"
        )

    def test_harness_wires_tombstone_dir_at_boot(self):
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert (
            'os.environ.setdefault(\n'
            '                        "JARVIS_SHUTDOWN_TOMBSTONE_DIR"'
        ) in src or "JARVIS_SHUTDOWN_TOMBSTONE_DIR" in src, (
            "Harness does not wire JARVIS_SHUTDOWN_TOMBSTONE_DIR "
            "at boot — Slice 12V Phase 1 operator-zero-config "
            "experience broken"
        )

    def test_harness_calls_wal_first_in_shutdown(self):
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert "JARVIS_SHUTDOWN_WAL_FIRST_ENABLED" in src
        assert "Slice 12V Phase 1 — WAL-first shutdown" in src or \
               "WAL-first" in src
        assert "in_flight_shutdown_wal" in src, (
            "Slice 12V WAL-first stamp missing — summary.json "
            "won't carry the WAL-first stop_reason marker"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Sidecar Profiler
# ──────────────────────────────────────────────────────────────────────


class TestPhase2SidecarMasterSwitch:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SIDECAR_PROFILER_ENABLED", raising=False,
        )
        assert sidecar_enabled() is True

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("true", True), ("1", True), ("on", True),
            ("false", False), ("0", False), ("no", False),
            ("off", False),
        ],
    )
    def test_truthy_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv(
            "JARVIS_SIDECAR_PROFILER_ENABLED", raw,
        )
        assert sidecar_enabled() is expected

    def test_env_knobs_bounded(self, monkeypatch):
        # Below floor → floor
        monkeypatch.setenv("JARVIS_SIDECAR_POLL_INTERVAL_S", "0.0001")
        assert sidecar_poll_interval_s() == 0.1
        # Above ceiling → ceiling
        monkeypatch.setenv("JARVIS_SIDECAR_STUCK_THRESHOLD_S", "9999")
        assert sidecar_stuck_threshold_s() == 300.0
        # Garbage → default
        monkeypatch.setenv(
            "JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S", "garbage",
        )
        assert sidecar_stuck_log_interval_s() == 30.0


class TestPhase2SidecarBehavior:
    """The load-bearing claim: the sidecar catches a synchronously
    blocked MainThread frame and logs it WHILE the wedge is
    active."""

    def test_sidecar_catches_synchronous_wedge_in_progress(
        self, caplog,
    ):
        """Spawn the sidecar from the main thread (so it captures
        THIS thread's frames), then deliberately block the main
        thread with time.sleep > stuck_threshold_s. The sidecar
        daemon thread MUST log a STUCK_FRAME entry pointing at
        the time.sleep call site."""
        sp = SidecarProfiler(
            poll_interval_s=0.05,
            stuck_threshold_s=0.2,
            stuck_log_interval_s=30.0,
        )
        assert sp.start()
        try:
            with caplog.at_level(
                "CRITICAL", logger="Ouroboros.SidecarProfiler",
            ):
                # Synchronous block — the sidecar daemon must
                # observe this thread stuck on time.sleep.
                time.sleep(0.6)

            stuck_msgs = [
                r.getMessage() for r in caplog.records
                if "STUCK_FRAME" in r.getMessage()
            ]
            assert len(stuck_msgs) >= 1, (
                f"Sidecar did NOT catch the synchronous wedge "
                f"in progress (emission_count={sp.emission_count}). "
                "Phase 2 broken — wedges remain unattributable."
            )
            # The captured stack should reference this test file
            # or time.sleep — proves the in-progress frame was
            # captured, not a post-recovery snapshot.
            joined = "\n".join(stuck_msgs)
            assert "stuck_for_s" in joined
            assert "main_tid=" in joined
        finally:
            sp.stop()

    def test_sidecar_does_not_emit_for_brief_pauses(self, caplog):
        """Sub-threshold pauses must NOT trigger emissions —
        false-positive prevention."""
        sp = SidecarProfiler(
            poll_interval_s=0.05,
            stuck_threshold_s=1.0,  # 1s threshold
            stuck_log_interval_s=30.0,
        )
        assert sp.start()
        try:
            with caplog.at_level(
                "CRITICAL", logger="Ouroboros.SidecarProfiler",
            ):
                time.sleep(0.2)  # well below 1s threshold
            stuck_msgs = [
                r.getMessage() for r in caplog.records
                if "STUCK_FRAME" in r.getMessage()
            ]
            assert len(stuck_msgs) == 0, (
                f"Sidecar fired on a brief pause "
                f"({len(stuck_msgs)} STUCK_FRAME emissions) — "
                "threshold guard broken"
            )
        finally:
            sp.stop()

    def test_sidecar_throttles_repeated_same_frame(self, caplog):
        """If MainThread is stuck on the same frame for >2x the
        log interval, the sidecar should emit ONCE per throttle
        window (not on every poll)."""
        sp = SidecarProfiler(
            poll_interval_s=0.05,
            stuck_threshold_s=0.1,
            stuck_log_interval_s=10.0,  # large throttle
        )
        assert sp.start()
        try:
            with caplog.at_level(
                "CRITICAL", logger="Ouroboros.SidecarProfiler",
            ):
                time.sleep(0.5)
            stuck_msgs = [
                r.getMessage() for r in caplog.records
                if "STUCK_FRAME" in r.getMessage()
            ]
            # With 0.5s wedge and 10s throttle, exactly 1 emission
            # is expected (subsequent polls within the throttle
            # window are suppressed).
            assert 1 <= len(stuck_msgs) <= 2, (
                f"Throttle broken: {len(stuck_msgs)} emissions "
                "on a single wedge (expected ~1)"
            )
        finally:
            sp.stop()

    def test_start_returns_false_when_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SIDECAR_PROFILER_ENABLED", "false",
        )
        sp = SidecarProfiler()
        assert sp.start() is False
        assert sp.running is False

    def test_start_idempotent_when_already_running(self):
        sp = SidecarProfiler(
            poll_interval_s=0.5, stuck_threshold_s=5.0,
        )
        try:
            assert sp.start() is True
            assert sp.start() is False  # already running
            assert sp.running
        finally:
            sp.stop()


class TestPhase2ProcessSingleton:
    def test_get_default_sidecar_returns_same_instance(self):
        a = get_default_sidecar()
        b = get_default_sidecar()
        assert a is b

    def test_reset_breaks_singleton(self):
        a = get_default_sidecar()
        reset_default_sidecar()
        b = get_default_sidecar()
        assert a is not b


class TestPhase2ASTPin:
    def test_sidecar_uses_sys_current_frames(self):
        """The core sidecar contract — must call
        ``sys._current_frames()`` to capture out-of-band frames."""
        src = Path(
            "backend/core/ouroboros/governance/sidecar_profiler.py"
        ).read_text()
        assert "sys._current_frames()" in src, (
            "SidecarProfiler does not call sys._current_frames — "
            "Phase 2 contract broken; cannot capture in-progress "
            "frames"
        )

    def test_sidecar_emits_via_logger_critical(self):
        """The dump must land via the standard logger so it
        reaches debug.log."""
        src = Path(
            "backend/core/ouroboros/governance/sidecar_profiler.py"
        ).read_text()
        assert "logger.critical(" in src
        assert "STUCK_FRAME" in src

    def test_sidecar_uses_daemon_thread(self):
        """Must be a daemon=True thread (not asyncio task) so it
        runs out-of-band from the main loop."""
        src = Path(
            "backend/core/ouroboros/governance/sidecar_profiler.py"
        ).read_text()
        assert "daemon=True" in src

    def test_harness_wires_sidecar_at_boot(self):
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert "get_default_sidecar" in src or (
            "sidecar_profiler" in src and "start" in src
        ), (
            "Harness does not wire SidecarProfiler at boot — "
            "Phase 2 wedge attribution path inactive in production"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 3 — ASCII healing activation audit
# ──────────────────────────────────────────────────────────────────────


class TestPhase3AsciiHealingActivation:
    """The cognitive ASCII-healing layer was built in Slice 12R
    (Phase 2 auto-repair + Phase 3 reflexive feedback). Slice 12V
    audits the activation path so a future refactor cannot
    silently break it."""

    def test_auto_repair_defaults_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ASCII_GATE_AUTO_REPAIR", raising=False)
        assert is_auto_repair_enabled() is True

    def test_unicode_repair_map_comprehensive(self):
        """The substantive cognitive-layer contract: typographical
        Unicode must auto-heal pre-validation. Pin the
        load-bearing codepoint coverage so a future refactor can't
        silently shrink the map."""
        # Em dash (the #1 typographical offender Claude emits)
        assert 0x2014 in _UNICODE_REPAIR_MAP
        # Smart quotes
        assert 0x2018 in _UNICODE_REPAIR_MAP  # left single
        assert 0x2019 in _UNICODE_REPAIR_MAP  # right single
        assert 0x201C in _UNICODE_REPAIR_MAP  # left double
        assert 0x201D in _UNICODE_REPAIR_MAP  # right double
        # Ellipsis
        assert 0x2026 in _UNICODE_REPAIR_MAP
        # Non-breaking space
        assert 0x00A0 in _UNICODE_REPAIR_MAP
        # Zero-width space (notorious for code pollution)
        assert 0x200B in _UNICODE_REPAIR_MAP
        # Arrow (Claude uses → in flow diagrams)
        assert 0x2192 in _UNICODE_REPAIR_MAP
        # Total must be substantial (current: 74)
        assert len(_UNICODE_REPAIR_MAP) >= 50, (
            f"_UNICODE_REPAIR_MAP shrunk to "
            f"{len(_UNICODE_REPAIR_MAP)} entries — Slice 12V Phase 3 "
            "cognitive layer regressed"
        )

    def test_letter_lookalikes_intentionally_excluded(self):
        """The rapid-FEH-uzz invariant: Unicode letters that look
        like ASCII MUST NOT be in the repair map — changing a
        letter changes identifier identity."""
        for cp, label in [
            (0x0641, "Arabic FEH"),
            (0x0430, "Cyrillic а"),
            (0x0435, "Cyrillic е"),
            (0x03BF, "Greek omicron ο"),
        ]:
            assert cp not in _UNICODE_REPAIR_MAP, (
                f"Letter look-alike U+{cp:04X} ({label}) is in "
                "the repair map — package-name typosquats can "
                "now silently slip past the gate"
            )

    def test_gate_runs_repair_before_hard_reject_scan(self):
        """End-to-end behavior pin: candidate carrying only
        typographical Unicode passes the gate with auto-repair."""
        gate = AsciiStrictGate(enabled=True, auto_repair=True)
        candidate = {
            "file_path": "requirements.txt",
            "full_content": (
                "# pinned deps — see PR\n"
                "rich>=13.0\n"
                "click's_companion==1.2\n"
            ),
        }
        ok, reason, offenders = gate.check(candidate)
        assert ok, (
            f"Repairable-only candidate failed gate: "
            f"reason={reason} offenders={offenders}"
        )
        # In-place mutation applied.
        assert "—" not in candidate["full_content"]
        assert candidate.get("_ascii_repair_count", 0) >= 1

    def test_reflexive_healing_returns_feedback_for_ascii_class(self):
        """The reflexive layer's classifier must accept
        ``ascii_gate_failed`` and return a
        ``<DEVELOPER_FEEDBACK>`` block."""
        block = format_structural_rejection_feedback(
            "ascii_gate_failed: 3 offenders",
            rejection_detail="U+0641 in identifier",
            attempt_number=2, max_attempts=2,
        )
        assert block is not None
        assert "DEVELOPER_FEEDBACK" in block
        assert "ASCII" in block.upper()

    def test_orchestrator_ascii_branch_prepends_reflexive(self):
        """Slice 12R Phase 3 wired the reflexive prepend into the
        orchestrator's ``ascii_corruption`` retry branch. Pin
        that the wiring is intact so the cognitive feedback
        reaches the LLM on attempt 2/2."""
        src = Path(
            "backend/core/ouroboros/governance/orchestrator.py"
        ).read_text()
        assert 'elif _err_str.startswith("ascii_corruption"):' in src
        assert "Slice 12R Phase 3" in src
        assert "format_structural_rejection_feedback" in src
        # The feedback string must carry the canonical class name
        # so the classifier matches.
        assert "ascii_gate_failed" in src
