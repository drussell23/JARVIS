"""Autonomous daemon auto-spawn reflex.

`ov` boots `ouroboros_battle_test.py`, which has no audio pipeline; the mic
lives in `unified_supervisor.py`. Rather than blur the boundary by importing
the pipeline into the thin client, `ov` STARTS the process that owns the
hardware and subscribes over the existing UDS.

Three mandated assertions:

  (1) a dead socket triggers the spawn;
  (2) the backoff awaiter connects once the socket eventually binds;
  (3) a live socket on boot skips the spawn entirely.

Every seam is injected, so no test spawns a 98K-line kernel or binds a socket.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.cli import audio_daemon_reflex as reflex


class _Probe:
    """A socket that becomes live after N probes — models kernel boot delay."""

    def __init__(self, live_after: int = 0) -> None:
        self.calls = 0
        self._live_after = live_after

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._live_after


class _Spawn:
    def __init__(self, pid=4242) -> None:
        self.calls = 0
        self._pid = pid

    def __call__(self):
        self.calls += 1
        return self._pid


async def _no_sleep(_d: float) -> None:
    return None


# ---------------------------------------------------------------------------
# (3) live socket -> no spawn
# ---------------------------------------------------------------------------


async def test_live_socket_skips_the_spawn_entirely():
    """(3) The common case must cost ONE connect — no spawn, no sleep."""
    probe, spawn = _Probe(live_after=0), _Spawn()

    ok, reason = await reflex.ensure_audio_daemon(
        probe=probe, spawn=spawn, sleep=_no_sleep,
    )

    assert ok is True
    assert reason == "already_live"
    assert spawn.calls == 0, "spawned despite a healthy supervisor"
    assert probe.calls == 1, "probed more than once on the happy path"


# ---------------------------------------------------------------------------
# (1) dead socket -> spawn
# ---------------------------------------------------------------------------


async def test_dead_socket_triggers_the_spawn():
    """(1) No listener -> start the process that owns the hardware."""
    probe, spawn = _Probe(live_after=1), _Spawn()

    ok, reason = await reflex.ensure_audio_daemon(
        probe=probe, spawn=spawn, sleep=_no_sleep,
    )

    assert spawn.calls == 1, "did not spawn on a dead socket"
    assert ok is True and reason == "spawned"


async def test_spawn_failure_degrades_to_text_only():
    """A cockpit that cannot start audio must still be a working cockpit."""
    ok, reason = await reflex.ensure_audio_daemon(
        probe=_Probe(live_after=99), spawn=lambda: None, sleep=_no_sleep,
    )
    assert ok is False and reason == "spawn_failed"


async def test_reflex_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_AUDIO_AUTOSPAWN", "false")
    spawn = _Spawn()
    ok, reason = await reflex.ensure_audio_daemon(probe=_Probe(), spawn=spawn)
    assert ok is False and reason == "disabled"
    assert spawn.calls == 0


async def test_a_raising_probe_never_propagates():
    async def _boom() -> bool:
        raise OSError("socket layer exploded")

    ok, reason = await reflex.ensure_audio_daemon(
        probe=_boom, spawn=lambda: None, sleep=_no_sleep,
    )
    assert ok is False and reason in ("spawn_failed", "boot_timeout")


# ---------------------------------------------------------------------------
# (2) backoff awaiter
# ---------------------------------------------------------------------------


async def test_backoff_connects_once_the_socket_binds():
    """(2) The kernel takes time to listen; the awaiter must keep trying."""
    probe = _Probe(live_after=6)
    assert await reflex.await_socket(
        budget_s=60.0, probe=probe, sleep=_no_sleep,
    ) is True
    assert probe.calls == 7


async def test_backoff_gives_up_within_budget():
    """Bounded: an operator must eventually get a prompt back."""
    t = {"now": 0.0}

    async def _sleep(d):
        t["now"] += max(d, 0.01)

    ok = await reflex.await_socket(
        budget_s=5.0, probe=_Probe(live_after=10_000),
        sleep=_sleep, clock=lambda: t["now"],
    )
    assert ok is False
    assert t["now"] >= 5.0, "returned before exhausting the budget"


async def test_backoff_delays_grow_and_are_capped():
    """Exponential growth keeps a slow boot cheap; the cap keeps a long boot
    responsive."""
    t = {"now": 0.0}
    delays = []

    async def _sleep(d):
        delays.append(d)
        t["now"] += 0.001          # advance slowly so many attempts fit

    await reflex.await_socket(
        budget_s=30.0, probe=_Probe(live_after=8),
        sleep=_sleep, clock=lambda: t["now"],
    )
    assert delays, "never slept between probes"
    cap = reflex._backoff_cap_s()
    assert all(0.0 <= d <= cap + 1e-9 for d in delays), f"uncapped delay: {delays}"
    # Full jitter is uniform(0, delay), so assert the ENVELOPE grows rather
    # than each sample — sampling makes per-step monotonicity untestable.
    assert max(delays[len(delays) // 2:]) >= max(delays[:2]) or cap in delays


async def test_full_jitter_desynchronises_concurrent_cockpits():
    """Several `ov` instances started together must not probe in lockstep and
    hammer the booting kernel at identical instants."""
    runs = []
    for _ in range(6):
        delays = []

        async def _sleep(d, _s=delays):
            _s.append(d)

        await reflex.await_socket(
            budget_s=30.0, probe=_Probe(live_after=5), sleep=_sleep,
            clock=lambda: 0.0,
        )
        runs.append(tuple(delays))
    assert len(set(runs)) > 1, "all cockpits produced identical delay sequences"


async def test_final_probe_after_the_last_sleep():
    """The socket may bind during the final sleep — do not report failure
    without one last look."""
    t = {"now": 0.0}

    async def _sleep(d):
        t["now"] += 10.0           # blow the budget in one step

    probe = _Probe(live_after=1)
    assert await reflex.await_socket(
        budget_s=5.0, probe=probe, sleep=_sleep, clock=lambda: t["now"],
    ) is True


# ---------------------------------------------------------------------------
# spawn mechanics
# ---------------------------------------------------------------------------


def test_supervisor_path_resolves_from_the_module_not_the_cwd():
    """`ov` is launched from arbitrary directories, so a cwd-relative guess
    would break for every operator not sitting in the repo root."""
    p = reflex.supervisor_path()
    assert p is not None, "unified_supervisor.py not located"
    assert p.name == "unified_supervisor.py" and p.is_file()


def test_spawn_is_detached_and_silenced(monkeypatch):
    """Detached so the audio plane outlives an ephemeral cockpit; stdio
    silenced so a chatty boot cannot corrupt the TUI's terminal."""
    seen = {}

    class _P:
        pid = 999

    def _fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _P()

    monkeypatch.setattr(reflex.subprocess, "Popen", _fake_popen)
    pid = reflex.spawn_supervisor()

    assert pid == 999
    assert seen["argv"][1].endswith("unified_supervisor.py")
    assert seen["kw"]["start_new_session"] is True
    assert seen["kw"]["stdout"] == reflex.subprocess.DEVNULL
    assert seen["kw"]["stderr"] == reflex.subprocess.DEVNULL
    assert seen["kw"]["stdin"] == reflex.subprocess.DEVNULL


def test_spawn_never_raises_when_exec_is_refused(monkeypatch):
    def _boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(reflex.subprocess, "Popen", _boom)
    assert reflex.spawn_supervisor() is None


def test_missing_supervisor_script_returns_none(tmp_path):
    assert reflex.spawn_supervisor(script=tmp_path / "nope.py") is None


def test_no_duplicate_guard_is_taken_here(monkeypatch):
    """Deliberate: `unified_supervisor._fast_kernel_check()` exits early when a
    healthy kernel exists, so a duplicate spawn costs a short-lived process
    rather than a second audio owner. Racing cockpits converge without
    coordination — so spawn must NOT block or lock."""
    calls = []

    class _P:
        pid = 1

    monkeypatch.setattr(
        reflex.subprocess, "Popen",
        lambda argv, **kw: (calls.append(argv), _P())[1],
    )
    for _ in range(3):
        reflex.spawn_supervisor()
    assert len(calls) == 3, "spawn took a lock it should not have"


# ---------------------------------------------------------------------------
# probe semantics
# ---------------------------------------------------------------------------


async def test_probe_of_a_nonexistent_socket_is_false_not_an_exception(tmp_path):
    assert await reflex.probe_socket(tmp_path / "absent.sock", timeout=0.2) is False


async def test_probe_of_a_stale_socket_file_is_false(tmp_path):
    """A stale inode survives SIGKILL — file presence proves nothing, which is
    exactly why the reflex connects instead of stat-ing."""
    stale = tmp_path / "stale.sock"
    stale.write_bytes(b"")
    assert await reflex.probe_socket(stale, timeout=0.2) is False


async def test_probe_true_against_a_real_listener(tmp_path):
    """Positive control on a REAL unix socket — otherwise every probe test
    could pass simply because probing always returns False."""
    sock = tmp_path / "live.sock"

    async def _handler(_r, w):
        try:
            w.close()
        except Exception:
            pass

    try:
        server = await asyncio.start_unix_server(_handler, path=str(sock))
    except (PermissionError, OSError) as exc:
        pytest.skip(f"cannot bind a unix socket in this environment: {exc}")
    try:
        assert await reflex.probe_socket(sock, timeout=1.0) is True
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
