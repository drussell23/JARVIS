"""An external binary may cost us time. It may never cost us a thread.

Offloading `osascript` and `screencapture` off the event loop fixed the
stalls and opened a worse failure mode: the blocking call now happens on a
POOL worker, so the loop keeps breathing while a wedged binary holds that
worker forever. A stall is visible; a leaked worker is not.

The load-bearing case is `test_a_surviving_grandchild_cannot_capture_the_worker`:
`subprocess.run(timeout=...)` kills the direct child and then reaps it — but
`_get_cursor_position` runs an `osascript` whose script spawns a python
interpreter, and that grandchild holds the inherited pipe open, so the
post-kill wait blocks on a pipe that never closes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from backend.core import bounded_subprocess as bs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bs.reset_for_tests()
    monkeypatch.delenv("JARVIS_SUBPROC_BREAKER_TRIPS", raising=False)
    monkeypatch.delenv("JARVIS_SUBPROC_BREAKER_COOLDOWN_S", raising=False)
    yield
    bs.reset_for_tests()


class TestItReturnsRealResults:
    def test_a_normal_command_comes_back(self):
        r = bs.run_bounded([sys.executable, "-c", "print('hi')"],
                           timeout=10, text=True)
        assert r is not None and r.returncode == 0
        assert "hi" in r.stdout

    def test_a_nonzero_exit_is_a_result_not_a_failure(self):
        r = bs.run_bounded([sys.executable, "-c", "raise SystemExit(3)"],
                           timeout=10)
        assert r is not None and r.returncode == 3

    def test_a_missing_binary_is_None_and_does_NOT_trip_the_breaker(self):
        """A binary that is not installed answers deterministically. Counting
        it as a hang would make an uninstalled tool look like a wedged one."""
        for _ in range(5):
            assert bs.run_bounded(["definitely-not-a-real-binary-xyz"],
                                  timeout=2) is None
        assert bs.breaker_state()["open"] == []


class TestATimeoutNeverHoldsTheWorker:
    def test_a_hanging_command_returns_within_its_budget(self):
        t0 = time.monotonic()
        assert bs.run_bounded([sys.executable, "-c",
                               "import time; time.sleep(30)"],
                              timeout=0.5) is None
        elapsed = time.monotonic() - t0
        assert elapsed < 5, f"took {elapsed:.1f}s — the worker was held"

    def test_a_surviving_grandchild_cannot_capture_the_worker(self):
        """THE case `subprocess.run(timeout=...)` does not cover.

        The child spawns a grandchild that outlives it and inherits the
        pipe. Killing only the child leaves the post-kill reap blocking on a
        pipe nothing will close. Signalling the process GROUP is what makes
        this return.
        """
        script = (
            "import subprocess, sys, time; "
            # grandchild holds the inherited stdout pipe open, then sleeps
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "time.sleep(30)"
        )
        t0 = time.monotonic()
        assert bs.run_bounded([sys.executable, "-c", script],
                              timeout=0.5) is None
        elapsed = time.monotonic() - t0
        assert elapsed < 8, (
            f"took {elapsed:.1f}s — a grandchild held the worker hostage")

    def test_the_process_group_is_actually_dead_afterwards(self):
        """Not merely 'we stopped waiting' — the tree is reaped, or the next
        call inherits a machine full of zombies."""
        marker = "bounded-subprocess-reap-probe"
        script = f"import time; time.sleep(30)  # {marker}"
        assert bs.run_bounded([sys.executable, "-c", script],
                              timeout=0.5) is None
        time.sleep(0.4)
        out = subprocess.run(["ps", "-eo", "command"], capture_output=True,
                             text=True, timeout=10).stdout
        assert marker not in out, "the child survived the reap"


class TestTheCircuitBreaker:
    def test_it_opens_after_repeated_timeouts_and_then_refuses_instantly(
            self, monkeypatch):
        """A binary wedged on a revoked TCC permission does not heal because
        we retried. Refusing costs no worker at all."""
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "2")
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        for _ in range(2):
            assert bs.run_bounded(cmd, timeout=0.3) is None
        assert bs.breaker_open(cmd) is True

        t0 = time.monotonic()
        assert bs.run_bounded(cmd, timeout=5) is None
        assert time.monotonic() - t0 < 0.2, "a refusal must not spawn anything"

    def test_a_success_closes_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "2")
        hang = [sys.executable, "-c", "import time; time.sleep(30)"]
        bs.run_bounded(hang, timeout=0.3)
        assert bs.breaker_state()["consecutive_failures"]
        bs.run_bounded([sys.executable, "-c", "print(1)"], timeout=10)
        assert not bs.breaker_state()["consecutive_failures"]

    def test_the_cooldown_lets_exactly_one_probe_through(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "1")
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_COOLDOWN_S", "1")
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        bs.run_bounded(cmd, timeout=0.3)
        assert bs.breaker_open(cmd) is True
        time.sleep(1.1)
        assert bs.breaker_open(cmd) is False, "cooldown must allow a retry"

    def test_the_key_is_the_command_not_the_arguments(self):
        """`screencapture` hanging on a consent prompt hangs for EVERY temp
        path. Keying on the full argv would open a fresh breaker per call and
        never trip."""
        a = ["screencapture", "-x", "-t", "png", "/tmp/one.png"]
        b = ["screencapture", "-x", "-t", "png", "/tmp/two.png"]
        assert bs._key(a) == bs._key(b) == "screencapture"
        # And NOT a guessed sub-verb: a flag and a sub-verb are not
        # distinguishable by shape, so the key is the binary alone.
        assert bs._key(["yabai", "-m", "query"]) == "yabai"
        assert bs._key(["/usr/sbin/screencapture", "-x"]) == "screencapture"

    def test_state_is_a_bounded_projection(self):
        st = bs.breaker_state()
        assert st["schema_version"].startswith("bounded_subprocess")
        assert set(st) >= {"consecutive_failures", "open", "trips_to_open"}


class TestItNeverRaises:
    @pytest.mark.parametrize("cmd", [[], ["/"], ["", ""]])
    def test_hostile_commands_return_None(self, cmd):
        assert bs.run_bounded(cmd, timeout=1) is None

    def test_breaker_helpers_never_raise(self):
        assert bs.breaker_open([]) in (True, False)
        assert isinstance(bs.breaker_state(), dict)


class TestTheSharedVoiceCatalog:
    """Two subsystems were each running `say -v ?` — 15s and 5s timeouts —
    from their constructors, for the same immutable list. Both were caught on
    the wedged main loop; after the screen-context fixes they were the only
    two frames left."""

    @pytest.fixture(autouse=True)
    def _fresh(self):
        from backend.core import system_voices as sv
        sv.reset_for_tests()
        yield
        sv.reset_for_tests()

    def test_the_query_runs_once_and_is_shared(self, monkeypatch):
        from backend.core import system_voices as sv
        calls = {"n": 0}

        def _fake(cmd, **kw):
            calls["n"] += 1
            return subprocess.CompletedProcess(cmd, 0, "Alex  en_US\n", "")

        monkeypatch.setattr(bs, "run_bounded", _fake)
        assert "Alex" in sv.voice_catalog_raw()
        assert "Alex" in sv.voice_catalog_raw()
        assert calls["n"] == 1, "the second caller must hit the cache"

    def test_an_unanswered_query_is_None_not_empty(self, monkeypatch):
        """None = 'could not ask'. '' would claim the machine has no voices,
        which sends a caller to a wrong default instead of its own."""
        from backend.core import system_voices as sv
        monkeypatch.setattr(bs, "run_bounded", lambda *a, **k: None)
        assert sv.voice_catalog_raw() is None

    def test_a_failure_is_cached_too(self, monkeypatch):
        """A machine without `say` will not grow one. Retrying per
        constructor is how a missing binary becomes a recurring stall."""
        from backend.core import system_voices as sv
        calls = {"n": 0}

        def _absent(*a, **k):
            calls["n"] += 1
            return None

        monkeypatch.setattr(bs, "run_bounded", _absent)
        sv.voice_catalog_raw()
        sv.voice_catalog_raw()
        assert calls["n"] == 1

    def test_force_reasks(self, monkeypatch):
        from backend.core import system_voices as sv
        calls = {"n": 0}

        def _fake(cmd, **kw):
            calls["n"] += 1
            return subprocess.CompletedProcess(cmd, 0, "Alex\n", "")

        monkeypatch.setattr(bs, "run_bounded", _fake)
        sv.voice_catalog_raw()
        sv.voice_catalog_raw(force=True)
        assert calls["n"] == 2

    def test_the_timeout_is_seconds_not_a_fifteen_second_budget(self):
        """`say -v ?` reads a local catalogue in milliseconds. A 15s budget
        does not make a slow machine succeed; it makes a wedged one
        expensive."""
        from backend.core import system_voices as sv
        assert sv._timeout_s() <= 5.0


class TestTheTripIsVisibleAcrossProcesses:
    """A breaker that exists only in the daemon's memory is invisible to
    `trinity status`, which runs in its own process — a revoked permission
    would present as "everything fine, the screenshots merely stopped"."""

    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_LEDGER",
                           str(tmp_path / "breakers.json"))
        bs.reset_for_tests()
        yield
        bs.reset_for_tests()

    def test_a_trip_is_written_where_another_process_can_read_it(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "1")
        bs.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                       timeout=0.3)
        assert bs.open_breakers_on_disk(), "the trip never reached disk"

    def test_the_full_lifecycle_trip_refuse_cooldown_recover_clear(
            self, monkeypatch, tmp_path):
        """Every step of the operator-visible cycle, in order."""
        import shutil
        fake = tmp_path / "screencapture"
        shutil.copy(sys.executable, fake)
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "1")
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_COOLDOWN_S", "1")
        hang = [str(fake), "-c", "import time; time.sleep(30)"]
        ok = [str(fake), "-c", "print(1)"]

        assert bs.run_bounded(hang, timeout=0.3) is None          # trip
        assert bs.open_breakers_on_disk() == ("screencapture",)
        assert bs.run_bounded(ok, timeout=5) is None              # refused
        time.sleep(1.2)                                           # cooldown
        assert bs.run_bounded(ok, timeout=10) is not None         # probe ok
        assert bs.open_breakers_on_disk() == (), "recovery must CLEAR it"

    def test_asking_whether_it_is_open_does_not_change_the_answer(
            self, monkeypatch):
        """`breaker_open` used to POP the state, so the success that followed
        found nothing open and never cleared the on-disk ledger — recovered
        in memory, tripped forever on the operator's screen. A question must
        not mutate."""
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "1")
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_COOLDOWN_S", "1")
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        bs.run_bounded(cmd, timeout=0.3)
        time.sleep(1.2)
        assert bs.breaker_open(cmd) is False        # cooldown elapsed
        assert bs.open_breakers_on_disk(), "the question erased the record"

    def test_a_fresh_process_inherits_a_live_trip(self, monkeypatch):
        """A daemon restarting into a still-wedged binary should honour the
        cooldown it already earned, not spend a worker rediscovering it."""
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_TRIPS", "1")
        bs.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                       timeout=0.3)
        bs._fails.clear(); bs._opened_at.clear()      # simulate a new process
        bs._hydrated = False
        assert bs.breaker_open([sys.executable]) is True
