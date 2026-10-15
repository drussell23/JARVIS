"""Trinity sibling bring-up: the organism starts J-Prime and Reactor-Core it needs."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import trinity_siblings as ts


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("JARVIS_PRIME_URL", "JARVIS_JPRIME_START_CMD", "JARVIS_REACTOR_START_CMD",
              "JARVIS_TRINITY_AUTOSTART", "REACTOR_CORE_API_URL", "JARVIS_LOCAL_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3-coder-ov:30b")
    monkeypatch.setenv("JARVIS_NUM_CTX_CEILING", "32768")
    monkeypatch.setenv("JARVIS_TRINITY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_JPRIME_START_BUDGET_S", "5")
    monkeypatch.setenv("JARVIS_REACTOR_START_BUDGET_S", "5")
    monkeypatch.setenv("JARVIS_VISION_MODEL_NAME", "jarvis-vision:8b")


@pytest.fixture(autouse=True)
def _no_live_judgment(monkeypatch):
    # The network is simulated, so is the code judgment: never the live engine.
    monkeypatch.setattr(ts, "sibling_staleness", lambda sib, base: None)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(up, spawned=None, comes_up_after=None, lent=None):
    clock = Clock()
    spawned = spawned if spawned is not None else []

    def probe(url, _timeout):
        if url in up:
            return True
        return comes_up_after is not None and url in comes_up_after and clock.t >= comes_up_after[url]

    def spawn(cmd, values):
        spawned.append((cmd, values))

    lines = []
    # The network is simulated: admission is too (never the live engine).
    out = ts.ensure_siblings(say=lines.append, probe=probe, spawn=spawn, sleep=clock.sleep, clock=clock,
                             lent=lambda sib, base: (lent or {}).get(sib.key, ""))
    return {s.key: s for s in out}, spawned, lines


def test_serving_siblings_are_left_alone():
    st, spawned, _ = _run({"http://127.0.0.1:8000/api/version", "http://127.0.0.1:8090/health"})
    assert st["jprime"].state == "serving" and st["reactor"].state == "serving"
    assert spawned == []


def test_down_sibling_is_started_with_template_filled_from_the_organisms_config(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "pwsh -File x.ps1 -Preload {model} -Port {port} -Ctx {ctx} -Coresident {vision_model}")
    st, spawned, _ = _run({"http://127.0.0.1:8090/health"},
                          comes_up_after={"http://127.0.0.1:8000/api/version": 3.0})
    assert st["jprime"].state == "started"
    cmd, values = spawned[0]
    assert values == {"model": "qwen3-coder-ov:30b", "port": "8000", "ctx": "32768",
                      "vision_model": "jarvis-vision:8b"}


def test_sibling_that_never_answers_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("JARVIS_REACTOR_START_CMD", "bash reactor_core.sh start")
    st, spawned, lines = _run({"http://127.0.0.1:8000/api/version"})
    assert st["reactor"].state == "failed" and "within 5s" in st["reactor"].detail
    assert len(spawned) == 1


def test_unconfigured_start_is_reported(monkeypatch):
    st, spawned, lines = _run(set())
    assert st["jprime"].state == "not_configured" and spawned == []
    assert any("(required)" in ln for ln in lines)


def test_jprime_skipped_when_local_lane_off(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    st, spawned, _ = _run({"http://127.0.0.1:8090/health"})
    assert st["jprime"].state == "skipped" and spawned == []


def test_prime_url_takes_precedence_like_the_lane_resolver(monkeypatch):
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://10.0.0.5:8000")
    st, _, _ = _run({"http://10.0.0.5:8000/api/version", "http://127.0.0.1:8090/health"})
    assert st["jprime"].url == "http://10.0.0.5:8000"


def test_master_switch_off_does_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_AUTOSTART", "false")
    st, spawned, _ = _run(set())
    assert st == {} and spawned == []


def test_spawn_error_never_raises(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")

    def boom(cmd, values):
        raise OSError("no such file")
    out = ts.ensure_siblings(say=lambda _l: None, probe=lambda u, t: u.endswith("/health"),
                             spawn=boom, sleep=lambda s: None, clock=lambda: 0.0)
    assert {s.key: s.state for s in out}["jprime"] == "failed"


def test_boot_calls_bring_up_before_the_lane_gate():
    """The call site is pinned: bring-up must precede the fatal lane gate."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "scripts" / "ouroboros_battle_test.py").read_text()
    assert src.index("ensure_siblings()") < src.index("    _check_api_keys_or_die()\n")


def test_a_lent_mind_is_reported_lent_and_never_restarted():
    st, spawned, lines = _run({"http://127.0.0.1:8000/api/version", "http://127.0.0.1:8090/health"},
                              lent={"jprime": "training cycle handoff-1 in TRAINING"})
    assert st["jprime"].state == "lent" and "handoff-1" in st["jprime"].detail
    assert spawned == [] and any("lent" in ln for ln in lines)


def test_serving_siblings_names_only_the_ones_that_answer():
    """Orphan cleanup spares exactly these; a down sibling is not protected."""
    answered = []

    def probe(url, timeout):
        answered.append(url)
        return url.startswith("http://127.0.0.1:8000")

    assert ts.serving_siblings(probe=probe) == ["http://127.0.0.1:8000"]
    assert any(u.endswith("/api/version") for u in answered)


def test_serving_sibling_pids_spares_the_listener_and_its_engines(monkeypatch):
    """Both boot sweeps (takeover orphans, heuristic zombies) consult this."""
    import psutil
    from types import SimpleNamespace as NS

    listen = NS(status=psutil.CONN_LISTEN, pid=100, laddr=NS(port=8000))
    other = NS(status=psutil.CONN_LISTEN, pid=200, laddr=NS(port=9999))
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [listen, other])
    monkeypatch.setattr(psutil, "Process",
                        lambda pid: NS(children=lambda recursive: [NS(pid=101), NS(pid=102)]))
    probe = lambda url, timeout: url.startswith("http://127.0.0.1:8000")
    assert ts.serving_sibling_pids(probe=probe) == {100, 101, 102}
    assert ts.serving_sibling_pids(probe=lambda u, t: False) == set()


# -- a sibling running older code is handed over ------------------------------------
JP = "http://127.0.0.1:8000/api/version"
RC = "http://127.0.0.1:8090/health"


def _stale_world(monkeypatch, *, inflight=(0,), killed=None, kill_ok=True):
    """J-Prime answers with older code until it is stopped, then comes up again
    once spawned."""
    from backend.core import daemon_handover as dh
    from backend.core import process_identity as pi
    state = {"alive": True, "spawned": False, "polls": list(inflight)}
    killed = killed if killed is not None else []
    stale = dh.Staleness(pid=4242, started_at=1.0, changed=["jarvis_prime/engine/engine_pool.py"],
                         what="J-Prime (Mind)")
    monkeypatch.setattr(ts, "sibling_staleness", lambda sib, base: stale if sib.key == "jprime" else None)

    def in_flight(sib, base):
        return state["polls"].pop(0) if len(state["polls"]) > 1 else state["polls"][0]
    monkeypatch.setattr(ts, "_in_flight", in_flight)

    def kill(pid, timeout):
        killed.append(pid)
        state["alive"] = not kill_ok
        return kill_ok
    monkeypatch.setattr(pi, "kill_process_tree", kill)

    def probe(url, _t):
        if url == JP:
            return state["alive"] or state["spawned"]
        return url == RC

    def spawn(cmd, values):
        state["spawned"] = True

    clock = Clock()
    lines = []
    out = ts.ensure_siblings(say=lines.append, probe=probe, spawn=spawn, sleep=clock.sleep,
                             clock=clock, lent=lambda sib, base: "")
    return {s.key: s for s in out}, state, killed, lines


def test_a_mind_running_older_code_is_restarted_from_its_checkout(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "pwsh -File x.ps1 -Port {port}")
    st, state, killed, lines = _stale_world(monkeypatch)
    assert killed == [4242] and state["spawned"]
    assert st["jprime"].state == "started"
    assert any("predates 1 changed source file(s): jarvis_prime/engine/engine_pool.py" in ln
               for ln in lines)


def test_a_busy_mind_finishes_its_requests_first(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    st, state, killed, _ = _stale_world(monkeypatch, inflight=(2, 1, 0))
    assert killed == [4242] and st["jprime"].state == "started"


def test_a_mind_still_serving_after_the_drain_window_is_kept(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    monkeypatch.setenv("JARVIS_SIBLING_HANDOVER_DRAIN_S", "3")
    st, state, killed, lines = _stale_world(monkeypatch, inflight=(1,))
    assert killed == [] and not state["spawned"]
    assert st["jprime"].state == "serving"
    assert any("kept running because it was still serving 1 request(s)" in ln for ln in lines)


def test_a_mind_that_cannot_say_what_it_serves_is_kept(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    st, _, killed, _ = _stale_world(monkeypatch, inflight=(None,))
    assert killed == [] and st["jprime"].state == "serving"


def test_without_a_start_command_a_stale_mind_is_never_stopped(monkeypatch):
    st, state, killed, _ = _stale_world(monkeypatch)
    assert killed == [] and st["jprime"].state == "serving"


def test_attach_policy_keeps_the_running_mind(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    monkeypatch.setenv("JARVIS_STALE_SIBLING_POLICY", "attach")
    st, _, killed, _ = _stale_world(monkeypatch)
    assert killed == [] and st["jprime"].state == "serving"


def test_the_sibling_policy_follows_the_kernels_unless_set(monkeypatch):
    monkeypatch.delenv("JARVIS_STALE_SIBLING_POLICY", raising=False)
    monkeypatch.setenv("JARVIS_STALE_DAEMON_POLICY", "attach")
    assert ts.stale_sibling_policy() == "attach"
    monkeypatch.setenv("JARVIS_STALE_SIBLING_POLICY", "replace")
    assert ts.stale_sibling_policy() == "replace"


def test_a_mind_that_will_not_stop_is_reported_and_kept(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    st, state, killed, lines = _stale_world(monkeypatch, kill_ok=False)
    assert killed == [4242] and not state["spawned"] and st["jprime"].state == "serving"
    assert any("could not be stopped" in ln for ln in lines)


# -- the judgment itself, against a real checkout and a real process ---------------
def _reporting(monkeypatch, code, cwd=None):
    monkeypatch.undo()   # drop the autouse stub for these
    if cwd is not None:
        monkeypatch.chdir(cwd)                      # this process "runs from" the checkout
    monkeypatch.setattr(ts, "_get_json", lambda url, t: {"code": code})
    sib = [s for s in ts.siblings() if s.key == "jprime"][0]
    return ts.sibling_staleness(sib, "http://127.0.0.1:8000")


def test_a_source_changed_after_the_engine_started_is_stale(monkeypatch, tmp_path):
    import os
    import time
    (tmp_path / "jarvis_prime").mkdir()
    src = tmp_path / "jarvis_prime" / "engine.py"
    src.write_text("x = 1\n")
    started = time.time() - 60
    os.utime(src, (started + 30, started + 30))
    stale = _reporting(monkeypatch, {"pid": os.getpid(), "root": str(tmp_path), "started_at": started,
                                     "sources": ["jarvis_prime/engine.py"]}, cwd=tmp_path)
    assert stale is not None and stale.changed == ["jarvis_prime/engine.py"]
    os.utime(src, (started - 30, started - 30))
    assert _reporting(monkeypatch, {"pid": os.getpid(), "root": str(tmp_path), "started_at": started,
                                    "sources": ["jarvis_prime/engine.py"]}, cwd=tmp_path) is None


def test_an_engine_on_another_host_is_never_judged(monkeypatch, tmp_path):
    # Across the WSL/Windows loopback the reported PID and path name nothing here.
    import os
    assert _reporting(monkeypatch, {"pid": os.getpid(), "root": r"C:\Users\x\prime" if os.sep == "/"
                                    else "/home/x/prime", "started_at": 1.0,
                                    "sources": ["jarvis_prime/engine.py"]}) is None


def test_bring_up_is_one_at_a_time_per_sibling():
    assert ts._bring_up_lock("jprime") is ts._bring_up_lock("jprime")
    assert ts._bring_up_lock("jprime") is not ts._bring_up_lock("reactor")


# -- runtime state: the adopting probe, reused by the supervisor's monitor ----
def test_runtime_state_serving_when_probe_answers():
    assert ts.sibling_runtime_state("jprime", probe=lambda u, t: u.endswith("/api/version"),
                                    lent=lambda s, b: "") == "serving"


def test_runtime_state_lent_is_not_down():
    assert ts.sibling_runtime_state("jprime", probe=lambda u, t: True,
                                    lent=lambda s, b: "training holds the lease") == "lent"


def test_runtime_state_down_when_probe_fails():
    assert ts.sibling_runtime_state("jprime", probe=lambda u, t: False,
                                    lent=lambda s, b: "") == "down"


def test_runtime_state_unknown_for_undeclared_sibling():
    assert ts.sibling_runtime_state("nope", probe=lambda u, t: True) == "unknown"


def test_runtime_state_never_raises():
    def _boom(u, t):
        raise RuntimeError("x")
    assert ts.sibling_runtime_state("jprime", probe=_boom) == "unknown"
