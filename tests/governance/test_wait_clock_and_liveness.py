"""Two invariants the boot wait depends on, pinned rather than assumed.

CLOCK DISCIPLINE — and the rule is NOT "monotonic everywhere".

A Tailscale topology between an M1 and a Windows box guarantees NTP skew, so
the temptation is to ban ``time.time()`` outright. That would be wrong, and
wrong in a way that silently breaks state: ``time.monotonic()`` is only
comparable WITHIN one process run. Its epoch is arbitrary and resets when the
process does, so a value persisted to ``.jarvis/provider_liquidity.json`` and
read back by the next boot is meaningless. The honest rule has two halves:

  * an IN-PROCESS duration (how long has this wait run, how long since the
    last tick) MUST use ``time.monotonic()`` — it cannot be dragged backwards
    by an NTP correction mid-boot;
  * a PERSISTED or CROSS-PROCESS timestamp MUST use wall clock, and must be
    read through a plausibility guard, because that is the only clock two
    processes can agree on and it is the one that can be wrong.

`economic_state._plausible_recorded` is that guard, and `economic_view`
surfaces its verdict as ``stale_clock`` rather than silently trusting a row.

LIVENESS — the overrun counter must not outlive the daemon.

`+Xs over` is unbounded arithmetic on elapsed time. If the organism dies, or a
tailnet socket drops, an incrementing counter is a lie told once a second. Two
existing mechanisms already prevent it and are pinned here so a refactor
cannot quietly remove them: the wait is bounded by a deadline, and
``child_poll`` makes a spawned daemon's death observable within one probe.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from backend.core.ouroboros.cli import boot_progress as bp
from backend.core.ouroboros.cli import thin_client as tc


def _calls(fn):
    """Every ``a.b()`` attribute call inside *fn*, by dotted name."""
    src = textwrap.dedent(inspect.getsource(fn))
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            base = getattr(n.func.value, "id", "")
            out.add(f"{base}.{n.func.attr}" if base else n.func.attr)
    return out


class TestInProcessDurationsAreMonotonic:
    """An NTP correction mid-boot must not move a duration."""

    def test_the_wait_path_never_reads_the_wall_clock(self):
        src = inspect.getsource(tc)
        assert "time.time()" not in src, (
            "a wall-clock delta in the wait loop can be dragged backwards by "
            "an NTP step, which on a skewed tailnet is guaranteed, not rare"
        )
        assert "time.monotonic()" in src

    @pytest.mark.parametrize("fn", [tc.await_socket, tc._mk_tick])
    def test_the_loop_and_the_tick_both_anchor_on_monotonic(self, fn):
        called = _calls(fn)
        assert "time.monotonic" in called
        assert "time.time" not in called

    def test_boot_progress_takes_elapsed_and_reads_no_clock_itself(self):
        """One clock owner. If the module sampled its own, it could disagree
        with the deadline that governs the very loop calling it."""
        src = inspect.getsource(bp)
        assert "time.time()" not in src
        assert "time.monotonic()" not in src


class TestPersistedTimestampsUseTheOnlySharedClock:
    def test_staleness_is_measured_against_wall_clock_by_necessity(self):
        """`unverified_since` is written by one process and read by the next.

        Monotonic here would be a bug, not a hardening: its epoch resets with
        the process, so the delta would be nonsense across a restart.
        """
        from backend.core.ouroboros.governance import economic_state as es
        called = _calls(es.display_liquidity)
        assert "time.time" in called
        assert "time.monotonic" not in called

    def test_an_implausible_row_is_flagged_not_trusted(self):
        """The guard that makes wall clock safe to use across machines."""
        from backend.core.ouroboros.governance import economic_state as es
        assert es._plausible_recorded(0.0) is None            # pre-2020
        assert es._plausible_recorded(2 ** 40) is None        # far future
        assert es._plausible_recorded("not-a-number") is None
        import time as _t
        assert es._plausible_recorded(_t.time()) is not None

    def test_a_skewed_clock_is_reported_rather_than_believed(self, tmp_path,
                                                             monkeypatch):
        import json
        import time as _t
        from backend.core.ouroboros.governance import economic_state as es
        from backend.core.ouroboros.governance import provider_liquidity_ledger as pl
        p = tmp_path / "liq.json"
        p.write_text(json.dumps({"providers": {"anthropic": {
            "recorded_unix": _t.time() + 90_000.0,      # a day+ ahead
            "quota_reason": "class=economic::402",
        }}}))
        monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH", str(p))
        pl._read_cache.clear()
        assert es.economic_view("anthropic")["stale_clock"] is True, (
            "a future-dated row is a clock fault and must be surfaced, not "
            "folded silently into a duration"
        )


class TestTheCounterCannotOutliveTheDaemon:
    def test_the_wait_is_bounded_and_the_bound_is_tunable(self):
        assert tc._boot_wait_s() > 0
        assert _calls(tc.await_socket) & {"time.monotonic"}
        src = textwrap.dedent(inspect.getsource(tc.await_socket))
        assert "deadline" in src, "an unbounded wait can increment forever"

    @pytest.mark.parametrize("raw,expected", [
        ("5", 5.0), ("0.1", 5.0), ("99999", 900.0), ("junk", 120.0), ("", 120.0),
    ])
    def test_the_bound_clamps_and_survives_a_malformed_override(
            self, monkeypatch, raw, expected):
        monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", raw)
        assert tc._boot_wait_s() == expected

    def test_a_dead_spawned_daemon_stops_the_wait(self):
        """`child_poll` is what turns "wait the full deadline over a corpse"
        into "stop within one probe". Structural pin: the loop must consult
        it, and both SPAWN call sites must pass it."""
        src = textwrap.dedent(inspect.getsource(tc.await_socket))
        assert "child_poll" in src
        whole = inspect.getsource(tc)
        # Every call that hands `await_socket` a spawned process must also
        # hand it that process's poll — a spawn site without it reverts to
        # the blind full-deadline vigil.
        spawn_sites = whole.count("record_boot=True")
        assert spawn_sites >= 1
        assert whole.count("child_poll=getattr(proc") >= spawn_sites

    def test_render_is_pure_and_cannot_extend_the_wait(self):
        """The progress line must never be able to keep a dead wait alive:
        no sleeping, no probing, no I/O of its own."""
        called = _calls(bp.Progress.render)
        for forbidden in ("time.sleep", "asyncio.sleep", "socket.connect",
                          "subprocess.run"):
            assert forbidden not in called

    def test_overrun_stays_finite_at_absurd_elapsed(self):
        """Even handed a nonsense clock the arithmetic must not produce
        inf/NaN — the line is rendered, not evaluated."""
        p = bp.Progress(stages=bp.DEFAULT_STAGES, expected_s=40.0)
        for elapsed in (1e12, float("inf"), float("nan"), -1.0):
            out = p.render(elapsed)
            assert isinstance(out, str)
            assert "inf" not in out and "nan" not in out
