"""A test session that boots the application dies alone — as a TEST failure.

bt-2026-09-21-225249: three candidate tests of ``jarvis_reload_manager.py``
each started a real JARVIS backend; the daemon's tree went 2.8 → 36.9 GB in
45 s and its watchdog stopped the daemon. The session should have died, not
the soak — and the candidate should have been told why.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import process_session as ps

pytestmark = pytest.mark.skipif(not Path("/proc").is_dir() or sys.platform == "win32", reason="needs /proc")

HOG = (
    "import time\n"
    "chunk = bytearray(64 * 1024 * 1024)\n"   # 64 MB resident
    "time.sleep(120)\n"
)


@pytest.fixture(autouse=True)
def _clean():
    ps.reset_budget_for_tests()
    yield
    ps.reset_budget_for_tests()


def _spawn(script_body: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script_body], start_new_session=True)


def _wait_rss_over(pid: int, mb: float, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ps._rss_kb(pid) / 1024.0 >= mb:
            return
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# The budget is derived, not written
# ---------------------------------------------------------------------------


def test_no_cap_means_no_budget():
    assert ps.session_budget_mb(1000.0, 3) == float("inf")


def test_one_session_gets_the_whole_headroom_and_three_share_it(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_BUDGET_FLOOR_MB", "1")
    ps.configure_tree_budget(10_000.0, 15.0)
    assert ps.session_budget_mb(4_000.0, 1) == pytest.approx(6_000.0)
    assert ps.session_budget_mb(4_000.0, 3) == pytest.approx(2_000.0)


def test_the_floor_keeps_a_crowded_moment_from_starving_everyone(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_BUDGET_FLOOR_MB", "700")
    ps.configure_tree_budget(10_000.0, 15.0)
    assert ps.session_budget_mb(9_900.0, 8) == 700.0


def test_polling_tightens_toward_the_cap():
    ps.configure_tree_budget(10_000.0, 15.0)
    assert ps._budget_interval_s(0.0) > ps._budget_interval_s(9_000.0) >= 1.0


# ---------------------------------------------------------------------------
# A session over budget is ended, alone, and the reason is kept
# ---------------------------------------------------------------------------


def test_a_hog_session_is_ended_and_its_neighbour_is_not(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_BUDGET_FLOOR_MB", "1")
    hog = _spawn(HOG)
    quiet = _spawn("import time; time.sleep(120)")
    try:
        ps.register_session(hog.pid, "hog")
        ps.register_session(quiet.pid, "quiet")
        _wait_rss_over(hog.pid, 50.0)
        # Cap = tree baseline + a little: the hog's 64 MB share is over it.
        total_kb, per = ps._tree_and_sessions_rss_kb([hog.pid, quiet.pid])
        base_mb = (total_kb - sum(per.values())) / 1024.0
        ps.configure_tree_budget(base_mb + 40.0, 15.0)
        ps._budget_tick()
        hog.wait(timeout=5)
        assert ps.over_budget(hog.pid) is not None
        assert ps.over_budget(hog.pid) is None, "the mark is consumed once"
        assert quiet.poll() is None, "a session within budget must be untouched"
        assert ps.over_budget(quiet.pid) is None
    finally:
        for p in (hog, quiet):
            ps.unregister_session(p.pid)
            if p.poll() is None:
                p.kill(); p.wait()


def test_a_budget_kill_is_not_confused_with_a_pressure_shed(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_BUDGET_FLOOR_MB", "1")
    hog = _spawn(HOG)
    try:
        ps.register_session(hog.pid, "hog")
        _wait_rss_over(hog.pid, 50.0)
        ps.configure_tree_budget(1.0, 15.0)
        ps._budget_tick()
        hog.wait(timeout=5)
        assert ps.was_shed(hog.pid) is False
        assert ps.over_budget(hog.pid) is not None
    finally:
        ps.unregister_session(hog.pid)
        if hog.poll() is None:
            hog.kill(); hog.wait()


@pytest.mark.asyncio
async def test_a_real_test_run_over_budget_is_a_TEST_failure_with_the_reason(tmp_path, monkeypatch):
    """No stub: a real TestRunner, a test that allocates like a booting app."""
    from backend.core.ouroboros.governance.test_runner import TestRunner

    monkeypatch.setenv("JARVIS_SESSION_BUDGET_FLOOR_MB", "1")
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_boots_the_app.py"
    test.write_text(
        "import time\n\ndef test_boot():\n"
        "    payload = bytearray(96 * 1024 * 1024)\n"
        "    time.sleep(30)\n    assert payload\n"
    )
    total_kb, _ = ps._tree_and_sessions_rss_kb([])
    ps.configure_tree_budget(total_kb / 1024.0 + 30.0, 0.2)
    monkeypatch.setenv("JARVIS_SESSION_BUDGET_POLL_FLOOR_S", "0.2")
    runner = TestRunner(repo_root=tmp_path, timeout=60.0, per_test_timeout_s=50)
    t0 = time.monotonic()
    result = await runner.run(test_files=(test,), sandbox_dir=None)
    assert time.monotonic() - t0 < 25, "the session was not ended promptly"
    assert result.passed is False
    assert result.timed_out is False, "must be the candidate's failure, not infra"
    assert "MemoryBudgetExceeded" in (result.stdout or "")
    assert "never start the application under test" in (result.stdout or "")
