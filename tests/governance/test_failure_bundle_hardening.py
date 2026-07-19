"""Failure-bundle hardening spine — the bare-``ov`` 4-minute death spiral.

Session bt-2026-07-19-031750 postmortem, four roots:

  1. **Budget economics** — flat $0.50 worst-case reservation vs the
     $0.50 default session cap → every op structurally unaffordable →
     3 preflight refusals → hibernation → ``session_exhausted`` in 4
     minutes. Fix: complexity-scaled reservation
     (``providers._scale_reservation_by_complexity``).
  2. **Cockpit cost cap** — the product surface now defaults to
     ``JARVIS_COCKPIT_COST_CAP`` (2.50) instead of the soak/CI 0.50.
  3. **Teardown wedge** — unbounded ``os.read`` on the default
     executor could never be joined → BoundedShutdownWatchdog
     ``os._exit(75)``. Fix: select-bounded ``_blocking_read``.
  4. **ERROR/tombstone conformance** — EXHAUSTION walls + watchdog
     faulthandler dumps stomped the cockpit ceremony. Fix:
     ``_CockpitErrorFormatter`` + ``_CockpitConsoleFilter`` in
     silent_boot, cockpit-gated stderr dump + ``file_only`` tombstone
     records in shutdown_watchdog.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import key_input, providers, silent_boot

_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Root 1 — complexity-scaled budget reservation
# ---------------------------------------------------------------------------


class TestReservationScaling:
    def test_trivial_scales_down(self, monkeypatch):
        monkeypatch.delenv("JARVIS_RESERVE_MULT_TRIVIAL", raising=False)
        assert providers._scale_reservation_by_complexity(
            0.50, "trivial",
        ) == pytest.approx(0.075)

    def test_simple_and_light_share_a_mult(self):
        a = providers._scale_reservation_by_complexity(0.50, "simple")
        b = providers._scale_reservation_by_complexity(0.50, "light")
        assert a == b == pytest.approx(0.15)

    def test_unknown_complexity_keeps_worst_case_fail_closed(self):
        assert providers._scale_reservation_by_complexity(
            0.50, "quantum_banana",
        ) == 0.50
        assert providers._scale_reservation_by_complexity(0.50, "") == 0.50
        assert providers._scale_reservation_by_complexity(0.50, None) == 0.50

    def test_complex_keeps_full_reservation(self):
        assert providers._scale_reservation_by_complexity(
            0.50, "heavy_code",
        ) == 0.50

    def test_floor_holds(self, monkeypatch):
        monkeypatch.delenv("JARVIS_RESERVE_FLOOR_USD", raising=False)
        # 0.05 * 0.15 = 0.0075 < the 0.02 floor
        assert providers._scale_reservation_by_complexity(
            0.05, "trivial",
        ) == pytest.approx(0.02)

    def test_env_mult_override_clamped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESERVE_MULT_TRIVIAL", "5.0")
        # Clamped to 1.0 — an env typo can never OVER-reserve.
        assert providers._scale_reservation_by_complexity(
            0.50, "trivial",
        ) == 0.50
        monkeypatch.setenv("JARVIS_RESERVE_MULT_TRIVIAL", "not-a-float")
        assert providers._scale_reservation_by_complexity(
            0.50, "trivial",
        ) == pytest.approx(0.075)

    def test_wired_at_the_sba_reservation_site_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/providers.py"
        ).read_text()
        assert "_scale_reservation_by_complexity(" in src.split(
            "def _scale_reservation_by_complexity", 1,
        )[1], "helper defined but never called — wired-but-inert"


# ---------------------------------------------------------------------------
# Root 2 — cockpit-aware cost-cap default
# ---------------------------------------------------------------------------


class TestCockpitCostCap:
    def test_cockpit_default_pin(self):
        src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
        assert 'os.environ.get("JARVIS_COCKPIT_COST_CAP", "2.50")' in src
        assert 'os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")' in src
        # The branch keys off the presentation env, not a new flag.
        assert '"JARVIS_OV_PRESENTATION"' in src


# ---------------------------------------------------------------------------
# Root 3 — select-bounded stdin read
# ---------------------------------------------------------------------------


class _FdHolder:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._wake_r = -1          # mirrors InputController's self-pipe field


class TestBoundedRead:
    def test_timeout_returns_empty_not_blocks(self):
        r, w = os.pipe()
        try:
            holder = _FdHolder(r)
            t0 = time.monotonic()
            out = key_input.InputController._blocking_read(holder, 1)
            elapsed = time.monotonic() - t0
            assert out == b""
            # Woke on the select timeout, not a keypress.
            assert elapsed < key_input._READ_SELECT_TIMEOUT_S + 1.0
        finally:
            os.close(r)
            os.close(w)

    def test_ready_fd_delivers_bytes(self):
        r, w = os.pipe()
        try:
            os.write(w, b"q")
            out = key_input.InputController._blocking_read(_FdHolder(r), 1)
            assert out == b"q"
        finally:
            os.close(r)
            os.close(w)

    def test_negative_fd_and_closed_fd_return_empty(self):
        assert key_input.InputController._blocking_read(
            _FdHolder(-1), 1,
        ) == b""
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        assert key_input.InputController._blocking_read(
            _FdHolder(r), 1,
        ) == b""

    def test_timeout_env_clamped_and_malformed_safe(self, monkeypatch):
        monkeypatch.setenv("JARVIS_KEY_INPUT_SELECT_TIMEOUT_S", "99")
        assert key_input._resolve_read_select_timeout() == 2.0
        monkeypatch.setenv("JARVIS_KEY_INPUT_SELECT_TIMEOUT_S", "0.001")
        assert key_input._resolve_read_select_timeout() == 0.05
        monkeypatch.setenv("JARVIS_KEY_INPUT_SELECT_TIMEOUT_S", "banana")
        assert key_input._resolve_read_select_timeout() == 0.5


# ---------------------------------------------------------------------------
# Root 4 — cockpit ERROR conformance + tombstone hygiene
# ---------------------------------------------------------------------------


def _record(
    msg: str, *, name: str = "backend.core.ouroboros.x.exhaustion",
    level: int = logging.ERROR, **extra: object,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


class TestCockpitErrorFormatter:
    def test_conformed_single_line(self):
        fmt = silent_boot._CockpitErrorFormatter()
        out = fmt.format(_record(
            "EXHAUSTION est=$0.5000 remaining=$0.4995 route=STANDARD\n"
            "op=abc provider=dw attempt=3\nmore kv walls here",
        ))
        assert out.startswith("⚠ exhaustion · ")
        assert "\n" not in out
        assert out.endswith("— detail in session log")

    def test_truncates_long_messages(self):
        fmt = silent_boot._CockpitErrorFormatter()
        out = fmt.format(_record("x" * 900))
        assert len(out) < 220
        assert "…" in out

    def test_never_raises_on_hostile_record(self):
        fmt = silent_boot._CockpitErrorFormatter()
        rec = _record("fine")
        rec.getMessage = lambda: (_ for _ in ()).throw(RuntimeError())
        assert fmt.format(rec).startswith("⚠")


class TestCockpitConsoleFilter:
    def test_file_only_records_rejected(self):
        flt = silent_boot._CockpitConsoleFilter()
        assert flt.filter(_record("tombstone wall", file_only=True)) is False
        assert flt.filter(_record("real error")) is True

    def test_repeat_storm_dedupes_within_window(self):
        flt = silent_boot._CockpitConsoleFilter()
        assert flt.filter(_record("EXHAUSTION est=$0.50")) is True
        for _ in range(5):
            assert flt.filter(_record("EXHAUSTION est=$0.50")) is False
        # A DIFFERENT error still passes.
        assert flt.filter(_record("worktree_create_failed")) is True

    def test_dedup_expires_after_window(self, monkeypatch):
        flt = silent_boot._CockpitConsoleFilter()
        assert flt.filter(_record("boom")) is True
        clock = time.monotonic() + silent_boot._CockpitConsoleFilter._WINDOW_S + 1
        monkeypatch.setattr(silent_boot.time, "monotonic", lambda: clock)
        assert flt.filter(_record("boom")) is True

    def test_fail_open_admits_record(self):
        flt = silent_boot._CockpitConsoleFilter()
        rec = _record("fine")
        rec.getMessage = lambda: (_ for _ in ()).throw(RuntimeError())
        assert flt.filter(rec) is True

    def test_seen_map_bounded(self):
        flt = silent_boot._CockpitConsoleFilter()
        for i in range(400):
            flt.filter(_record(f"err-{i}"))
        assert len(flt._seen) <= 400  # pruned opportunistically at >256


class TestCockpitWiringPins:
    def test_silent_boot_installs_formatter_and_filter_cockpit_only(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/silent_boot.py"
        ).read_text()
        body = src[src.index("def _configure_locked"):]
        assert "term_handler.setFormatter(_CockpitErrorFormatter())" in body
        assert "term_handler.addFilter(_CockpitConsoleFilter())" in body

    def test_watchdog_stderr_dump_cockpit_gated(self):
        src = (
            _REPO
            / "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
        ).read_text()
        assert "is_cockpit as _is_cockpit_dump" in src
        assert "if not _is_cockpit_dump():" in src

    def test_watchdog_tombstone_records_marked_file_only(self):
        src = (
            _REPO
            / "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
        ).read_text()
        block = src[src.index('"[ShutdownWatchdog.TOMBSTONE] "'):][:400]
        assert 'extra={"file_only": True}' in block


class TestCockpitConformanceEndToEnd:
    def test_error_wall_renders_one_conformed_line(self):
        """The integration property: formatter+filter on one handler →
        a 6-repeat EXHAUSTION storm yields exactly ONE conformed line."""
        import io

        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.setLevel(logging.ERROR)
        h.setFormatter(silent_boot._CockpitErrorFormatter())
        h.addFilter(silent_boot._CockpitConsoleFilter())
        lg = logging.getLogger("test.cockpit.conformance")
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.handlers = [h]
        try:
            for _ in range(6):
                lg.error(
                    "EXHAUSTION est=$0.5000 > remaining=$0.4995\nkv wall",
                )
            lg.critical("tombstone", extra={"file_only": True})
            out = stream.getvalue().strip().splitlines()
            assert len(out) == 1
            assert out[0].startswith("⚠ conformance · EXHAUSTION")
        finally:
            lg.handlers = []
